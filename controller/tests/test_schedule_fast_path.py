"""Local (no-LLM) EchoMuseCreate/List/Action handling in the pipecat fast path.

Drives a real FastPathState built from the actual custom_sentences grammar
(custom_sentences/en/echomuse_schedule.yaml) through a fake MCP bridge that
applies controller/em_workflow_tools.py's REAL command() validation/shape
against a tiny in-memory schedule store -- so the argument-shape adapter
(schedule_mcp.py) is exercised against the authoritative target schema, not
a guess at it.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
TZ = "Europe/Warsaw"


def load(filename, name, directory="tools/pipecat_echomuse"):
    spec = importlib.util.spec_from_file_location(name, ROOT/directory/filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


em_workflow_tools = load('em_workflow_tools.py', 'test_schedule_workflow_tools', directory='controller')


class FakeScheduler:
    """Mimics enough of the device's own schedule snapshot (em_schedule.py)
    for em_workflow_tools.command()'s output to be applied and read back."""

    def __init__(self):
        self.items = []
        self.active = None
        self.settings = {"snooze": 540}

    def snapshot(self):
        return {"items": self.items, "active": self.active, "settings": self.settings}

    def create(self, item):
        created = {**item, "id": item.get("id") or uuid.uuid4().hex, "state": "scheduled", "enabled": True}
        self.items.append(created)
        return created

    def apply_action(self, cmd):
        item = next((i for i in self.items if i["id"] == cmd["id"]), None)
        if item is None:
            raise ValueError("No matching item")
        item["state"] = {
            "cancel": "canceled", "dismiss": "completed", "pause": "paused",
            "resume": "scheduled", "enable": "scheduled", "disable": "disabled",
            "snooze": "snoozed",
        }.get(cmd["action"], item["state"])
        return item


class FakeBridge:
    def __init__(self, scheduler, now):
        self.scheduler = scheduler
        self.now = now
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        now_ts = int(self.now.timestamp())
        if name == "EchoMuseList":
            snapshot = self.scheduler.snapshot()
            if args.get("kind"):
                snapshot = {**snapshot, "items": [i for i in snapshot["items"] if i["kind"] == args["kind"]]}
            return json.dumps({"device_acknowledged": True, "now_utc": self.now.isoformat(),
                               "timezone": TZ, "snapshot": snapshot})
        if name == "EchoMuseCreate":
            cmd = em_workflow_tools.command(name, args, now_ts)
            self.scheduler.create(cmd["item"])
            return json.dumps({"device_acknowledged": True, "now_utc": self.now.isoformat(),
                               "timezone": TZ, "snapshot": self.scheduler.snapshot()})
        if name == "EchoMuseAction":
            cmd = em_workflow_tools.command(name, args, now_ts)
            self.scheduler.apply_action(cmd)
            return json.dumps({"device_acknowledged": True, "now_utc": self.now.isoformat(),
                               "timezone": TZ, "snapshot": self.scheduler.snapshot()})
        raise ValueError(f"Unknown scheduler tool {name}")


@pytest.fixture
def harness(monkeypatch):
    class Frame: pass
    class VADUserStartedSpeakingFrame(Frame): pass
    class LLMContextFrame(Frame):
        def __init__(self, messages):
            self.context = SimpleNamespace(get_messages=lambda: messages)
    class TTSSpeakFrame(Frame):
        def __init__(self, text): self.text = text
    class FrameProcessor:
        def __init__(self, **kwargs): self.sent = []
        async def process_frame(self, frame, direction): pass
        async def push_frame(self, frame, direction): self.sent.append((frame, direction))
    direction = SimpleNamespace(DOWNSTREAM='down', UPSTREAM='up')
    monkeypatch.setitem(sys.modules, 'pipecat.frames.frames', SimpleNamespace(
        Frame=Frame, LLMContextFrame=LLMContextFrame, TTSSpeakFrame=TTSSpeakFrame,
        VADUserStartedSpeakingFrame=VADUserStartedSpeakingFrame))
    monkeypatch.setitem(sys.modules, 'pipecat.processors.frame_processor', SimpleNamespace(
        FrameDirection=direction, FrameProcessor=FrameProcessor))
    fast_path = load('fast_path.py', 'test_schedule_fast_path_module')
    intents, report = fast_path.load_intents('en', [ROOT/'custom_sentences'])
    state = fast_path.FastPathState(intents=intents, report=report)
    # Built-in HA templates reference {name}/{area}/{floor}; an empty list is
    # "no match" to hassil, while no list at all is a MissingListError on
    # every sentence -- see FastPathState.set_slot_lists's own docstring.
    state.set_slot_lists([], [])

    now = datetime(2026, 10, 18, 12, 0, tzinfo=ZoneInfo(TZ))  # a Sunday
    scheduler = FakeScheduler()
    bridge = FakeBridge(scheduler, now)
    protocol = SimpleNamespace(continuation_requested=False)
    step = fast_path.FastPathStep(
        state=state, language='en', bridge=bridge, tool_names={},
        protocol=protocol, timezone=TZ,
    )

    messages = []

    async def say(text):
        # A real session's context accumulates across turns; each call here
        # is a fresh wake/VAD start followed by one more user message, the
        # same shape test_local_commands.py's own multi-turn test uses.
        start = VADUserStartedSpeakingFrame()
        await step.process_frame(start, direction.DOWNSTREAM)
        messages.append({'role': 'user', 'content': text})
        frame = LLMContextFrame(list(messages))
        await step.process_frame(frame, direction.DOWNSTREAM)
        last = step.sent[-1][0] if step.sent else None
        return getattr(last, 'text', None)

    return SimpleNamespace(step=step, bridge=bridge, scheduler=scheduler, protocol=protocol,
                           say=say, direction=direction)


def test_bare_timer_asks_for_duration_then_creates(harness):
    # "start a timer" is ambiguous in the real grammar -- hassil matches the
    # em_request wildcard template with request="a" rather than the bare
    # one. "start timer" matches the bare template cleanly (kind=timer, no
    # other slots), the case this test means to cover.
    reply = asyncio.run(harness.say("start timer"))
    assert reply == "For how long? Say, for example, five minutes"
    assert harness.bridge.calls == []
    assert harness.protocol.continuation_requested
    harness.protocol.continuation_requested = False
    reply = asyncio.run(harness.say("ten minutes"))
    assert "10 minutes" in reply
    assert harness.bridge.calls == [("EchoMuseCreate", {"kind": "timer", "timezone": TZ, "duration_seconds": 600})]


def test_explicit_duration_timer_creates_with_no_clarification(harness):
    # "set a 10 minute timer" matches HA's own built-in HassStartTimer (more
    # specific than our wildcard template) and is HA's local agent's job, not
    # ours; "a 10 minute timer" matches EchoMuseCreate.
    reply = asyncio.run(harness.say("a 10 minute timer"))
    assert reply == "Your timer is set for 10 minutes."
    assert harness.bridge.calls == [("EchoMuseCreate", {"kind": "timer", "timezone": TZ, "duration_seconds": 600})]
    assert not harness.protocol.continuation_requested


def test_reminder_with_explicit_time_creates_directly(harness):
    reply = asyncio.run(harness.say("remind me to call mum tomorrow at 8am"))
    assert "call mum" in reply
    assert len(harness.bridge.calls) == 1
    name, args = harness.bridge.calls[0]
    assert name == "EchoMuseCreate"
    assert args["kind"] == "reminder" and args["message"] == "call mum" and args["timezone"] == TZ
    due = datetime.fromisoformat(args["when"])
    assert due.astimezone(ZoneInfo(TZ)).hour == 8
    assert due.date() == (harness.bridge.now + harness.bridge.now.resolution).date() or due.day != harness.bridge.now.day


def test_ambiguous_meridian_asks_then_completes(harness):
    reply = asyncio.run(harness.say("remind me to call mum tomorrow at 8"))
    assert reply == "Did you mean AM or PM?"
    reply = asyncio.run(harness.say("pm"))
    assert "call mum" in reply
    name, args = harness.bridge.calls[0]
    due = datetime.fromisoformat(args["when"])
    assert due.astimezone(ZoneInfo(TZ)).hour == 20


def test_bare_remind_me_asks_time_then_message_then_creates(harness):
    assert asyncio.run(harness.say("remind me")) == "What time? Say, for example, 8am"
    assert asyncio.run(harness.say("8am")) == "What should I remind you to do?"
    reply = asyncio.run(harness.say("to call mum"))
    assert "call mum" in reply
    assert harness.bridge.calls and harness.bridge.calls[0][0] == "EchoMuseCreate"


@pytest.mark.parametrize("cancel_word", ["cancel", "cancel it", "never mind", "stop"])
def test_cancel_word_aborts_a_pending_clarification(harness, cancel_word):
    assert asyncio.run(harness.say("start timer")) == "For how long? Say, for example, five minutes"
    reply = asyncio.run(harness.say(cancel_word))
    assert reply == "Okay, I haven't set the timer."
    assert harness.bridge.calls == []
    assert harness.step._pending_schedule is None


def test_unresolvable_clarification_gives_up_after_four_tries(harness):
    assert asyncio.run(harness.say("start timer")) == "For how long? Say, for example, five minutes"
    for _ in range(3):
        reply = asyncio.run(harness.say("a dog"))
        assert reply == "For how long? Say, for example, five minutes"
    reply = asyncio.run(harness.say("a dog"))
    assert reply == "I haven't set the timer. Please try again with a duration."
    assert harness.bridge.calls == []
    assert harness.step._pending_schedule is None


def test_listing_alarms_reads_the_scheduler_with_no_tool_call_to_the_llm(harness):
    harness.scheduler.items.append({
        "id": "a1", "kind": "alarm", "label": "alarm", "state": "scheduled",
        "due": int(datetime(2026, 10, 19, 7, 0, tzinfo=ZoneInfo(TZ)).timestamp()), "timezone": TZ,
    })
    reply = asyncio.run(harness.say("what alarms do I have"))
    assert "scheduled" in reply
    assert harness.bridge.calls == [("EchoMuseList", {"kind": "alarm"})]


def test_no_alarms_reports_none_rather_than_an_empty_string(harness):
    reply = asyncio.run(harness.say("what alarms do I have"))
    assert reply == "You don't have any alarms set."


def test_stop_ringing_dismisses_the_single_match_and_acts(harness):
    # "cancel the timer"/"cancel timer" match HA's own built-in
    # HassCancelTimer, not our EchoMuseAction -- "stop ringing" is one of
    # the EchoMuseAction sentences the grammar does not also give HA a more
    # specific built-in match for.
    asyncio.run(harness.say("a 10 minute timer"))
    harness.bridge.calls.clear()
    reply = asyncio.run(harness.say("stop ringing"))
    assert reply == "Timer stopped."
    # The list call resolving the active item/kind comes first, the action
    # itself last.
    assert harness.bridge.calls[-1][0] == "EchoMuseAction"
    assert harness.bridge.calls[-1][1]["action"] == "dismiss"
    assert harness.scheduler.items[0]["state"] == "completed"


def test_cancel_all_has_no_mcp_equivalent_and_falls_through_to_the_llm(harness):
    frame = sys.modules['pipecat.frames.frames'].LLMContextFrame(
        [{'role': 'user', 'content': 'cancel all my timers'}])
    asyncio.run(harness.step.process_frame(frame, harness.direction.DOWNSTREAM))
    assert harness.bridge.calls == []
    # Absorbed by neither the local scheduler nor HA's local agent -- passed
    # through unchanged for the LLM's own (equally limited) tool to try.
    assert harness.step.sent == [(frame, harness.direction.DOWNSTREAM)]
