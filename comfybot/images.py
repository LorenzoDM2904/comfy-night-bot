"""Getting images into data/inputs.

Three sources: the file of a Telegram message (a photo or a document), a
link (a Pinterest page or an image URL) and the last photo sent in the
chat. Whoever receives an image names the source and calls acquire().
"""

import json
from dataclasses import dataclass
from pathlib import Path

from . import config, db, linkfetch, runtime as rt


@dataclass(frozen=True)
class TelegramFile:
    """The message that carries a photo or a document."""
    message: dict

    @property
    def message_id(self):
        return self.message["message_id"]


@dataclass(frozen=True)
class Link:
    url: str


@dataclass(frozen=True)
class LastPhoto:
    """The last photo sent in this chat (for /add as plain text)."""
    chat: int


def image_ext(content):
    """Extension from the magic bytes (PNG, JPEG, WebP), None if it is
    not an accepted image."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


def remember_photo(chat, message_id, path):
    """Remembers the chat's last photo, for /add X..Y as plain text."""
    db.put_setting(
        f"last_photo:{chat}",
        json.dumps({"message_id": message_id, "path": str(path)}),
    )


def _last_photo(chat):
    raw = db.setting(f"last_photo:{chat}")
    data = json.loads(raw) if raw else {}
    if not data or not Path(data["path"]).is_file():
        raise ValueError(
            "No recent photo: send the photo with the caption "
            "/add X..Y, or reply to a photo with /add X..Y."
        )
    return data


def input_key(source):
    """The image's key among a draft's inputs: the photo's message. This
    way the same photo enters a draft only once, whichever form of /add
    is used."""
    if isinstance(source, TelegramFile):
        return source.message_id
    if isinstance(source, LastPhoto):
        return _last_photo(source.chat)["message_id"]
    raise ValueError("Invalid image source for a draft.")


async def acquire(source, name=""):
    """Local path of the image. `name`: the file name (without
    extension) for images downloaded from a link."""
    if isinstance(source, TelegramFile):
        return await rt.TG.download(source.message)
    if isinstance(source, LastPhoto):
        return _last_photo(source.chat)["path"]
    url = source.url
    if linkfetch.is_pinterest_url(url):
        url = await linkfetch.extract_image_url(url)
    content = await linkfetch.download_image(url, config.MAX_BYTES)
    ext = image_ext(content)
    if not ext:
        raise ValueError("The link does not point to a PNG/JPEG/WebP image.")
    return _save(content, config.DATA / "inputs" / f"{name}{ext}")


def _save(content, path):
    """Atomic write: a half-written file is never read."""
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_bytes(content)
    temp.replace(path)
    return str(path)
