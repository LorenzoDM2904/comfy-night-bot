"""Input plan: which images the component receives (a single rule).

Whether a component is compatible with a draft is decided here, with
plan_inputs: see confirmation.component_options.
"""


def slot_limits(manifest):
    """Min/max images of the manifest (default: the number of bindings)."""
    need = len(manifest.get("images", []))
    return (
        int(manifest.get("min_images", need)),
        int(manifest.get("max_images", need)),
    )


ROLE_CHARACTER = "character"


ROLE_SCENE = "scene"


def plan_inputs(scene, refs, manifest):
    """The single rule that decides which images the workflow receives.

    - Scene photos (sent by the user) are NEVER dropped: if there are
      more of them than slots, the component is not compatible.
    - The character's references fill the remaining slots and come
      first (Image 1, 2, ...).

    Returns (plan, unused_references), where plan is a list of
    (path, role). Raises ValueError if the component does not fit.
    """
    mn, mx = slot_limits(manifest)
    if len(scene) > mx:
        raise ValueError(
            f"{len(scene)} scene photos, but the workflow accepts "
            f"{mx} at most."
        )
    used_refs = list(refs[:mx - len(scene)])
    plan = (
        [(path, ROLE_CHARACTER) for path in used_refs]
        + [(path, ROLE_SCENE) for path in scene]
    )
    if len(plan) < mn:
        raise ValueError(
            f"At least {mn} images are needed, {len(plan)} available."
        )
    return plan, len(refs) - len(used_refs)
