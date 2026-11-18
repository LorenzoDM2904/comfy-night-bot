"""Text commands (/queue, /status, /character, ...).

COMMANDS maps each command name to its function. /create, /add,
/describe and /list are not here: inbound turns them into requests of
their own (draft, image, description, batch). Plugin commands live in
plugins.COMMANDS.
"""

from . import (
    catalog,
    characters,
    config,
    db,
    delivery,
    derived,
    failure,
    inbound,
    jobs,
    lighting,
    matrix,
    plugins,
    runtime as rt,
    store,
)


async def run(cmd):
    """Runs a Command from inbound."""
    for before in plugins.BEFORE.get(cmd.name, ()):
        if await before(cmd):
            return
    handler = COMMANDS.get(cmd.name) or plugins.COMMANDS.get(cmd.name)
    if handler is None:
        raise ValueError("Unknown command. Use /help.")
    await handler(cmd)


def _words(cmd, count):
    """The first `count` arguments; the last one holds the rest of the text."""
    return cmd.args.split(maxsplit=count - 1) if cmd.args else []


def _job_id(cmd, usage=None):
    words = _words(cmd, 2)
    if not words or not words[0].isdigit():
        raise ValueError(usage or f"Usage: {cmd.name} ID")
    return int(words[0])


async def _reply(cmd, text, keyboard=None):
    await rt.TG.text(cmd.chat, text, keyboard)


# --- help and components ------------------------------------------------------

async def _help(cmd):
    await _reply(cmd, HELP + plugins.help_text())


async def _workflows(cmd):
    text = "\n".join(
        f"{cid}: {m['name']} "
        f"({'enabled' if m.get('enabled') else 'to be configured'})"
        for cid, m in catalog.components().items()
    )
    await _reply(cmd, text or "No components.")


# --- queue and night ----------------------------------------------------------

async def _night(cmd):
    if cmd.args:
        if cmd.args not in ("on", "off"):
            raise ValueError("Usage: /night on or /night off")
        db.put_setting("night", "1" if cmd.args == "on" else "0")
    await _reply(
        cmd,
        f"Night: {'ON' if db.setting('night') == '1' else 'OFF'}\n"
        f"Window: {config.START:%H:%M}–{config.FINISH:%H:%M}, "
        f"{config.TZ.key}\n"
        f"Paused: {'yes' if db.setting('paused') == '1' else 'no'}",
    )


async def _pause(cmd):
    db.put_setting("paused", "1")
    await _reply(cmd, "Paused.")


async def _resume(cmd):
    db.put_setting("paused", "0")
    await _reply(cmd, "Resumed.")


QUEUE_STATES = {
    "draft": "📝 draft",
    "queued": "⏳ queued",
    "preparing": "⚙️ preparing",
    "submitting": "📤 submitting",
    "running": "▶️ running",
    "failed": "❌ failed",
    "unknown": "❓ uncertain outcome",
    "done": "✅ done",
}

def _state(row):
    return QUEUE_STATES.get(row["state"], row["state"])


def _job_line(row):
    return f"#{row['id']} {_state(row)} — {row['component'] or 'to be chosen'}"


def _container_line(parent):
    kind = plugins.kind(parent["kind"])
    if kind and kind.queue_header:
        return kind.queue_header(parent)
    return f"#{parent['id']} {_state(parent)} — {parent['kind']}"


def _child_line(child):
    kind = plugins.kind(child["kind"])
    what = kind.queue_label(child) if kind and kind.queue_label \
        else child["component"] or child["kind"]
    return f"    #{child['id']} {_state(child)} — {what}"


def _queue_text(rows):
    """The lines of /queue: single jobs in order, each plugin container
    draft in place of its first open job, with its chain indented below
    (in the order the plugin chooses)."""
    containers = plugins.kinds(container=True)
    chain = plugins.kinds(chain=True)
    groups, order = {}, []
    for row in rows:
        if row["kind"] in containers:
            key = row["id"]
        elif row["kind"] in chain and row["parent_job_id"]:
            key = row["parent_job_id"]
        else:
            order.append(row)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        if row["kind"] not in containers:
            groups[key].append(row)
    lines = []
    for item in order:
        if not isinstance(item, int):
            lines.append(_job_line(item))
            continue
        parent = rt.JOBS.get(item)
        if not parent:
            lines.append(f"#{item}")
            continue
        lines.append(_container_line(parent))
        kind = plugins.kind(parent["kind"])
        key = kind.queue_order if kind and kind.queue_order \
            else (lambda row: row["id"])
        lines += [_child_line(child) for child in sorted(groups[item], key=key)]
    return "\n".join(lines)


async def _queue(cmd):
    rows = rt.JOBS.open_jobs(cmd.owner)
    await _reply(cmd, _queue_text(rows) or "The queue is empty.")


# --- one job ------------------------------------------------------------------

async def _status(cmd):
    job = rt.JOBS.owned(_job_id(cmd), cmd.owner)
    await jobs.summary(job["id"])
    if job["error"]:
        await _reply(
            cmd, "Detail: " + failure.describe(job["error"])[:1500],
        )


async def _submit(cmd):
    await jobs.start(_job_id(cmd), cmd.owner)


async def _cancel(cmd):
    """/cancel ID or /cancel X..Y (all at once)."""
    lo, hi = inbound.parse_range(cmd.args, "/cancel")
    if lo == hi:
        await jobs.cancel_job(lo, cmd.owner)
    else:
        await jobs.cancel_range(cmd.chat, cmd.owner, lo, hi)


async def _result(cmd):
    job = rt.JOBS.owned(_job_id(cmd), cmd.owner)
    if job["state"] != "done":
        raise ValueError("The job has no final results yet.")
    await delivery.deliver(job["id"])


async def _retry(cmd):
    job = rt.JOBS.owned(_job_id(cmd), cmd.owner)
    # An explicit retry, never automatic for an uncertain outcome.
    if not rt.JOBS.retry(job["id"]):
        raise ValueError(
            "Retry is only allowed for "
            + "/".join(store.LIFECYCLE["retry"][0]) + "."
        )
    await _reply(
        cmd,
        "New attempt allowed. If the previous one is still in "
        "ComfyUI, the image may be generated twice.",
    )


async def _prompt(cmd):
    words = _words(cmd, 2)
    if len(words) < 2 or not words[0].isdigit():
        raise ValueError("Usage: /prompt ID text")
    job = rt.JOBS.owned(int(words[0]), cmd.owner)
    if not rt.JOBS.edit_draft(job["id"], prompt=words[1]):
        raise ValueError("Only a draft can be changed.")
    await jobs.summary(job["id"])


async def _light(cmd):
    args = cmd.args.split()
    if not args:
        return await _reply(cmd, lighting.describe_all(cmd.owner))
    if args[0].isdigit():
        if len(args) < 2:
            raise ValueError("Usage: /light <ID> <preset|off|default>")
        job = rt.JOBS.owned(int(args[0]), cmd.owner)
        value = args[1].lower()
        if not rt.JOBS.edit_draft(
            job["id"],
            light=None if value == "default" else lighting.parse(value),
        ):
            raise ValueError("Only a draft's light can be changed.")
        return await jobs.summary(job["id"])
    name = lighting.parse(args[0])
    db.put_setting(f"light:{cmd.owner}", name)
    await _reply(
        cmd,
        f"Default light: {name}. It applies to drafts confirmed from now "
        "on that have no light of their own.",
    )


async def _matrix(cmd):
    args = cmd.args.split()
    if not args or not args[0].isdigit():
        raise ValueError(matrix.USAGE)
    await matrix.run(cmd.chat, cmd.owner, int(args[0]), args[1:])


async def _upscale(cmd):
    job = rt.JOBS.owned(_job_id(cmd, "Usage: /upscale <job ID>"), cmd.owner)
    await derived.request_upscale(job)


# --- characters ---------------------------------------------------------------

async def _character(cmd):
    words = _words(cmd, 2)
    if not words:
        raise ValueError(
            "Usage: /character name (in reply to a photo = "
            "reference), or /character <job ID> <name> "
            "to assign it to a draft (off to remove it)."
        )
    first = words[0].lower()
    if first.isdigit() and len(words) > 1:
        return await _assign_character(cmd, int(first), words[1].lower())
    name = first
    if not name.replace("_", "").isalnum():
        raise ValueError("Name: letters, digits and underscores.")
    characters.ensure_char(cmd.owner, name)
    if not cmd.reply:
        return await _reply(
            cmd,
            f"Character {name} saved. References: "
            f"{len(characters.char_refs(cmd.owner, name))} photos. "
            "Fixed caption: /caption name text",
        )
    path = await rt.TG.download(cmd.reply.message)
    added = characters.attach_char_ref(cmd.owner, name, path)
    extra = "" if added else " (photo already there)"
    await _reply(
        cmd,
        f"Character {name} saved.{extra} It now has "
        f"{len(characters.char_refs(cmd.owner, name))} reference photos: "
        "they are attached automatically to its jobs.",
    )


async def _assign_character(cmd, job_id, who):
    """/character <job ID> <name|off>."""
    job = rt.JOBS.owned(job_id, cmd.owner)
    if who != "off" and not characters.char_row(cmd.owner, who):
        raise ValueError(f"No character called {who} (see /characters).")
    if not rt.JOBS.edit_draft(
        job["id"], character=None if who == "off" else who
    ):
        raise ValueError("A character can only be assigned to a draft.")
    if who == "off":
        await _reply(cmd, f"Character removed from job #{job['id']}.")
    await jobs.summary(job["id"])


async def _characters(cmd):
    rows = characters.char_list(cmd.owner)
    if not rows:
        return await _reply(
            cmd,
            "No characters. Create one: /character name (reply to "
            "a photo of them for the reference).",
        )
    lines = []
    for r in rows:
        n = len(characters.char_refs(cmd.owner, r["name"]))
        cap = (r["caption"] or "").strip()
        suffix = f" — {cap[:60]}" if cap else ""
        lines.append(f"👤 {r['name']} ({n} photos){suffix}")
    lines.append("")
    lines.append("For a job: /character <ID> <name>")
    await _reply(cmd, "\n".join(lines))


def _existing_character(cmd, words):
    if not words:
        raise ValueError(f"Usage: {cmd.name} name")
    name = words[0].lower()
    if not characters.char_row(cmd.owner, name):
        raise ValueError("No such character.")
    return name


async def _caption(cmd):
    words = _words(cmd, 2)
    name = _existing_character(cmd, words)
    if len(words) < 2:
        raise ValueError("Usage: /caption name text")
    characters.set_caption(cmd.owner, name, words[1])
    await _reply(cmd, "Caption saved.")


async def _reference(cmd):
    name = _existing_character(cmd, _words(cmd, 1))
    paths = characters.character_ref_paths(cmd.owner, name)
    if not paths:
        raise ValueError(
            f"{name} has no reference photos: reply to a photo of them "
            f"with /character {name}."
        )
    for i, path in enumerate(paths, start=1):
        await rt.TG.file(
            cmd.chat, path, f"Reference {i}/{len(paths)}: {name}"
        )


async def _use(cmd):
    raise ValueError(
        "Drafts start without a character: assign one to a draft "
        "with /character <ID> <name>."
    )


COMMANDS = {
    "/start": _help,
    "/help": _help,
    "/workflows": _workflows,
    "/night": _night,
    "/pause": _pause,
    "/resume": _resume,
    "/queue": _queue,
    "/status": _status,
    "/submit": _submit,
    "/cancel": _cancel,
    "/result": _result,
    "/retry": _retry,
    "/prompt": _prompt,
    "/light": _light,
    "/matrix": _matrix,
    "/upscale": _upscale,
    "/character": _character,
    "/characters": _characters,
    "/caption": _caption,
    "/reference": _reference,
    "/use": _use,
}


HELP = """Comfy Night Bot — /help shows this list

DRAFTS — anything you send creates a draft, without a character:
 text = prompt · photo with a caption = photo + prompt
 album = a single draft with all the photos
 Pinterest link or image URL = I download the photos, the rest is the
   prompt
 reply to a photo = that photo + your text (each reply is a new draft)
 .txt/.md file = its content is the prompt
/create [component] text — a draft with the workflow already chosen (as
   text, a caption, a reply to a photo or a file's caption)
/workflows — available components

CHANGING A DRAFT
/prompt ID text — changes the prompt
/add ID · /add X..Y — adds a photo to a draft or to drafts X to Y: as
   the photo's caption, in reply to a photo, or as text (uses the last
   photo sent). The same photo goes in only once.
/character ID name — assigns a character (off removes it)
/light ID preset — the draft's light (default = your default light)

STARTING AND THE QUEUE
/submit ID — like the Start button: picks the workflow and queues it
/status ID — summary and any error
/queue — jobs not finished yet
/cancel ID · /cancel X..Y — cancels a draft or a queued job, or all of
   those from X to Y at once
/retry ID — tries failed/unknown/skipped again
/result ID — sends the results again without generating
/night [on|off] — night window · /pause · /resume

RESULTS
/upscale ID — enlarges into the UPSCALE box (proportions kept)
   Upscale starts right away, even by day or when paused.
/matrix ID light=a,b size=WxH,WxH [seed=N] [now] — variants with the
   same prompt, images and seed that change only light and/or
   resolution (12 at most)
/light — lighting presets; /light preset = default light

DESCRIBING AN IMAGE
/describe — reply to a photo, send it with the caption /describe, or
   /describe <link>: I write a prompt and offer to make a draft of it.
   You can add directions after the command. The described photo does
   not go into the draft: then add the character's photo with /add ID.
/describeimg — like /describe, with a prompt that uses <image1>

BATCH JOBS
/list — a batch from JSON (inline, a .json file or in reply to one),
   with a shared image; /list alone shows the schema. Without an image
   it creates the drafts: then /add X..Y with a photo and ▶️ Start all.

CHARACTERS
/character name — creates the character; in reply to a photo it adds
   it to the references (attached automatically to its jobs)
/characters — list with photos and caption
/caption name text — fixed caption put at the start of the prompt
/reference name — shows the reference photos
"""
