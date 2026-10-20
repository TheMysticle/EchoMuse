"""
Speaker groups: several Echoes playing the same Spotify in sync
(docs/speaker-groups.md).

One Echo of a group LEADS: it plays its own Spotify and sends the same audio,
stamped for a moment a little ahead, to the others over Sendspin. The rest are
ordinary Sendspin players. The controller's whole part is the arrangement; it
never carries audio:

- **The group** is the user's: a name, the Echoes in it and each one's channel
  (mono, left or right). It is kept in `system_config` and edited whole.
- **The leader** is chosen here (`elect`). Eligible means connected, able to
  lead (`group_leader`, emOS), with a linked Spotify engine. A pinned
  preference wins, then the sitting leader, then the first in the list. A
  leader that disappears is kept for `FAILOVER_S` before another takes over,
  because an Echo that drops off WiFi for a few seconds must not move the
  group's Spotify name under someone who just chose it.
- **The orders are an overlay on the config push** (`groupRole`, `groupLead`),
  never stored in a device's config: a group is the controller's arrangement,
  so a device that loses the controller is a plain Echo again.
- **Keys** (`_pair_keys`). The controller is the trust anchor, so it mints a
  random 32-byte secret for each leader/member pair and sends it to both as
  `group_trust` (to the member, naming the leader) and `group_key` (to the
  leader, naming the member). Neither the secret nor the message is ever
  logged, stored or put in an event; both ends keep it in their own store. The
  leader says when a member needs one (`needs_key`), which also covers a
  member that forgot it, so the pairing heals itself.

Rules the rest of the module depends on:

- **Nothing runs for a fleet with no groups.** The background task sleeps on
  an event, wakes for a change, and does one empty check.
- **Unknown is not ready.** A member the leader has not reported on is
  `starting`, never `ready`; an Echo the controller cannot see is `offline`.
- **Disabled with the reason, never silently.** `devices` in the view says why
  an Echo cannot take part.
- **Nothing here is on the voice, wake or audio path** (`tests/test_groups.py`
  checks that no module of those imports it).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field

import em_db as db
import em_labels
import em_state
import em_tasks

log = logging.getLogger("echomuse.groups")

CONFIG_KEY = "speaker_groups"

CHANNELS = ("mono", "left", "right")
MAX_GROUPS = 16
MAX_MEMBERS = 8
SENDSPIN_PORT = 8928

# How long a leader that has gone is kept before another takes over.
FAILOVER_S = 30.0
# At least this long between two key mints for one leader/member pair, so a
# member that keeps refusing cannot be hammered with new secrets.
MINT_EVERY_S = 15.0
# With groups, look again this often even if nothing happened.
TICK_S = 30.0
# Changes arriving together are handled together.
DEBOUNCE_S = 0.5
ACK_TIMEOUT_S = 8.0

# What a member of the leader's list can be, as the leader reports it.
READY = "ready"
_STARTING = {"connecting", "needs_key"}
_PROBLEM = {"busy", "unreachable", "error"}


# ── The model ───────────────────────────────────────────────────────────────

def normalize(raw, *, strict: bool = False) -> list[dict]:
    """
    Groups as they are kept: `[{id, name, members: [{device_id, channel}],
    prefer}]`. With `strict` (a save) anything wrong raises ValueError with a
    sentence for the user; without it (a read of what was stored) a bad entry
    is dropped, so one corrupt record cannot stop the controller starting.
    """
    if not isinstance(raw, list):
        raise ValueError("groups must be a list")
    if len(raw) > MAX_GROUPS:
        raise ValueError(f"at most {MAX_GROUPS} groups")
    out, used, names = [], set(), set()
    for g in raw:
        try:
            group = _normalize_group(g, used, names)
        except ValueError:
            if strict:
                raise
            continue
        out.append(group)
    return out


def _normalize_group(g, used: set, names: set) -> dict:
    if not isinstance(g, dict):
        raise ValueError("a group must be an object")
    name, why = em_labels.check_label(g.get("name"))
    if why:
        raise ValueError(f"group name: {why}")
    key = em_labels.label_key(name)
    if key in names:
        raise ValueError(f"there are two groups called “{name}”")
    members_in = g.get("members")
    if not isinstance(members_in, list):
        raise ValueError(f"“{name}”: members must be a list")
    if len(members_in) > MAX_MEMBERS:
        raise ValueError(f"“{name}” can have at most {MAX_MEMBERS} Echoes")
    members = []
    for m in members_in:
        if not isinstance(m, dict) or not isinstance(m.get("device_id"), str) or not m["device_id"]:
            raise ValueError(f"“{name}”: each Echo needs a device_id")
        did, channel = m["device_id"], m.get("channel", "mono")
        if channel not in CHANNELS:
            raise ValueError(f"“{name}”: channel must be mono, left or right")
        if did in used:
            raise ValueError("an Echo can be in only one group")
        used.add(did)
        members.append({"device_id": did, "channel": channel})
    prefer = g.get("prefer")
    if prefer is not None and prefer not in {m["device_id"] for m in members}:
        prefer = None
    gid = g.get("id")
    if not isinstance(gid, str) or not gid or len(gid) > 64:
        gid = uuid.uuid4().hex[:12]
    names.add(key)
    return {"id": gid, "name": name, "members": members, "prefer": prefer}


_groups: list[dict] = []
_save_lock: asyncio.Lock | None = None
_wake: asyncio.Event | None = None
_task: asyncio.Task | None = None
_views: dict = {"groups": [], "at": 0.0}


def groups() -> list[dict]:
    return _groups


def active() -> bool:
    return bool(_groups)


async def load(db_path: str | None = None) -> None:
    """Read the saved groups. Called once at start-up; a bad record starts empty."""
    global _groups, _save_lock, _wake
    _save_lock = asyncio.Lock()
    _wake = asyncio.Event()
    raw = await asyncio.to_thread(db.get_config, CONFIG_KEY)
    try:
        _groups = normalize(json.loads(raw)["groups"]) if raw else []
    except (ValueError, TypeError, KeyError):
        log.warning("saved speaker groups are unreadable; starting with none")
        _groups = []
    if _groups:
        poke()


async def set_groups(raw) -> list[dict]:
    """Replace every group (raises ValueError with a sentence on a bad one)."""
    global _groups
    new = normalize(raw, strict=True)
    async with _save_lock:
        text = json.dumps({"groups": new})
        await asyncio.to_thread(db.set_config, CONFIG_KEY, text)
        _groups = new
    poke()
    return new


def poke() -> None:
    """Something changed that the arrangement may depend on."""
    if _wake is not None and (_groups or _pushed_any()):
        _wake.set()


def _pushed_any() -> bool:
    return any(getattr(d, "group_pushed", None) for d in em_state.devices.values())


# ── Facts about a device, taken once per pass ───────────────────────────────

@dataclass
class Facts:
    device_id: str
    label: str = ""
    exists: bool = True
    connected: bool = False
    caps: frozenset = frozenset()
    base_os: str | None = None
    ip: str = ""
    spotify: dict | None = None
    sendspin: dict | None = None
    group_status: dict | None = None

    @property
    def name(self) -> str:
        return self.label or f"Echo {self.device_id[-4:]}"

    @property
    def client_id(self) -> str:
        return (self.sendspin or {}).get("clientId") or ""

    @property
    def can_member(self) -> bool:
        return self.connected and "group_member" in self.caps and "sendspin" in self.caps

    @property
    def can_lead(self) -> bool:
        return self.connected and "group_leader" in self.caps and self.base_os == "emos"


def lead_problem(f: Facts) -> str:
    """Why this Echo cannot lead right now, or '' when it can."""
    if not f.exists:
        return "no longer exists"
    if not f.connected:
        return "offline"
    if "group_leader" not in f.caps or f.base_os != "emos":
        return "only an Echo on emOS can lead"
    sp = f.spotify
    if not sp or sp.get("engine") != "running":
        return "Spotify is not running on it"
    if sp.get("link") != "linked":
        return "Spotify is not linked on it"
    return ""


def member_problem(f: Facts) -> str:
    if not f.exists:
        return "no longer exists"
    if not f.connected:
        return "offline"
    if "group_member" not in f.caps or "sendspin" not in f.caps:
        return "needs a firmware update for speaker groups"
    return ""


def collect(labels: dict, exists: set) -> dict[str, Facts]:
    """Facts for every device a group names, from the live registry."""
    wanted = {m["device_id"] for g in _groups for m in g["members"]}
    out = {}
    for did in wanted:
        live = em_state.devices.get(did)
        f = Facts(device_id=did, label=labels.get(did, ""), exists=did in exists)
        if live is not None:
            f.connected = True
            f.caps = frozenset(live.capabilities or [])
            f.base_os = getattr(live, "base_os", None)
            f.ip = live.ip or ""
            f.spotify = getattr(live, "spotify", None)
            f.sendspin = getattr(live, "sendspin", None)
            f.group_status = getattr(live, "group_status", None)
        out[did] = f
    return out


# ── The leader ──────────────────────────────────────────────────────────────

def elect(group: dict, facts: dict, state: dict, now: float) -> tuple[str | None, dict, str]:
    """
    Choose the group's leader. `state` is what this function returned last
    time for the group (`{id, lost}`); it returns (leader id or None, the new
    state, a note for the user when there is nobody).
    """
    order = [m["device_id"] for m in group["members"]]
    able = [d for d in order if not lead_problem(facts[d])]
    cur, lost = state.get("id"), state.get("lost")
    prefer = group.get("prefer")
    if prefer in able:
        return prefer, {"id": prefer, "lost": None}, ""
    if cur in able:
        return cur, {"id": cur, "lost": None}, ""
    if cur in order:
        # The sitting leader is not usable. Give it a moment before moving.
        lost = lost if lost is not None else now
        wait = FAILOVER_S - (now - lost)
        if wait > 0 and not (able and cur is None):
            return None, {"id": cur, "lost": lost}, (
                f"Waiting for {facts[cur].name} to come back "
                f"({lead_problem(facts[cur])}); another Echo takes over in {int(wait) + 1} s.")
    if able:
        return able[0], {"id": able[0], "lost": None}, ""
    return None, {"id": cur if cur in order else None, "lost": lost}, _nobody(order, facts)


def _nobody(order: list, facts: dict) -> str:
    parts = [f"{facts[d].name}: {lead_problem(facts[d]) or 'ok'}" for d in order]
    return "No Echo here can lead yet. " + "; ".join(parts) + "."


# ── The plan ────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    views: list = field(default_factory=list)
    overlays: dict = field(default_factory=dict)   # device_id → config fields
    pairs: list = field(default_factory=list)      # (leader id, member id) needing a key


def plan(group_list: list, facts: dict, states: dict, now: float) -> Plan:
    p = Plan()
    for g in group_list:
        gid = g["id"]
        leader, states[gid], note = elect(g, facts, states.get(gid, {}), now)
        p.views.append(_view(g, facts, leader, note, p))
    return p


def _view(g: dict, facts: dict, leader: str | None, note: str, p: Plan) -> dict:
    members = []
    for m in g["members"]:
        f = facts[m["device_id"]]
        members.append({"device_id": f.device_id, "label": f.name, "channel": m["channel"],
                        "connected": f.connected, "role": "leader" if f.device_id == leader else "member",
                        "state": "offline", "message": ""})
    view = {"id": g["id"], "name": g["name"], "prefer": g.get("prefer"), "members": members,
            "leader": leader, "spotify_name": "", "instruction": "", "status": "", "message": ""}
    if len(members) < 2:
        view.update(status="incomplete", message="Add at least one more Echo to make a group.")
        _member_states(view, facts, None)
        return view
    if leader is None:
        view.update(status="no_leader", message=note)
        _member_states(view, facts, None)
        return view
    lf = facts[leader]
    view["spotify_name"] = lf.name
    view["instruction"] = f"Choose “{lf.name}” in Spotify."
    _overlays(g, facts, leader, p)
    _member_states(view, facts, leader)
    _status(view, lf)
    _key_jobs(g, facts, leader, p)
    return view


def _overlays(g: dict, facts: dict, leader: str, p: Plan) -> None:
    lead_members = []
    for m in g["members"]:
        f = facts[m["device_id"]]
        if f.device_id == leader:
            continue
        if f.can_member:
            p.overlays[f.device_id] = {"groupRole": "member"}
        if f.can_member and f.client_id and f.ip:
            lead_members.append({"id": f.device_id, "clientId": f.client_id, "host": f.ip,
                                 "port": SENDSPIN_PORT, "channel": m["channel"]})
    own = next(m["channel"] for m in g["members"] if m["device_id"] == leader)
    p.overlays[leader] = {"groupRole": "leader", "groupLead": {
        "name": g["name"], "channel": own, "members": lead_members}}


def _member_states(view: dict, facts: dict, leader: str | None) -> None:
    reported = {}
    if leader is not None:
        st = facts[leader].group_status or {}
        reported = {r.get("id"): r for r in st.get("members") or []}
    for m in view["members"]:
        f = facts[m["device_id"]]
        why = member_problem(f)
        if m["role"] == "leader":
            lp = lead_problem(f)
            m["state"], m["message"] = ("leader", "Plays Spotify and sends it to the others.") if not lp \
                else ("problem", f"Cannot lead: {lp}.")
        elif why:
            m["state"], m["message"] = ("offline" if not f.connected else "unsupported"), why.capitalize() + "."
        elif leader is None:
            m["state"], m["message"] = "idle", "Waiting for a leader."
        else:
            m["state"], m["message"] = _reported(reported.get(f.device_id), f)


def _reported(r: dict | None, f: Facts) -> tuple[str, str]:
    if not f.client_id:
        return "starting", "Starting its speaker player."
    if r is None:
        return "starting", "Waiting for the leader to reach it."
    state = r.get("state")
    if state == READY:
        return "ready", "In sync with the leader."
    if state in _STARTING:
        return "starting", "Setting up a secure link." if state == "needs_key" else "Connecting."
    if state == "busy":
        return "busy", "Playing something else; it rejoins when it is free."
    if state == "unreachable":
        return "problem", "The leader cannot reach it on the network."
    return "problem", r.get("detail") or "The link to it failed."


def _status(view: dict, lf: Facts) -> None:
    others = [m for m in view["members"] if m["role"] != "leader"]
    states = [m["state"] for m in others]
    if all(s == "ready" for s in states):
        view.update(status="ready", message="Ready. Play from Spotify and every Echo here plays together.")
    elif "ready" in states:
        view.update(status="partial", message="Playing on the Echoes that are ready; the others rejoin when they can.")
    elif any(s in ("problem", "offline", "unsupported") for s in states):
        view.update(status="problem", message="No other Echo is reachable yet, so the leader plays alone.")
    else:
        view.update(status="starting", message="Starting up.")
    if not (lf.group_status or {}).get("serverId") and view["status"] == "ready":
        view.update(status="starting", message="Starting up.")


def _key_jobs(g: dict, facts: dict, leader: str, p: Plan) -> None:
    st = facts[leader].group_status or {}
    if not st.get("serverId"):
        return
    reported = {r.get("id"): r for r in st.get("members") or []}
    for m in g["members"]:
        did = m["device_id"]
        r = reported.get(did)
        f = facts[did]
        if did != leader and r and r.get("state") == "needs_key" and f.can_member \
                and f.client_id and r.get("clientId") == f.client_id:
            p.pairs.append((leader, did))


# ── Doing it ────────────────────────────────────────────────────────────────

_states: dict = {}
_last_mint: dict = {}


async def _labels() -> tuple[dict, set]:
    rows = await asyncio.to_thread(db.get_all_devices)
    return ({r["device_id"]: r["label"] or "" for r in rows}, {r["device_id"] for r in rows})


async def reconcile(now: float | None = None) -> Plan:
    """One pass: plan, push what changed, mint what is missing."""
    now = time.monotonic() if now is None else now
    labels, exists = await _labels()
    facts = collect(labels, exists)
    p = plan(_groups, facts, _states, now)
    live_ids = {g["id"] for g in _groups}
    for gid in [k for k in _states if k not in live_ids]:
        del _states[gid]
    await _push_overlays(p)
    for leader, member in p.pairs:
        await _pair_keys(leader, member, now)
    _views["groups"], _views["at"] = p.views, time.time()
    return p


async def _push_overlays(p: Plan) -> None:
    for did, dev in list(em_state.devices.items()):
        want = p.overlays.get(did)
        have = getattr(dev, "group_pushed", None)
        if want is None and have is not None:
            want = {"groupRole": ""}
        elif want is None or want == have:
            continue
        try:
            await dev.push_config(**want)
        except Exception as e:  # a device that went away mid-pass
            log.warning("[group] %s: could not send its part: %s", did, e)
            continue
        dev.group_pushed = None if want == {"groupRole": ""} else want


async def _ask(dev, msg: dict) -> dict:
    """Send a message carrying a secret and wait for the device's answer.
    The message is never logged."""
    fut = asyncio.get_running_loop().create_future()
    dev.group_waiters[msg["id"]] = fut
    try:
        await dev.send_control(msg)
        return await asyncio.wait_for(fut, ACK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "no answer"}
    finally:
        dev.group_waiters.pop(msg["id"], None)


async def _pair_keys(leader: str, member: str, now: float) -> None:
    if now - _last_mint.get((leader, member), -MINT_EVERY_S) < MINT_EVERY_S:
        return
    _last_mint[(leader, member)] = now
    ldev, mdev = em_state.devices.get(leader), em_state.devices.get(member)
    if ldev is None or mdev is None:
        return
    server_id = (ldev.group_status or {}).get("serverId")
    client_id = (mdev.sendspin or {}).get("clientId")
    if not server_id or not client_id:
        return
    psk = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    # The member learns the leader first: a leader that dials before that is
    # refused, and two refusals make it throw the key away.
    first = await _ask(mdev, {"type": "group_trust", "id": uuid.uuid4().hex, "peer": server_id, "psk": psk})
    if not first.get("ok"):
        log.warning("[group] %s did not accept a link to %s: %s", member, leader, first.get("error"))
        return
    second = await _ask(ldev, {"type": "group_key", "id": uuid.uuid4().hex, "peer": client_id, "psk": psk})
    if not second.get("ok"):
        log.warning("[group] %s did not accept a link to %s: %s", leader, member, second.get("error"))
        return
    log.info("[group] linked %s to leader %s", member, leader)


def on_ack(device, msg: dict) -> None:
    """A device answered a group_trust or group_key."""
    fut = device.group_waiters.get(msg.get("id"))
    if fut is not None and not fut.done():
        fut.set_result({"ok": bool(msg.get("ok")), "error": str(msg.get("error") or "")[:200]})


def on_status(device, status) -> None:
    device.group_status = status if isinstance(status, dict) else None
    poke()


async def _run() -> None:
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), TICK_S if _groups else 3600)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
        await asyncio.sleep(DEBOUNCE_S)
        if not _groups and not _pushed_any():
            _views["groups"] = []
            continue
        try:
            await reconcile()
        except Exception:
            log.exception("[group] pass failed")


def start() -> None:
    """Start the background task (once, from main)."""
    global _task
    if _task is None or _task.done():
        _task = em_tasks.spawn(_run(), name="speaker-groups")


# ── What the dashboard reads ────────────────────────────────────────────────

async def view() -> dict:
    labels, exists = await _labels()
    devices = []
    in_group = {m["device_id"]: g["id"] for g in _groups for m in g["members"]}
    for did, label in sorted(labels.items(), key=lambda kv: (kv[1].lower(), kv[0])):
        live = em_state.devices.get(did)
        f = Facts(device_id=did, label=label, connected=live is not None,
                  caps=frozenset(live.capabilities or []) if live else frozenset())
        devices.append({"device_id": did, "label": f.name, "connected": f.connected,
                        "can_member": f.can_member, "can_lead": bool(live) and "group_leader" in f.caps,
                        "reason": member_problem(f) if live is not None else "",
                        "group": in_group.get(did)})
    return {"groups": _views["groups"] or [], "configured": _groups, "devices": devices,
            "limits": {"groups": MAX_GROUPS, "members": MAX_MEMBERS}, "channels": list(CHANNELS)}


async def validate_members(raw) -> None:
    """Refuse a save naming an Echo that does not exist or is connected and
    cannot take part. An offline Echo is allowed: its abilities are unknown."""
    labels, exists = await _labels()
    for g in normalize(raw, strict=True):
        for m in g["members"]:
            did = m["device_id"]
            if did not in exists:
                raise ValueError("one of those Echoes does not exist")
            live = em_state.devices.get(did)
            if live is not None and member_problem(Facts(did, labels.get(did, ""), connected=True,
                                                         caps=frozenset(live.capabilities or []))):
                raise ValueError(f"{labels.get(did) or did[-4:]} needs a firmware update for speaker groups")
