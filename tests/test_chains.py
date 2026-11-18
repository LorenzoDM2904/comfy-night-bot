"""Plugin chains: job kinds, jobs that wait for each other, jobs run by
the bot, worker hooks and commands on videos (with a fake plugin,
«bundle»: a container draft with steps and a final join)."""

import asyncio
import json
import textwrap

import pytest
from PIL import Image

from conftest import CHAT, OWNER
from comfybot import (
    bot, commands, confirmation, derived, inbound, jobs, plugins, worker,
)
from comfybot import runtime as rt
from comfybot.engine import Done, Output
from comfybot.snapshot import Snapshot

PLUGIN = '''
from comfybot import runtime as rt

CALLS = []


def register(bot):
    bot.schema(lambda conn: conn.execute(
        "CREATE TABLE IF NOT EXISTS bundle_note(job_id INTEGER, body TEXT)"))
    bot.job_kind(
        "bundle", container=True, summary=summary, start=start,
        edit_fields=("character",), edit_hint="A bundle changes step by step.",
        queue_header=lambda job: f"📦 bundle #{job['id']}",
        queue_order=lambda job: (job["kind"] == "join", -job["id"]),
    )
    bot.job_kind("step", chain=True, intermediate=True,
                 queue_label=lambda job: f"step {job['segment']}")
    bot.job_kind("join", chain=True, run=run, gathers=True)
    bot.command("/bundle", bundle)
    bot.before_command("/prompt", before_prompt)
    bot.button("tap", touched, answers=True)
    bot.prepare_input(prepare)
    bot.after_job(after)


async def summary(job_id):
    CALLS.append(("summary", job_id))


async def start(job_id, owner):
    CALLS.append(("start", job_id, owner))


async def run(job, out):
    CALLS.append(("run", job["id"], out.parent.name))
    if rt.JOBS.claim(job["id"]):
        rt.JOBS.run_done(job["id"], [str(out.with_suffix(".txt"))])


async def bundle(cmd):
    CALLS.append(("bundle", cmd.args, cmd.reply, cmd.message_id))


async def before_prompt(cmd):
    if "." not in cmd.args.split(" ")[0]:
        return False
    CALLS.append(("prompt", cmd.args))
    return True


async def touched(request, payload):
    CALLS.append(("tap", payload))


async def prepare(path, role):
    CALLS.append(("prepare", role))
    return path


async def after(job_id):
    CALLS.append(("after", job_id))
    return f"checked #{job_id}"
'''


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bundle(env, tmp_path):
    folder = tmp_path / "plugin"
    folder.mkdir()
    (folder / "bundle.py").write_text(textwrap.dedent(PLUGIN))
    assert plugins.load_all([folder]) == ["bundle"]
    module = __import__(plugins.module_name_of("bundle"))
    module.CALLS.clear()
    return module


def container(env, character="anna"):
    return rt.JOBS.create_container(
        OWNER, CHAT, "bundle", character, "overview",
        [(10, env.image("video.mp4"))], "bundle",
    )


def child(ref, segment, after=(), assets=()):
    snap = Snapshot(name="x", workflow={}, images=[], output_nodes=[],
                    assets=list(assets))
    return {
        "ref": ref, "kind": "step", "segment": segment, "component": "c",
        "prompt": f"step {segment}", "snapshot": snap.to_json(),
        "after": list(after),
    }


def start_chain(env):
    parent = container(env)
    final = {"kind": "join", "snapshot": "{}", "after": ["a", "b"]}
    assert rt.JOBS.start_chain(parent["id"], "{}", [
        child("a", 1), child("b", 2, after=["a"], assets=["@a", "/x.png"]),
    ], final)
    rows = rt.JOBS.children(parent["id"], ("step", "join"))
    return parent["id"], {r["kind"] + str(r["segment"] or ""): r for r in rows}


def finish(job_id):
    rt.DB.execute(
        "UPDATE jobs SET state='done', outputs=? WHERE id=?",
        (json.dumps([f"/out/{job_id}.png"]), job_id),
    )
    rt.DB.commit()


# --- job kinds -------------------------------------------------------------------

def test_container_draft_has_its_inputs_and_rules(env, bundle):
    parent = container(env)
    assert parent["kind"] == "bundle" and parent["component"] == ""
    assert rt.JOBS.scene_inputs(parent["id"])[0].endswith("video.mp4")
    assert rt.JOBS.edit_draft(parent["id"], character="bob")
    with pytest.raises(ValueError, match="step by step"):
        rt.JOBS.edit_draft(parent["id"], prompt="other")
    with pytest.raises(ValueError, match="Photos cannot be added"):
        rt.JOBS.attach_input(parent["id"], 11, "/photo.png")


def test_container_summary_and_start_are_the_plugins(env, bundle):
    parent = container(env)
    run(jobs.summary(parent["id"]))
    run(jobs.start(parent["id"], OWNER))
    assert bundle.CALLS == [
        ("summary", parent["id"]), ("start", parent["id"], OWNER),
    ]
    with pytest.raises(ValueError, match="starts on its own"):
        confirmation.confirm(parent["id"], OWNER)


def test_plugin_tables_are_created(env, bundle):
    rt.DB.execute("INSERT INTO bundle_note VALUES(1, 'hello')")


def test_a_plugin_cannot_take_a_core_kind(env, tmp_path):
    (tmp_path / "a.py").write_text(textwrap.dedent('''
        def register(bot):
            bot.job_kind("upscale", chain=True)
    '''))
    assert plugins.load_all([tmp_path]) == []


# --- chain ----------------------------------------------------------------------

def test_chain_children_wait_for_each_other(env, bundle):
    parent_id, rows = start_chain(env)
    first, second, final = rows["step1"], rows["step2"], rows["join"]
    assert rt.JOBS.get(parent_id)["state"] == "queued"
    assert json.loads(second["depends_on"]) == [first["id"]]
    assets = json.loads(second["snapshot"])["assets"]
    assert assets == [f"job:{first['id']}", "/x.png"]
    assert json.loads(final["depends_on"]) == [first["id"], second["id"]]

    # The container and the join never go to ComfyUI.
    assert rt.JOBS.next_runnable(True)["id"] == first["id"]
    assert rt.JOBS.next_run() is None
    finish(first["id"])
    assert rt.JOBS.next_runnable(True)["id"] == second["id"]
    finish(second["id"])
    assert rt.JOBS.next_runnable(True) is None
    assert rt.JOBS.next_run()["id"] == final["id"]


def test_start_chain_only_once(env, bundle):
    parent = container(env)
    assert rt.JOBS.start_chain(parent["id"], "{}", [child("a", 1)])
    assert not rt.JOBS.start_chain(parent["id"], "{}", [child("a", 1)])


def test_worker_resolves_outputs_of_the_chain(env, bundle):
    _, rows = start_chain(env)
    first = rows["step1"]
    with pytest.raises(RuntimeError, match="has no result yet"):
        worker.resolve_asset(f"job:{first['id']}")
    out = env.image("made.png")
    rt.DB.execute(
        "UPDATE jobs SET state='done', outputs=? WHERE id=?",
        (json.dumps([out]), first["id"]),
    )
    assert worker.resolve_asset(f"job:{first['id']}") == out


def test_worker_runs_bot_jobs(env, bundle):
    _, rows = start_chain(env)
    finish(rows["step1"]["id"])
    finish(rows["step2"]["id"])
    final = rt.JOBS.next_run()
    run(worker.run_local(final))
    assert bundle.CALLS == [("run", final["id"], "outputs")]
    assert env.row(final["id"])["state"] == "done"


def test_a_cancelled_step_leaves_the_gathering_job(env, bundle):
    parent_id, rows = start_chain(env)
    final = rows["join"]
    rt.JOBS.cancel(rows["step2"]["id"])
    assert json.loads(env.row(final["id"])["depends_on"]) == [
        rows["step1"]["id"],
    ]
    assert env.row(final["id"])["state"] == "queued"
    rt.JOBS.cancel(rows["step1"]["id"])
    assert env.row(final["id"])["state"] == "cancelled"
    assert env.row(parent_id)["state"] == "cancelled"


def test_cancelling_the_container_cancels_the_chain(env, bundle):
    parent_id, rows = start_chain(env)
    rt.JOBS.cancel(parent_id)
    assert {env.row(r["id"])["state"] for r in rows.values()} == {"cancelled"}


def test_redo_chain_supersedes_and_adds_a_new_final(env, bundle):
    parent_id, rows = start_chain(env)
    finish(rows["step1"]["id"])
    ids = rt.JOBS.redo_chain(
        parent_id, [rows["step2"]["id"], rows["join"]["id"]],
        [child("b2", 2, after=[rows["step1"]["id"]])],
        {"kind": "join", "snapshot": "{}",
         "after": [rows["step1"]["id"], "b2"]},
    )
    assert env.row(rows["step2"]["id"])["state"] == "cancelled"
    assert env.row(rows["join"]["id"])["state"] == "cancelled"
    assert env.row(ids["b2"])["state"] == "queued"
    assert len(rt.JOBS.children(parent_id, ("join",))) == 2


def test_queue_groups_the_chain_under_the_container(env, bundle):
    normal = env.job(state="queued", component="text")
    parent_id, rows = start_chain(env)
    run(env.dispatch(env.message("/queue")))
    assert env.tg.texts[-1].split("\n") == [
        f"#{normal} ⏳ queued — text",
        f"📦 bundle #{parent_id}",
        f"    #{rows['step2']['id']} ⏳ queued — step 2",
        f"    #{rows['step1']['id']} ⏳ queued — step 1",
        f"    #{rows['join']['id']} ⏳ queued — join",
    ]


def test_queue_hides_the_chain_of_a_cancelled_container(env, bundle):
    parent_id, rows = start_chain(env)
    rt.DB.execute(
        "UPDATE jobs SET state='failed' WHERE id=?", (rows["step1"]["id"],),
    )
    rt.DB.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (parent_id,))
    rt.DB.commit()
    assert rt.JOBS.open_jobs(OWNER) == []


def test_intermediate_results_have_no_buttons(env, bundle):
    from comfybot import delivery
    _, rows = start_chain(env)
    assert delivery.result_keyboard(rows["step1"]) is None


# --- worker hooks -----------------------------------------------------------------

def test_worker_hooks_run_around_a_job(env, bundle, tmp_path):
    source = tmp_path / "photo.png"
    Image.new("RGB", (100, 200), "white").save(source)
    parent = env.job(state="done", outputs=json.dumps([str(source)]),
                     delivery="sent", snapshot="{}")
    run(derived.request_upscale(env.row(parent)))
    upscale = rt.JOBS.next_runnable(True)
    output = Snapshot.from_json(upscale["snapshot"]).output_nodes[0]
    env.engine.statuses = [Done({output: [Output("x.png")]})]
    run(worker.run_job(upscale))
    assert bundle.CALLS == [("prepare", "output"), ("after", upscale["id"])]
    assert env.tg.texts[-1] == f"checked #{upscale['id']}"


def test_a_failing_after_hook_does_not_stop_delivery(env, bundle, monkeypatch):
    async def broken(job_id):
        raise RuntimeError("boom")
    monkeypatch.setattr(plugins, "AFTER_JOB", [broken] + plugins.AFTER_JOB)
    assert run(plugins.after_job(7)) == ["checked #7"]


# --- commands ----------------------------------------------------------------------

def video(caption=None, mid=5):
    message = {
        "message_id": mid, "video": {"file_id": "v", "file_size": 10},
        "chat": {"id": CHAT, "type": "private"}, "from": {"id": OWNER},
    }
    if caption:
        message["caption"] = caption
    return message


def test_plugin_command_on_a_video(env, bundle):
    request = inbound.parse({"message": video("/bundle three")}, {OWNER})
    assert request == inbound.Command(
        CHAT, OWNER, "/bundle", "three", inbound.TelegramFile(video("/bundle three")),
    )
    assert request.message_id == 5
    reply = env.message("/bundle")
    reply["reply_to_message"] = video(mid=4)
    request = inbound.parse({"message": reply}, {OWNER})
    assert request.reply == inbound.TelegramFile(video(mid=4))


def test_videos_without_a_plugin_command_are_refused(env, bundle):
    with pytest.raises(ValueError, match="command that uses them"):
        inbound.parse({"message": video("a prompt")}, {OWNER})
    reply = env.message("hello")
    reply["reply_to_message"] = video(mid=4)
    assert isinstance(inbound.parse({"message": reply}, {OWNER}),
                      inbound.CreateDraft)


def test_before_command_can_take_over_a_core_command(env, bundle):
    run(env.dispatch(env.message("/prompt 12.3 another text")))
    assert bundle.CALLS == [("prompt", "12.3 another text")]
    job_id = env.job()
    run(env.dispatch(env.message(f"/prompt {job_id} new")))
    assert env.row(job_id)["prompt"] == "new"


def test_before_command_only_on_core_commands(env, tmp_path):
    (tmp_path / "a.py").write_text(textwrap.dedent('''
        def register(bot):
            bot.before_command("/made_up", None)
    '''))
    assert plugins.load_all([tmp_path]) == []


def test_self_answering_buttons_are_not_acknowledged(env, bundle):
    update = {"callback_query": {
        "id": "cb", "from": {"id": OWNER}, "data": "tap:9",
        "message": {"chat": {"id": CHAT}, "message_id": 3},
    }}
    run(bot.process_update(update))
    assert bundle.CALLS == [("tap", "9")]
    assert "answerCallbackQuery" not in env.tg.calls


def test_queue_without_plugins_still_works(env):
    job_id = env.job(state="queued", component="text")
    assert commands._queue_text(rt.JOBS.open_jobs(OWNER)) == (
        f"#{job_id} ⏳ queued — text"
    )

