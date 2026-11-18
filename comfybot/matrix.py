"""/matrix: a controlled comparison of a job.

Clones a job into variants that differ ONLY in light and/or resolution:
same prompt, same images, same character, same component and same seed.
The variants are ordinary queued jobs (at night, or right away with
"now"); each carries a label shown at start and on delivery.

Usage: /matrix <ID> light=window,sun size=384x640,768x1280 seed=123 [now]
"""

import random

from . import confirmation, jobs, lighting, runtime as rt

MAX_VARIANTS = 12
USAGE = (
    "Usage: /matrix <ID> light=window,sun size=384x640,768x1280 "
    "[seed=N] [now]\n"
    "At least one of light and size. Same prompt, images and seed for "
    "every variant."
)


def parse_size(text):
    try:
        w, h = text.lower().split("x")
        w, h = int(w), int(h)
    except ValueError:
        raise ValueError(f"Invalid size {text!r}: use WIDTHxHEIGHT.")
    if not (64 <= w <= 4096 and 64 <= h <= 4096):
        raise ValueError(f"Size {text!r} out of range (64–4096).")
    return {"width": w, "height": h}


def parse_args(args):
    """args: the words after the ID. Returns (lights, sizes, seed, now)."""
    lights, sizes, seed, now = [], [], None, False
    for arg in args:
        key, _, value = arg.partition("=")
        key = key.lower()
        if not value:
            if key == "now":
                now = True
                continue
            raise ValueError(USAGE)
        values = [v for v in value.split(",") if v]
        if key == "light":
            lights += [lighting.parse(v) for v in values]
        elif key == "size":
            sizes += [parse_size(v) for v in values]
        elif key == "seed":
            if not value.isdigit():
                raise ValueError("seed must be a whole number >= 0.")
            seed = int(value)
        else:
            raise ValueError(USAGE)
    if not lights and not sizes:
        raise ValueError(USAGE)
    return lights, sizes, seed, now


def _label(source_id, light, size, seed):
    bits = [f"variant of #{source_id}"]
    if light:
        bits.append(f"light {light}")
    if size:
        bits.append(f"{size['width']}×{size['height']}")
    bits.append(f"seed {seed}")
    return " · ".join(bits)


async def run(chat, owner, source_id, args):
    source = rt.JOBS.owned(source_id, owner)
    if source["kind"] != "generate":
        raise ValueError("Variants are made from an ordinary job, not a derived one.")
    if not source["component"]:
        raise ValueError(f"Choose the workflow of job #{source_id} first.")
    if not source["prompt"].strip():
        raise ValueError(f"Job #{source_id} has no prompt.")
    lights, sizes, seed, now = parse_args(args)
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    combos = [
        (light, size)
        for light in (lights or [None])
        for size in (sizes or [None])
    ]
    if len(combos) > MAX_VARIANTS:
        raise ValueError(
            f"{len(combos)} variants: the maximum is {MAX_VARIANTS}."
        )

    created = []
    for light, size in combos:
        overrides = {"seed": seed}
        if size:
            overrides["size"] = size
        overrides["label"] = _label(source_id, light, size, seed)
        job_id = rt.JOBS.create_variant(
            source, chat, light or source["light"], overrides, now,
        )
        created.append((job_id, overrides["label"]))

    lines = [f"🧪 Comparison of #{source_id}: {len(created)} variants, seed {seed}."]
    for job_id, label in created:
        try:
            outcome = confirmation.confirm(job_id, owner)
            why = jobs.reason(outcome)
        except Exception as exc:
            why = str(exc)
        if why:
            # A variant that cannot start is not left behind as a draft.
            rt.JOBS.cancel(job_id, error=why[:300])
            lines.append(f"  #{job_id} ✗ {why[:150]}")
            continue
        lines.append(f"  #{job_id} queued — {label.split(' · ', 1)[1]}")
    lines.append(
        "They start right away." if now
        else "They start in the night window (/queue for the status)."
    )
    await rt.TG.text(chat, "\n".join(lines))
