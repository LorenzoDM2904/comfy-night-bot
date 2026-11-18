"""Plugins: loading, commands, buttons, /describe kinds, components."""

import asyncio
import shutil
import textwrap

import pytest

from conftest import CHAT, OWNER, ROOT
from comfybot import (
    bot, buttons, catalog, commands, delivery, describer, drafts, inbound,
    plugins,
)


def run(coro):
    return asyncio.run(coro)


def write_plugin(directory, name, body):
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(body))
    return path


def photo(caption):
    return {
        "message_id": 4, "caption": caption, "photo": [{"file_id": "f"}],
        "chat": {"id": CHAT, "type": "private"}, "from": {"id": OWNER},
    }


def press(data):
    return inbound.ButtonPress(CHAT, OWNER, "cb", buttons.decode(data), 7)


# --- the example plugin ---------------------------------------------------------

@pytest.fixture
def example(env, tmp_path):
    """plugins/_example copied without the underscore, as the guide says."""
    shutil.copytree(ROOT / "plugins" / "_example", tmp_path / "dice")
    assert plugins.load_all([tmp_path]) == ["dice"]


def test_example_is_off_until_copied(env):
    assert plugins.discover(ROOT / "plugins") == []


def test_example_command_and_button(env, example):
    run(env.dispatch(env.message("/dice 20")))
    assert env.tg.texts[-1].startswith("🎲 d20: ")
    [[button]] = env.tg.keyboards[-1]
    assert button["callback_data"] == "dice:20"
    run(bot.press(press(button["callback_data"])))
    assert env.tg.texts[-1].startswith("🎲 d20: ")


def test_example_errors_reach_the_user(env, example):
    with pytest.raises(ValueError, match="Usage: /dice"):
        run(env.dispatch(env.message("/dice many")))


def test_help_lists_plugin_commands(env, example):
    run(env.dispatch(env.message("/help")))
    assert env.tg.texts[-1].endswith(
        "\nPLUGINS\n/dice [faces] — rolls a die (example plugin)"
    )


def test_without_plugins_the_command_is_unknown(env):
    with pytest.raises(ValueError, match="Unknown command"):
        run(env.dispatch(env.message("/dice")))


# --- loading --------------------------------------------------------------------

def test_broken_plugin_is_skipped_and_registers_nothing(env, tmp_path):
    write_plugin(tmp_path, "broken", """
        def register(bot):
            bot.command("/broken", None)
            raise RuntimeError("boom")
    """)
    write_plugin(tmp_path, "healthy", """
        def register(bot):
            async def hello(cmd):
                pass
            bot.command("/hello", hello)
    """)
    assert plugins.load_all([tmp_path]) == ["healthy"]
    assert set(plugins.COMMANDS) == {"/hello"}


def test_plugin_cannot_take_core_names(env, tmp_path):
    write_plugin(tmp_path, "a", """
        def register(bot):
            bot.command("/queue", None)
    """)
    write_plugin(tmp_path, "b", """
        def register(bot):
            bot.button("submit", None)
    """)
    write_plugin(tmp_path, "c", """
        def register(bot):
            bot.describe("/describe", model="m", system_prompt="p")
    """)
    assert plugins.load_all([tmp_path]) == []
    assert "/queue" in commands.COMMANDS


def test_two_plugins_cannot_share_a_command(env, tmp_path):
    for name in ("a", "b"):
        write_plugin(tmp_path, name, """
            def register(bot):
                bot.command("/same", None)
        """)
    assert plugins.load_all([tmp_path]) == ["a"]


def test_loading_twice_changes_nothing(env, tmp_path):
    write_plugin(tmp_path, "a", """
        def register(bot):
            bot.command("/uno", None, help="/uno")
    """)
    plugins.load_all([tmp_path])
    assert plugins.load_all([tmp_path]) == ["a"]
    assert plugins.HELP == [("a", "/uno")]


def test_missing_directory_is_ignored(env, tmp_path):
    assert plugins.load_all([tmp_path / "missing"]) == []


def test_package_plugin_can_import_its_modules(env, tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "texts.py").write_text("GREETING = 'hello from the module'\n")
    (package / "__init__.py").write_text(textwrap.dedent("""
        from comfybot import runtime as rt
        from .texts import GREETING

        def register(bot):
            async def greet(cmd):
                await rt.TG.text(cmd.chat, GREETING)
            bot.command("/greet", greet)
    """))
    assert plugins.load_all([tmp_path]) == ["package"]
    run(env.dispatch(env.message("/greet")))
    assert env.tg.texts[-1] == "hello from the module"


# --- commands with a photo ---------------------------------------------------------

def test_plugin_command_as_photo_caption(env, tmp_path):
    write_plugin(tmp_path, "a", """
        def register(bot):
            bot.command("/look", None)
    """)
    plugins.load_all([tmp_path])
    request = inbound.parse({"message": photo("/look closely")}, {OWNER})
    assert request == inbound.Command(
        CHAT, OWNER, "/look", "closely", inbound.TelegramFile(photo("/look closely")),
    )


def test_unknown_caption_is_still_refused(env):
    with pytest.raises(ValueError, match="is not valid as a caption"):
        inbound.parse({"message": photo("/look")}, {OWNER})


# --- buttons ---------------------------------------------------------------------

def test_button_payload_round_trip(env, tmp_path):
    write_plugin(tmp_path, "a", """
        def register(bot):
            bot.button("try", None)
    """)
    plugins.load_all([tmp_path])
    button = plugins.button("Try", "try", "12:x")
    assert buttons.decode(button["callback_data"]) == buttons.Press(
        "try", payload="12:x",
    )


def test_unregistered_button_is_invalid(env):
    with pytest.raises(ValueError, match="Invalid button"):
        buttons.decode("try:1")


def test_button_data_too_long():
    with pytest.raises(ValueError, match="64 bytes"):
        plugins.button("x", "try", "a" * 64)


def test_result_buttons_go_under_results(env, tmp_path):
    write_plugin(tmp_path, "a", """
        from comfybot import plugins

        def register(bot):
            bot.result_button(lambda job: plugins.button("⭐", "star", job["id"]))
            bot.result_button(lambda job: None)
    """)
    plugins.load_all([tmp_path])
    job_id = env.job(state="done", outputs="[]")
    [row] = delivery.result_keyboard(env.row(job_id))
    assert row[-1] == {"text": "⭐", "callback_data": f"star:{job_id}"}


# --- /describe kinds --------------------------------------------------------------

def test_describe_kind(env, tmp_path, monkeypatch):
    write_plugin(tmp_path, "a", """
        def register(bot):
            bot.describe(
                "/describeart", model="artist", system_prompt="PAINT",
                env_key="ART_PROMPT", header="🖌 ", show_analysis=True,
                help="/describeart — like /describe, but a painting",
            )
    """)
    plugins.load_all([tmp_path])
    assert inbound.parse({"message": photo("/describeart")}, {OWNER}).kind == (
        "describeart"
    )
    assert describer.get_system_prompt("describeart") == "PAINT"
    monkeypatch.setenv("ART_PROMPT", "OTHER")
    assert describer.get_system_prompt("describeart") == "OTHER"

    seen = []

    async def fake_describe(path, kind, model, base_url, directive=""):
        seen.append((kind, model))
        return "ANALYSIS", "PROMPT"

    monkeypatch.setattr(describer, "describe", fake_describe)
    run(drafts.describe_flow(CHAT, OWNER, env.image(), kind="describeart"))
    assert seen == [("describeart", "artist")]
    assert env.tg.texts[-1].startswith("🔎 Analysis:\n\nANALYSIS\n\n🖌 PROMPT")


# --- components -----------------------------------------------------------------

def test_plugin_components_join_the_catalog(env, tmp_path):
    folder = tmp_path / "a"
    shutil.copytree(
        ROOT / "tests" / "fixtures" / "components" / "upscale", folder / "components" / "enlarge",
    )
    manifest = folder / "components" / "enlarge" / "manifest.json"
    manifest.write_text(
        manifest.read_text().replace('"id": "upscale"', '"id": "enlarge"')
        .replace('"enabled": false', '"enabled": true')
    )
    (folder / "__init__.py").write_text(textwrap.dedent("""
        from pathlib import Path

        def register(bot):
            bot.components(Path(__file__).parent / "components")
    """))
    plugins.load_all([tmp_path])
    assert "enlarge" in catalog.all_components()
    manifest, _ = catalog.load_derived("enlarge")
    assert manifest["derived"]


# --- character LoRA -------------------------------------------------------------

LORA = {"name": "anna.safetensors", "strength": 0.7}


def test_character_lora_goes_before_the_sampler(env):
    from comfybot import compiler
    _, graph = catalog.load_component("text")
    graph = compiler.inject_character_lora(graph, LORA)
    [lora_id] = [k for k, n in graph.items()
                 if n["class_type"] == "LoraLoaderModelOnly"]
    assert graph[lora_id]["inputs"] == {
        "lora_name": "anna.safetensors", "strength_model": 0.7,
        "model": ["1", 0],
    }
    assert graph["7"]["inputs"]["model"] == [lora_id, 0]


def test_lora_family_needs_a_sampler_or_a_plugin_node(env, tmp_path):
    from comfybot import compiler
    manifest, graph = catalog.load_component("video")
    errors = compiler.check_manifest({**manifest, "lora_family": "v"}, graph)
    assert any("lora_family" in e for e in errors)
    with pytest.raises(ValueError, match="sampler"):
        compiler.inject_character_lora(graph, LORA)

    write_plugin(tmp_path, "video_lora", """
        def register(bot):
            bot.lora_target("VideoModelLoader", attach)


        def attach(graph, node_id, cfg):
            graph[node_id]["inputs"]["lora"] = cfg["name"]
    """)
    plugins.load_all([tmp_path])
    assert compiler.check_manifest({**manifest, "lora_family": "v"}, graph) == []
    graph = compiler.inject_character_lora(graph, LORA)
    assert graph["1"]["inputs"]["lora"] == "anna.safetensors"
    assert not any(n["class_type"] == "LoraLoaderModelOnly"
                   for n in graph.values())
