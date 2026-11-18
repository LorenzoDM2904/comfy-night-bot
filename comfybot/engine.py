"""ComfyUI adapter. The rest of the bot knows neither the HTTP routes
nor the shape of the responses: it sees submissions, submission states
and outputs.

Submission states: Pending, Running, Done (with the outputs), Failed
(with ComfyUI's detail), Missing (neither queued nor in the history).
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import httpx

from .failure import compact_status

log = logging.getLogger("nightbot.engine")


@dataclass(frozen=True)
class Output:
    """A file produced by ComfyUI (fetched with download)."""
    filename: str
    subfolder: str = ""
    type: str = "output"

    @property
    def suffix(self):
        return Path(self.filename).suffix.lower()


@dataclass(frozen=True)
class Pending:
    """Queued in ComfyUI, not started yet."""


@dataclass(frozen=True)
class Running:
    """Running (or in the history but not finished yet)."""


@dataclass(frozen=True)
class Done:
    outputs: Dict[str, List[Output]] = field(default_factory=dict)  # by node


@dataclass(frozen=True)
class Failed:
    detail: str


@dataclass(frozen=True)
class Missing:
    """Neither queued nor in the history: ComfyUI knows nothing of it."""


# Outcomes of cancel().
INTERRUPTED = "interrupted"   # it was running: interrupted
REMOVED = "removed"           # it was waiting: removed from the queue
ABSENT = "absent"             # there was nothing left to cancel


class DefinitiveSubmissionError(Exception):
    """Request refused: the server did not accept it."""


class LocalEngine:
    name = "local"

    def __init__(self, base_url):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=60,
        )

    async def close(self):
        await self.http.aclose()

    async def health(self):
        r = await self.http.get("/system_stats")
        r.raise_for_status()
        return r.json()

    async def upload_input(self, path):
        path = Path(path)
        with path.open("rb") as f:
            r = await self.http.post(
                "/upload/image",
                files={"image": (path.name, f, "application/octet-stream")},
                data={"type": "input", "overwrite": "false"},
            )
        r.raise_for_status()
        data = r.json()
        name = data["name"]
        subfolder = data.get("subfolder", "")
        return f"{subfolder}/{name}" if subfolder else name

    async def submit(self, workflow, token):
        r = await self.http.post(
            "/prompt",
            json={
                "prompt": workflow,
                "client_id": "comfy-night-bot",
                "extra_data": {"job_token": token},
            },
            timeout=120,
        )
        if 400 <= r.status_code < 500:
            raise DefinitiveSubmissionError(r.text[:1500])
        r.raise_for_status()
        data = r.json()
        # ComfyUI can accept the prompt (prompt_id present) and still
        # report node_errors for invalid outputs: the job is queued, so it
        # must not be treated as refused.
        if not data.get("prompt_id"):
            raise DefinitiveSubmissionError(json.dumps(data)[:1500])
        if data.get("node_errors"):
            log.warning(
                "Prompt %s accepted with node_errors: %s",
                data["prompt_id"], json.dumps(data["node_errors"])[:1500],
            )
        return data["prompt_id"]

    async def status(self, prompt_id):
        """The state of submission `prompt_id`."""
        entry = (await self._history(prompt_id)).get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                return Failed(json.dumps(compact_status(status))[:3000])
            if status.get("completed"):
                return Done({
                    node: [
                        Output(
                            m["filename"], m.get("subfolder", ""),
                            m.get("type", "output"),
                        )
                        for m in node_output.get("images", [])
                    ]
                    for node, node_output in entry.get("outputs", {}).items()
                })
        running, pending = _queue_ids(await self._queue())
        if prompt_id in pending:
            return Pending()
        if prompt_id in running or entry:
            return Running()
        return Missing()

    async def cancel(self, prompt_id):
        """Cancels a submission: interrupts it if running, removes it from
        the queue if waiting. Returns INTERRUPTED, REMOVED or ABSENT."""
        running, pending = _queue_ids(await self._queue())
        if prompt_id in running:
            # Recent ComfyUI versions interrupt only if `prompt_id` is the
            # running one; older ones ignore the body.
            r = await self.http.post(
                "/interrupt", json={"prompt_id": prompt_id}
            )
            r.raise_for_status()
            return INTERRUPTED
        if prompt_id in pending:
            r = await self.http.post("/queue", json={"delete": [prompt_id]})
            r.raise_for_status()
            return REMOVED
        return ABSENT

    async def is_busy(self):
        """Is ComfyUI already busy (with our submissions or manual ones)?"""
        running, pending = _queue_ids(await self._queue())
        return bool(running or pending)

    async def object_info(self):
        """Installed nodes with their inputs and accepted values."""
        r = await self.http.get("/object_info")
        r.raise_for_status()
        return r.json()

    async def _queue(self):
        r = await self.http.get("/queue")
        r.raise_for_status()
        return r.json()

    async def _history(self, prompt_id=None):
        suffix = f"/{prompt_id}" if prompt_id else ""
        r = await self.http.get("/history" + suffix)
        r.raise_for_status()
        return r.json()

    async def find_submission(self, token):
        """Finds an interrupted submission again through extra_data."""
        queue = await self._queue()
        for entry in (
            queue.get("queue_running", []) +
            queue.get("queue_pending", [])
        ):
            if len(entry) > 3 and isinstance(entry[3], dict):
                if entry[3].get("job_token") == token:
                    return entry[1]

        history = await self._history()
        for prompt_id, entry in history.items():
            prompt = entry.get("prompt", [])
            if len(prompt) > 3 and isinstance(prompt[3], dict):
                if prompt[3].get("job_token") == token:
                    return prompt_id
        return None

    async def download(self, output, path):
        """Downloads an Output to `path` (atomic write)."""
        r = await self.http.get(
            "/view",
            params={
                "filename": output.filename,
                "subfolder": output.subfolder,
                "type": output.type,
            },
            timeout=120,
        )
        r.raise_for_status()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".part")
        temp.write_bytes(r.content)
        temp.replace(path)
        return str(path)


def _queue_ids(queue):
    """Ids of the running and waiting submissions in a /queue response."""
    return (
        [entry[1] for entry in queue.get("queue_running", [])],
        [entry[1] for entry in queue.get("queue_pending", [])],
    )
