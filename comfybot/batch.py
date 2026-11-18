"""/list and operations on ranges of jobs (/add X..Y, Start all)."""

import json
from pathlib import Path

from . import buttons, catalog, characters, config, confirmation, images, jobs
from . import runtime as rt
from .linkfetch import extract_urls


LIST_MAX = 50


LIST_SCHEMA = """\
📋 /list — starts a batch of generations from JSON.

USAGE:
  /list                 -> shows this schema
  /list { ...json... }  -> inline JSON
  /list as a reply to a .json file

FORMAT (JSON):
{
  "image": "https://pin.it/xxxx or an image URL",  (optional)
  "character": "name",                             (optional)
  "prompts": [
    { "prompt": "...", "workflow": "component id" },
    { "prompt": "...", "character": "another" },
    { "prompt": "..." }
  ]
}

- "image": a Pinterest link or image URL used for ALL the prompts
  whose workflow takes a photo. Alternatively, reply to a photo with
  /list: that is the shared image.
- "character": a character (see /characters) for ALL the prompts:
  their reference photos and caption go to every job. With a
  character, "image" is not needed: the jobs are queued right away.
- "prompts": a required list (1-50). Each entry:
  - "prompt" (required): the text.
  - "workflow" (optional): an enabled component (see /workflows).
    If missing: the first suitable one in AUTO_COMPONENTS in the .env
    (one that takes photos when there is an image), or the only
    compatible one.
  - "character" (optional): the character for this entry only,
    instead of the general one; "off" for no character.

Each entry creates a draft and queues it: they run one after the
other in the night window. The shared image only goes to the
workflows that use it.
"""


def _auto_with_photo(comps):
    """The first component in AUTO_COMPONENTS that takes photos, or ""."""
    return next(
        (cid for cid in config.AUTO_COMPONENTS
         if cid in comps and comps[cid].get("images")),
        "",
    )


async def handle_list(req):
    """/list: a batch of generations from JSON (inline or a .json file)
    with a shared image (a link in the JSON or a photo replied to)."""
    chat, owner = req.chat, req.owner
    body = req.body
    if req.body_file:
        doc = await rt.TG.download(req.body_file.message, text=True)
        body = Path(doc).read_text(errors="replace")
    body = body.strip()
    if not body:
        await rt.TG.text(chat, LIST_SCHEMA)
        return
    try:
        data = json.loads(body)
    except Exception:
        raise ValueError("Invalid JSON: check the syntax.")
    if not isinstance(data, dict):
        raise ValueError("The JSON must be an object { ... }.")
    prompts = data.get("prompts")
    if not isinstance(prompts, list) or not prompts or len(prompts) > LIST_MAX:
        raise ValueError(f"'prompts': a list of 1 to {LIST_MAX} entries is required.")

    comps = catalog.components()
    owner_chars = None

    def resolve_char(value, where):
        """The character (lower case) or None; an error if it does not exist."""
        nonlocal owner_chars
        if value is None or not str(value).strip():
            return None
        name = str(value).strip().lower()
        if name == "off":
            return None
        if not characters.char_row(owner, name):
            if owner_chars is None:
                owner_chars = [r["name"] for r in characters.char_list(owner)]
            known = ", ".join(owner_chars) or "none (create one with /character)"
            raise ValueError(
                f"{where}: no character called «{name}». "
                f"Yours: {known}."
            )
        return name

    default_char = resolve_char(data.get("character"), "'character'")

    def resolve_wf(wf):
        if wf is None or not str(wf).strip():
            return ""
        wf = str(wf).strip()
        if wf not in comps or not comps[wf].get("enabled"):
            raise ValueError(f"Invalid or disabled workflow: {wf}")
        return wf

    # Shared image: from "image" in the JSON or a photo replied to.
    image_path = None
    img_cfg = str(data.get("image") or "").strip()
    if img_cfg:
        urls = extract_urls(img_cfg)
        if not urls:
            raise ValueError("'image': use a Pinterest link or an image URL.")
        await rt.TG.call("sendChatAction", {"chat_id": chat, "action": "upload_photo"})
        try:
            image_path = await images.acquire(
                images.Link(urls[0]), f"{chat}_list_{req.message_id}"
            )
        except Exception as exc:
            raise ValueError(f"Image not downloaded: {str(exc)[:300]}")
    elif req.image:
        image_path = await images.acquire(req.image)
    have_image = image_path is not None

    # The whole JSON is valid before the first draft is created.
    entries = []
    for idx, item in enumerate(prompts):
        if not isinstance(item, dict):
            raise ValueError(f"prompts[{idx}]: must be an object.")
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"prompts[{idx}]: 'prompt' is missing.")
        wf = resolve_wf(item.get("workflow"))
        who = (
            resolve_char(item["character"], f"prompts[{idx}].character")
            if "character" in item else default_char
        )
        entries.append((prompt, wf, who))

    mid = req.message_id
    created = []
    for idx, (prompt, wf, who) in enumerate(entries):
        if not wf and have_image:
            # Automatic with an image: the first in AUTO_COMPONENTS that
            # takes photos; without one, confirmation decides.
            wf = _auto_with_photo(comps)
        needs_image = not wf or len(comps.get(wf, {}).get("images", [])) > 0
        attach = needs_image and image_path is not None
        job = rt.JOBS.create_draft(
            owner, chat, prompt, wf, origin=f"{chat}:list:{mid}:{idx}",
        )
        if who:
            rt.JOBS.edit_draft(job["id"], character=who)
        if attach:
            rt.JOBS.attach_input(job["id"], -(idx + 1), image_path)
        created.append(job)

    if not created:
        raise ValueError("No job created.")

    if have_image or all(who for _, _, who in entries):
        # The image is already there, or every job has its character
        # (the references are the photos): straight to the queue.
        started, failed = _confirm_all([job["id"] for job in created], owner)
        names = sorted({who for _, _, who in entries if who})
        lines = [
            f"✅ Batch: {len(started)} of {len(created)} jobs queued"
            + (f" (character: {', '.join(names)})." if names else "."),
            "They will run one after the other in the night window. /queue for the status.",
        ]
        if failed:
            lines.append("Not started:")
            lines += [f"#{i}: {why}" for i, why in failed]
        await rt.TG.text(chat, "\n".join(lines))
        return

    # No image: drafts ready; then /add X..Y and a single start.
    lo = min(j["id"] for j in created)
    hi = max(j["id"] for j in created)
    groups = {}
    for job in created:
        groups.setdefault(job["component"] or "auto", []).append(job["id"])
    lines = [f"✅ Jobs #{lo}–#{hi} ready ({len(created)}):"]
    for wf, ids in groups.items():
        label = "chosen automatically at start" if wf == "auto" else wf
        lines.append(f"• {label}: " + " ".join(str(i) for i in ids))
    lines.append("")
    lines.append(
        f"Send a photo with /add {lo}..{hi} to use it on every job, "
        "then tap ▶️ Start all."
    )
    keyboard = [[buttons.start_all(lo, hi)]]
    await rt.TG.text(chat, "\n".join(lines), keyboard)


def add_image_to_jobs(owner, lo, hi, image_path, src_id):
    """Attaches the same image to every draft in the range."""
    added = []
    for job_id in range(lo, hi + 1):
        job = rt.JOBS.get(job_id)
        if not job or job["owner"] != owner:
            continue
        if rt.JOBS.attach_input(job_id, src_id, image_path):
            added.append(job_id)
    return added


async def confirm_added(chat, added, lo, hi):
    """Confirms the image was added to several jobs + the Start all button."""
    if not added:
        raise ValueError("No valid job in the range (your drafts not yet confirmed).")
    a, b = min(added), max(added)
    keyboard = [[buttons.start_all(a, b)]]
    await rt.TG.text(
        chat,
        f"💡 Image added to {len(added)} jobs: "
        + " ".join(f"#{i}" for i in added)
        + "\nTap ▶️ Start all to start them together.",
        keyboard,
    )


async def batch_start(chat, owner, lo, hi):
    """Starts every draft in the range with a single tap."""
    ids = [
        job_id for job_id in range(lo, hi + 1)
        if (job := rt.JOBS.get(job_id)) and job["owner"] == owner
    ]
    started, failed = _confirm_all(ids, owner)
    already = [i for i, why in failed if why == ALREADY]
    failed = [(i, why) for i, why in failed if why != ALREADY]
    msg = [f"▶️ Started {len(started)} jobs."]
    if already:
        msg.append("Already confirmed: " + " ".join(f"#{i}" for i in already))
    if failed:
        msg.append("Cannot start:")
        msg += [f"#{i}: {why}" for i, why in failed]
    await rt.TG.text(chat, "\n".join(msg))


ALREADY = "already confirmed"


def _confirm_all(job_ids, owner):
    """Confirms drafts created in a batch (automatic component choice).
    Returns (started, [(id, reason)] for those not started)."""
    started, failed = [], []
    for job_id in job_ids:
        try:
            outcome = confirmation.confirm(job_id, owner, batch=True)
            why = jobs.reason(outcome)
        except Exception as exc:
            why = str(exc)[:150]
        if why:
            failed.append((job_id, why))
        else:
            started.append(job_id)
    return started, failed
