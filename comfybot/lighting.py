"""Lighting presets.

The light is not decided by the describe system prompts: the user picks
it with /light, per job or as a default, and the preset's sentence is
appended to the prompt on confirmation.

jobs.light: NULL = the user's default at confirmation time,
"off" = no lighting sentence, otherwise the preset name.
"""

from . import db

PRESETS = {
    "window": (
        "Lighting: soft natural window light coming from one side, gentle "
        "falloff into shadow on the far side, realistic indoor daylight, "
        "natural skin tones."
    ),
    "overcast": (
        "Lighting: overcast daylight, soft diffuse light with no hard "
        "shadows, even exposure, muted natural colors."
    ),
    "sun": (
        "Lighting: direct midday sunlight, crisp hard-edged shadows, "
        "bright saturated colors, high contrast."
    ),
    "flash": (
        "Lighting: direct on-camera flash, bright frontal light, hard "
        "shadow just behind the subject, slightly darker background, "
        "candid snapshot look."
    ),
    "studio": (
        "Lighting: studio setup with a large softbox key light and soft "
        "fill, clean even illumination, controlled shadows."
    ),
    "sunset": (
        "Lighting: warm low golden-hour sun from the side, long soft "
        "shadows, warm orange tones, gentle rim light on the hair."
    ),
}

# Names used by earlier versions, still found in saved settings and jobs.
ALIASES = {
    "finestra": "window", "nuvoloso": "overcast", "sole": "sun",
    "tramonto": "sunset",
}

OFF = "off"


def _current(name):
    return ALIASES.get(name, name)


def default(owner):
    """The user's default preset (OFF if not set)."""
    return _current(db.setting(f"light:{owner}") or OFF)


def resolve(job):
    """A job's effective preset: its own, or the user's default."""
    return _current(job["light"]) if job["light"] else default(job["owner"])


def _unknown(name):
    return ValueError(
        f"Unknown light {name!r}. Presets: {', '.join(PRESETS)}, off."
    )


def sentence(name):
    """The sentence to append to the prompt ('' for off)."""
    name = _current(name)
    if name == OFF:
        return ""
    if name not in PRESETS:
        raise _unknown(name)
    return PRESETS[name]


def parse(name):
    """A valid preset name from user input (lower case), or an error."""
    name = _current((name or "").strip().lower())
    if name != OFF and name not in PRESETS:
        raise _unknown(name)
    return name


def describe_all(owner):
    lines = [f"Default light: {default(owner)}", ""]
    for name, text in PRESETS.items():
        lines.append(f"• {name} — {text.split(': ', 1)[1]}")
    lines += [
        "• off — no lighting hint",
        "",
        "/light <preset> — default for new jobs",
        "/light <ID> <preset|off|default> — for one draft only",
    ]
    return "\n".join(lines)
