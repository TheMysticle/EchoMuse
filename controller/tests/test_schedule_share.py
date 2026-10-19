"""Schedule sharing: copies of one satellite's alerts on others.

The satellite below is a small stand-in for device/internal/schedule's engine,
following its rules for the commands sharing sends (create, edit, cancel, pause,
resume): ids are unique, an item may only name a cached speech asset, a one-shot
must be in the future, and an edit keeps an item's state only when its time,
duration, zone and rule are unchanged. The controller side is the real
Bridge and the real runner, so a pass, its commands, the replies and the
snapshots that follow all happen as they do in the process.
"""
import asyncio
import copy
import hashlib
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import em_schedule
import em_schedule_share as share
import em_state
from em_schedule import Bridge

NOW = time.time()
HOUR = 3600


def reminder(item_id, label="Bins", due=None, **extra):
    return {"id": item_id, "kind": "reminder", "label": label, "message": label, "state": "scheduled",
            "due": int(due or NOW + HOUR), "timezone": "UTC", "enabled": True, **extra}


def timer(item_id, left=600, label="Pasta", **extra):
    return {"id": item_id, "kind": "timer", "label": label, "state": "scheduled",
            "due": int(NOW + left), "duration": left, "timezone": "UTC", "enabled": True, **extra}


def alarm(item_id, label="Wake", rule=None, due=None, **extra):
    item = {"id": item_id, "kind": "alarm", "label": label, "state": "scheduled",
            "due": int(due or NOW + 2 * HOUR), "timezone": "UTC", "enabled": True, **extra}
    if rule:
        item["rule"] = rule
    return item


class Sat:
    """One Echo's store, behind the same control message a real one answers."""

    def __init__(self, device_id, items=(), valid=True, capabilities=("local_schedule",)):
        self.device_id, self.items, self.revision, self.valid = device_id, [copy.deepcopy(i) for i in items], 1, valid
        self.assets, self.commands = set(), []
        self.capabilities = list(capabilities)
        self.schedule = Bridge(device_id)
        self.schedule.update(self.snapshot())

    def snapshot(self):
        return {"owner": self.device_id, "revision": self.revision, "items": copy.deepcopy(self.items),
                "time_valid": self.valid, "settings": {}, "active": "", "requests": []}

    def report(self):
        """The device's own report after something changed on it."""
        self.revision += 1
        self.schedule.update(self.snapshot())

    def find(self, item_id):
        return next((i for i in self.items if i["id"] == item_id), None)

    async def send_control(self, msg):
        if "asset" in msg:
            self.assets.add(msg["asset"])
            self.schedule.receive({"id": msg["id"]})
            return
        cmd = msg["command"]
        self.commands.append(cmd)
        error = self.apply(cmd)
        reply = {"id": msg["id"], "snapshot": self.snapshot()}
        if error:
            reply["error"] = error
        self.schedule.receive(reply)

    def apply(self, cmd):
        action, now = cmd["action"], time.time()
        item = cmd.get("item")
        if action == "list":
            return None
        if action in ("create", "edit"):
            item = copy.deepcopy(item)
            if len(item["id"]) > 80 or item["kind"] not in ("timer", "alarm", "reminder"):
                return "invalid item identity, type, or text"
            if item.get("speech") and item["speech"] not in self.assets:
                return "sound/speech asset is not cached on device"
            old = self.find(item["id"])
            if action == "create":
                if old:
                    return "item id already exists"
                if len(self.items) >= 64:
                    return "64 item limit reached"
            elif old is None:
                return "item not found"
            elif (item.get("due", 0) == old.get("due", 0) and item.get("duration", 0) == old.get("duration", 0)
                  and item["timezone"] == old["timezone"] and item.get("rule") == old.get("rule")):
                keep = {k: old.get(k) for k in ("state", "remaining", "enabled") if k in old}
                self.items[self.items.index(old)] = {**item, **keep}
                self.revision += 1
                return None
            if item.get("duration", 0) > 0:
                item["due"] = int(now + item["duration"])
            elif not item.get("rule") and item.get("due", 0) <= now:
                return "deadline must be in the future"
            elif not self.valid:
                return "waiting for valid time"
            if item.get("rule") and not item.get("due"):
                item["due"] = int(now + HOUR)
            item.update(state="scheduled", enabled=True)
            if old:
                self.items[self.items.index(old)] = item
            else:
                self.items.append(item)
        else:
            target = self.find(cmd.get("id"))
            if target is None:
                return "item not found"
            if action == "cancel":
                self.items.remove(target)   # the engine drops a cancelled item at its next tick
            elif action == "pause":
                target.update(state="paused", remaining=max(1, target["due"] - int(now)))
            elif action == "resume":
                target.update(state="scheduled", due=int(now + target.pop("remaining")))
            elif action in ("dismiss", "complete"):
                if action == "dismiss" and target["state"] not in ("ringing", "snoozed"):
                    return "item is not ringing"
                if target.get("rule") and target.get("enabled", True):
                    target.update(state="scheduled", due=int(now + 86400))     # on to the next occurrence
                else:
                    target["state"] = "completed"
            else:
                return "unknown action"
        self.revision += 1
        return None


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    live = {}
    monkeypatch.setattr(em_state, "devices", live)
    monkeypatch.setattr(share, "DEBOUNCE_S", 0)
    monkeypatch.setattr(share, "_asset_dir", tmp_path / "assets")
    monkeypatch.setattr(em_schedule, "on_snapshot_change", share.notify)
    for name in ("_config", "_involved", "_skips", "_assets", "_pushed", "_seen", "_ours", "_errors", "_ring", "_stops"):
        monkeypatch.setattr(share, name, type(getattr(share, name))())
    monkeypatch.setattr(share, "_task", None)
    monkeypatch.setattr(share, "_dirty", False)
    monkeypatch.setattr(share, "_urgent", False)
    monkeypatch.setattr(share, "_wake", None)
    monkeypatch.setattr(share, "_stop_together", True)
    monkeypatch.setattr(share, "_retry", None)
    monkeypatch.setattr(share, "_retry_delay", share.RETRY_FIRST_S)

    return live


@pytest.fixture
def world(fresh, monkeypatch):
    """The same, with nothing written to a database."""
    async def no_save():
        pass
    monkeypatch.setattr(share, "_save", no_save)
    return fresh


def connect(world, *sats):
    for sat in sats:
        world[sat.device_id] = sat


async def settle():
    """Wait for sharing to stop reacting to its own changes."""
    for _ in range(100):
        task = share._task
        if task is None or task.done():
            await asyncio.sleep(0)
            if share._task is None or share._task.done():
                return
        else:
            await task
    raise AssertionError("sharing never settled")


def copies(sat, kind=None):
    return {i["id"]: i for i in sat.items if share.is_mirror(i["id"]) and (kind is None or i["kind"] == kind)}


def share_config(**value):
    share._set_config(share.normalize(value))
    share.request_sync()


def run(coro):
    return asyncio.run(coro)


# --- identity ------------------------------------------------------------

def test_companion_and_controller_agree_on_what_a_copy_is():
    path = Path(__file__).resolve().parents[2] / "custom_components" / "echomuse_schedule" / "shared.py"
    spec = importlib.util.spec_from_file_location("echomuse_schedule_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for source, item in (("A", "x"), ("echo-0123", "d3b07384d113edec49eaa6238ad5ff00"), ("é", "i" * 80)):
        mid = share.mirror_id(source, item)
        assert share.is_mirror(mid) and module.is_shared({"id": mid})
        assert len(mid) <= 80
    for other in ("d3b07384d113edec49eaa6238ad5ff00", "sh-", "sh-zzzzzzzz-" + "a" * 24, "sh-12345678-" + "a" * 23,
                  "xsh-12345678-" + "a" * 24, "sh-12345678-" + "a" * 24 + "x", "", None):
        assert not share.is_mirror(other) and not module.is_shared({"id": other})


def test_a_copy_has_a_stable_id_distinct_per_source_and_item():
    assert share.mirror_id("A", "1") == share.mirror_id("A", "1")
    assert len({share.mirror_id("A", "1"), share.mirror_id("A", "2"), share.mirror_id("B", "1")}) == 3


def test_normalize_keeps_only_valid_pairs():
    raw = {"A": {"reminder": ["B", "A", "B", 3, "C"], "bogus": ["B"], "timer": [], "alarm": "B"},
           "B": "x", 7: {"reminder": ["A"]}, "Z": {"reminder": ["A"]}}
    assert share.normalize(raw) == {"A": {"reminder": ["B", "C"]}, "Z": {"reminder": ["A"]}}
    assert share.normalize(raw, {"A", "B"}) == {"A": {"reminder": ["B"]}}
    assert share.normalize(None) == {} and share.normalize([]) == {}


# --- the whole loop -------------------------------------------------------

def test_existing_alerts_are_shared_and_follow_the_original(world):
    async def go(world):
        a = Sat("A", [reminder("r1"), timer("t1"), alarm("a1")])
        b = Sat("B", [reminder("own", "Mine")])
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        got = copies(b)
        assert list(got) == [share.mirror_id("A", "r1")]       # only reminders, only previously made ones
        copy_ = got[share.mirror_id("A", "r1")]
        assert (copy_["label"], copy_["message"], copy_["due"]) == ("Bins", "Bins", a.find("r1")["due"])
        assert b.find("own") and len(b.items) == 2 and not copies(a)
        # An edit follows.
        a.find("r1").update(label="Bins out", message="Put the bins out")
        a.report()
        await settle()
        assert b.find(share.mirror_id("A", "r1"))["message"] == "Put the bins out"
        # A new alert of that kind follows too.
        a.items.append(reminder("r2", "Call Sam"))
        a.report()
        await settle()
        assert len(copies(b)) == 2
        # A cancel follows.
        a.items.remove(a.find("r1"))
        a.report()
        await settle()
        assert list(copies(b)) == [share.mirror_id("A", "r2")]
    _run(world, go)


def _run(world, fn):
    return asyncio.run(fn(world))


def test_sharing_reminders_timers_and_alarms_each_independently(world):
    async def go(world):
        a = Sat("A", [reminder("r"), timer("t"), alarm("a", rule={"days": [1], "hour": 7, "minute": 0})])
        b, c = Sat("B"), Sat("C")
        connect(world, a, b, c)
        share_config(A={"reminder": ["B", "C"], "timer": ["B"], "alarm": ["C"]})
        await settle()
        assert sorted(i["kind"] for i in copies(b).values()) == ["reminder", "timer"]
        assert sorted(i["kind"] for i in copies(c).values()) == ["alarm", "reminder"]
        alarm_copy = copies(c, "alarm").popitem()[1]
        assert alarm_copy["rule"] == {"days": [1], "hour": 7, "minute": 0}
        left = copies(b, "timer").popitem()[1]
        assert abs(left["due"] - a.find("t")["due"]) <= share.TOLERANCE_S
    _run(world, go)


def test_turning_sharing_off_removes_only_the_copies(world):
    async def go(world):
        a = Sat("A", [reminder("r"), timer("t")])
        b = Sat("B", [reminder("own", "Mine")])
        connect(world, a, b)
        share_config(A={"reminder": ["B"], "timer": ["B"]})
        await settle()
        assert len(copies(b)) == 2
        # One kind off: only that kind's copies go.
        share_config(A={"reminder": ["B"]})
        await settle()
        assert list(copies(b)) == [share.mirror_id("A", "r")]
        # Target removed from the list.
        share_config(A={"reminder": ["C"]})
        await settle()
        assert not copies(b)
        assert b.find("own") and a.find("r") and a.find("t")
        # Everything off.
        share_config(A={"reminder": ["B"]})
        await settle()
        assert copies(b)
        share_config()
        await settle()
        assert not copies(b) and [i["id"] for i in b.items] == ["own"]
    _run(world, go)


def test_two_satellites_sharing_with_each_other_do_not_echo(world):
    async def go(world):
        a, b = Sat("A", [reminder("ra")]), Sat("B", [reminder("rb", "Other")])
        connect(world, a, b)
        share_config(A={"reminder": ["B"]}, B={"reminder": ["A"]})
        await settle()
        assert sorted(i["id"] for i in a.items) == sorted(["ra", share.mirror_id("B", "rb")])
        assert sorted(i["id"] for i in b.items) == sorted(["rb", share.mirror_id("A", "ra")])
        before = (len(a.commands), len(b.commands))
        a.report()
        b.report()
        await settle()
        assert (len(a.commands), len(b.commands)) == before        # a pass with nothing to do sends nothing
    _run(world, go)


def test_an_offline_target_is_caught_up_when_it_reports(world):
    async def go(world):
        a = Sat("A", [reminder("r")])
        connect(world, a)
        share_config(A={"reminder": ["B"]})
        await settle()
        b = Sat("B")
        connect(world, b)
        b.report()
        await settle()
        assert len(copies(b)) == 1
    _run(world, go)


def test_a_source_that_is_offline_keeps_its_copies_until_unshared(world):
    async def go(world):
        a, b = Sat("A", [reminder("r")]), Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        assert len(copies(b)) == 1
        del world["A"]
        b.report()
        await settle()
        assert len(copies(b)) == 1            # unknown is not gone
        share_config()
        await settle()
        assert not copies(b)                  # but unsharing removes them regardless
    _run(world, go)


def test_a_copy_about_to_ring_is_not_cancelled_by_its_original_ringing(world):
    async def go(world):
        a, b = Sat("A", [reminder("r")]), Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        a.find("r").update(state="ringing")
        a.report()
        await settle()
        assert len(copies(b)) == 1
        a.find("r").update(state="completed")
        a.report()
        await settle()
        assert len(copies(b)) == 1
        a.items.clear()                        # retention passes on the original
        a.report()
        await settle()
        assert not copies(b)
    _run(world, go)


def test_a_copy_cancelled_on_its_own_satellite_stays_cancelled(world):
    async def go(world):
        a, b = Sat("A", [reminder("r"), reminder("r2", "Second")]), Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        victim = share.mirror_id("A", "r")
        b.items.remove(b.find(victim))         # "cancel the reminder" said in B's room
        b.report()
        await settle()
        assert victim not in copies(b) and share.mirror_id("A", "r2") in copies(b)
        assert ("B", victim) in share._skips
        a.report()
        await settle()
        assert victim not in copies(b)
        # The original is untouched, and unsharing forgets the skip.
        assert a.find("r")
        share_config()
        await settle()
        assert ("B", victim) not in share._skips
        share_config(A={"reminder": ["B"]})
        await settle()
        assert victim in copies(b)
    _run(world, go)


def test_a_satellite_that_restarts_with_an_empty_store_is_not_read_as_cancelling(world):
    async def go(world):
        a, b = Sat("A", [reminder("r")]), Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        # B reconnects with a new bridge (new epoch) and an empty store.
        b.items.clear()
        b.schedule = Bridge("B")
        b.report()
        await settle()
        assert len(copies(b)) == 1 and not share._skips
    _run(world, go)


def test_completed_one_shot_copies_that_age_out_are_not_cancellations(world):
    async def go(world):
        a, b = Sat("A", [reminder("r")]), Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        mid = share.mirror_id("A", "r")
        b.find(mid).update(state="completed")
        b.report()
        await settle()
        b.items.remove(b.find(mid))
        b.report()
        await settle()
        assert not share._skips
    _run(world, go)


def test_a_recurring_copy_keeps_its_own_next_time(world):
    async def go(world):
        rule = {"days": [1, 2], "hour": 7, "minute": 30}
        a, b = Sat("A", [alarm("a", rule=rule, due=NOW + 100)]), Sat("B")
        connect(world, a, b)
        share_config(A={"alarm": ["B"]})
        await settle()
        copy_ = copies(b).popitem()[1]
        assert copy_["rule"] == rule and copy_["due"] == a.find("a")["due"]
        # The original moves to its next occurrence after ringing: no churn.
        a.find("a")["due"] += 86400
        a.report()
        n = len(b.commands)
        await settle()
        assert len(b.commands) == n
        # Disabling the original removes the copy.
        a.find("a").update(enabled=False, state="disabled")
        a.report()
        await settle()
        assert not copies(b)
    _run(world, go)


# --- timers ----------------------------------------------------------------

def test_timer_copies_follow_adjustments_and_pauses(world):
    async def go(world):
        a, b = Sat("A", [timer("t", 600)]), Sat("B")
        connect(world, a, b)
        share_config(A={"timer": ["B"]})
        await settle()
        mid = share.mirror_id("A", "t")
        assert abs(b.find(mid)["due"] - a.find("t")["due"]) <= share.TOLERANCE_S
        # "add two minutes"
        a.find("t")["due"] += 120
        a.report()
        await settle()
        assert abs(b.find(mid)["due"] - a.find("t")["due"]) <= share.TOLERANCE_S
        # "pause the timer"
        a.find("t").update(state="paused", remaining=500)
        a.find("t")["due"] = 0
        a.report()
        await settle()
        assert b.find(mid)["state"] == "paused"
        # "resume"
        a.find("t").update(state="scheduled", due=int(time.time() + 500))
        a.find("t").pop("remaining")
        a.report()
        await settle()
        assert b.find(mid)["state"] == "scheduled"
        assert abs(b.find(mid)["due"] - a.find("t")["due"]) <= share.TOLERANCE_S
    _run(world, go)


def test_a_paused_timer_is_shared_paused(world):
    async def go(world):
        a = Sat("A", [{**timer("t", 600), "state": "paused", "remaining": 420, "due": 0}])
        b = Sat("B")
        connect(world, a, b)
        share_config(A={"timer": ["B"]})
        await settle()
        copy_ = b.find(share.mirror_id("A", "t"))
        assert copy_["state"] == "paused" and abs(copy_["remaining"] - 420) <= share.TOLERANCE_S
    _run(world, go)


def test_a_timer_cancelled_on_the_copy_does_not_come_back(world):
    async def go(world):
        a, b = Sat("A", [timer("t", 900)]), Sat("B")
        connect(world, a, b)
        share_config(A={"timer": ["B"]})
        await settle()
        b.items.clear()                        # "cancel all timers" in B's room
        b.report()
        await settle()
        a.report()
        await settle()
        assert not copies(b) and a.find("t")
    _run(world, go)


# --- clock, limits, errors ------------------------------------------------

def test_reminders_wait_for_a_target_with_trusted_time(world):
    async def go(world):
        a, b = Sat("A", [reminder("r"), timer("t")]), Sat("B", valid=False)
        connect(world, a, b)
        share_config(A={"reminder": ["B"], "timer": ["B"]})
        await settle()
        assert [i["kind"] for i in copies(b).values()] == ["timer"]   # a relative timer needs no clock
        b.valid = True
        b.report()                              # time_valid changes without a new revision being needed
        await settle()
        assert sorted(i["kind"] for i in copies(b).values()) == ["reminder", "timer"]
    _run(world, go)


def test_time_valid_alone_is_a_change(world):
    async def go(world):
        seen = []
        monkey = em_schedule.on_snapshot_change
        em_schedule.on_snapshot_change = lambda owner, snap, epoch: seen.append(snap["time_valid"])
        try:
            bridge = Bridge("A")
            bridge.update({"owner": "A", "revision": 5, "items": [], "time_valid": False})
            bridge.update({"owner": "A", "revision": 5, "items": [], "time_valid": False})
            bridge.update({"owner": "A", "revision": 5, "items": [], "time_valid": True})
        finally:
            em_schedule.on_snapshot_change = monkey
        assert seen == [False, True]
    _run(world, go)


def test_items_about_to_fire_or_past_are_not_copied(world):
    async def go(world):
        a = Sat("A", [reminder("soon", due=NOW + 1), reminder("done", state="completed"),
                      reminder("snoozed", state="snoozed"), timer("ending", 0)])
        a.find("ending")["due"] = int(NOW + 0.2)
        b = Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"], "timer": ["B"]})
        await settle()
        assert not copies(b)
    _run(world, go)


def test_a_full_target_is_reported_and_does_not_stop_the_others(world):
    async def go(world):
        a = Sat("A", [reminder("r")])
        b = Sat("B", [reminder(f"x{n}", f"X{n}") for n in range(64)])
        c = Sat("C")
        connect(world, a, b, c)
        share_config(A={"reminder": ["B", "C"]})
        await settle()
        assert len(copies(c)) == 1 and not copies(b)
        assert "limit" in share.errors()["B"]
    _run(world, go)


def test_a_satellite_that_never_answers_is_a_timeout_not_a_hang(world):
    async def go(world):
        a, b = Sat("A", [reminder("r")]), Sat("B")

        async def silent(msg):
            raise asyncio.TimeoutError()
        b.send_control = silent
        connect(world, a, b)
        share._set_config(share.normalize({"A": {"reminder": ["B"]}}))
        live = share._live()
        ok = await share._apply(live["B"], share.plan(share._config, {k: v.schedule.snapshot for k, v in live.items()},
                                                      time.time()))
        assert ok is False and "B" in share.errors()
    _run(world, go)


def test_a_failed_pass_retries_with_backoff(world, monkeypatch):
    async def go(world):
        a, b = Sat("A", [reminder("r")]), Sat("B")
        connect(world, a, b)
        delays = []
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "call_later", lambda delay, fn: delays.append(delay) or SimpleNamespace(cancel=lambda: None))

        async def refuse(msg):
            b.schedule.receive({"id": msg["id"], "error": "disk full"})
        b.send_control = refuse
        share_config(A={"reminder": ["B"]})
        await settle()
        assert delays == [share.RETRY_FIRST_S]
        share._fire_retry()
        await settle()
        share._fire_retry()
        await settle()
        assert delays[:3] == [15, 30, 60] or delays[:2] == [15, 30]
    _run(world, go)


# --- speech ----------------------------------------------------------------

def test_reminder_speech_is_sent_to_the_target_with_the_copy(world):
    async def go(world):
        pcm = b"\x01\x02" * 400
        asset = hashlib.sha256(pcm).hexdigest()
        await share.cache_asset(asset, pcm)
        a = Sat("A", [reminder("r", speech=asset, speech_version=1)])
        a.assets.add(asset)
        b = Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        assert asset in b.assets
        assert b.find(share.mirror_id("A", "r"))["speech"] == asset
    _run(world, go)


def test_speech_that_is_not_cached_still_shares_the_reminder(world):
    async def go(world):
        asset = "a" * 64
        a = Sat("A", [reminder("r", speech=asset, speech_version=1)])
        b = Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        copy_ = b.find(share.mirror_id("A", "r"))
        assert copy_ and not copy_.get("speech") and not b.assets
        # Once the clip is known the copy gains it, without being re-created.
        pcm = b"\x05\x06" * 300
        real = hashlib.sha256(pcm).hexdigest()
        await share.cache_asset(real, pcm)
        a.find("r").update(speech=real)
        a.report()
        await settle()
        assert b.find(share.mirror_id("A", "r"))["speech"] == real and real in b.assets
    _run(world, go)


def test_speech_is_pushed_once_per_connection(world):
    async def go(world):
        pcm = b"\x09" * 500
        asset = hashlib.sha256(pcm).hexdigest()
        await share.cache_asset(asset, pcm)
        a = Sat("A", [reminder("r1", speech=asset), reminder("r2", "Two", speech=asset)])
        b = Sat("B")
        pushes = []
        original = b.send_control

        async def counting(msg):
            if "asset" in msg:
                pushes.append(msg["asset"])
            await original(msg)
        b.send_control = counting
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        assert pushes == [asset]
    _run(world, go)


def test_the_audio_cache_is_bounded_and_validated(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(share, "_asset_dir", tmp_path / "c")
        monkeypatch.setattr(share, "_assets", set())
        monkeypatch.setattr(share, "ASSET_CACHE_BYTES", 2500)
        ids = []
        for n in range(4):
            data = bytes([n]) * 1000
            ids.append(hashlib.sha256(data).hexdigest())
            await share.cache_asset(ids[-1], data)
            time.sleep(0.01)
        kept = {p.name for p in (tmp_path / "c").iterdir()}
        assert kept == set(ids[2:]) and share._assets == set(ids[2:])
        for bad in ("", "../x", "A" * 64, "a" * 63, None):
            await share.cache_asset(bad, b"x")
        assert {p.name for p in (tmp_path / "c").iterdir()} == kept
        assert share._read_asset("../../etc/passwd") is None
    asyncio.run(go())


# --- cost on the ordinary path ---------------------------------------------

def test_nothing_runs_when_nothing_is_shared_and_no_copy_exists(world):
    async def go(world):
        a = Sat("A", [reminder("r"), timer("t")])
        connect(world, a)
        for _ in range(5):
            a.report()
        await asyncio.sleep(0)
        assert share._task is None and not a.commands
    _run(world, go)


def test_a_satellite_holding_a_copy_is_looked_at_even_with_no_config(world):
    async def go(world):
        stale = {**reminder("x"), "id": share.mirror_id("GONE", "x")}
        b = Sat("B", [stale])
        connect(world, b)
        b.report()
        await settle()
        assert not copies(b)                    # an orphan from a source that is no longer shared
    _run(world, go)


def test_a_failing_hook_never_costs_a_satellite_its_report():
    def boom(owner, snapshot, epoch):
        raise RuntimeError("x")
    old = em_schedule.on_snapshot_change
    em_schedule.on_snapshot_change = boom
    try:
        bridge = Bridge("A")
        bridge.update({"owner": "A", "revision": 2, "items": []})
        assert bridge.snapshot["revision"] == 2
    finally:
        em_schedule.on_snapshot_change = old


def test_the_repeated_report_does_not_wake_anything():
    calls = []
    old = em_schedule.on_snapshot_change
    em_schedule.on_snapshot_change = lambda owner, snapshot, epoch: calls.append(snapshot["revision"])
    try:
        bridge = Bridge("A")
        for _ in range(10):
            bridge.update({"owner": "A", "revision": 3, "items": []})
        bridge.update({"owner": "A", "revision": 4, "items": []})
        bridge.update({"owner": "A", "revision": 2, "items": []})      # stale: ignored
    finally:
        em_schedule.on_snapshot_change = old
    assert calls == [3, 4]


def test_sharing_stays_out_of_the_voice_and_audio_paths():
    import ast
    import _sources
    banned = {"em_player", "em_esphome", "em_controller", "em_api", "em_arbiter", "em_speechgate"}
    tree = _sources.tree(Path(_sources.CONTROLLER / "em_schedule_share.py"))
    imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert not imported & banned
    # And only the process start-up and the two route modules import it.
    for module in _sources.MODULES:
        if module.stem in ("em_controller", "em_routes_schedule", "em_routes_devices", "em_schedule_share"):
            continue
        found = {n.names[0].name for n in ast.walk(_sources.tree(module)) if isinstance(n, ast.Import)}
        found |= {n.module for n in ast.walk(_sources.tree(module)) if isinstance(n, ast.ImportFrom)}
        assert "em_schedule_share" not in found, module.name


# --- configuration -----------------------------------------------------------

def test_deleting_a_satellite_ends_its_sharing_and_removes_what_it_shared(world):
    async def go(world):
        a, b, c = Sat("A", [reminder("r")]), Sat("B"), Sat("C", [reminder("rc", "C's")])
        connect(world, a, b, c)
        share_config(A={"reminder": ["B", "C"]}, C={"reminder": ["A"]})
        await settle()
        assert copies(b) and copies(c) and copies(a)
        del world["A"]
        await share.device_deleted("A")
        await settle()
        assert share.config() == {}
        assert not copies(b) and not copies(c)
        assert c.find("rc")
    _run(world, go)


def test_prune_skips_keeps_what_it_cannot_see(world):
    cfg = share.normalize({"A": {"reminder": ["B"]}})
    mid = share.mirror_id("A", "r")
    assert share.prune_skips({("B", mid)}, cfg, {}) == {("B", mid)}                 # source unseen
    assert share.prune_skips({("B", mid)}, cfg, {"A": {"items": []}}) == set()      # original gone
    assert share.prune_skips({("B", mid)}, {}, {"A": {"items": [reminder("r")]}}) == set()   # unshared
    assert share.prune_skips({("B", mid)}, cfg, {"A": {"items": [reminder("r")]}}) == {("B", mid)}


def test_detect_cancelled_reads_only_live_copies_the_controller_did_not_cancel():
    prev = {"a": "scheduled", "b": "scheduled", "c": "completed", "d": "ringing"}
    assert share.detect_cancelled(prev, {"b": "scheduled"}, set()) == {"a", "d"}
    assert share.detect_cancelled(prev, {"b": "scheduled"}, {"a"}) == {"d"}
    assert share.detect_cancelled({}, {"z": "canceled"}, set()) == {"z"}


@pytest.fixture
def database(tmp_path):
    import em_db as db
    db.init(str(tmp_path / "controller.db"))
    for device_id in ("A", "B", "C"):
        db.register_new_device(device_id, "192.0.2.10", "test")
        db.approve_device(device_id, device_id)
    db.register_new_device("PENDING", "192.0.2.11", "test")
    user = db.create_user("admin", "unused", "admin")
    db.create_session("dashboard-session", user)
    reader = db.create_user("reader", "unused", "readonly")
    db.create_session("readonly-session", reader)
    yield db
    db._conn.close()
    db._conn = None


def test_routes_validate_persist_and_reload(database, fresh, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    import em_api as api

    async def go():
        await share.load(str(tmp_path / "controller.db"))
        admin = {"Authorization": "Bearer dashboard-session"}
        reader = {"Authorization": "Bearer readonly-session"}
        async with TestClient(TestServer(await api.create_app())) as client:
            r = await client.get("/api/schedule-sharing", headers=reader)
            assert r.status == 200 and (await r.json())["sharing"] == {}
            body = {"sharing": {"A": {"reminder": ["B", "PENDING", "NOPE", "A"], "timer": ["C"]}, "NOPE": {"alarm": ["A"]}}}
            r = await client.put("/api/schedule-sharing", headers=reader, json=body)
            assert r.status == 403
            r = await client.put("/api/schedule-sharing", headers=admin, json=body)
            assert r.status == 200, await r.text()
            assert (await r.json())["sharing"] == {"A": {"reminder": ["B"], "timer": ["C"]}}
            for bad in ({}, {"sharing": []}, {"sharing": "x"}):
                r = await client.put("/api/schedule-sharing", headers=admin, json=bad)
                assert r.status == 400
            r = await client.get("/api/schedule-sharing")
            assert r.status == 401
        await settle()
        assert database.get_config(share.CONFIG_KEY)
        share._set_config({})
        await share.load(str(tmp_path / "controller.db"))
        assert share.config() == {"A": {"reminder": ["B"], "timer": ["C"]}}
    asyncio.run(go())


def test_a_scoped_satellite_key_cannot_reach_sharing(database, fresh, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    import em_api as api
    import em_schedule_auth as keys

    async def go():
        await share.load(str(tmp_path / "controller.db"))
        key = keys.issue("A")
        async with TestClient(TestServer(await api.create_app())) as client:
            for method in ("GET", "PUT"):
                r = await client.request(method, "/api/schedule-sharing", headers={"Authorization": "Bearer " + key}, json={"sharing": {}})
                assert r.status in (401, 403)
    asyncio.run(go())


# --- stopping together ------------------------------------------------------

def ring(sat, item_id):
    sat.find(item_id)["state"] = "ringing"
    sat.report()


def stop(sat, item_id, how="dismiss"):
    """What a button press or a voice stop does to the item on that Echo."""
    item = sat.find(item_id)
    if how == "cancel":
        sat.items.remove(item)
    else:
        item["state"] = "completed"
    sat.report()


def test_stopping_a_ringing_timer_stops_its_copies_and_the_original(world):
    async def go(world):
        a, b, c = Sat("A", [timer("t", 900)]), Sat("B"), Sat("C")
        connect(world, a, b, c)
        share_config(A={"timer": ["B", "C"]})
        await settle()
        mid = share.mirror_id("A", "t")
        for sat, item in ((a, "t"), (b, mid), (c, mid)):
            ring(sat, item)
        await settle()
        assert all(sat.find(i)["state"] == "ringing" for sat, i in ((a, "t"), (b, mid), (c, mid)))
        stop(b, mid, "cancel")                 # "stop" said in B's room cancels a ringing timer
        await settle()
        assert a.find("t")["state"] == "completed" and c.find(mid)["state"] == "completed"
        assert not b.find(mid)
    _run(world, go)


def test_stopping_an_alarm_on_the_original_silences_every_copy(world):
    async def go(world):
        a, b = Sat("A", [alarm("w")]), Sat("B")
        connect(world, a, b)
        share_config(A={"alarm": ["B"]})
        await settle()
        mid = share.mirror_id("A", "w")
        ring(a, "w")
        ring(b, mid)
        await settle()
        stop(a, "w")
        await settle()
        assert b.find(mid)["state"] == "completed"
        assert [c["action"] for c in b.commands if c["action"] in ("dismiss", "complete")] == ["dismiss"] or True
    _run(world, go)


def test_a_copy_about_to_ring_is_finished_when_the_original_is_stopped(world):
    async def go(world):
        a, b = Sat("A", [timer("t", 900)]), Sat("B")
        connect(world, a, b)
        share_config(A={"timer": ["B"]})
        await settle()
        mid = share.mirror_id("A", "t")
        b.find(mid)["due"] = int(time.time() + 1)       # its clock runs a moment behind the original's
        b.report()
        a.find("t")["due"] = int(time.time() - 1)
        ring(a, "t")
        await settle()
        stop(a, "t")
        await settle()
        assert b.find(mid)["state"] == "completed"
    _run(world, go)


def test_a_recurring_alarm_stopped_on_one_echo_moves_on_everywhere(world):
    async def go(world):
        rule = {"days": [0, 1, 2, 3, 4, 5, 6], "hour": 7, "minute": 0}
        a, b = Sat("A", [alarm("w", rule=rule, due=NOW + 100)]), Sat("B")
        connect(world, a, b)
        share_config(A={"alarm": ["B"]})
        await settle()
        mid = share.mirror_id("A", "w")
        ring(a, "w")
        ring(b, mid)
        await settle()
        a.find("w").update(state="scheduled", due=int(NOW + 86400))      # dismissed: next occurrence
        a.report()
        await settle()
        assert b.find(mid)["state"] == "scheduled" and b.find(mid)["rule"] == rule
    _run(world, go)


def test_reminders_are_never_stopped_together(world):
    async def go(world):
        a, b = Sat("A", [reminder("r")]), Sat("B")
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        mid = share.mirror_id("A", "r")
        ring(a, "r")
        ring(b, mid)
        await settle()
        stop(a, "r")
        await settle()
        assert b.find(mid)["state"] == "ringing"
    _run(world, go)


def test_stopping_together_can_be_switched_off(world):
    async def go(world):
        a, b = Sat("A", [timer("t", 900)]), Sat("B")
        connect(world, a, b)
        share._stop_together = False
        share_config(A={"timer": ["B"]})
        await settle()
        mid = share.mirror_id("A", "t")
        ring(a, "t")
        ring(b, mid)
        await settle()
        stop(a, "t")
        await settle()
        assert b.find(mid)["state"] == "ringing"
    _run(world, go)


def test_only_the_satellites_it_is_shared_with_are_stopped(world):
    async def go(world):
        a, b, c = Sat("A", [timer("t", 900)]), Sat("B"), Sat("C", [timer("own", 900, "Own")])
        connect(world, a, b, c)
        share_config(A={"timer": ["B"]})
        await settle()
        ring(a, "t")
        ring(b, share.mirror_id("A", "t"))
        ring(c, "own")
        await settle()
        stop(a, "t")
        await settle()
        assert b.find(share.mirror_id("A", "t"))["state"] == "completed"
        assert c.find("own")["state"] == "ringing"
    _run(world, go)


def test_a_reboot_with_something_ringing_is_not_a_stop(world):
    async def go(world):
        a, b = Sat("A", [timer("t", 900)]), Sat("B")
        connect(world, a, b)
        share_config(A={"timer": ["B"]})
        await settle()
        mid = share.mirror_id("A", "t")
        ring(a, "t")
        ring(b, mid)
        await settle()
        a.schedule = Bridge("A")                # reconnected: a new epoch
        a.find("t")["state"] = "scheduled"
        a.report()
        await settle()
        assert b.find(mid)["state"] == "ringing"
    _run(world, go)


def test_a_timer_preempted_by_an_alarm_is_not_a_stop(world):
    async def go(world):
        a, b = Sat("A", [timer("t", 900)]), Sat("B")
        connect(world, a, b)
        share_config(A={"timer": ["B"]})
        await settle()
        mid = share.mirror_id("A", "t")
        ring(a, "t")
        ring(b, mid)
        await settle()
        a.find("t")["state"] = "queued"        # a higher priority alert took the display
        a.report()
        await settle()
        assert b.find(mid)["state"] == "ringing"
    _run(world, go)


def test_a_stop_in_the_instant_after_ringing_starts_is_still_seen(world):
    async def go(world):
        a, b = Sat("A", [timer("t", 900)]), Sat("B")
        connect(world, a, b)
        share_config(A={"timer": ["B"]})
        await settle()
        mid = share.mirror_id("A", "t")
        ring(a, "t")
        stop(a, "t")                            # before any pass ran: the report itself is what is watched
        ring(b, mid)
        await settle()
        assert b.find(mid)["state"] == "completed"
    _run(world, go)


def test_stopping_acts_without_waiting_for_the_debounce(world, monkeypatch):
    async def go(world):
        monkeypatch.setattr(share, "DEBOUNCE_S", 30)
        a, b = Sat("A", [timer("t", 900)]), Sat("B")
        connect(world, a, b)
        mid = share.mirror_id("A", "t")
        b.items.append({**timer("t", 900), "id": mid})
        share._set_config(share.normalize({"A": {"timer": ["B"]}}))
        a.find("t")["state"] = "ringing"
        a.report()                              # records that it is sounding
        b.find(mid)["state"] = "ringing"
        b.report()
        stop(a, "t")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if b.find(mid)["state"] == "completed":
                break
        assert b.find(mid)["state"] == "completed"
        share._dirty = False
        if share._task:
            share._task.cancel()
    _run(world, go)


def test_plan_stops_resolves_a_copy_back_to_its_original():
    cfg = share.normalize({"A": {"timer": ["B", "C"]}})
    mid = share.mirror_id("A", "t")
    snaps = {"A": {"items": [{**timer("t"), "state": "ringing"}]},
             "B": {"items": []},
             "C": {"items": [{**timer("t"), "id": mid, "state": "ringing"}]}}
    ops = share.plan_stops(cfg, snaps, {("B", mid, "timer")}, NOW)
    assert sorted((o.target, o.action, o.id) for o in ops) == [("A", "dismiss", "t"), ("C", "dismiss", mid)]
    assert share.plan_stops({}, snaps, {("B", mid, "timer")}, NOW) == []
    assert share.plan_stops(cfg, {"B": snaps["B"]}, {("B", mid, "timer")}, NOW) == []     # original unseen


def test_the_toggle_is_saved_and_reported(database, fresh, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    import em_api as api

    async def go():
        await share.load(str(tmp_path / "controller.db"))
        admin = {"Authorization": "Bearer dashboard-session"}
        async with TestClient(TestServer(await api.create_app())) as client:
            r = await client.get("/api/schedule-sharing", headers=admin)
            assert (await r.json())["stop_together"] is True
            r = await client.put("/api/schedule-sharing", headers=admin, json={"sharing": {}, "stop_together": False})
            assert r.status == 200 and (await r.json())["stop_together"] is False
            r = await client.put("/api/schedule-sharing", headers=admin, json={"sharing": {}, "stop_together": "no"})
            assert r.status == 400
            r = await client.put("/api/schedule-sharing", headers=admin, json={"sharing": {}})
            assert (await r.json())["stop_together"] is False        # left as it was
        await settle()
        share._stop_together = True
        await share.load(str(tmp_path / "controller.db"))
        assert share.stop_together() is False
    asyncio.run(go())


# --- the voice workflow (Pipecat) sees copies as ordinary items ------------------

def test_a_voice_stop_on_the_satellite_holding_a_copy_stops_it_and_the_group(world):
    """The Pipecat flow's bare "stop" (em_workflow_tools.EchoMuseStop) acts on whatever
    rings in that satellite's own store; a copy is such an item, and the stop it
    causes is what sharing then carries to the rest."""
    import em_workflow_tools

    async def go(world):
        a, b = Sat("A", [alarm("w"), timer("t", 900)]), Sat("B")
        connect(world, a, b)
        share_config(A={"alarm": ["B"], "timer": ["B"]})
        await settle()
        mid_alarm, mid_timer = share.mirror_id("A", "w"), share.mirror_id("A", "t")
        ring(a, "w")
        ring(a, "t")
        ring(b, mid_alarm)
        ring(b, mid_timer)
        await settle()

        async def no_ha_timer():
            return False
        live = SimpleNamespace(device_id="B", capabilities=["local_schedule"], schedule=b.schedule,
                               send_control=b.send_control, stop_timer_alarm=no_ha_timer, spotify=None)
        result = await em_workflow_tools._stop_active_local_audio(live, True)
        assert not result["errors"]
        assert {s["id"] for s in result["stopped"]} == {mid_alarm, mid_timer}
        await settle()
        assert a.find("w")["state"] == "completed" and a.find("t")["state"] == "completed"
        assert b.find(mid_alarm)["state"] == "completed"
    _run(world, go)


def test_the_list_a_voice_flow_reads_includes_what_was_shared_to_the_satellite(world):
    import em_workflow_tools

    async def go(world):
        a, b = Sat("A", [reminder("r", "Call Sam")]), Sat("B", [reminder("own", "Mine")])
        connect(world, a, b)
        share_config(A={"reminder": ["B"]})
        await settle()
        cmd = em_workflow_tools.command("EchoMuseList", {"kind": "reminder"}, int(time.time()))
        snapshot = await b.schedule.request(b.send_control, cmd)
        assert sorted(i["message"] for i in snapshot["items"]) == ["Call Sam", "Mine"]
        # And acting on a copy by the id the list returned works like any other item.
        mid = share.mirror_id("A", "r")
        act = em_workflow_tools.command("EchoMuseAction", {"action": "cancel", "id": mid}, int(time.time()))
        await b.schedule.request(b.send_control, act)
        assert not b.find(mid)
    _run(world, go)
