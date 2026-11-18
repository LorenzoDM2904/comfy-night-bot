"""A failed job's error, as one readable line (/status).

The database keeps the error as it arrived: the ComfyUI history status
(status_str "error"), the response to a rejected submission (error and
node_errors) or a text from the bot. Here it is only read for display;
anything not recognised is shown as it is.
"""

import json

# execution_error data that does not help understand the error and that
# alone fills the 3000 saved characters (see compact_status).
BULKY = ("traceback", "current_inputs", "current_outputs")


def compact_status(status):
    """The history status without traceback and node inputs/outputs, so
    the saved JSON stays whole and readable by describe()."""
    messages = []
    for item in status.get("messages", []):
        if isinstance(item, list) and len(item) == 2 \
                and isinstance(item[1], dict):
            item = [item[0], {
                k: v for k, v in item[1].items() if k not in BULKY
            }]
        messages.append(item)
    return {**status, "messages": messages}


def describe(text):
    """A short explanation of the saved error, or the text as it is."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(data, dict):
        return text
    if data.get("status_str") == "error":
        return _history(data) or text
    if "error" in data or "node_errors" in data:
        return _rejected(data) or text
    return text


def _history(status):
    events = {
        item[0]: item[1] for item in status.get("messages", [])
        if isinstance(item, list) and len(item) == 2
        and isinstance(item[1], dict)
    }
    start = events.get("execution_start", {}).get("timestamp")
    if "execution_interrupted" in events:
        event = events["execution_interrupted"]
        return (
            f"Interrupted in ComfyUI during {_node(event)}"
            f"{_after(start, event.get('timestamp'))}. "
            "The bot does not interrupt running jobs: the interruption "
            "came from outside (the ComfyUI interface or another client)."
        )
    if "execution_error" in events:
        event = events["execution_error"]
        what = ": ".join(
            part for part in (
                event.get("exception_type"),
                (event.get("exception_message") or "").strip(),
            ) if part
        )
        return (
            f"ComfyUI error in {_node(event)}"
            f"{_after(start, event.get('timestamp'))}"
            + (f": {what}" if what else ".")
        )
    return None


def _rejected(data):
    lines = []
    error = data.get("error")
    if isinstance(error, dict):
        line = error.get("message") or error.get("type") or ""
        if error.get("details"):
            line += f" ({error['details']})"
        lines.append(f"Rejected by ComfyUI: {line}")
    elif error:
        lines.append(f"Rejected by ComfyUI: {error}")
    nodes = data.get("node_errors")
    if isinstance(nodes, dict):
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            for err in node.get("errors", []):
                msg = err.get("message", "")
                if err.get("details"):
                    msg += f": {err['details']}"
                lines.append(
                    f"- {node.get('class_type', '?')} (node {node_id}): {msg}"
                )
    return "\n".join(lines) or None


def _node(event):
    kind = event.get("node_type") or "a node"
    node = event.get("node_id")
    return f"{kind} (node {node})" if node else kind


def _after(start, end):
    """", after 22 min" between two history timestamps (milliseconds)."""
    if not isinstance(start, (int, float)) or \
            not isinstance(end, (int, float)) or end < start:
        return ""
    seconds = round((end - start) / 1000)
    if seconds < 60:
        return f", after {seconds} s"
    return f", after {round(seconds / 60)} min"
