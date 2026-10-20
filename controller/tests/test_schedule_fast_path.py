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
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import time

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
            # The speaker's own handler leaves finished items out of a list.
            snapshot = {**snapshot, "items": [i for i in snapshot["items"] if i["state"] not in ("completed", "canceled")]}
            if args.get("kind"):
                snapshot = {**snapshot, "items": [i for i in snapshot["items"] if i["kind"] == args["kind"]]}
            if args.get("date"):
                # The step resolves days against the real clock, so the fake must too.
                snapshot = {**snapshot, "items": em_workflow_tools.day_view(snapshot["items"], args["date"], int(time.time()))}
            return json.dumps({"device_acknowledged": True, "now_utc": self.now.isoformat(),
                               "timezone": TZ, "snapshot": snapshot})
        if name == "EchoMuseCreate":
            cmd = em_workflow_tools.command(name, args, now_ts)
            self.scheduler.create(cmd["item"])
            return json.dumps({"device_acknowledged": True, "now_utc": self.now.isoformat(),
                               "timezone": TZ, "snapshot": self.scheduler.snapshot()})
        if name == "EchoMuseAction":
            cmd = em_workflow_tools.command(name, args, now_ts, self.scheduler.items)
            if cmd["action"] == "edit":
                self.scheduler.items[:] = [cmd["item"] if i["id"] == cmd["id"] else i for i in self.scheduler.items]
            else:
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
    assert "7 AM" in reply and "scheduled" not in reply and "CEST" not in reply
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


def _reminders_on_three_days(harness):
    zone = ZoneInfo(TZ)
    today = datetime.now(zone).replace(hour=23, minute=0, second=0, microsecond=0)
    for name, days in (("alpha", 0), ("bravo", 1), ("charlie", 3)):
        harness.scheduler.items.append({
            "id": name, "kind": "reminder", "label": name, "message": name, "state": "scheduled",
            "due": int((today + timedelta(days=days)).timestamp()), "timezone": TZ})
    return today


def test_reminders_for_today_asks_the_speaker_for_today_only(harness):
    _reminders_on_three_days(harness)
    reply = asyncio.run(harness.say("what are my reminders for today"))
    assert harness.bridge.calls == [("EchoMuseList", {"kind": "reminder", "date": "today"})]
    assert "alpha" in reply and "bravo" not in reply and "charlie" not in reply


def test_reminders_for_tomorrow(harness):
    _reminders_on_three_days(harness)
    reply = asyncio.run(harness.say("do I have any reminders for tomorrow"))
    assert harness.bridge.calls == [("EchoMuseList", {"kind": "reminder", "date": "tomorrow"})]
    assert "bravo" in reply and "alpha" not in reply and "charlie" not in reply


def test_reminders_for_a_weekday_are_resolved_to_a_date(harness):
    today = _reminders_on_three_days(harness)
    third = today + timedelta(days=3)
    reply = asyncio.run(harness.say("what reminders do I have on " + third.strftime("%A").lower()))
    assert harness.bridge.calls == [("EchoMuseList", {"kind": "reminder", "date": third.date().isoformat()})]
    assert "charlie" in reply and "alpha" not in reply and "bravo" not in reply


def test_a_day_with_no_reminders_is_said_to_be_empty(harness):
    harness.scheduler.items.clear()
    reply = asyncio.run(harness.say("what are my reminders for tomorrow"))
    assert reply == "You don't have any reminders set for tomorrow."


def test_reminders_without_a_day_still_list_everything(harness):
    _reminders_on_three_days(harness)
    reply = asyncio.run(harness.say("what are my reminders"))
    assert harness.bridge.calls == [("EchoMuseList", {"kind": "reminder"})]
    assert all(name in reply for name in ("alpha", "bravo", "charlie"))


def test_resolve_day():
    lang = load('schedule_language.py', 'test_schedule_language_resolve')
    fri = datetime(2026, 10, 16, 8, tzinfo=ZoneInfo(TZ))
    r = lambda text: lang.resolve_day(text, fri)
    assert (r("today"), r("tomorrow")) == ("today", "tomorrow")
    assert r("friday") == "2026-10-16"      # today counts
    assert r("saturday") == "2026-10-17"
    assert r("thursday") == "2026-10-22"    # wraps into next week
    assert r("the 24th") == "2026-10-24"
    assert r("the twenty fourth") == "2026-10-24"
    assert r("the 3rd") == "2026-11-03"     # already past this month
    assert r("the 31st") == "2026-10-31"
    assert r("someday") is None


def _add_reminder(harness, name, days=2, hour=23, **over):
    zone = ZoneInfo(TZ)
    due = datetime.now(zone).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
    harness.scheduler.items.append({"id": name, "kind": "reminder", "label": name, "message": name,
                                    "state": "scheduled", "timezone": TZ, "due": int(due.timestamp()), **over})
    return due


def _edits(harness):
    return [c for c in harness.bridge.calls if c[0] == "EchoMuseAction"]


@pytest.mark.parametrize("phrase,hour,said", [
    ("move my reminder to 5pm tomorrow", 17, "Okay, your reminder is now at 5 PM tomorrow."),
    ("reschedule my reminder to tomorrow at 9am", 9, "Okay, your reminder is now at 9 AM tomorrow."),
    ("move the dentist reminder to half past six in the evening tomorrow", 18, "Okay, your reminder is now at 6:30 PM tomorrow."),
])
def test_moving_a_reminder_by_voice(harness, phrase, hour, said):
    _add_reminder(harness, "dentist", days=3)
    reply = asyncio.run(harness.say(phrase))
    assert reply == said
    calls = _edits(harness)
    assert len(calls) == 1 and calls[0][1]["action"] == "edit" and calls[0][1]["id"] == "dentist"
    moved = datetime.fromisoformat(calls[0][1]["when"]).astimezone(ZoneInfo(TZ))
    assert moved.hour == hour
    stored = harness.scheduler.items[0]
    assert stored["message"] == "dentist" and stored["due"] == int(moved.timestamp())


def test_a_named_reminder_among_several_is_the_one_moved(harness):
    _add_reminder(harness, "dentist", days=3)
    _add_reminder(harness, "bins", days=4)
    reply = asyncio.run(harness.say("move the bins reminder to 8am tomorrow"))
    assert reply == "Okay, your reminder is now at 8 AM tomorrow."
    assert _edits(harness)[0][1]["id"] == "bins"
    assert harness.scheduler.items[0]["due"] != harness.scheduler.items[1]["due"]


def test_several_reminders_and_no_name_asks_rather_than_guessing(harness):
    _add_reminder(harness, "dentist", days=3)
    _add_reminder(harness, "bins", days=4)
    reply = asyncio.run(harness.say("move my reminder to 5pm tomorrow"))
    assert "Several items match" in reply and _edits(harness) == []


def test_moving_the_next_reminder(harness):
    _add_reminder(harness, "later", days=5)
    _add_reminder(harness, "sooner", days=2)
    asyncio.run(harness.say("move my next reminder to 6pm tomorrow"))
    assert _edits(harness)[0][1]["id"] == "sooner"


def test_a_clock_without_am_or_pm_is_left_to_the_llm(harness):
    _add_reminder(harness, "dentist")
    frame = sys.modules['pipecat.frames.frames'].LLMContextFrame(
        [{'role': 'user', 'content': 'move my reminder to 8'}])
    asyncio.run(harness.step.process_frame(frame, harness.direction.DOWNSTREAM))
    assert _edits(harness) == []
    assert harness.step.sent[-1] == (frame, harness.direction.DOWNSTREAM)


def test_a_finished_reminder_cannot_be_moved(harness):
    _add_reminder(harness, "old", state="completed")
    reply = asyncio.run(harness.say("move my reminder to 5pm tomorrow"))
    assert reply == "No matching item" and _edits(harness) == []


def test_a_repeating_alarm_keeps_its_days_and_takes_the_new_time(harness):
    harness.scheduler.items.append({"id": "wake", "kind": "alarm", "label": "alarm", "state": "scheduled", "timezone": TZ,
                                    "due": int(datetime.now(ZoneInfo(TZ)).timestamp()) + 5000,
                                    "rule": {"days": [1, 2, 3, 4, 5], "interval": 0, "hour": 7, "minute": 0}})
    reply = asyncio.run(harness.say("move my alarm to 8am"))
    assert reply == "Okay, your alarm is now at 8 AM every weekday."
    assert harness.scheduler.items[0]["rule"] == {"days": [1, 2, 3, 4, 5], "interval": 0, "hour": 8, "minute": 0}


def test_a_repeat_is_not_a_place_to_move_to(harness):
    _add_reminder(harness, "dentist")
    reply = asyncio.run(harness.say("move my reminder to every weekday at 7am"))
    assert reply == "I can only move it to one time, not a repeat" and _edits(harness) == []
