# Plugins: your own commands, outside the repository

A plugin adds commands, buttons, `/describe` kinds, components and whole
chains of jobs to the bot, without changing the bot's code. Plugins stay
out of git (see `.gitignore`): you can keep them to yourself or in a
private repository of your own.

This guide is also written for an AI you ask to create a plugin: it
contains everything there is to know.

## Where they live

The bot looks for plugins in the folders listed in `PLUGINS_DIRS` in
the `.env`, separated by `:` (default `./plugins`). Each plugin is:

- a folder with `__init__.py` (it can hold other modules and a
  `components/` folder), or
- a single `.py` file.

Names starting with `_` or `.` are not loaded: `plugins/_example` is
the template to copy. To try it:

```bash
cp -r plugins/_example plugins/dice
```

Then restart the bot and send `/dice 20` in the chat.

To keep your plugins in a private repository, clone it wherever you
like and add its folder to `PLUGINS_DIRS`:

```
PLUGINS_DIRS=./plugins:./private/plugins
```

## register(bot)

At startup the bot imports the plugin and calls its `register(bot)`
function. That is where the plugin declares what it adds:

| Call | What it adds |
|---|---|
| `bot.command("/name", handler, help="...")` | a command; `help` is the line shown by `/help` |
| `bot.button("action", handler)` | the answer to taps on buttons made with `plugins.button(text, "action", payload)` |
| `bot.result_button(make)` | a button under every delivered result: `make(job)` returns a button or `None` |
| `bot.describe("/name", model=..., system_prompt=..., env_key="", header="", show_analysis=False, help="")` | a command like `/describe` with its own model and system prompt |
| `bot.components(folder)` | a folder of the plugin's components, shaped like `components/` |
| `bot.before_command("/prompt", handler)` | runs before one of the bot's commands: `handler(cmd)` returns `True` if it handled it, `False` to leave it to the bot |
| `bot.button("action", handler, answers=True)` | as above, but the handler answers the tap itself (`rt.TG.answer(request.callback_id, text)`) |
| `bot.job_kind("kind", ...)` | a job kind for chains (see below) |
| `bot.prepare_input(hook)` | `async hook(path, role) -> path`: every input of a job goes through here before the upload to ComfyUI |
| `bot.after_job(hook)` | `async hook(job_id) -> text or None`: after every job finished in ComfyUI, before delivery; the text goes to the chat |
| `bot.lora_target("NodeClass", attach)` | the character's LoRA on a node of that class: `attach(graph, node_id, cfg)` changes the graph (`cfg`: `name`, `strength`); without it, the bot puts the LoRA before the sampler |
| `bot.schema(setup)` | `setup(conn)`: creates or updates the plugin's tables (`CREATE TABLE IF NOT EXISTS`, new columns with `db.add_columns`) |

The declarations only count if `register` finishes without errors. A
plugin that fails to load (an error on import or in `register`, a
command or button name already taken) is skipped with a message in the
log, and the rest of the bot starts anyway.

A plugin cannot take a name the bot already uses: `/create`, `/add`,
`/list`, `/describe`, `/describeimg`, the commands in `/help` and the
bot's button prefixes (`submit`, `cancel`, `pick`, `upscale`,
`batchstart`, `describe`).

## Handlers

Handlers are `async` functions.

**Command:** `async def handler(cmd)`. `cmd` is an `inbound.Command`
with:

- `chat`: the chat to answer;
- `owner`: the Telegram user;
- `name`: the command, with the slash;
- `args`: the text after the command;
- `reply`: the photo, file or video the message replies to, or that
  carries the command as a caption. `None` if there is none. A photo is
  downloaded with `await images.acquire(cmd.reply)`, a video with
  `await rt.TG.download_video(cmd.reply.message)` (to recognise it:
  `videos.is_video_message(cmd.reply.message)`); both return the local
  path;
- `message_id`: the command's message.

The bot itself does not use videos: a video only reaches plugin
commands, as a caption or in a reply.

**Button:** `async def handler(request, payload)`.

- `request` is an `inbound.ButtonPress`, with `chat`, `owner` and
  `message_id`.
- `payload` is the text put in the button. `callback_data` is limited
  to 64 bytes in all: for longer data, save the rest somewhere and pass
  only an id.

**Errors:** a `ValueError` raised by a handler goes back to the chat as
«Not done: …». Use it for user errors, for example a wrong use of the
command.

## What the bot offers

The services are in `comfybot.runtime` (usually imported as `rt`):

- `await rt.TG.text(chat, text, keyboard=None)`: sends a message. The
  `keyboard` is a list of rows of buttons.
- `await rt.TG.file(chat, path, caption)`: sends a file.
- `await rt.TG.call(method, payload)`: any Bot API method, for example
  `sendChatAction`.
- `rt.JOBS.owned(job_id, owner)`: the job's row (`ValueError` if it does
  not exist or is not the user's).
- `rt.JOBS.create_draft(owner, chat, prompt, component)`: creates a new
  draft; `jobs.summary(job_id)` shows it with the Start button.

Other useful modules:

- **The text model:** `describer.complete(model, config.LLAMA_URL,
  messages)` calls the llama.cpp server and returns the answer's text.
  `messages` uses the OpenAI format.
- **Characters:** `characters.char_row(owner, name)` and
  `characters.character_ref_paths(owner, name)` for the reference
  photos.
- **Derived jobs,** i.e. jobs made on a result that start right away,
  even by day, like upscale:
  1. `snap = snapshot.for_derived(kind, [images], [roles], title)`;
  2. `rt.JOBS.enqueue_derived(job, kind, snap.to_json())`.

  It needs a component with `"derived": true` and the id equal to
  `kind`, declared with `bot.components(...)`. The worker runs it and
  delivers the result like other jobs.

The bot's code is documented module by module: the map is in
`comfybot/__init__.py`.

## Chains of jobs

For a job made of several steps (for example: an image for each piece
of a video, then a clip for each image, then the final edit) the plugin
declares its job kinds with `bot.job_kind`:

- **container** (`container=True`): the draft the user sees and starts.
  It never goes to ComfyUI. Options: `summary(job_id)` (the summary),
  `start(job_id, owner)` (Start and `/submit`), `edit_fields` and
  `edit_hint` (what can be changed in the draft), `queue_header(job)`
  and `queue_order(job)` (how it shows in `/queue`);
- **steps** (`chain=True`): the child jobs. In `/queue` they sit under
  the container (`queue_label(job)` to describe them) and are cancelled
  with it. `intermediate=True`: the result is a passing one, only the
  preview is delivered, without buttons;
- **bot jobs** (`run=async run(job, out)`): the bot runs them, not
  ComfyUI (for example an ffmpeg edit). `run` claims the job with
  `rt.JOBS.claim(id)` and closes it with `rt.JOBS.run_done(id, [file])`
  or `rt.JOBS.run_fail(id, error)`. `gathers=True`: if one of the jobs
  it waits for is cancelled, it drops it from its list instead of being
  cancelled.

The operations on the Job store (`rt.JOBS`):

- `create_container(owner, chat, kind, character, prompt, [(message_id,
  path)], origin, extra=fn(conn, job_id))`: the container draft;
  `extra` writes the plugin's tables in the same transaction;
- `start_chain(job_id, snapshot, children, final)`: starts the draft and
  creates the children. Each child is a dict with `ref`, `kind`,
  `segment`, `component`, `prompt`, `snapshot` (JSON, for example from
  `snapshot.for_chain`) and `after` (the `ref`s it waits for). In the
  snapshot's assets `"@ref"` becomes that job's output;
- `redo_chain(...)`: redoes part of the chain with new jobs;
- `children(job_id, kinds)`, `finish_container(job_id)`.

A component used only by chains can have `"chain_only": true`: it does
not show among the ones the user picks for drafts.

## Tests

The bot's tests are in `tests/`. Their harness (`tests/harness.py`: a
temporary database, a fake Telegram and ComfyUI) can be used for a
plugin's tests too. A `conftest.py` next to the plugin's tests does
this:

```python
import sys
from pathlib import Path

import pytest

ROOT = Path("path/to/the/bot")           # the folder with comfybot/
sys.path.insert(0, str(ROOT / "tests"))
from harness import *  # noqa: E402,F401,F403
from comfybot import plugins  # noqa: E402


@pytest.fixture(autouse=True)
def my_plugins(env):
    plugins.load_all([Path(__file__).parents[1] / "plugins"])
    yield
    plugins.reset()
```

In the tests, `env.dispatch(env.message("/dice 6"))` sends a message as
if it came from Telegram, and `env.tg.texts` holds the answers. Test
files must have names different from those in `tests/`.
