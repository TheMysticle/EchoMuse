"""
Sharing a satellite's reminders, timers and alarms with other satellites.

An Echo owns its schedule (docs/local-schedules.md): the store, the ringing and
the cached audio are on the device, and the controller only carries commands.
Sharing keeps that. For each (source, kind, target) the controller keeps a COPY
of the source's items in the target's own store, made with the ordinary
create/edit/cancel commands, so a copy rings with the controller and Home
Assistant stopped exactly as an original does, and no firmware change is
needed. The controller holds no timer of its own.

Rules the rest of the module depends on:

- **A copy is recognised by its id** (`sh-<source tag>-<item hash>`, `mirror_id`).
  The Echo's item has no origin field, and the id is deterministic, so a lost
  controller cache costs nothing: the next pass derives the same ids. Copies are
  never copied again, which is what stops two satellites sharing with each other
  from echoing forever. `custom_components/echomuse_schedule/shared.py` holds
  the same pattern so the companion leaves copies out of its entities (a
  controller test checks they agree).
- **A pass is a pure function of snapshots** (`plan`), run in the background
  after a snapshot's revision changes. Nothing here runs on the audio, wake or
  voice path, and a satellite that takes part in no sharing and holds no copy
  costs one short list scan per revision change and nothing else.
- **Unknown is not absent.** A source with no live snapshot keeps its copies; a
  copy is cancelled only when the source says the item is gone, or the pair is
  no longer shared. An offline target is brought in line when it reports again.
- **An original that is ringing, snoozed or finished is left alone**, so a copy
  about to ring is never cancelled because its original rang a second earlier.
- **A copy cancelled on its own satellite stays cancelled** (a skip, persisted),
  because "cancel all timers" said in the kitchen must not bring the timer back
  from the living room. A copy edited on its own satellite follows the original
  again on the next pass.
- **A timer or alarm stopped on one satellite is stopped on the others**
  (`stop_together`, on by default, one switch for the house). The original and
  its copies are one alert that happens to ring in several rooms, so whichever
  is silenced silences the rest, including a copy that is a moment from
  ringing. It is detected from the snapshot itself (an item that was ringing
  and no longer is), so it works for a button press, a voice "stop", the
  dashboard and Home Assistant alike, and it is acted on at once rather than
  after the debounce. Reminders are never stopped this way.
- **Spoken reminder text travels with the copy.** The Echo needs the speech
  asset cached on it before an item may name it, and only the satellite the
  companion uploaded to has it, so uploads are kept in a small disk cache
  (`schedule_assets/` beside the database) and sent to a target when a copy
  needs them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import em_db as db
import em_schedule
import em_state
import em_tasks

log = logging.getLogger("echomuse.schedule_share")

KINDS = ("reminder", "timer", "alarm")
CONFIG_KEY = "schedule_sharing"
SKIPS_KEY = "schedule_sharing_skips"
OPTIONS_KEY = "schedule_sharing_options"
ASSETS_SUBDIR = "schedule_assets"
ASSET_CACHE_BYTES = 64 * 1024 * 1024   # the Echo's own audio budget
MAX_SKIPS = 256
DEBOUNCE_S = 0.5
RETRY_FIRST_S = 15
RETRY_MAX_S = 300
TOLERANCE_S = 3     # a timer's seconds are rounded on each end
LEAD_S = 2          # an original this close to firing is not worth copying
STOP_KINDS = ("timer", "alarm")
SOUNDING = ("ringing", "queued")
GRACE_S = 5         # a copy this close to ringing is stopped with the rest

_MIRROR = re.compile(r"^sh-([0-9a-f]{8})-[0-9a-f]{24}\Z")
_ASSET = re.compile(r"^[a-f0-9]{64}\Z")
TERMINAL = ("completed", "missed", "canceled")
LIVE_COPY = ("scheduled", "paused", "waiting_for_time")


def tag(device_id: str) -> str:
    return hashlib.sha1(device_id.encode()).hexdigest()[:8]


def mirror_id(source: str, item_id: str) -> str:
    digest = hashlib.sha1(f"{source}:{item_id}".encode()).hexdigest()[:24]
    return f"sh-{tag(source)}-{digest}"


def is_mirror(item_id) -> bool:
    return bool(_MIRROR.match(str(item_id)))


def _tag_of(item_id: str) -> str:
    return _MIRROR.match(item_id).group(1)


def normalize(raw, known: set[str] | None = None) -> dict:
    """`{source: {kind: [target, ...]}}`, sorted, with nothing empty, nothing
    shared with itself, and (when `known` is given) only known satellites."""
    out: dict[str, dict[str, list[str]]] = {}
    if not isinstance(raw, dict):
        return out
    for source, kinds in raw.items():
        if not isinstance(source, str) or not isinstance(kinds, dict):
            continue
        if known is not None and source not in known:
            continue
        for kind, targets in kinds.items():
            if kind not in KINDS or not isinstance(targets, list):
                continue
            keep = sorted({t for t in targets if isinstance(t, str) and t != source
                           and (known is None or t in known)})
            if keep:
                out.setdefault(source, {})[kind] = keep
    return out


@dataclass
class Op:
    target: str
    action: str                       # create | edit | cancel | pause | resume
    id: str = ""
    item: dict | None = None
    speech: str = ""                  # asset the target must hold first
    source: str = ""


@dataclass
class _Want:
    spec: dict
    item: dict                        # the original
    paused: bool = False
    speech: str = ""


def _spec(item: dict, now: float, assets) -> _Want | None:
    """What a copy of `item` should be, or None when it is not worth copying
    now (finished, ringing, snoozed, disabled, or about to fire)."""
    if not item.get("enabled", True):
        return None
    kind, state = item.get("kind"), item.get("state")
    spec = {"kind": kind, "label": item.get("label", ""),
            "timezone": item.get("timezone", "UTC")}
    if item.get("message"):
        spec["message"] = item["message"]
    if kind == "timer":
        if state == "scheduled" and item.get("due"):
            left = item["due"] - now
        elif state == "paused":
            left = item.get("remaining", 0)
        else:
            return None
        if left < 1:
            return None
        spec["duration"] = int(left)
        return _Want(spec, item, paused=state == "paused")
    if state != "scheduled":
        return None
    due = item.get("due", 0)
    if item.get("rule"):
        spec["rule"] = item["rule"]
        spec["due"] = due if due > now + LEAD_S else 0
    elif due > now + LEAD_S:
        spec["due"] = due
    else:
        return None
    want = _Want(spec, item)
    if kind == "reminder" and item.get("speech") in assets:
        spec["speech"] = want.speech = item["speech"]
        spec["speech_version"] = item.get("speech_version", 1)
    return want


def _differs(want: _Want, copy: dict) -> bool:
    spec = want.spec
    if (spec["label"] != copy.get("label", "") or spec.get("message", "") != copy.get("message", "")
            or spec["timezone"] != copy.get("timezone")):
        return True
    if spec["kind"] == "timer":
        return False  # its time is judged separately, see `_timer_ops`
    if spec.get("rule") != copy.get("rule"):
        return True
    if want.speech and copy.get("speech") != want.speech:
        return True
    return "rule" not in spec and spec.get("due") != copy.get("due")


def _edit_item(want: _Want, copy: dict, mid: str) -> dict:
    """The full item for an `edit`. The Echo keeps a copy's state through an
    edit only when its time, duration, zone and rule are unchanged, so
    metadata-only edits send the copy's own numbers back."""
    item = {**want.spec, "id": mid}
    if want.spec["kind"] == "timer":
        item["due"], item["duration"] = copy.get("due", 0), copy.get("duration", 0)
    elif want.spec.get("rule") == copy.get("rule") and want.spec["timezone"] == copy.get("timezone"):
        item["due"] = copy.get("due", 0)
    else:
        item["due"] = 0
    return item


def _timer_ops(target, mid, want: _Want, copy: dict, source: str) -> list[Op]:
    """Bring a copy of a timer to the original's time and pause state."""
    item = want.item
    skew = None
    if want.paused and copy["state"] == "paused":
        skew = abs(copy.get("remaining", 0) - item.get("remaining", 0))
    elif not want.paused and copy["state"] == "scheduled" and copy.get("due") and item.get("due"):
        skew = abs(copy["due"] - item["due"])
    if skew is not None and skew > TOLERANCE_S:
        # Restarts it from the original's remaining time (paused ones are
        # paused again by the next pass).
        return [Op(target, "edit", mid, {**want.spec, "id": mid}, source=source)]
    if want.paused and copy["state"] == "scheduled":
        return [Op(target, "pause", mid, source=source)]
    if not want.paused and copy["state"] == "paused":
        return [Op(target, "resume", mid, source=source)]
    return []


def plan(config: dict, snaps: dict[str, dict], now: float, assets=frozenset(),
         skips=frozenset()) -> list[Op]:
    """The commands that bring every live target's copies in line with `config`.

    `snaps` holds only satellites that can be commanded now. A source missing
    from it is unknown, not empty."""
    ops: list[Op] = []
    for target, snap in snaps.items():
        mirrors = {i["id"]: i for i in snap.get("items", []) if is_mirror(i.get("id"))}
        pairs: dict[tuple[str, str], str] = {}
        wanted: dict[str, tuple[str, _Want]] = {}
        alive: set[str] = set()
        for source, kinds in config.items():
            for kind, targets in kinds.items():
                if target not in targets:
                    continue
                pairs[(tag(source), kind)] = source
                src = snaps.get(source)
                if src is None:
                    continue
                for item in src.get("items", []):
                    if item.get("kind") != kind or is_mirror(item.get("id")):
                        continue
                    if item.get("state") == "canceled" or (item.get("rule") and not item.get("enabled", True)):
                        continue
                    mid = mirror_id(source, item["id"])
                    alive.add(mid)
                    if (target, mid) in skips:
                        continue
                    want = _spec(item, now, assets)
                    if want is not None:
                        wanted[mid] = (source, want)
        clock_ok = snap.get("time_valid", False)
        for mid, (source, want) in wanted.items():
            copy = mirrors.get(mid)
            wall = want.spec["kind"] != "timer"
            if copy is None:
                if wall and not clock_ok:
                    continue
                ops.append(Op(target, "create", mid, {**want.spec, "id": mid},
                              speech=want.speech, source=source))
                if want.paused:
                    ops.append(Op(target, "pause", mid, source=source))
            elif copy.get("state") in LIVE_COPY:
                if want.spec["kind"] == "timer":
                    if _differs(want, copy):
                        ops.append(Op(target, "edit", mid, _edit_item(want, copy, mid), source=source))
                    else:
                        ops.extend(_timer_ops(target, mid, want, copy, source))
                elif _differs(want, copy) and clock_ok:
                    ops.append(Op(target, "edit", mid, _edit_item(want, copy, mid),
                                  speech=want.speech, source=source))
        for mid, copy in mirrors.items():
            if mid in wanted or copy.get("state") in TERMINAL:
                continue
            source = pairs.get((_tag_of(mid), copy.get("kind")))
            if source is not None and (snaps.get(source) is None or mid in alive):
                continue
            ops.append(Op(target, "cancel", mid, source=source or ""))
    return ops


def prune_skips(skips: set, config: dict, snaps: dict[str, dict]) -> set:
    """Drop skips whose original is gone or whose pair is no longer shared.
    A skip whose source cannot be seen right now is kept."""
    keep = set()
    for target, mid in skips:
        for source, kinds in config.items():
            if tag(source) != _tag_of(mid):
                continue
            if not any(target in targets for targets in kinds.values()):
                continue
            src = snaps.get(source)
            if src is None or any(mirror_id(source, i["id"]) == mid and i.get("state") != "canceled"
                                  for i in src.get("items", []) if not is_mirror(i.get("id"))):
                keep.add((target, mid))
    return keep


def plan_stops(config: dict, snaps: dict[str, dict], stops, now: float) -> list[Op]:
    """Silence the rest of an alert's group. `stops` are (satellite, item id,
    kind) that were sounding and no longer are. The group is the original and
    its copies on the satellites it is shared with for that kind."""
    ops: list[Op] = []
    done: set[tuple[str, str]] = set()
    for device, item_id, kind in stops:
        source, original = device, item_id
        if is_mirror(item_id):
            source = next((s for s, kinds in config.items()
                           if tag(s) == _tag_of(item_id) and device in kinds.get(kind, ())), None)
            snap = snaps.get(source) if source else None
            if snap is None:
                continue
            original = next((i["id"] for i in snap.get("items", []) if not is_mirror(i.get("id"))
                             and mirror_id(source, i["id"]) == item_id), None)
            if original is None:
                continue
        targets = config.get(source, {}).get(kind, [])
        if not targets:
            continue
        members = [(source, original)] + [(t, mirror_id(source, original)) for t in targets]
        for member, member_id in members:
            if (member, member_id) == (device, item_id) or (member, member_id) in done:
                continue
            item = next((i for i in snaps.get(member, {}).get("items", []) if i.get("id") == member_id), None)
            if item is None:
                continue
            state = item.get("state")
            if state == "ringing":
                action = "dismiss"
            elif state == "queued" or (state in ("scheduled", "snoozed") and item.get("enabled", True)
                                       and item.get("due") and 0 <= item["due"] - now <= GRACE_S):
                action = "complete"       # not sounding yet: finish it without a ring
            else:
                continue
            done.add((member, member_id))
            ops.append(Op(member, action, member_id, source=source))
    return ops


def detect_cancelled(prev: dict[str, str], current: dict[str, str], ours: set[str]) -> set[str]:
    """Copies that were live at the last look and are now cancelled or gone,
    which the controller did not do: someone cancelled them on that satellite."""
    gone = {mid for mid, state in prev.items()
            if state not in TERMINAL and current.get(mid, "canceled") == "canceled"}
    gone |= {mid for mid, state in current.items() if state == "canceled"}
    return gone - ours


# --- state -------------------------------------------------------------

_config: dict = {}
_skips: set[tuple[str, str]] = set()
_involved: set[str] = set()
_assets: set[str] = set()
_asset_dir: Path | None = None
_pushed: set[tuple[str, str, str]] = set()
_seen: dict[str, tuple[str, dict[str, str]]] = {}   # target -> (epoch, id -> state)
_ours: dict[str, set[str]] = {}
_errors: dict[str, str] = {}
_ring: dict[str, tuple[str, dict[str, str]]] = {}   # satellite -> (epoch, sounding id -> kind)
_stops: set[tuple[str, str, str]] = set()
_stop_together = True
_task: asyncio.Task | None = None
_dirty = False
_urgent = False
_wake: asyncio.Event | None = None   # the running pass's debounce, cut short by a stop
_retry_delay = RETRY_FIRST_S
_retry = None
_save_lock: asyncio.Lock | None = None


def config() -> dict:
    return {s: {k: list(t) for k, t in kinds.items()} for s, kinds in _config.items()}


def errors() -> dict[str, str]:
    return dict(_errors)


def _set_config(value: dict) -> None:
    global _config, _involved
    _config = value
    _involved = set(value) | {t for kinds in value.values() for ts in kinds.values() for t in ts}


async def load(db_path: str) -> None:
    """Read the saved sharing and the asset cache, then start listening for
    snapshot changes. Called once at start-up."""
    global _asset_dir, _skips, _save_lock, _stop_together
    _save_lock = asyncio.Lock()
    _asset_dir = Path(db_path).resolve().parent / ASSETS_SUBDIR

    def read():
        raw = db.get_config(CONFIG_KEY)
        skips = db.get_config(SKIPS_KEY)
        options = db.get_config(OPTIONS_KEY)
        found = {p.name for p in _asset_dir.iterdir()} if _asset_dir.is_dir() else set()
        return raw, skips, options, found

    raw, skips, options, found = await asyncio.to_thread(read)
    try:
        _set_config(normalize(json.loads(raw)) if raw else {})
        _skips = {(t, m) for t, m in json.loads(skips)} if skips else set()
        _stop_together = bool(json.loads(options).get("stop_together", True)) if options else True
    except (ValueError, TypeError):
        log.warning("saved schedule sharing is unreadable; starting with none")
        _set_config({})
        _skips = set()
    _assets.update(name for name in found if _ASSET.match(name))
    em_schedule.on_snapshot_change = notify


async def _save() -> None:
    async with _save_lock:
        cfg, skips = json.dumps(_config), json.dumps(sorted(_skips)[:MAX_SKIPS])
        options = json.dumps({"stop_together": _stop_together})

        def write():
            db.set_config(CONFIG_KEY, cfg)
            db.set_config(SKIPS_KEY, skips)
            db.set_config(OPTIONS_KEY, options)
        await asyncio.to_thread(write)


def stop_together() -> bool:
    return _stop_together


async def set_sharing(raw, stop_together: bool | None = None) -> dict:
    """Replace the sharing, validated against approved satellites. A change is
    acted on at once, including the removal of copies that are no longer shared."""
    global _stop_together
    known = await asyncio.to_thread(_approved_ids, _referenced(raw))
    value = normalize(raw, known)
    _set_config(value)
    if stop_together is not None:
        _stop_together = bool(stop_together)
        _ring.clear()
        _stops.clear()
    await _save()
    request_sync()
    return config()


def _referenced(raw) -> set[str]:
    found: set[str] = set()
    if isinstance(raw, dict):
        for source, kinds in raw.items():
            found.add(source)
            if isinstance(kinds, dict):
                for targets in kinds.values():
                    if isinstance(targets, list):
                        found |= {t for t in targets if isinstance(t, str)}
    return {s for s in found if isinstance(s, str)}


def _approved_ids(ids: set[str]) -> set[str]:
    out = set()
    for device_id in ids:
        row = db.get_device(device_id)
        if row is not None and row["approved"]:
            out.add(device_id)
    return out


async def device_deleted(device_id: str) -> None:
    """A deleted satellite stops sharing and receiving; copies it made on the
    others are cancelled by the next pass."""
    value = {}
    for source, kinds in _config.items():
        if source == device_id:
            continue
        kept = {k: [t for t in ts if t != device_id] for k, ts in kinds.items()}
        value[source] = {k: ts for k, ts in kept.items() if ts}
    _set_config({s: k for s, k in value.items() if k})
    for key in [k for k in _skips if k[0] == device_id]:
        _skips.discard(key)
    _seen.pop(device_id, None)
    _ours.pop(device_id, None)
    _ring.pop(device_id, None)
    await _save()
    request_sync()


# --- triggering ---------------------------------------------------------

def notify(owner: str, snapshot: dict, epoch: str = "") -> None:
    """Bridge hook: a satellite's snapshot changed. Nothing here awaits."""
    items = snapshot.get("items", ())
    urgent = False
    if _stop_together and owner in _involved:
        # Noticed here, on the report itself, so a stop within the debounce of
        # the ring starting is not missed. A new connection (epoch) has no
        # "before" to compare with, and a reboot is not a stop.
        sounding = {i["id"]: i["kind"] for i in items
                    if i.get("state") in SOUNDING and i.get("kind") in STOP_KINDS}
        before = _ring.get(owner)
        _ring[owner] = (epoch, sounding)
        if before is not None and before[0] == epoch:
            for item_id, kind in before[1].items():
                if item_id not in sounding:
                    _stops.add((owner, item_id, kind))
                    urgent = True
    if urgent or owner in _involved or any(is_mirror(i.get("id")) for i in items):
        request_sync(urgent)


def request_sync(urgent: bool = False) -> None:
    global _dirty, _task, _urgent
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _dirty = True
    _urgent = _urgent or urgent
    if urgent and _wake is not None:
        _wake.set()
    if _task is None or _task.done():
        _task = em_tasks.spawn(_run(), name="schedule-sharing")


def _retry_later() -> None:
    global _retry, _retry_delay
    if _retry is None:
        _retry = asyncio.get_running_loop().call_later(_retry_delay, _fire_retry)
        _retry_delay = min(_retry_delay * 2, RETRY_MAX_S)


def _fire_retry() -> None:
    global _retry
    _retry = None
    request_sync()


async def _run() -> None:
    global _dirty, _task, _retry_delay, _urgent, _wake
    _wake = asyncio.Event()
    while _dirty:
        _dirty = False
        if not _urgent:
            try:
                await asyncio.wait_for(_wake.wait(), DEBOUNCE_S)
            except asyncio.TimeoutError:
                pass
        _urgent = False
        _wake.clear()
        try:
            clean = await sync_once()
        except Exception:
            log.exception("schedule sharing pass failed")
            clean = False
        if clean:
            _retry_delay = RETRY_FIRST_S
        else:
            _retry_later()
    _wake = None
    _task = None


def _live() -> dict:
    return {device_id: d for device_id, d in list(em_state.devices.items())
            if "local_schedule" in (d.capabilities or []) and d.schedule.snapshot is not None}


async def sync_once() -> bool:
    """One pass. True when every command was acknowledged."""
    live = _live()
    snaps = {device_id: d.schedule.snapshot for device_id, d in live.items()}
    stopped_ok = True
    if _stops:
        stops = set(_stops)
        _stops.clear()
        stop_ops = plan_stops(_config, snaps, stops, time.time()) if _stop_together else []
        if stop_ops:
            stopped_ok = await _run_ops(live, snaps, stop_ops)
            if not stopped_ok:
                _stops.update(stops)      # replayed on the retry; the checks make it idempotent
            snaps = {device_id: d.schedule.snapshot for device_id, d in live.items()}
    changed = False
    for device_id, snap in snaps.items():
        current = {i["id"]: i.get("state", "") for i in snap.get("items", []) if is_mirror(i.get("id"))}
        epoch = live[device_id].schedule.epoch
        before = _seen.get(device_id)
        ours = _ours.setdefault(device_id, set())
        if before is not None and before[0] == epoch:
            for mid in detect_cancelled(before[1], current, ours):
                if (device_id, mid) not in _skips:
                    _skips.add((device_id, mid))
                    changed = True
        ours &= set(current)
        _seen[device_id] = (epoch, current)
    kept = prune_skips(_skips, _config, snaps)
    if kept != _skips or len(kept) > MAX_SKIPS:
        _skips.intersection_update(kept)
        changed = True
    ops = plan(_config, snaps, time.time(), _assets, _skips)
    if changed:
        await _save()
    if not ops:
        _errors.clear()
        return stopped_ok
    return await _run_ops(live, snaps, ops) and stopped_ok


async def _run_ops(live: dict, snaps: dict, ops: list[Op]) -> bool:
    by_target: dict[str, list[Op]] = {}
    for op in ops:
        by_target.setdefault(op.target, []).append(op)
    results = await asyncio.gather(*(_apply(live[t], items) for t, items in by_target.items()))
    for target in snaps:
        if target not in by_target:
            _errors.pop(target, None)
    return all(results)


async def _apply(dev, ops: list[Op]) -> bool:
    target = dev.device_id
    refused = False
    for op in ops:
        command = {"request": "sh_" + uuid.uuid4().hex}
        item = op.item
        try:
            if op.speech:
                item = await _with_speech(dev, op)
            if op.action in ("create", "edit"):
                command.update(action=op.action, item=item)
                if op.action == "edit":
                    command["id"] = op.id
            else:
                command.update(action=op.action, id=op.id)
            if op.action == "cancel":
                _ours.setdefault(target, set()).add(op.id)
            await dev.schedule.request(dev.send_control, command)
        except (ValueError, asyncio.TimeoutError) as err:
            message = str(err) or "the satellite did not acknowledge"
            if "not ringing" in message or "not found" in message:
                continue    # already stopped or removed there: the goal is met
            _errors[target] = message
            log.warning("sharing %s on %s failed: %s", op.action, target, message)
            if isinstance(err, asyncio.TimeoutError):
                return False
            if "limit" not in message and "exists" not in message:
                return False
            refused = True   # a full store or a clash: waiting will not fix it, so no retry
    if not refused:
        _errors.pop(target, None)
    return True


async def _with_speech(dev, op: Op) -> dict:
    """The op's item with its speech on the target, or without it when the
    audio is not in the cache (the copy then plays the reminder's chime)."""
    item = dict(op.item)
    key = (dev.device_id, dev.schedule.epoch, op.speech)
    if key not in _pushed:
        data = await asyncio.to_thread(_read_asset, op.speech)
        if data is None:
            item.pop("speech", None)
            item.pop("speech_version", None)
            return item
        await dev.schedule.request(dev.send_control, asset=op.speech, data=data)
        if len(_pushed) > 512:
            _pushed.clear()
        _pushed.add(key)
    return item


# --- uploaded audio ------------------------------------------------------

def _read_asset(asset: str) -> bytes | None:
    if _asset_dir is None or not _ASSET.match(asset):
        return None
    path = _asset_dir / asset
    try:
        data = path.read_bytes()
        os.utime(path)
    except OSError:
        _assets.discard(asset)
        return None
    return data


def _write_asset(asset: str, data: bytes) -> None:
    _asset_dir.mkdir(parents=True, exist_ok=True)
    tmp = _asset_dir / (asset + ".part")
    tmp.write_bytes(data)
    tmp.replace(_asset_dir / asset)
    files = sorted((p for p in _asset_dir.iterdir() if _ASSET.match(p.name)),
                   key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while total > ASSET_CACHE_BYTES and len(files) > 1:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink(missing_ok=True)
        _assets.discard(oldest.name)


async def cache_asset(asset: str, data: bytes) -> None:
    """Keep an uploaded sound or speech clip so it can be sent to a satellite
    that shares the item. Failing to cache never fails the upload."""
    if _asset_dir is None or not _ASSET.match(asset or "") or asset in _assets:
        return
    try:
        await asyncio.to_thread(_write_asset, asset, bytes(data))
        _assets.add(asset)
    except OSError as err:
        log.warning("could not cache schedule audio: %s", err)
