#!/usr/bin/env python3
"""Character management for Comfy Night Bot.

Characters live in the bot's database (DATA_DIR/app.sqlite3, like the
bot); reference photos are copied to DATA_DIR/charrefs/<name>/.

Examples:
  python characters.py list
  python characters.py add alice --caption "short dark hair, freckles" \
      photo1.jpg photo2.jpg photo3.jpg
  python characters.py refs alice another.jpg
  python characters.py info alice
  python characters.py export-dataset alice --out training/datasets/alice
  python characters.py lora alice flux alice_flux.safetensors \
      --strength 0.9 --trigger alice_woman
  python characters.py lora alice flux --off
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

from comfybot import characters as chars
from comfybot import config, db

# Same DATA_DIR and same schema as the bot (comfybot.db).
DATA = config.DATA
DB_PATH = DATA / "app.sqlite3"
CHARREFS = DATA / "charrefs"


def dotenv_owner():
    """The first allowed ID in the .env, or 0."""
    ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    first = ids[0].strip()
    return int(first) if first.isdigit() else 0


def connect():
    """A connection with the bot's schema and migrations: the CLI works
    even before the bot has ever run."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return db.open_database(DB_PATH)


def ensure_char(conn, owner, name, caption=""):
    chars.ensure_char(owner, name, caption, conn=conn)


def add_photos(conn, owner, name, photos):
    added = 0
    dest = CHARREFS / name
    dest.mkdir(parents=True, exist_ok=True)
    for photo in photos:
        src = Path(photo)
        if not src.is_file():
            print(f"  ! missing {photo}", file=sys.stderr)
            continue
        target = dest / src.name
        if not target.exists():
            shutil.copy2(src, target)
        if not chars.attach_char_ref(owner, name, target, conn=conn):
            print(f"  = already there: {target.name}")
            continue
        added += 1
        print(f"  + {target.name}")
    return added


def cmd_list(conn, owner):
    rows = chars.char_list(owner, conn=conn)
    if not rows:
        print("No characters.")
        return
    for r in rows:
        nrefs = len(chars.char_refs(owner, r["name"], conn=conn))
        cap = (r["caption"] or "").strip()
        suffix = f" — {cap[:80]}" if cap else ""
        print(f"👤 {r['name']} ({nrefs} photos){suffix}")


def cmd_info(conn, owner, name):
    row = chars.char_row(owner, name, conn=conn)
    if not row:
        print(f"No character called {name}.")
        return
    print(f"👤 {row['name']}")
    print(f"Caption: {row['caption'] or '(none)'}")
    for i, path in enumerate(chars.char_refs(owner, name, conn=conn), 1):
        p = Path(path)
        print(f"  [{i}] {p} {'✓' if p.is_file() else '✗ missing'}")
    for family, lora in chars.char_loras(owner, name, conn=conn).items():
        trigger = f", trigger «{lora['trigger']}»" if lora["trigger"] else ""
        print(f"  LoRA {family}: {lora['name']} at {lora['strength']:g}{trigger}")


def cmd_lora(conn, owner, args):
    """The character's LoRA for a base model (e.g. flux). The file goes
    in ComfyUI's loras folder; only its name is written here."""
    if not args.off and not args.file:
        raise SystemExit("Give the LoRA file or --off.")
    if not chars.set_lora(owner, args.name, args.family,
                          None if args.off else args.file,
                          args.strength, args.trigger, conn=conn):
        raise SystemExit(f"No character called {args.name}.")
    if args.off:
        print(f"{args.name}: LoRA {args.family} removed.")
    else:
        print(f"{args.name}: LoRA {args.family} = {args.file} "
              f"at {args.strength:g}.")


def cmd_export_dataset(conn, owner, name, out_dir):
    """Prepares a dataset folder for LoRA training (one caption per image)."""
    row = chars.char_row(owner, name, conn=conn)
    if not row:
        raise SystemExit(f"No character called {name}.")
    refs = chars.char_refs(owner, name, conn=conn)
    if not refs:
        raise SystemExit(f"Character {name} has no reference photos.")
    out = Path(out_dir)
    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)
    caption = (row["caption"] or "").strip().replace("\n", " ")
    for i, path in enumerate(refs, 1):
        src = Path(path)
        if not src.is_file():
            print(f"  ! missing {src}", file=sys.stderr)
            continue
        dst = images / f"{name}_{i:03d}{src.suffix.lower()}"
        shutil.copy2(src, dst)
        # Caption = trigger word + the character's fixed caption.
        (images / f"{dst.stem}.txt").write_text(
            f"{name} {caption}".strip()
        )
        print(f"  + {dst.name} -> {dst.stem}.txt")
    print(f"\nDataset ready in {out}/")
    print("Use it with the LoRA trainer of your choice.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", type=int, default=None,
                    help="the owner's Telegram ID "
                         "(default: the first ID in .env)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("--caption", default="")
    p_add.add_argument("photos", nargs="+")

    p_refs = sub.add_parser("refs")
    p_refs.add_argument("name")
    p_refs.add_argument("photos", nargs="+")

    p_info = sub.add_parser("info")
    p_info.add_argument("name")

    p_exp = sub.add_parser("export-dataset")
    p_exp.add_argument("name")
    p_exp.add_argument("--out", default=None)

    p_lora = sub.add_parser("lora")
    p_lora.add_argument("name")
    p_lora.add_argument("family", help="base model, e.g. flux")
    p_lora.add_argument("file", nargs="?",
                        help="file name in ComfyUI's loras folder")
    p_lora.add_argument("--strength", type=float, default=0.9)
    p_lora.add_argument("--trigger", default="",
                        help="trigger word put at the start of the prompt")
    p_lora.add_argument("--off", action="store_true",
                        help="removes the LoRA for this base model")

    args = ap.parse_args()
    owner = args.owner if args.owner is not None else dotenv_owner()
    conn = connect()

    if args.cmd == "list":
        cmd_list(conn, owner)
    elif args.cmd == "add":
        ensure_char(conn, owner, args.name, args.caption)
        n = add_photos(conn, owner, args.name, args.photos)
        print(f"\n{args.name}: {n} photos added.")
    elif args.cmd == "refs":
        ensure_char(conn, owner, args.name)
        n = add_photos(conn, owner, args.name, args.photos)
        print(f"\n{args.name}: {n} photos added.")
    elif args.cmd == "info":
        cmd_info(conn, owner, args.name)
    elif args.cmd == "lora":
        cmd_lora(conn, owner, args)
    elif args.cmd == "export-dataset":
        name = args.name
        out = args.out or str(DATA / "datasets" / name)
        cmd_export_dataset(conn, owner, name, out)
    conn.close()


if __name__ == "__main__":
    main()
