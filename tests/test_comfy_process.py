"""Detecting that ComfyUI exited (crash) when the bot manages it."""

import asyncio
import os
import subprocess
import sys
import time

from comfybot import comfy_process, config, worker
from comfybot.engine import Failed, Missing, Running



def run(coro):
    return asyncio.run(coro)


def _pidfile(monkeypatch, tmp_path, pid):
    path = tmp_path / "comfy.pid"
    path.write_text(str(pid))
    monkeypatch.setattr(config, "COMFYUI_PIDFILE", str(path))


def test_comfy_running_detects_zombie_child(monkeypatch, tmp_path):
    # A child that exits at once and is not reaped: it stays a zombie.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(1.0)
    os.kill(proc.pid, 0)  # the zombie still answers signal 0
    _pidfile(monkeypatch, tmp_path, proc.pid)

    assert comfy_process.comfy_running() is False
    # Reaped: later checks see it as exited too.
    assert comfy_process.comfy_running() is False


def test_comfy_running_live_child(monkeypatch, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        _pidfile(monkeypatch, tmp_path, proc.pid)
        assert comfy_process.comfy_running() is True
    finally:
        proc.kill()
        proc.wait()


def test_comfy_running_without_pidfile(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config, "COMFYUI_PIDFILE", str(tmp_path / "assente.pid")
    )
    assert comfy_process.comfy_running() is False


def test_run_job_fails_fast_when_managed_comfy_dies(env, monkeypatch):
    job_id = env.job(state="running", prompt_id="pid-x", started=time.time())

    env.engine.statuses = [ConnectionError("connection refused")]
    monkeypatch.setattr(config, "COMFYUI_MANAGE", True)
    monkeypatch.setattr(comfy_process, "comfy_pid", lambda: 12345)
    monkeypatch.setattr(comfy_process, "comfy_running", lambda: False)

    run(asyncio.wait_for(worker.run_job(env.row(job_id)), timeout=5))

    row = env.row(job_id)
    assert row["state"] == "failed"
    assert "ComfyUI exited" in row["error"]
    assert "ComfyUI closed" in env.tg.all_text()


def test_run_job_keeps_waiting_while_comfy_alive(env, monkeypatch):
    job_id = env.job(state="running", prompt_id="pid-x", started=time.time())
    env.engine.statuses = [ConnectionError("network blip"), Failed("error")]
    monkeypatch.setattr(config, "COMFYUI_MANAGE", True)
    monkeypatch.setattr(comfy_process, "comfy_pid", lambda: 12345)
    monkeypatch.setattr(comfy_process, "comfy_running", lambda: True)
    monkeypatch.setattr(config, "POLL", 0)

    run(asyncio.wait_for(worker.run_job(env.row(job_id)), timeout=5))

    # A transient error with ComfyUI alive: try again, do not close at once.
    row = env.row(job_id)
    assert row["state"] == "failed"
    assert "ComfyUI exited" not in (row["error"] or "")


# --- worker rules on the submission state ---------------------------------------

def test_invio_missing_three_times_becomes_uncertain(env, monkeypatch):
    monkeypatch.setattr(config, "POLL", 0)
    job_id = env.job(state="running", prompt_id="pid-x", started=time.time())
    env.engine.statuses = [Missing()]
    run(asyncio.wait_for(worker.run_job(env.row(job_id)), timeout=5))
    row = env.row(job_id)
    assert row["state"] == "unknown"
    assert "no longer in ComfyUI" in row["error"]


def test_missing_count_restarts_when_the_submission_reappears(env, monkeypatch):
    monkeypatch.setattr(config, "POLL", 0)
    job_id = env.job(state="running", prompt_id="pid-x", started=time.time())
    env.engine.statuses = [
        Missing(), Missing(), Running(), Missing(), Missing(), Failed("x"),
    ]
    run(asyncio.wait_for(worker.run_job(env.row(job_id)), timeout=5))
    assert env.row(job_id)["state"] == "failed"


def test_comfy_gets_the_bot_environment(monkeypatch, tmp_path):
    seen = {}

    class FakeProc:
        pid = 4242

    def fake_popen(*args, **kwargs):
        seen.update(kwargs)
        return FakeProc()

    monkeypatch.setenv("NIGHTBOT_TEST_VAR", "dal bot")
    monkeypatch.setattr(comfy_process, "comfy_running", lambda: False)
    monkeypatch.setattr(comfy_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(config, "COMFYUI_LOG", str(tmp_path / "comfy.log"))
    monkeypatch.setattr(config, "COMFYUI_PIDFILE", str(tmp_path / "comfy.pid"))
    for cmd in ("", "custom-start"):
        monkeypatch.setattr(config, "COMFYUI_START_CMD", cmd)
        comfy_process.start_comfy()
        assert seen["env"]["NIGHTBOT_TEST_VAR"] == "dal bot"
