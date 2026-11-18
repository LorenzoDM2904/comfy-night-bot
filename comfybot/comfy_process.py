"""Starting and stopping ComfyUI when the bot manages it (COMFYUI_MANAGE)."""

import asyncio
import logging
import os
import re
import signal
import subprocess
import sys
import time

from . import config, runtime as rt

log = logging.getLogger("nightbot")


def comfy_pid():
    if not os.path.exists(config.COMFYUI_PIDFILE):
        return None
    try:
        with open(config.COMFYUI_PIDFILE) as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


def comfy_running():
    pid = comfy_pid()
    if not pid:
        return False
    # If ComfyUI is a child of the bot and exits (a crash included), it
    # stays a zombie until someone reaps it: os.kill(pid, 0) would still
    # see it alive. waitpid(WNOHANG) reaps it and detects the exit.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            return False
    except ChildProcessError:
        pass  # not our child (e.g. started before the bot restarted)
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _comfy_argv():
    url = config.COMFYUI_BASE_URL or "http://127.0.0.1:8188"
    m = re.match(r"https?://[^:]+(?::(\d+))?", url)
    port = m.group(1) if m and m.group(1) else "8188"
    py = os.path.join(config.COMFYUI_INSTALL, ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    argv = [
        py, os.path.join(config.COMFYUI_INSTALL, "main.py"),
        "--listen", "127.0.0.1", "--port", port,
    ]
    if config.COMFYUI_EXTRA:
        argv += ["--extra-model-paths-config", config.COMFYUI_EXTRA]
    if config.COMFYUI_SHARED:
        argv += [
            "--input-directory", os.path.join(config.COMFYUI_SHARED, "input"),
            "--output-directory", os.path.join(config.COMFYUI_SHARED, "output"),
        ]
    return argv


def _comfy_env():
    """ComfyUI's environment: the bot's own (with the .env), so variables
    for installed nodes are set in a single place (a plugin can set them
    too, with os.environ.setdefault in register)."""
    return dict(os.environ)


def start_comfy():
    if comfy_running():
        return
    log.info("Starting ComfyUI on demand...")
    logf = open(config.COMFYUI_LOG, "a")
    if config.COMFYUI_START_CMD:
        proc = subprocess.Popen(
            config.COMFYUI_START_CMD, shell=True, stdout=logf, stderr=logf,
            start_new_session=True, env=_comfy_env(),
        )
    else:
        proc = subprocess.Popen(
            _comfy_argv(), stdout=logf, stderr=logf,
            start_new_session=True, cwd=config.COMFYUI_INSTALL, env=_comfy_env(),
        )
    with open(config.COMFYUI_PIDFILE, "w") as f:
        f.write(str(proc.pid))


async def stop_comfy():
    pid = comfy_pid()
    if pid and comfy_running():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(40):
            if not comfy_running():
                break
            await asyncio.sleep(0.5)
        if comfy_running():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if os.path.exists(config.COMFYUI_PIDFILE):
        try:
            os.remove(config.COMFYUI_PIDFILE)
        except OSError:
            pass
    log.info("ComfyUI stopped (idle, on demand).")


async def ensure_comfy(needed):
    """Starts or stops ComfyUI as needed (when managed)."""
    if not config.COMFYUI_MANAGE:
        return True
    if not needed:
        if comfy_running():
            await stop_comfy()
        return True
    if not comfy_running():
        start_comfy()
    deadline = time.time() + config.COMFYUI_BOOT
    while time.time() < deadline:
        try:
            await rt.ENGINE.health()
            return True
        except Exception:
            await asyncio.sleep(3)
    try:
        await rt.ENGINE.health()
        return True
    except Exception:
        raise ValueError(
            "ComfyUI did not start in time (on demand). "
            f"Log: {config.COMFYUI_LOG}"
        )
