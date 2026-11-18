"""Telegram input: reading updates, the single rules of /create and
/add, buttons, dispatch, commands and /list."""

import asyncio
import json
import re

import pytest

from conftest import CHAT, OWNER
from comfybot import bot, buttons, commands, inbound, linkfetch
from comfybot import runtime as rt
from comfybot.images import LastPhoto, Link, TelegramFile

PNG = b"\x89PNG\r\n\x1a\n fake"


def run(coro):
    return asyncio.run(coro)


def text(body, mid=100, **extra):
    msg = {
        "message_id": mid, "text": body, "from": {"id": OWNER},
        "chat": {"id": CHAT, "type": "private"},
    }
    msg.update(extra)
    return msg


def photo(mid=10, caption=None, album=None):
    msg = {
        "message_id": mid, "photo": [{"file_id": f"f{mid}"}],
        "from": {"id": OWNER}, "chat": {"id": CHAT, "type": "private"},
    }
    if caption is not None:
        msg["caption"] = caption
    if album:
        msg["media_group_id"] = album
    return msg


def text_file(mid=20, caption=None, name="prompt.txt", content="from the file"):
    msg = {
        "message_id": mid, "from": {"id": OWNER},
        "chat": {"id": CHAT, "type": "private"},
        "document": {"file_id": "d", "file_name": name},
        "_content": content,
    }
    if caption is not None:
        msg["caption"] = caption
    return msg


def parse(message):
    return inbound.parse({"message": message}, {OWNER})


def drafts(env):
    return env.db.execute("SELECT * FROM jobs ORDER BY id").fetchall()


# --- /create: a single rule ------------------------------------------------------

@pytest.mark.parametrize("rest, expected", [
    ("text a lighthouse", ("text", "a lighthouse")),
    ("a lighthouse", ("", "a lighthouse")),
    ("text\nline one\nline two", ("text", "line one\nline two")),
    ("text", ("text", "")),
    ("", ("", "")),
    ("swap a face", ("", "swap a face")),  # not a component
])
def test_split_create(rest, expected):
    assert inbound.split_create(rest) == expected


@pytest.mark.parametrize("message", [
    text("/create text a lighthouse"),
    text("/create@nightbot text a lighthouse"),
    photo(caption="/create text a lighthouse"),
    text("/create text a lighthouse", reply_to_message=photo(mid=5)),
])
def test_create_reads_the_same_everywhere(message):
    request = parse(message)
    assert isinstance(request, inbound.CreateDraft)
    assert (request.component, request.prompt) == ("text", "a lighthouse")


def test_create_on_a_text_file():
    request = parse(text_file(caption="/create text"))
    assert request.component == "text"
    assert request.text_file == TelegramFile(text_file(caption="/create text"))


def test_create_on_a_text_file_without_component_is_a_normal_draft(env):
    run(env.dispatch(text_file(caption="/create", content="a lighthouse")))
    [row] = drafts(env)
    assert (row["component"], row["prompt"]) == ("", "a lighthouse")


def test_create_without_text_needs_an_image(env):
    with pytest.raises(ValueError, match="Write the text"):
        parse(text("/create text"))
    run(env.dispatch(photo(caption="/create text")))
    [row] = drafts(env)
    assert row["component"] == "text" and row["prompt"] == ""
    assert len(rt.JOBS.scene_inputs(row["id"])) == 1


def test_wrong_caption_command_is_refused():
    with pytest.raises(ValueError, match="is not valid as a caption"):
        parse(photo(caption="/prompt 3 hello"))
    with pytest.raises(ValueError, match="is not valid with a text file"):
        parse(text_file(caption="/add 3"))


# --- /add: a single rule ---------------------------------------------------------

def test_add_forms():
    by_caption = parse(photo(mid=7, caption="/add 3"))
    by_reply = parse(text("/add 3..5", reply_to_message=photo(mid=7)))
    by_text = parse(text("/add 5-3"))
    assert by_caption == inbound.AddImage(
        CHAT, OWNER, 3, 3, TelegramFile(photo(mid=7, caption="/add 3"))
    )
    assert (by_reply.first, by_reply.last) == (3, 5)
    assert by_reply.image == TelegramFile(photo(mid=7))
    assert (by_text.first, by_text.last, by_text.image) == (3, 5, LastPhoto(CHAT))


def test_the_same_photo_enters_a_draft_once(env):
    target = env.job()
    run(env.dispatch(photo(mid=7)))                    # the chat's last photo
    run(env.dispatch(photo(mid=7, caption=f"/add {target}")))
    run(env.dispatch(text(f"/add {target}")))
    run(env.dispatch(text(f"/add {target}", reply_to_message=photo(mid=7))))
    assert len(rt.JOBS.scene_inputs(target)) == 1


def test_add_text_without_a_recent_photo(env):
    target = env.job()
    with pytest.raises(ValueError, match="No recent photo"):
        run(env.dispatch(text(f"/add {target}")))


def test_add_range_offers_start_all(env):
    first, second = env.job(), env.job()
    run(env.dispatch(photo(mid=8, caption=f"/add {first}..{second}")))
    assert all(len(rt.JOBS.scene_inputs(j)) == 1 for j in (first, second))
    [[button]] = env.tg.keyboards[-1]
    assert buttons.decode(button["callback_data"]) == buttons.Press(
        "start_all", job_id=first, last_id=second
    )


def test_bad_range():
    with pytest.raises(ValueError, match="Usage: /add ID"):
        parse(text("/add three"))


# --- other forms -----------------------------------------------------------------

def test_album_makes_one_draft_with_the_caption_anywhere(env):
    async def album():
        await bot.dispatch(parse(photo(mid=1, album="a")))
        await bot.dispatch(parse(photo(mid=2, album="a", caption="a lighthouse")))
        await bot.dispatch(parse(photo(mid=3, album="a")))
    run(album())
    [row] = drafts(env)
    assert row["prompt"] == "a lighthouse"
    assert len(rt.JOBS.scene_inputs(row["id"])) == 3


def test_links_become_the_draft_images(env, monkeypatch):
    async def fake_download(url, max_bytes):
        return PNG
    monkeypatch.setattr(linkfetch, "download_image", fake_download)
    url = "https://example.com/a.jpg"
    request = parse(text(f"a lighthouse {url}"))
    assert request.links == (url,) and request.prompt == "a lighthouse"
    run(bot.dispatch(request))
    [row] = drafts(env)
    [path] = rt.JOBS.scene_inputs(row["id"])
    assert path.endswith(".png")


def test_describe_sources():
    url = "https://pin.it/abc"
    assert parse(text(f"/describe {url} at the seaside")).image == Link(url)
    assert parse(text(f"/describe {url} at the seaside")).directive == "at the seaside"
    reply = parse(text("/describeimg", reply_to_message=photo(mid=4)))
    assert reply.kind == "describeimg"
    assert reply.image == TelegramFile(photo(mid=4))
    with pytest.raises(ValueError, match="reply to a photo"):
        parse(text("/describe"))


def test_other_commands_in_reply_keep_the_photo():
    request = parse(text("/character anna", reply_to_message=photo(mid=4)))
    assert request == inbound.Command(
        CHAT, OWNER, "/character", "anna", TelegramFile(photo(mid=4))
    )


# --- buttons ---------------------------------------------------------------------

@pytest.mark.parametrize("button, press", [
    (buttons.start(4), buttons.Press("start", job_id=4)),
    (buttons.cancel(4), buttons.Press("cancel", job_id=4)),
    (buttons.pick(4, "text", "Z"), buttons.Press(
        "pick", job_id=4, component="text")),
    (buttons.upscale(4), buttons.Press("upscale", job_id=4)),
    (buttons.describe(9), buttons.Press("describe", description=9)),
    (buttons.start_all(2, 6), buttons.Press(
        "start_all", job_id=2, last_id=6)),
])
def test_buttons_round_trip(button, press):
    assert len(button["callback_data"].encode()) <= 64
    assert buttons.decode(button["callback_data"]) == press


def test_older_buttons():
    assert buttons.decode("describe:9:p") == buttons.Press(
        "describe", description=9
    )
    for data in ("describeimg:9", "submit:x", "pick:3", "boh:1", ""):
        with pytest.raises(ValueError):
            buttons.decode(data)


def callback(data, user=OWNER):
    return {"callback_query": {
        "id": "cb", "data": data, "from": {"id": user},
        "message": {"chat": {"id": CHAT}},
    }}


def test_button_press_is_acknowledged_and_runs(env):
    job_id = env.job(component="text")
    run(bot.process_update(callback(f"submit:{job_id}")))
    assert env.tg.calls[0] == "answerCallbackQuery"
    assert env.row(job_id)["state"] == "queued"


def test_invalid_button_answers_in_chat(env):
    run(bot.process_update(callback("boh:1")))
    assert env.tg.texts[-1] == "Not done: Invalid button."


# --- dispatch --------------------------------------------------------------------

def test_strangers_are_ignored(env):
    msg = text("hello")
    msg["from"] = {"id": 999}
    run(bot.process_update({"message": msg}))
    run(bot.process_update(callback("submit:1", user=999)))
    assert env.tg.texts == [] and drafts(env) == []


def test_group_chats_are_refused(env):
    msg = text("hello")
    msg["chat"]["type"] = "group"
    run(bot.process_update({"message": msg}))
    assert env.tg.texts == ["Use a private chat."]


def test_errors_reach_the_chat(env):
    run(bot.process_update({"message": text("/status 12345")}))
    assert env.tg.texts[-1].startswith("Not done: No such job")


# --- commands --------------------------------------------------------------------

def test_help_lists_every_command():
    every = set(commands.COMMANDS) | {"/create", "/add", "/list"}
    every |= set(inbound.DESCRIBE)
    every -= {"/start", "/use"}
    missing = [
        c for c in every
        if not re.search(re.escape(c) + r"(?![\w])", commands.HELP)
    ]
    assert missing == []


def test_workflow_command_is_gone(env):
    with pytest.raises(ValueError, match="Unknown command"):
        run(env.dispatch(text("/workflow text")))


def test_reference_sends_every_photo(env):
    env.character("anna", [env.image("a.png"), env.image("b.png")])
    run(env.dispatch(text("/reference anna")))
    assert [caption for _, caption, _ in env.tg.files] == [
        "Reference 1/2: anna", "Reference 2/2: anna",
    ]


def test_character_reference_from_a_reply(env):
    run(env.dispatch(text("/character anna", reply_to_message=photo(mid=4))))
    assert "1 reference photos" in env.tg.texts[-1]
    run(env.dispatch(text("/caption anna red hair")))
    run(env.dispatch(text("/characters")))
    assert "anna (1 photos) — red hair" in env.tg.texts[-1]


# --- /list ------------------------------------------------------------------------

def test_list_without_image_prepares_drafts(env):
    body = json.dumps({"prompts": [
        {"prompt": "one"}, {"prompt": "two", "workflow": "text"},
    ]})
    run(env.dispatch(text(f"/list {body}")))
    rows = drafts(env)
    assert [(r["prompt"], r["component"]) for r in rows] == [
        ("one", ""), ("two", "text"),
    ]
    assert all(r["state"] == "draft" for r in rows)
    [[button]] = env.tg.keyboards[-1]
    assert button["callback_data"] == f"batchstart:{rows[0]['id']}:{rows[1]['id']}"


def test_list_from_a_json_file_with_a_shared_photo(env):
    body = json.dumps({"prompts": [{"prompt": "one"}]})
    run(env.dispatch(text_file(caption="/list", name="b.json", content=body)))
    [row] = drafts(env)
    assert row["state"] == "draft"
    run(env.dispatch(text("/list " + body, mid=101,
                          reply_to_message=photo(mid=4))))
    rows = drafts(env)
    assert rows[-1]["state"] == "queued"
    assert rows[-1]["component"] == "edit"
    assert "1 of 1 jobs queued" in env.tg.texts[-1]


def test_list_alone_shows_the_schema(env):
    run(env.dispatch(text("/list")))
    assert env.tg.texts[-1].startswith("📋 /list")


# --- /cancel all at once---------------------------------------------------------

def test_cancel_range_cancels_what_it_can(env):
    draft = env.job()
    queued = env.job(state="queued")
    running = env.job(state="running")
    done = env.job(state="cancelled")
    run(env.dispatch(text(f"/cancel {draft}..{done}")))
    assert env.row(draft)["state"] == env.row(queued)["state"] == "cancelled"
    assert env.row(running)["state"] == "running"
    assert env.tg.texts[-1] == (
        f"🗑 Cancelled 2 jobs: #{draft} #{queued}\n"
        f"Already cancelled: #{done}\n"
        f"Cannot be cancelled (already sent to ComfyUI or finished): #{running}"
    )


def test_cancel_range_skips_jobs_of_others(env):
    mine = env.job()
    other = env.job()
    env.db.execute("UPDATE jobs SET owner=? WHERE id=?", (OWNER + 1, other))
    run(env.dispatch(text(f"/cancel {mine}-{other}")))
    assert env.row(other)["state"] == "draft"
    assert env.tg.texts[-1] == f"🗑 Cancelled 1 job: #{mine}"


def test_cancel_range_without_own_jobs(env):
    with pytest.raises(ValueError, match="None of your jobs between #50 and #60"):
        run(env.dispatch(text("/cancel 50..60")))


def test_cancel_single_id_unchanged(env):
    job_id = env.job()
    run(env.dispatch(text(f"/cancel {job_id}")))
    assert env.row(job_id)["state"] == "cancelled"
    assert env.tg.texts[-1] == f"Job #{job_id} cancelled."


def test_cancel_without_id():
    with pytest.raises(ValueError, match="Usage: /cancel ID or /cancel X..Y"):
        run(commands.run(inbound.Command(CHAT, OWNER, "/cancel")))
