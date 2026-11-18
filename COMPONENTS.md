# Components: wiring a ComfyUI workflow to the bot

This guide is written above all for an AI (or a person) that has to
create the **manifest** of a workflow, the file that tells the bot where
to write the prompt, the images and the parameters. The repository
contains no components: everyone creates their own, with the models they
have installed.

If you are an AI: read the whole guide before writing anything, and
follow the [checklist](#checklist-for-an-ai) at the end.

## What a component is

The bot is a job manager: it receives requests on Telegram, queues them
and runs them in ComfyUI at night (or when you tell it to). A component
is a ComfyUI workflow plus the contract for using it:

```
components/<id>/
  manifest.json      the contract: where prompt, images and parameters go
  workflow.json      the workflow in API format (see below)
  README.md          what it is for, required nodes and models, licences
```

- The folder is `COMPONENTS_DIR` in the `.env` (default `./components`,
  ignored by git). Plugins can have their own component folders too
  (see [plugins/README.md](plugins/README.md)).
- The manifest's id matches the folder name.
- The workflow sits directly in the component's folder.
- A component contains no Python code: everything it needs is declared
  in the manifest. Telegram logic, queue, night, credentials and
  delivery belong to the bot, not to the component.

Working examples (small, fake, used by the tests) are in
[tests/fixtures/components](tests/fixtures/components): `text` (text to
image), `edit` (1-10 photos + prompt, with an enhancer), `upscale`
(derived), `video` and `video_from_photo`.

## 1. The workflow in API format

`workflow.json` must be the workflow's **API export**, the one ComfyUI
runs: an object `{node id: {"class_type", "inputs"}}`.

```json
{
  "7": {
    "class_type": "KSampler",
    "inputs": {"seed": 0, "steps": 20, "model": ["1", 0], "positive": ["4", 0]}
  }
}
```

- In ComfyUI: Workflow menu → **Export (API)**. The normal file
  (`"nodes"`, `"links"`, positions) is **not** accepted: the bot
  refuses it.
- Node ids inside a subgraph look like `"459:474"`: use them as they
  are in the export.
- A value like `["1", 0]` is a **link** (output 0 of node 1), not a
  value: the bot never writes on links.
- Export the workflow after trying it in ComfyUI, on the same version of
  ComfyUI and of the nodes the bot will use.
- File names in LoadImage nodes do not matter (the bot replaces them),
  but it is better to leave them empty or neutral: the export may
  contain names of personal photos.

## 2. The manifest

The simplest manifest, text to image:

```json
{
  "id": "text",
  "name": "Text → image",
  "version": "1.0.0",
  "engine": "local",
  "enabled": true,
  "workflow": "workflow.json",
  "positive_prompt": {"node": "4", "field": "text"},
  "images": [],
  "model": {"node": "1", "field": "unet_name"},
  "steps": 12,
  "size": {"width": 832, "height": 1216},
  "params": {
    "steps":  [{"node": "7", "field": "steps"}],
    "seed":   [{"node": "7", "field": "seed"}],
    "width":  [{"node": "6", "field": "width"}],
    "height": [{"node": "6", "field": "height"}]
  },
  "output_nodes": ["9"]
}
```

### The fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | the same as the folder name; lower-case letters, digits and `_` |
| `name` | yes | the name shown in the chat |
| `version` | no | the component's version, for you |
| `engine` | yes | `"local"` (the only one supported) |
| `enabled` | yes | `false` = present but not used |
| `workflow` | yes | `"workflow.json"` |
| `positive_prompt` | yes (except derived) | `{"node", "field"}`: where the user's prompt goes |
| `images` | yes | an ordered list of `{"node", "field"}`, one per image (usually the `image` field of a LoadImage); `[]` if the workflow takes no photos |
| `min_images`, `max_images` | no | how many images it really accepts (default: the number of `images`) |
| `model` | no | `{"node", "field"}` of the model name: informative only, shown in the start message |
| `steps`, `seed`, `cfg`, `denoise`, `sampler`, `scheduler`, `length`, `fps` | no | parameter values (see below) |
| `size` | no | `{"width", "height"}` in pixels |
| `params` | if there are values | for each parameter, the list of nodes to write it on |
| `output_nodes` | yes | the ids of the nodes that save the results to deliver |
| `enhancer` | no | nodes that rewrite the prompt, which can be turned off (see below) |
| `lora` | no | a fixed LoRA: `{"name", "strength"}` |
| `lora_family` | no | the base model for the character's LoRA (see below) |
| `derived` | no | `true` for a component used only by derived jobs (see below) |
| `chain_only` | no | `true` for a component used only by a plugin's chains |

### What the bot changes in the graph

The bot writes **only**:

1. the positive prompt, in the `positive_prompt` field (or in
   `enhancer.off_prompt`, if the enhancer is off);
2. the image file names, in the `images` fields;
3. `filename_prefix` of the output nodes, if present;
4. the parameters that have a value in the manifest, on the `params`
   nodes;
5. with the enhancer off: it removes its nodes;
6. the LoRAs (`lora` and the character's LoRA).

Everything else (negative prompt, models, undeclared nodes) stays as it
is in the workflow. The bot does not look for «every KSampler» or «every
text field»: every field it touches is declared in the manifest.

### Parameters

The **value** goes in the manifest key of the same name; the **nodes**
to write it on go in `params` (name → list of `{"node", "field"}`):

| Parameter | Type | Value in the manifest |
|---|---|---|
| `steps`, `seed`, `length`, `fps` | integer | `"steps": 12` |
| `cfg`, `denoise` | number | `"cfg": 1.5` |
| `sampler` | text | `"sampler": "euler"` (the KSampler's `sampler_name` field) |
| `scheduler` | text | `"scheduler": "simple"` |
| `width`, `height` | integer | `"size": {"width": 832, "height": 1216}` |
| `keyframe`, `mask` | integer, text | used by plugins' video chains |

Rules:

- A value without its `params.<name>` is an error: the component is not
  loaded.
- A binding in `params` without a value leaves the workflow's value. It
  pays to declare the useful bindings anyway: `/matrix` changes seed and
  size, and the start message shows the effective parameters.
- The binding must point to a field holding a value, not a link. If the
  value comes from another node (a primitive or an «int» node), the
  binding points to that node.

### Images

- `images` is ordered: the first image goes to the first binding.
- The bot fills the slots like this: first the character's reference
  photos (if the draft has one), then the scene photos sent by the
  user, in message order. Scene photos are never dropped: if there are
  more than `max_images`, the component does not fit.
- With variable slots (for example 1-10 images): declare one LoadImage
  per slot and the real `min_images`/`max_images`. On confirmation the
  bot uses the first N images and **removes** the LoadImage nodes of the
  unused slots from the graph, with their links: the nodes receiving
  them must accept missing inputs. Check it in the node's source
  («optional» or «autogrow» inputs), do not take it for granted.
- For a draft the bot only offers compatible components: those without
  images for drafts without photos, those with N slots for drafts with
  photos.
- PNG, JPEG and WebP images are supported.

### The enhancer (optional)

Some workflows have nodes that rewrite the prompt with a text model. To
be able to turn them off:

```json
"enhancer": {
  "enabled": false,
  "nodes": ["459:477", "459:500", "459:480"],
  "off_prompt": {"node": "459:474", "field": "prompt"}
}
```

With `"enabled": false` the bot removes the `nodes` and writes the
user's prompt into `off_prompt` (which in the workflow can be a link to
the enhancer). If a link to a removed node is left, confirmation fails:
list every enhancer node in `nodes`. In this case `positive_prompt` is
the prompt field used when the enhancer is on.

### LoRAs

- `lora`: a fixed LoRA of the component, `{"name": "file.safetensors",
  "strength": 0.8}`. The bot inserts a LoraLoaderModelOnly between the
  model and the first sampler (KSampler, KSamplerAdvanced,
  SamplerCustom, SamplerCustomAdvanced) with a linked `model` input.
- `lora_family`: the name of the base model (for example `"flux"`). If
  the draft's character has a LoRA for that family
  (`python characters.py lora <name> <family> <file>`), the bot wires
  it the same way, before the sampler. For models that do not use
  ComfyUI's sampler, a plugin can say how to wire it
  (`bot.lora_target`, see [plugins/README.md](plugins/README.md)).

### Derived components

A component with `"derived": true` works on a result already made: it
does not show among the components chosen for drafts and its id equals
the job kind. The bot uses one: **`upscale`** (the `/upscale` command
and the button under results), with one input image and the
`width`/`height` parameters (the bot computes the size from `UPSCALE` in
the `.env`). For a derived component `positive_prompt` is optional.

### Outputs

- `output_nodes`: only the nodes that save the results to deliver (not
  previews or comparisons).
- The bot reads from ComfyUI's history the files those nodes report
  under `images` (SaveImage, and video nodes that behave the same way).
  If a node saves in another way, the bot does not see the files: do not
  pretend it works, say so.

## 3. Checking

```bash
.venv/bin/python -m comfybot.check
```

It checks every manifest and, with ComfyUI running, compares the graphs
with the installed nodes (missing nodes, fields, models, samplers,
schedulers, LoRAs). With `--offline` it only checks the manifests.

To start from a workflow there is also an interactive helper, which
shows nodes and fields and asks for the main bindings:

```bash
.venv/bin/python configure_component.py /path/to/workflow_api.json
```

Then complete the manifest by hand (parameters, size, enhancer) and run
the check again. A real test: a draft from the bot with
`/create <id> a test prompt` and `/submit`.

## Jobs already confirmed

On confirmation the job freezes manifest, workflow, images and prompt:
changing a component does not change the jobs already queued. To try a
change you need a new draft.

## Checklist for an AI

1. Ask for the **API export** of the working workflow. If you only have
   the graph JSON or a screenshot, ask for the export: do not rebuild
   the API format by hand and do not convert subgraphs.
2. Read the JSON: find the real nodes for positive prompt, images,
   sampler, size, outputs. **Do not invent node ids** and do not copy
   those of the examples.
3. Do not use the `text` field of a random node: it can be the negative
   prompt, a system prompt or an internal instruction. If it is not
   clear which one is the positive prompt, ask.
4. Every binding points to a field holding a value, never to a link.
5. For variable image slots, check that the downstream nodes accept
   missing inputs.
6. Declare only the output nodes to deliver.
7. Do not change models or paths on a guess, do not download models:
   write in the component's README.md which ones are needed (with their
   licences) and which custom nodes to install.
8. Run `python -m comfybot.check` and fix things until it is clean.
9. If something does not add up (a node that saves in a different way,
   a required input that would be removed), say so instead of working
   around it.
