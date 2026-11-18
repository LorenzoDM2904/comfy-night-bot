"""A minimal Telegram Bot API client."""

import json
from pathlib import Path

import httpx

from . import config, images, videos


def _split_text(text, limit=4000):
    """Splits a long text into pieces <= limit (Telegram's max is 4096),
    preferring line and word boundaries. Empty -> a single empty piece."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


class Telegram:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=70)

    async def call(self, method, payload=None, files=None):
        url = f"https://api.telegram.org/bot{config.TOKEN}/{method}"
        if files:
            r = await self.http.post(
                url, data=payload or {}, files=files
            )
        else:
            r = await self.http.post(url, json=payload or {})
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram error"))
        return body["result"]

    async def text(self, chat, text, keyboard=None, reply_to=None):
        # Telegram accepts 4096 characters per message at most: a long
        # text is split into several messages (on word boundaries)
        # instead of being cut. Keyboard and reply go on the first one.
        # Returns the first message sent (the one with the keyboard).
        chunks = _split_text(text)
        first = True
        sent = None
        for chunk in chunks:
            data = {"chat_id": chat, "text": chunk}
            if first:
                if keyboard:
                    data["reply_markup"] = {"inline_keyboard": keyboard}
                if reply_to:
                    data["reply_to_message_id"] = reply_to
            result = await self.call("sendMessage", data)
            if first:
                sent = result
            first = False
        return sent

    async def edit_keyboard(self, chat, message_id, keyboard):
        """Replaces the buttons of a message already sent."""
        await self.call("editMessageReplyMarkup", {
            "chat_id": chat, "message_id": message_id,
            "reply_markup": {"inline_keyboard": keyboard or []},
        })

    async def edit_text(self, chat, message_id, text, keyboard=None):
        """Replaces the text (4096 characters at most) and the buttons of
        a message already sent."""
        data = {"chat_id": chat, "message_id": message_id,
                "text": text[:4096]}
        if keyboard:
            data["reply_markup"] = {"inline_keyboard": keyboard}
        await self.call("editMessageText", data)

    async def answer(self, callback_id, text=""):
        """Answers a button tap: a short notice above the chat."""
        data = {"callback_query_id": callback_id}
        if text:
            data["text"] = text[:200]
        await self.call("answerCallbackQuery", data)

    async def file(self, chat, path, caption="", photo=False,
                   video=False, keyboard=None):
        path = Path(path)
        # Media captions are limited to 1024 characters: a longer one is
        # sent as separate messages and the caption is left empty.
        if len(caption) > 1000:
            await self.text(chat, caption)
            caption = ""
        key = "photo" if photo else ("video" if video else "document")
        method = (
            "sendPhoto" if photo else
            "sendVideo" if video else "sendDocument"
        )
        data = {"chat_id": str(chat), "caption": caption[:1000]}
        if video:
            data["supports_streaming"] = True
        if keyboard:
            data["reply_markup"] = json.dumps(
                {"inline_keyboard": keyboard}
            )
        with path.open("rb") as f:
            return await self.call(
                method, data,
                {key: (path.name, f, "application/octet-stream")},
            )

    async def download(self, message, text=False):
        doc = message.get("document")
        if doc:
            if doc.get("file_size", 0) > config.MAX_BYTES:
                raise ValueError("File too large.")
            file_id = doc["file_id"]
        elif message.get("photo"):
            photo = message["photo"][-1]
            if photo.get("file_size", 0) > config.MAX_BYTES:
                raise ValueError("Photo too large.")
            file_id = photo["file_id"]
        else:
            raise ValueError("Send a photo or an image file.")

        content = await self._fetch(file_id, config.MAX_BYTES)

        name = f"{message['chat']['id']}_{message['message_id']}"
        if text:
            # Text file: no magic check, extension from the name.
            doc_name = (doc or {}).get("file_name") or "document.txt"
            ext = Path(doc_name).suffix.lower()
            if ext not in (".txt", ".md", ".text", ".log", ".json"):
                ext = ".txt"
        else:
            ext = images.image_ext(content)
            if not ext:
                raise ValueError("Accepted formats: PNG, JPEG and WebP.")
        return _save(name, ext, content)

    async def download_video(self, message):
        """A message's video (a video or a video document) into
        data/inputs. Limit: VIDEO_MAX_BYTES (the Bot API does not
        download more than 20 MB)."""
        media = message.get("video") or message.get("document") or {}
        if not media.get("file_id"):
            raise ValueError("Send a video.")
        limit = config.VIDEO_MAX_BYTES
        if media.get("file_size", 0) > limit:
            raise ValueError(_too_big_video(limit))
        try:
            content = await self._fetch(media["file_id"], limit)
        except ValueError:
            raise ValueError(_too_big_video(limit))
        except RuntimeError as exc:
            # getFile over 20 MB: "Bad Request: file is too big".
            if "too big" in str(exc).lower():
                raise ValueError(_too_big_video(limit))
            raise
        ext = videos.video_ext(content)
        if not ext:
            raise ValueError("Accepted video formats: MP4, MOV and WebM.")
        name = f"{message['chat']['id']}_{message['message_id']}"
        return _save(name, ext, content)

    async def _fetch(self, file_id, limit):
        """Contents of the Telegram file `file_id`; ValueError past `limit`."""
        info = await self.call("getFile", {"file_id": file_id})
        if info.get("file_size", 0) > limit:
            raise ValueError("File too large.")
        url = f"https://api.telegram.org/file/bot{config.TOKEN}/{info['file_path']}"
        chunks = []
        size = 0
        async with self.http.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ValueError("File too large.")
                chunks.append(chunk)
        return b"".join(chunks)


def _too_big_video(limit):
    return (
        f"Video too large: Telegram lets bots download "
        f"{limit // (1024 * 1024)} MB at most. Trim or compress it."
    )


def _save(name, ext, content):
    """Deterministic name (a repeated update does not create another
    file) and atomic write."""
    path = config.DATA / "inputs" / f"{name}{ext}"
    temp = path.with_suffix(ext + ".part")
    temp.write_bytes(content)
    temp.replace(path)
    return str(path)
