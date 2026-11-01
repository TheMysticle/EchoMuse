"""The heard check, against what Gemini Live actually did in this house.

Every junk case is a call Gemini made, taken from the add-on's logs
(2026-10-26..28) or from replaying clips into gemini-3.1-flash-live-preview
with the add-on's own prompt and tools; every command case is one that must
keep working, chained commands and Polish included.
"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import heard_check as hc

NAMES = hc.Names(
    groups=[["Kitchen Master", "Kitchen Lights"], ["Kitchen WLED", "kitchen ambient"],
            ["Floor Lamp Floor Lamp", "Floor Lamp"], ["Bazzite PC", "Bazzite", "Living room PC", "Computer"],
            ["OLED", "TV"], ["Bedroom Lights", "bedroom lights"]],
    areas=[["Living Room"], ["Kitchen"], ["Bedroom"], ["Bathroom"]],
    scripts={
        "vacuum_clean_kitchen": ["Vacuum: Clean Kitchen", "Clean the kitchen", "vacuum kitchen", "vacuum the kitchen"],
        "vacuum_go_to_corridor": ["Vacuum: Go to Corridor"],
        "activate_movie_mode": ["Activate Movie Mode", "Turn on movie mode", "movie mode"],
        "tv_toggle_oled": ["Toggle TV Power", "Turn on tv", "Turn on the tv"],
    },
)

MCP = "assist__"

COMMANDS = [
    ("Turn on Kitchen Master", "intent__HassTurnOn", {"name": "Kitchen Master"}),
    ("Turn on the kitchen lights", "intent__HassTurnOn", {"name": "Kitchen Master"}),
    ("Turn off the floor lamp", "intent__HassTurnOff", {"name": "floor lamp"}),
    ("Set floor lamp to one percent brightness", "light__HassLightSet", {"brightness": 1, "name": "floor lamp"}),
    ("ustaw lampę stojącą na 10%", "light__HassLightSet", {"brightness": 10, "name": "lampa stojąca"}),
    ("Włącz lampę stojącą", "intent__HassTurnOn", {"name": "Standing Lamp"}),
    ("włącz komputer w salonie", "intent__HassTurnOn", {"area": "Living Room", "name": "Computer"}),
    ("Wyłącz światło w sypialni.", "intent__HassTurnOff", {"area": "sypialnia", "domain": ["light"]}),
    ("Włącz światło w kuchni", "intent__HassTurnOn", {"domain": ["light"], "area": "kitchen"}),
    ("Turn off the living room TV", "intent__HassTurnOff", {"device_class": ["tv"], "area": "Living Room"}),
    ("Vacuum the kitchen", "script__vacuum_clean_kitchen", {}),
    ("Odkurz kuchnię.", "script__vacuum_clean_kitchen", {}),
    ("Activate movie mode", "script__activate_movie_mode", {}),
    ("Set an alarm for twelve PM", "EchoMuseCreate", {"kind": "alarm"}),
    ("Nastaw budzik na siódmą", "EchoMuseCreate", {"kind": "alarm"}),
    ("Stop.", "EchoMuseStop", {}),
    ("Pause music.", "media_player__HassMediaPause", {}),
    ("Volume 20", "EchoMuseVolume", {"level": 20}),
]

CHAINS = [
    ("Turn off the bedroom lights and dim the floor lamp to ten percent, then turn on the kitchen WLED",
     [("intent__HassTurnOff", {"area": "Bedroom"}),
      ("light__HassLightSet", {"name": "floor lamp", "brightness": 10}),
      ("intent__HassTurnOn", {"name": "kitchen W LED"})]),
    ("Turn on movie mode and set the floor lamp to five percent",
     [("script__activate_movie_mode", {}), ("light__HassLightSet", {"name": "floor lamp", "brightness": 5})]),
    ("Wyłącz światło w sypialni, ustaw lampę stojącą na 20%",
     [("intent__HassTurnOff", {"area": "Bedroom"}), ("light__HassLightSet", {"name": "floor lamp", "brightness": 20})]),
    ("Turn off all the lights in the living room and turn on the computer",
     [("intent__HassTurnOff", {"area": "Living Room"}), ("intent__HassTurnOn", {"name": "computer"})]),
]

JUNK = [
    ("internal kitchen master", "script__vacuum_clean_kitchen", {}),
    ("Internal kitchen master", "intent__HassTurnOn", {"name": "Internal kitchen master"}),
    ("Komm du jetzt in Salon ja?", "script__vacuum_go_to_corridor", {}),
    ("Kommst du jetzt in Salon ja?", "intent__HassTurnOn", {"area": "Living_Room"}),
    ("Kommst du jetzt in Salon ja?", "light__HassLightSet", {"name": "Salon ja"}),
    ("Ciągle kupić, pomyślałam, że przestaniesz i to bardzo. Nie, nie, twoja.",
     "media_player__HassMediaPause", {"name": "TV"}),
    ("Ciągle kupić, pomyślałam, że przestaniesz i to bardzo. Nie, nie, twoja.",
     "intent__HassTurnOff", {"name": "TV"}),
    ("No, I'm not a photographer.", "EchoMuseCreate", {"kind": "reminder", "message": "buy dog food"}),
    ("eso", "EchoMuseStop", {}),
    ("eso", "media_player__HassMediaPause", {"area": "Living Room", "domain": ["media_player"]}),
    ("Virgil TV Paul", "media_player__HassMediaPause", {"device_class": ["tv"], "name": "Virgil TV"}),
    ("Tenh film v televizi byl v ogóle bez sensu. Wyłączyłabym go po 10 minutach.",
     "echomuse-local-schedules__EchoMuseCreate", {"kind": "timer", "when": "+10m", "target": "tv"}),
    ("Coming up after the break, why kitchen renovations are costing families more than ever.", "EchoMuseStop", {}),
    ("", "intent__HassTurnOn", {"name": "Kitchen Master"}),
]


@pytest.mark.parametrize("text,tool,args", COMMANDS)
def test_a_command_that_was_said_runs(text, tool, args):
    ok, why = hc.check(MCP + tool, args, text, NAMES)
    assert ok, why


@pytest.mark.parametrize("text,calls", CHAINS)
def test_every_call_of_a_chained_command_runs(text, calls):
    for tool, args in calls:
        ok, why = hc.check(MCP + tool, args, text, NAMES)
        assert ok, (tool, why)


@pytest.mark.parametrize("text,tool,args", JUNK)
def test_a_call_on_words_that_never_asked_for_it_is_refused(text, tool, args):
    ok, why = hc.check(MCP + tool, args, text, NAMES)
    assert not ok, why


def test_reads_are_never_refused():
    for tool in ("homeassistant__GetLiveContext", "llm__GetDateTime", "EchoMuseList", "script__get_weather"):
        assert hc.check(MCP + tool, {}, "pão de chuchu", NAMES)[0]


def test_a_request_made_to_someone_else_is_the_known_gap():
    # The words really are "clean the kitchen". Who they were said to is not in
    # the transcript; a confirmation for the vacuum is what would catch this.
    assert hc.check(MCP + "script__vacuum_clean_kitchen", {}, "Mamo, posprzątasz potem kuchnię?", NAMES)[0]


def test_a_clarification_answer_is_judged_with_its_question():
    s = hc.HeardState()
    s.user_text("Set an alarm for midday")
    s.bot_text_out("Is that for today?")
    s.user_text("Yes")
    assert hc.check("EchoMuseCreate", {"kind": "alarm"}, s.transcript(), NAMES)[0]


def test_a_new_turn_after_a_plain_reply_starts_from_nothing():
    s = hc.HeardState()
    s.user_text("Set an alarm for midday")
    s.bot_text_out("Alarm set.")
    s.user_text("No, I'm not a photographer.")
    assert not hc.check("EchoMuseCreate", {"kind": "alarm"}, s.transcript(), NAMES)[0]


def test_a_finished_model_turn_ends_the_user_turn_even_without_speech():
    s = hc.HeardState()
    s.user_text("Turn on Kitchen Master")
    s.bot_text_out("")  # turn_complete after a silent tool call
    s.user_text("eso")
    assert s.transcript() == "eso"


def test_transcript_chunks_of_one_turn_are_joined():
    s = hc.HeardState()
    for chunk in ("Turn off the ", "bedroom lights ", "and the floor lamp"):
        s.user_text(chunk)
    assert hc.check(MCP + "intent__HassTurnOff", {"name": "floor lamp"}, s.transcript(), NAMES)[0]


def test_script_aliases_come_from_the_tool_description():
    tools = [SimpleNamespace(name="assist__script__vacuum_clean_kitchen",
                             description="Home Assistant MCP: Aliases: ['Vacuum: Clean Kitchen', 'vacuum kitchen']"),
             SimpleNamespace(name="assist__script__odd", description="no aliases here"),
             SimpleNamespace(name="assist__intent__HassTurnOn", description="")]
    assert hc.script_names(tools) == {
        "vacuum_clean_kitchen": ["vacuum clean kitchen", "Vacuum: Clean Kitchen", "vacuum kitchen"],
        "odd": ["odd"],
    }


def test_live_context_keeps_each_entitys_aliases_together():
    yaml_text = ("Live Context: An overview of the areas and the devices in this smart home:\n"
                 "- names: Kitchen Master, Kitchen Lights\n  domain: light\n  state: 'on'\n  areas: Kitchen\n"
                 "- names: Bazzite PC\n  domain: switch\n  areas: Living Room, Salon\n")
    groups, areas = hc.live_context_groups(json.dumps({"result": yaml_text}))
    assert groups == [["Kitchen Master", "Kitchen Lights"], ["Bazzite PC"]]
    assert areas == [["Kitchen"], ["Living Room", "Salon"]]
    assert hc.live_context_groups("not json") == ([], [])


def _params(tool, args, sink):
    async def result_callback(result):
        sink.append(result)
    return SimpleNamespace(function_name=tool, arguments=args, result_callback=result_callback)


def test_the_gate_runs_a_heard_call_and_refuses_an_unheard_one():
    ran, results = [], []

    async def handler(params):
        ran.append(params.function_name)

    service = SimpleNamespace()
    gate = hc.gated(service, handler)
    hc.state_of(service).names = NAMES

    async def run():
        hc.state_of(service).user_text("internal kitchen master")
        await gate(_params(MCP + "script__vacuum_clean_kitchen", {}, results))
        await gate(_params(MCP + "homeassistant__GetLiveContext", {}, results))
        hc.state_of(service).bot_text_out("Sorry, I didn't catch that.")
        hc.state_of(service).user_text("Vacuum the kitchen")
        await gate(_params(MCP + "script__vacuum_clean_kitchen", {}, results))
    asyncio.run(run())
    assert ran == [MCP + "homeassistant__GetLiveContext", MCP + "script__vacuum_clean_kitchen"]
    assert len(results) == 1 and results[0]["error"] == "not_heard"


def test_the_gate_waits_briefly_for_a_transcript_still_on_its_way():
    ran = []

    async def handler(params):
        ran.append(True)

    service = SimpleNamespace()
    gate = hc.gated(service, handler)

    async def run():
        call = asyncio.create_task(gate(_params(MCP + "intent__HassTurnOff", {"name": "floor lamp"}, [])))
        await asyncio.sleep(0.1)
        hc.state_of(service).user_text("Turn off the floor lamp")
        await call
    asyncio.run(run())
    assert ran == [True]


def test_the_gate_can_be_switched_off(monkeypatch):
    async def handler(params):
        pass
    monkeypatch.setenv("ECHOMUSE_HEARD_CHECK", "off")
    assert hc.gated(SimpleNamespace(), handler) is handler


PLAY = MCP + "media_player__play_media"


@pytest.mark.parametrize("said,title", [
    # 2026-11-01 add-on log: all of these were refused.
    ("Play Twenty Ten hits", "2010 hits"),
    ("Play Twenty Tens hits", "2010s hits"),
    ("Czy trenował on jakieś szlachetne sztuki walki? Play twenty tens hits", "2010s hits"),
    ("Play Twenty Tens hits in the living room", "2010s hits"),
    ("Play Queen", "Queen"),
    ("Zagraj Queen", "Queen"),
    ("Puść coś spokojnego", "calm music"),
    ("Put on Queen", "Queen"),
    ("Play the second one", "spotify:playlist:37i9dQZF1DX"),
])
def test_a_play_request_reaches_the_spotify_search(said, title):
    ok, why = hc.check(PLAY, {"media_content_id": title, "media_content_type": "music"}, said, NAMES)
    assert ok, why


@pytest.mark.parametrize("said,title", [
    ("internal kitchen master", "2010s hits"),
    ("eso", "Queen"),
    ("Virgil TV Paul", "Paul Simon"),
    ("Komm du jetzt in Salon ja?", "Salon Jazz"),
    ("eso", "spotify:playlist:37i9dQZF1DX"),
    ("Ciągle kupić, pomyślałam, że przestaniesz i to bardzo.", "Queen"),
    ("Twenty Tens hits revista", "2010s hits"),
    ("", "Queen"),
])
def test_music_nobody_asked_for_is_refused(said, title):
    ok, why = hc.check(PLAY, {"media_content_id": title, "media_content_type": "music"}, said, NAMES)
    assert not ok, why


def test_an_empty_title_is_refused():
    assert not hc.check(PLAY, {"media_content_type": "music"}, "Play Queen", NAMES)[0]


@pytest.mark.parametrize("tool,args", [
    ("script__vacuum_clean_kitchen", {}),
    ("intent__HassTurnOff", {"name": "TV"}),
    ("media_player__HassMediaPause", {"name": "TV"}),
])
def test_a_play_word_is_evidence_for_play_media_alone(tool, args):
    assert not hc.check(MCP + tool, args, "Play some music", NAMES)[0]
