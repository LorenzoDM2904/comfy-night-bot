"""Creates a component from a workflow in API format, asking for the
bindings instead of guessing them. The result is a starting point: the
parameters (steps, size, seed, ...) are then added to the manifest
following COMPONENTS.md, and `python -m comfybot.check` verifies it.
"""

import json
import re
import sys
from pathlib import Path

from comfybot import config

if len(sys.argv) != 2:
    raise SystemExit(
        "Usage: python configure_component.py /path/to/workflow_api.json"
    )

graph = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(graph, dict) or not graph:
    raise SystemExit("The file does not contain a valid API workflow.")
if "nodes" in graph:
    raise SystemExit(
        "This is the graph JSON. Use Workflow → Export (API) in ComfyUI."
    )
if not all(
    isinstance(node, dict)
    and "class_type" in node
    and isinstance(node.get("inputs"), dict)
    for node in graph.values()
):
    raise SystemExit("API structure not recognised.")

print("\nAvailable nodes and fields:")
for node_id, node in graph.items():
    print(f"\n{node_id}: {node['class_type']}")
    for field, value in node["inputs"].items():
        text = repr(value)
        print(f"  {field}: {text[:180]}")

def binding(label):
    text = input(f"\n{label}, as ID.field: ").strip()
    if "." not in text:
        raise SystemExit("Required format: ID.field")
    node_id, field = text.rsplit(".", 1)
    if node_id not in graph or field not in graph[node_id]["inputs"]:
        raise SystemExit(f"No such field: {text}")
    value = graph[node_id]["inputs"][field]
    if isinstance(value, list):
        raise SystemExit(
            "This field is a link: choose the source node that holds "
            "the text value or the file name."
        )
    return {"node": node_id, "field": field}

cid = input("\nComponent id (lower-case letters, digits, _): ").strip()
if not re.fullmatch(r"[a-z0-9_]+", cid):
    raise SystemExit("Invalid id: use lower-case letters, digits and _.")
name = input("Display name: ").strip() or cid
positive = binding("POSITIVE PROMPT field")
count = int(input("\nNumber of images the component takes [0]: ") or "0")
if not 0 <= count <= 10:
    raise SystemExit("Use 0 to 10 images.")

images = [
    binding(f"File name field for image{i + 1}")
    for i in range(count)
]

outputs = [
    x.strip()
    for x in input(
        "\nIDs of the nodes that save the outputs, comma separated: "
    ).split(",")
    if x.strip()
]
if not outputs or any(x not in graph for x in outputs):
    raise SystemExit("Invalid output node IDs.")

destination = config.COMPONENTS / cid
if destination.exists():
    raise SystemExit(f"Already exists: {destination}")
destination.mkdir(parents=True)

manifest = {
    "id": cid,
    "name": name,
    "version": "1.0.0",
    "engine": "local",
    "enabled": True,
    "workflow": "workflow.json",
    "positive_prompt": positive,
    "images": images,
    "output_nodes": outputs,
}

(destination / "workflow.json").write_text(
    json.dumps(graph, indent=2, ensure_ascii=False)
)
(destination / "manifest.json").write_text(
    json.dumps(manifest, indent=2, ensure_ascii=False)
)

print(f"\nComponent created in {destination}.")
print("The negative prompt and the other parameters were not changed.")
print("Check it: python -m comfybot.check")
