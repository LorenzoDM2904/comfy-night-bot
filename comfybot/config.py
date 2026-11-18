"""Configuration read from the .env (and the environment) on import.

No side effects here: the checks that stop startup live in validate(),
called by main. Other modules read `config.NAME` when they use it, so
tests can replace a value.
"""

import os
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _flag(name, default):
    value = os.getenv(name, default).strip().lower()
    return value in ("1", "true", "yes")


def _parse_wh(spec):
    try:
        parts = str(spec).lower().replace("x", " ").split()
        w, h = (int(x) for x in parts[:2])
        return max(16, w), max(16, h)
    except Exception:
        return 1080, 1350


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOW = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if x.strip()
}

DATA = Path(os.getenv("DATA_DIR", "./data")).resolve()
COMPONENTS = Path(os.getenv("COMPONENTS_DIR", "./components")).resolve()
# Batch drafts (/list) without a component: the first compatible of these.
AUTO_COMPONENTS = [
    c.strip() for c in os.getenv("AUTO_COMPONENTS", "").split(",") if c.strip()
]
# Plugin folders (see plugins.py), separated by ":".
PLUGINS_DIRS = [
    Path(p).expanduser().resolve()
    for p in os.getenv("PLUGINS_DIRS", "./plugins").split(os.pathsep)
    if p.strip()
]

TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Rome"))
START = clock_time.fromisoformat(os.getenv("NIGHT_START", "00:30"))
FINISH = clock_time.fromisoformat(os.getenv("NIGHT_FINISH", "08:00"))
NIGHT_MODE_DEFAULT = (
    os.getenv("NIGHT_MODE_DEFAULT", "true").lower() == "true"
)

POLL = max(3, int(os.getenv("POLL_SECONDS", "10")))
TIMEOUT = int(os.getenv("JOB_TIMEOUT_MINUTES", "240")) * 60
MAX_BYTES = int(os.getenv("MAX_INPUT_MB", "18")) * 1024 * 1024
# Incoming videos (for plugins): the Bot API downloads 20 MB at most.
VIDEO_MAX_BYTES = int(os.getenv("VIDEO_MAX_MB", "20")) * 1024 * 1024
WATCHDOG = max(5, int(os.getenv("WATCHDOG_MINUTES", "20"))) * 60
EXECUTION_BACKEND = os.getenv("EXECUTION_BACKEND", "local")

# A single value, used both by the adapter and by managed startup.
COMFYUI_BASE_URL = os.getenv(
    "COMFYUI_BASE_URL", "http://127.0.0.1:8188"
).strip()
LLAMA_URL = os.getenv("LLAMA_BASE_URL", "http://127.0.0.1:9931")
DESCRIBE_MODEL = os.getenv(
    "DESCRIBE_MODEL",
    "unsloth/Qwen3.5-4B-MTP-GGUF:Q4_K_XL",
)

# ComfyUI managed on demand (only when COMFYUI_MANAGE=true).
COMFYUI_MANAGE = _flag("COMFYUI_MANAGE", "false")
# The ComfyUI folder (with main.py), or a full command in
# COMFYUI_START_CMD. Optional: a folder for input/ and output/ and the
# extra_model_paths.yaml file (for example ComfyUI Desktop's).
COMFYUI_INSTALL = os.path.expanduser(os.getenv("COMFYUI_INSTALL_DIR", ""))
COMFYUI_SHARED = os.path.expanduser(os.getenv("COMFYUI_SHARED", ""))
COMFYUI_EXTRA = os.path.expanduser(
    os.getenv("COMFYUI_EXTRA_MODEL_PATHS_CONFIG", "")
)
COMFYUI_START_CMD = os.getenv("COMFYUI_START_CMD", "").strip()
COMFYUI_PIDFILE = os.getenv("COMFYUI_PIDFILE", "/tmp/comfyui_nightbot.pid")
COMFYUI_LOG = os.getenv("COMFYUI_LOG", "/tmp/comfyui_nightbot.log")
COMFYUI_BOOT = int(os.getenv("COMFYUI_BOOT_TIMEOUT", "360"))

# Upscale on request: a "WxH" box (proportions kept, fit_in_box).
UPSCALE_W, UPSCALE_H = _parse_wh(os.getenv("UPSCALE", "1080x1350"))


def validate():
    """Checks that block the bot from starting."""
    if not TOKEN or not ALLOW:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS in the .env."
        )
    if COMFYUI_MANAGE and not (COMFYUI_START_CMD or COMFYUI_INSTALL):
        raise SystemExit(
            "COMFYUI_MANAGE=true: set COMFYUI_INSTALL_DIR (the ComfyUI "
            "folder) or COMFYUI_START_CMD."
        )
    if START == FINISH:
        raise SystemExit("NIGHT_START and NIGHT_FINISH cannot be the same.")
    if EXECUTION_BACKEND != "local":
        raise SystemExit(
            "Only the local backend is implemented. "
            "A cloud backend needs another adapter."
        )
