"""Plugins: commands, buttons, /describe kinds, components and job kinds
added from outside, without touching the core.

A plugin is a folder with __init__.py (or a .py file) inside one of the
PLUGINS_DIRS folders (default: plugins). At startup the bot imports it
and calls its register(bot) function, which declares what it adds (see
Bot). Names starting with "_" or "." are not loaded: plugins/_example
is the template to copy. The guide is plugins/README.md.

A plugin that fails to load (an error on import, in register, or a name
already taken) is skipped with a message in the log: the rest of the bot
starts anyway, so a broken plugin does not cost a night.
"""

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

log = logging.getLogger("nightbot")

CALLBACK_LIMIT = 64     # callback_data bytes Telegram accepts


@dataclass(frozen=True)
class DescribeKind:
    """An extra /describe kind (for example /describeart): the same flow
    as the core /describe (photo analysis, then the prompt), with its own
    model and system prompt for the second step."""
    kind: str               # the command without the slash: "describeart"
    model: str              # the model on the llama.cpp server
    system_prompt: str      # step 2: from the analysis to the prompt
    env_key: str = ""       # environment variable that replaces the prompt
    header: str = ""        # header of the chat answer
    show_analysis: bool = False   # also show the step 1 analysis


@dataclass(frozen=True)
class JobKind:
    """A plugin's job kind (the jobs.kind column), for chains: a
    «container» draft that, when started, creates child jobs that wait
    for each other (store.start_chain), plus any jobs the bot runs itself
    instead of ComfyUI (for example an ffmpeg edit). Ordinary (generate)
    and derived jobs need no JobKind.
    """
    kind: str
    # Parent draft: it never goes to ComfyUI and is started with `start`.
    container: bool = False
    summary: Optional[Callable] = None      # async summary(job_id)
    start: Optional[Callable] = None        # async start(job_id, owner)
    edit_fields: Tuple[str, ...] = ()       # draft fields that can change
    edit_hint: str = ""                     # error for the other fields
    queue_header: Optional[Callable] = None  # fn(parent) -> /queue line
    queue_order: Optional[Callable] = None   # fn(child) -> sort key of the children
    # Child of a chain: in /queue under its parent, cancelled with it.
    chain: bool = False
    queue_label: Optional[Callable] = None   # fn(child) -> text in /queue
    intermediate: bool = False   # intermediate result: preview, no buttons
    # Run by the bot: async run(job, out) once the jobs it depends on are
    # done (out: a free path in data/outputs, without extension).
    run: Optional[Callable] = None
    # With run: a cancelled job it depends on leaves the list instead of
    # cancelling this one (if none is left it is cancelled with the parent).
    gathers: bool = False


# --- registry (filled by load_all, read by the core) --------------------------

COMMANDS = {}           # "/name" -> async handler(cmd: inbound.Command)
BEFORE = {}             # core "/name" -> [async fn(cmd) -> True if handled]
BUTTONS = {}            # action -> async handler(request: inbound.ButtonPress, payload)
SELF_ANSWER = set()     # actions whose handler answers the tap itself
DESCRIBE = {}           # "describeart" -> DescribeKind
KINDS = {}              # jobs.kind -> JobKind
RESULT_BUTTONS = []     # fn(job) -> button or None, under every result
PREPARE_INPUT = []      # async fn(path, role) -> path, before the upload
AFTER_JOB = []          # async fn(job_id) -> message or None, once a job is done
SCHEMAS = []            # fn(conn): the plugin's tables, when the database opens
LORA_TARGETS = {}       # class_type -> fn(graph, node_id, cfg): the character's LoRA
COMPONENT_DIRS = []     # extra component folders
HELP = []               # (plugin, text) for /help
LOADED = []             # names of the loaded plugins
_FILES = {}             # name -> file of the loaded plugin


def reset():
    """Empties the registry (tests). Modules already imported stay: a new
    load() reuses them."""
    for table in (COMMANDS, BEFORE, BUTTONS, DESCRIBE, KINDS, LORA_TARGETS):
        table.clear()
    for items in (RESULT_BUTTONS, PREPARE_INPUT, AFTER_JOB, SCHEMAS,
                  COMPONENT_DIRS, HELP, LOADED):
        items.clear()
    SELF_ANSWER.clear()
    _FILES.clear()


# --- reads for the core -------------------------------------------------------

def kinds(**flags):
    """The plugins' job kinds with the given flags, for example
    kinds(container=True) or kinds(run=True) (run: those with a run)."""
    found = []
    for kind in KINDS.values():
        if all(bool(getattr(kind, key)) == value for key, value in flags.items()):
            found.append(kind.kind)
    return tuple(found)


def kind(name):
    """A plugin's JobKind, or None for core jobs."""
    return KINDS.get(name)


async def prepare_input(path, role):
    """A snapshot input passed through the plugins' hooks before the
    upload (for example a video to trim)."""
    for hook in PREPARE_INPUT:
        path = await hook(path, role)
    return path


async def after_job(job_id):
    """The plugins' hooks on a job just finished, before delivery: the
    messages to send afterwards."""
    messages = []
    for hook in AFTER_JOB:
        try:
            message = await hook(job_id)
        except Exception:
            log.exception("A plugin hook on job %s", job_id)
            continue
        if message:
            messages.append(message)
    return messages


def apply_schemas(conn):
    for schema in SCHEMAS:
        schema(conn)
    conn.commit()


def button(text, action, payload=""):
    """A plugin's inline button. On a tap the bot calls the handler
    registered with bot.button(action, ...) passing it `payload` (text,
    for example a job id)."""
    data = f"{action}:{payload}"
    if len(data.encode()) > CALLBACK_LIMIT:
        raise ValueError(
            f"Button data too long ({data!r}): at most "
            f"{CALLBACK_LIMIT} bytes. Save the rest and pass only an id."
        )
    return {"text": text, "callback_data": data}


def help_text():
    """The plugins' /help lines, grouped by plugin."""
    if not HELP:
        return ""
    lines = ["", "PLUGINS"]
    lines += [text for _, text in HELP]
    return "\n".join(lines)


def result_buttons(job):
    """The plugins' buttons under a delivered result."""
    found = []
    for make in RESULT_BUTTONS:
        try:
            made = make(job)
        except Exception:
            log.exception("A plugin button under job %s", job["id"])
            continue
        if made:
            found.append(made)
    return found


# --- the interface given to register(bot) -------------------------------------

class Bot:
    """What a plugin can add. The declarations enter the registry only
    if register() finishes without errors."""

    def __init__(self, name):
        self.name = name
        self.commands = {}
        self.before = {}
        self.buttons = {}
        self.self_answer = set()
        self.describe_kinds = {}
        self.kinds = {}
        self.result_buttons = []
        self.prepare_inputs = []
        self.after_jobs = []
        self.schemas = []
        self.lora_targets = {}
        self.component_dirs = []
        self.help = []

    def command(self, name, handler, help=""):
        """/name: handler(cmd) receives an inbound.Command (chat, owner,
        name, args = the text after the command, reply = the photo or
        file it replies to or that carries the command as a caption). A
        ValueError goes back to the chat as «Not done: …»."""
        _check_command(name)
        self.commands[name.lower()] = handler
        if help:
            self.help.append(help)

    def before_command(self, name, handler):
        """Before a core command (for example /prompt): handler(cmd)
        returns True if it handled it (then the core command does not
        run), otherwise False."""
        self.before.setdefault(name.lower(), []).append(handler)

    def button(self, action, handler, answers=False):
        """A button made with plugins.button(text, action, payload):
        handler(request, payload) receives an inbound.ButtonPress.
        `answers`: the handler answers the tap itself
        (answerCallbackQuery, for example with a notice); otherwise the
        bot answers right away."""
        if not action.replace("_", "").isalnum():
            raise ValueError(f"Invalid button action: {action!r}.")
        self.buttons[action] = handler
        if answers:
            self.self_answer.add(action)

    def job_kind(self, kind, **fields):
        """A job kind for chains (see JobKind)."""
        if not kind.replace("_", "").isalnum():
            raise ValueError(f"Invalid job kind: {kind!r}.")
        self.kinds[kind] = JobKind(kind, **fields)

    def prepare_input(self, hook):
        """async hook(path, role) -> path: every input of a job goes
        through here before the upload to ComfyUI."""
        self.prepare_inputs.append(hook)

    def after_job(self, hook):
        """async hook(job_id) -> text or None: after every job finished in
        ComfyUI, before delivery; the text goes to the chat afterwards."""
        self.after_jobs.append(hook)

    def schema(self, setup):
        """setup(conn): creates or updates the plugin's tables
        (idempotent: CREATE TABLE IF NOT EXISTS, columns added if
        missing)."""
        self.schemas.append(setup)

    def describe(self, name, *, model, system_prompt, env_key="",
                 header="", show_analysis=False, help=""):
        """A command like /describe (a photo with the caption, a reply or
        a link) with its own model and system prompt."""
        _check_command(name)
        kind = name[1:].lower()
        self.describe_kinds[kind] = DescribeKind(
            kind, model, system_prompt, env_key,
            header or f"📝 /{kind}:\n\n", show_analysis,
        )
        if help:
            self.help.append(help)

    def result_button(self, make):
        """make(job) -> button (plugins.button) or None: added under every
        delivered result (job is the jobs row)."""
        self.result_buttons.append(make)

    def lora_target(self, class_type, attach):
        """The character's LoRA on a node of class `class_type`:
        attach(graph, node_id, cfg) changes the graph (cfg: {name,
        strength}). Without such nodes the bot puts a LoraLoaderModelOnly
        before the sampler (see compiler.inject_character_lora)."""
        self.lora_targets[class_type] = attach

    def components(self, path):
        """A folder of the plugin's components (same shape as
        components/: <id>/manifest.json + workflow)."""
        path = Path(path).resolve()
        if not path.is_dir():
            raise ValueError(f"Component folder not found: {path}.")
        self.component_dirs.append(path)


def _check_command(name):
    if not name.startswith("/") or not name[1:].replace("_", "").isalnum():
        raise ValueError(f"Invalid command name: {name!r}.")


CORE_KINDS = ("generate", "upscale")


def _taken():
    """Core commands, actions and kinds, plus those of the plugins
    already loaded."""
    from . import buttons, commands, inbound
    names = set(commands.COMMANDS) | set(inbound.RESERVED) | set(COMMANDS)
    names |= {f"/{kind}" for kind in DESCRIBE}
    return (
        names, set(buttons.CORE_ACTIONS) | set(BUTTONS),
        set(CORE_KINDS) | set(KINDS),
    )


def _commit(bot):
    names, actions, kinds_taken = _taken()
    clash = (
        (set(bot.commands) | {f"/{k}" for k in bot.describe_kinds}) & names
    )
    if clash:
        raise ValueError(f"commands already taken: {', '.join(sorted(clash))}")
    clash = set(bot.buttons) & actions
    if clash:
        raise ValueError(f"buttons already taken: {', '.join(sorted(clash))}")
    clash = set(bot.lora_targets) & set(LORA_TARGETS)
    if clash:
        raise ValueError(f"LoRA nodes already handled: {', '.join(sorted(clash))}")
    clash = set(bot.kinds) & kinds_taken
    if clash:
        raise ValueError(f"job kinds already taken: {', '.join(sorted(clash))}")
    unknown = set(bot.before) - (names - set(COMMANDS))
    if unknown:
        raise ValueError(
            f"before_command on commands that are not the bot's: "
            f"{', '.join(sorted(unknown))}"
        )
    from . import runtime as rt
    if rt.DB is not None:
        # Database already open (tests, reload): the tables right away.
        for setup in bot.schemas:
            setup(rt.DB)
        rt.DB.commit()
    COMMANDS.update(bot.commands)
    for name, hooks in bot.before.items():
        BEFORE.setdefault(name, []).extend(hooks)
    BUTTONS.update(bot.buttons)
    SELF_ANSWER.update(bot.self_answer)
    DESCRIBE.update(bot.describe_kinds)
    KINDS.update(bot.kinds)
    RESULT_BUTTONS.extend(bot.result_buttons)
    PREPARE_INPUT.extend(bot.prepare_inputs)
    AFTER_JOB.extend(bot.after_jobs)
    SCHEMAS.extend(bot.schemas)
    LORA_TARGETS.update(bot.lora_targets)
    COMPONENT_DIRS.extend(bot.component_dirs)
    HELP.extend((bot.name, text) for text in bot.help)
    LOADED.append(bot.name)


# --- loading ------------------------------------------------------------------

def discover(directory):
    """(name, file to import, package folder or None) of the plugins in
    `directory`, by name."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith(("_", ".")):
            continue
        if path.is_dir() and (path / "__init__.py").is_file():
            found.append((path.name, path / "__init__.py", path))
        elif path.is_file() and path.suffix == ".py":
            found.append((path.stem, path, None))
    return found


def load(name, file, package=None):
    """Imports a plugin and calls register(bot). True if it is loaded
    (also if it already was: loading it again does nothing)."""
    file = Path(file).resolve()
    if name in _FILES:
        if _FILES[name] == file:
            return True
        log.error(
            "Plugin %s skipped (%s): there is already a plugin with the "
            "same name (%s).", name, file, _FILES[name],
        )
        return False
    module_name = module_name_of(name)
    fresh = False
    try:
        module = sys.modules.get(module_name)
        if module is None or Path(module.__file__).resolve() != file:
            # Already imported (registry emptied by reset): the module is
            # reused, so whoever imported it sees the same object.
            spec = importlib.util.spec_from_file_location(
                module_name, file,
                submodule_search_locations=[str(package)] if package else None,
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            fresh = True
            spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            raise ValueError("the register(bot) function is missing")
        bot = Bot(name)
        register(bot)
        _commit(bot)
        _FILES[name] = file
    except Exception as exc:
        if fresh:
            sys.modules.pop(module_name, None)
        log.exception("Plugin %s not loaded: %s", name, exc)
        return False
    log.info("Plugin loaded: %s", name)
    return True


def module_name_of(name):
    """A plugin's Python module name: its inner modules are imported as
    nightbot_plugin_<name>.<module> (in tests too)."""
    return f"nightbot_plugin_{name}"


def load_all(directories=None):
    """Loads the plugins of every folder (default config.PLUGINS_DIRS)."""
    if directories is None:
        from . import config
        directories = config.PLUGINS_DIRS
    for directory in directories:
        for name, file, package in discover(directory):
            load(name, file, package)
    return list(LOADED)
