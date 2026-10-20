"""EchoMuseAction `edit`: moving and rewording a reminder or alarm by voice.

Written from what the device does with an `edit` (device/internal/schedule): it
swaps the whole item out, keeps state only if due/duration/zone/rule are
unchanged, and otherwise resets the item to scheduled, finished ones included.
"""
from datetime import datetime, timezone

import pytest

import em_workflow_tools as tools

from test_schedule_auth import database  # noqa: F401  (fixture)

NOW = int(datetime(2026, 10, 20, 10, tzinfo=timezone.utc).timestamp())
TOMORROW_9 = "2026-10-21T09:00:00+02:00"           # Europe/Warsaw is +02:00 until 25 Oct


def reminder(**over):
    item = {"id": "r1", "kind": "reminder", "label": "take out the bins", "message": "take out the bins",
            "due": NOW + 3600, "duration": 0, "timezone": "Europe/Warsaw", "state": "scheduled",
            "enabled": True, "speech": "abc", "speech_version": "v1"}
    item.update(over)
    return item


def edit(items, **args):
    return tools.command("EchoMuseAction", {"action": "edit", "id": "r1", **args}, NOW, items)


def test_moving_a_reminder_sets_only_its_deadline():
    cmd = edit([reminder()], when=TOMORROW_9)
    assert cmd["action"] == "edit" and cmd["id"] == "r1" and cmd["request"]
    assert cmd["item"]["due"] == int(datetime.fromisoformat(TOMORROW_9).timestamp())
    # The words and their cached audio are untouched by a move.
    assert cmd["item"]["message"] == "take out the bins" and cmd["item"]["speech"] == "abc"


def test_rewording_keeps_the_deadline_and_drops_the_cached_audio():
    cmd = edit([reminder()], message="feed the cat")
    item = cmd["item"]
    assert item["due"] == NOW + 3600                # unchanged, so the device keeps its state
    assert item["message"] == "feed the cat" and item["label"] == "feed the cat"
    assert "speech" not in item and "speech_version" not in item


def test_a_hand_named_label_survives_rewording():
    item = edit([reminder(label="Mum")], message="feed the cat")["item"]
    assert item["label"] == "Mum" and item["message"] == "feed the cat"


def test_the_command_does_not_mutate_the_snapshot():
    items = [reminder()]
    edit(items, message="feed the cat", when=TOMORROW_9)
    assert items == [reminder()]


def test_a_repeating_alarm_takes_the_new_clock_time_and_keeps_its_days():
    alarm = {"id": "r1", "kind": "alarm", "label": "alarm", "due": NOW + 100, "timezone": "Europe/Warsaw",
             "state": "scheduled", "rule": {"days": [1, 2, 3, 4, 5], "interval": 0, "hour": 7, "minute": 0}}
    item = edit([alarm], when="2026-10-21T08:30:00+02:00")["item"]
    assert item["rule"] == {"days": [1, 2, 3, 4, 5], "interval": 0, "hour": 8, "minute": 30}
    assert item["due"] == 0                          # the device computes the next occurrence


def test_the_clock_is_read_in_the_items_own_zone_not_the_offset_given():
    alarm = {"id": "r1", "kind": "alarm", "label": "alarm", "due": NOW + 100, "timezone": "Europe/Warsaw",
             "state": "scheduled", "rule": {"days": [0], "interval": 0, "hour": 7, "minute": 0}}
    # 07:30 UTC is 09:30 in Warsaw.
    assert edit([alarm], when="2026-10-21T07:30:00Z")["item"]["rule"]["hour"] == 9


@pytest.mark.parametrize("state", ["completed", "canceled", "missed"])
def test_a_finished_item_is_not_brought_back(state):
    with pytest.raises(ValueError, match="already finished"):
        edit([reminder(state=state)], when=TOMORROW_9)


@pytest.mark.parametrize("args,match", [
    ({}, "Say what to change"),
    ({"message": "   "}, "Say what to change"),
    ({"when": "2026-10-21T09:00:00"}, "explicit UTC offset"),
    ({"when": "tomorrow at nine"}, "RFC3339"),
    ({"when": "2026-10-19T09:00:00+02:00"}, "future"),
])
def test_bad_edits_are_refused_with_a_sentence(args, match):
    with pytest.raises(ValueError, match=match):
        edit([reminder()], **args)


def test_unknown_timer_shared_and_interval_items_are_refused():
    with pytest.raises(ValueError, match="No item"):
        edit([], when=TOMORROW_9)
    with pytest.raises(ValueError, match="adjust"):
        edit([reminder(kind="timer")], when=TOMORROW_9)
    shared = "sh-0123abcd-" + "0" * 24
    with pytest.raises(ValueError, match="shared"):
        tools.command("EchoMuseAction", {"action": "edit", "id": shared, "when": TOMORROW_9}, NOW,
                      [reminder(id=shared)])
    every = reminder(rule={"days": [], "interval": 600, "hour": 0, "minute": 0})
    with pytest.raises(ValueError, match="interval"):
        edit([every], when=TOMORROW_9)


def test_an_alarm_has_no_message_to_reword():
    with pytest.raises(ValueError, match="Only a reminder"):
        edit([reminder(kind="alarm")], message="x")


def test_message_and_when_are_refused_on_every_other_action():
    for key, value in (("message", "x"), ("when", TOMORROW_9)):
        with pytest.raises(ValueError, match="only for the edit"):
            tools.command("EchoMuseAction", {"action": "cancel", "id": "r1", key: value}, NOW)


def test_the_other_actions_are_unchanged():
    cmd = tools.command("EchoMuseAction", {"action": "snooze", "id": "r1", "seconds": 300}, NOW)
    assert cmd["action"] == "snooze" and cmd["id"] == "r1" and cmd["seconds"] == 300 and "item" not in cmd


def test_over_mcp_list_hides_finished_and_edit_round_trips(database):
    """The model's path end to end: list, edit, and the refusals as tool errors."""
    import asyncio
    import json
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    from aiohttp.test_utils import TestClient, TestServer

    import em_api as api
    import em_ingressauth
    import em_state
    import em_schedule_auth as keys
    from em_schedule import Bridge
    import test_schedule_auth as fixtures

    async def main():
        now = int(time.time())
        bridge = Bridge('A')
        bridge.update({'owner': 'A', 'revision': 1, 'items': [
            reminder(id='live', due=now + 7200),
            reminder(id='done', state='completed', message='finished one', label='finished one'),
            reminder(id='gone', state='canceled', message='cancelled one', label='cancelled one'),
            reminder(id='late', state='missed', message='missed one', label='missed one')]})
        sent = []

        async def send(message):
            cmd = message['command']
            sent.append(cmd)
            items = bridge.snapshot['items']
            if cmd['action'] == 'edit':
                items = [cmd['item'] if i['id'] == cmd['id'] else i for i in items]
            bridge.receive({'id': message['id'], 'snapshot': {'owner': 'A', 'revision': bridge.snapshot['revision'] + 1, 'items': items}})

        live = SimpleNamespace(capabilities=['local_schedule'], schedule=bridge, send_control=send,
                               workflow=SimpleNamespace(active=True, config={'local_schedules': True}))
        key = keys.issue('A', voice=True)
        with patch.object(em_state, 'devices', {'A': live}), patch.object(em_ingressauth, 'INGRESS_ONLY', True), \
                patch.object(em_ingressauth, 'INGRESS_GATEWAY_IP', '172.30.32.2'):
            async with TestClient(TestServer(await api.create_app())) as client:
                async def call(name, arguments):
                    response = await client.post('/api/devices/A/voice-workflow/mcp', headers={'Authorization': 'Bearer ' + key},
                                                 json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                       'params': {'name': name, 'arguments': arguments}})
                    result = (await response.json())['result']
                    text = result['content'][0]['text']
                    return result['isError'], (json.loads(text) if not result['isError'] else text)

                error, listed = await call('EchoMuseList', {'kind': 'reminder'})
                assert not error
                assert [i['id'] for i in listed['snapshot']['items']] == ['live', 'late']   # missed stays, it is news

                when = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
                error, edited = await call('EchoMuseAction', {'action': 'edit', 'id': 'live', 'when': when, 'message': 'feed the cat'})
                assert not error and edited['device_acknowledged']
                assert [c['action'] for c in sent] == ['list', 'list', 'edit']        # list, the edit's own read, edit
                assert sent[-1]['item']['message'] == 'feed the cat' and sent[-1]['item']['due'] == now + 86400
                assert 'speech' not in sent[-1]['item']
                # The reply to the model carries no cached-audio fields.
                assert all('speech' not in i for i in edited['snapshot']['items'])

                error, text = await call('EchoMuseAction', {'action': 'edit', 'id': 'done', 'when': when})
                assert error and 'already finished' in text
                error, text = await call('EchoMuseAction', {'action': 'edit', 'id': 'live', 'when': '2020-01-01T09:00:00+00:00'})
                assert error and 'future' in text
                assert [c['action'] for c in sent].count('edit') == 1                  # nothing refused reached the device
    asyncio.run(main())


# The household's zone wins over the one a model names.

def create(args, now=NOW):
    return tools.command("EchoMuseCreate", {"kind": "reminder", "message": "x", **args}, now)["item"]


def test_a_model_that_says_utc_still_saves_in_the_households_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    item = create({"timezone": "UTC", "when": "2026-10-21T20:00:00+00:00"})
    assert item["timezone"] == "Europe/Warsaw"
    assert item["due"] == int(datetime(2026, 10, 21, 20, tzinfo=timezone.utc).timestamp())    # the instant is untouched


def test_a_repeat_is_read_on_the_households_clock(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    item = create({"kind": "alarm", "timezone": "UTC", "when": "2026-10-21T06:00:00+00:00", "repeat_days": [1]})
    assert item["rule"]["hour"] == 8 and item["timezone"] == "Europe/Warsaw"     # 06:00 UTC is 08:00 there


def test_without_a_known_zone_the_models_is_used_and_checked_as_before(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert create({"timezone": "Europe/Warsaw", "when": "2026-10-21T09:00:00+02:00"})["timezone"] == "Europe/Warsaw"
    with pytest.raises(ValueError, match="offset"):
        create({"timezone": "Europe/Warsaw", "when": "2026-10-21T09:00:00+00:00"})


def test_an_unusable_tz_is_ignored(monkeypatch):
    monkeypatch.setenv("TZ", "Not/AZone")
    assert tools.local_zone() is None
    monkeypatch.setenv("TZ", "")
    assert tools.local_zone() is None


def test_moving_an_item_saved_in_utc_brings_it_into_the_households_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    item = edit([reminder(timezone="UTC")], when=TOMORROW_9)["item"]
    assert item["timezone"] == "Europe/Warsaw"
    assert edit([reminder(timezone="UTC")], message="y")["item"]["timezone"] == "UTC"      # a reword is not a move
