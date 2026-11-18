"""Incoming videos: recognition and format.

The bot itself does not use videos: a video captioned with a plugin
command (or a reply to a video with that command) reaches the plugin as
an inbound.Command, which downloads it with rt.TG.download_video.
"""

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")


def is_video_message(message):
    """Does the message carry a video (as a video or as a document)?"""
    if not message:
        return False
    if message.get("video"):
        return True
    doc = message.get("document") or {}
    name = (doc.get("file_name") or "").lower()
    mime = (doc.get("mime_type") or "").lower()
    return bool(doc) and (mime.startswith("video/") or name.endswith(VIDEO_EXTS))


def video_ext(content):
    """Extension from the magic bytes (MP4/MOV, WebM), None if it is not
    an accepted video."""
    if content[4:8] == b"ftyp":
        return ".mov" if content[8:12] == b"qt  " else ".mp4"
    if content.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    return None
