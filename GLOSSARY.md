# comfy-night-bot

A Telegram bot that collects image and video generation requests, queues them and runs them with ComfyUI, preferably at night.

## Jobs

**Job**:
A single generation request, from the draft to the delivery of its results.
_Avoid_: task, request

**Draft**:
A job in its initial state, still editable (prompt, component, character, images) and not yet confirmed.
_Avoid_: open request

**Album**:
Several photos sent together in Telegram, which arrive as separate messages but make a single draft.
_Avoid_: photo group, media group

**Origin key**:
Groups into one draft the messages that belong together, like the photos of an album. It is not there to avoid duplicates: every other message or tap always creates a new draft.
_Avoid_: source key, idempotency key

**Component**:
A ComfyUI workflow ready to use, with its rules on how many images it accepts (from a minimum to a maximum).
_Avoid_: workflow (when talking about the user's choice), model

**Derived component**:
A component used only by derived jobs (for example upscale). It is checked like the others, but it never shows in the picker, is never chosen automatically and cannot be assigned to a draft.
_Avoid_: upscale graph, internal workflow

**Input plan**:
The ordered list of images a component receives, each with its role: a character reference or a scene photo. Scene photos are never dropped; references fill the remaining slots.
_Avoid_: assets, image list

**Compatible component**:
A component for which there is a valid input plan with the images of the draft and of its character.
_Avoid_: suitable component, possible workflow

**Confirmation**:
The step that turns a draft into a queued job, freezing in a snapshot what will run. It has four outcomes: confirmed, needs a component, already confirmed, missing prompt. If the draft has no component and only one is compatible, confirmation picks it; a component chosen by the user but not compatible is never silently replaced: a component is needed.
_Avoid_: submit, start (starting is when the worker takes the job)

**Automatic component choice**:
For drafts created in a batch (`/list`, «Start all»): the first compatible component in `AUTO_COMPONENTS` order, or the only compatible one, so you do not have to choose draft by draft.
_Avoid_: auto, default

**Snapshot**:
The frozen copy, made at confirmation, of everything the job will run: component, final prompt, input plan, light. Changing the component or the character afterwards does not change the jobs already queued.
_Avoid_: configuration, saved state

**Effective parameters**:
The values the job will really run with (model, steps, seed, resolution, length…), read from the snapshot where the manifest declares them. They are the ones shown in the start message.
_Avoid_: settings, technical details

**Title**:
The name a derived job is announced and delivered with, for example «🔍 Upscale 1080×1350 (from #12)».
_Avoid_: delivery title, label

**Variant label**:
The text that tells a `/matrix` variant apart from the others, for example «light sun». It goes with the result, without replacing the job's name.
_Avoid_: title

**Job lifecycle**:
The set of a job's states and of the steps allowed from one state to another. Each step starts only from declared states: if the job has changed meanwhile, the step does not happen.
_Avoid_: workflow (that is the ComfyUI graph), pipeline

**Derived job**:
A job made from the result of a finished job, like upscale (or those of plugins). It goes straight into the queue, without a draft, and starts as soon as the worker is free.
_Avoid_: sub-job, child job

**Delivery**:
Sending a finished job's results to the chat. It has its own state, separate from the job's: an interrupted delivery never makes the job generate again; it is repeated with `/result`.
_Avoid_: sending

**Prompt**:
The text that describes what to generate, written by the user in the draft (plus the character's caption and the light, at confirmation).
_Avoid_: prompt for the request sent to ComfyUI (that is a submission)

**Submission**:
A request sent to ComfyUI to run a job. A job can have more than one: each `/retry` allows a new submission.
_Avoid_: prompt, attempt (the code keeps `prompt_id`, the name ComfyUI uses)

**Submission state**:
What is happening to a submission in ComfyUI: pending, running, done (with the outputs), failed, or missing (neither queued nor in the history).
_Avoid_: status, history

**Uncertain outcome**:
The bot could not verify whether ComfyUI ran the job. The bot never retries on its own: it starts again only with `/retry`.
_Avoid_: error, lost job

## Extensions

**Plugin**:
A folder outside the repository with a `register(bot)` function that adds commands, buttons, `/describe` kinds, components or chains of jobs (see plugins/README.md).
_Avoid_: extension, add-on module

**Chain**:
A job made of several jobs that wait for each other, declared by a plugin: a container draft, the child jobs and, if needed, a job run by the bot instead of ComfyUI (for example a video edit).
_Avoid_: pipeline, workflow

**Container draft**:
The draft of a chain: the user sees it and starts it, but it never goes to ComfyUI; when started it creates the child jobs.
_Avoid_: parent job (in text for the user)
