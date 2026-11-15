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
    assert json.loads(cmd[cmd.index("--settings") + 1])["sandbox"]["allowUnsandboxedCommands"] is False
