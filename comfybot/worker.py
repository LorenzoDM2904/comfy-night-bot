"""The night queue: scheduling, running, monitoring, watchdog."""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from . import (
    comfy_process, config, db, delivery, engine, plugins, runtime as rt,
    videos,
)
from .engine import DefinitiveSubmissionError
from .snapshot import Snapshot

log = logging.getLogger("nightbot")


def can_start():
    if db.setting("paused") == "1":
        return False
    if db.setting("night") != "1":
        return True
    now = datetime.now(config.TZ).time()
    if config.START < config.FINISH:
        return config.START <= now < config.FINISH
    return now >= config.START or now < config.FINISH


async def uncertain(job_id, reason):
    if not rt.JOBS.mark_uncertain(job_id, reason):
        return
    job = rt.JOBS.get(job_id)
    try:
        await rt.TG.text(
            job["chat"],
            f"Job #{job_id}: uncertain outcome.\n{reason}\n"
            "It will not be generated again automatically. "
            "Check ComfyUI before /retry.",
        )
    except Exception:
        log.exception("Uncertain-outcome notice failed")


CANCELLED = {
    engine.INTERRUPTED: "run interrupted in ComfyUI",
    engine.REMOVED: "removed from the ComfyUI queue",
    engine.ABSENT: "no longer in ComfyUI",
}

# Output formats the bot saves and delivers.
OUTPUT_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".mkv")


async def skip_unknown(job):
    """Uncertain outcome: cancel the remote job and skip it, never block."""
    job_id = job["id"]
    remote = "no remote submission"
    try:
        if job["prompt_id"]:
            remote = CANCELLED[await rt.ENGINE.cancel(job["prompt_id"])]
    except Exception:
        log.exception("Remote cancel failed for job %s", job_id)
        remote = "remote cancel FAILED: check ComfyUI"
    # 'skipped', not 'cancelled': the outcome stays uncertain and /retry is allowed.
    if not rt.JOBS.skip(
        job_id,
        (job["error"] or "") + f" | skipped automatically ({remote})",
    ):
        return
    try:
        await rt.TG.text(
            job["chat"],
            f"Job #{job_id}: uncertain outcome, skipped ({remote}). "
            f"The next job can start; /retry {job_id} to "
            "try it again.",
        )
    except Exception:
        log.exception("Skip notice failed")


def next_output_counter():
    """The next number for outputs, all saved in data/outputs. It goes on
    from the highest numbered file there (3.png, 4.mp4...)."""
    out = config.DATA / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    mx = 0
    for p in out.iterdir():
        if p.is_file() and p.stem.isdigit():
            mx = max(mx, int(p.stem))
    return mx + 1


async def prepare_submission(job):
    """Uploads the inputs and compiles the graph to submit. It submits
    nothing: an error here does not start a generation."""
    snap = Snapshot.from_json(job["snapshot"])
    # Uploads before the intent to submit: a crash at this stage does not
    # start a generation.
    uploaded = []
    roles = list(snap.roles) + [""] * len(snap.assets)
    for path, role in zip(snap.assets[:len(snap.images)], roles):
        path = await plugins.prepare_input(resolve_asset(path), role)
        uploaded.append(await rt.ENGINE.upload_input(path))
    graph = snap.compile(uploaded, f"nightbot/{job['id']}_{job['token']}")
    return graph, snap


def resolve_asset(path):
    """A snapshot asset: a path, or "job:<id>" for the image or video
    produced by another job in the same chain (see store.start_chain)."""
    if not str(path).startswith("job:"):
        return path
    source = rt.JOBS.get(int(path[4:]))
    if not source or source["state"] != "done":
        raise RuntimeError(f"Job #{path[4:]} has no result yet.")
    media = (".png", ".jpg", ".jpeg", ".webp", *videos.VIDEO_EXTS)
    images = [
        out for out in json.loads(source["outputs"])
        if Path(out).suffix.lower() in media
    ]
    if not images or not Path(images[0]).is_file():
        raise RuntimeError(f"Result of job #{path[4:]} not found.")
    return images[0]


async def run_local(job):
    """A job the bot runs itself (a plugin's JobKind.run): the plugin
    claims it (rt.JOBS.claim) and closes it (run_done or run_fail).
    `out`: the next free name in data/outputs, without extension."""
    out = config.DATA / "outputs" / str(next_output_counter())
    await plugins.kind(job["kind"]).run(job, out)


def save_attempt(job_id, token, graph, snap):
    """A copy on disk of what is sent to ComfyUI, per attempt: a /retry
    does not erase the record of the previous attempt."""
    try:
        folder = config.DATA / "attempts"
        folder.mkdir(exist_ok=True)
        record = {
            "job_id": job_id,
            "token": token,
            "time": time.time(),
            "assets": snap.assets,
            "roles": snap.roles,
            "positive": snap.positive,
            "workflow": graph,
        }
        path = folder / f"{job_id}_{token}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    except Exception:
        log.exception("Saving attempt %s failed", job_id)


async def run_job(job):
    job_id = job["id"]

    if job["state"] == "submitting":
        prompt_id = await rt.ENGINE.find_submission(job["token"])
        if not prompt_id:
            await uncertain(
                job_id,
                "Submission interrupted: no match in the queue/history.",
            )
            return
        if not rt.JOBS.mark_running(job_id, prompt_id):
            return
        await delivery.started_message(job_id)

    elif job["state"] == "queued":
        # Atomic claim BEFORE the uploads: a /cancel that arrives
        # meanwhile wins and the job does not start.
        if not rt.JOBS.claim(job_id):
            return
        try:
            graph, snap = await prepare_submission(job)
        except Exception:
            rt.JOBS.release(job_id)
            raise
        if not rt.JOBS.begin_submit(job_id):
            log.info("Job %s cancelled while preparing", job_id)
            return
        save_attempt(job_id, job["token"], graph, snap)
        try:
            prompt_id = await rt.ENGINE.submit(graph, job["token"])
        except DefinitiveSubmissionError as exc:
            if rt.JOBS.fail(job_id, str(exc)):
                await rt.TG.text(
                    job["chat"], f"Job #{job_id} rejected by ComfyUI."
                )
            return
        except Exception:
            # A timeout may come AFTER acceptance.
            # The next round tries to find the submission again.
            log.exception("Uncertain submission for job %s", job_id)
            return
        if not rt.JOBS.mark_running(job_id, prompt_id):
            return
        await delivery.started_message(job_id)

    job = rt.JOBS.get(job_id)
    prompt_id = job["prompt_id"]
    missing = 0

    while True:
        try:
            status = await rt.ENGINE.status(prompt_id)
            if isinstance(status, engine.Failed):
                if rt.JOBS.fail(job_id, status.detail):
                    await rt.TG.text(
                        job["chat"], f"Job #{job_id} failed in ComfyUI."
                    )
                return
            if isinstance(status, engine.Done):
                outputs = await save_outputs(job, status)
                if not outputs:
                    rt.JOBS.fail(
                        job_id, "No images in the declared output nodes.",
                    )
                    return
                if rt.JOBS.complete(job_id, outputs):
                    # Before the next job: a plugin can stop its chain if
                    # the result came out wrong.
                    notes = await plugins.after_job(job_id)
                    await delivery.deliver(job_id)
                    for note in notes:
                        await rt.TG.text(job["chat"], note)
                return

            if not isinstance(status, engine.Missing):
                missing = 0
            else:
                missing += 1
                if missing >= 3:
                    await uncertain(
                        job_id,
                        "The submission is no longer in ComfyUI's queue or history.",
                    )
                    return

            if time.time() - (job["started"] or job["created"]) > config.TIMEOUT:
                await uncertain(
                    job_id,
                    "Monitoring timeout. The remote job was NOT "
                    "interrupted and may still be running.",
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Monitoring job %s: trying again", job_id)
            if config.COMFYUI_MANAGE and comfy_process.comfy_pid() and not comfy_process.comfy_running():
                # ComfyUI managed by the bot has exited: the job can no
                # longer finish. Close it now instead of waiting for
                # TIMEOUT, so the worker can restart ComfyUI and go on.
                if not rt.JOBS.fail(
                    job_id,
                    f"ComfyUI exited during the job. "
                    f"Log: {config.COMFYUI_LOG}",
                ):
                    return
                try:
                    await rt.TG.text(
                        job["chat"],
                        f"Job #{job_id} failed: ComfyUI closed "
                        f"while running (crash?). Log: {config.COMFYUI_LOG}\n"
                        "Use /retry to try again.",
                    )
                except Exception:
                    log.exception("ComfyUI crash notice failed")
                return
            if time.time() - (job["started"] or job["created"]) > config.TIMEOUT:
                await uncertain(job_id, "No connection past the timeout.")
                return

        await asyncio.sleep(config.POLL)


async def save_outputs(job, done):
    """Downloads the outputs of the nodes declared in the snapshot into
    data/outputs, with a global running number (3.png, 4.mp4...)."""
    snap = Snapshot.from_json(job["snapshot"])
    folder = config.DATA / "outputs"
    paths = []
    counter = next_output_counter()
    for node in snap.output_nodes:
        for output in done.outputs.get(node, []):
            if output.suffix not in OUTPUT_EXTS:
                raise ValueError("Unsupported output format.")
            while (folder / f"{counter}{output.suffix}").exists():
                counter += 1
            path = folder / f"{counter}{output.suffix}"
            paths.append(await rt.ENGINE.download(output, path))
            counter += 1
    return paths


def comfy_needed():
    """Is ComfyUI needed now? True if there is work that needs it."""
    return rt.JOBS.needs_comfy(can_start())


def next_queued_job():
    """The next job to start, given the night window and the pause
    (run_now jobs start anyway)."""
    return rt.JOBS.next_runnable(can_start())


async def worker_loop():
    while True:
        try:
            if config.COMFYUI_MANAGE:
                await comfy_process.ensure_comfy(comfy_needed())

            # First pick up submissions/jobs already started, even outside the window.
            job = rt.JOBS.active_job()
            if job:
                await run_job(job)
                await asyncio.sleep(config.POLL)
                continue

            pending = rt.JOBS.next_pending_delivery()
            if pending:
                await delivery.deliver(pending["id"])

            # Uncertain outcome: cancel the remote one (if any), skip it
            # and let the next one start: never block the night.
            unknown = rt.JOBS.next_uncertain()
            if unknown:
                await skip_unknown(unknown)
                await asyncio.sleep(config.POLL)
                continue

            # A job the bot runs itself (a plugin's JobKind.run, for
            # example an ffmpeg edit): it needs neither ComfyUI nor the
            # night window.
            local = rt.JOBS.next_run()
            if local:
                await run_local(local)
                continue

            # comfy_needed() counts the same jobs: when managed on demand,
            # ComfyUI was already started at the top of the loop.
            job = next_queued_job()
            if job:
                await rt.ENGINE.health()
                # Do not run our job on top of manual generations.
                if not await rt.ENGINE.is_busy():
                    await run_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Worker: checking again on the next round")
        await asyncio.sleep(config.POLL)


async def watchdog_loop():
    """Every WATCHDOG minutes: ComfyUI health + progress."""
    log.info("Watchdog started (every %s seconds)", config.WATCHDOG)
    while True:
        try:
            ok = True
            try:
                await rt.ENGINE.health()
            except Exception:
                ok = False
            # Managed on demand: ComfyUI off while idle is not a fault.
            if config.COMFYUI_MANAGE and not comfy_needed() and not ok:
                ok = True
            prev = db.setting("watchdog_state", "")
            down = "down" if not ok else "ok"
            if not ok and prev != down:
                db.put_setting("watchdog_state", down)
                for chat in config.ALLOW:
                    try:
                        await rt.TG.text(
                            chat,
                            "⚠️ Watchdog: ComfyUI is not answering. "
                            "Check the computer.",
                        )
                    except Exception:
                        log.exception("Watchdog notice not sent")
            elif ok and prev == "down":
                db.put_setting("watchdog_state", down)
                for chat in config.ALLOW:
                    try:
                        await rt.TG.text(
                            chat,
                            "✅ Watchdog: ComfyUI is reachable again.",
                        )
                    except Exception:
                        log.exception("Watchdog notice not sent")

            job = rt.JOBS.active_job()
            if job:
                started = job["started"] or job["created"]
                minutes = int((time.time() - started) // 60)
                last = float(db.setting(f"heartbeat:{job['id']}", "0") or 0)
                if minutes >= 20 and time.time() - last >= config.WATCHDOG:
                    db.put_setting(
                        f"heartbeat:{job['id']}", str(time.time())
                    )
                    await rt.TG.text(
                        job["chat"],
                        f"🖥️ Still working: #{job['id']} running for "
                        f"{minutes} min. ComfyUI: {down}.",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Watchdog: next round in %s seconds", config.WATCHDOG)
        await asyncio.sleep(config.WATCHDOG)
