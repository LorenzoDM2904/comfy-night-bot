"""Derived jobs (upscale) in the queue, and delivery of the originals."""

import asyncio
import json

import pytest
from PIL import Image

from conftest import snap
from comfybot import config, db, delivery, derived, worker
from comfybot import runtime as rt
from comfybot.snapshot import Snapshot


def run(coro):
    return asyncio.run(coro)


def _done_job(env, character=None, output=None, kind="generate"):
    out = output or env.image("output.png")
    job_id = env.job(
        state="done", character=character, outputs=json.dumps([out]),
        delivery="sent", snapshot=snap(), kind=kind,
    )
    return job_id, out


def _children(env, parent_id):
    return env.db.execute(
        "SELECT * FROM jobs WHERE parent_job_id=? ORDER BY id", (parent_id,)
    ).fetchall()


def _real_image(tmp_path, size, name="photo.png"):
    path = tmp_path / name
    Image.new("RGB", size, "white").save(path)
    return str(path)


# --- queueing --------------------------------------------------------------

def test_upscale_keeps_aspect_ratio(env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPSCALE_W", 1080)
    monkeypatch.setattr(config, "UPSCALE_H", 1350)
    src = _real_image(tmp_path, (100, 200))
    parent, _ = _done_job(env, output=src)
    run(env.dispatch(env.message(f"/upscale {parent}")))
    [child] = _children(env, parent)
    params = Snapshot.from_json(child["snapshot"]).effective_params()
    assert (params["width"], params["height"]) == (675, 1350)


def test_fit_in_box():
    from comfybot.derived import fit_in_box
    assert fit_in_box(1000, 1000, 1080, 1350) == (1080, 1080)
    assert fit_in_box(2000, 1000, 1080, 1350) == (1080, 540)


def test_upscale_refuses_video(env):
    parent, _ = _done_job(env, output=env.image("clip.mp4"))
    with pytest.raises(ValueError):
        run(derived.request_upscale(env.row(parent)))


# --- scheduling ------------------------------------------------------------

def test_run_now_bypasses_night_window(env, monkeypatch, tmp_path):
    normal = env.job(state="queued")
    parent, _ = _done_job(env, output=_real_image(tmp_path, (100, 200)))
    run(derived.request_upscale(env.row(parent)))
    [upscale] = _children(env, parent)

    monkeypatch.setattr(worker, "can_start", lambda: False)
    assert worker.next_queued_job()["id"] == upscale["id"]
    assert worker.comfy_needed()

    monkeypatch.setattr(worker, "can_start", lambda: True)
    assert worker.next_queued_job()["id"] == upscale["id"]
    rt.JOBS.cancel(upscale["id"])
    assert worker.next_queued_job()["id"] == normal


def test_closed_window_without_run_now_needs_nothing(env, monkeypatch):
    env.job(state="queued")
    monkeypatch.setattr(worker, "can_start", lambda: False)
    assert worker.next_queued_job() is None
    assert not worker.comfy_needed()


# --- running and delivery --------------------------------------------------

def test_deliver_sends_preview_and_original(env):
    job_id, out = _done_job(env, character="anna")
    env.db.execute("UPDATE jobs SET delivery='pending' WHERE id=?", (job_id,))
    run(delivery.deliver(job_id))
    (p1, c1, k1), (p2, c2, k2) = env.tg.files
    assert p1 == p2 == out
    assert k1.get("photo") and k1.get("keyboard")
    assert not k2.get("photo") and not k2.get("keyboard")
    assert c2 == "📎 Original"
    assert env.row(job_id)["delivery"] == "sent"


def test_deliver_falls_back_to_document_when_preview_fails(env):
    job_id, out = _done_job(env)
    env.tg.file_error = "photo"
    run(delivery.deliver(job_id))
    [(path, caption, kwargs)] = env.tg.files
    assert path == out
    assert "done." in caption and kwargs.get("keyboard")
    assert env.row(job_id)["delivery"] == "sent"


def test_upscale_result_has_no_upscale_button(env):
    job_id, _ = _done_job(env, kind="upscale")
    assert delivery.result_keyboard(env.row(job_id)) is None


def test_migration_v2_is_idempotent(env):
    env.db.execute("PRAGMA user_version=1")
    db.migrate(env.db)
    # Later migrations apply on top.
    assert env.db.execute("PRAGMA user_version").fetchone()[0] >= 2
