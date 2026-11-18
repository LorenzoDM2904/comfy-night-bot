"""ComfyUI adapter: from HTTP responses (ComfyUI's real shape) to
submission states. The rest of the bot never sees these responses."""

import asyncio
import json

import httpx
import pytest

from comfybot.engine import (
    ABSENT, INTERRUPTED, REMOVED,
    DefinitiveSubmissionError, Done, Failed, LocalEngine, Missing, Output,
    Pending, Running,
)


def run(coro):
    return asyncio.run(coro)


def entry(prompt_id, token=None):
    """A /queue entry: [number, prompt_id, graph, extra_data, outputs]."""
    extra = {"job_token": token} if token else {}
    return [0, prompt_id, {}, extra, []]


class Comfy:
    """A fake ComfyUI at the HTTP level: answers like the real routes."""

    def __init__(self, running=(), pending=(), history=None):
        self.queue = {
            "queue_running": [entry(p) for p in running],
            "queue_pending": [entry(p) for p in pending],
        }
        self.history = history or {}
        self.posts = []

    def handler(self, req):
        path = req.url.path
        if req.method == "POST":
            self.posts.append((path, json.loads(req.content)))
            return httpx.Response(200, json={})
        if path == "/queue":
            return httpx.Response(200, json=self.queue)
        if path.startswith("/history"):
            pid = path.rsplit("/", 1)[-1]
            if path == "/history":
                return httpx.Response(200, json=self.history)
            return httpx.Response(
                200, json={pid: self.history[pid]} if pid in self.history else {}
            )
        return httpx.Response(404)

    def engine(self):
        return _engine(self.handler)


def _engine(handler):
    engine = LocalEngine("http://comfy.test")
    engine.http = httpx.AsyncClient(
        base_url="http://comfy.test", transport=httpx.MockTransport(handler)
    )
    return engine


# --- submission state --------------------------------------------------------

def test_completed_invio_reports_outputs_per_node():
    comfy = Comfy(history={"p1": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "9": {"images": [
                {"filename": "a.png", "subfolder": "nightbot", "type": "output"},
                {"filename": "b.png", "subfolder": "", "type": "output"},
            ]},
            "12": {"images": [{"filename": "c.mp4", "subfolder": "", "type": "output"}]},
        },
    }})
    status = run(comfy.engine().status("p1"))
    assert status == Done({
        "9": [Output("a.png", "nightbot"), Output("b.png")],
        "12": [Output("c.mp4")],
    })
    assert status.outputs["12"][0].suffix == ".mp4"


def test_error_in_comfy_is_a_failed_invio():
    comfy = Comfy(history={"p1": {"status": {
        "status_str": "error", "completed": False,
        "messages": [["execution_error", {"node_id": "9"}]],
    }}})
    status = run(comfy.engine().status("p1"))
    assert isinstance(status, Failed)
    assert "execution_error" in status.detail


@pytest.mark.parametrize("comfy, expected", [
    (Comfy(running=["p1"]), Running()),
    (Comfy(pending=["p0", "p1"]), Pending()),
    (Comfy(running=["other"]), Missing()),
    # In the history but not finished yet (e.g. just ended).
    (Comfy(history={"p1": {"status": {"completed": False}}}), Running()),
])
def test_invio_not_finished(comfy, expected):
    assert run(comfy.engine().status("p1")) == expected


# --- cancelling a submission-------------------------------------------------------

def test_cancel_interrupts_a_running_invio():
    comfy = Comfy(running=["p1"])
    assert run(comfy.engine().cancel("p1")) == INTERRUPTED
    assert comfy.posts == [("/interrupt", {"prompt_id": "p1"})]


def test_cancel_removes_a_pending_invio():
    comfy = Comfy(pending=["p1"])
    assert run(comfy.engine().cancel("p1")) == REMOVED
    assert comfy.posts == [("/queue", {"delete": ["p1"]})]


def test_cancel_of_an_unknown_invio_does_nothing():
    comfy = Comfy(running=["other"])
    assert run(comfy.engine().cancel("p1")) == ABSENT
    assert comfy.posts == []


def test_cancel_reports_http_errors():
    comfy = Comfy(pending=["p1"])

    def handler(req):
        if req.method == "POST":
            return httpx.Response(500)
        return comfy.handler(req)

    with pytest.raises(httpx.HTTPStatusError):
        run(_engine(handler).cancel("p1"))


# --- ComfyUI busy, finding again, submitting ----------------------------------------

@pytest.mark.parametrize("comfy, busy", [
    (Comfy(), False), (Comfy(running=["x"]), True), (Comfy(pending=["x"]), True),
])
def test_is_busy(comfy, busy):
    assert run(comfy.engine().is_busy()) is busy


def test_find_submission_by_token_in_queue_and_history():
    comfy = Comfy(history={"p9": {"prompt": entry("p9", token="t9")}})
    comfy.queue["queue_pending"] = [entry("p1", token="t1")]
    engine = comfy.engine()
    assert run(engine.find_submission("t1")) == "p1"
    assert run(engine.find_submission("t9")) == "p9"
    assert run(engine.find_submission("nessuno")) is None


def test_submit_accepted_with_node_errors_returns_prompt_id():
    engine = _engine(lambda req: httpx.Response(
        200, json={"prompt_id": "abc", "number": 1,
                   "node_errors": {"9": {"errors": ["x"]}}},
    ))
    assert run(engine.submit({}, "t")) == "abc"


def test_submit_without_prompt_id_is_definitive():
    engine = _engine(lambda req: httpx.Response(200, json={"error": "x"}))
    with pytest.raises(DefinitiveSubmissionError):
        run(engine.submit({}, "t"))


def test_download_writes_the_file(tmp_path):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, content=b"PNG")

    path = tmp_path / "out" / "1.png"
    result = run(_engine(handler).download(Output("a.png", "nightbot"), path))
    assert result == str(path) and path.read_bytes() == b"PNG"
    assert seen == [{"filename": "a.png", "subfolder": "nightbot", "type": "output"}]
