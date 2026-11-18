"""Dispatching Telegram requests, and polling."""

import asyncio
import logging

from . import (
    batch,
    commands,
    config,
    db,
    derived,
    drafts,
    inbound,
    jobs,
    plugins,
    runtime as rt,
)

log = logging.getLogger("nightbot")


async def process_update(update):
    """Reads the update (inbound) and runs the request. A user error goes
    back to the chat as «Not done: …»."""
    try:
        request = inbound.parse(update, config.ALLOW)
        if request is None:
            return
        if isinstance(request, inbound.ButtonPress) and not _answers_itself(
            request
        ):
            await _ack(request)
        await dispatch(request)
    except Exception as exc:
        await rt.TG.text(
            inbound.chat_of(update), f"Not done: {str(exc)[:1500]}"
        )


async def dispatch(request):
    if isinstance(request, inbound.CreateDraft):
        await drafts.create(request)
    elif isinstance(request, inbound.AddImage):
        await drafts.add_image(request)
    elif isinstance(request, inbound.Describe):
        await drafts.describe(request)
    elif isinstance(request, inbound.BatchList):
        await batch.handle_list(request)
    elif isinstance(request, inbound.Command):
        await commands.run(request)
    elif isinstance(request, inbound.ButtonPress):
        await press(request)
    elif isinstance(request, inbound.Refuse):
        await rt.TG.text(request.chat, request.text)


def _answers_itself(request):
    """Plugin buttons that answer the tap themselves, with a notice
    (plugins.SELF_ANSWER): a tap must be answered only once."""
    return request.press is not None \
        and request.press.action in plugins.SELF_ANSWER


async def _ack(request):
    try:
        await rt.TG.call(
            "answerCallbackQuery", {"callback_query_id": request.callback_id}
        )
    except Exception:
        # A failed ack (400 on an expired or already answered id) must
        # not stop the button's action from running.
        log.warning("answerCallbackQuery failed for %r", request.press)


async def press(request):
    """A tap on a button (see buttons)."""
    p, owner = request.press, request.owner
    if p is None:
        raise ValueError("Invalid button.")
    if p.action == "start":
        await jobs.start(p.job_id, owner)
    elif p.action == "pick":
        # The picker only appears after Start: picking starts the job.
        await jobs.start(p.job_id, owner, component=p.component)
    elif p.action == "cancel":
        await jobs.cancel_job(p.job_id, owner)
    elif p.action == "upscale":
        await derived.request_upscale(rt.JOBS.owned(p.job_id, owner))
    elif p.action == "describe":
        await drafts.draft_from_description(p.description, owner)
    elif p.action == "start_all":
        await batch.batch_start(request.chat, owner, p.job_id, p.last_id)
    elif p.action in plugins.BUTTONS:
        await plugins.BUTTONS[p.action](request, p.payload)


async def telegram_loop():
    while True:
        try:
            offset = int(db.setting("telegram_offset", "0"))
            updates = await rt.TG.call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 45,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            for update in updates:
                update_id = update.get("update_id")
                try:
                    await process_update(update)
                except Exception:
                    # An update that cannot be processed must not block the
                    # whole batch: it is skipped and the offset moves on.
                    log.exception(
                        "Update %s not processed: moving on",
                        update_id,
                    )
                if update_id is not None:
                    db.put_setting("telegram_offset", update_id + 1)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram polling: retrying in 5 seconds")
            await asyncio.sleep(5)
