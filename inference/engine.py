"""Local inference using CatVTON Mix attention and the SD 1.5 inpainting U-Net."""
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from diffusers import AutoencoderKL, DDIMScheduler, UNet2DConditionModel
from diffusers.models.attention_processor import AttnProcessor2_0, SlicedAttnProcessor
from diffusers.utils.torch_utils import randn_tensor
from PIL import Image, ImageFilter, ImageOps
from safetensors.torch import load_file
from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor

from .masking import blending_mask, garment_mask, inference_region, prepare_garment


@dataclass(frozen=True)
class Settings:
    width: int = 384
    height: int = 512
    steps: int = 25
    seed: int = 42
    guidance: float = 2.5

    def __post_init__(self):
        if any(value < 128 or value > 1024 or value % 64 for value in (self.width, self.height)):
            raise ValueError("width and height must be multiples of 64 between 128 and 1024")
        if not 1 <= self.steps <= 100 or not 0 <= self.seed < 2**63 or not 1 <= self.guidance <= 10:
            raise ValueError("Invalid sampling steps, guidance or seed")


class SelfAttention(torch.nn.Module):
    """Keep the same registered module layout as the published attention checkpoint."""

    def __init__(self):
        super().__init__()
        self.operation = AttnProcessor2_0()
        self.sliced = SlicedAttnProcessor(slice_size=1)

    def forward(self, attn, hidden_states, encoder_hidden_states=None, attention_mask=None):
        # Large MPS attention matrices can exceed the unified-memory limit in a single allocation.
        operation = self.sliced if hidden_states.device.type == "mps" and hidden_states.shape[1] > 6144 else self.operation
        return operation(attn, hidden_states, encoder_hidden_states, attention_mask)


class SkipTextAttention(torch.nn.Module):
    def forward(self, attn, hidden_states, encoder_hidden_states=None, attention_mask=None):
        return hidden_states


def load_attention(unet, path):
    processors = {
        name: SelfAttention() if name.endswith("attn1.processor") else SkipTextAttention()
        for name in unet.attn_processors
    }
    unet.set_attn_processor(processors)
    # The original exporter numbers all modules below attn1, including child projections.
    # Safetensors retains one name for each shared tensor. Require full parameter coverage.
    layers = torch.nn.ModuleList(module for name, module in unet.named_modules() if "attn1" in name)
    parameters = dict(layers.named_parameters(remove_duplicate=False))
    loaded = set()
    with torch.no_grad():
        for name, value in load_file(str(path)).items():
            target = parameters[name]
            if target.shape != value.shape:
                raise ValueError(f"Invalid CatVTON attention tensor: {name}")
            target.copy_(value)
            loaded.add(id(target))
    if loaded != {id(parameter) for parameter in parameters.values()}:
        raise ValueError("CatVTON checkpoint does not cover all self-attention weights")


def choose_device(requested):
    available = {"cpu": True, "cuda": torch.cuda.is_available(), "mps": torch.backends.mps.is_available()}
    if requested == "auto":
        return next(name for name in ("cuda", "mps", "cpu") if available[name])
    if not available.get(requested):
        raise ValueError(f"Requested compute device unavailable: {requested}")
    return requested


class TryOnEngine:
    def __init__(self, root: Path, device: str, settings: Settings):
        self.device = choose_device(device)
        self.settings = settings
        self.dtype = torch.float32 if self.device == "cpu" else torch.float16
        local = {"local_files_only": True}
        self.unet = UNet2DConditionModel.from_pretrained(
            root / "base/unet", variant="fp16", torch_dtype=self.dtype, use_safetensors=True, **local
        )
        load_attention(self.unet, root / "attention/mix-48k-1024/attention/model.safetensors")
        self.unet.eval().to(self.device)
        self.vae = AutoencoderKL.from_pretrained(root / "vae", use_safetensors=True, **local).eval().to(self.device)
        self.scheduler = DDIMScheduler.from_pretrained(root / "base/scheduler", **local)
        parser_config = json.loads((root / "parser/preprocessor_config.json").read_text())
        # The pinned checkpoint predates the feature extractor to image processor rename.
        parser_config.pop("feature_extractor_type", None)
        parser_config["do_reduce_labels"] = parser_config.pop("reduce_labels", False)
        self.processor = SegformerImageProcessor(**parser_config)
        self.segmenter = SegformerForSemanticSegmentation.from_pretrained(
            root / "parser", use_safetensors=True, **local
        ).eval().to(self.device)

    def _encode(self, image, generator):
        posterior = self.vae.encode(image.to(self.device, torch.float32)).latent_dist
        return (posterior.sample(generator=generator) * self.vae.config.scaling_factor).to(self.dtype)

    @torch.inference_mode()
    def generate(self, person, garment, category):
        cfg = self.settings
        size = (cfg.width, cfg.height)
        person = ImageOps.fit(person, size, Image.Resampling.LANCZOS)
        garment, exposed_arms = prepare_garment(garment)
        short_lower = category == "lower" and garment.height <= garment.width * 1.05
        garment = ImageOps.pad(garment, size, Image.Resampling.LANCZOS, color="white")
        pixels = self.processor(images=person, return_tensors="pt").pixel_values.to(self.device)
        segmentation = self.segmenter(pixel_values=pixels)
        labels = self.processor.post_process_semantic_segmentation(
            segmentation, target_sizes=[(cfg.height, cfg.width)]
        )[0].cpu().numpy()
        full_person = person
        full_mask = garment_mask(labels, category, exposed_arms)
        region = inference_region(full_mask, category)
        person = full_person.crop(region).resize(size, Image.Resampling.LANCZOS)
        mask = full_mask.crop(region).resize(size, Image.Resampling.NEAREST)
        mask_tensor = torch.from_numpy(np.asarray(mask).copy()).float()[None, None] / 255

        def image_tensor(image):
            array = np.array(image, dtype=np.float32) / 127.5 - 1.0
            return torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0)

        generator = torch.Generator(device="cpu").manual_seed(cfg.seed)
        person_latent = self._encode(image_tensor(person) * (mask_tensor < 0.5), generator)
        garment_latent = self._encode(image_tensor(garment), generator)
        mask_latent = torch.nn.functional.interpolate(
            mask_tensor, size=person_latent.shape[-2:], mode="nearest"
        ).to(self.device, self.dtype)
        # Stack person and garment vertically as in the original mask-based CatVTON.
        condition = torch.cat((person_latent, garment_latent), dim=2)
        unconditioned = torch.cat((person_latent, torch.zeros_like(garment_latent)), dim=2)
        conditions = torch.cat((unconditioned, condition))
        spatial_mask = torch.cat((mask_latent, torch.zeros_like(mask_latent)), dim=2).repeat(2, 1, 1, 1)
        latents = randn_tensor(condition.shape, generator=generator, device=torch.device(self.device), dtype=self.dtype)
        self.scheduler.set_timesteps(cfg.steps, device=self.device)
        latents *= self.scheduler.init_noise_sigma
        for step in self.scheduler.timesteps:
            noisy = self.scheduler.scale_model_input(torch.cat((latents, latents)), step)
            network_input = torch.cat((noisy, spatial_mask, conditions), dim=1)
            base, guided = self.unet(network_input, step, encoder_hidden_states=None).sample.chunk(2)
            noise = base + cfg.guidance * (guided - base)
            latents = self.scheduler.step(noise, step, latents, eta=1.0, generator=generator).prev_sample

        output = latents[:, :, :person_latent.shape[2], :].float() / self.vae.config.scaling_factor
        decoded = self.vae.decode(output).sample[0].permute(1, 2, 0)
        if not torch.isfinite(decoded).all().item():
            raise RuntimeError("Non-finite model output")
        rgb = ((decoded / 2 + 0.5).clamp(0, 1) * 255).round().byte().cpu().numpy()
        left, top, right, bottom = region
        patch = Image.fromarray(rgb).resize((right - left, bottom - top), Image.Resampling.LANCZOS)
        result = full_person.copy()
        paste_mask = blending_mask(full_mask, labels, short_lower)
        result.paste(patch, region, paste_mask.crop(region).filter(ImageFilter.GaussianBlur(2)))
        return result
