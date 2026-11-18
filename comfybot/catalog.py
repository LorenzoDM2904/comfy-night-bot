"""Components (ComfyUI workflows) declared in components/.

Two families: components for drafts (the picker, automatic choice,
/create) and derived components (`"derived": true`, for example upscale),
used only by derived jobs whose kind equals their id.
A draft component with `"chain_only": true` does not show up in the
picker: only plugin chains use it (for example a component that wants a
video rather than a photo).
"""

import json
import logging
from pathlib import Path

from . import compiler, config, plugins

log = logging.getLogger("nightbot")


def all_components():
    """Every readable manifest, derived ones included (for the check):
    those in COMPONENTS_DIR and those in the plugins' folders."""
    found = {}
    for path in manifest_paths():
        try:
            manifest = json.loads(path.read_text())
            cid = manifest["id"]
            if cid != path.parent.name:
                raise ValueError("The id and the folder name must match.")
            manifest["_dir"] = str(path.parent)
            found[cid] = manifest
        except Exception as exc:
            log.warning("Invalid manifest: %s (%s)", path, exc)
    return found


def manifest_paths():
    for directory in (config.COMPONENTS, *plugins.COMPONENT_DIRS):
        yield from sorted(directory.glob("*/manifest.json"))


def components():
    """The components that can be assigned to a draft."""
    return {
        cid: m for cid, m in all_components().items()
        if not m.get("derived") and not m.get("chain_only")
    }


def load_component(cid):
    """Manifest and graph of a draft component."""
    return _load(components().get(cid))


def load_chain(cid):
    """Manifest and graph of a component for a plugin chain: a draft
    component or a `chain_only` one."""
    manifest = all_components().get(cid)
    return _load(manifest if manifest and not manifest.get("derived") else None)


def load_derived(kind):
    """Manifest and graph of the derived component for `kind` (upscale, ...)."""
    manifest = all_components().get(kind)
    return _load(manifest if manifest and manifest.get("derived") else None)


def load_any(cid):
    """Manifest and graph of any component (for the check)."""
    return _load(all_components().get(cid))


def _load(manifest):
    if not manifest or not manifest.get("enabled"):
        raise ValueError("Component missing or not configured.")
    cid = manifest["id"]
    if manifest.get("engine") != "local":
        raise ValueError("Unsupported component engine.")
    graph_path = Path(manifest["_dir"]) / manifest["workflow"]
    if graph_path.resolve().parent != Path(manifest["_dir"]).resolve():
        raise ValueError("The workflow must be in the component's folder.")
    graph = json.loads(graph_path.read_text())
    if "nodes" in graph or not isinstance(graph, dict) or not graph:
        raise ValueError("The workflow must be in API format.")
    errors = compiler.check_manifest(manifest, graph)
    if errors:
        raise ValueError(f"Invalid manifest {cid}: " + "; ".join(errors))
    return manifest, graph
