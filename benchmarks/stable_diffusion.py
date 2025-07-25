# https://arxiv.org/pdf/2112.10752.pdf
# https://github.com/ekagra-ranjan/huggingface-blog/blob/main/stable_diffusion.md
import tempfile
from pathlib import Path
import argparse
from collections import namedtuple
from typing import Dict, Any
import time
import statistics
import csv
import json

from PIL import Image
import numpy as np
from tinygrad import Device, GlobalCounters, dtypes, Tensor, TinyJit
from tinygrad.helpers import Timing, Context, getenv, fetch, colored, tqdm
from tinygrad.nn import Conv2d, GroupNorm
from tinygrad.nn.state import torch_load, load_state_dict, get_state_dict
from extra.models.clip import Closed, Tokenizer
from extra.models.unet import UNetModel
from extra.bench_log import BenchEvent, WallTimeEvent

class AttnBlock:
  def __init__(self, in_channels):
    self.norm = GroupNorm(32, in_channels)
    self.q = Conv2d(in_channels, in_channels, 1)
    self.k = Conv2d(in_channels, in_channels, 1)
    self.v = Conv2d(in_channels, in_channels, 1)
    self.proj_out = Conv2d(in_channels, in_channels, 1)

  def __call__(self, x):
    h_ = self.norm(x)
    q, k, v = self.q(h_), self.k(h_), self.v(h_)
    b, c, h, w = q.shape
    q, k, v = [y.reshape(b, c, h*w).transpose(1, 2) for y in (q, k, v)]
    h_ = Tensor.scaled_dot_product_attention(q, k, v).transpose(1, 2).reshape(b, c, h, w)
    return x + self.proj_out(h_)

class ResnetBlock:
  def __init__(self, in_channels, out_channels=None):
    self.norm1 = GroupNorm(32, in_channels)
    self.conv1 = Conv2d(in_channels, out_channels, 3, padding=1)
    self.norm2 = GroupNorm(32, out_channels)
    self.conv2 = Conv2d(out_channels, out_channels, 3, padding=1)
    self.nin_shortcut = Conv2d(in_channels, out_channels, 1) if in_channels != out_channels else lambda x: x

  def __call__(self, x):
    h = self.conv1(self.norm1(x).swish())
    h = self.conv2(self.norm2(h).swish())
    return self.nin_shortcut(x) + h

class Mid:
  def __init__(self, block_in):
    self.block_1 = ResnetBlock(block_in, block_in)
    self.attn_1 = AttnBlock(block_in)
    self.block_2 = ResnetBlock(block_in, block_in)

  def __call__(self, x):
    return x.sequential([self.block_1, self.attn_1, self.block_2])

class Decoder:
  def __init__(self):
    sz = [(128, 256), (256, 512), (512, 512), (512, 512)]
    self.conv_in = Conv2d(4, 512, 3, padding=1)
    self.mid = Mid(512)

    arr = []
    for i, s in enumerate(sz):
      arr.append({
        "block": [
          ResnetBlock(s[1], s[0]),
          ResnetBlock(s[0], s[0]),
          ResnetBlock(s[0], s[0])
        ]
      })
      if i != 0:
        arr[-1]["upsample"] = {"conv": Conv2d(s[0], s[0], 3, padding=1)}
    self.up = arr

    self.norm_out = GroupNorm(32, 128)
    self.conv_out = Conv2d(128, 3, 3, padding=1)

  def __call__(self, x):
    x = self.conv_in(x)
    x = self.mid(x)

    for l in self.up[::-1]:
      print("decode", x.shape)
      for b in l["block"]:
        x = b(x)
      if "upsample" in l:
        bs, c, py, px = x.shape
        x = x.reshape(bs, c, py, 1, px, 1).expand(bs, c, py, 2, px, 2).reshape(bs, c, py*2, px*2)
        x = l["upsample"]["conv"](x)
      x.realize()

    return self.conv_out(self.norm_out(x).swish())

class Encoder:
  def __init__(self):
    sz = [(128, 128), (128, 256), (256, 512), (512, 512)]
    self.conv_in = Conv2d(3, 128, 3, padding=1)

    arr = []
    for i, s in enumerate(sz):
      arr.append({
        "block": [
          ResnetBlock(s[0], s[1]),
          ResnetBlock(s[1], s[1])
        ]
      })
      if i != 3:
        arr[-1]["downsample"] = {"conv": Conv2d(s[1], s[1], 3, stride=2, padding=(0,1,0,1))}
    self.down = arr

    self.mid = Mid(512)
    self.norm_out = GroupNorm(32, 512)
    self.conv_out = Conv2d(512, 8, 3, padding=1)

  def __call__(self, x):
    x = self.conv_in(x)
    for l in self.down:
      print("encode", x.shape)
      for b in l["block"]:
        x = b(x)
      if "downsample" in l:
        x = l["downsample"]["conv"](x)
    x = self.mid(x)
    return self.conv_out(self.norm_out(x).swish())

class AutoencoderKL:
  def __init__(self):
    self.encoder = Encoder()
    self.decoder = Decoder()
    self.quant_conv = Conv2d(8, 8, 1)
    self.post_quant_conv = Conv2d(4, 4, 1)

  def __call__(self, x):
    latent = self.encoder(x)
    latent = self.quant_conv(latent)
    latent = latent[:, 0:4]  # only the means
    print("latent", latent.shape)
    latent = self.post_quant_conv(latent)
    return self.decoder(latent)

def get_alphas_cumprod(beta_start=0.00085, beta_end=0.0120, n_training_steps=1000):
  betas = np.linspace(beta_start ** 0.5, beta_end ** 0.5, n_training_steps, dtype=np.float32) ** 2
  alphas = 1.0 - betas
  alphas_cumprod = np.cumprod(alphas, axis=0)
  return Tensor(alphas_cumprod)

unet_params: Dict[str, Any] = {
  "adm_in_ch": None,
  "in_ch": 4,
  "out_ch": 4,
  "model_ch": 320,
  "attention_resolutions": [4, 2, 1],
  "num_res_blocks": 2,
  "channel_mult": [1, 2, 4, 4],
  "n_heads": 8,
  "transformer_depth": [1, 1, 1, 1],
  "ctx_dim": 768,
  "use_linear": False,
}

class StableDiffusion:
  def __init__(self):
    self.alphas_cumprod = get_alphas_cumprod()
    self.model = namedtuple("DiffusionModel", ["diffusion_model"])(
      diffusion_model=UNetModel(**unet_params)
    )
    self.first_stage_model = AutoencoderKL()
    self.cond_stage_model = namedtuple("CondStageModel", ["transformer"])(
      transformer=namedtuple("Transformer", ["text_model"])(
        text_model=Closed.ClipTextTransformer()
      )
    )

  def get_x_prev_and_pred_x0(self, x, e_t, a_t, a_prev):
    sigma_t = 0
    sqrt_one_minus_at = (1 - a_t).sqrt()
    pred_x0 = (x - sqrt_one_minus_at * e_t) / a_t.sqrt()
    dir_xt = (1. - a_prev - sigma_t**2).sqrt() * e_t
    x_prev = a_prev.sqrt() * pred_x0 + dir_xt
    return x_prev, pred_x0

  def get_model_output(self, unconditional_context, context, latent, timestep, unconditional_guidance_scale):
    latents = self.model.diffusion_model(
      latent.expand(2, *latent.shape[1:]),
      timestep,
      unconditional_context.cat(context, dim=0)
    )
    unconditional_latent, latent = latents[0:1], latents[1:2]
    e_t = unconditional_latent + unconditional_guidance_scale * (latent - unconditional_latent)
    return e_t

  def decode(self, x):
    x = self.first_stage_model.post_quant_conv(1/0.18215 * x)
    x = self.first_stage_model.decoder(x)
    x = (x + 1.0) / 2.0
    x = x.reshape(3, 512, 512).permute(1, 2, 0).clip(0, 1) * 255
    return x.cast(dtypes.uint8)

  def __call__(self, unconditional_context, context, latent, timestep, alphas, alphas_prev, guidance):
    e_t = self.get_model_output(unconditional_context, context, latent, timestep, guidance)
    x_prev, _ = self.get_x_prev_and_pred_x0(latent, e_t, alphas, alphas_prev)
    return x_prev.realize()

if __name__ == "__main__":
    default_prompt = "a horse sized cat eating a bagel"
    parser = argparse.ArgumentParser(
        description="Run Stable Diffusion",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--steps",    type=int,   default=6,   help="Number of steps in diffusion")
    parser.add_argument("--out",      type=str,   default=None,
                        help="Output filename or directory")
    parser.add_argument("--noshow",   action="store_true", help="Do not show the image")
    parser.add_argument("--fp16",     action="store_true", help="Cast the weights to float16")
    parser.add_argument("--timing",   action="store_true", help="Print timing per step")
    parser.add_argument("--seed",     type=int,   help="Set the random latent seed")
    parser.add_argument("--guidance", type=float, default=7.5, help="Prompt strength")
    args = parser.parse_args()

    # load prompts from CSV in the same directory
    csv_path = Path(__file__).parent / "image_generation_prompts.csv"
    prompts = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            prompts.append(row.get("Prompt", default_prompt))

    # prepare model
    model = StableDiffusion()
    with WallTimeEvent(BenchEvent.LOAD_WEIGHTS):
        sd = torch_load(fetch(
            "https://huggingface.co/CompVis/stable-diffusion-v-1-4-original/"
            "resolve/main/sd-v1-4.ckpt",
            "sd-v1-4.ckpt"
        ))["state_dict"]
        load_state_dict(model, sd, strict=False)
        if args.fp16:
            for k, v in get_state_dict(model).items():
                if k.startswith("model"):
                    v.replace(v.cast(dtypes.float16).realize())

    # timesteps setup
    timesteps = list(range(1, 1000, 1000 // args.steps))
    alphas = model.alphas_cumprod[Tensor(timesteps)]
    alphas_prev = Tensor([1.0]).cat(alphas[:-1])

    @TinyJit
    def run(model, *x):
        return model(*x).realize()

    # storage for stats
    first_time = first_peak = first_ips = None
    subsequent_times, subsequent_peaks, subsequent_ips = [], [], []

    tokenizer = Tokenizer.ClipTokenizer()

    for idx, prompt_text in enumerate(prompts, start=1):
        print(f"\n=== Prompt {idx}/{len(prompts)}: {prompt_text}")

        # encode contexts
        ids = tokenizer.encode(prompt_text)
        context = model.cond_stage_model.transformer.text_model(Tensor([ids])).realize()
        uncond_ids = tokenizer.encode("")
        uncond_tensor = Tensor([uncond_ids])
        uncond = (
            model.cond_stage_model.transformer.text_model(uncond_tensor)
        ).realize()

        if args.seed is not None:
            Tensor.manual_seed(args.seed + idx)
        latent = Tensor.randn(1, 4, 64, 64)

        # run diffusion
        peak_vram = 0
        start = time.perf_counter()
        for step_idx, tval in (t := tqdm(list(enumerate(timesteps))[::-1], desc=f"Run {idx}")):
            GlobalCounters.reset()
            t.set_description(f"{step_idx:3d} {tval:3d}")
            with WallTimeEvent(BenchEvent.STEP):
                latent = run(
                    model, uncond, context, latent,
                    Tensor([tval]), alphas[Tensor([step_idx])], alphas_prev[Tensor([step_idx])],
                    Tensor([args.guidance])
                )
            peak_vram = max(peak_vram, GlobalCounters.mem_used)
        elapsed = time.perf_counter() - start
        ips = len(timesteps) / elapsed

        # capture stats
        if idx == 1:
            first_time, first_peak, first_ips = elapsed, peak_vram, ips
        else:
            subsequent_times.append(elapsed)
            subsequent_peaks.append(peak_vram)
            subsequent_ips.append(ips)

        # decode and save
        arr = model.decode(latent).numpy()
        img = Image.fromarray(arr)
        safe = "".join(c if c.isalnum() else "_" for c in prompt_text)[:50]
        out_dir = Path(args.out) if args.out and Path(args.out).is_dir() else Path(tempfile.gettempdir())
        out_file = out_dir / f"{safe}.png"
        img.save(out_file)
        if not args.noshow:
            img.show()

        print(f"Run {idx}: {elapsed:.2f}s, peak VRAM {peak_vram/1e9:.2f} GB, {ips:.2f} iters/s")

    # summary
    print("\nBenchmark Summary:")
    print(f"First run: {first_time:.2f}s, {first_peak/1e9:.2f} GB, {first_ips:.2f} iters/s")
    if subsequent_times:
        avg_t = statistics.mean(subsequent_times)
        avg_p = statistics.mean(subsequent_peaks)/1e9
        avg_i = statistics.mean(subsequent_ips)
        print(f"Subsequent runs (avg over {len(subsequent_times)}):")
        print(f"  Time: {avg_t:.2f}s (min {min(subsequent_times):.2f}, max {max(subsequent_times):.2f})")
        print(f"  VRAM: {avg_p:.2f} GB (min {min(subsequent_peaks)/1e9:.2f}, max {max(subsequent_peaks)/1e9:.2f})")
        print(f"  Iters/sec: {avg_i:.2f} (min {min(subsequent_ips):.2f}, max {max(subsequent_ips):.2f})")

    # Build dictionary for JSON output
    benchmark_result = {
        "first_run": {
            "time_seconds": round(first_time, 2),
            "peak_vram_gb": round(first_peak / 1e9, 2),
            "iterations_per_second": round(first_ips, 2)
        }
    }

    if subsequent_times:
        benchmark_result["subsequent_runs"] = {
            "average": {
                "time_seconds": round(avg_t, 2),
                "peak_vram_gb": round(avg_p, 2),
                "iterations_per_second": round(avg_i, 2),
            },
            "min": {
                "time_seconds": round(min(subsequent_times), 2),
                "peak_vram_gb": round(min(subsequent_peaks) / 1e9, 2),
                "iterations_per_second": round(min(subsequent_ips), 2),
            },
            "max": {
                "time_seconds": round(max(subsequent_times), 2),
                "peak_vram_gb": round(max(subsequent_peaks) / 1e9, 2),
                "iterations_per_second": round(max(subsequent_ips), 2),
            },
            "num_runs": len(subsequent_times)
        }

    # Save to JSON file
    output_json_path = Path(__file__).parent / "sdv_stats.json"
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(benchmark_result, f, indent=2)

    print(f"\nBenchmark results saved to: {output_json_path}")