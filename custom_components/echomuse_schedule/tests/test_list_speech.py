"""How the Echo reads out reminders, alarms and timers.

The same function lives in the companion (`dialogue.py`) and in the Pipecat
add-on (`schedule_dialogue.py`); every case runs through both and must agree.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.echomuse_schedule import dialogue as ha

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "pipecat_echomuse"))
import schedule_dialogue as pipecat   # noqa: E402

COPIES = [ha, pipecat]
ZONE = ZoneInfo("Europe/Warsaw")
NOW = int(datetime(2026, 10, 20, 12, 0, tzinfo=ZONE).timestamp())     # a Tuesday, noon


def at(day, hour, minute=0, month=10):
    return int(datetime(2026, month, day, hour, minute, tzinfo=ZONE).timestamp())


def reminder(message, due, state="scheduled", **over):
    return {"kind": "reminder", "label": message, "message": message, "due": due,
            "timezone": "Europe/Warsaw", "state": state, **over}


def say(kind, items, **kw):
    out = {c.__name__: c.list_speech(kind, items, NOW, **kw) for c in COPIES}
    assert len(set(out.values())) == 1, out
    return out[ha.__name__]


def test_a_days_reminders_read_in_time_order_without_date_or_zone():
    items = [reminder("collect brother from the airport", at(20, 22)),
             reminder("go to your aunt's house", at(20, 15, 30)),
             reminder("take out the recycling", at(20, 18))]
    assert say("reminder", items, day="today") == (
        "You have 3 reminders today. At 3:30 PM, go to your aunt's house. "
        "At 6 PM, take out the recycling. And at 10 PM, collect brother from the airport.")


def test_nothing_that_is_finished_is_read_out():
    items = [reminder("done already", at(20, 9), "completed"), reminder("cancelled", at(20, 10), "canceled"),
             reminder("take out the recycling", at(20, 18))]
    text = say("reminder", items, day="today")
    assert text == "You have one reminder today, at 6 PM: take out the recycling."
    for word in ("done already", "cancelled", "completed", "CEST", "scheduled"):
        assert word not in text


def test_only_finished_ones_means_none():
    assert say("reminder", [reminder("x", at(20, 9), "completed")], day="today") == \
        "You don't have any reminders set for today."


@pytest.mark.parametrize("day,said", [
    (None, "You don't have any reminders set."),
    ("tomorrow", "You don't have any reminders set for tomorrow."),
    ("on Monday", "You don't have any reminders set on Monday."),
    ("on the 24th", "You don't have any reminders set on the 24th."),
])
def test_none_says_which_day(day, said):
    assert say("reminder", [], day=day) == said


def test_with_no_day_each_one_says_when_it_is():
    items = [reminder("a", at(20, 18)), reminder("b", at(21, 9)), reminder("c", at(23, 7, 30)),
             reminder("d", at(30, 7))]
    assert say("reminder", items) == (
        "You have 4 reminders. At 6 PM today, a. At 9 AM tomorrow, b. At 7:30 AM on Friday, c. "
        "And at 7 AM on Friday 30 October, d.")


def test_a_week_away_is_dated_not_just_named():
    # Seven days on is "Tuesday" again, which would read as today.
    assert "on Tuesday 27 October" in say("reminder", [reminder("x", at(27, 9))])


def test_noon_and_midnight_are_said_as_words():
    text = say("reminder", [reminder("a", at(20, 12)), reminder("b", at(21, 0))])
    assert "At noon today, a." in text and "midnight tomorrow, b" in text


def test_one_reminder_unfiltered():
    assert say("reminder", [reminder("take out the recycling", at(21, 9))]) == \
        "You have one reminder, at 9 AM tomorrow: take out the recycling."


def test_next_reminder():
    items = [reminder("later", at(22, 9)), reminder("sooner", at(20, 18))]
    assert say("reminder", items, nxt=True) == "Your next reminder is at 6 PM today: sooner."


def test_the_day_is_judged_in_the_items_own_zone():
    ny = reminder("call", int(datetime(2026, 10, 20, 23, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()),
                  timezone="America/New_York")
    # 05:30 tomorrow in Warsaw, but 11:30 tonight where it was set.
    assert say("reminder", [ny]) == "You have one reminder, at 11:30 PM today: call."


def test_a_long_list_is_cut_short():
    items = [reminder(f"item {n}", at(20, 13 + n)) for n in range(8)]
    text = say("reminder", items, day="today")
    assert text.startswith("You have 8 reminders today.") and text.endswith("And 3 more.")
    assert "item 4" in text and "item 5" not in text


def test_repeating_reminders_and_alarms_name_their_days():
    rule = {"days": [1, 2, 3, 4, 5], "interval": 0, "hour": 7, "minute": 0}
    assert say("reminder", [reminder("stand up", 0, rule=rule)]) == \
        "You have one reminder, at 7 AM every weekday: stand up."
    alarm = {"kind": "alarm", "label": "alarm", "due": at(21, 7), "timezone": "Europe/Warsaw", "state": "scheduled"}
    gym = {"kind": "alarm", "label": "gym", "due": 0, "timezone": "Europe/Warsaw", "state": "disabled",
           "rule": {"days": [1, 3], "interval": 0, "hour": 6, "minute": 30}}
    assert say("alarm", [alarm]) == "You have one alarm, at 7 AM tomorrow."
    assert say("alarm", [alarm, gym]) == (
        "You have 2 alarms. At 7 AM tomorrow. And at 6:30 AM every Monday and Wednesday, gym, "
        "which is turned off.")
    every = reminder("drink water", 0, rule={"days": [], "interval": 7200, "hour": 0, "minute": 0})
    assert say("reminder", [every]) == "You have one reminder, every 2 hours: drink water."


def test_timers_say_what_is_left():
    tea = {"kind": "timer", "label": "tea", "due": NOW + 245, "state": "scheduled", "timezone": "UTC"}
    plain = {"kind": "timer", "label": "timer", "due": NOW + 3725, "state": "scheduled", "timezone": "UTC"}
    assert say("timer", [tea]) == "The tea timer has 4 minutes and 5 seconds left."
    assert say("timer", [tea, plain]) == (
        "You have 2 timers. The tea timer has 4 minutes and 5 seconds left. "
        "And a timer has 1 hour and 2 minutes left.")
    paused = {**tea, "state": "paused"}
    assert say("timer", [paused]) == "The tea timer is paused."
    assert say("timer", [{**tea, "state": "ringing"}]) == "The tea timer is ringing."
    assert say("timer", []) == "You don't have any timers set."


def test_a_missed_or_ringing_reminder_says_so():
    assert say("reminder", [reminder("x", at(20, 9), "missed")]) == \
        "You have one reminder, at 9 AM today: x, which you missed."
    assert "going off now" in say("reminder", [reminder("x", at(20, 11, 59), "ringing")])


def test_the_text_is_free_of_what_was_wrong_before():
    items = [reminder("a", at(20, 15, 30)), reminder("b", at(21, 9))]
    for day in (None, "today"):
        text = say("reminder", items, day=day)
        for word in ("CEST", "CET", "scheduled", "15:30", "Tuesday 20", "20 October"):
            assert word not in text


@pytest.mark.parametrize("raw,said", [("today", "today"), ("Tomorrow", "tomorrow"), ("monday", "on Monday"),
                                       (" SUNDAY ", "on Sunday"), ("the 24th", "on the 24th")])
def test_day_phrase(raw, said):
    assert {c.day_phrase(raw) for c in COPIES} == {said}


@pytest.mark.parametrize("hour,minute,said", [(0, 0, "midnight"), (0, 5, "12:05 AM"), (9, 0, "9 AM"),
                                              (12, 0, "noon"), (12, 30, "12:30 PM"), (23, 59, "11:59 PM")])
def test_clock_speech(hour, minute, said):
    assert {c.clock_speech(datetime(2026, 1, 1, hour, minute)) for c in COPIES} == {said}
