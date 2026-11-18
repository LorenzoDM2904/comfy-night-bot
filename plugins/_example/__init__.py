"""Example plugin: /dice rolls a die, with a button to roll it again.

The leading underscore in the folder name keeps it off. To try it, copy
the folder without the underscore (plugins/dice) and restart the bot.
The full guide is plugins/README.md.
"""

import random

from comfybot import plugins
from comfybot import runtime as rt

ACTION = "dice"         # prefix of the button data: unique among plugins


def register(bot):
    """The bot calls it once at startup: this is where the plugin says
    what it adds. If it raises an error the plugin is skipped."""
    bot.command("/dice", roll_command, help=(
        "/dice [faces] — rolls a die (example plugin)"
    ))
    bot.button(ACTION, roll_button)


async def roll_command(cmd):
    """cmd is an inbound.Command: chat, owner, name, args (the text after
    the command) and reply (the photo or file it replies to)."""
    faces = _faces(cmd.args)
    await _roll(cmd.chat, faces)


async def roll_button(request, payload):
    """request is an inbound.ButtonPress; payload is the text put in the
    button by plugins.button (here the number of faces)."""
    await _roll(request.chat, _faces(payload))


def _faces(text):
    text = (text or "").strip() or "6"
    if not text.isdigit() or not 2 <= int(text) <= 1000:
        # A ValueError goes back to the chat as «Not done: …».
        raise ValueError("Usage: /dice [faces], from 2 to 1000.")
    return int(text)


async def _roll(chat, faces):
    keyboard = [[plugins.button("🎲 Roll again", ACTION, faces)]]
    await rt.TG.text(
        chat, f"🎲 d{faces}: {random.randint(1, faces)}", keyboard,
    )
