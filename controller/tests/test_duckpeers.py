"""Echoes that duck WITH another's turn: a ringing alarm, a speaker group, a
Sendspin group (em_duckpeers, em_player._peer_holds).

2026-11-05: a shared alarm rang on two Echoes, the nearer one ceded the wake
and its alarm came back up at full level into the far one's microphone; the
"stop" never reached speech-to-text. The rules pinned here are what stop
that, and what keep the duck from bouncing: every Echo's wire state moves
only when its answer changes, overlapping turns never release a duck another
still needs, and the set ducks in one go.
"""
import asyncio

import em_duckpeers as dp
import em_player
from em_duckpeers import Facts


# ── who ducks with whom ─────────────────────────────────────────────────────

def test_an_echo_with_an_alarm_ringing_ducks_with_any_turn():
    facts = {"a": Facts(), "b": Facts(ringing=True), "c": Facts()}
    assert dp.companions("a", facts, []) == {"b"}


def test_a_speaker_group_ducks_together():
    groups = [{"id": "g", "members": [{"device_id": "a"}, {"device_id": "b"}, {"device_id": "c"}]},
              {"id": "h", "members": [{"device_id": "d"}]}]
    facts = {k: Facts() for k in "abcd"}
    assert dp.companions("a", facts, groups) == {"b", "c"}
    assert dp.companions("d", facts, groups) == set()


def test_a_sendspin_group_ducks_together_only_on_the_same_server():
    tv = ("NAS streamer", "Living")
    facts = {"a": Facts(sendspin=tv), "b": Facts(sendspin=tv),
             "c": Facts(sendspin=("Music Assistant", "Living")), "d": Facts()}
    assert dp.companions("a", facts, []) == {"b"}
    assert dp.companions("d", facts, []) == set()


def test_offline_echoes_are_not_listed():
    groups = [{"id": "g", "members": [{"device_id": "a"}, {"device_id": "gone"}]}]
    assert dp.companions("a", {"a": Facts()}, groups) == set()


def test_an_echo_never_ducks_with_itself():
    groups = [{"id": "g", "members": [{"device_id": "a"}]}]
    facts = {"a": Facts(ringing=True, sendspin=("s", "g"))}
    assert dp.companions("a", facts, groups) == set()


def test_alarm_ringing_reads_the_schedule_snapshot():
    assert dp.alarm_ringing({"items": [{"state": "scheduled"}, {"state": "ringing"}]})
    assert not dp.alarm_ringing({"items": [{"state": "queued"}, {"state": "snoozed"}]})
    for bad in (None, {}, {"items": None}, {"items": ["x"]}):
        assert not dp.alarm_ringing(bad)


def test_sendspin_group_needs_a_server_a_group_and_membership():
    assert dp.sendspin_group({"state": "playing", "server": "S", "group": "G"}) == ("S", "G")
    assert dp.sendspin_group({"state": "connected", "server": "S", "group": "G"}) == ("S", "G")
    assert dp.sendspin_group({"state": "listening", "server": "S", "group": "G"}) is None
    assert dp.sendspin_group({"state": "playing", "server": "S"}) is None
    assert dp.sendspin_group({"state": "playing", "group": "G"}) is None
    assert dp.sendspin_group(None) is None


# ── the wire ────────────────────────────────────────────────────────────────

class Dev:
    def __init__(self, device_id, mix=True):
        self.device_id = device_id
        self.audio_mix_capable = mix
        self.msgs = []

    async def send_control(self, msg):
        self.msgs.append(msg)

    def ducks(self):
        return [m["on"] for m in self.msgs if m.get("type") == "duck"]


def wire(devs, companions):
    em_player._sessions.clear()
    em_player._peer_holds.clear()
    em_player.init(get_device=devs.get, notify_state=None)
    em_player.set_duck_companions(lambda d: companions.get(d, set()))


def teardown_function():
    em_player._sessions.clear()
    em_player._peer_holds.clear()
    em_player.set_duck_companions(None)


def test_a_turn_ducks_its_companions_and_releases_them_once():
    devs = {k: Dev(k) for k in "abc"}
    wire(devs, {"a": {"b", "c"}})

    async def main():
        await em_player.interrupt("a")
        assert [devs[k].ducks() for k in "abc"] == [[True], [True], [True]]
        await em_player.resume_interrupted("a")
    asyncio.run(main())
    assert [devs[k].ducks() for k in "abc"] == [[True, False]] * 3


def test_the_set_ducks_concurrently_not_one_round_trip_apart():
    """Every Echo starts its ramp in the same instant: no send waits on
    another's."""
    started = []

    class Slow(Dev):
        async def send_control(self, msg):
            started.append(self.device_id)
            await self.gate.wait()
            self.msgs.append(msg)

    async def main():
        gate = asyncio.Event()
        devs = {k: Slow(k) for k in "abc"}
        for d in devs.values():
            d.gate = gate
        wire(devs, {"a": {"b", "c"}})
        task = asyncio.ensure_future(em_player.interrupt("a"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert sorted(started) == ["a", "b", "c"], "all three sends must be in flight at once"
        gate.set()
        await task
    asyncio.run(main())


def test_a_nested_owner_does_not_reclaim_or_release_the_companions():
    devs = {k: Dev(k) for k in "ab"}
    wire(devs, {"a": {"b"}})

    async def main():
        await em_player.interrupt("a")            # the turn
        await em_player.interrupt("a")            # an announcement inside it
        await em_player.resume_interrupted("a")   # the announcement ends
        assert devs["b"].ducks() == [True], "b must stay down while a's turn goes on"
        await em_player.resume_interrupted("a")
    asyncio.run(main())
    assert devs["b"].ducks() == [True, False]


def test_overlapping_turns_on_grouped_echoes_never_bounce():
    """a and b in one group, both take turns that overlap. Each stays down
    until BOTH turns are over, and no Echo sees on/off/on."""
    devs = {k: Dev(k) for k in "ab"}
    wire(devs, {"a": {"b"}, "b": {"a"}})

    async def main():
        await em_player.interrupt("a")
        await em_player.interrupt("b")
        await em_player.resume_interrupted("a")
        assert devs["a"].ducks() == [True], "a is still in b's group turn"
        assert devs["b"].ducks() == [True], "b has its own turn"
        await em_player.resume_interrupted("b")
    asyncio.run(main())
    assert devs["a"].ducks() == [True, False]
    assert devs["b"].ducks() == [True, False]


def test_an_echo_with_its_own_turn_is_not_released_by_the_other_turn():
    devs = {k: Dev(k) for k in "ab"}
    wire(devs, {"a": {"b"}})

    async def main():
        await em_player.interrupt("b")            # b's own turn
        await em_player.interrupt("a")            # a's turn ducks b: already down
        await em_player.resume_interrupted("a")
        assert devs["b"].ducks() == [True], "a's turn ending must not undo b's own duck"
        await em_player.resume_interrupted("b")
    asyncio.run(main())
    assert devs["b"].ducks() == [True, False]


def test_a_companion_that_cannot_mix_is_left_alone():
    devs = {"a": Dev("a"), "b": Dev("b", mix=False)}
    wire(devs, {"a": {"b"}})

    async def main():
        await em_player.interrupt("a")
        await em_player.resume_interrupted("a")
    asyncio.run(main())
    assert devs["b"].msgs == [], "pausing another room is not a duck"


def test_the_holder_disconnecting_mid_turn_releases_its_companions():
    devs = {k: Dev(k) for k in "ab"}
    wire(devs, {"a": {"b"}})

    async def main():
        await em_player.interrupt("a")
        em_player.device_gone("a")
        for _ in range(5):
            await asyncio.sleep(0)
        await em_player.resume_interrupted("a")   # the turn's finally, after the session went
    asyncio.run(main())
    assert devs["b"].ducks() == [True, False], "released once, not stuck and not twice"


def test_ducks_with_answers_for_the_cede_before_the_turn_starts():
    devs = {k: Dev(k) for k in "abc"}
    wire(devs, {"a": {"b"}})
    assert em_player.ducks_with("a", "b")
    assert not em_player.ducks_with("a", "c")


def test_a_failing_companion_lookup_costs_nothing_but_the_peers():
    devs = {"a": Dev("a")}
    wire(devs, {})

    def boom(_):
        raise RuntimeError("corrupt group record")
    em_player.set_duck_companions(boom)

    async def main():
        await em_player.interrupt("a")
        await em_player.resume_interrupted("a")
    asyncio.run(main())
    assert devs["a"].ducks() == [True, False]


def test_without_companions_nothing_changes():
    devs = {"a": Dev("a")}
    wire(devs, {})
    em_player.set_duck_companions(None)

    async def main():
        await em_player.interrupt("a")
        await em_player.resume_interrupted("a")
    asyncio.run(main())
    assert devs["a"].ducks() == [True, False]


# ── the user's choice of who in a speaker group ducks together ──────────────

def test_only_the_members_chosen_to_duck_duck_together():
    groups = [{"id": "g", "members": [{"device_id": "a"}, {"device_id": "b", "duck": True},
                                      {"device_id": "c", "duck": False}]}]
    facts = {k: Facts() for k in "abc"}
    assert dp.companions("a", facts, groups) == {"b"}
    assert dp.companions("b", facts, groups) == {"a"}
    assert dp.companions("c", facts, groups) == set(), "one left out ducks nobody else"


def test_a_member_left_out_still_ducks_for_a_ringing_alarm():
    """The group choice is about the group's music; an alarm ringing
    anywhere still goes down for any command."""
    groups = [{"id": "g", "members": [{"device_id": "a", "duck": False}, {"device_id": "b"}]}]
    facts = {"a": Facts(), "b": Facts(ringing=True)}
    assert dp.companions("a", facts, groups) == {"b"}


def test_the_group_record_keeps_the_choice_and_defaults_it_on():
    import em_groups
    out = em_groups.normalize([{"name": "Downstairs", "members": [
        {"device_id": "a"}, {"device_id": "b", "duck": False}]}])
    assert [m["duck"] for m in out[0]["members"]] == [True, False]


def test_a_duck_that_is_not_a_boolean_is_refused_on_save():
    import em_groups
    import pytest
    with pytest.raises(ValueError, match="duck must be true or false"):
        em_groups.normalize([{"name": "G", "members": [{"device_id": "a", "duck": "no"}]}], strict=True)


def test_playing_here_counts_alarms_music_and_sendspin_not_only_a_reply():
    from types import SimpleNamespace as NS
    quiet = NS(speaking=False, timer_alarm_ringing=False, schedule=NS(snapshot=None), sendspin=None)
    assert not dp.playing_here(quiet, False)
    assert dp.playing_here(quiet, True)
    assert dp.playing_here(NS(**{**vars(quiet), "speaking": True}), False)
    assert dp.playing_here(NS(**{**vars(quiet), "schedule": NS(snapshot={"items": [{"state": "ringing"}]})}), False)
    assert dp.playing_here(NS(**{**vars(quiet), "sendspin": {"state": "playing"}}), False)


def test_facts_of_reads_live_devices():
    from types import SimpleNamespace as NS
    devs = [NS(device_id="a", timer_alarm_ringing=False, schedule=NS(snapshot={"items": [{"state": "ringing"}]}),
               sendspin={"state": "playing", "server": "S", "group": "G"}),
            NS(device_id="b", timer_alarm_ringing=False, schedule=NS(snapshot=None), sendspin=None)]
    assert dp.facts_of(devs) == {"a": Facts(True, ("S", "G")), "b": Facts(False, None)}
