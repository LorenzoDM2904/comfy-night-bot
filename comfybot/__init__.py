"""Comfy Night Bot: a night-time queue of ComfyUI generations over Telegram.

Module map:
  config         .env and constants
  runtime        shared services (rt.DB, rt.JOBS, rt.TG, rt.ENGINE)
  db             schema, migrations, settings
  store          Job store: jobs, lifecycle, images, delivery
  characters     characters and their reference photos
  catalog        components in components/ (derived ones included)
  planner        which images the workflow receives
  compiler       manifest and graph: checks, params, enhancer, LoRA
  snapshot       snapshot format, graph to submit, effective parameters
  check          python -m comfybot.check: component checks
  inbound        Telegram input: from an update to a request
  buttons        inline buttons: building and decoding their data
  images         getting images (Telegram, links, the last photo)
  drafts, batch  drafts, /add, /describe and /list
  confirmation   confirmation: compatible components, outcomes
  jobs           Telegram side: summary, outcomes, picker, cancelling
  derived        upscale: source, size, queueing
  lighting       lighting presets (/light)
  matrix         variants for a controlled comparison (/matrix)
  delivery       start messages and delivery of results
  commands, bot  command table and request dispatch
  plugins        plugin commands, buttons, /describe kinds, components, chains
  videos         recognising incoming videos (for plugins)
  worker         scheduling, running, monitoring, watchdog
  comfy_process  ComfyUI managed by the bot (COMFYUI_MANAGE)
  engine         ComfyUI adapter: submissions, submission state, outputs
  main           startup
"""
