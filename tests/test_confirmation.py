"""Confirmation: outcomes as values, then as chat messages."""

import asyncio
import json

import pytest

from conftest import CHAT, OWNER
from comfybot import batch, confirmation, jobs, matrix
from comfybot.confirmation import (
    AlreadyConfirmed, Confirmed, MissingPrompt, NeedsComponent,
)

TEXT_ONLY = {"text", "video"}
ONE_PHOTO = {"edit", "video_from_photo"}


def run(coro):
    return asyncio.run(coro)


def option_ids(outcome):
    return {option.component for option in outcome.options}


# --- outcomes-------------------------------------------------------------------

def test_missing_prompt(env):
    job_id = env.job(prompt="  ")
    assert confirmation.confirm(job_id, OWNER) == MissingPrompt(job_id)
    assert env.row(job_id)["state"] == "draft"


def test_already_confirmed(env):
    job_id = env.job(state="queued")
    assert confirmation.confirm(job_id, OWNER) == AlreadyConfirmed(job_id)


def test_another_owners_job_is_an_error(env):
    job_id = env.job()
    with pytest.raises(ValueError, match="not accessible"):
        confirmation.confirm(job_id, 2)


def test_no_component_and_several_compatible_asks(env):
    job_id = env.job(component="")
    outcome = confirmation.confirm(job_id, OWNER)
    assert isinstance(outcome, NeedsComponent)
    assert outcome.rejected is None
    assert option_ids(outcome) == TEXT_ONLY
    assert env.row(job_id)["state"] == "draft"


def test_one_photo_offers_edit_and_video(env):
    job_id = env.job(component="")
    env.scene(job_id, env.image())
    outcome = confirmation.confirm(job_id, OWNER)
    assert isinstance(outcome, NeedsComponent)
    assert option_ids(outcome) == ONE_PHOTO
    assert env.row(job_id)["state"] == "draft"


def test_single_compatible_component_is_chosen(env):
    job_id = env.job(component="")
    env.scene(job_id, env.image(), env.image())
    outcome = confirmation.confirm(job_id, OWNER)
    assert isinstance(outcome, Confirmed)
    assert outcome.component == "edit"
    assert outcome.auto_chosen
    row = env.row(job_id)
    assert row["state"] == "queued"
    assert row["component"] == "edit"


def test_chosen_but_incompatible_component_is_never_replaced(env):
    job_id = env.job(component="text")
    env.scene(job_id, env.image())
    outcome = confirmation.confirm(job_id, OWNER)
    assert isinstance(outcome, NeedsComponent)
    assert outcome.rejected == "text"
    assert "0 at most" in outcome.reason
    assert option_ids(outcome) == ONE_PHOTO
    row = env.row(job_id)
    assert row["state"] == "draft" and row["component"] == "text"


def test_no_compatible_component_at_all(env):
    job_id = env.job(component="")
    env.scene(job_id, *[env.image() for _ in range(11)])
    outcome = confirmation.confirm(job_id, OWNER)
    assert isinstance(outcome, NeedsComponent)
    assert outcome.options == [] and outcome.scene_count == 11


def test_picking_a_component_confirms(env):
    job_id = env.job(component="")
    outcome = confirmation.confirm(job_id, OWNER, component="text")
    assert isinstance(outcome, Confirmed)
    assert not outcome.auto_chosen
    assert env.row(job_id)["component"] == "text"


def test_picking_an_unknown_component_is_an_error(env):
    job_id = env.job(component="")
    with pytest.raises(ValueError, match="No such component"):
        confirmation.confirm(job_id, OWNER, component="nothing")
    assert env.row(job_id)["component"] == ""


@pytest.mark.parametrize("photos, expected", [
    (0, "text"), (1, "edit"),
])
def test_batch_uses_the_automatic_choice(env, photos, expected):
    job_id = env.job(component="")
    for _ in range(photos):
        env.scene(job_id, env.image())
    outcome = confirmation.confirm(job_id, OWNER, batch=True)
    assert isinstance(outcome, Confirmed)
    assert outcome.component == expected and outcome.auto_chosen


def test_confirmed_snapshot_freezes_the_plan(env):
    job_id = env.job()
    scene = env.image()
    env.scene(job_id, scene)
    outcome = confirmation.confirm(job_id, OWNER)
    snap = json.loads(env.row(job_id)["snapshot"])
    assert outcome.plan == [(scene, "scene")]
    assert snap["assets"] == [scene] and snap["roles"] == ["scene"]


# --- messages -----------------------------------------------------------------

def test_incompatible_choice_explains_then_offers_the_picker(env):
    job_id = env.job(component="text")
    env.scene(job_id, env.image())
    run(jobs.start(job_id, OWNER))
    explain, picker = env.tg.texts[-2:]
    assert "it cannot run with text" in explain
    assert f"/add {job_id}" in explain and f"/character {job_id}" in explain
    assert "choose the workflow" in picker


def test_automatic_choice_is_announced(env):
    job_id = env.job(component="")
    env.scene(job_id, env.image(), env.image())
    run(jobs.start(job_id, OWNER))
    assert "the only compatible one" in env.tg.texts[-1]
    assert f"Job #{job_id} queued." in env.tg.texts[-1]


def test_missing_prompt_message(env):
    job_id = env.job(prompt="")
    run(jobs.start(job_id, OWNER))
    assert env.tg.texts[-1] == f"Set the prompt: /prompt {job_id} text"


# --- callers ---------------------------------------------------------------------

def test_start_all_counts_only_confirmed_jobs(env):
    ok = env.job(component="")
    no_prompt = env.job(component="", prompt="")
    # It used to be counted as started: the picker was sent and the
    # call returned normally.
    incompatible = env.job(component="text")
    env.scene(incompatible, env.image())
    queued = env.job(state="queued")
    run(batch.batch_start(CHAT, OWNER, ok, queued))
    text = env.tg.texts[-1]
    assert text.startswith("▶️ Started 1 jobs.")
    assert f"Already confirmed: #{queued}" in text
    assert f"#{no_prompt}: the prompt is missing" in text
    assert f"#{incompatible}: text not compatible" in text
    assert env.row(ok)["component"] == "text"


def test_matrix_cancels_variants_that_cannot_start(env):
    # An edit without images or a character: no plan is possible.
    source = env.job(component="edit")
    run(matrix.run(CHAT, OWNER, source, ["light=sun"]))
    variant = env.db.execute(
        "SELECT * FROM jobs WHERE parent_job_id=?", (source,)
    ).fetchone()
    assert variant["state"] == "cancelled"
    assert "edit not compatible" in variant["error"]
    assert f"#{variant['id']} ✗" in env.tg.texts[-1]
    assert not any("choose the workflow" in t for t in env.tg.texts)
