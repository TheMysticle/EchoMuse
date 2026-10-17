"""Acknowledged transport for device-owned schedules; no controller timers."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import uuid

MAX_INPUT = 8 * 1024 * 1024
MAX_PCM = 4 * 1024 * 1024


class Bridge:
    def __init__(self, owner):
        self.owner = owner
        # One Bridge lives for one control connection, and a device whose store
        # was reset reconnects, so a revision that goes DOWN across epochs is a
        # restart, not a stale message. Readers compare epochs before they
        # compare revisions.
        self.epoch = uuid.uuid4().hex
        self.snapshot = None
        self.waiters = {}
        self.lock = asyncio.Lock()

    def update(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("owner") != self.owner:
            raise ValueError("wrong satellite snapshot")
        if not isinstance(snapshot.get("items"), list) or len(snapshot["items"]) > 64:
            raise ValueError("invalid schedule snapshot")
        revision = snapshot.get("revision")
        if type(revision) is not int:
            raise ValueError("invalid schedule snapshot")
        if self.snapshot is None or revision >= self.snapshot["revision"]:
            self.snapshot = snapshot

    def receive(self, msg):
        if msg.get("snapshot") is not None:
            self.update(msg["snapshot"])
        future = self.waiters.get(msg.get("id"))
        if future is not None and not future.done():
            future.set_result(msg)

    async def request(self, send, command=None, *, asset=None, data=None):
        async with self.lock:
            rid = uuid.uuid4().hex
            msg = {"type": "schedule_cmd", "id": rid}
            if command is not None:
                command = dict(command)
                if command.get("owner", self.owner) != self.owner:
                    raise ValueError("wrong satellite")
                command["owner"] = self.owner
                command.setdefault("request", rid)
                msg["command"] = command
            else:
                msg.update(asset=asset, data=base64.b64encode(data).decode())
            future = asyncio.get_running_loop().create_future()
            self.waiters[rid] = future
            try:
                await send(msg)
                reply = await asyncio.wait_for(future, 20)
                if reply.get("error"):
                    raise ValueError(reply["error"])
                return self.snapshot if reply.get("snapshot") is not None else {"asset": asset}
            finally:
                self.waiters.pop(rid, None)


async def convert_audio(data):
    """Decode bounded uploaded bytes, never a user-supplied path or URL."""
    if not data or len(data) > MAX_INPUT:
        raise ValueError("audio upload must be between 1 byte and 8 MiB")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        # M4A commonly stores its index after the audio. A bounded upload can
        # be cached and rewound without allowing file paths or network URLs.
        "-protocol_whitelist", "cache,pipe", "-read_ahead_limit", "-1",
        "-i", "cache:pipe:0", "-t", "43",
        "-f", "s16le", "-ar", "48000", "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(process.communicate(data), 30)
        if process.returncode or not out or len(out) > MAX_PCM:
            raise ValueError("audio must decode to at most 43 seconds of mono PCM")
        return hashlib.sha256(out).hexdigest(), out
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
