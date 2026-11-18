"""Characters and reference photos.

The same functions serve the bot (shared connection rt.DB) and the
characters.py CLI, which passes its own connection with `conn=`.
"""

import json
from pathlib import Path

from . import runtime as rt


def _db(conn):
    return conn if conn is not None else rt.DB


def char_row(owner, name, conn=None):
    return _db(conn).execute(
        "SELECT * FROM characters WHERE owner=? AND name=?",
        (owner, name),
    ).fetchone()


def require(owner, name):
    """An error for the user if the character does not exist, listing
    the ones they have."""
    if char_row(owner, name):
        return
    known = [r["name"] for r in char_list(owner)]
    hint = (
        "Available characters: " + ", ".join(known) + "."
        if known else "Create a character first with /character."
    )
    raise ValueError(f"No character called «{name}». {hint}")


def char_list(owner, conn=None):
    """The user's characters (name and caption), by name."""
    return _db(conn).execute(
        "SELECT name, caption FROM characters WHERE owner=? ORDER BY name",
        (owner,),
    ).fetchall()


def char_refs(owner, name, conn=None):
    """A character's (canonical) reference photos, in order."""
    rows = _db(conn).execute(
        "SELECT path FROM character_refs WHERE owner=? AND name=? "
        "ORDER BY slot, path",
        (owner, name),
    )
    return [r["path"] for r in rows]


def ensure_char(owner, name, caption="", conn=None):
    """Creates the character if missing. caption is NOT NULL: a string
    is always inserted; on an existing character it changes only when a
    new one arrives."""
    db = _db(conn)
    db.execute(
        "INSERT INTO characters(owner,name,caption) VALUES(?,?,?) "
        "ON CONFLICT(owner,name) DO UPDATE SET "
        "caption=CASE WHEN excluded.caption='' THEN characters.caption "
        "ELSE excluded.caption END",
        (owner, name, (caption or "").strip()),
    )
    db.commit()


def set_caption(owner, name, caption, conn=None):
    """The character's fixed caption. False if it does not exist."""
    db = _db(conn)
    cur = db.execute(
        "UPDATE characters SET caption=? WHERE owner=? AND name=?",
        ((caption or "").strip(), owner, name),
    )
    db.commit()
    return cur.rowcount == 1


def attach_char_ref(owner, name, path, conn=None):
    """Adds a reference photo to a character.
    Returns False if it was already there."""
    db = _db(conn)
    path = str(path)
    exists = db.execute(
        "SELECT 1 FROM character_refs WHERE owner=? AND name=? AND path=?",
        (owner, name, path),
    ).fetchone()
    if exists:
        return False
    slot = db.execute(
        "SELECT COALESCE(MAX(slot), -1) + 1 AS n FROM character_refs "
        "WHERE owner=? AND name=?",
        (owner, name),
    ).fetchone()["n"]
    db.execute(
        "INSERT INTO character_refs(owner, name, path, slot) "
        "VALUES(?,?,?,?)",
        (owner, name, path, slot),
    )
    db.execute(
        "UPDATE characters SET reference=COALESCE(reference, ?) "
        "WHERE owner=? AND name=?",
        (path, owner, name),
    )
    db.commit()
    return True


def character_ref_paths(owner, name, conn=None):
    """The character's references still on disk."""
    if not name:
        return []
    return [p for p in char_refs(owner, name, conn) if Path(p).is_file()]


def char_loras(owner, name, conn=None):
    """The character's LoRAs by base model: {family: {name, strength,
    trigger}}. Empty if it has none (or the character does not exist)."""
    if not name:
        return {}
    row = _db(conn).execute(
        "SELECT loras FROM characters WHERE owner=? AND name=?",
        (owner, name),
    ).fetchone()
    return json.loads(row["loras"] or "{}") if row else {}


def set_lora(owner, name, family, lora_name=None, strength=0.9,
             trigger="", conn=None):
    """Sets (or removes, with lora_name None) the character's LoRA for a
    base model (`family`, e.g. "flux"). False if it does not exist."""
    db = _db(conn)
    if not char_row(owner, name, conn):
        return False
    loras = char_loras(owner, name, conn)
    if lora_name:
        loras[family] = {
            "name": lora_name,
            "strength": float(strength),
            "trigger": (trigger or "").strip(),
        }
    else:
        loras.pop(family, None)
    db.execute(
        "UPDATE characters SET loras=? WHERE owner=? AND name=?",
        (json.dumps(loras) if loras else None, owner, name),
    )
    db.commit()
    return True
