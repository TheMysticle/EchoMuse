"""
em_groups: the arrangement of speaker groups, from its edges
(docs/speaker-groups.md). The planner is pure; the key exchange and the
pushes run against fake devices.
"""

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

import _sources
import em_groups as G
import em_state


def member(did, channel="mono"):
    return {"device_id": did, "channel": channel}


def group(*ms, name="Lounge", prefer=None, gid="g1"):
    return {"id": gid, "name": name, "members": list(ms), "prefer": prefer}


def facts(did, *, connected=True, leader=True, label=None, linked=True, client="c-" , ip="10.0.0.2",
          status=None, base_os="emos", member_ok=True):
    caps = {"sendspin"}
    if member_ok:
        caps.add("group_member")
    if leader:
        caps.add("group_leader")
    return G.Facts(device_id=did, label=label if label is not None else did.upper(), connected=connected,
                   caps=frozenset(caps), base_os=base_os, ip=ip,
                   spotify={"engine": "running", "link": "linked" if linked else "unlinked"},
                   sendspin={"clientId": client + did}, group_status=status)


def fx(*fs):
    return {f.device_id: f for f in fs}


# ── The model ───────────────────────────────────────────────────────────────

def test_normalize_keeps_a_good_group_and_gives_it_an_id():
    out = G.normalize([{"name": "Lounge", "members": [member("a", "left"), member("b", "right")]}], strict=True)
    assert out[0]["name"] == "Lounge" and out[0]["id"] and out[0]["prefer"] is None
    assert [m["channel"] for m in out[0]["members"]] == ["left", "right"]


@pytest.mark.parametrize("raw,fragment", [
    ("nope", "list"),
    ([{"name": "", "members": []}], "name"),
    ([{"name": "A", "members": "x"}], "list"),
    ([{"name": "A", "members": [{"device_id": "a", "channel": "centre"}]}], "mono, left or right"),
    ([{"name": "A", "members": [{"channel": "mono"}]}], "device_id"),
    ([{"name": "A", "members": [member("a")]}, {"name": "B", "members": [member("a")]}], "only one group"),
    ([{"name": "A", "members": [member("a")]}, {"name": "a ", "members": [member("b")]}], "two groups"),
    ([{"name": "A", "members": [member(str(i)) for i in range(G.MAX_MEMBERS + 1)]}], "at most"),
    ([{"name": f"G{i}", "members": []} for i in range(G.MAX_GROUPS + 1)], "at most"),
])
def test_normalize_refuses_each_bad_save(raw, fragment):
    with pytest.raises(ValueError) as e:
        G.normalize(raw, strict=True)
    assert fragment in str(e.value)


def test_a_stored_bad_group_is_dropped_rather_than_stopping_start():
    raw = [{"name": "Good", "members": [member("a")]}, {"name": "", "members": []}, "junk"]
    assert [g["name"] for g in G.normalize(raw)] == ["Good"]


def test_prefer_must_be_a_member():
    out = G.normalize([{"name": "A", "members": [member("a")], "prefer": "zzz"}], strict=True)
    assert out[0]["prefer"] is None
    out = G.normalize([{"name": "A", "members": [member("a")], "prefer": "a"}], strict=True)
    assert out[0]["prefer"] == "a"


def test_channel_defaults_to_mono():
    out = G.normalize([{"name": "A", "members": [{"device_id": "a"}]}], strict=True)
    assert out[0]["members"][0]["channel"] == "mono"


# ── Who can do what ─────────────────────────────────────────────────────────

def test_lead_problem_names_each_reason():
    assert G.lead_problem(facts("a")) == ""
    assert "offline" in G.lead_problem(facts("a", connected=False))
    assert "emOS" in G.lead_problem(facts("a", base_os="fireos"))
    assert "emOS" in G.lead_problem(facts("a", leader=False))
    assert "not linked" in G.lead_problem(facts("a", linked=False))
    f = facts("a")
    f.spotify = None
    assert "not running" in G.lead_problem(f)
    assert "no longer exists" in G.lead_problem(G.Facts("a", exists=False))


def test_member_problem_asks_for_both_capabilities():
    assert G.member_problem(facts("a")) == ""
    assert "firmware" in G.member_problem(facts("a", member_ok=False))


# ── Choosing the leader ─────────────────────────────────────────────────────

def test_the_first_able_echo_leads_and_a_pin_beats_it():
    g = group(member("a"), member("b"))
    f = fx(facts("a"), facts("b"))
    assert G.elect(g, f, {}, 0)[0] == "a"
    g["prefer"] = "b"
    assert G.elect(g, f, {"id": "a", "lost": None}, 0)[0] == "b"


def test_a_sitting_leader_keeps_the_role_when_another_would_be_first():
    g = group(member("a"), member("b"))
    f = fx(facts("a"), facts("b"))
    assert G.elect(g, f, {"id": "b", "lost": None}, 0)[0] == "b"


def test_a_pin_that_cannot_lead_is_ignored_until_it_can():
    g = group(member("a"), member("b"), prefer="b")
    f = fx(facts("a"), facts("b", connected=False))
    assert G.elect(g, f, {}, 0)[0] == "a"


def test_a_vanished_leader_is_waited_for_then_replaced():
    g = group(member("a"), member("b"))
    f = fx(facts("a", connected=False), facts("b"))
    leader, st, note = G.elect(g, f, {"id": "a", "lost": None}, 100.0)
    assert leader is None and "Waiting for A" in note and st["lost"] == 100.0
    leader, st, _ = G.elect(g, f, st, 100.0 + G.FAILOVER_S - 1)
    assert leader is None
    leader, st, _ = G.elect(g, f, st, 100.0 + G.FAILOVER_S + 1)
    assert leader == "b" and st == {"id": "b", "lost": None}


def test_a_leader_that_returns_in_time_keeps_the_role():
    g = group(member("a"), member("b"))
    gone = fx(facts("a", connected=False), facts("b"))
    _, st, _ = G.elect(g, gone, {"id": "a", "lost": None}, 0.0)
    back = fx(facts("a"), facts("b"))
    assert G.elect(g, back, st, 5.0) == ("a", {"id": "a", "lost": None}, "")


def test_nobody_able_says_why_for_each_echo():
    g = group(member("a"), member("b"))
    f = fx(facts("a", linked=False), facts("b", connected=False))
    leader, _, note = G.elect(g, f, {}, 0)
    assert leader is None
    assert "A: Spotify is not linked on it" in note and "B: offline" in note


def test_the_first_leader_is_chosen_at_once_when_there_was_none_before():
    g = group(member("a"), member("b"))
    assert G.elect(g, fx(facts("a", linked=False), facts("b")), {}, 0)[0] == "b"


# ── The plan: statuses, overlays, instructions ──────────────────────────────

def leader_status(*members, server="srv1"):
    return {"serverId": server, "engaged": False, "members": list(members)}


def rep(did, state, client="c-", detail=""):
    return {"id": did, "clientId": client + did, "channel": "mono", "state": state, "detail": detail}


def test_one_echo_is_an_incomplete_group_and_gets_no_orders():
    p = G.plan([group(member("a"))], fx(facts("a")), {}, 0)
    assert p.views[0]["status"] == "incomplete" and p.overlays == {}


def test_no_leader_means_no_orders_to_anyone():
    p = G.plan([group(member("a"), member("b"))], fx(facts("a", linked=False), facts("b", linked=False)), {}, 0)
    assert p.views[0]["status"] == "no_leader" and p.overlays == {}
    assert p.views[0]["instruction"] == ""


def test_the_view_tells_the_user_which_spotify_device_to_choose():
    p = G.plan([group(member("a"), member("b"))], fx(facts("a", label="Kitchen"), facts("b")), {}, 0)
    v = p.views[0]
    assert v["leader"] == "a" and v["spotify_name"] == "Kitchen"
    assert v["instruction"] == "Choose “Kitchen” in Spotify."


def test_an_unlabelled_echo_is_named_by_its_serial_tail():
    f = facts("device-abcd", label="")
    assert f.name == "Echo abcd"


def test_overlays_give_the_leader_its_members_and_the_members_their_role():
    g = group(member("a", "left"), member("b", "right"), member("c", "mono"))
    p = G.plan([g], fx(facts("a"), facts("b", ip="10.0.0.3"), facts("c", ip="10.0.0.4")), {}, 0)
    lead = p.overlays["a"]
    assert lead["groupRole"] == "leader" and lead["groupLead"]["channel"] == "left"
    assert lead["groupLead"]["name"] == "Lounge"
    assert lead["groupLead"]["members"] == [
        {"id": "b", "clientId": "c-b", "host": "10.0.0.3", "port": G.SENDSPIN_PORT, "channel": "right"},
        {"id": "c", "clientId": "c-c", "host": "10.0.0.4", "port": G.SENDSPIN_PORT, "channel": "mono"}]
    assert p.overlays["b"] == {"groupRole": "member"} and p.overlays["c"] == {"groupRole": "member"}


def test_a_member_without_a_client_id_is_told_its_role_but_not_listed_yet():
    b = facts("b")
    b.sendspin = {}
    p = G.plan([group(member("a"), member("b"))], fx(facts("a"), b), {}, 0)
    assert p.overlays["b"] == {"groupRole": "member"}
    assert p.overlays["a"]["groupLead"]["members"] == []
    assert p.views[0]["members"][1]["state"] == "starting"


def test_an_offline_or_old_member_is_not_given_orders_and_is_shown_why():
    g = group(member("a"), member("b"), member("c"))
    p = G.plan([g], fx(facts("a"), facts("b", connected=False), facts("c", member_ok=False)), {}, 0)
    assert "b" not in p.overlays and "c" not in p.overlays
    states = {m["device_id"]: (m["state"], m["message"]) for m in p.views[0]["members"]}
    assert states["b"][0] == "offline" and states["c"][0] == "unsupported"
    assert "firmware" in states["c"][1]


@pytest.mark.parametrize("reports,status", [
    ([("b", "ready"), ("c", "ready")], "ready"),
    ([("b", "ready"), ("c", "unreachable")], "partial"),
    ([("b", "unreachable"), ("c", "error")], "problem"),
    ([("b", "connecting"), ("c", "needs_key")], "starting"),
    ([("b", "ready"), ("c", "busy")], "partial"),
    ([], "starting"),
])
def test_group_status_follows_what_the_leader_reports(reports, status):
    st = leader_status(*[rep(d, s) for d, s in reports])
    g = group(member("a"), member("b"), member("c"))
    p = G.plan([g], fx(facts("a", status=st), facts("b"), facts("c")), {}, 0)
    assert p.views[0]["status"] == status


def test_ready_needs_the_leader_to_have_reported_at_all():
    g = group(member("a"), member("b"))
    p = G.plan([g], fx(facts("a", status=None), facts("b")), {}, 0)
    assert p.views[0]["status"] == "starting"


def test_keys_are_wanted_only_for_a_member_whose_client_id_matches_the_report():
    g = group(member("a"), member("b"), member("c"))
    st = leader_status(rep("b", "needs_key"), rep("c", "needs_key", client="stale-"))
    p = G.plan([g], fx(facts("a", status=st), facts("b"), facts("c")), {}, 0)
    assert p.pairs == [("a", "b")]


def test_no_keys_are_wanted_before_the_leader_has_an_identity():
    g = group(member("a"), member("b"))
    st = {"serverId": "", "members": [rep("b", "needs_key")]}
    p = G.plan([g], fx(facts("a", status=st), facts("b")), {}, 0)
    assert p.pairs == []


# ── Pushing orders and minting keys against fake devices ────────────────────

class FakeDevice:
    def __init__(self, did, *, caps=("sendspin", "group_member", "group_leader"), sendspin=None, status=None):
        self.device_id, self.ip, self.capabilities = did, "10.0.0.9", list(caps)
        self.base_os = "emos"
        self.spotify = {"engine": "running", "link": "linked"}
        self.sendspin = sendspin if sendspin is not None else {"clientId": "c-" + did}
        self.group_status, self.group_pushed, self.group_waiters = status, None, {}
        self.pushed, self.sent = [], []
        self.ack = lambda msg: {"ok": True}

    async def push_config(self, **kw):
        self.pushed.append(kw)

    async def send_control(self, msg):
        self.sent.append(msg)
        fut = self.group_waiters[msg["id"]]
        r = self.ack(msg)
        if r is not None:
            G.on_ack(self, {"id": msg["id"], **r})


@pytest.fixture
def world(monkeypatch):
    devices = {}
    monkeypatch.setattr(em_state, "devices", devices)
    monkeypatch.setattr(G, "_groups", [])
    monkeypatch.setattr(G, "_states", {})
    monkeypatch.setattr(G, "_last_mint", {})

    async def labels():
        return {d: d.upper() for d in devices} | {"gone": "Gone"}, set(devices) | {"gone"}
    monkeypatch.setattr(G, "_labels", labels)
    return devices


def run(coro):
    return asyncio.run(coro)


def test_reconcile_pushes_each_part_once_and_clears_it_on_dissolve(world, monkeypatch):
    world["a"], world["b"] = FakeDevice("a"), FakeDevice("b")
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])

    async def go():
        await G.reconcile(0)
        await G.reconcile(1)
        assert [len(world[d].pushed) for d in "ab"] == [1, 1]
        assert world["a"].pushed[0]["groupRole"] == "leader" and world["b"].pushed[0] == {"groupRole": "member"}
        monkeypatch.setattr(G, "_groups", [])
        await G.reconcile(2)
        await G.reconcile(3)
        assert world["a"].pushed[-1] == {"groupRole": ""} and world["b"].pushed[-1] == {"groupRole": ""}
        assert len(world["a"].pushed) == 2 and world["a"].group_pushed is None
    run(go())


def test_a_changed_member_list_is_pushed_again(world, monkeypatch):
    world["a"], world["b"] = FakeDevice("a"), FakeDevice("b", sendspin={})
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])

    async def go():
        await G.reconcile(0)
        assert world["a"].pushed[-1]["groupLead"]["members"] == []
        world["b"].sendspin = {"clientId": "c-b"}
        await G.reconcile(1)
        assert [m["id"] for m in world["a"].pushed[-1]["groupLead"]["members"]] == ["b"]
    run(go())


def test_a_fleet_with_no_groups_sends_nothing(world):
    world["a"] = FakeDevice("a")
    run(G.reconcile(0))
    assert world["a"].pushed == [] and world["a"].sent == []


def test_a_failed_push_is_retried_next_pass(world, monkeypatch):
    world["a"], world["b"] = FakeDevice("a"), FakeDevice("b")
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])
    calls = []

    async def flaky(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise ConnectionError("gone")
    world["a"].push_config = flaky

    async def go():
        await G.reconcile(0)
        assert world["a"].group_pushed is None
        await G.reconcile(1)
        assert world["a"].group_pushed is not None and len(calls) == 2
    run(go())


def test_a_key_is_minted_for_both_ends_member_first_and_never_stored(world, monkeypatch):
    st = leader_status(rep("b", "needs_key"))
    world["a"], world["b"] = FakeDevice("a", status=st), FakeDevice("b")
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])
    run(G.reconcile(0))
    trust, key = world["b"].sent[0], world["a"].sent[0]
    assert trust["type"] == "group_trust" and trust["peer"] == "srv1"
    assert key["type"] == "group_key" and key["peer"] == "c-b"
    assert trust["psk"] == key["psk"]
    raw = base64.urlsafe_b64decode(trust["psk"] + "=" * (-len(trust["psk"]) % 4))
    assert len(raw) == 32 and "=" not in trust["psk"]
    assert trust["psk"] not in json.dumps(G._views, default=str)


def test_each_mint_is_a_fresh_secret_and_is_rate_limited(world, monkeypatch):
    st = leader_status(rep("b", "needs_key"))
    world["a"], world["b"] = FakeDevice("a", status=st), FakeDevice("b")
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])

    async def go():
        await G.reconcile(0)
        await G.reconcile(G.MINT_EVERY_S - 1)
        assert len(world["b"].sent) == 1
        await G.reconcile(G.MINT_EVERY_S + 1)
        assert len(world["b"].sent) == 2
        assert world["b"].sent[0]["psk"] != world["b"].sent[1]["psk"]
    run(go())


def test_the_leader_is_not_given_a_key_the_member_refused(world, monkeypatch):
    st = leader_status(rep("b", "needs_key"))
    world["a"], world["b"] = FakeDevice("a", status=st), FakeDevice("b")
    world["b"].ack = lambda msg: {"ok": False, "error": "no player"}
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])
    run(G.reconcile(0))
    assert world["a"].sent == []


def test_an_unanswered_key_message_gives_up_and_cleans_its_waiter(world, monkeypatch):
    st = leader_status(rep("b", "needs_key"))
    world["a"], world["b"] = FakeDevice("a", status=st), FakeDevice("b")
    world["b"].ack = lambda msg: None
    monkeypatch.setattr(G, "ACK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])
    run(G.reconcile(0))
    assert world["b"].group_waiters == {} and world["a"].sent == []


def test_the_log_never_sees_a_secret(world, monkeypatch, caplog):
    st = leader_status(rep("b", "needs_key"))
    world["a"], world["b"] = FakeDevice("a", status=st), FakeDevice("b")
    world["a"].ack = lambda msg: {"ok": False, "error": "x"}
    monkeypatch.setattr(G, "_groups", [group(member("a"), member("b"))])
    with caplog.at_level("DEBUG"):
        run(G.reconcile(0))
    assert world["b"].sent[0]["psk"] not in caplog.text


def test_an_ack_for_nothing_outstanding_is_ignored():
    dev = SimpleNamespace(group_waiters={})
    G.on_ack(dev, {"id": "zzz", "ok": True})


def test_a_status_that_is_not_an_object_clears_the_cached_one(monkeypatch):
    monkeypatch.setattr(G, "_wake", None)
    dev = SimpleNamespace(group_status={"x": 1})
    G.on_status(dev, None)
    assert dev.group_status is None
    G.on_status(dev, {"serverId": "s"})
    assert dev.group_status == {"serverId": "s"}


# ── The view and the save ───────────────────────────────────────────────────

def test_view_lists_every_echo_with_the_reason_it_cannot_join(world, monkeypatch):
    world["a"] = FakeDevice("a")
    world["b"] = FakeDevice("b", caps=("sendspin",))
    monkeypatch.setattr(G, "_groups", [group(member("a"))])
    v = run(G.view())
    by = {d["device_id"]: d for d in v["devices"]}
    assert by["a"]["can_member"] and by["a"]["group"] == "g1"
    assert not by["b"]["can_member"] and "firmware" in by["b"]["reason"]
    assert by["gone"]["connected"] is False and by["gone"]["reason"] == ""


def test_a_save_naming_a_missing_or_incapable_echo_is_refused(world):
    world["b"] = FakeDevice("b", caps=("sendspin",))
    with pytest.raises(ValueError, match="does not exist"):
        run(G.validate_members([{"name": "A", "members": [member("nope")]}]))
    with pytest.raises(ValueError, match="firmware"):
        run(G.validate_members([{"name": "A", "members": [member("b")]}]))
    run(G.validate_members([{"name": "A", "members": [member("gone")]}]))  # offline is allowed


def test_set_groups_persists_and_load_reads_it_back(monkeypatch):
    store = {}
    monkeypatch.setattr(G.db, "get_config", lambda k: store.get(k))
    monkeypatch.setattr(G.db, "set_config", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(G, "_wake", None)

    async def go():
        await G.load()
        saved = await G.set_groups([{"name": "A", "members": [member("a", "left"), member("b", "right")]}])
        monkeypatch.setattr(G, "_groups", [])
        await G.load()
        assert G.groups() == saved
        store[G.CONFIG_KEY] = "{not json"
        await G.load()
        assert G.groups() == []
    run(go())


# ── Where it is, and where it is not ────────────────────────────────────────

def test_nothing_on_the_voice_wake_or_audio_path_imports_it():
    allowed = {"em_controller", "em_routes_groups", "em_groups"}
    for path in _sources.MODULES:
        if path.stem in allowed:
            continue
        assert "em_groups" not in _sources.code_only(_sources.source(path)), f"{path.stem} uses em_groups"


def test_a_secret_never_reaches_a_log_call_or_an_event():
    import ast
    tree = _sources.tree(_sources.CONTROLLER / "em_groups.py")
    secret = {"psk", "msg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "log":
            names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
            assert not names & secret, f"line {node.lineno} logs a message that carries a secret"
    imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert "em_events" not in imports, "group state must not ride the event stream"


# ── The routes ──────────────────────────────────────────────────────────────

def _route_world(monkeypatch, role):
    import em_routes_groups as R
    from unittest.mock import AsyncMock
    monkeypatch.setattr(R.auth, "resolve_session", AsyncMock(return_value={"role": role} if role else None))
    return R


def test_anyone_signed_in_may_read_and_only_an_admin_may_save(world, monkeypatch):
    async def go():
        R = _route_world(monkeypatch, "readonly")
        assert (await R._get_groups({})).status == 200
        assert (await R._put_groups({})).status == 403
        R = _route_world(monkeypatch, None)
        assert (await R._get_groups({})).status == 401
        assert (await R._put_groups({})).status == 401
    run(go())


def test_a_save_replaces_the_groups_and_a_bad_one_changes_nothing(world, monkeypatch):
    world["a"], world["b"] = FakeDevice("a"), FakeDevice("b")
    store = {}
    monkeypatch.setattr(G.db, "get_config", lambda k: store.get(k))
    monkeypatch.setattr(G.db, "set_config", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(G, "_wake", None)

    async def go():
        await G.load()
        R = _route_world(monkeypatch, "admin")

        def req(body):
            from unittest.mock import AsyncMock
            return SimpleNamespace(json=AsyncMock(return_value=body))
        good = {"groups": [{"name": "Lounge", "members": [member("a", "left"), member("b", "right")]}]}
        r = await R._put_groups.__wrapped__(req(good))
        out = json.loads(r.text)
        assert r.status == 200 and out["groups"][0]["status"] in ("starting", "ready", "partial")
        assert out["groups"][0]["instruction"] == "Choose “A” in Spotify."
        before = list(G.groups())
        bad = {"groups": [{"name": "X", "members": [member("nope")]}]}
        r = await R._put_groups.__wrapped__(req(bad))
        assert r.status == 400 and G.groups() == before
        r = await R._put_groups.__wrapped__(req({"groups": []}))
        assert r.status == 200 and G.groups() == []
    run(go())
