"""Test harness (tests/conftest.py imports all of it; a plugin's tests
import it the same way from their own conftest).

comfybot.config reads the environment on import: test variables are set
here BEFORE the import, so tests use a temporary DATA_DIR and never touch
real data. load_dotenv() does not override variables already set. The
test database is opened the way main.init_database does it; Telegram and
ComfyUI are fakes (rt.TG, rt.ENGINE).
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))

_DATA = Path(tempfile.mkdtemp(prefix="nightbot-test-"))

# The test components: the fake ones in tests/fixtures/components (the
# repository has no real components), copied with `enabled` set here.
ENABLED_IN_TESTS = {"text", "edit", "upscale", "video", "video_from_photo"}


def _test_components():
    target = Path(tempfile.mkdtemp(prefix="nightbot-components-"))
    shutil.copytree(FIXTURES / "components", target, dirs_exist_ok=True)
    for path in target.glob("*/manifest.json"):
        manifest = json.loads(path.read_text())
        manifest["enabled"] = manifest.get("id") in ENABLED_IN_TESTS
        path.write_text(json.dumps(manifest, indent=2))
    return target


os.environ.update({
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_ALLOWED_USER_IDS": "1",
    "DATA_DIR": str(_DATA),
    "COMPONENTS_DIR": str(_test_components()),
    "COMFYUI_BASE_URL": "http://127.0.0.1:8188",
    "COMFYUI_MANAGE": "false",
    "EXECUTION_BACKEND": "local",
    "NIGHT_MODE_DEFAULT": "false",
    "PLUGINS_DIRS": "",     # no plugins: tests load them by hand
    "AUTO_COMPONENTS": "edit,text",
})

from comfybot import (  # noqa: E402
    bot, characters, db, engine, inbound, main, plugins,
)
from comfybot import runtime as rt  # noqa: E402

main.prepare_data_dir()
main.init_database()

OWNER = 1
CHAT = 1


def snap(**fields):
    """A minimal snapshot, already serialised, to set up jobs in tests."""
    from comfybot.snapshot import Snapshot
    base = {"name": "test", "workflow": {}, "images": [], "output_nodes": []}
    return Snapshot(**{**base, **fields}).to_json()


class FakeTG:
    def __init__(self):
        self.texts = []
        self.files = []
        self.file_error = None  # e.g. "photo": sending as a photo fails
        self.keyboards = []
        self.calls = []
        self.downloads = []
        self.edits = []      # (type, message_id, text, keyboard)
        self.answers = []    # texts of the button answers
        self.next_message_id = 500

    async def text(self, chat, text, keyboard=None, reply_to=None):
        self.texts.append(text)
        self.keyboards.append(keyboard)

    async def download(self, message, text=False):
        """The message's file: `_content` (text) or a fake PNG."""
        self.downloads.append(message["message_id"])
        name = f"{message['chat']['id']}_{message['message_id']}"
        if text:
            path = _DATA / "inputs" / f"{name}.txt"
            path.write_text(message.get("_content", ""))
        else:
            path = _DATA / "inputs" / f"{name}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        return str(path)

    async def download_video(self, message):
        """The message's video: the file in `_video` (a path)."""
        self.downloads.append(message["message_id"])
        return message["_video"]

    async def file(self, chat, path, caption="", **kwargs):
        if self.file_error and kwargs.get(self.file_error):
            raise RuntimeError("send failed")
        self.files.append((path, caption, kwargs))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    async def call(self, method, payload=None, files=None):
        self.calls.append(method)
        return {}

    async def edit_keyboard(self, chat, message_id, keyboard):
        self.edits.append(("keyboard", message_id, None, keyboard))

    async def edit_text(self, chat, message_id, text, keyboard=None):
        self.edits.append(("text", message_id, text, keyboard))

    async def answer(self, callback_id, text=""):
        self.answers.append(text)

    def all_text(self):
        return "\n".join(self.texts)


class FakeEngine:
    """A fake ComfyUI: the submission states are a scripted sequence.

    `statuses`: what status() returns on each call, in order; the last
    one repeats. An exception in the list is raised.
    """

    def __init__(self):
        self.uploaded = []
        self.submitted = []
        self.cancelled = []
        self.statuses = []
        self.cancel_result = engine.ABSENT
        self.cancel_error = None
        self.busy = False
        self.found = None
        self.on_upload = None
        self.upload_error = None

    async def health(self):
        return {}

    async def upload_input(self, path):
        if self.on_upload:
            await self.on_upload(path)
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append(path)
        return Path(path).name

    async def submit(self, graph, token):
        self.submitted.append((graph, token))
        return "pid-1"

    async def find_submission(self, token):
        return self.found

    async def status(self, prompt_id):
        assert self.statuses, "submission state not scripted in the test"
        status = (
            self.statuses.pop(0) if len(self.statuses) > 1
            else self.statuses[0]
        )
        if isinstance(status, Exception):
            raise status
        return status

    async def cancel(self, prompt_id):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append(prompt_id)
        return self.cancel_result

    async def is_busy(self):
        return self.busy

    async def download(self, output, path):
        Path(path).write_bytes(b"output")
        return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    conn = rt.DB
    for table in (
        "jobs", "inputs", "characters", "character_refs", "descriptions",
        "settings",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    db.ensure_defaults(night_default=False)
    plugins.reset()
    tg = FakeTG()
    engine = FakeEngine()
    monkeypatch.setattr(rt, "TG", tg)
    monkeypatch.setattr(rt, "ENGINE", engine)

    counter = {"n": 0}

    def image(name="img.png"):
        counter["n"] += 1
        path = tmp_path / f"{counter['n']}_{name}"
        path.write_bytes(b"\x89PNG fake")
        return str(path)

    def job(state="draft", component="edit",
            prompt="a portrait", character=None, **fields):
        counter["n"] += 1
        cur = conn.execute(
            "INSERT INTO jobs(source_key, owner, chat, component, character,"
            " prompt, state, token, created) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"test:{counter['n']}", OWNER, CHAT, component, character,
             prompt, state, f"tok{counter['n']}", 0.0),
        )
        job_id = cur.lastrowid
        if fields:
            # Data setup: any column, without the lifecycle rules.
            columns = ", ".join(f"{key}=?" for key in fields)
            conn.execute(
                f"UPDATE jobs SET {columns} WHERE id=?",
                (*fields.values(), job_id),
            )
        conn.commit()
        return job_id

    def scene(job_id, *paths):
        for i, path in enumerate(paths):
            conn.execute(
                "INSERT INTO inputs(job_id, message_id, path) VALUES(?,?,?)",
                (job_id, i + 1, path),
            )
        conn.commit()

    def character(name, refs=(), caption=""):
        conn.execute(
            "INSERT INTO characters(owner, name, caption) VALUES(?,?,?)",
            (OWNER, name, caption),
        )
        conn.commit()
        for path in refs:
            characters.attach_char_ref(OWNER, name, path)

    def row(job_id):
        return conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    async def dispatch(message):
        """Like a message arriving from Telegram, but user errors come out
        as exceptions instead of in the chat."""
        request = inbound.parse({"message": message}, {OWNER})
        await bot.dispatch(request)

    def message(text):
        return {
            "text": text, "chat": {"id": CHAT, "type": "private"},
            "message_id": 999, "from": {"id": OWNER},
        }

    return SimpleNamespace(
        db=conn, tg=tg, engine=engine, image=image, job=job,
        scene=scene, character=character, row=row, message=message,
        dispatch=dispatch,
        data=_DATA,
    )
