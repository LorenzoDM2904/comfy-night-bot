"""Database access: schema, migrations, settings.

There is a single schema, shared by the bot and the characters.py CLI.
The job tables (jobs, inputs) belong to the Job store (store.py). Plugins
add their own tables with bot.schema (see plugins.py).
"""

import sqlite3

from . import plugins
from . import runtime as rt

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT UNIQUE NOT NULL,
    owner INTEGER NOT NULL,
    chat INTEGER NOT NULL,
    component TEXT NOT NULL,
    character TEXT,
    prompt TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft',
    token TEXT NOT NULL,
    snapshot TEXT,
    prompt_id TEXT,
    engine TEXT NOT NULL DEFAULT 'local',
    created REAL NOT NULL,
    started REAL,
    outputs TEXT NOT NULL DEFAULT '[]',
    delivery TEXT NOT NULL DEFAULT 'pending',
    error TEXT
);
CREATE TABLE IF NOT EXISTS inputs(
    job_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY(job_id, message_id)
);
CREATE TABLE IF NOT EXISTS characters(
    owner INTEGER NOT NULL,
    name TEXT NOT NULL,
    reference TEXT,
    caption TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(owner, name)
);
CREATE TABLE IF NOT EXISTS character_refs(
    owner INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    slot INTEGER NOT NULL,
    PRIMARY KEY(owner, name, path)
);
CREATE TABLE IF NOT EXISTS descriptions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner INTEGER NOT NULL,
    chat INTEGER NOT NULL,
    text TEXT NOT NULL,
    image_path TEXT,
    created REAL NOT NULL
);
"""


def open_database(path):
    """Opens the database, creates missing tables and applies the
    migrations. Safe to call while the bot is running (CLI)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(SCHEMA)
    conn.commit()
    backfill_character_refs(conn)
    migrate(conn)
    plugins.apply_schemas(conn)
    return conn


def backfill_character_refs(conn):
    """Compatibility with the old single `reference` field."""
    for row in conn.execute(
        "SELECT owner, name, reference FROM characters "
        "WHERE reference IS NOT NULL"
    ).fetchall():
        if conn.execute(
            "SELECT 1 FROM character_refs WHERE owner=? AND name=? LIMIT 1",
            (row["owner"], row["name"]),
        ).fetchone() is None:
            conn.execute(
                "INSERT INTO character_refs(owner, name, path, slot) "
                "VALUES(?,?,?,0)",
                (row["owner"], row["name"], row["reference"]),
            )
    conn.commit()


def _v1_skipped_state(db):
    # Uncertain outcomes skipped by the worker ended up 'cancelled' and
    # could not be retried: they become 'skipped'. (The text matched is
    # the error written by earlier versions.)
    db.execute(
        "UPDATE jobs SET state='skipped' WHERE state='cancelled' "
        "AND error LIKE '%saltato automaticamente%'"
    )


def add_columns(db, table, columns):
    """Adds the missing columns [(name, definition)] (plugin tables
    too)."""
    present = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in present:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _v2_derived_jobs(db):
    # Derived jobs (for example upscale) in the same queue as ordinary ones.
    add_columns(db, "jobs", (
        ("kind", "kind TEXT NOT NULL DEFAULT 'generate'"),
        ("parent_job_id", "parent_job_id INTEGER"),
        ("run_now", "run_now INTEGER NOT NULL DEFAULT 0"),
    ))


def _v3_light_and_matrix(db):
    # Light per job, overrides for /matrix variants, the photo analysis
    # kept apart from the prompt in descriptions.
    add_columns(db, "jobs", (
        ("light", "light TEXT"),
        ("overrides", "overrides TEXT"),
    ))
    add_columns(db, "descriptions", (("analysis", "analysis TEXT"),))


def _v4_restart_jobs(db):
    # New snapshot format (snapshot.py): jobs start over from scratch.
    # Characters, settings and descriptions stay; the files in data/ stay
    # on disk. Job numbering restarts from #1.
    db.execute("DELETE FROM inputs")
    db.execute("DELETE FROM jobs")
    db.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")


def _v5_chains(db):
    # Plugin chains: jobs that wait for other jobs (depends_on, a JSON
    # list of ids) and the part of the parent draft they belong to.
    add_columns(db, "jobs", (
        ("depends_on", "depends_on TEXT"),
        ("segment", "segment INTEGER"),
    ))


def _moved_to_plugin(db):
    # A migration of a plugin's tables, which the plugin now creates and
    # updates itself (bot.schema): only the version number is left here.
    pass


def _v9_character_loras(db):
    # Character LoRAs, one per base model (JSON {family: {name,
    # strength, trigger}}, see characters.char_loras): components that
    # declare `lora_family` load it on their own.
    add_columns(db, "characters", (("loras", "loras TEXT"),))


# One migration per version, in order: the n-th one brings user_version to n.
MIGRATIONS = (
    _v1_skipped_state,
    _v2_derived_jobs,
    _v3_light_and_matrix,
    _v4_restart_jobs,
    _v5_chains,
    _moved_to_plugin,
    _moved_to_plugin,
    _moved_to_plugin,
    _v9_character_loras,
    _moved_to_plugin,
)


def migrate(db):
    """Numbered migrations through PRAGMA user_version."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(MIGRATIONS, start=1):
        if version < number:
            step(db)
            db.execute(f"PRAGMA user_version={number}")
    db.commit()


def ensure_defaults(night_default):
    """Initial settings values, only when missing."""
    if setting("night") is None:
        put_setting("night", "1" if night_default else "0")
    if setting("paused") is None:
        put_setting("paused", "0")


def setting(key, default=None):
    row = rt.DB.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row else default


def put_setting(key, value):
    rt.DB.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    rt.DB.commit()
