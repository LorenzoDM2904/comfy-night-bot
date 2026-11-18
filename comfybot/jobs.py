"""Jobs on the Telegram side: summary, confirmation outcomes, picker,
cancelling. The confirmation rules live in confirmation."""

from pathlib import Path

from . import buttons, catalog, characters, confirmation, lighting, plugins
from . import runtime as rt
from .snapshot import Snapshot


async def summary(job_id):
    job = rt.JOBS.get(job_id)
    kind = plugins.kind(job["kind"])
    if kind and kind.summary:
        return await kind.summary(job_id)
    if job["kind"] != "generate":
        title = Snapshot.from_json(job["snapshot"]).title or job["kind"]
        return await rt.TG.text(
            job["chat"],
            f"Job #{job_id} — {title}\nState: {job['state']}",
        )
    count = len(rt.JOBS.scene_inputs(job_id))
    refs = len(characters.character_ref_paths(job["owner"], job["character"]))
    keyboard = None
    if job["state"] == "draft":
        keyboard = [[buttons.start(job_id), buttons.cancel(job_id)]]
    await rt.TG.text(
        job["chat"],
        f"Job #{job_id} — {job['component'] or 'to be chosen'}\n"
        f"State: {job['state']}\n"
        f"Images received: {count}\n"
        f"Order: the order of the messages in the album.\n"
        f"Character: {job['character'] or 'none'}"
        + (
            f" ({refs} reference photos)" if job["character"]
            else f" (/character {job_id} name to assign one)"
            if job["state"] == "draft" else ""
        )
        + "\n"
        f"Light: {_light_label(job)}\n"
        f"Prompt: {job['prompt'][:2000] or '(missing)'}\n"
        f"Wait for all the images before confirming.\n"
        f"To change it: /prompt {job_id} text · /light {job_id} preset",
        keyboard,
    )


def _light_label(job):
    if job["light"]:
        return lighting.resolve(job)
    return f"{lighting.default(job['owner'])} (default)"


async def start(job_id, owner, component=None):
    """Start (button, /submit, a choice from the picker): confirms the
    draft and reports the outcome in the chat. Returns the outcome."""
    kind = plugins.kind(rt.JOBS.owned(job_id, owner)["kind"])
    if kind and kind.start:
        return await kind.start(job_id, owner)
    outcome = confirmation.confirm(job_id, owner, component=component)
    await show(outcome)
    return outcome


async def show(outcome):
    """Chat messages for a confirmation outcome."""
    job = rt.JOBS.get(outcome.job_id)
    chat, job_id = job["chat"], outcome.job_id
    if isinstance(outcome, confirmation.AlreadyConfirmed):
        return await rt.TG.text(chat, "Job already confirmed.")
    if isinstance(outcome, confirmation.MissingPrompt):
        return await rt.TG.text(
            chat, f"Set the prompt: /prompt {job_id} text"
        )
    if isinstance(outcome, confirmation.NeedsComponent):
        if outcome.rejected:
            await rt.TG.text(
                chat,
                f"Job #{job_id}: it cannot run with "
                f"{outcome.rejected} ({outcome.reason}).\n"
                f"Change the draft (/add {job_id} for photos, "
                f"/character {job_id} for the character) or choose "
                "another component.",
            )
        return await _picker(chat, outcome)
    await rt.TG.text(chat, _confirmed_text(outcome))


def reason(outcome):
    """Why an outcome is not a confirmation, briefly (batch, /matrix)."""
    if isinstance(outcome, confirmation.AlreadyConfirmed):
        return "already confirmed"
    if isinstance(outcome, confirmation.MissingPrompt):
        return "the prompt is missing"
    if isinstance(outcome, confirmation.NeedsComponent):
        if outcome.rejected:
            return f"{outcome.rejected} not compatible: {outcome.reason}"
        if not outcome.options:
            return "no component compatible with the images"
        return "choose the component"
    return ""


def describe_counts(scene_count, ref_count):
    text = f"{scene_count} images"
    if ref_count:
        text += f" + {ref_count} character references"
    return text


async def _picker(chat, outcome):
    job_id = outcome.job_id
    counts = describe_counts(outcome.scene_count, outcome.ref_count)
    if not outcome.options:
        need_desc = ", ".join(
            f"{m.get('name') or cid} "
            f"({m.get('min_images', len(m.get('images', [])))}–"
            f"{m.get('max_images', len(m.get('images', [])))}"
            f" images)"
            for cid, m in catalog.components().items()
            if m.get("enabled")
        )
        return await rt.TG.text(
            chat,
            f"Job #{job_id}: no workflow compatible with "
            f"{counts}.\n"
            f"Required: {need_desc or 'none'}.\n"
            f"Add photos with /add {job_id}.",
        )
    keyboard = []
    for option in outcome.options:
        mn, mx = option.min_images, option.max_images
        if mn == 0 and mx == 0:
            label = f"{option.name} (text only)"
        elif mn == mx:
            label = f"{option.name} ({mn})"
        else:
            label = f"{option.name} ({mn}–{mx} images)"
        keyboard.append([buttons.pick(job_id, option.component, label)])
    keyboard.append([buttons.cancel(job_id)])
    await rt.TG.text(
        chat,
        f"Job #{job_id} — {counts} — choose the workflow "
        "(choosing starts it):",
        keyboard,
    )


def _confirmed_text(outcome):
    lines = [f"Job #{outcome.job_id} queued."]
    if outcome.auto_chosen:
        lines.append(f"Component: {outcome.name} (the only compatible one).")
    if outcome.plan:
        lines.append("Workflow inputs:")
        lines += [
            f"  Image {i} → {role} ({Path(path).name})"
            for i, (path, role) in enumerate(outcome.plan, 1)
        ]
    if outcome.unused_refs:
        lines.append(
            f"{outcome.unused_refs} character references not used: "
            "no slots left (scene photos come first)."
        )
    if 0 < len(outcome.plan) < outcome.slots:
        lines.append(
            f"Slots {len(outcome.plan) + 1}–{outcome.slots} "
            "are not run."
        )
    lines.append("The night window limits when it starts, not how long it runs.")
    return "\n".join(lines)


async def cancel_job(job_id, owner):
    job = rt.JOBS.owned(job_id, owner)
    # Conditional: if the worker has meanwhile started submitting, the
    # cancel does not happen and we say so.
    if not rt.JOBS.cancel(job_id):
        raise ValueError(
            "Only drafts and queued jobs can be cancelled. "
            "A job already sent to ComfyUI is not interrupted."
        )
    await rt.TG.text(job["chat"], f"Job #{job_id} cancelled.")


async def cancel_range(chat, owner, lo, hi):
    """/cancel X..Y: cancels the user's jobs in the range at once. Those
    already sent or finished stay as they were and are listed."""
    cancelled, refused, already = [], [], []
    for job_id in range(lo, hi + 1):
        job = rt.JOBS.get(job_id)
        if not job or job["owner"] != owner:
            continue
        if rt.JOBS.cancel(job_id):
            cancelled.append(job_id)
        elif job["state"] == "cancelled":
            already.append(job_id)
        else:
            refused.append(job_id)
    if not (cancelled or refused or already):
        raise ValueError(f"None of your jobs between #{lo} and #{hi}.")
    ids = lambda items: " ".join(f"#{i}" for i in items)  # noqa: E731
    count = len(cancelled)
    lines = [("🗑 Cancelled 1 job" if count == 1
              else f"🗑 Cancelled {count} jobs")
             + (f": {ids(cancelled)}" if cancelled else ".")]
    if already:
        lines.append(f"Already cancelled: {ids(already)}")
    if refused:
        lines.append(
            f"Cannot be cancelled (already sent to ComfyUI or finished): "
            f"{ids(refused)}"
        )
    await rt.TG.text(chat, "\n".join(lines))
