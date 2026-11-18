"""Manifest and ComfyUI graph.

check_manifest validates the manifest against its workflow;
apply_overrides writes the manifest's parameters into the graph (the
snapshot uses it on confirmation); check_object_info compares the graph
with the installed nodes; inject_lora and inject_character_lora wire the
LoRAs (the character's one through plugins too, plugins.LORA_TARGETS).
Pure functions: they do not change the manifest and touch neither the
network nor the database. The snapshot compiles the graph to submit
(snapshot.py).
"""

from . import plugins

# Parameters the manifest can set: name -> value conversion.
# The values are in the manifest keys with the same name (width/height
# in "size"); the nodes to write them on are in "params".
PARAMS = {
    "steps": int,
    "seed": int,
    "cfg": float,
    "denoise": float,
    "sampler": str,
    "scheduler": str,
    "width": int,
    "height": int,
    "length": int,
    "fps": int,
    "keyframe": int,    # position of the guide frame (video chains)
    "mask": str,        # areas to black out in the control video (JSON)
}


def param_values(manifest):
    """The parameter values set in the manifest, already converted."""
    raw = {name: manifest.get(name) for name in PARAMS}
    size = manifest.get("size") or {}
    raw["width"] = size.get("width")
    raw["height"] = size.get("height")
    return {
        name: PARAMS[name](value)
        for name, value in raw.items() if value is not None
    }


def _is_link(value):
    return isinstance(value, list) and len(value) == 2 \
        and isinstance(value[0], str)


def _check_binding(graph, binding, what, literal=True):
    """A readable error if the binding does not point to a graph field."""
    if not isinstance(binding, dict) or not binding.get("node") \
            or not binding.get("field"):
        return f"{what}: {{\"node\", \"field\"}} required, found {binding!r}"
    node = graph.get(binding["node"])
    if node is None:
        return f"{what}: node {binding['node']} is not in the workflow"
    inputs = node.get("inputs", {})
    if binding["field"] not in inputs:
        return (
            f"{what}: node {binding['node']} "
            f"({node.get('class_type')}) has no field {binding['field']}"
        )
    if literal and _is_link(inputs[binding["field"]]):
        return (
            f"{what}: {binding['node']}.{binding['field']} is a "
            "link; the binding must point to the node that holds the value"
        )
    return None


def _check_values(manifest):
    errors = []
    steps = manifest.get("steps")
    if steps is not None and (
        not isinstance(steps, (int, float)) or int(steps) < 1
    ):
        errors.append("steps must be a whole number >= 1.")
    size = manifest.get("size")
    if size is not None:
        ok = (
            isinstance(size, dict)
            and isinstance(size.get("width"), (int, float))
            and isinstance(size.get("height"), (int, float))
            and int(size["width"]) > 0 and int(size["height"]) > 0
        )
        if not ok:
            errors.append("size needs whole-number width and height > 0.")
    for name in ("seed", "length", "fps", "cfg", "denoise", "keyframe"):
        value = manifest.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0
        ):
            errors.append(f"{name} must be a number >= 0.")
    for name in ("sampler", "scheduler"):
        value = manifest.get(name)
        if value is not None and not isinstance(value, str):
            errors.append(f"{name} must be text.")
    return errors


def check_manifest(manifest, graph):
    """Every problem of the manifest against its workflow (an empty list
    if it is valid). No implicit defaults: every value set must have its
    nodes in params."""
    errors = _check_values(manifest)

    bindings = []
    # Derived components (for example upscale) have no prompt.
    if not manifest.get("derived") or manifest.get("positive_prompt"):
        bindings.append(("positive_prompt", manifest.get("positive_prompt")))
    # Optional: where the model name is, for the effective parameters.
    if manifest.get("model") is not None:
        bindings.append(("model", manifest["model"]))
    bindings += [
        (f"images[{i}]", b) for i, b in enumerate(manifest.get("images", []))
    ]
    for what, b in bindings:
        error = _check_binding(graph, b, what)
        if error:
            errors.append(error)

    params = manifest.get("params", {})
    if not isinstance(params, dict):
        errors.append("params must be an object {name: [binding]}.")
        params = {}
    for name, targets in params.items():
        if name not in PARAMS:
            errors.append(
                f"params.{name}: unknown parameter "
                f"(allowed: {', '.join(PARAMS)})"
            )
            continue
        if not isinstance(targets, list) or not targets:
            errors.append(f"params.{name}: a list of bindings is required.")
            continue
        for i, b in enumerate(targets):
            error = _check_binding(graph, b, f"params.{name}[{i}]")
            if error:
                errors.append(error)
    if not _check_values(manifest):
        for name in param_values(manifest):
            if name not in params:
                where = "size" if name in ("width", "height") else name
                errors.append(
                    f"{where} is set but params.{name} is missing: "
                    "say which nodes to write it on."
                )

    errors += _check_enhancer(manifest, graph)

    # The character's LoRA goes on a node handled by a plugin or before
    # the sampler (see inject_character_lora).
    if manifest.get("lora_family") is not None:
        if not isinstance(manifest["lora_family"], str) \
                or not manifest["lora_family"]:
            errors.append("lora_family must be a name, e.g. \"flux\".")
        elif not _lora_targets(graph) and _model_sampler(graph) is None:
            errors.append(
                "lora_family: the workflow has no sampler with a linked "
                "model input (or a plugin node that loads the LoRA)."
            )

    outputs = manifest.get("output_nodes", [])
    if not outputs or any(x not in graph for x in outputs):
        errors.append("output_nodes: nodes missing from the workflow.")
    return errors


def _check_enhancer(manifest, graph):
    enhancer = manifest.get("enhancer")
    if enhancer is None:
        return []
    if not isinstance(enhancer, dict):
        return [
            "enhancer must be an object "
            "{\"enabled\", \"nodes\", \"off_prompt\"}."
        ]
    errors = []
    if not isinstance(enhancer.get("enabled"), bool):
        errors.append("enhancer.enabled must be true or false.")
    nodes = enhancer.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("enhancer.nodes: the list of nodes to remove is required.")
        nodes = []
    missing = [n for n in nodes if n not in graph]
    if missing:
        errors.append(f"enhancer.nodes: nodes missing from the workflow {missing}")
    off = enhancer.get("off_prompt")
    # The off_prompt field is linked to the enhancer while it is on.
    error = _check_binding(graph, off, "enhancer.off_prompt", literal=False)
    if error:
        errors.append(error)
    elif off["node"] in nodes:
        errors.append("enhancer.off_prompt cannot be among the removed nodes.")
    return errors


def apply_overrides(graph, manifest):
    """Writes the manifest's parameters (steps, cfg, size, ...) into the
    graph on the nodes declared in params, then turns the enhancer off if
    asked.

    Returns (graph, prompt field): with the enhancer off the prompt goes
    into a field other than positive_prompt. The manifest is not changed;
    the graph is (pass a copy if needed)."""
    errors = check_manifest(manifest, graph)
    if errors:
        raise ValueError(
            f"Manifest {manifest.get('id')}: " + "; ".join(errors)
        )
    params = manifest.get("params", {})
    for name, value in param_values(manifest).items():
        for b in params[name]:
            graph[b["node"]]["inputs"][b["field"]] = value
    prompt_field = _apply_enhancer_off(manifest, graph)
    if prompt_field is None:
        prompt_field = manifest.get("positive_prompt")
    return graph, prompt_field


def _apply_enhancer_off(manifest, graph):
    """enhancer.enabled=false: the user's prompt goes straight into the
    enhancer.off_prompt field and the enhancer.nodes are removed, so the
    text model behind the enhancer is not loaded (less memory).
    Returns the prompt field to use (off_prompt), or None if the enhancer
    stays on.
    """
    enhancer = manifest.get("enhancer")
    if not enhancer or enhancer["enabled"]:
        return None
    off = enhancer["off_prompt"]
    nodes = set(enhancer["nodes"])
    # A literal value: compiling replaces it with the prompt.
    graph[off["node"]]["inputs"][off["field"]] = ""
    for nid in nodes:
        graph.pop(nid, None)
    # Safety: no link left pointing to the removed nodes.
    dangling = [
        f"{n}.{f}" for n, nd in graph.items()
        for f, v in nd.get("inputs", {}).items()
        if _is_link(v) and v[0] in nodes
    ]
    if dangling:
        raise ValueError(f"Enhancer off: links left over {dangling}")
    return {"node": off["node"], "field": off["field"]}


def _choices(spec):
    """The accepted values of a choice input, in both /object_info
    formats: [[...], {...}] or ["COMBO", {"options": [...]}]."""
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options")
    return None


def check_object_info(manifest, graph, info):
    """Comparison with ComfyUI's /object_info: installed nodes, fields
    declared in the manifest exist, choice values (models, samplers,
    schedulers, LoRAs) available. `graph` is the one after
    apply_overrides."""
    problems = []
    for nid, node in graph.items():
        cls = node.get("class_type")
        if cls not in info:
            problems.append(f"node {nid}: {cls} is not installed in ComfyUI")
    image_fields = {
        (b["node"], b["field"]) for b in manifest.get("images", [])
    }
    declared = [manifest.get("positive_prompt"), manifest.get("model")]
    declared += manifest.get("images", [])
    for targets in manifest.get("params", {}).values():
        declared += targets
    for b in declared:
        node = graph.get(b["node"]) if b else None
        if node is None or node.get("class_type") not in info:
            continue
        spec = info[node["class_type"]].get("input", {})
        known = {}
        for group in ("required", "optional"):
            known.update(spec.get(group) or {})
        # "a.b" fields are options of a dynamic input: "a" is checked.
        base = b["field"].split(".")[0]
        if base not in known:
            problems.append(
                f"{b['node']}.{b['field']}: unknown field for "
                f"{node['class_type']}"
            )
    for nid, node in graph.items():
        cls = node.get("class_type")
        if cls not in info:
            continue
        spec = info[cls].get("input", {})
        known = {}
        for group in ("required", "optional"):
            known.update(spec.get(group) or {})
        for field, value in node.get("inputs", {}).items():
            if (nid, field) in image_fields or not isinstance(value, str):
                continue
            choices = _choices(known.get(field))
            if choices and value not in choices:
                problems.append(
                    f"{nid}.{field}: {value!r} not available in ComfyUI"
                )
    lora = manifest.get("lora") or {}
    loader = info.get("LoraLoaderModelOnly", {}).get("input", {})
    choices = _choices((loader.get("required") or {}).get("lora_name"))
    if lora.get("name") and choices and lora["name"] not in choices:
        problems.append(f"lora {lora['name']!r} not available in ComfyUI")
    return problems


SAMPLERS = ("KSampler", "KSamplerAdvanced", "SamplerCustom",
            "SamplerCustomAdvanced")


def _model_sampler(graph):
    """The first sampler with a linked model input, or None."""
    for node in graph.values():
        if node.get("class_type", "") in SAMPLERS:
            model_link = node.get("inputs", {}).get("model")
            if isinstance(model_link, list) and len(model_link) == 2:
                return node
    return None


def _add_lora_loader(graph, name, strength, prefix):
    """A LoraLoaderModelOnly between the base model and the sampler.
    False if the graph has no sampler with a linked model input."""
    sampler = _model_sampler(graph)
    if sampler is None:
        return False
    node_id = f"{prefix}{abs(hash(name)) % 10**8}"
    while node_id in graph:
        node_id = node_id + "x"
    graph[node_id] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "lora_name": name,
            "strength_model": strength,
            "model": sampler["inputs"]["model"],
        },
    }
    sampler["inputs"]["model"] = [node_id, 0]
    return True


def inject_lora(graph, cfg):
    """Injects a LoraLoaderModelOnly between the base model and the
    sampler. The component's manifest declares `lora: {name, strength}`."""
    if not _add_lora_loader(
        graph, cfg["name"], float(cfg.get("strength", 0.8)), "_lora_",
    ):
        raise ValueError(
            "manifest.lora needs a sampler with a linked model input."
        )
    return graph


def _lora_targets(graph):
    """The graph nodes on which a plugin can wire the character's LoRA
    (plugins.LORA_TARGETS, by node class)."""
    return [
        node_id for node_id, node in graph.items()
        if node.get("class_type") in plugins.LORA_TARGETS
    ]


def inject_character_lora(graph, cfg):
    """Wires the character's LoRA `cfg` ({name, strength}): on nodes
    handled by a plugin (for example the loaders of a video model that
    does not use ComfyUI's sampler), otherwise with a
    LoraLoaderModelOnly before the sampler. An error if the graph has
    neither."""
    targets = _lora_targets(graph)
    for node_id in targets:
        plugins.LORA_TARGETS[graph[node_id]["class_type"]](graph, node_id, cfg)
    if targets:
        return graph
    if not _add_lora_loader(
        graph, cfg["name"], float(cfg.get("strength", 0.9)), "_charlora_",
    ):
        raise ValueError(
            "The character's LoRA needs a sampler with a linked model "
            "input, or a plugin node that loads it."
        )
    return graph
