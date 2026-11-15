"""The PC side of Claude Code by voice: tools/claude_bridge/bridge.py.

Driven end to end against a stand-in `claude` that speaks the CLI's
stream-json and calls the bridge's own MCP `ask_user`, so CI needs no
sign-in. Lives here because this is where CI runs Python.
"""
import importlib.util
import json
from pathlib import Path
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("claude_bridge", ROOT / "tools" / "claude_bridge" / "bridge.py")
bridge_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mod)
sys.path.insert(0, str(ROOT / "tools" / "claude_bridge"))   # printing.py, as bridge.py imports it

FAKE_CLAUDE = r'''#!PYTHON
import json, sys, urllib.request
args = sys.argv[1:]
prompt = sys.stdin.read()
mcp = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["echomuse"]
def out(obj):
    print(json.dumps(obj), flush=True)
if "--session-id" in args:
    sid = args[args.index("--session-id") + 1]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "ask_user", "arguments": {"question": "Blue or red?"}}}).encode()
    req = urllib.request.Request(mcp["url"], body, {**mcp["headers"], "Content-Type": "application/json"})
    answer = json.loads(urllib.request.urlopen(req).read())
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__echomuse__ask_user", "input": {}}]}})
    out({"type": "result", "is_error": False, "result": "Asked.", "session_id": sid, "total_cost_usd": 0.01})
else:
    sid = args[args.index("--resume") + 1]
    if "FAIL" in prompt:
        print("boom", file=sys.stderr)
        sys.exit(2)
    open("made.txt", "w").write(prompt)
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Writing it."}]}})
    out({"type": "result", "is_error": False, "result": "I made it in blue.", "session_id": sid, "total_cost_usd": 0.02})
'''


@pytest.fixture
def running(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE.replace("PYTHON", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    config = {**bridge_mod.DEFAULTS, "token": "t" * 40, "host": "127.0.0.1", "port": 0,
              "projects_dir": str(tmp_path / "projects"), "claude": str(fake), "sandbox": False}
    bridge = bridge_mod.Bridge(config)
    server = bridge_mod.ThreadingHTTPServer(("127.0.0.1", 0), bridge_mod.make_handler(bridge))
    config["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield bridge, f"http://127.0.0.1:{config['port']}"
    server.shutdown()


def call(base, method, path, body=None, token="t" * 40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data, method=method,
                                 headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def wait_for(base, after, kind):
    deadline = time.time() + 10
    while time.time() < deadline:
        _, data = call(base, "GET", f"/events?after={after}&wait=2")
        for event in data["events"]:
            if event["type"] == kind:
                return event
            after = max(after, event["seq"])
    raise AssertionError("no " + kind + " event")


def test_a_task_asks_resumes_on_the_answer_and_finishes(running):
    bridge, base = running
    status, started = call(base, "POST", "/tasks", {"device": "kitchen", "title": "Coffee tamper",
                                                    "request": "A tamper for my capsules"})
    assert status == 200 and started["status"] == "running"
    ask = wait_for(base, 0, "ask")
    assert ask["text"] == "Blue or red?" and ask["device"] == "kitchen" and ask["task_id"] == started["task_id"]
    _, view = call(base, "GET", "/tasks/" + started["task_id"])
    assert view["status"] == "waiting" and view["question"] == "Blue or red?"
    call(base, "POST", f"/tasks/{started['task_id']}/reply", {"text": "Blue", "device": "lounge"})
    done = wait_for(base, ask["seq"], "done")
    assert done["text"] == "I made it in blue." and done["device"] == "lounge"   # said where it was answered
    folder = Path(bridge.tasks[started["task_id"]].folder)
    assert folder.parent == bridge.root and (folder / "made.txt").read_text() == "The user said: Blue"
    assert bridge.tasks[started["task_id"]].cost_usd == pytest.approx(0.03)


def test_a_failed_run_is_reported_with_its_reason(running):
    bridge, base = running
    _, started = call(base, "POST", "/tasks", {"device": "kitchen", "title": "x", "request": "y"})
    ask = wait_for(base, 0, "ask")
    call(base, "POST", f"/tasks/{started['task_id']}/reply", {"text": "FAIL"})
    failed = wait_for(base, ask["seq"], "failed")
    assert "boom" in failed["text"]


def test_an_answer_voids_the_question_still_waiting_to_be_said(running):
    bridge, base = running
    _, started = call(base, "POST", "/tasks", {"device": "kitchen", "title": "x", "request": "y"})
    wait_for(base, 0, "ask")
    call(base, "POST", f"/tasks/{started['task_id']}/reply", {"text": "Blue"})
    # A controller that was offline asks from scratch: the old question is not offered again.
    _, data = call(base, "GET", "/events?after=0&wait=0")
    assert [e["type"] for e in data["events"]][:1] in (["clear"], ["done"])
    assert "ask" not in [e["type"] for e in data["events"]]


def test_everything_needs_the_token(running):
    bridge, base = running
    for method, path in (("GET", "/tasks"), ("GET", "/events"), ("POST", "/tasks")):
        assert call(base, method, path, {}, token=None)[0] == 401
        assert call(base, method, path, {}, token="wrong")[0] == 401


def test_the_agent_endpoint_takes_only_its_own_task_token(running):
    bridge, base = running
    _, started = call(base, "POST", "/tasks", {"device": "kitchen", "title": "x", "request": "y"})
    path = f"/agent/{started['task_id']}/mcp"
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert call(base, "POST", path, rpc)[0] == 401                       # the controller's token is not it
    status, reply = call(base, "POST", path, rpc, token=bridge.tasks[started["task_id"]].agent_token)
    assert status == 200 and [t["name"] for t in reply["result"]["tools"]] == ["ask_user"]


@pytest.mark.parametrize("path", ["/tasks/../events", "/tasks/UPPER", "/tasks/a%2Fb"])
def test_a_task_id_cannot_name_a_path(running, path):
    bridge, base = running
    assert call(base, "GET", path)[0] == 404


def test_the_command_runs_the_configured_model_in_the_task_folder_without_an_api_key(running, monkeypatch):
    bridge, base = running
    task = bridge_mod.Task(id="t-1", folder=str(bridge.root))
    cmd = bridge._command(task, first=True)
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert "--strict-mcp-config" in cmd and "--session-id" in cmd
    assert "Bash" not in cmd[cmd.index("--allowedTools") + 1:]          # no sandbox, no general shell
    bridge.config["sandbox"] = True
    cmd = bridge._command(task, first=False)
    assert "Bash" in cmd[cmd.index("--allowedTools") + 1:] and "--resume" in cmd
    sandbox = json.loads(cmd[cmd.index("--settings") + 1])["sandbox"]
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["allowedDomains"] == ["pypi.org", "files.pythonhosted.org"]


# ----- printing (docs/claude-bridge.md, "Printing") --------------------------
# A stand-in slicer and printer helper: the slicer writes the G-code header the
# real one does; the helper keeps the printer's state in a JSON file.

FAKE_SLICER = r'''#!PYTHON
import json, sys, pathlib
args = sys.argv[1:]
out = pathlib.Path(args[args.index("--outputdir") + 1])
model = args[-1]
if "bad" in model:
    (out / "result.json").write_text(json.dumps({"return_code": -100, "error_string": "Empty layers"}))
    sys.exit(1)
(out / "plate_1.gcode").write_text("; HEADER_BLOCK_START\n; HEADER_BLOCK_END\n" + "G1 X0 Y0\n" * 20000
                                  + "; estimated printing time (normal mode) = 1h 5m 30s\n; CONFIG_BLOCK_START\n")
(out / "result.json").write_text(json.dumps({"return_code": 0, "error_string": "Success."}))
'''

FAKE_HELPER = r'''#!PYTHON
import json, os, sys
path = os.environ["FAKE_PRINTER"]
st = json.load(open(path))
host, cmd, args = sys.argv[1], sys.argv[2], sys.argv[3:]
if st.get("offline"):
    print(json.dumps({"error": "TimeoutError: no route"})); sys.exit(1)
if cmd == "status":
    print(json.dumps({k: st[k] for k in ("state", "print_status", "progress", "filename")}))
elif cmd == "upload":
    st["uploads"].append(args[1]); print(json.dumps({"uploaded": args[1]}))
elif cmd == "start":
    if args[0] not in st["uploads"]:
        print(json.dumps({"error": "RuntimeError: the printer refused start (Ack 2)"})); sys.exit(1)
    st.update(print_status=13, state=1, filename=args[0], progress=0); st["started"].append(args[0])
    print(json.dumps({"start": True, "ack": 0}))
elif cmd in ("pause", "resume", "stop"):
    st["print_status"] = {"pause": 6, "resume": 13, "stop": 8}[cmd]; st["actions"].append(cmd)
    if cmd == "stop":
        st["state"] = 0
    print(json.dumps({cmd: True, "ack": 0}))
json.dump(st, open(path, "w"))
'''


def _script(path, body):
    path.write_text(body.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def printer_bridge(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    import printing as P
    for kind, name in (("machine", P.MACHINE), ("process", P.QUALITIES["standard"]),
                       ("process", P.QUALITIES["fine"]), ("filament", P.MATERIALS["PLA"]),
                       ("filament", P.MATERIALS["PETG"]), ("filament", P.MATERIALS["Rapid PETG"])):
        (profiles / kind / "ECC").mkdir(parents=True, exist_ok=True)
        (profiles / kind / "ECC" / f"{name}.json").write_text("{}")
    state = tmp_path / "printer.json"
    state.write_text(json.dumps({"state": 0, "print_status": 0, "progress": 0, "filename": "",
                                 "uploads": [], "started": [], "actions": []}))
    monkeypatch.setenv("FAKE_PRINTER", str(state))
    config = {**bridge_mod.DEFAULTS, "token": "t" * 40, "host": "127.0.0.1", "port": 0,
              "projects_dir": str(tmp_path / "projects"), "claude": sys.executable, "sandbox": False,
              "printer_poll_s": 0.05,
              "printer": {"host": "10.0.0.9", "python": sys.executable,
                          "helper": str(_script(tmp_path / "helper.py", FAKE_HELPER)),
                          "slicer": str(_script(tmp_path / "slicer", FAKE_SLICER)), "profiles": str(profiles)}}
    bridge = bridge_mod.Bridge(config)
    server = bridge_mod.ThreadingHTTPServer(("127.0.0.1", 0), bridge_mod.make_handler(bridge))
    config["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    folder = bridge.root / "2026-11-15-tamper"
    folder.mkdir(parents=True)
    (folder / "tamper.stl").write_text("solid t\nendsolid t\n")
    task = bridge_mod.Task(id="tamper-1a2b", title="Coffee tamper", device="kitchen", folder=str(folder),
                           status="running")
    bridge.tasks[task.id] = task

    def printer():
        return json.loads(state.read_text())

    def set_printer(**values):
        st = printer()
        st.update(values)
        state.write_text(json.dumps(st))
    yield bridge, f"http://127.0.0.1:{config['port']}", task, printer, set_printer
    server.shutdown()


def agent_call(base, task, name, arguments):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return call(base, "POST", f"/agent/{task.id}/mcp", rpc, token=task.agent_token)[1]["result"]


def test_the_printer_setup_is_checked(printer_bridge):
    bridge, *_ = printer_bridge
    assert bridge.printer.problems() == []
    bridge.printer.slicer = "no-such-slicer"
    assert any("no-such-slicer" in p for p in bridge.printer.problems())


def test_claude_slices_and_uploads_but_never_starts(printer_bridge):
    bridge, base, task, printer, _ = printer_bridge
    result = agent_call(base, task, "send_to_printer", {"model": "tamper.stl", "material": "PETG"})
    assert not result["isError"], result
    assert "NOT started" in result["content"][0]["text"]
    assert printer()["uploads"] == ["tamper-1a2b.gcode"] and printer()["started"] == []
    assert task.print["state"] == "ready" and task.print["minutes"] == 66 and task.print["material"] == "PETG"
    assert (Path(task.folder) / "print" / "tamper-PETG-standard.gcode").is_file()
    # the agent has no way to start: its MCP lists two tools, neither of them a start
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    names = [t["name"] for t in call(base, "POST", f"/agent/{task.id}/mcp", rpc, token=task.agent_token)[1]["result"]["tools"]]
    assert names == ["ask_user", "send_to_printer"]


@pytest.mark.parametrize("arguments, why", [
    ({"model": "../../etc/passwd.stl"}, "inside this task's folder"),
    ({"model": "/etc/hosts"}, "inside this task's folder"),
    ({"model": "missing.stl"}, "existing STL"),
    ({"model": "tamper.stl", "material": "Wood glue"}, "Unknown material"),
    ({"model": "tamper.stl", "quality": "ultra"}, "Unknown quality"),
])
def test_a_bad_model_or_setting_is_refused_with_a_reason(printer_bridge, arguments, why):
    bridge, base, task, printer, _ = printer_bridge
    result = agent_call(base, task, "send_to_printer", arguments)
    assert result["isError"] and why in result["content"][0]["text"]
    assert printer()["uploads"] == []


def test_a_slicer_failure_is_reported_and_nothing_is_uploaded(printer_bridge):
    bridge, base, task, printer, _ = printer_bridge
    (Path(task.folder) / "bad.stl").write_text("solid\nendsolid\n")
    result = agent_call(base, task, "send_to_printer", {"model": "bad.stl"})
    assert result["isError"] and "Empty layers" in result["content"][0]["text"]
    assert printer()["uploads"] == [] and task.print is None


def finish_task(bridge, task):
    """What _watch does when a run ends cleanly."""
    with bridge.lock:
        task.status = "done"
        ready = task.print if task.print and task.print.get("state") == "ready" else None
        bridge._event("done", task, "Made it.", **({"print": {k: ready.get(k) for k in ("minutes", "material")}} if ready else {}))


def test_the_done_event_says_a_print_is_waiting(printer_bridge):
    bridge, base, task, *_ = printer_bridge
    agent_call(base, task, "send_to_printer", {"model": "tamper.stl"})
    finish_task(bridge, task)
    done = wait_for(base, 0, "done")
    assert done["print"] == {"minutes": 66, "material": "PLA"}


def test_start_only_when_the_printer_is_free_then_report_the_finish(printer_bridge):
    bridge, base, task, printer, set_printer = printer_bridge
    agent_call(base, task, "send_to_printer", {"model": "tamper.stl"})
    finish_task(bridge, task)
    set_printer(print_status=13, state=1, filename="someone-else.gcode")
    status, body = call(base, "POST", "/printer", {"action": "start"})
    assert status == 409 and "printing" in body["error"] and printer()["started"] == []
    set_printer(print_status=9, state=0, filename="someone-else.gcode")           # finished, bed checked by the user
    status, body = call(base, "POST", "/printer", {"action": "start"})
    assert status == 200 and body["started"] and printer()["started"] == ["tamper-1a2b.gcode"]
    assert call(base, "POST", "/printer", {"action": "start"})[0] == 409          # not twice
    _, view = call(base, "GET", "/printer")
    assert view["printer"] == "printing" and view["title"] == "Coffee tamper"
    set_printer(progress=50)
    assert call(base, "GET", "/printer")[1]["about_minutes_left"] == 33
    set_printer(print_status=9, state=0, progress=100)
    printed = wait_for(base, 0, "printed")
    assert printed["device"] == "kitchen" and task.print["state"] == "printed"


def test_a_stop_asked_for_is_not_announced_but_one_at_the_printer_is(printer_bridge):
    bridge, base, task, printer, set_printer = printer_bridge
    agent_call(base, task, "send_to_printer", {"model": "tamper.stl"})
    call(base, "POST", "/printer", {"action": "start"})
    assert call(base, "POST", "/printer", {"action": "resume"})[0] == 409         # not paused
    assert call(base, "POST", "/printer", {"action": "stop"})[0] == 200
    time.sleep(0.4)
    _, data = call(base, "GET", "/events?after=0&wait=0")
    assert "print_failed" not in [e["type"] for e in data["events"]] and task.print["state"] == "stopped"
    # again, but stopped on the printer's own screen
    task.print["state"] = "ready"
    status, body = call(base, "POST", "/printer", {"action": "start"})
    assert status == 200, body
    set_printer(print_status=8, state=0)
    assert "stopped on the printer" in wait_for(base, 0, "print_failed")["text"]


def test_no_print_waiting_and_an_unreachable_printer_are_said_plainly(printer_bridge):
    bridge, base, task, printer, set_printer = printer_bridge
    status, body = call(base, "POST", "/printer", {"action": "start"})
    assert status == 409 and "No print is waiting" in body["error"]
    set_printer(offline=True)
    status, body = call(base, "GET", "/printer")
    assert status == 409 and "could not be reached" in body["error"]
    assert call(base, "POST", "/printer", {"action": "start", "task_id": "../x"})[0] == 400


def test_without_a_printer_there_is_no_print_tool(running):
    bridge, base = running
    task = bridge_mod.Task(id="t-1", folder=str(bridge.root), status="running")
    bridge.tasks[task.id] = task
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    names = [t["name"] for t in call(base, "POST", "/agent/t-1/mcp", rpc, token=task.agent_token)[1]["result"]["tools"]]
    assert names == ["ask_user"]
    assert "send_to_printer" not in bridge._command(task, first=True)[-1]
    assert call(base, "GET", "/printer")[0] == 409


def test_the_estimate_is_read_in_every_form():
    import printing as P
    assert P.parse_minutes("; estimated printing time (normal mode) = 12m 22s\n") == 12
    assert P.parse_minutes("; estimated printing time (normal mode) = 1d 2h 3m\n") == 1563
    assert P.parse_minutes("; estimated printing time (normal mode) = 40s\n") == 1
    assert P.parse_minutes("no estimate") is None


def test_a_printer_error_is_said(printer_bridge):
    bridge, base, task, printer, set_printer = printer_bridge
    agent_call(base, task, "send_to_printer", {"model": "tamper.stl"})
    call(base, "POST", "/printer", {"action": "start"})
    set_printer(print_status=14)
    assert "error" in wait_for(base, 0, "print_failed")["text"] and task.print["state"] == "failed"


def test_inherited_presets_are_flattened_for_the_slicer(tmp_path):
    import printing as P
    folder = tmp_path / "process" / "ECC"
    folder.mkdir(parents=True)
    (folder / "0.20mm Standard @Elegoo CC 0.4 nozzle.json").write_text(json.dumps(
        {"name": "0.20mm Standard @Elegoo CC 0.4 nozzle", "inherits": "fdm_process_elegoo_04020", "from": "system",
         "layer_height": "0.2", "compatible_printers": ["Elegoo Centauri Carbon 0.4 nozzle"]}))
    draft = folder / "0.24mm Draft @Elegoo CC 0.4 nozzle.json"
    draft.write_text(json.dumps({"name": "0.24mm Draft @Elegoo CC 0.4 nozzle",
                                 "inherits": "0.20mm Standard @Elegoo CC 0.4 nozzle", "layer_height": "0.24"}))
    printer = P.Printer({"printer": {"host": "x"}}, tmp_path / "cache")
    flat = json.loads(printer._standalone(draft).read_text())
    assert flat["layer_height"] == "0.24" and flat["from"] == "system"
    assert flat["inherits"] == "fdm_process_elegoo_04020" and flat["compatible_printers"]
    assert flat["name"] == "0.24mm Draft @Elegoo CC 0.4 nozzle"
    standard = folder / "0.20mm Standard @Elegoo CC 0.4 nozzle.json"
    assert printer._standalone(standard) == standard


def test_the_usual_material_is_offered_and_used_when_none_is_given(printer_bridge):
    bridge, base, task, printer, _ = printer_bridge
    bridge.printer.material = "Rapid PETG"
    assert "offer Rapid PETG" in bridge._command(task, first=True)[bridge._command(task, first=True).index("--append-system-prompt") + 1]
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    tools = call(base, "POST", f"/agent/{task.id}/mcp", rpc, token=task.agent_token)[1]["result"]["tools"]
    assert tools[1]["inputSchema"]["properties"]["material"]["default"] == "Rapid PETG"
    assert not agent_call(base, task, "send_to_printer", {"model": "tamper.stl"})["isError"]
    assert task.print["material"] == "Rapid PETG"
    assert (Path(task.folder) / "print" / "tamper-RapidPETG-standard.gcode").is_file()


def test_an_unknown_usual_material_is_reported_and_falls_back(printer_bridge):
    bridge, *_ = printer_bridge
    bridge.printer.material = "Unobtainium"
    assert any("Unobtainium" in p for p in bridge.printer.problems()) and bridge.printer.material == "PLA"


def test_the_effort_level_reaches_every_run(tmp_path):
    bridge = bridge_mod.Bridge({**bridge_mod.DEFAULTS, "token": "t" * 40, "projects_dir": str(tmp_path),
                                "claude": "claude", "sandbox": False})
    task = bridge_mod.Task(id="t-1", folder=str(tmp_path), status="running", session_id="s")
    for first in (True, False):
        cmd = bridge._command(task, first=first)
        assert cmd[cmd.index("--effort") + 1] == "medium"
    bridge.config["effort"] = ""
    assert "--effort" not in bridge._command(task, first=True)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"token": "x" * 40, "effort": "turbo"}))
    assert bridge_mod.load_config(path)["effort"] == "medium"
    path.write_text(json.dumps({"token": "x" * 40, "effort": "high"}))
    assert bridge_mod.load_config(path)["effort"] == "high"
