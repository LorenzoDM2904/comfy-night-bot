"""Bot startup: configuration, database, services and the three loops."""

import asyncio
import fcntl
import logging

from . import bot, config, db, plugins, runtime as rt, telegram, worker
from .engine import LocalEngine
from .store import JobStore

log = logging.getLogger("nightbot")

LOCK = None  # lock file, kept open for the whole life of the process


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def prepare_data_dir():
    config.DATA.mkdir(parents=True, exist_ok=True)
    (config.DATA / "inputs").mkdir(exist_ok=True)
    (config.DATA / "outputs").mkdir(exist_ok=True)


def acquire_lock():
    """Prevents two instances running on the same database. The returned
    file must stay open for the whole life of the process."""
    lock = (config.DATA / "app.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another instance of the bot is already running.")
    return lock


def init_database():
    rt.DB = db.open_database(config.DATA / "app.sqlite3")
    rt.JOBS = JobStore(rt.DB)
    db.ensure_defaults(config.NIGHT_MODE_DEFAULT)


def init_services():
    rt.TG = telegram.Telegram()
    rt.ENGINE = LocalEngine(config.COMFYUI_BASE_URL)


async def main():
    log.info(
        "Starting: timezone=%s, window=%s–%s, backend=local",
        config.TZ.key, config.START, config.FINISH,
    )
    try:
        await asyncio.gather(
            bot.telegram_loop(), worker.worker_loop(), worker.watchdog_loop()
        )
    finally:
        await rt.ENGINE.close()
        await rt.TG.http.aclose()
        rt.DB.close()


def run():
    global LOCK
    setup_logging()
    config.validate()
    plugins.load_all()
    prepare_data_dir()
    LOCK = acquire_lock()
    init_database()
    rt.JOBS.release_interrupted()
    rt.JOBS.recover_interrupted_deliveries()
    init_services()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped. Remote jobs are not interrupted.")
