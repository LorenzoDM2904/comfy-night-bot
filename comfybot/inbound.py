"""Telegram input: from an update to a request.

The only place that reads the shape of Telegram messages (photos,
documents, captions, replies, callbacks). parse() returns a typed
request; whoever runs it (bot.dispatch) no longer looks at the message.
The rules of /create and /add live here, once.

User errors (an invalid caption, /add without an ID, ...) come out as
ValueError, like in the rest of the bot.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple, Union

from . import buttons, catalog, plugins, videos
from .images import LastPhoto, Link, TelegramFile
from .linkfetch import URL_RE, extract_urls

DESCRIBE = ("/describe", "/describeimg")
# Commands that inbound turns into requests of their own: they are not
# in commands.COMMANDS and a plugin cannot use them.
RESERVED = ("/create", "/add", "/list", *DESCRIBE)
TEXT_DOC_EXTS = (".txt", ".md", ".text", ".log", ".json")


# --- requests -----------------------------------------------------------------

@dataclass(frozen=True)
class CreateDraft:
    """A new draft. At most one of image, links and text_file."""
    chat: int
    owner: int
    origin: str                     # the draft's origin key
    prompt: str = ""
    component: str = ""
    image: Optional[TelegramFile] = None
    album: Optional[str] = None     # photos of an album: a single draft
    links: Tuple[str, ...] = ()     # Pinterest links or image URLs
    text_file: Optional[TelegramFile] = None  # the prompt is in the file


@dataclass(frozen=True)
class AddImage:
    """Adds an image to draft `first` or to drafts first..last."""
    chat: int
    owner: int
    first: int
    last: int
    image: Union[TelegramFile, LastPhoto]


@dataclass(frozen=True)
class Describe:
    chat: int
    owner: int
    kind: str                       # describe, describeimg or a plugin's
    image: Union[TelegramFile, Link]
    directive: str = ""             # extra directions for the prompt
    reply_to: Optional[int] = None
    album: Optional[str] = None


@dataclass(frozen=True)
class BatchList:
    """/list: the JSON is in `body` or in the file `body_file`."""
    chat: int
    owner: int
    message_id: int
    body: str = ""
    body_file: Optional[TelegramFile] = None
    image: Optional[TelegramFile] = None   # photo replied to: shared


@dataclass(frozen=True)
class Command:
    """Any other command: /queue, /status, /character, ..."""
    chat: int
    owner: int
    name: str                       # with the slash: "/status"
    args: str = ""                  # the text after the command
    # The photo, document or video it replies to, or that carries the
    # command as a caption (videos only for plugin commands).
    reply: Optional[TelegramFile] = None
    message_id: int = field(default=0, compare=False)   # the message


@dataclass(frozen=True)
class ButtonPress:
    chat: int
    owner: int
    callback_id: str
    press: Optional[buttons.Press]  # None if the button is not valid
    message_id: int = 0             # the message that carries the button


@dataclass(frozen=True)
class Refuse:
    """A message outside the private chat."""
    chat: int
    text: str


# --- shared rules -------------------------------------------------------------

def split_command(text):
    """("/name", rest) of a text that starts with a command, also in the
    form /name@bot; ("", text) otherwise."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", text
    head, _, rest = text.partition(" ")
    if "\n" in head:
        head, _, more = head.partition("\n")
        rest = more + (" " + rest if rest else "")
    return head.split("@")[0], rest.strip()


def split_create(rest):
    """The single rule of /create [component] text: the component is the
    first line (if the text has line breaks) or the first word; a word
    that is not a component is part of the prompt. Returns (component,
    prompt)."""
    components = catalog.components()
    first, newline, tail = rest.partition("\n")
    if newline and first.strip() in components:
        return first.strip(), tail.strip()
    words = rest.split(maxsplit=1)
    if words and words[0] in components:
        return words[0], words[1].strip() if len(words) > 1 else ""
    return "", rest.strip()


def parse_range(spec, command="/add"):
    """An ID or a range "X..Y" / "X-Y" -> (first, last)."""
    spec = (spec or "").split()[0].rstrip(".,;") if spec else ""
    if ".." in spec:
        a, b = spec.split("..", 1)
    elif spec.count("-") == 1 and spec.strip("0123456789-") == "":
        a, b = spec.split("-", 1)
    else:
        a = b = spec
    try:
        lo, hi = int(a), int(b)
    except ValueError:
        raise ValueError(
            f"Usage: {command} ID or {command} X..Y (a range of jobs)."
        )
    return (lo, hi) if lo <= hi else (hi, lo)


def is_text_document(message):
    """Is an attached document a text file (by name or mime)?"""
    doc = message.get("document") or {}
    name = (doc.get("file_name") or "").lower()
    mime = (doc.get("mime_type") or "").lower()
    if name.endswith(TEXT_DOC_EXTS):
        return True
    return mime.startswith("text/") or mime == "application/json"


def _has_file(message):
    return bool(message and (message.get("photo") or message.get("document")))


def is_describe(command):
    """/describe, /describeimg or a plugin's /describe kind."""
    return command in DESCRIBE or (
        bool(command) and command[1:] in plugins.DESCRIBE
    )


def _directive(rest):
    """The directions after /describe, without the links."""
    return " ".join(URL_RE.sub(" ", rest).split())


# --- parse --------------------------------------------------------------------

def parse(update, allowed):
    """The request in the update, or None if it is to be ignored
    (unauthorised user, update type not handled)."""
    callback = update.get("callback_query")
    if callback:
        owner = callback["from"]["id"]
        if owner not in allowed:
            return None
        try:
            press = buttons.decode(callback.get("data"))
        except ValueError:
            press = None
        return ButtonPress(
            callback["message"]["chat"]["id"], owner, callback["id"], press,
            callback["message"].get("message_id", 0),
        )

    message = update.get("message")
    if not message:
        return None
    owner = message.get("from", {}).get("id")
    if owner not in allowed:
        return None
    chat = message["chat"]["id"]
    if message["chat"].get("type") != "private":
        return Refuse(chat, "Use a private chat.")

    if videos.is_video_message(message):
        return _video(message, chat, owner)
    if _has_file(message):
        if message.get("document") and is_text_document(message):
            return _text_file(message, chat, owner)
        return _photo(message, chat, owner)
    reply = message.get("reply_to_message")
    if videos.is_video_message(reply):
        request = _reply_to_video(message, reply, chat, owner)
        if request:
            return request
    elif _has_file(reply):
        request = _reply_to_photo(message, reply, chat, owner)
        if request:
            return request
    return _text(message, chat, owner)


def chat_of(update):
    """The chat to answer in case of error."""
    callback = update.get("callback_query")
    if callback:
        return callback["message"]["chat"]["id"]
    return update["message"]["chat"]["id"]


def _text_file(message, chat, owner):
    """A .txt/.md document (or .json with /list): the content is the text."""
    command, rest = split_command(message.get("caption"))
    name = ((message.get("document") or {}).get("file_name") or "").lower()
    if command == "/list":
        return BatchList(
            chat, owner, message["message_id"],
            body_file=TelegramFile(message),
        )
    if name.endswith(".json"):
        raise ValueError(
            ".json files are used with /list (as caption) or in reply to /list."
        )
    if command and command != "/create":
        raise ValueError(
            f"{command} is not valid with a text file: "
            "use /create or /list."
        )
    component = split_create(rest)[0] if command else ""
    return CreateDraft(
        chat, owner, f"{chat}:doc:{message['message_id']}",
        component=component, text_file=TelegramFile(message),
    )


def _video(message, chat, owner):
    """An attached video: the bot does not use videos, plugin commands do
    (as a caption)."""
    command, rest = split_command(message.get("caption"))
    if command in plugins.COMMANDS:
        return Command(
            chat, owner, command, rest, TelegramFile(message),
            message_id=message["message_id"],
        )
    raise ValueError(
        "The bot does not use videos on its own: send them with the "
        "caption of the command that uses them (see /help)."
    )


def _reply_to_video(message, reply, chat, owner):
    """A reply to a video: a plugin command receives it. Everything else
    (commands, free text) goes on as if the video were not there."""
    command, rest = split_command(message.get("text") or "")
    if command in plugins.COMMANDS:
        return Command(
            chat, owner, command, rest, TelegramFile(reply),
            message_id=message["message_id"],
        )
    return None


def _photo(message, chat, owner):
    """An attached photo or image, with an optional caption."""
    caption = message.get("caption") or ""
    command, rest = split_command(caption)
    album = message.get("media_group_id")
    if is_describe(command):
        return Describe(
            chat, owner, command.strip("/"), TelegramFile(message),
            directive=_directive(rest), reply_to=message["message_id"],
            album=album,
        )
    if command == "/add":
        if not rest:
            raise ValueError(
                "Usage: /add ID or /add X..Y, as the photo's caption."
            )
        first, last = parse_range(rest)
        return AddImage(chat, owner, first, last, TelegramFile(message))
    if command in plugins.COMMANDS:
        return Command(
            chat, owner, command, rest, TelegramFile(message),
            message_id=message["message_id"],
        )
    if command and command != "/create":
        raise ValueError(
            f"{command} is not valid as a caption: "
            "use /create or /add."
        )
    component, prompt = split_create(rest) if command else ("", caption)
    return CreateDraft(
        chat, owner, f"{chat}:message:{message['message_id']}",
        prompt=prompt.strip(), component=component,
        image=TelegramFile(message), album=album,
    )


def _reply_to_photo(message, reply, chat, owner):
    """A reply to a photo: the photo counts as sent now. Commands other
    than /add and /create go to their handler."""
    text = message.get("text") or ""
    command, rest = split_command(text)
    if command == "/add":
        if not rest:
            raise ValueError(
                "Usage: /add ID or /add X..Y in reply to the photo."
            )
        first, last = parse_range(rest)
        return AddImage(chat, owner, first, last, TelegramFile(reply))
    if command and command != "/create":
        return None
    component, prompt = split_create(rest) if command else ("", text)
    # Each reply creates a new draft, even on the same photo.
    return CreateDraft(
        chat, owner, f"{chat}:reply:{reply['message_id']}",
        prompt=prompt.strip(), component=component,
        image=TelegramFile(reply),
    )


def _text(message, chat, owner):
    text = (message.get("text") or "").strip()
    command, rest = split_command(text)
    reply = message.get("reply_to_message")
    reply_file = TelegramFile(reply) if _has_file(reply) else None

    if not command:
        if not text:
            raise ValueError("Empty message.")
        origin = f"{chat}:message:{message['message_id']}"
        urls = extract_urls(text)
        if urls:
            # The prompt is the text left once the links are removed.
            return CreateDraft(
                chat, owner, f"{chat}:links:{message['message_id']}",
                prompt=" ".join(URL_RE.sub(" ", text).split()),
                links=tuple(urls),
            )
        return CreateDraft(chat, owner, origin, prompt=text)

    if command == "/create":
        component, prompt = split_create(rest)
        if not prompt:
            raise ValueError("Write the text after /create.")
        return CreateDraft(
            chat, owner, f"{chat}:message:{message['message_id']}",
            prompt=prompt, component=component,
        )
    if command == "/add":
        if not rest:
            raise ValueError(
                "Usage: /add ID or /add X..Y (after sending a photo)."
            )
        first, last = parse_range(rest)
        return AddImage(chat, owner, first, last, LastPhoto(chat))
    if is_describe(command):
        urls = extract_urls(rest)
        if urls:
            image = Link(urls[0])
        elif reply_file:
            image = reply_file
        else:
            raise ValueError(
                f"Usage: reply to a photo with {command}, send the photo "
                f"with the caption {command}, or {command} <Pinterest "
                "link or image URL>."
            )
        return Describe(
            chat, owner, command.strip("/"), image,
            directive=_directive(rest), reply_to=message["message_id"],
        )
    if command == "/list":
        body_file = None
        image = None
        if not rest and reply and reply.get("document"):
            body_file = reply_file
        elif reply and reply.get("photo"):
            image = reply_file
        return BatchList(
            chat, owner, message["message_id"], body=rest,
            body_file=body_file, image=image,
        )
    return Command(
        chat, owner, command, rest, reply_file,
        message_id=message["message_id"],
    )
