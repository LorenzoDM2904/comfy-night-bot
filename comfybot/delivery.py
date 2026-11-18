"""Start messages and delivery of results."""

import json
import logging
from pathlib import Path

from . import buttons, plugins, runtime as rt
from .snapshot import Snapshot

log = logging.getLogger("nightbot")


def social_keyboard(job):
    """Extension point. No social network is wired in."""
    return []


async def started_message(job_id):
    """Start notice: a short summary of what the job does, with the
    snapshot's effective parameters."""
    job = rt.JOBS.get(job_id)
    snap = Snapshot.from_json(job["snapshot"])
    positive = snap.positive.strip()

    lines = [f"⚙️ Job #{job_id} · {snap.title or snap.name} · RUNNING"]
    if positive:
        one = " ".join(positive.split())
        lines.append(f"📝 Prompt: “{one[:180]}”")
    if snap.light and snap.light != "off":
        lines.append(f"💡 Light: {snap.light}")
    if snap.variant:
        lines.append(f"🧪 {snap.variant}")
    if snap.assets:
        lines.append(f"🖼️ {len(snap.assets)} images uploaded to ComfyUI")
    details = describe_params(snap.effective_params())
    if details:
        lines.append("⚙️ " + details)
    lines.append("⏳ Checking the status: I will write when it is done.")

    await rt.TG.text(job["chat"], "\n".join(lines))


def describe_params(params):
    """Effective parameters on one line: model, resolution, steps..."""
    bits = []
    if params.get("model"):
        bits.append(f"model {params['model']}")
    if params.get("width") and params.get("height"):
        bits.append(f"{params['width']}×{params['height']} px")
    if params.get("length"):
        bits.append(f"{params['length']} frames")
    if params.get("fps"):
        bits.append(f"{params['fps']} fps")
    if params.get("steps") is not None:
        bits.append(f"{params['steps']} steps")
    if params.get("sampler"):
        bits.append(f"{params['sampler']}/{params.get('scheduler') or '?'}")
    if params.get("cfg") is not None:
        bits.append(f"cfg {params['cfg']:g}")
    if params.get("denoise") is not None:
        bits.append(f"denoise {params['denoise']:g}")
    if isinstance(params.get("seed"), int):
        bits.append(f"seed {params['seed']}")
    return " · ".join(bits)


VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv")


def result_keyboard(job):
    """Buttons under a delivered image."""
    if job["kind"] in plugins.kinds(intermediate=True):
        return None
    kb = list(social_keyboard(job) or [])
    row = []
    if job["kind"] != "upscale":
        row.append(buttons.upscale(job["id"]))
    row += plugins.result_buttons(job)
    if row:
        kb.append(row)
    return kb or None


async def deliver(job_id):
    """For each image: a preview (a photo, compressed by Telegram) and
    the original as a document. Videos are sent as videos."""
    job = rt.JOBS.get(job_id)
    outputs = json.loads(job["outputs"])
    snap = Snapshot.from_json(job["snapshot"])
    if snap.title:
        text = f"Job #{job_id} · {snap.title}"
    else:
        text = f"Job #{job_id} done."
    if snap.variant:
        text += f"\n🧪 {snap.variant}"
    if snap.caption:
        text += "\n\nSocial caption:\n" + snap.caption

    # Mark the delivery before touching the network: a crash does not cause
    # automatic retries that could duplicate it. /result delivers again on
    # request.
    if not rt.JOBS.begin_delivery(job_id):
        raise ValueError(f"Delivery of job #{job_id} already in progress.")
    try:
        for index, path in enumerate(outputs):
            first = index == 0
            label = text if first else f"Job #{job_id}, output {index + 1}"
            if Path(path).suffix.lower() in VIDEO_EXTS:
                await rt.TG.file(job["chat"], path, label, video=True)
                continue
            keyboard = result_keyboard(job) if first else None
            try:
                await rt.TG.file(
                    job["chat"], path, label, photo=True, keyboard=keyboard,
                )
                if job["kind"] in plugins.kinds(intermediate=True):
                    continue    # an intermediate result: the preview is enough
                label, keyboard = "📎 Original", None
            except Exception:
                # The preview is optional: text and buttons move to the
                # original, which is delivered anyway.
                log.warning("Preview not sent for job %s", job_id)
            await rt.TG.file(job["chat"], path, label, keyboard=keyboard)
        rt.JOBS.finish_delivery(job_id, sent=True)
    except Exception:
        rt.JOBS.finish_delivery(job_id, sent=False)
        log.exception("Uncertain delivery for job %s; use /result", job_id)
