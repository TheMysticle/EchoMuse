"""
em_duckpeers.py — which OTHER Echoes duck with one that is taking a turn
=========================================================================

A turn ducks the music and alarms on the Echo that took it (em_player.
interrupt). Until 2026-11-05 that was the only one, and three things were
wrong with that, each heard rather than reasoned about:

- **A shared alarm rings on several Echoes at once** (em_schedule_share), and
  only the one that took the wake turned it down. The others rang on at full
  level into the microphone that was listening for "stop", and their alarm is
  not in that Echo's echo canceller. Measured 2026-11-05 12:00: Lounge took
  the wake, Living Room — nearer the speaker — ceded and its alarm came back
  up within ~250ms; the first "stop" never reached speech-to-text, the second,
  spoken 10dB louder, did.
- **A speaker group plays one stream on several Echoes** (em_groups), so
  ducking one leaves the room as loud as it was.
- **A Sendspin group does the same** (Music Assistant, or the household's own
  streamer, syncing several players): same server, same group name.

So the Echoes that duck with a turn on X are: X's speaker group, X's Sendspin
group, and every Echo with an alarm or timer ringing. Alarms are fleet-wide
for the reason em_controller._hold_alarms is: a wake heard while an alarm
rings is almost always about the alarm, and the Echo that took it need not be
the one ringing (arbitration picks by level, not by which is making noise).

Only the duck is shared. Nothing here touches what a group plays or when:
the duck is the gain message the device already handles, applied in its own
write loop with its own ramp, so the synchronised stream is untouched and
nothing is added to the Sendspin path.

Pure: no I/O, no controller imports. Tested in tests/test_duckpeers.py.
"""
from __future__ import annotations

from typing import NamedTuple

# Schedule item states that are making sound now. "queued" is waiting behind
# another and is silent.
SOUNDING = ("ringing",)

# Sendspin player states in which the player belongs to a server's group.
_SENDSPIN_IN_GROUP = ("connected", "playing")


class Facts(NamedTuple):
    # An alarm, timer or reminder sounding on this Echo now.
    ringing: bool = False
    # (server, group) of the Sendspin group this Echo is in, else None.
    sendspin: tuple | None = None


def alarm_ringing(snapshot) -> bool:
    """Whether a device's schedule snapshot (em_schedule.Bridge) has anything
    sounding. A missing or malformed snapshot is not ringing."""
    if not isinstance(snapshot, dict):
        return False
    items = snapshot.get("items")
    if not isinstance(items, list):
        return False
    return any(isinstance(i, dict) and i.get("state") in SOUNDING for i in items)


def sendspin_group(status) -> tuple | None:
    """(server, group) for a Sendspin status in a group; None otherwise.

    Both must be present: a group name alone could be shared by two servers'
    unrelated groups, and a player that is only listening is in no group."""
    if not isinstance(status, dict) or status.get("state") not in _SENDSPIN_IN_GROUP:
        return None
    server, group = status.get("server"), status.get("group")
    if not isinstance(server, str) or not server or not isinstance(group, str) or not group:
        return None
    return (server, group)


def sendspin_playing(status) -> bool:
    return isinstance(status, dict) and status.get("state") == "playing"


def speaker_group_of(device_id: str, groups: list) -> set:
    """The Echoes in device_id's speaker group that duck together, itself
    included; empty if it is in none or is not one of them.

    Which members duck together is the user's choice per group (`duck` on
    each member, em_groups, default on): a group can span a kitchen and a
    lounge where a command in one should leave the other's music alone. A
    turn on a member that does not duck with its group ducks nobody else in
    it, and is ducked by nobody in it."""
    for g in groups or ():
        members = [m for m in g.get("members", ()) if isinstance(m, dict)]
        if device_id not in {m.get("device_id") for m in members}:
            continue
        ducking = {m.get("device_id") for m in members if m.get("duck", True) is not False}
        if device_id not in ducking:
            return set()
        return {i for i in ducking if isinstance(i, str) and i}
    return set()


def facts_of(devices) -> dict:
    """Facts for each live Device: its alarm or timer ringing (the schedule
    snapshot it reports every 2s, or a controller-rung HA timer) and its
    Sendspin group."""
    return {
        d.device_id: Facts(
            ringing=bool(getattr(d, "timer_alarm_ringing", False)
                         or alarm_ringing(getattr(getattr(d, "schedule", None), "snapshot", None))),
            sendspin=sendspin_group(getattr(d, "sendspin", None)),
        )
        for d in devices
    }


def playing_here(device, player_playing: bool) -> bool:
    """Whether an Echo's own speaker was sounding when it heard a wake: a
    reply, a ringing alarm or timer, HA's music (`player_playing`) or a
    Sendspin stream.

    The wake level (em_wakelevel) is measured over the 2s before the
    crossing, so it contains all of these, and each is louder at the Echo's
    own microphones than any talker: its level then says how loud the Echo
    was, not how near the person is. 2026-11-05: both Echoes ringing a shared
    alarm read 3.1dB apart and the further one won. Arbitration falls back to
    time for such a wake (em_arbiter.pick), as it already did for a reply."""
    return bool(getattr(device, "speaking", False)
                or getattr(device, "timer_alarm_ringing", False)
                or alarm_ringing(getattr(getattr(device, "schedule", None), "snapshot", None))
                or player_playing
                or sendspin_playing(getattr(device, "sendspin", None)))


def companions(device_id: str, facts: dict, groups: list) -> set:
    """
    The Echoes, other than device_id, that duck while device_id takes a turn.

    `facts` maps every CONNECTED device id to its Facts; an Echo not in it is
    left alone, since there is nothing to send it.
    """
    out = {d for d, f in facts.items() if f.ringing}
    out |= speaker_group_of(device_id, groups)
    mine = facts.get(device_id)
    if mine is not None and mine.sendspin is not None:
        out |= {d for d, f in facts.items() if f.sendspin == mine.sendspin}
    out.discard(device_id)
    return {d for d in out if d in facts}
