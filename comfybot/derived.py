"""Derived jobs: upscaling a result.

This module picks the source image and the size; the graph is the
derived component components/upscale. A plugin creates its own derived
jobs the same way: snapshot.for_derived(kind, ...) and then
rt.JOBS.enqueue_derived(job, kind, ...), with a derived component whose
id equals kind.
"""

import json
from pathlib import Path

from PIL import Image

from . import config, snapshot, runtime as rt


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def fit_in_box(width, height, box_w, box_h):
    """Size that fits in the box, keeping the proportions."""
    scale = min(box_w / width, box_h / height)
    return max(16, round(width * scale)), max(16, round(height * scale))


async def request_upscale(job):
    outs = json.loads(job["outputs"])
    if not outs:
        raise ValueError("This job has no output to upscale.")
    src = Path(outs[0])
    if src.suffix.lower() not in IMAGE_EXTS:
        raise ValueError("Upscale only works on images.")
    if not src.is_file():
        raise ValueError("The source image is no longer available.")
    with Image.open(src) as img:
        width, height = img.size
    out_w, out_h = fit_in_box(width, height, config.UPSCALE_W, config.UPSCALE_H)
    snap = snapshot.for_derived(
        "upscale", [str(src)], ["output"],
        f"🔍 Upscale {out_w}×{out_h} (from #{job['id']})",
        size={"width": out_w, "height": out_h},
    )
    job_id, created = rt.JOBS.enqueue_derived(job, "upscale", snap.to_json())
    if not created:
        return await rt.TG.text(
            job["chat"], f"Upscale already queued: job #{job_id}."
        )
    await rt.TG.text(
        job["chat"],
        f"🔍 Upscale {out_w}×{out_h} queued as job #{job_id}: "
        "it starts as soon as ComfyUI is free.",
    )
