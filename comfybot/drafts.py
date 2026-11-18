"""Drafts: creation (text, photo, album, link, text file), /add and
/describe. They receive requests already parsed by inbound."""

import asyncio
import logging
import time
from pathlib import Path

from . import batch, buttons, config, describer, images, jobs, plugins
from . import runtime as rt

log = logging.getLogger("nightbot")


# De-duplicates albums whose /describe caption repeats on several photos.
DESCRIBED_ALBUMS = {}


ALBUM_TASKS = {}


async def describe_flow(chat, owner, image_path, reply_to=None, note="",
                        kind="describe", directive=""):
    """Describes the image and answers with a button to create a draft.

    The described photo is only used to write the prompt and is NOT put
    in the draft: the user then adds their character's photo with
    /add ID. kind (describe, describeimg or a plugin's kind, see
    plugins.DescribeKind) changes the model, the system prompt and the
    answer's header.

    The description goes into the descriptions table (expires after
    24h): the callback carries only the id, because a button cannot
    carry the text (callback_data is limited to 64 bytes).
    """
    await rt.TG.call(
        "sendChatAction", {"chat_id": chat, "action": "typing"}
    )
    extra = plugins.DESCRIBE.get(kind)
    if extra:
        model, header = extra.model, extra.header
    elif kind == "describeimg":
        model = config.DESCRIBE_MODEL
        header = "🎭 Prompt with <image1>:\n\n"
    else:
        model = config.DESCRIBE_MODEL
        header = "📝 Description:\n\n"
    hint = (
        "The photo used for the description does NOT go into the draft: "
        "once it is created, send the photo of the character to use with "
        "/add <ID>. You can still change the prompt with /prompt; pick "
        "the light with /light."
    )
    # The photo analysis and the shot design are two separate calls
    # (see describer): the model does not decide the light.
    analysis, text = await describer.describe(
        image_path, kind, model, config.LLAMA_URL, directive=directive,
    )
    if extra and extra.show_analysis:
        header = f"🔎 Analysis:\n\n{analysis}\n\n{header}"
    rt.DB.execute(
        "DELETE FROM descriptions WHERE created < ?",
        (time.time() - 24 * 3600,),
    )
    cur = rt.DB.execute(
        "INSERT INTO descriptions(owner,chat,text,image_path,created,"
        "analysis) VALUES(?,?,?,?,?,?)",
        (owner, chat, text, image_path, time.time(), analysis),
    )
    rt.DB.commit()
    did = cur.lastrowid
    keyboard = [[buttons.describe(did)]]
    await rt.TG.text(
        chat,
        f"{header}{text}\n\n{note}{hint} Available for 24 hours.",
        keyboard,
        reply_to=reply_to,
    )


async def draft_from_description(did, owner):
    """The button's callback: creates a draft with the prompt ONLY.

    The described photo is never put in the draft: the user adds their
    character's photo with /add ID.
    """
    row = rt.DB.execute(
        "SELECT * FROM descriptions WHERE id=?", (did,)
    ).fetchone()
    if not row or row["owner"] != owner:
        raise ValueError("Description not found or not yours.")
    if time.time() - row["created"] > 24 * 3600:
        raise ValueError("Description expired: run the command again.")

    # Each tap on the button creates a new draft with the same prompt.
    job = rt.JOBS.create_draft(
        owner, row["chat"], row["text"], origin=f"{owner}:descr:{did}",
    )
    await jobs.summary(job["id"])
    try:
        await rt.TG.text(
            job["chat"],
            "💡 The described photo was not added: now send the photo "
            f"of the character to use with /add {job['id']}.",
        )
    except Exception:
        log.exception("/add hint failed")


async def delayed_summary(job_id):
    try:
        await asyncio.sleep(2)
        await jobs.summary(job_id)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Album summary failed")


async def create(req):
    """A new draft from a CreateDraft request: text, photo (from an album
    or replied to, too), links or a text file."""
    chat, owner = req.chat, req.owner
    prompt, paths, errors = req.prompt, [], []

    if req.text_file:
        path = await rt.TG.download(req.text_file.message, text=True)
        prompt = Path(path).read_text(errors="replace").strip()
        if not prompt:
            raise ValueError("The text file is empty.")
    elif req.links:
        paths, errors = await _download_links(req)
    path = None
    if req.image and not req.album:
        # Before the draft: a failed download leaves no empty draft behind.
        path = await images.acquire(req.image)

    if req.album:
        # An album's photos arrive as separate messages: same draft.
        job = rt.JOBS.album_draft(owner, chat, req.album, prompt, req.component)
    else:
        job = rt.JOBS.create_draft(
            owner, chat, prompt, req.component, origin=req.origin,
        )

    for i, link_path in enumerate(paths):
        rt.JOBS.attach_input(job["id"], -(i + 1), link_path)
    if errors:
        more = "..." if len(errors) > 5 else ""
        await rt.TG.text(
            chat,
            "⚠️ Some links were not downloaded:\n"
            + "\n".join(errors[:5]) + more,
        )

    if not req.image:
        return await jobs.summary(job["id"])

    key = req.image.message_id
    if not rt.JOBS.has_input(job["id"], key):
        path = path or await images.acquire(req.image)
        if not rt.JOBS.attach_input(job["id"], key, path):
            raise ValueError("Job already confirmed: make a new request.")
        # For /add X..Y as plain text, without sending the photo again.
        images.remember_photo(chat, key, path)
    if not req.album:
        return await jobs.summary(job["id"])

    if prompt:
        # An album's caption can be on any of its photos.
        rt.JOBS.edit_draft(job["id"], prompt=prompt, component=req.component)
    # A single summary, once the album stops arriving.
    previous = ALBUM_TASKS.get(job["id"])
    if previous:
        previous.cancel()
    ALBUM_TASKS[job["id"]] = asyncio.create_task(delayed_summary(job["id"]))


async def _download_links(req):
    """Downloads the links' images: the same result as sending the
    photos. Returns (paths, errors); no paths at all is an error."""
    await rt.TG.call(
        "sendChatAction", {"chat_id": req.chat, "action": "upload_photo"}
    )
    mid = req.origin.rsplit(":", 1)[-1]
    paths, errors = [], []
    for i, url in enumerate(req.links):
        try:
            paths.append(await images.acquire(
                images.Link(url), f"{req.chat}_link_{mid}_{i}"
            ))
        except Exception as exc:
            errors.append(f"{url}: {str(exc)[:200]}")
            log.warning("Link not downloaded %s: %s", url, exc)
    if not paths:
        raise ValueError("No link downloaded:\n" + "\n".join(errors))
    return paths, errors


async def add_image(req):
    """/add: the same image to one draft or a range of drafts. A photo
    enters a draft only once, whichever form is used."""
    key = images.input_key(req.image)
    if req.first == req.last:
        target = rt.JOBS.owned(req.first, req.owner)
        # An early check only to avoid downloading the photo for nothing:
        # attach_input decides.
        if target["state"] != "draft":
            raise ValueError("You can only add to a draft not yet confirmed.")
        if not rt.JOBS.has_input(target["id"], key):
            path = await images.acquire(req.image)
            if not rt.JOBS.attach_input(target["id"], key, path):
                raise ValueError(
                    "You can only add to a draft not yet confirmed."
                )
        return await jobs.summary(target["id"])
    path = await images.acquire(req.image)
    added = batch.add_image_to_jobs(
        req.owner, req.first, req.last, path, key,
    )
    await batch.confirm_added(req.chat, added, req.first, req.last)


async def describe(req):
    """/describe on a photo (attached or replied to) or a link."""
    note = ""
    if req.album:
        last = DESCRIBED_ALBUMS.get(req.album)
        if last and time.time() - last < 60:
            return  # the same caption repeated on several photos
        DESCRIBED_ALBUMS[req.album] = time.time()
        note = "Describing the album's first photo.\n\n"
    if isinstance(req.image, images.Link):
        await rt.TG.call(
            "sendChatAction", {"chat_id": req.chat, "action": "upload_photo"}
        )
        try:
            path = await images.acquire(
                req.image, f"{req.chat}_descr_{req.reply_to}"
            )
        except Exception as exc:
            raise ValueError(f"Link not downloaded: {str(exc)[:300]}")
    else:
        path = await images.acquire(req.image)
    await describe_flow(
        req.chat, req.owner, path, reply_to=req.reply_to, note=note,
        kind=req.kind, directive=req.directive,
    )
