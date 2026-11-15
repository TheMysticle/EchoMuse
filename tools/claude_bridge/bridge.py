#!/usr/bin/env python3
"""Claude Code build tasks for EchoMuse voice satellites (docs/claude-bridge.md).

Runs on the PC where the `claude` CLI is installed and signed in (a Pro or Max
plan works: nothing here needs an API key). The EchoMuse controller is its
only client:

  POST /tasks                      start a task   {device, request, title}
  GET  /tasks, /tasks/<id>         status
  POST /tasks/<id>/reply           an answer or more instructions {text}
  POST /tasks/<id>/cancel
  GET  /events?after=N&wait=S      long-poll: a task asks something, finishes or fails

Each task gets its own folder under `projects_dir` and runs
`claude -p --output-format stream-json` there, on `model` (Sonnet 5.5 by
default). The agent gets one extra tool, `ask_user`, served by this process
over MCP: the question becomes an event the controller speaks on the Echo
that started the task, the run ends, and the answer resumes the same session
(`--resume`). Standard library only; Python 3.10+.

Everything is behind a bearer token generated on first run. Run it on a
trusted LAN only.
"""
import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import printing  # noqa: E402  (beside this file; standard library only)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "echomuse-claude-bridge"
DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8790,
    "projects_dir": "~/voice-projects",
    "model": "claude-sonnet-5-5",
    "effort": "medium",                 # claude --effort: low, medium, high, xhigh, max ("" = the CLI's own)
    "claude": "claude",
    "max_running": 2,
    # "auto": Claude Code's bash sandbox when bubblewrap and socat are installed.
    "sandbox": "auto",
    # Hosts shell commands may reach inside the sandbox (web search and fetch
    # are Claude Code's own tools and are not limited by this). PyPI, so a task
    # can `pip install` into a venv in its folder.
    "sandbox_domains": ["pypi.org", "files.pythonhosted.org"],
    "permission_mode": "acceptEdits",
    # Reading and editing inside the task folder needs no rule under acceptEdits;
    # anything outside it is refused, because nobody is there to approve it.
    "allowed_tools": ["WebSearch", "WebFetch", "TodoWrite", "Bash(openscad *)"],
    "max_turn_seconds": 3600,
    # A Centauri Carbon on the LAN: {"host": "<printer IP>"} turns printing on
    # (docs/claude-bridge.md, "Printing"). Claude can slice and upload; only the
    # user, by voice, can start a print.
    "printer": {},
    "printer_poll_s": 30,
}
TEXT_MAX = 4000
PROGRESS_KEEP = 12
EVENTS_KEEP = 200
TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

SYSTEM = """You are doing a build task the user asked for out loud, through a smart speaker in their home. They are not at this computer, and whatever you send them is read aloud.

How to work:
- Work only in the current directory, which is this task's own folder ("{folder}"). Put everything you make there.
- You have the ask_user tool. Use it when the user's answer would change what you build and you cannot find it out yourself: a measurement only they can take, a preference, a choice between real options. Find out what you can first (search the web for product dimensions and specifications, for example) and offer what you found as the default: "I found the capsule is about 53 millimetres across. Shall I use that, or will you measure yours?"
- Ask ONE question at a time, short enough to say in one breath, with the options named. After calling ask_user, stop: end your turn with one short line and do no more work. The user's answer arrives as your next message.
- Ask the questions that matter early, before the long work, so the user is not kept waiting. Never ask permission for ordinary work in this folder.
- Shell commands run in a sandbox: they can write only in this folder, and reach only PyPI on the network. For Python packages, make a venv here (`python3 -m venv .venv`, then `.venv/bin/pip install --no-cache-dir ...`). OpenSCAD is installed for rendering models to STL.
- If a tool you want is not permitted, do the work another way, or say in the README what the user needs to run.

When you are finished:
- Leave a README.md in the folder: what you made, the key decisions and dimensions, and how to use it (for a 3D model: which file to print and suggested print settings).
- Your final message is spoken aloud: two or three short plain sentences. No markdown, lists, file paths or code. Say what you made, the main choice you made, and that it is in the folder "{folder}"."""

PRINT_NOTE = """

Printing: the user's 3D printer (an Elegoo Centauri Carbon, 0.4 mm nozzle, 256 x 256 x 256 mm) is connected. When a model is finished and checked, call send_to_printer with the model file (STL, 3MF or OBJ) in this folder. It slices with the printer's own profiles and puts the file on the printer; it never starts a print. Only the user starts it, by voice, once they have checked the bed. So:
- Put the part in its print orientation in the file itself: largest flat face down on Z=0, as little overhang as you can, no supports needed if you can design it that way.
- Before you call send_to_printer, ask the user which material to print in with ask_user, unless they already said: offer MATERIAL, which is what they usually have loaded, and mention another only if the part needs it (heat resistance, flexibility). Put it in the same question as anything else you need to ask. Choose the quality yourself (standard unless the part needs otherwise).
- In your final message, say it is on the printer and roughly how long it takes."""

ASK_RESULT = ("Your question will be said to the user on their speaker. End your turn now with one short line and "
              "do no more work; their answer arrives as your next message.")


def log(*parts):
    print(datetime.now().strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


EFFORTS = ("", "low", "medium", "high", "xhigh", "max")


def load_config(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not stored.get("token"):
        stored["token"] = secrets.token_urlsafe(32)
        path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        log(f"New token written to {path}")
    config = {**DEFAULTS, **stored}
    if config.get("effort") not in EFFORTS:
        log(f"Unknown effort {config.get('effort')!r} (use one of {', '.join(e for e in EFFORTS if e)}); using medium")
        config["effort"] = "medium"
    return config


def slugify(text):
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "-".join(words)[:40].strip("-") or "task"


def sandbox_on(config):
    mode = config.get("sandbox", "auto")
    if mode == "auto":
        return bool(shutil.which("bwrap") and shutil.which("socat"))
    return bool(mode)


class Task:
    FIELDS = ("id", "title", "request", "device", "folder", "status", "session_id", "question",
              "result", "error", "progress", "created", "updated", "cost_usd", "runs", "queued", "print")

    def __init__(self, **values):
        self.id = values["id"]
        self.title = values.get("title", "")
        self.request = values.get("request", "")
        self.device = values.get("device")
        self.folder = values["folder"]
        self.status = values.get("status", "starting")
        self.session_id = values.get("session_id") or str(uuid.uuid4())
        self.question = values.get("question")
        self.result = values.get("result")
        self.error = values.get("error")
        self.progress = list(values.get("progress") or [])
        self.created = values.get("created", time.time())
        self.updated = values.get("updated", time.time())
        self.cost_usd = values.get("cost_usd", 0.0)
        self.runs = values.get("runs", 0)
        self.queued = values.get("queued")
        # The print Claude prepared: {file, minutes, material, quality, state, ...}.
        # state: ready (on the printer), printing, printed, stopped, failed.
        self.print = values.get("print")
        self.agent_token = secrets.token_urlsafe(24)
        self.proc = None
        self.cancelled = False

    def save_view(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    def note(self, line):
        line = " ".join(str(line).split())[:200]
        if line and (not self.progress or self.progress[-1] != line):
            self.progress.append(line)
            del self.progress[:-PROGRESS_KEEP]
        self.updated = time.time()


class Bridge:
    def __init__(self, config):
        self.config = config
        self.root = Path(os.path.expanduser(config["projects_dir"])).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / ".bridge-state.json"
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.tasks: dict[str, Task] = {}
        self.events: list[dict] = []
        self.seq = 0
        self.claude = shutil.which(config["claude"]) or config["claude"]
        self.printer = printing.Printer(config, self.root / ".bridge-cache")
        self._load()
        for task in self.tasks.values():
            if task.print and task.print.get("state") == "printing":
                self._watch_print(task)            # the bridge restarted mid-print

    # ----- persistence -------------------------------------------------
    def _load(self):
        if not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return
        for values in data.get("tasks", []):
            task = Task(**values)
            if task.status in ("starting", "running"):
                task.status, task.error = "failed", "The bridge was restarted while it was running."
            self.tasks[task.id] = task
        self.events = [e for e in data.get("events", []) if isinstance(e, dict)]
        self.seq = max([e.get("seq", 0) for e in self.events] + [data.get("seq", 0)])

    def _save(self):
        data = {"seq": self.seq, "tasks": [t.save_view() for t in self.tasks.values()],
                "events": self.events[-EVENTS_KEEP:]}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def _event(self, kind, task, text="", **extra):
        self.seq += 1
        self.events.append({"seq": self.seq, "type": kind, "task_id": task.id, "device": task.device,
                            "title": task.title, "text": text, "delivered": False, **extra})
        del self.events[:-EVENTS_KEEP]
        self._save()
        self.changed.notify_all()

    # ----- views -------------------------------------------------------
    def view(self, task, detail=False):
        out = {"task_id": task.id, "title": task.title, "status": task.status,
               "minutes_since_update": round((time.time() - task.updated) / 60, 1)}
        if task.status == "waiting" and task.question:
            out["question"] = task.question
        if task.status == "done" and task.result:
            out["result"] = task.result
        if task.status == "failed" and task.error:
            out["problem"] = task.error
        if task.print:
            out["print"] = {k: task.print.get(k) for k in ("state", "material", "quality", "minutes") if task.print.get(k)}
        if detail:
            out["folder"] = Path(task.folder).name
            out["latest_progress"] = task.progress[-5:]
            try:
                out["files"] = sorted(p.name for p in Path(task.folder).iterdir() if not p.name.startswith("."))[:20]
            except OSError:
                out["files"] = []
            if task.queued:
                out["instructions_queued"] = True
        return out

    def list_view(self):
        with self.lock:
            tasks = sorted(self.tasks.values(), key=lambda t: t.updated, reverse=True)[:10]
            return {"tasks": [self.view(t) for t in tasks]}

    # ----- actions -----------------------------------------------------
    def running_count(self):
        return sum(1 for t in self.tasks.values() if t.status in ("starting", "running"))

    def start(self, device, request, title):
        with self.lock:
            if self.running_count() >= int(self.config["max_running"]):
                raise ValueError("Two tasks are already running on the PC; wait for one to finish or cancel one.")
            base = slugify(title or request)
            task_id = f"{base}-{secrets.token_hex(2)}"
            folder = self.root / f"{datetime.now():%Y-%m-%d}-{base}"
            n = 2
            while folder.exists():
                folder = self.root / f"{datetime.now():%Y-%m-%d}-{base}-{n}"
                n += 1
            folder.mkdir(parents=True)
            task = Task(id=task_id, title=title or base.replace("-", " "), request=request,
                        device=device, folder=str(folder))
            task.note("started")
            self.tasks[task_id] = task
            self._save()
        prompt = ("The user asked for this, by voice:\n\n" + request +
                  "\n\nWork out what is needed, ask what only the user can answer, then build it.")
        self._run(task, prompt, first=True)
        return {"task_id": task.id, "title": task.title, "folder": Path(task.folder).name, "status": "running"}

    def reply(self, task_id, text, device=None):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if device:
                task.device = device          # answer on the Echo that was used
            task.question = None
            self._clear(task)
            if task.status in ("starting", "running"):
                task.queued = ((task.queued + "\n") if task.queued else "") + text
                task.note("instructions queued")
                self._save()
                return {"task_id": task.id, "status": task.status, "note": "It will get this as soon as its current step ends."}
            if self.running_count() >= int(self.config["max_running"]):
                raise ValueError("Two tasks are already running on the PC; try again when one finishes.")
            task.status = "starting"
            task.note("got the user's answer")
            self._save()
        self._run(task, "The user said: " + text)
        return {"task_id": task.id, "status": "running"}

    def cancel(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            task.cancelled = True
            proc = task.proc
            task.status, task.question, task.queued = "cancelled", None, None
            task.note("cancelled")
            self._clear(task)
            self._save()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return {"task_id": task.id, "status": "cancelled"}

    def _clear(self, task):
        """Undelivered questions for this task are void now; tell the controller to stop saying them."""
        for event in self.events:
            if event["task_id"] == task.id and not event["delivered"]:
                event["delivered"] = True
        self._event("clear", task)

    # ----- printing ----------------------------------------------------
    def prepare_print(self, task_id, model, material="PLA", quality="standard"):
        """Claude's half: slice its model and put it on the printer. Never starts anything."""
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or task.status not in ("starting", "running"):
                raise printing.PrinterError("This task is not running.")
            folder = Path(task.folder).resolve()
        if not self.printer.enabled:
            raise printing.PrinterError("No printer is set up.")
        path = (folder / str(model)).resolve()
        if folder not in path.parents:
            raise printing.PrinterError("The model must be a file inside this task's folder.")
        if path.suffix.lower() not in printing.MODEL_SUFFIXES or not path.is_file():
            raise printing.PrinterError("Give an existing STL, 3MF or OBJ file in this folder.")
        if path.stat().st_size > printing.MAX_MODEL_BYTES:
            raise printing.PrinterError("That model file is too large.")
        with self.lock:
            if task.print and task.print.get("state") == "printing":
                raise printing.PrinterError("This task's print is running now; it cannot be replaced.")
            task.note(f"slicing {path.name} ({material}, {quality})")
        gcode, minutes = self.printer.slice(path, folder / "print", material, quality)
        remote = f"{task.id}.gcode"
        with self.lock:
            task.note("uploading to the printer")
        self.printer.upload(gcode, remote)
        with self.lock:
            task.print = {"file": remote, "local": gcode.name, "model": path.name, "minutes": minutes,
                          "material": material, "quality": quality, "state": "ready", "at": time.time()}
            task.note(f"on the printer, about {minutes} minutes" if minutes else "on the printer")
            self._save()
        log(f"[{task.id}] {path.name} sliced ({material}, {quality}, ~{minutes} min) and uploaded as {remote}")
        about = f" It takes about {minutes} minutes." if minutes else ""
        return (f"Sliced ({material}, {quality} quality) and uploaded to the printer as {remote}.{about} "
                "It has NOT started: the user starts it by voice after checking the bed. Say in your final "
                "message that it is on the printer and how long it takes.")

    def _print_task(self, task_id=None):
        """The task whose print the user means: the one named, else the newest ready one."""
        if task_id:
            task = self.tasks.get(task_id)
            if task is None or not task.print:
                raise printing.PrinterError("That task has nothing on the printer.")
            return task
        ready = [t for t in self.tasks.values() if t.print and t.print.get("state") == "ready"]
        if not ready:
            raise printing.PrinterError("No print is waiting on the printer.")
        return max(ready, key=lambda t: t.print.get("at", 0))

    def printer_view(self):
        if not self.printer.enabled:
            raise printing.PrinterError("No printer is set up.")
        status = self.printer.status()
        with self.lock:
            ours = next((t for t in self.tasks.values() if t.print and t.print.get("file") == status.get("filename")), None)
            out = printing.describe(status, ours.print.get("minutes") if ours else None)
            if ours:
                out["task_id"], out["title"] = ours.id, ours.title
            waiting = [t for t in self.tasks.values() if t.print and t.print.get("state") == "ready"]
            if waiting:
                out["waiting_to_start"] = [{"task_id": t.id, "title": t.title, "minutes": t.print.get("minutes"),
                                            "material": t.print.get("material")} for t in waiting]
        return out

    def printer_action(self, action, task_id=None):
        """The user's half, on their spoken request only (the controller's ClaudePrint)."""
        if not self.printer.enabled:
            raise printing.PrinterError("No printer is set up.")
        if action == "status":
            return self.printer_view()
        if action == "start":
            with self.lock:
                task = self._print_task(task_id)
                job = dict(task.print)
            if job.get("state") == "printing":
                raise printing.PrinterError("That print is already running.")
            problem = printing.startable_problem(self.printer.status())
            if problem:
                raise printing.PrinterError(problem)
            self.printer.start(job["file"])
            with self.lock:
                task.print.update(state="printing", started=time.time())
                task.note("printing")
                self._save()
            log(f"[{task.id}] print started on the user's request ({job['file']})")
            self._watch_print(task)
            return {"started": True, "title": task.title, "about_minutes": job.get("minutes"),
                    "material": job.get("material")}
        if action in ("pause", "resume", "stop"):
            code = self.printer.status().get("print_status")
            if action == "resume" and code != printing.PAUSED:
                raise printing.PrinterError("The printer is not paused.")
            if action in ("pause", "stop") and code in printing.STARTABLE:
                raise printing.PrinterError("The printer is not printing.")
            with self.lock:
                if action == "stop":
                    for task in self.tasks.values():
                        if task.print and task.print.get("state") == "printing":
                            task.print["state"] = "stopping"   # the watcher stays quiet about it
            try:
                self.printer.control(action)
            except printing.PrinterError:
                with self.lock:
                    for task in self.tasks.values():
                        if task.print and task.print.get("state") == "stopping":
                            task.print["state"] = "printing"
                raise
            log(f"printer: {action} on the user's request")
            return {action: True}
        raise printing.PrinterError("Unknown printer action.")

    def _watch_print(self, task):
        """Follow a print this bridge started, and say when it ends. One thread per print."""
        def watch():
            failures = 0
            seen_printing = False
            minutes = (task.print or {}).get("minutes") or 600
            deadline = time.time() + minutes * 180 + 7200
            while time.time() < deadline:
                time.sleep(float(self.config.get("printer_poll_s", 30)))
                with self.lock:
                    job = task.print or {}
                    if job.get("state") not in ("printing", "stopping"):
                        return
                try:
                    status = self.printer.status()
                    failures = 0
                except printing.PrinterError:
                    failures += 1
                    if failures >= 20:                     # printer off for ~10 minutes
                        log(f"[{task.id}] lost the printer; stopped following the print")
                        return
                    continue
                code = status.get("print_status")
                if status.get("filename") and status.get("filename") != job.get("file"):
                    return                                  # something else is printing now
                if code not in printing.STARTABLE and code != printing.ERROR:
                    seen_printing = True
                    continue
                if not seen_printing and code == printing.IDLE:
                    continue                                # not begun yet
                with self.lock:
                    stopping = job.get("state") == "stopping"
                    if code == printing.COMPLETED:
                        task.print["state"] = "printed"
                        task.note("printed")
                        self._event("printed", task, f"The {task.title} has finished printing.")
                    elif code == printing.ERROR:
                        task.print["state"] = "failed"
                        task.note("print failed")
                        self._event("print_failed", task, "The printer reported an error.")
                    else:
                        task.print["state"] = "stopped"
                        task.note("print stopped")
                        if not stopping:
                            self._event("print_failed", task, "The print was stopped on the printer.")
                    self._save()
                log(f"[{task.id}] print ended: {printing.WORDS.get(code, code)}")
                return
        threading.Thread(target=watch, daemon=True, name=f"print-{task.id}").start()

    def ask(self, task_id, question):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or task.status not in ("starting", "running"):
                return False
            task.question = " ".join(question.split())[:600]
            task.note("asked: " + task.question)
            self._save()
            return True

    def events_after(self, after, wait):
        deadline = time.time() + max(0, min(wait, 60))
        with self.lock:
            for event in self.events:
                if event["seq"] <= after:
                    event["delivered"] = True
            while True:
                fresh = [e for e in self.events if e["seq"] > after and not e["delivered"]]
                if fresh or time.time() >= deadline:
                    return {"events": [{k: v for k, v in e.items() if k != "delivered"} for e in fresh]}
                self.changed.wait(deadline - time.time())

    # ----- the claude process -------------------------------------------
    def _command(self, task, first):
        port = self.config["port"]
        mcp = {"mcpServers": {"echomuse": {
            "type": "http", "url": f"http://127.0.0.1:{port}/agent/{task.id}/mcp",
            "headers": {"Authorization": "Bearer " + task.agent_token}}}}
        tools = list(self.config["allowed_tools"]) + ["mcp__echomuse__ask_user"]
        if self.printer.enabled:
            tools.append("mcp__echomuse__send_to_printer")
        cmd = [self.claude, "-p", "--output-format", "stream-json", "--verbose",
               "--model", self.config["model"], *(["--effort", self.config["effort"]] if self.config.get("effort") else []),
               "--permission-mode", self.config["permission_mode"],
               "--append-system-prompt", SYSTEM.format(folder=Path(task.folder).name)
               + (PRINT_NOTE.replace("MATERIAL", self.printer.material) if self.printer.enabled else ""),
               "--mcp-config", json.dumps(mcp), "--strict-mcp-config"]
        if sandbox_on(self.config):
            tools.append("Bash")
            cmd += ["--settings", json.dumps({"sandbox": {
                "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                "network": {"allowedDomains": list(self.config.get("sandbox_domains") or [])}}})]
        cmd += ["--session-id", task.session_id] if first else ["--resume", task.session_id]
        cmd += ["--allowedTools", *tools]
        return cmd

    def _run(self, task, prompt, first=False):
        cmd = self._command(task, first)
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with self.lock:
            task.status, task.error, task.result = "running", None, None
            task.runs += 1
            task.cancelled = False
            task.proc = subprocess.Popen(cmd, cwd=task.folder, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         start_new_session=True)
            self._save()
        log(f"[{task.id}] run {task.runs} started ({'new' if first else 'resumed'} session, {self.config['model']})")
        threading.Thread(target=self._watch, args=(task, task.proc, prompt), daemon=True).start()

    def _watch(self, task, proc, prompt):
        stderr_tail = []

        def drain_stderr():
            for line in proc.stderr:
                stderr_tail.append(line.strip())
                del stderr_tail[:-20]
        threading.Thread(target=drain_stderr, daemon=True).start()
        timer = threading.Timer(float(self.config["max_turn_seconds"]), proc.kill)
        timer.start()
        result = None
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("type")
                if kind == "assistant":
                    for block in (message.get("message") or {}).get("content") or []:
                        if block.get("type") == "text" and block.get("text", "").strip():
                            with self.lock:
                                task.note(block["text"])
                        elif block.get("type") == "tool_use":
                            with self.lock:
                                task.note("using " + _tool_label(block))
                elif kind == "result":
                    result = message
            proc.wait()
        finally:
            timer.cancel()
        with self.lock:
            task.proc = None
            if result:
                task.cost_usd = round(task.cost_usd + float(result.get("total_cost_usd") or 0), 4)
                task.session_id = result.get("session_id") or task.session_id
            if task.cancelled:
                self._save()
                return
            if task.queued:
                text, task.queued = task.queued, None
                task.question = None
                self._save()
                threading.Thread(target=self._run, args=(task, "The user said: " + text), daemon=True).start()
                return
            ok = proc.returncode == 0 and result is not None and not result.get("is_error")
            text = " ".join(str((result or {}).get("result") or "").split())
            if task.question:
                task.status = "waiting"
                self._event("ask", task, task.question)
                log(f"[{task.id}] waiting for the user: {task.question}")
            elif ok:
                task.status, task.result = "done", text[:900]
                task.note("finished")
                ready = task.print if task.print and task.print.get("state") == "ready" else None
                self._event("done", task, task.result,
                            **({"print": {k: ready.get(k) for k in ("minutes", "material")}} if ready else {}))
                log(f"[{task.id}] done (${task.cost_usd})")
            else:
                why = text or (stderr_tail[-1] if stderr_tail else f"exit code {proc.returncode}")
                task.status, task.error = "failed", why[:400]
                task.note("failed")
                self._event("failed", task, "It stopped with an error: " + task.error)
                log(f"[{task.id}] failed: {why}")
            self._save()


def _tool_label(block):
    name = block.get("name", "a tool")
    args = block.get("input") or {}
    for key in ("query", "url", "file_path", "command", "pattern"):
        if isinstance(args.get(key), str):
            return f"{name}: {args[key][:80]}"
    return name


ASK_TOOL = {
    "name": "ask_user",
    "description": ("Ask the user one question, out loud, on the speaker they used to start this task. "
                    "Use it for what only they can answer. After calling it, end your turn at once; the "
                    "answer arrives as your next message."),
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["question"],
                    "properties": {"question": {"type": "string", "description": "One short spoken question, options named."}}},
}


SEND_TOOL = {
    "name": "send_to_printer",
    "description": ("Slice a finished model with the user's printer's own profiles and upload it to the printer. "
                    "It never starts a print: the user starts it by voice after checking the bed. Call it once the "
                    "model is final and in print orientation."),
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["model"],
                    "properties": {
                        "model": {"type": "string", "description": "STL, 3MF or OBJ file in this task's folder (relative path)."},
                        "material": {"type": "string", "enum": list(printing.MATERIALS), "default": "PLA"},
                        "quality": {"type": "string", "enum": list(printing.QUALITIES), "default": "standard",
                                    "description": "draft 0.24 mm, standard 0.20 mm, fine 0.16 mm, strong 0.20 mm with more walls and infill."}}},
}



def send_tool(material):
    """SEND_TOOL with the user's usual material as its default."""
    tool = json.loads(json.dumps(SEND_TOOL))
    tool["inputSchema"]["properties"]["material"]["default"] = material
    return tool

def agent_mcp(bridge, task_id, payload):
    """The task's own MCP server: initialize, tools/list, tools/call ask_user."""
    method = payload.get("method")
    if "id" not in payload:
        return None
    response = {"jsonrpc": "2.0", "id": payload["id"]}
    if method == "initialize":
        version = (payload.get("params") or {}).get("protocolVersion") or "2025-03-26"
        response["result"] = {"protocolVersion": version, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "EchoMuse speaker", "version": "1"}}
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": [ASK_TOOL, *([send_tool(bridge.printer.material)] if bridge.printer.enabled else [])]}
    elif method == "tools/call":
        params = payload.get("params") or {}
        args = params.get("arguments") or {}
        question = args.get("question")
        if params.get("name") == "send_to_printer" and bridge.printer.enabled:
            try:
                text = bridge.prepare_print(task_id, args.get("model", ""), args.get("material") or bridge.printer.material,
                                            args.get("quality") or "standard")
                response["result"] = {"content": [{"type": "text", "text": text}], "isError": False}
            except printing.PrinterError as error:
                response["result"] = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        elif params.get("name") != "ask_user" or not isinstance(question, str) or not question.strip():
            response["result"] = {"content": [{"type": "text", "text": "ask_user needs a question."}], "isError": True}
        elif bridge.ask(task_id, question):
            response["result"] = {"content": [{"type": "text", "text": ASK_RESULT}], "isError": False}
        else:
            response["result"] = {"content": [{"type": "text", "text": "This task is not running."}], "isError": True}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    return response


def make_handler(bridge):
    token = bridge.config["token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "EchoMuseClaudeBridge/1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, expected):
            got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return bool(expected) and hmac.compare_digest(got.encode(), expected.encode())

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                raise ValueError("Request too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return data

        def _route(self, method):
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/")
            segments = path.split("/")[1:]
            if len(segments) == 3 and segments[0] == "agent" and segments[2] == "mcp":
                task = bridge.tasks.get(segments[1])
                if task is None or not self._authorized(task.agent_token):
                    return self._send(401, {"error": "unauthorized"})
                if method != "POST":
                    return self._send(405, {"error": "POST only"})
                reply = agent_mcp(bridge, task.id, self._body())
                if reply is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._send(200, reply)
            if not self._authorized(token):
                return self._send(401, {"error": "unauthorized"})
            if method == "GET" and path == "/events":
                query = parse_qs(parts.query)
                after = int((query.get("after") or ["0"])[0])
                wait = float((query.get("wait") or ["0"])[0])
                return self._send(200, bridge.events_after(after, wait))
            if method == "GET" and path == "/tasks":
                return self._send(200, bridge.list_view())
            if path == "/printer" and method in ("GET", "POST"):
                body = self._body() if method == "POST" else {"action": "status"}
                action, task_id = body.get("action"), body.get("task_id") or None
                if task_id is not None and not (isinstance(task_id, str) and TASK_ID.match(task_id)):
                    return self._send(400, {"error": "Unknown task id."})
                try:
                    return self._send(200, bridge.printer_action(str(action), task_id))
                except printing.PrinterError as error:
                    return self._send(409, {"error": str(error)})
            if method == "POST" and path == "/tasks":
                body = self._body()
                request, title = str(body.get("request", "")).strip(), str(body.get("title", "")).strip()
                device = body.get("device")
                if not request or len(request) > TEXT_MAX or len(title) > 80 or not isinstance(device, str):
                    return self._send(400, {"error": "A request, a short title and the device are needed."})
                return self._send(200, bridge.start(device, request, title))
            if len(segments) >= 2 and segments[0] == "tasks" and TASK_ID.match(segments[1]):
                task_id = segments[1]
                if task_id not in bridge.tasks:
                    return self._send(404, {"error": "No task with that id; ClaudeStatus lists them."})
                if method == "GET" and len(segments) == 2:
                    with bridge.lock:
                        return self._send(200, bridge.view(bridge.tasks[task_id], detail=True))
                if method == "POST" and segments[2:] == ["reply"]:
                    body = self._body()
                    text = str(body.get("text", "")).strip()
                    device = body.get("device") if isinstance(body.get("device"), str) else None
                    if not text or len(text) > TEXT_MAX:
                        return self._send(400, {"error": "Say what to pass on."})
                    return self._send(200, bridge.reply(task_id, text, device))
                if method == "POST" and segments[2:] == ["cancel"]:
                    return self._send(200, bridge.cancel(task_id))
            return self._send(404, {"error": "not found"})

        def _guard(self, method):
            try:
                self._route(method)
            except ValueError as error:
                self._send(409, {"error": str(error)})
            except KeyError:
                self._send(404, {"error": "No task with that id."})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self._guard("GET")

        def do_POST(self):
            self._guard("POST")

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=CONFIG_DIR / "config.json")
    parser.add_argument("--show-token", action="store_true", help="print the token and exit")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.show_token:
        print(config["token"])
        return
    if not shutil.which(config["claude"]):
        sys.exit(f"`{config['claude']}` not found: install Claude Code and sign in (`claude`, then /login).")
    bridge = Bridge(config)
    server = ThreadingHTTPServer((config["host"], int(config["port"])), make_handler(bridge))
    server.daemon_threads = True
    log(f"Claude bridge on http://{config['host']}:{config['port']}  projects: {bridge.root}  model: {config['model']}  effort: {config.get('effort') or 'CLI default'}")
    log(f"Bash sandbox: {'on' if sandbox_on(config) else 'off (install bubblewrap and socat to allow shell commands)'}")
    if bridge.printer.enabled:
        problems = bridge.printer.problems()
        log(f"Printer {bridge.printer.host}: " + ("ready" if not problems else "NOT ready: " + "; ".join(problems)))
    log(f"Token: in {args.config} (run with --show-token to print it)")
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with bridge.lock:
            for task in bridge.tasks.values():
                if task.proc is not None and task.proc.poll() is None:
                    task.proc.terminate()


if __name__ == "__main__":
    main()
