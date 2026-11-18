"""A failed job's saved error, as one readable line."""

import asyncio
import json

from comfybot import failure


def run(coro):
    return asyncio.run(coro)


def history(*messages):
    return json.dumps({
        "status_str": "error", "completed": False,
        "messages": [list(m) for m in messages],
    })


START = ("execution_start", {"prompt_id": "p", "timestamp": 1_000_000})


def test_interrupted_names_the_node_and_the_time():
    text = failure.describe(history(START, ("execution_interrupted", {
        "prompt_id": "p", "node_id": "459:458", "node_type": "KSampler",
        "executed": ["1"], "timestamp": 1_000_000 + 22 * 60_000,
    })))
    assert text.startswith(
        "Interrupted in ComfyUI during KSampler (node 459:458), after 22 min."
    )
    assert "from outside" in text


def test_execution_error_shows_the_exception():
    text = failure.describe(history(START, ("execution_error", {
        "node_id": "7", "node_type": "VAEDecode",
        "exception_type": "torch.OutOfMemoryError",
        "exception_message": "out of memory\n",
        "timestamp": 1_000_000 + 30_000,
    })))
    assert text == (
        "ComfyUI error in VAEDecode (node 7), after 30 s: "
        "torch.OutOfMemoryError: out of memory"
    )


def test_rejected_submission_lists_node_errors():
    text = failure.describe(json.dumps({
        "error": {"type": "prompt_outputs_failed_validation",
                  "message": "Prompt outputs failed validation"},
        "node_errors": {"12": {
            "class_type": "LoadImage",
            "errors": [{"message": "Value not in list",
                        "details": "image: 'x.png'"}],
        }},
    }))
    assert text == (
        "Rejected by ComfyUI: Prompt outputs failed validation\n"
        "- LoadImage (node 12): Value not in list: image: 'x.png'"
    )


def test_missing_node_type():
    text = failure.describe(json.dumps({
        "error": {"type": "missing_node_type",
                  "message": "Node 'CustomNode' not found.",
                  "details": "Node ID '#3'"},
        "node_errors": {},
    }))
    assert text == "Rejected by ComfyUI: Node 'CustomNode' not found. " \
        "(Node ID '#3')"


def test_unknown_text_is_shown_as_it_is():
    for text in ("No images in the declared output nodes.",
                 '{"status_str": "error", "messa', "[1, 2]"):
        assert failure.describe(text) == text


def test_compact_status_drops_the_bulky_parts():
    status = {"status_str": "error", "messages": [
        list(START),
        ["execution_error", {"node_id": "7", "traceback": ["x"] * 500,
                             "current_inputs": {"a": "b" * 5000},
                             "exception_message": "boom"}],
    ]}
    compact = failure.compact_status(status)
    assert compact["messages"][1][1] == {
        "node_id": "7", "exception_message": "boom",
    }
    assert compact["messages"][0] == list(START)


def test_status_shows_the_readable_detail(env):
    job_id = env.job(state="failed", error=history(START, (
        "execution_interrupted",
        {"node_id": "3", "node_type": "KSampler", "timestamp": 1_000_000},
    )))
    run(env.dispatch(env.message(f"/status {job_id}")))
    assert env.tg.texts[-1].startswith(
        "Detail: Interrupted in ComfyUI during KSampler (node 3), after 0 s."
    )
