"""Characters, input planning, atomic claim, uncertain outcomes, configuration."""

import asyncio
import json

import httpx
import pytest

from conftest import OWNER
from comfybot import comfy_process, confirmation, db, engine, jobs, worker
from comfybot.engine import Failed


def run(coro):
    return asyncio.run(coro)


# --- /characters -----------------------------------------------------------

def test_characters_lists_name_and_caption(env):
    env.character("anna", [env.image()], caption="red hair, freckles")
    run(env.dispatch(env.message("/characters")))
    out = env.tg.all_text()
    assert "anna (1 photos)" in out
    assert "red hair" in out


def test_characters_empty_message(env):
    run(env.dispatch(env.message("/characters")))
    assert "No characters" in env.tg.all_text()


# --- input plan -----------------------------------------------------------

MANIFEST_3 = {"images": [{}, {}, {}], "min_images": 1, "max_images": 3}


def test_plan_keeps_scene_and_trims_refs():
    from comfybot.planner import plan_inputs
    plan, unused = plan_inputs(["s1", "s2"], ["r1", "r2", "r3"], MANIFEST_3)
    assert plan == [("r1", "character"), ("s1", "scene"), ("s2", "scene")]
    assert unused == 2


def test_plan_refuses_too_many_scene_photos():
    from comfybot.planner import plan_inputs
    with pytest.raises(ValueError):
        plan_inputs(["s1", "s2", "s3", "s4"], [], MANIFEST_3)


def test_plan_refs_count_towards_minimum():
    from comfybot.planner import plan_inputs
    plan, unused = plan_inputs([], ["r1"], MANIFEST_3)
    assert plan == [("r1", "character")]
    with pytest.raises(ValueError):
        plan_inputs([], [], MANIFEST_3)


def test_compatibility_counts_character_refs(env):
    def ids(job_id):
        options = confirmation.component_options(env.row(job_id))
        return {option.component for option in options}

    plain = env.job(component="")
    assert "edit" not in ids(plain)
    assert "text" in ids(plain)
    env.character("anna", [env.image(), env.image()])
    assert "edit" in ids(env.job(component="", character="anna"))


def test_submit_with_only_character_refs(env):
    refs = [env.image("face.png"), env.image("body.png")]
    env.character("anna", refs, caption="adult woman")
    job_id = env.job(character="anna")
    run(jobs.start(job_id, OWNER))
    row = env.row(job_id)
    assert row["state"] == "queued"
    snap = json.loads(row["snapshot"])
    assert snap["assets"] == refs
    assert snap["roles"] == ["character", "character"]
    assert snap["positive"].startswith("adult woman\n")
    assert "Image 1 → character" in env.tg.all_text()


def test_submit_never_drops_scene_photos(env):
    refs = [env.image() for _ in range(10)]
    env.character("anna", refs)
    job_id = env.job(character="anna")
    scene = env.image("scene.png")
    env.scene(job_id, scene)
    run(jobs.start(job_id, OWNER))
    snap = json.loads(env.row(job_id)["snapshot"])
    assert len(snap["assets"]) == 10
    assert snap["assets"][-1] == scene
    assert snap["roles"][-1] == "scene"
    assert "1 character references not used" in env.tg.all_text()


def test_double_submit_queues_once(env):
    job_id = env.job()
    env.scene(job_id, env.image())
    run(jobs.start(job_id, OWNER))
    snap = env.row(job_id)["snapshot"]
    outcome = run(jobs.start(job_id, OWNER))
    assert isinstance(outcome, confirmation.AlreadyConfirmed)
    assert env.row(job_id)["snapshot"] == snap


# --- atomic claim ----------------------------------------------------------

def _queued_job(env):
    job_id = env.job()
    env.scene(job_id, env.image())
    run(jobs.start(job_id, OWNER))
    assert env.row(job_id)["state"] == "queued"
    return job_id


def test_cancel_during_upload_prevents_submission(env):
    job_id = _queued_job(env)

    async def cancel_meanwhile(path):
        assert env.row(job_id)["state"] == "preparing"
        await jobs.cancel_job(job_id, OWNER)

    env.engine.on_upload = cancel_meanwhile
    run(worker.run_job(env.row(job_id)))
    assert env.engine.submitted == []
    assert env.row(job_id)["state"] == "cancelled"


def test_upload_error_returns_job_to_queue(env):
    job_id = _queued_job(env)
    env.engine.upload_error = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        run(worker.run_job(env.row(job_id)))
    assert env.row(job_id)["state"] == "queued"
    assert env.engine.submitted == []


def test_cancel_refused_once_submitting(env):
    job_id = env.job(state="submitting")
    with pytest.raises(ValueError):
        run(jobs.cancel_job(job_id, OWNER))
    assert env.row(job_id)["state"] == "submitting"


def test_run_job_saves_attempt(env):
    job_id = _queued_job(env)
    token = env.row(job_id)["token"]
    env.engine.statuses = [Failed("error in node 9")]
    run(worker.run_job(env.row(job_id)))
    assert len(env.engine.submitted) == 1
    attempt = env.data / "attempts" / f"{job_id}_{token}.json"
    record = json.loads(attempt.read_text())
    assert record["roles"] == ["scene"]
    assert record["workflow"] == env.engine.submitted[0][0]
    assert env.row(job_id)["state"] == "failed"


# --- uncertain outcomes-------------------------------------------------------

def test_skip_unknown_cancels_remote_and_allows_retry(env):
    job_id = env.job(state="unknown", prompt_id="p1")
    env.engine.cancel_result = engine.REMOVED
    run(worker.skip_unknown(env.row(job_id)))
    assert env.engine.cancelled == ["p1"]
    row = env.row(job_id)
    assert row["state"] == "skipped"
    assert "removed from the ComfyUI queue" in row["error"]
    run(env.dispatch(env.message(f"/retry {job_id}")))
    row = env.row(job_id)
    assert row["state"] == "queued"
    assert row["prompt_id"] is None


def test_skip_unknown_reports_remote_failure(env):
    job_id = env.job(state="unknown", prompt_id="p1")
    env.engine.cancel_error = httpx.HTTPError("405")
    run(worker.skip_unknown(env.row(job_id)))
    row = env.row(job_id)
    assert row["state"] == "skipped"
    assert "FAILED" in row["error"]


def test_skip_unknown_reports_interrupted_run(env):
    job_id = env.job(state="unknown", prompt_id="p1")
    env.engine.cancel_result = engine.INTERRUPTED
    run(worker.skip_unknown(env.row(job_id)))
    assert "run interrupted in ComfyUI" in env.row(job_id)["error"]


def test_migration_marks_auto_skipped_jobs(env):
    skipped = env.job(
        state="cancelled", error="x | saltato automaticamente (boh)"
    )
    cancelled = env.job(state="cancelled")
    db.MIGRATIONS[0](env.db)
    assert env.row(skipped)["state"] == "skipped"
    assert env.row(cancelled)["state"] == "cancelled"


# --- configuration ---------------------------------------------------------

def test_comfy_argv_uses_configured_port(env):
    argv = comfy_process._comfy_argv()
    assert argv[argv.index("--port") + 1] == "8188"


def test_describer_reads_env_at_call_time(monkeypatch):
    from comfybot.describer import get_system_prompt
    monkeypatch.setenv("DESCRIBEIMG_SYSTEM_PROMPT", "TEST PROMPT")
    assert get_system_prompt("describeimg") == "TEST PROMPT"
    monkeypatch.delenv("DESCRIBEIMG_SYSTEM_PROMPT")
    assert get_system_prompt("describeimg") != "TEST PROMPT"


def test_cli_ensure_char_without_caption(monkeypatch, tmp_path):
    import characters
    monkeypatch.setattr(characters, "DB_PATH", tmp_path / "cli.sqlite3")
    conn = characters.connect()
    characters.ensure_char(conn, 1, "anna")
    characters.ensure_char(conn, 1, "anna", "a caption")
    characters.ensure_char(conn, 1, "anna")
    caption = conn.execute(
        "SELECT caption FROM characters WHERE name='anna'"
    ).fetchone()[0]
    assert caption == "a caption"
