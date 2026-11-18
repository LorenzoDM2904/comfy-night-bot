"""/list: the character chosen in the JSON."""

import asyncio
import json

import pytest

from conftest import OWNER


def run(coro):
    return asyncio.run(coro)


def send_list(env, data):
    run(env.dispatch(env.message("/list " + json.dumps(data))))


def list_jobs(env):
    return env.db.execute(
        "SELECT * FROM jobs WHERE owner=? ORDER BY id", (OWNER,)
    ).fetchall()


def test_general_character_queues_every_job_on_its_references(env):
    env.character("alice", refs=[env.image("alice.jpg")])
    send_list(env, {
        "character": "Alice",
        "prompts": [{"prompt": "in profile"}, {"prompt": "from behind"}],
    })
    rows = list_jobs(env)
    assert [r["character"] for r in rows] == ["alice", "alice"]
    # No image in the JSON: the character's references are enough.
    assert all(r["state"] == "queued" for r in rows)
    assert all(r["component"] == "edit" for r in rows)
    assert "2 of 2 jobs queued (character: alice)" in env.tg.texts[-1]


def test_item_character_overrides_the_general_one(env):
    env.character("alice", refs=[env.image()])
    env.character("bea", refs=[env.image()])
    send_list(env, {
        "character": "alice",
        "prompts": [
            {"prompt": "a"},
            {"prompt": "b", "character": "bea"},
            {"prompt": "c", "character": "off", "workflow": "text"},
        ],
    })
    rows = list_jobs(env)
    assert [r["character"] for r in rows] == ["alice", "bea", None]
    # An entry without character or image: they stay drafts for /add.
    assert all(r["state"] == "draft" for r in rows)


def test_unknown_character_creates_nothing(env):
    env.character("alice")
    with pytest.raises(ValueError, match="no character called «anna».*alice"):
        send_list(env, {
            "prompts": [
                {"prompt": "a"},
                {"prompt": "b", "character": "anna"},
            ],
        })
    assert list_jobs(env) == []


def test_without_character_nothing_changes(env):
    send_list(env, {"prompts": [{"prompt": "a"}]})
    rows = list_jobs(env)
    assert rows[0]["character"] is None
    assert rows[0]["state"] == "draft"
