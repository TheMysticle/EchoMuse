"""Voice access to Claude Code on a PC through the claude_bridge service (docs/claude-bridge.md).

The bridge (`tools/claude_bridge/bridge.py`) runs on the user's own computer
and drives the `claude` CLI there. This module is its only client, and adds
nothing on the voice, wake or audio path:

- **Tools.** `em_workflow_tools.handle` lists `CLAUDE_TOOLS` on a Pipecat
  satellite's own MCP endpoint when the bridge is configured, and forwards a
  call here with the satellite the bearer key belongs to. The model cannot pick
  a different speaker, so the answer to a question goes back to the Echo that
  started the task.
- **Events.** `poll_loop` long-polls the bridge for "a task needs an answer"
  and "a task finished", and hands each to that Echo's `Bridge.prompt`, which
  opens a session that speaks first and then hears the answer. A busy Echo is
  retried (`RETRY_S`) for `DELIVER_FOR_S`; after that the task still holds its
  question and "how is my PC task going" reads it.

The controller connects OUT to the bridge, so no route is added to the
controller and nothing on the LAN can reach it through this. The bridge's
token is never logged.
"""
import asyncio
import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("echomuse.claude_bridge")

POLL_WAIT_S = 25
RETRY_S = 15.0
DELIVER_FOR_S = 30 * 60
CALL_TIMEOUT_S = 15
TEXT_MAX = 4000
TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


CLAUDE_TOOLS = [
    {"name": "ClaudeStart",
     "description": (
         "Start a build task on the user's PC with Claude Code, for something to be made there "
         "(a 3D-printable model, a script, a document, a small program). Use it only for a request "
         "to build or make something on the computer. BEFORE calling it, say back in one sentence "
         "what will be built and get the user's explicit yes. `request` is the whole request in "
         "detail: everything the user said about it, their own words kept. `title` is two to five "
         "words naming it. Claude Code works on its own for minutes; it may ask the user questions "
         "through this speaker, and this speaker says when it is done. Tell the user that."),
     "inputSchema": _schema({"request": {"type": "string", "maxLength": TEXT_MAX},
                             "title": {"type": "string", "maxLength": 80}}, ("request", "title"))},
    {"name": "ClaudeStatus",
     "description": (
         "How the user's PC build tasks are going. Without `task_id` it lists recent tasks; with it, "
         "one task's state, its latest progress, the question it is waiting on, or its result. "
         "Summarise in a sentence or two; never read out file paths or code."),
     "inputSchema": _schema({"task_id": {"type": "string", "maxLength": 64}})},
    {"name": "ClaudeReply",
     "description": (
         "Give a PC build task the user's answer to its question, or more instructions, or changes "
         "to a finished task. `text` is what the user said, in their own words. Use the task id the "
         "question or ClaudeStatus gave."),
     "inputSchema": _schema({"task_id": {"type": "string", "maxLength": 64},
                             "text": {"type": "string", "maxLength": TEXT_MAX}}, ("task_id", "text"))},
    {"name": "ClaudePrint",
     "description": (
         "The user's 3D printer, for prints a PC build task put on it. `start` ONLY when the user "
         "themselves says to start the print in this conversation (\"start the print\", \"print it\"); "
         "never on your own, never because a task finished. If they have not said the bed is clear and "
         "the filament is loaded, ask that first, then start on their yes. `pause`, `resume` and `stop` "
         "likewise only when asked. `status` for how a print is going. Without `task_id`, start takes "
         "the newest print waiting on the printer."),
     "inputSchema": _schema({"action": {"type": "string", "maxLength": 10,
                                        "enum": ["start", "pause", "resume", "stop", "status"]},
                             "task_id": {"type": "string", "maxLength": 64}}, ("action",))},
    {"name": "ClaudeCancel",
     "description": "Stop a PC build task. Only when the user asks for it to be stopped or cancelled.",
     "inputSchema": _schema({"task_id": {"type": "string", "maxLength": 64}}, ("task_id",))},
]
TOOL_NAMES = {tool["name"] for tool in CLAUDE_TOOLS}


def settings():
    """(url, token) when the bridge is configured, else None. Read per call: no restart to test."""
    url = os.environ.get("CLAUDE_BRIDGE_URL", "").strip().rstrip("/")
    token = os.environ.get("CLAUDE_BRIDGE_TOKEN", "").strip()
    if not url or not token or not url.startswith(("http://", "https://")):
        return None
    return url, token


def enabled():
    return settings() is not None


def check_args(name, args):
    """The arguments as the schema allows them, or ValueError with a sentence the model can use."""
    spec = next((tool for tool in CLAUDE_TOOLS if tool["name"] == name), None)
    if spec is None or not isinstance(args, dict):
        raise ValueError("Unknown tool or invalid arguments")
    schema = spec["inputSchema"]
    if set(args) - schema["properties"].keys():
        raise ValueError("Unsupported argument: " + ", ".join(sorted(set(args) - schema["properties"].keys())))
    missing = set(schema["required"]) - {k for k, v in args.items() if isinstance(v, str) and v.strip()}
    if missing:
        raise ValueError("Missing: " + ", ".join(sorted(missing)))
    out = {}
    for key, value in args.items():
        if not isinstance(value, str) or len(value) > schema["properties"][key]["maxLength"]:
            raise ValueError("Invalid " + key)
        out[key] = value.strip()
    if out.get("task_id") and not TASK_ID.match(out["task_id"]):
        raise ValueError("Unknown task id; call ClaudeStatus for the list")
    return out


async def _call(method, path, body=None, *, timeout=CALL_TIMEOUT_S):
    conf = settings()
    if conf is None:
        raise RuntimeError("The PC bridge is not configured")
    url, token = conf
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.request(method, url + path, json=body,
                                   headers={"Authorization": "Bearer " + token}) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError((data or {}).get("error") or f"Bridge answered {resp.status}")
            return data


async def tool(name, args, device_id):
    """Run one Claude tool for the satellite `device_id`; returns the MCP result object."""
    try:
        args = check_args(name, args)
        if name == "ClaudeStart":
            data = await _call("POST", "/tasks", {"device": device_id, **args})
        elif name == "ClaudeStatus":
            data = await _call("GET", "/tasks/" + args["task_id"] if args.get("task_id") else "/tasks")
        elif name == "ClaudePrint":
            if args["action"] not in ("start", "pause", "resume", "stop", "status"):
                raise ValueError("Unknown printer action")
            data = await _call("POST", "/printer", {"action": args["action"], **({"task_id": args["task_id"]} if args.get("task_id") else {})},
                               timeout=90)
        elif name == "ClaudeReply":
            data = await _call("POST", f"/tasks/{args['task_id']}/reply", {"text": args["text"], "device": device_id})
        else:
            data = await _call("POST", f"/tasks/{args['task_id']}/cancel", {})
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
    except (ValueError, RuntimeError) as error:
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {"content": [{"type": "text", "text": "The PC is not answering; it may be off or the bridge not running."}],
                "isError": True}


def listens(event):
    """Whether the Echo opens its microphone after saying this event (not for a print report)."""
    return event.get("type") not in ("printed", "print_failed")


def spoken_instruction(event):
    """What the model is asked to say for a bridge event, or None for an event not to be spoken."""
    task_id = str(event.get("task_id", ""))[:64]
    title = " ".join(str(event.get("title") or "your PC task").split())[:80]
    text = " ".join(str(event.get("text") or "").split())[:900]
    kind = event.get("type")
    if kind == "ask" and text:
        return (f'The build task "{title}" that the user started on their PC (task id {task_id}) needs an '
                f"answer from them. Ask them now, briefly and naturally, in your own voice: {text} "
                f'When they answer, call ClaudeReply with task_id "{task_id}" and their answer in their '
                "own words. If they say not now, say that is fine and call no tool.")
    if kind == "done":
        job = event.get("print") if isinstance(event.get("print"), dict) else None
        if job:
            minutes = job.get("minutes")
            material = " ".join(str(job.get("material") or "the filament").split())[:20]
            about = f", about {int(minutes)} minutes" if isinstance(minutes, int) else ""
            return (f'The build task "{title}" on the user\'s PC (task id {task_id}) has finished and its print '
                    f"is waiting on the 3D printer, not started{about}. Tell them now, in at most three short "
                    f"sentences: {text or 'it is done.'} Then tell them that when the bed is clear and "
                    f"{material} is loaded they can say \"start the print\". Only if they then clearly say to "
                    f'start it, call ClaudePrint with action "start" and task_id "{task_id}". If they want '
                    f'changes instead, call ClaudeReply with task_id "{task_id}" and what they want changed.')
        return (f'The build task "{title}" on the user\'s PC (task id {task_id}) has finished. Tell them now, '
                f"in at most three short sentences: {text or 'it is done.'} Then ask whether they want any "
                f'changes; if they do, call ClaudeReply with task_id "{task_id}" and what they want changed.')
    if kind == "printed":
        return (f'Tell the user, in one short sentence, that the 3D print of "{title}" has finished. '
                "Call no tool.")
    if kind == "print_failed":
        return (f'Tell the user, briefly, that the 3D print of "{title}" did not finish: '
                f"{text or 'it stopped.'} Call no tool.")
    if kind == "failed":
        return (f'The build task "{title}" on the user\'s PC (task id {task_id}) stopped with a problem. '
                f"Tell them briefly: {text or 'it did not finish.'} If they want it to try again or do "
                f'something differently, call ClaudeReply with task_id "{task_id}" and what they said.')
    return None


_deliveries: dict[str, asyncio.Task] = {}


async def deliver(event, devices, *, retry_s=RETRY_S, give_up_s=DELIVER_FOR_S):
    """Say one event on the Echo that started the task, retrying while it is busy."""
    instruction = spoken_instruction(event)
    device_id = event.get("device")
    if instruction is None or not isinstance(device_id, str):
        return False
    loop = asyncio.get_running_loop()
    until = loop.time() + give_up_s
    while True:
        live = devices.get(device_id)
        workflow = getattr(live, "workflow", None) if live else None
        why = await workflow.prompt(instruction, listen=listens(event)) if workflow is not None else "offline"
        if why is None:
            log.info("[%s] PC task %s: said the %s update", device_id, event.get("task_id"), event.get("type"))
            return True
        if why in ("adapter_required", "empty"):
            log.warning("[%s] PC task %s: cannot say the update (%s)", device_id, event.get("task_id"), why)
            return False
        if loop.time() + retry_s > until:
            log.info("[%s] PC task %s: gave up saying the %s update (%s); it waits in the task's status",
                     device_id, event.get("task_id"), event.get("type"), why)
            return False
        await asyncio.sleep(retry_s)


def _schedule(event, devices):
    """One delivery per task: a newer event (an answer arrived, then it finished) replaces the older."""
    key = str(event.get("task_id"))
    old = _deliveries.get(key)
    if old is not None and not old.done():
        old.cancel()
    task = asyncio.get_running_loop().create_task(deliver(event, devices))
    _deliveries[key] = task
    task.add_done_callback(lambda done, key=key: _deliveries.pop(key, None) if _deliveries.get(key) is done else None)


async def poll_loop(devices):
    """Long-poll the bridge for events while it is configured. Never raises."""
    cursor = 0
    delay = 5.0
    while True:
        if not enabled():
            await asyncio.sleep(60)
            continue
        try:
            data = await _call("GET", f"/events?after={cursor}&wait={POLL_WAIT_S}", timeout=POLL_WAIT_S + 10)
            delay = 5.0
            for event in data.get("events", []):
                if isinstance(event, dict) and isinstance(event.get("seq"), int):
                    cursor = max(cursor, event["seq"])
                    _schedule(event, devices)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.debug("PC bridge poll failed (%s); retrying in %.0fs", type(error).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120.0)
