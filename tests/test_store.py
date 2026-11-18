"""Job store: tests through its interface, on an in-memory SQLite."""

import asyncio
import json

import pytest

from comfybot import db, delivery
from comfybot import runtime as rt
from comfybot.store import LIFECYCLE, JobStore

from conftest import CHAT, OWNER, snap

OTHER = 2


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store():
    return JobStore(db.open_database(":memory:"))


def force_state(store, job_id, state):
    """Data setup only: puts a job in any state."""
    store.conn.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))
    store.conn.commit()


# --- creation -----------------------------------------------------------------

def test_create_draft_is_always_new(store):
    a = store.create_draft(OWNER, CHAT, "a portrait", origin="x")
    b = store.create_draft(OWNER, CHAT, "a portrait", origin="x")
    assert a["id"] != b["id"]
    assert a["state"] == b["state"] == "draft"
    assert a["component"] == "" and a["character"] is None


def test_album_photos_share_one_draft(store):
    first = store.album_draft(OWNER, CHAT, "alb1", "prompt")
    second = store.album_draft(OWNER, CHAT, "alb1", "")
    assert first["id"] == second["id"]
    assert store.album_draft(OWNER, CHAT, "alb2", "")["id"] != first["id"]


def test_album_of_another_owner_is_refused(store):
    store.album_draft(OWNER, CHAT, "alb1", "")
    with pytest.raises(ValueError, match="another user"):
        store.album_draft(OTHER, CHAT, "alb1", "")


def test_late_album_photo_opens_a_new_draft(store):
    first = store.album_draft(OWNER, CHAT, "alb1", "")
    force_state(store, first["id"], "queued")
    late = store.album_draft(OWNER, CHAT, "alb1", "")
    assert late["id"] != first["id"]
    assert late["state"] == "draft"


def test_variant_copies_source_and_its_images(store):
    source = store.create_draft(OWNER, CHAT, "prompt", "text")
    store.edit_draft(source["id"], character="alice")
    store.attach_input(source["id"], 1, "/a.png")
    store.attach_input(source["id"], 2, "/b.png")
    source = store.get(source["id"])

    vid = store.create_variant(source, CHAT, "sun", {"seed": 7}, run_now=True)
    variant = store.get(vid)
    assert variant["state"] == "draft"
    assert variant["prompt"] == "prompt"
    assert variant["component"] == "text"
    assert variant["character"] == "alice"
    assert variant["light"] == "sun"
    assert variant["parent_job_id"] == source["id"]
    assert variant["run_now"] == 1
    assert json.loads(variant["overrides"]) == {"seed": 7}
    assert store.scene_inputs(vid) == ["/a.png", "/b.png"]


# --- lifecycle ----------------------------------------------------------------

STATES = (
    "draft", "queued", "preparing", "submitting", "running",
    "done", "failed", "unknown", "skipped", "cancelled",
)

# Every step of the table, called through its operation.
STEPS = {
    "confirm": lambda s, j: s.confirm(j, "{}", "text"),
    "cancel": lambda s, j: s.cancel(j),
    "claim": lambda s, j: s.claim(j),
    "release": lambda s, j: s.release(j),
    "begin_submit": lambda s, j: s.begin_submit(j),
    "mark_running": lambda s, j: s.mark_running(j, "pid"),
    "complete": lambda s, j: s.complete(j, ["/out.png"]),
    "fail": lambda s, j: s.fail(j, "error"),
    "mark_uncertain": lambda s, j: s.mark_uncertain(j, "uncertain"),
    "skip": lambda s, j: s.skip(j, "skipped"),
    "retry": lambda s, j: s.retry(j),
    "run_done": lambda s, j: s.run_done(j, ["/edited.mp4"]),
    "run_fail": lambda s, j: s.run_fail(j, "error"),
    "supersede": lambda s, j: s.supersede(j),
    "finish_container": lambda s, j: s.finish_container(j),
}


def test_every_step_has_an_operation():
    assert set(STEPS) == set(LIFECYCLE)


@pytest.mark.parametrize("step", sorted(LIFECYCLE))
@pytest.mark.parametrize("state", STATES)
def test_lifecycle_table_is_enforced(store, step, state):
    sources, target = LIFECYCLE[step]
    job = store.create_draft(OWNER, CHAT, "p")
    force_state(store, job["id"], state)
    allowed = STEPS[step](store, job["id"])
    assert allowed == (state in sources)
    assert store.get(job["id"])["state"] == (target if allowed else state)


def test_nothing_leaves_submitting_except_the_outcome_of_the_send():
    # From 'submitting' a request may already be in ComfyUI: only the
    # outcome of the send (or finding it again) can move the job.
    leaving = {
        step for step, (sources, _) in LIFECYCLE.items()
        if "submitting" in sources
    }
    assert leaving == {"mark_running", "fail", "mark_uncertain"}


def test_done_and_cancelled_are_final():
    for state in ("done", "cancelled"):
        assert not any(state in src for src, _ in LIFECYCLE.values())


def test_complete_stores_outputs_and_waits_for_delivery(store):
    job = store.create_draft(OWNER, CHAT, "p")
    force_state(store, job["id"], "running")
    store.conn.execute(
        "UPDATE jobs SET delivery='sent', error='x' WHERE id=?", (job["id"],)
    )
    assert store.complete(job["id"], ["/a.png", "/b.mp4"])
    row = store.get(job["id"])
    assert json.loads(row["outputs"]) == ["/a.png", "/b.mp4"]
    assert row["delivery"] == "pending"
    assert row["error"] is None


def test_retry_starts_a_fresh_attempt(store):
    job = store.create_draft(OWNER, CHAT, "p")
    force_state(store, job["id"], "running")
    store.mark_uncertain(job["id"], "vanished")
    store.conn.execute(
        "UPDATE jobs SET prompt_id='old', started=1 WHERE id=?", (job["id"],)
    )
    assert store.retry(job["id"])
    row = store.get(job["id"])
    assert row["state"] == "queued"
    assert row["token"] != job["token"]
    assert row["prompt_id"] is None and row["started"] is None
    assert row["error"] is None


def test_release_interrupted_requeues_only_preparing(store):
    ids = {}
    for state in ("preparing", "submitting", "running"):
        ids[state] = store.create_draft(OWNER, CHAT, "p")["id"]
        force_state(store, ids[state], state)
    store.release_interrupted()
    assert store.get(ids["preparing"])["state"] == "queued"
    assert store.get(ids["submitting"])["state"] == "submitting"
    assert store.get(ids["running"])["state"] == "running"


# --- derived jobs --------------------------------------------------------------

def done_job(store):
    job = store.create_draft(OWNER, CHAT, "p")
    force_state(store, job["id"], "running")
    store.complete(job["id"], ["/out.png"])
    return store.get(job["id"])


def test_derived_job_enters_the_queue_directly(store):
    parent = done_job(store)
    job_id, created = store.enqueue_derived(parent, "upscale", '{"x": 1}')
    job = store.get(job_id)
    assert created
    assert job["state"] == "queued" and job["run_now"] == 1
    assert job["kind"] == "upscale" and job["parent_job_id"] == parent["id"]
    assert json.loads(job["snapshot"]) == {"x": 1}


def test_double_tap_on_derived_reuses_the_active_one(store):
    parent = done_job(store)
    first, _ = store.enqueue_derived(parent, "swap", "{}", "alice")
    again, created = store.enqueue_derived(parent, "swap", "{}", "alice")
    assert (again, created) == (first, False)
    other, created = store.enqueue_derived(parent, "upscale", "{}")
    assert created and other != first

    force_state(store, first, "done")
    later, created = store.enqueue_derived(parent, "swap", "{}", "alice")
    assert created and later != first


# --- delivery --------------------------------------------------------------------

def delivery_of(store, job_id):
    return store.get(job_id)["delivery"]


def test_delivery_goes_pending_sending_sent(store):
    job = done_job(store)
    assert delivery_of(store, job["id"]) == "pending"
    assert store.begin_delivery(job["id"])
    assert delivery_of(store, job["id"]) == "sending"
    assert store.finish_delivery(job["id"], sent=True)
    assert delivery_of(store, job["id"]) == "sent"


def test_failed_delivery_becomes_unknown_and_can_be_repeated(store):
    job = done_job(store)
    store.begin_delivery(job["id"])
    store.finish_delivery(job["id"], sent=False)
    assert delivery_of(store, job["id"]) == "unknown"
    assert store.begin_delivery(job["id"])


def test_no_second_delivery_while_one_is_running(store):
    job = done_job(store)
    assert store.begin_delivery(job["id"])
    assert not store.begin_delivery(job["id"])


def test_only_finished_jobs_are_delivered(store):
    job = store.create_draft(OWNER, CHAT, "p")
    force_state(store, job["id"], "running")
    assert not store.begin_delivery(job["id"])


def test_interrupted_deliveries_are_recovered_at_startup(store):
    job = done_job(store)
    store.begin_delivery(job["id"])
    store.recover_interrupted_deliveries()
    assert delivery_of(store, job["id"]) == "unknown"


def test_result_during_a_delivery_is_refused(env):
    out = env.image("out.png")
    job_id = env.job(
        state="done", outputs=json.dumps([out]), delivery="sending",
        snapshot=snap(),
    )
    with pytest.raises(ValueError, match="already in progress"):
        run(delivery.deliver(job_id))
    assert env.tg.files == []


# --- query ----------------------------------------------------------------------

def test_open_jobs_hide_closed_ones_and_other_owners(store):
    ids = {}
    for state in ("draft", "queued", "failed", "done", "cancelled", "skipped"):
        ids[state] = store.create_draft(OWNER, CHAT, "p")["id"]
        force_state(store, ids[state], state)
    store.create_draft(OTHER, CHAT, "p")
    open_ids = [r["id"] for r in store.open_jobs(OWNER)]
    assert open_ids == [ids["draft"], ids["queued"], ids["failed"]]


def test_active_job_is_the_one_sent_to_comfy(store):
    queued = store.create_draft(OWNER, CHAT, "p")["id"]
    force_state(store, queued, "queued")
    assert store.active_job() is None
    running = store.create_draft(OWNER, CHAT, "p")["id"]
    force_state(store, running, "running")
    assert store.active_job()["id"] == running


def test_comfy_is_needed_for_a_pending_delivery(store):
    assert not store.needs_comfy(window_open=True)
    done_job(store)
    assert store.needs_comfy(window_open=False)


# --- drafts --------------------------------------------------------------------

def test_edit_draft_changes_only_drafts(store):
    job = store.create_draft(OWNER, CHAT, "old")
    assert store.edit_draft(job["id"], prompt="new", light="sun")
    assert store.get(job["id"])["prompt"] == "new"

    force_state(store, job["id"], "queued")
    assert not store.edit_draft(job["id"], prompt="late")
    assert store.get(job["id"])["prompt"] == "new"


def test_edit_draft_rejects_fields_outside_the_draft(store):
    job = store.create_draft(OWNER, CHAT, "p")
    with pytest.raises(TypeError):
        store.edit_draft(job["id"], state="queued")


def test_attach_input_is_idempotent_and_ordered(store):
    job = store.create_draft(OWNER, CHAT, "p")
    assert store.attach_input(job["id"], 5, "/b.png")
    assert store.attach_input(job["id"], 5, "/b.png")
    assert store.attach_input(job["id"], 2, "/a.png")
    assert store.scene_inputs(job["id"]) == ["/a.png", "/b.png"]
    assert store.has_input(job["id"], 5)
    assert not store.has_input(job["id"], 9)


def test_attach_input_refuses_confirmed_jobs(store):
    job = store.create_draft(OWNER, CHAT, "p")
    force_state(store, job["id"], "queued")
    assert not store.attach_input(job["id"], 1, "/a.png")
    assert store.scene_inputs(job["id"]) == []


# --- reads -----------------------------------------------------------------------

def test_owned_hides_jobs_of_other_owners(store):
    job = store.create_draft(OWNER, CHAT, "p")
    assert store.owned(job["id"], OWNER)["id"] == job["id"]
    with pytest.raises(ValueError, match="not accessible"):
        store.owned(job["id"], OTHER)
    with pytest.raises(ValueError, match="not accessible"):
        store.owned(9999, OWNER)


# --- from the user's point of view ----------------------------------------------

def test_two_replies_to_the_same_photo_make_two_drafts(env):
    photo = {"message_id": 50, "photo": [{"file_id": "f"}]}
    path = env.image()

    async def download(message, text=False):
        return path

    env.tg.download = download

    def reply(mid, text):
        msg = env.message(text)
        msg.update(message_id=mid, reply_to_message=photo)
        return msg

    run(env.dispatch(reply(51, "first")))
    run(env.dispatch(reply(52, "second")))
    rows = env.db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    assert [r["prompt"] for r in rows] == ["first", "second"]
    assert all(rt.JOBS.scene_inputs(r["id"]) for r in rows)
