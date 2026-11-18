"""Explicit manifests: params, enhancer, /object_info."""

import copy

import pytest

from comfybot import catalog, compiler


def graph_two_samplers():
    return {
        "1": {"class_type": "KSampler", "inputs": {
            "steps": 20, "cfg": 7, "sampler_name": "euler",
            "scheduler": "normal", "denoise": 1, "seed": 1,
            "model": ["9", 0],
        }},
        "2": {"class_type": "KSampler", "inputs": {
            "steps": 20, "cfg": 7, "sampler_name": "euler",
            "scheduler": "normal", "denoise": 1, "seed": 1,
            "model": ["9", 0],
        }},
        "3": {"class_type": "EmptyLatentImage", "inputs": {
            "width": 512, "height": 512, "batch_size": 1,
        }},
        "4": {"class_type": "ImageScale", "inputs": {
            "width": 1024, "height": 1024, "image": ["5", 0],
        }},
        "5": {"class_type": "VAEDecode", "inputs": {}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
        "9": {"class_type": "UNETLoader", "inputs": {"unet_name": "m.st"}},
    }


def manifest(**extra):
    m = {
        "id": "t",
        "positive_prompt": {"node": "6", "field": "text"},
        "images": [],
        "output_nodes": ["7"],
        "params": {
            "steps": [{"node": "1", "field": "steps"}],
            "cfg": [{"node": "1", "field": "cfg"}],
            "width": [{"node": "3", "field": "width"}],
            "height": [{"node": "3", "field": "height"}],
        },
    }
    m.update(extra)
    return m


def test_real_components_are_valid():
    found = catalog.all_components()
    assert "upscale" in found
    for cid, manifest in found.items():
        if not manifest.get("enabled"):
            continue  # like comfybot.check: disabled ones are not loaded
        m, g = catalog.load_any(cid)
        assert compiler.check_manifest(m, g) == []


def test_derived_components_are_not_selectable():
    assert "upscale" not in catalog.components()
    with pytest.raises(ValueError, match="missing"):
        catalog.load_component("upscale")
    with pytest.raises(ValueError, match="missing"):
        catalog.load_derived("text")


def test_prompt_is_required_only_outside_derived_components():
    m = manifest()
    del m["positive_prompt"]
    assert any("positive_prompt" in e
               for e in compiler.check_manifest(m, graph_two_samplers()))
    m["derived"] = True
    assert compiler.check_manifest(m, graph_two_samplers()) == []


def test_model_binding_is_checked():
    m = manifest(model={"node": "9", "field": "name"})
    errors = compiler.check_manifest(m, graph_two_samplers())
    assert errors == ["model: node 9 (UNETLoader) has no field name"]


def test_overrides_touch_only_declared_nodes():
    g = graph_two_samplers()
    m = manifest(steps=8, cfg=2, size={"width": 384, "height": 640})
    out, _ = compiler.apply_overrides(g, m)
    assert out["1"]["inputs"]["steps"] == 8
    assert out["1"]["inputs"]["cfg"] == 2.0
    assert isinstance(out["1"]["inputs"]["cfg"], float)
    # Second sampler and ImageScale not declared: unchanged.
    assert out["2"]["inputs"]["steps"] == 20
    assert out["4"]["inputs"]["width"] == 1024
    assert out["3"]["inputs"]["width"] == 384
    assert out["3"]["inputs"]["height"] == 640


def test_binding_without_value_leaves_workflow_default():
    out, _ = compiler.apply_overrides(graph_two_samplers(), manifest())
    assert out["1"]["inputs"]["steps"] == 20


def test_value_without_binding_fails_clearly():
    m = manifest(fps=24)
    errors = compiler.check_manifest(m, graph_two_samplers())
    assert any("params.fps" in e for e in errors)
    with pytest.raises(ValueError, match="params.fps"):
        compiler.apply_overrides(graph_two_samplers(), m)


def test_size_without_binding_names_size():
    m = manifest(size={"width": 1, "height": 1})
    del m["params"]["width"]
    errors = compiler.check_manifest(m, graph_two_samplers())
    assert errors == [
        "size is set but params.width is missing: say which nodes to "
        "write it on."
    ]


@pytest.mark.parametrize("binding, expected", [
    ({"node": "404", "field": "steps"}, "node 404 is not in the workflow"),
    ({"node": "3", "field": "steps"}, "has no field steps"),
    ({"node": "1", "field": "model"}, "is a link"),
    ({"node": "1"}, "required"),
])
def test_bad_bindings_are_reported(binding, expected):
    m = manifest()
    m["params"]["steps"] = [binding]
    errors = compiler.check_manifest(m, graph_two_samplers())
    assert len(errors) == 1 and expected in errors[0], errors


def test_unknown_param_is_reported():
    m = manifest()
    m["params"]["stepz"] = [{"node": "1", "field": "steps"}]
    errors = compiler.check_manifest(m, graph_two_samplers())
    assert any("params.stepz" in e for e in errors)


def test_invalid_values_are_reported():
    m = manifest(steps=0, size={"width": 0, "height": 5}, sampler=3)
    errors = compiler.check_manifest(m, graph_two_samplers())
    assert any(e.startswith("steps") for e in errors)
    assert any(e.startswith("size") for e in errors)
    assert any(e.startswith("sampler") for e in errors)


def test_enhancer_boolean_is_rejected():
    m = manifest(enhancer=False)
    errors = compiler.check_manifest(m, graph_two_samplers())
    assert any("enhancer must be an object" in e for e in errors)


def _edit():
    return catalog.load_component("edit")


def test_enhancer_off_removes_declared_nodes():
    m, g = _edit()
    nodes = m["enhancer"]["nodes"]
    before = copy.deepcopy(m)
    out, prompt_field = compiler.apply_overrides(g, m)
    assert not any(n in out for n in nodes)
    off = m["enhancer"]["off_prompt"]
    assert prompt_field == off
    assert out[off["node"]]["inputs"][off["field"]] == ""
    # The manifest is not changed.
    assert m == before


def test_enhancer_on_keeps_graph():
    m, g = _edit()
    m["enhancer"]["enabled"] = True
    out, prompt_field = compiler.apply_overrides(copy.deepcopy(g), m)
    assert all(n in out for n in m["enhancer"]["nodes"])
    assert prompt_field == m["positive_prompt"]


def test_enhancer_off_prompt_must_survive():
    m, g = _edit()
    m["enhancer"]["nodes"].append(m["enhancer"]["off_prompt"]["node"])
    errors = compiler.check_manifest(m, g)
    assert any("off_prompt" in e for e in errors)


def info():
    sampler = {"required": {
        "steps": ["INT", {}], "cfg": ["FLOAT", {}],
        "sampler_name": [["euler", "uni_pc"]],
        "scheduler": ["COMBO", {"options": ["normal", "simple"]}],
        "denoise": ["FLOAT", {}], "seed": ["INT", {}],
        "model": ["MODEL"],
    }}
    return {
        "KSampler": {"input": sampler},
        "EmptyLatentImage": {"input": {"required": {
            "width": ["INT", {}], "height": ["INT", {}],
            "batch_size": ["INT", {}],
        }}},
        "ImageScale": {"input": {"required": {
            "width": ["INT", {}], "height": ["INT", {}], "image": ["IMAGE"],
        }}},
        "VAEDecode": {"input": {"required": {}}},
        "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {}]}}},
        "SaveImage": {"input": {"required": {
            "filename_prefix": ["STRING", {}],
        }}},
        "UNETLoader": {"input": {"required": {
            "unet_name": [["m.st"]],
        }}},
        "LoraLoaderModelOnly": {"input": {"required": {
            "lora_name": [["mine.safetensors"]],
        }}},
    }


def test_object_info_accepts_matching_graph():
    m = manifest(lora={"name": "mine.safetensors"})
    assert compiler.check_object_info(m, graph_two_samplers(), info()) == []


def test_object_info_reports_problems():
    g = graph_two_samplers()
    g["8"] = {"class_type": "MissingNode", "inputs": {}}
    g["1"]["inputs"]["sampler_name"] = "ghost"
    g["2"]["inputs"]["scheduler"] = "karras"  # COMBO format
    g["9"]["inputs"]["unet_name"] = "other.st"
    i = info()
    del i["EmptyLatentImage"]["input"]["required"]["height"]
    m = manifest(lora={"name": "absent.safetensors"})
    problems = compiler.check_object_info(m, g, i)
    assert "node 8: MissingNode is not installed in ComfyUI" in problems
    assert "1.sampler_name: 'ghost' not available in ComfyUI" in problems
    assert "2.scheduler: 'karras' not available in ComfyUI" in problems
    assert "9.unet_name: 'other.st' not available in ComfyUI" in problems
    assert "3.height: unknown field for EmptyLatentImage" in problems
    assert "lora 'absent.safetensors' not available in ComfyUI" in problems
    assert len(problems) == 6
