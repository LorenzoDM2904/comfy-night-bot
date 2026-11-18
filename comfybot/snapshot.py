"""Snapshot: what a job will run, frozen on confirmation.

Owns the format saved in the jobs.snapshot column. It is built in two
ways: from a draft component (for_component, on confirmation) or from a
derived component (for_derived, for example upscale). Once saved it
never changes: the worker reads it and compiles it into the graph to
submit, and the start message shows its effective parameters.
"""

import copy
import json
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from . import catalog, compiler

VERSION = 2

# Allowed overrides (/matrix: size and seed; plugin chains also length,
# keyframe and mask): they replace the manifest's values.
OVERRIDABLE = ("size", "seed", "length", "keyframe", "mask")


@dataclass(frozen=True)
class Snapshot:
    name: str                       # component name
    workflow: dict                  # graph with the parameters written
    images: List[dict]              # image slots: {"node", "field"}
    output_nodes: List[str]
    prompt_field: Optional[dict] = None   # None for derived components
    params: Dict[str, List[dict]] = field(default_factory=dict)
    model: Optional[dict] = None    # where the model name is
    lora: Optional[dict] = None
    character_lora: Optional[dict] = None   # the character's LoRA
    assets: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    positive: str = ""              # final prompt (with character and light)
    caption: str = ""               # social caption, without the light
    light: str = "off"
    title: str = ""                 # title of a derived job
    variant: str = ""               # label of a /matrix variant

    def to_json(self):
        return json.dumps({"version": VERSION, **asdict(self)})

    @classmethod
    def from_json(cls, text):
        data = json.loads(text or "{}")
        if data.pop("version", None) != VERSION:
            raise ValueError(
                "A job from an earlier version of the bot: it can no longer run."
            )
        return cls(**data)

    def compile(self, uploaded, output_prefix):
        """The graph to submit: uploaded images in their slots, unused
        slots removed, prompt, optional LoRA, output prefix.

        `uploaded` are the names ComfyUI returned for the assets, in the
        same order."""
        graph = copy.deepcopy(self.workflow)
        for binding, name in zip(self.images, uploaded):
            graph[binding["node"]]["inputs"][binding["field"]] = name

        # Variable slots (min/max): unused slots are removed together with
        # the links that point to them, because the image nodes accept
        # missing inputs.
        unused = {b["node"] for b in self.images[len(self.assets):]}
        if unused:
            for node in graph.values():
                for name, value in list(node.get("inputs", {}).items()):
                    if isinstance(value, list) and value \
                            and value[0] in unused:
                        node["inputs"].pop(name, None)
            for node_id in unused:
                graph.pop(node_id, None)

        if self.prompt_field:
            b = self.prompt_field
            graph[b["node"]]["inputs"][b["field"]] = self.positive

        if self.lora and self.lora.get("name"):
            graph = compiler.inject_lora(graph, self.lora)
        if self.character_lora:
            graph = compiler.inject_character_lora(graph, self.character_lora)

        # Output names kept apart per job and submission.
        for node_id in self.output_nodes:
            inputs = graph[node_id]["inputs"]
            if "filename_prefix" in inputs:
                inputs["filename_prefix"] = output_prefix
        return graph

    def effective_params(self):
        """Effective parameters: the values in the frozen graph, read where
        the manifest declares them (model and params), in a fixed order."""
        found = {}
        if self.model:
            value = self._value(self.model)
            if value is not None:
                found["model"] = value
        for name in compiler.PARAMS:
            bindings = self.params.get(name) or []
            if bindings:
                value = self._value(bindings[0])
                if value is not None:
                    found[name] = value
        return found

    def _value(self, binding):
        node = self.workflow.get(binding["node"]) or {}
        value = (node.get("inputs") or {}).get(binding["field"])
        # A link is not a readable value.
        return None if isinstance(value, list) else value


def for_component(cid, plan, positive, caption, light, overrides=None,
                  loras=None):
    """Snapshot of an ordinary job. `plan`: the input plan
    [(path, role)]; `overrides`: size, seed and label from /matrix;
    `loras`: the character's LoRAs (characters.char_loras)."""
    manifest, graph = catalog.load_component(cid)
    overrides = overrides or {}
    return _build(
        manifest, graph,
        {key: overrides[key] for key in OVERRIDABLE if key in overrides},
        assets=[path for path, _ in plan],
        roles=[role for _, role in plan],
        positive=positive,
        caption=caption,
        light=light,
        variant=overrides.get("label") or "",
        loras=loras,
    )


def for_chain(cid, plan, positive, caption, title, overrides=None,
              loras=None):
    """Snapshot of a job in a plugin chain: like an ordinary job, without
    light and with a title. Assets can be "@ref" (the output of another
    job in the chain, see store.start_chain)."""
    manifest, graph = catalog.load_chain(cid)
    overrides = overrides or {}
    return _build(
        manifest, graph,
        {key: overrides[key] for key in OVERRIDABLE if key in overrides},
        assets=[path for path, _ in plan],
        roles=[role for _, role in plan],
        positive=positive,
        caption=caption,
        light="off",
        title=title,
        loras=loras,
    )


def for_derived(kind, assets, roles, title, size=None):
    """Snapshot of a derived job (for example upscale) from its component."""
    manifest, graph = catalog.load_derived(kind)
    return _build(
        manifest, graph, {"size": size} if size else {},
        assets=list(assets), roles=list(roles), title=title,
    )


def character_lora(manifest, loras):
    """The character's LoRA that fits the component: the one for the
    base model the manifest declares in `lora_family`. None if missing."""
    family = manifest.get("lora_family")
    cfg = (loras or {}).get(family) if family else None
    return dict(cfg) if cfg and cfg.get("name") else None


def _build(manifest, graph, values, loras=None, **fields):
    manifest = {**manifest, **values}
    graph, prompt_field = compiler.apply_overrides(graph, manifest)
    lora = character_lora(manifest, loras)
    if lora and lora.get("trigger") and prompt_field:
        # The LoRA's trigger word at the start of the prompt.
        fields["positive"] = f"{lora['trigger']}, {fields.get('positive', '')}"
    return Snapshot(
        name=manifest.get("name") or manifest["id"],
        workflow=graph,
        images=list(manifest.get("images", [])),
        output_nodes=list(manifest["output_nodes"]),
        prompt_field=prompt_field,
        params=dict(manifest.get("params", {})),
        model=manifest.get("model"),
        lora=manifest.get("lora"),
        character_lora=lora,
        **fields,
    )
