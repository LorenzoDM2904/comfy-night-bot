"""Confirming a draft: from draft to queued job.

Owns the rules on which components suit a draft and the choice of a
component when none is set; the snapshot format belongs to snapshot.py.
It does not write to the chat: it returns an outcome, which the Telegram
side (jobs) turns into messages.

Outcomes: Confirmed, NeedsComponent, AlreadyConfirmed, MissingPrompt.
Exceptions are for real errors (not your job, a component that does not
exist or is not configured).
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import catalog, characters, config, lighting, planner, plugins
from . import runtime as rt
from . import snapshot


@dataclass(frozen=True)
class Option:
    """A component compatible with the draft."""
    component: str
    name: str
    min_images: int
    max_images: int


@dataclass(frozen=True)
class Confirmed:
    job_id: int
    component: str
    name: str
    plan: List[Tuple[str, str]]   # (path, role), in order
    unused_refs: int              # character references left without a slot
    slots: int                    # image slots the component declares
    auto_chosen: bool             # the component was chosen on confirmation


@dataclass(frozen=True)
class NeedsComponent:
    job_id: int
    options: List[Option]
    scene_count: int
    ref_count: int
    rejected: Optional[str] = None  # a component chosen but not compatible
    reason: str = ""


@dataclass(frozen=True)
class AlreadyConfirmed:
    job_id: int


@dataclass(frozen=True)
class MissingPrompt:
    job_id: int


@dataclass(frozen=True)
class _Images:
    scene: List[str]
    refs: List[str] = field(default_factory=list)
    caption: str = ""


def component_options(job):
    """The components compatible with the draft, in catalog order."""
    return _options(_images(job))


def confirm(job_id, owner, component=None, batch=False):
    """Confirms the draft and returns the outcome.

    `component`: an explicit choice from the picker, saved on the draft.
    `batch`: a draft created in a batch; without a component the
    automatic choice is used instead of asking.
    """
    job = rt.JOBS.owned(job_id, owner)
    if job["state"] != "draft":
        return AlreadyConfirmed(job_id)
    kind = plugins.kind(job["kind"])
    if kind and kind.container:
        raise ValueError(
            f"Draft #{job_id} starts on its own: press Start on its "
            f"summary or send /submit {job_id}."
        )
    if not job["prompt"].strip():
        return MissingPrompt(job_id)
    if component is not None:
        if component not in catalog.components():
            raise ValueError("No such component.")
        if not rt.JOBS.edit_draft(job_id, component=component):
            return AlreadyConfirmed(job_id)
        job = rt.JOBS.get(job_id)

    images = _images(job)
    chosen, auto = job["component"], False
    if not chosen:
        chosen = _choose(_options(images), batch)
        if chosen is None:
            return _needs(job_id, images)
        auto = True
    elif chosen not in catalog.components():
        return _needs(job_id, images, chosen, "no such component")

    manifest = catalog.components()[chosen]
    try:
        plan, unused_refs = planner.plan_inputs(
            images.scene, images.refs, manifest
        )
    except ValueError as exc:
        return _needs(job_id, images, chosen, str(exc))

    positive = job["prompt"]
    if images.caption:
        positive = images.caption + "\n" + positive
    caption = positive
    light = lighting.resolve(job)
    if lighting.sentence(light):
        positive = positive + "\n" + lighting.sentence(light)

    # Immutable snapshot: changing the component does not change the queue.
    # /matrix variants carry size, seed and label in the overrides.
    snap = snapshot.for_component(
        chosen, plan, positive, caption, light,
        json.loads(job["overrides"] or "{}"),
        loras=characters.char_loras(job["owner"], job["character"]),
    )
    # Conditional: a double tap on "Start" does not queue twice.
    if not rt.JOBS.confirm(job_id, snap.to_json(), chosen):
        return AlreadyConfirmed(job_id)
    return Confirmed(
        job_id=job_id,
        component=chosen,
        name=snap.name,
        plan=plan,
        unused_refs=unused_refs,
        slots=len(manifest["images"]),
        auto_chosen=auto,
    )


def _images(job):
    """The draft's scene photos and, if it has a character, the
    character's references and fixed caption."""
    scene = rt.JOBS.scene_inputs(job["id"])
    if not job["character"]:
        return _Images(scene)
    character = characters.char_row(job["owner"], job["character"])
    if not character:
        return _Images(scene)
    return _Images(
        scene,
        characters.character_ref_paths(job["owner"], job["character"]),
        (character["caption"] or "").strip(),
    )


def _options(images):
    """The single compatibility rule: the input plan must succeed."""
    out = []
    for cid, manifest in catalog.components().items():
        if not manifest.get("enabled") or manifest.get("engine") != "local":
            continue
        try:
            planner.plan_inputs(images.scene, images.refs, manifest)
        except ValueError:
            continue
        mn, mx = planner.slot_limits(manifest)
        out.append(Option(cid, manifest.get("name") or cid, mn, mx))
    return out


def _choose(options, batch):
    """The component to use when the draft has none, or None."""
    ids = [option.component for option in options]
    if len(ids) == 1:
        return ids[0]
    if batch:
        # Batch drafts: the first compatible one in AUTO_COMPONENTS order.
        return next((cid for cid in config.AUTO_COMPONENTS if cid in ids), None)
    return None


def _needs(job_id, images, rejected=None, reason=""):
    return NeedsComponent(
        job_id=job_id,
        options=_options(images),
        scene_count=len(images.scene),
        ref_count=len(images.refs),
        rejected=rejected,
        reason=reason,
    )
