"""Inline buttons: building and decoding the callback data.

The only place that knows the callback_data format (64 bytes at most):
whoever sends a button uses the functions below, and the bot decodes it
with decode() into a Press.
"""

from dataclasses import dataclass

from . import plugins


@dataclass(frozen=True)
class Press:
    """A tap on a button, already decoded."""
    action: str          # start, cancel, pick, upscale, describe,
    #                      start_all, or a plugin's action
    job_id: int = 0      # the job (or the first of the range for start_all)
    last_id: int = 0     # the last job of the range (start_all)
    component: str = ""  # the chosen component (pick)
    description: int = 0  # the saved description (describe)
    payload: str = ""    # a plugin button's data (see plugins)


# The core's callback_data prefixes: a plugin cannot use them.
CORE_ACTIONS = (
    "submit", "cancel", "pick", "upscale", "batchstart", "describe",
)


def _button(text, data):
    return {"text": text, "callback_data": data}


def start(job_id):
    return _button("Start", f"submit:{job_id}")


def cancel(job_id):
    return _button("Cancel", f"cancel:{job_id}")


def pick(job_id, component, label):
    return _button(label, f"pick:{job_id}:{component}")


def upscale(job_id):
    return _button("🔍 Upscale", f"upscale:{job_id}")


def describe(description_id):
    return _button("📝 Draft with this prompt", f"describe:{description_id}")


def start_all(lo, hi):
    return _button(f"▶️ Start all (#{lo}–#{hi})", f"batchstart:{lo}:{hi}")


def decode(data):
    """callback_data -> Press. ValueError if the button is not valid."""
    action, _, rest = (data or "").partition(":")
    parts = rest.split(":")
    try:
        if action == "submit":
            return Press("start", job_id=int(parts[0]))
        if action in ("cancel", "upscale"):
            return Press(action, job_id=int(parts[0]))
        if action == "pick":
            return Press("pick", job_id=int(parts[0]), component=parts[1])
        if action == "batchstart":
            return Press(
                "start_all", job_id=int(parts[0]), last_id=int(parts[1])
            )
        if action == "describe":
            # Buttons sent by earlier versions had a trailing ":p".
            return Press("describe", description=int(parts[0]))
    except (IndexError, ValueError):
        pass
    if action not in CORE_ACTIONS and action in plugins.BUTTONS:
        return Press(action, payload=rest)
    raise ValueError("Invalid button.")
