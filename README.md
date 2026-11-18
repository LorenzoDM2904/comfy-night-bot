# Comfy Night Bot

A Telegram bot that puts your computer to work with ComfyUI while you
sleep or are away. From your phone you send photos, text or links: the
bot turns them into **drafts**, you confirm them, and it runs them one
after the other in the **night window** (or right away, if you like).
The results arrive in the chat.

- It runs locally: one computer with ComfyUI, one private chat.
- Jobs start in the night window (00:30–08:00 by default), unless you
  turn it off.
- Every confirmed job is frozen: changing a workflow does not change
  what is already queued.
- Restarting the bot or ComfyUI neither loses nor duplicates jobs.
- It is customisable: workflows are **components** you write (or an AI
  writes, with [COMPONENTS.md](COMPONENTS.md)), and extra commands are
  **plugins** kept outside the repository
  ([plugins/README.md](plugins/README.md)).

The [user guide](#user-guide) explains everything you can do from
Telegram.

---

## Requirements

- A working ComfyUI (ComfyUI Desktop or a manual install) with the
  models of the workflows you want to use. The bot was born on macOS
  with Apple Silicon but only uses the ComfyUI API.
- Python 3.9 or later.
- A Telegram bot (a token from @BotFather) and your Telegram user id.
- Optional: a llama.cpp server with a vision model, for `/describe`.

The project includes no models, workflows, ComfyUI or credentials.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Then open `.env` and set at least the token, the allowed ids and the
ComfyUI address: every setting is explained there. Do not expose
ComfyUI to the Internet.

## Your workflows (components)

The bot knows nothing about models: each ComfyUI workflow becomes a
**component**, a folder in `components/` with the workflow's API export
and a `manifest.json` that says where to write the prompt, the images
and the parameters.

1. In ComfyUI, open a workflow that works and export it in API format
   (Workflow → Export (API)).
2. Write the manifest following [COMPONENTS.md](COMPONENTS.md). The
   guide is written for an AI as well: you can give it the guide and
   the exported file and ask it for the manifest. Alternatively,
   `python configure_component.py workflow_api.json` walks you through
   it.
3. Check:

```bash
python -m comfybot.check            # manifests and, with ComfyUI running, nodes and models
python -m comfybot.check --offline  # manifests only
```

An invalid manifest is not loaded: the bot tells you why instead of
generating with the wrong parameters. The component folder can be
changed with `COMPONENTS_DIR` (for example to keep them in a repository
of your own).

`/upscale` needs a derived component with the id `upscale` (see
COMPONENTS.md).

## Starting

```bash
source .venv/bin/activate
python app.py
```

(the same as `python -m comfybot`; on macOS `caffeinate -i python
app.py` keeps the computer from sleeping). Stop it with Ctrl+C: a job
already sent to ComfyUI goes on there and the bot finds it again when
it restarts.

- With `COMFYUI_MANAGE=false` ComfyUI must already be running.
- With `COMFYUI_MANAGE=true` the bot starts ComfyUI only when there is
  something to run and stops it when the queue is empty (it needs
  `COMFYUI_INSTALL_DIR` or `COMFYUI_START_CMD`). If ComfyUI closes
  during a job, that job becomes `failed` (with a notice in the chat
  and the log path), ComfyUI is restarted and the queue goes on.
- Only one instance can run at a time on the same `DATA_DIR`.

The first time it starts after an update, the database updates itself.

## Plugins: your own commands

Commands, buttons, `/describe` kinds, components and whole chains of
jobs (for example a video cut into pieces and edited back together)
are added with a plugin: a folder with a `register(bot)` function.
Plugins live in `plugins/` (ignored by git) or in any folder listed in
`PLUGINS_DIRS`, so you can keep them private.

```bash
cp -r plugins/_example plugins/dice   # the example plugin: /dice
```

The full guide, written for an AI as well, is
[plugins/README.md](plugins/README.md).

## How the bot works

1. **Draft** (`draft`): created from every photo, album, text, link or
   file you send. You can change it until you confirm it.
2. **Confirmation**: press Start (and, if needed, choose the workflow).
   The bot freezes prompt, images, character, light and workflow in the
   job's *snapshot*, and the job is queued (`queued`).
3. **Running**: in the night window the worker takes one job at a time,
   uploads the images to ComfyUI, submits the graph and follows its
   progress. It does not run on top of generations started by hand in
   ComfyUI.
4. **Delivery**: the results are downloaded to `data/outputs/` and sent
   to the chat.

### Job states

| State | Meaning |
|---|---|
| `draft` | a draft, can be changed |
| `queued` | confirmed, waiting |
| `preparing` | taken by the worker, uploading (can still be cancelled) |
| `submitting` | being sent, to be found again if the bot stops |
| `running` | ComfyUI is running it |
| `done` | results downloaded and recorded |
| `failed` | a definitive error (can be retried with `/retry`) |
| `unknown` | the outcome cannot be verified |
| `skipped` | uncertain outcome: cancelled in ComfyUI and skipped (can be retried) |
| `cancelled` | cancelled by you before it was sent |

An uncertain outcome does not block the night: the bot tries to cancel
the job in ComfyUI, marks it `skipped` and moves on to the next one. The
bot never retries on its own, because it could generate twice: you
allow a new attempt with `/retry`.

### Recovery after a restart

- A job left half way through its uploads goes back to the queue.
- A job already sent is found again in ComfyUI (queue and history) by
  its identifier, without sending it again.
- An interrupted delivery is not generated again: send the result again
  with `/result ID`.

Every submission to ComfyUI is saved in `data/attempts/<ID>_<token>.json`
(the exact graph, the images and their role, the prompt).

## Data and backups

```
data/app.sqlite3   database (jobs, characters, settings)
data/inputs/       photos received
data/outputs/      results
data/attempts/     graphs sent to ComfyUI
data/charrefs/     character photos added from the terminal
```

Nothing is deleted automatically. Back up the whole `data` folder while
the bot is stopped. `.env` and backups contain personal data: protect
them.

## Characters from the terminal

It uses the bot's database:

```bash
python characters.py list
python characters.py add alice --caption "..." photo1.jpg photo2.jpg
python characters.py refs alice another.jpg
python characters.py info alice
python characters.py lora alice flux alice_flux.safetensors --strength 0.9
python characters.py export-dataset alice --out datasets/alice
```

`lora` gives the character a LoRA for a base model: components with the
same `"lora_family"` load it on their own (see COMPONENTS.md).
`export-dataset` prepares the photos to train one.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use a temporary `DATA_DIR`, a fake Telegram and ComfyUI and the
fake components in `tests/fixtures/components`: they never touch real
data.

`app.py` only starts the bot; the code lives in the `comfybot/`
package. The module map is in [comfybot/__init__.py](comfybot/__init__.py),
the terms used in the code and in the messages in
[GLOSSARY.md](GLOSSARY.md). Rule: modules are imported whole
(`from . import planner`) and used qualified (`planner.plan_inputs`);
tests replace the name in the module that defines it (monkeypatch on
`config.POLL`, `worker.can_start`, `rt.TG`).

## Limits

- One computer, one instance, private chats only.
- Local execution only: no cloud.
- No automatic installation of models or custom nodes.
- The bot itself does not use incoming videos (plugins do). Upscale
  works on images only.
- Telegram limits file sizes (about 50 MB for videos the bot sends,
  20 MB for those it receives).
- Delivery is not «exactly once»: network errors and manual resends can
  produce duplicate messages. `/result` does not start while a delivery
  of the same job is still in progress.
- An album cannot be reordered: to change the order, cancel the draft
  and send it again.

## Trying it out before automatic nights

1. `python -m comfybot.check` with no errors.
2. Confirm a job on a fast workflow from Telegram.
3. Check the preview and the original delivered.
4. Stop the bot during a generation and restart it: it must find the
   same job in ComfyUI without sending it again.
5. Try `/night on`/`/night off`, `/pause`/`/resume`, `/cancel`.
6. Try an id that is not allowed: the bot must ignore it.
7. Only then queue long workflows.

---

## User guide

Everything happens in the private chat with the bot. Every request
becomes a **job** with a number (`#42`): that number is the `ID` the
commands use. `/help` lists the commands in the chat too, plugin ones
included.

### In short

1. Send a photo with a caption, or just text.
2. The bot answers with the draft's summary: press **Start**.
3. If several workflows fit, tap the one you want: the job is queued.
4. In the morning you find the result, with buttons to improve it.

### 1. Creating a draft

Anything you send creates a draft, always **without a character**: if
you need one, assign it with `/character ID name`.

| What you send | What happens |
|---|---|
| **Text** | a draft without images; the text is the prompt |
| **A photo with a caption** | a draft with that photo; the caption is the prompt |
| **A photo without a caption** | a draft with the photo and no prompt (set it with `/prompt`) |
| **An album** | a single draft with all the photos, in message order; the summary arrives once the album is complete |
| **A message with Pinterest links** (`pin.it`, `pinterest.com`, public pins) or image URLs | the bot downloads the images and creates a draft with those photos; the remaining text is the prompt |
| **A reply to a photo** in the chat with free text (for example to one of the bot's results) | a new draft with that photo and the text as prompt. Each reply creates a new draft |
| **`/create <component> <text>`** (as text, a caption or a reply to a photo) | as above, with the workflow already chosen |
| **A `.txt` or `.md` file** | its content becomes the prompt, without Telegram's 4096-character limit; optional caption `/create [component]` |
| **`/describe`** on a photo or a link | the bot writes a prompt from the image and offers to make a draft of it (see [section 6](#6-describing-an-image-to-get-a-prompt)) |
| **`/list`** with a JSON | several drafts at once (see [section 8](#8-many-jobs-at-once-list)) |

Component names: `/workflows`.

### 2. Fixing the draft before starting it

While it is a draft (`draft`) you can change it:

| Command | Effect |
|---|---|
| `/prompt ID text` | replaces the prompt |
| `/add ID` | adds a photo: as the photo's caption, in reply to a photo already in the chat, or as text (uses the last photo you sent). The same photo goes into a draft only once |
| `/add X..Y` | adds the same photo to drafts #X to #Y |
| `/character ID name` | assigns a character (`off` removes it) |
| `/light ID preset` | picks the light for this draft (`default` goes back to your default) |
| `/status ID` | shows the summary again, with any error |
| `/cancel ID` · `/cancel X..Y` | cancels the draft, or drafts #X to #Y at once |

### 3. Starting: choosing the workflow and confirming

Press **Start** on the summary (or send `/submit ID`).

- If no workflow is chosen yet and **only one fits** the draft's
  images, the bot uses it and tells you.
- If several fit, the bot offers **only the compatible ones**: tapping
  one starts the job.
- If the chosen workflow (for example with `/create`) does not fit the
  images, the bot does not change it on its own: it tells you why and
  offers the compatible ones.
- On confirmation the job is queued and the bot lists which images the
  workflow receives and in which role (`Image 1 → character`,
  `Image 2 → scene`, ...).

How the images are chosen:

- The photos you sent (the *scene*) are never dropped.
- The character's reference photos fill the remaining slots and come
  first.
- If the scene photos exceed the workflow's slots, that workflow is not
  offered.

On confirmation, the character's fixed caption (at the start) and the
light sentence (at the end) are added to the prompt.

### 4. While the job is queued or running

| Command | Effect |
|---|---|
| `/queue` | your jobs not finished yet |
| `/status ID` | state and error detail, if any |
| `/cancel ID` · `/cancel X..Y` | cancels queued jobs (or jobs still uploading); a job already sent to ComfyUI is not interrupted |
| `/night` | shows the night window and the pause |
| `/night off` · `/night on` | jobs start right away, even by day · back to the night window |
| `/pause` · `/resume` | suspends or resumes starting jobs; the running job finishes anyway |

The window only limits **starting**: a job started at 7:55 finishes even
after 8:00. Night and pause apply to every allowed user.

What you receive meanwhile:

- **At start**, a message with workflow, prompt, light, images, model,
  resolution, steps, sampler and seed.
- **During a long job**, every `WATCHDOG_MINUTES`, a notice with the
  minutes elapsed.
- **If ComfyUI does not answer**, a notice, and another when it is back.

### 5. The result and what you can do with it

For each image you receive a **preview** (with the **🔍 Upscale** button
and the plugins' buttons) and the **original** as a document (📎).
Videos arrive as videos. The first message also has the **social
caption**: the prompt without the light sentence.

| Action | How | What it does |
|---|---|---|
| **Enlarge** | the 🔍 Upscale button or `/upscale ID` | fits the image into the `UPSCALE` box (e.g. 1080×1350) keeping the proportions |
| **Compare lights and resolutions** | `/matrix ID ...` | variants of the same job (see [section 7](#7-light-and-comparisons)) |
| **Generate again** | `/retry ID` | for `failed`, `unknown` or `skipped` jobs: a new attempt with the same snapshot |
| **Receive the result again** | `/result ID` | sends the outputs already generated again, without generating |
| **Start from the result** | reply to the preview with text (or `/create <component> text`) | a new draft with the result as the starting photo |

The upscale becomes a **new job** linked to the original, **starts right
away** (even by day or when paused), and a double tap does not create a
second one while the first is active. The files stay in `data/outputs/`.

### 6. Describing an image to get a prompt

`/describe` writes a prompt from a photo, with the local llama.cpp
model:

- reply to a photo with `/describe`;
- send a photo with the caption `/describe`;
- send `/describe <Pinterest link or image URL>`.

After the command you can add **directives**, which take priority over
everything, for example `/describe set it outdoors`.

| Command | Prompt produced |
|---|---|
| `/describe` | a detailed description of the scene, with the subject at the centre |
| `/describeimg` | an edit instruction that takes the identity from `<image1>` (your character's photo) and builds the scene around it |

- Two steps: first the model analyses the photo factually, then,
  without seeing it again, it writes the prompt from the analysis and
  the directives.
- The light is not described: you pick it with `/light`.
- The system prompts can be changed in the `.env`
  (`DESCRIBE_SYSTEM_PROMPT`, also as the `@path` of a file).

Then press **📝 Draft with this prompt** (valid for 24 hours). The
described photo does **not** go into the draft: add the character's
photo with `/add ID` or assign a character.

### 7. Light and comparisons

**Lighting presets**, added at the end of the prompt on confirmation:

| Preset | Light |
|---|---|
| `window` | soft natural side light from a window |
| `overcast` | diffuse light, no hard shadows |
| `sun` | midday sun, crisp shadows, saturated colours |
| `flash` | frontal snapshot flash, darker background |
| `studio` | studio softbox, clean and even |
| `sunset` | low golden sun, long shadows, rim light on the hair |
| `off` | no hint (default) |

- `/light`: lists the presets and shows your default.
- `/light sun`: the default for drafts you confirm from now on.
- `/light ID flash`: the light for that draft only.

The texts are in `comfybot/lighting.py`.

**Controlled comparison (`/matrix`)**: variants that change **one thing
only**.

```
/matrix ID light=window,studio size=384x640,768x1280 seed=123 now
```

- Each combination of light and size becomes a job, up to 12.
- Prompt, images, character, workflow and **seed** stay the same
  (without `seed=` a random one is picked, the same for all).
- Without `now` the variants run in the night window.
- Each variant carries the label `variant of #ID · light · size ·
  seed`.

### 8. Many jobs at once (`/list`)

`/list` creates a job for each prompt in a JSON: written after the
command, in a `.json` file with the caption `/list`, or in reply to a
`.json` file already in the chat. `/list` alone shows the schema.

```json
{
  "image": "https://pin.it/xxxx",
  "prompts": [
    { "prompt": "portrait in a Paris café", "workflow": "edit" },
    { "prompt": "mountain landscape in the morning", "workflow": "text" },
    { "prompt": "the same person on a beach" }
  ]
}
```

- 1 to 50 prompts. Without `workflow` the first suitable one in
  `AUTO_COMPONENTS` in the `.env` is used, or the only compatible one.
- **With a character** (`"character": "alice"` at the top of the JSON)
  every job receives their reference photos and caption; the jobs are
  queued right away. An entry can have its own `"character"`, or
  `"off"`.
- **With an image** (`"image"` in the JSON, or `/list` in reply to a
  photo) the jobs are queued right away.
- **Without an image** the bot creates the drafts and answers with the
  range (e.g. `#120–#125`): send a photo with the caption
  `/add 120..125` and press **▶️ Start all**.

### 9. Characters

A character is a recurring subject: a name, a **fixed caption** put at
the start of the prompt and one or more **reference photos** passed to
the workflow automatically.

| Command | Effect |
|---|---|
| `/character name` in reply to a photo | creates the character or adds that reference photo |
| `/character name` | creates the character without photos |
| `/caption name text` | the fixed caption |
| `/characters` | list with number of photos and caption |
| `/reference name` | shows the reference photos |
| `/character ID name` | assigns a character to a draft (`off` removes it) |

Names accept letters, digits and underscores.

### 10. All commands

| Command | Use |
|---|---|
| `/help` | list of commands (plugin ones too) |
| `/workflows` | available components |
| `/create [component] text` | a draft with the workflow already chosen |
| `/prompt ID text` | changes a draft's prompt |
| `/add ID` · `/add X..Y` | adds a photo to one or more drafts |
| `/character ID name\|off` | a draft's character |
| `/light [ID] preset` | default light or a draft's light |
| `/submit ID` | chooses the workflow and starts (like Start) |
| `/status ID` | summary and error |
| `/queue` | jobs not finished |
| `/cancel ID` · `/cancel X..Y` | cancels drafts or queued jobs |
| `/retry ID` | a new attempt for `failed`/`unknown`/`skipped` |
| `/result ID` | sends the results again |
| `/upscale ID` | enlarges the result |
| `/matrix ID light=… size=… [seed=N] [now]` | variants for comparison |
| `/describe` · `/describeimg` | a prompt from a photo |
| `/list` | batch jobs from JSON |
| `/character name` · `/characters` · `/caption` · `/reference` | characters |
| `/night [on\|off]` | night window |
| `/pause` · `/resume` | suspends or resumes starting jobs |

## License

Apache 2.0, see [LICENSE](LICENSE). Respect the licences of the models
and nodes you use in your components, and use the bot only with images
of people who have given their consent or with invented characters.
