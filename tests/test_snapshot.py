"""Snapshot: format, compiling, effective parameters; checking derived
components; restarting jobs (migration 4)."""

import asyncio

import pytest

from conftest import OWNER, snap
from comfybot import catalog, check, db, delivery, jobs, snapshot
from comfybot.snapshot import Snapshot


def run(coro):
    return asyncio.run(coro)


# --- format -----------------------------------------------------------------------

def test_round_trip():
    original = Snapshot.from_json(snap(positive="hello", variant="v"))
    assert Snapshot.from_json(original.to_json()) == original


def test_old_format_is_refused():
    old = '{"manifest": {}, "workflow": {}, "assets": [], "positive": ""}'
    with pytest.raises(ValueError, match="earlier version"):
        Snapshot.from_json(old)


# --- building --------------------------------------------------------------------

def test_for_component_freezes_effective_params():
    plan = [("/a.png", "scene")]
    s = snapshot.for_component(
        "edit", plan, "p", "p", "off",
        {"seed": 7, "size": {"width": 768, "height": 1280}, "label": "v1"},
    )
    params = s.effective_params()
    assert params["model"] == "edit_model.safetensors"
    assert params["seed"] == 7 and params["steps"] == 8
    assert (params["width"], params["height"]) == (768, 1280)
    assert s.variant == "v1" and s.title == ""
    # The catalog stays as it was: overrides only apply to the snapshot.
    manifest, _ = catalog.load_component("edit")
    assert manifest["size"] == {"width": 768, "height": 1024}


def test_compile_fills_slots_prompt_and_prefix():
    s = snapshot.for_component(
        "edit", [("/a.png", "scene")], "a lighthouse", "", "off",
    )
    graph = s.compile(["a.png"], "nightbot/1_tok")
    assert graph["470"]["inputs"]["image"] == "a.png"
    # Slots 2..10 unused: removed from the graph.
    assert "475" not in graph and "483" not in graph
    # Enhancer off: the prompt goes into the off_prompt field.
    assert graph["459:474"]["inputs"]["prompt"] == "a lighthouse"
    assert graph["503"]["inputs"]["filename_prefix"] == "nightbot/1_tok"
    # Compiling does not change the snapshot.
    assert s.workflow["470"]["inputs"]["image"] != "a.png"


def test_derived_snapshot_has_no_prompt():
    s = snapshot.for_derived(
        "upscale", ["/a.png"], ["output"], "🔍 Upscale",
        size={"width": 810, "height": 1350},
    )
    assert s.prompt_field is None
    assert s.effective_params() == {
        "model": "4x_upscale.pth",
        "width": 810, "height": 1350,
    }
    graph = s.compile(["a.png"], "nightbot/2_tok")
    assert graph["1"]["inputs"]["image"] == "a.png"


# --- start message --------------------------------------------------------------

def test_started_message_shows_effective_params(env):
    job_id = env.job(component="text", prompt="a lighthouse")
    run(jobs.start(job_id, OWNER))
    run(delivery.started_message(job_id))
    text = env.tg.texts[-1]
    assert "Text → image · RUNNING" in text
    assert (
        "⚙️ model text_model.gguf · 566×1080 px · 12 steps"
    ) in text


def test_describe_params_for_a_video():
    line = delivery.describe_params({
        "model": "wan.gguf", "width": 384, "height": 640, "length": 121,
        "fps": 24, "steps": 12, "sampler": "uni_pc", "scheduler": "simple",
        "cfg": 5, "denoise": 1.0, "seed": 3,
    })
    assert line == (
        "model wan.gguf · 384×640 px · 121 frames · 24 fps · 12 steps · "
        "uni_pc/simple · cfg 5 · denoise 1 · seed 3"
    )


# --- check -----------------------------------------------------------------------

def test_check_covers_derived_components(monkeypatch, capsys):
    async def no_nodes():
        return {}
    monkeypatch.setattr(check, "fetch_object_info", no_nodes)
    assert check.run() == 1
    out = capsys.readouterr().out
    assert "node 2: UpscaleModelLoader is not installed in ComfyUI" in out


# --- migration 4 ----------------------------------------------------------------

def test_restart_migration_clears_jobs_only(env):
    job_id = env.job(state="done")
    env.scene(job_id, env.image())
    env.character("anna", [env.image()])
    env.db.execute("PRAGMA user_version=3")
    db.migrate(env.db)
    count = lambda table: env.db.execute(  # noqa: E731
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]
    assert count("jobs") == count("inputs") == 0
    assert count("characters") == count("character_refs") == 1
    assert env.job() == 1
    assert env.db.execute("PRAGMA user_version").fetchone()[0] == len(
        db.MIGRATIONS
    )
