"""Lighting presets, /matrix, describing in two steps."""

import asyncio
import json

import pytest

from conftest import OWNER
from comfybot import (
    delivery, describer, jobs, lighting, runtime as rt,
)
from comfybot.snapshot import Snapshot


def run(coro):
    return asyncio.run(coro)


def snapshot(env, job_id):
    return json.loads(env.row(job_id)["snapshot"])


def text_job(env, **fields):
    return env.job(component="text", prompt="a woman at a bar", **fields)


# --- light ----------------------------------------------------------------

def test_no_light_by_default(env):
    job_id = text_job(env)
    run(jobs.start(job_id, OWNER))
    snap = snapshot(env, job_id)
    assert snap["positive"] == "a woman at a bar"
    assert snap["light"] == "off"


def test_default_light_is_appended_at_submit(env):
    run(env.dispatch(env.message("/light sun")))
    job_id = text_job(env)
    run(jobs.start(job_id, OWNER))
    snap = snapshot(env, job_id)
    assert snap["positive"] == (
        "a woman at a bar\n" + lighting.PRESETS["sun"]
    )
    assert snap["light"] == "sun"
    # The social caption stays without the technical light sentence.
    assert snap["caption"] == "a woman at a bar"


def test_job_light_overrides_default_and_can_be_reset(env):
    run(env.dispatch(env.message("/light sun")))
    job_id = text_job(env)
    run(env.dispatch(env.message(f"/light {job_id} OVERCAST")))
    assert env.row(job_id)["light"] == "overcast"
    assert "Light: overcast" in env.tg.texts[-1]
    run(env.dispatch(env.message(f"/light {job_id} default")))
    assert env.row(job_id)["light"] is None
    assert "Light: sun (default)" in env.tg.texts[-1]


def test_job_light_off_beats_default(env):
    run(env.dispatch(env.message("/light flash")))
    job_id = text_job(env, light="off")
    run(jobs.start(job_id, OWNER))
    assert "Lighting" not in snapshot(env, job_id)["positive"]


def test_unknown_light_is_rejected(env):
    with pytest.raises(ValueError, match="Unknown light"):
        run(env.dispatch(env.message("/light neon")))


def test_light_only_on_drafts(env):
    job_id = text_job(env, state="queued")
    with pytest.raises(ValueError, match="draft"):
        run(env.dispatch(env.message(f"/light {job_id} sun")))


def test_light_list(env):
    run(env.dispatch(env.message("/light")))
    text = env.tg.texts[-1]
    assert "Default light: off" in text
    assert all(name in text for name in lighting.PRESETS)


def test_started_message_shows_light(env):
    job_id = text_job(env, light="studio")
    run(jobs.start(job_id, OWNER))
    run(delivery.started_message(job_id))
    assert "💡 Light: studio" in env.tg.all_text()


# --- /matrix --------------------------------------------------------------

def _edit_source(env):
    job_id = env.job(prompt="portrait in a kitchen", state="done")
    env.scene(job_id, env.image("scene.png"))
    return job_id


def _variants(env, source_id):
    return env.db.execute(
        "SELECT * FROM jobs WHERE parent_job_id=? ORDER BY id", (source_id,)
    ).fetchall()


def test_matrix_creates_controlled_variants(env):
    source = _edit_source(env)
    run(env.dispatch(env.message(
        f"/matrix {source} light=sun,flash size=384x640,768x1280 seed=7"
    )))
    rows = _variants(env, source)
    assert len(rows) == 4
    seen = set()
    for row in rows:
        assert row["state"] == "queued" and row["run_now"] == 0
        snap = Snapshot.from_json(row["snapshot"])
        params = snap.effective_params()
        assert params["seed"] == 7
        size = (params["width"], params["height"])
        light = snap.light
        seen.add((light, size))
        assert snap.positive.startswith("portrait in a kitchen\n")
        assert snap.positive.endswith(lighting.PRESETS[light])
        assert snap.assets and snap.assets[0].endswith("scene.png")
        assert snap.variant == (
            f"variant of #{source} · light {light} · "
            f"{size[0]}×{size[1]} · seed 7"
        )
    assert seen == {
        ("sun", (384, 640)), ("sun", (768, 1280)),
        ("flash", (384, 640)), ("flash", (768, 1280)),
    }
    summary = env.tg.texts[-1]
    assert "4 variants, seed 7" in summary
    # A single summary message, not one per variant.
    assert sum("queued." in t for t in env.tg.texts) == 0


def test_matrix_without_seed_uses_one_seed_for_all(env):
    source = _edit_source(env)
    run(env.dispatch(
        env.message(f"/matrix {source} light=sun,studio,sunset now")))
    rows = _variants(env, source)
    seeds = {
        Snapshot.from_json(r["snapshot"]).effective_params()["seed"]
        for r in rows
    }
    assert len(rows) == 3 and len(seeds) == 1
    assert all(r["run_now"] == 1 for r in rows)
    # Light only: the resolution stays the manifest's.
    params = Snapshot.from_json(rows[0]["snapshot"]).effective_params()
    assert params["width"] == 768


@pytest.mark.parametrize("args, error", [
    ("", "Usage"),
    ("seed=3", "Usage"),
    ("light=neon", "Unknown light"),
    ("size=12", "Invalid size"),
    ("size=10x10", "out of range"),
    ("colour=red", "Usage"),
    ("light=sun,flash,studio,sunset size=1x1", "out of range"),
])
def test_matrix_bad_arguments(env, args, error):
    source = _edit_source(env)
    with pytest.raises(ValueError, match=error):
        run(env.dispatch(env.message(f"/matrix {source} {args}")))
    assert _variants(env, source) == []


def test_matrix_limit(env):
    source = _edit_source(env)
    sizes = ",".join(f"{64 * i}x640" for i in range(2, 6))
    with pytest.raises(ValueError, match="maximum"):
        run(env.dispatch(env.message(
            f"/matrix {source} light=sun,flash,studio,sunset size={sizes}"
        )))


def test_variant_delivery_shows_label(env):
    source = _edit_source(env)
    run(env.dispatch(
        env.message(f"/matrix {source} light=sun seed=1")))
    row = _variants(env, source)[0]
    out = env.image("out.png")
    env.db.execute(
        "UPDATE jobs SET state='done', outputs=? WHERE id=?",
        (json.dumps([out]), row["id"]),
    )
    env.db.commit()
    run(delivery.deliver(row["id"]))
    caption = env.tg.files[0][1]
    assert caption.startswith(f"Job #{row['id']} done.")
    assert "🧪 variant of" in caption and "light sun" in caption


# --- describing in two steps --------------------------------------------

def test_default_prompts_leave_lighting_out():
    for kind in ("analyze", "describe", "describeimg"):
        prompt = describer.get_system_prompt(kind)
        assert describer.NO_LIGHTING in prompt
        assert "golden hour, moody glow" not in prompt


def test_describe_is_two_calls(monkeypatch, tmp_path):
    calls = []

    async def fake_chat(model, base_url, system_prompt, content):
        calls.append((system_prompt, content))
        return "ANALYSIS" if len(calls) == 1 else "FINAL PROMPT"

    monkeypatch.setattr(describer, "chat", fake_chat)
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"\xff\xd8 fake")
    analysis, prompt = run(describer.describe(
        str(image), "describeimg", "m", "http://x", directive="at the seaside",
    ))
    assert (analysis, prompt) == ("ANALYSIS", "FINAL PROMPT")
    first, second = calls
    assert first[0] == describer.get_system_prompt("analyze")
    assert any(part.get("type") == "image_url" for part in first[1])
    assert second[0] == describer.get_system_prompt("describeimg")
    assert isinstance(second[1], str)  # text only: no photo
    assert "ANALYSIS" in second[1] and "at the seaside" in second[1]


# --- drafts without a character------------------------------------------

def test_new_drafts_have_no_character(env):
    # Even if a default was set earlier with /use.
    rt.DB.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
        (f"character:{OWNER}", "alice"),
    )
    rt.DB.commit()
    env.character("alice")
    run(env.dispatch(env.message("a woman at a bar")))
    job = env.db.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert job["character"] is None
    assert f"/character {job['id']} name" in env.tg.texts[-1]
    rt.DB.execute("DELETE FROM settings WHERE key=?", (f"character:{OWNER}",))
    rt.DB.commit()


def test_use_command_is_gone(env):
    with pytest.raises(ValueError, match="/character <ID> <name>"):
        run(env.dispatch(env.message("/use alice")))


def test_system_prompt_from_a_file(monkeypatch, tmp_path):
    prompt = tmp_path / "describe.txt"
    prompt.write_text("My prompt.\n")
    monkeypatch.setenv("DESCRIBE_SYSTEM_PROMPT", f"@{prompt}")
    assert describer.get_system_prompt("describe") == "My prompt."
    monkeypatch.setenv("DESCRIBE_SYSTEM_PROMPT", f"@{tmp_path / 'missing.txt'}")
    with pytest.raises(ValueError, match="cannot read"):
        describer.get_system_prompt("describe")
    monkeypatch.setenv("DESCRIBE_SYSTEM_PROMPT", "plain text")
    assert describer.get_system_prompt("describe") == "plain text"
