"""Component check: `python -m comfybot.check [--offline]`.

1. Every manifest is validated against its workflow (the same check as
   when a job is confirmed), derived components included (for example
   upscale).
2. If ComfyUI answers on COMFYUI_BASE_URL, the graph is compared with
   /object_info: installed nodes, existing fields, models, samplers,
   schedulers and available LoRAs. --offline skips step 2.

Run it after changing a manifest or updating ComfyUI.
Exits with code 1 if it finds problems.
"""

import asyncio
import sys

from . import catalog, compiler, config, plugins
from .engine import LocalEngine


async def fetch_object_info():
    engine = LocalEngine(config.COMFYUI_BASE_URL)
    try:
        return await engine.object_info()
    finally:
        await engine.close()


def run(offline=False):
    plugins.load_all()
    info = None
    if not offline:
        try:
            info = asyncio.run(fetch_object_info())
        except Exception as exc:
            print(
                f"ComfyUI not reachable at {config.COMFYUI_BASE_URL} "
                f"({exc.__class__.__name__}): checking the manifests only.\n"
            )
    found = catalog.all_components()
    if not found:
        print(f"No components in {config.COMPONENTS}.")
        return 1
    failed = 0
    for cid, manifest in sorted(found.items()):
        if not manifest.get("enabled"):
            print(f"-  {cid}: disabled")
            continue
        try:
            manifest, graph = catalog.load_any(cid)
            graph, _ = compiler.apply_overrides(graph, manifest)
        except Exception as exc:
            failed += 1
            print(f"✗  {cid}: {exc}")
            continue
        problems = (
            compiler.check_object_info(manifest, graph, info)
            if info is not None else []
        )
        if problems:
            failed += 1
            print(f"✗  {cid}:")
            for problem in problems:
                print(f"     {problem}")
        else:
            print(f"✓  {cid}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(offline="--offline" in sys.argv[1:]))
