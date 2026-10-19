# Copyright (c) 2024–2026, Daily. Pipecat protobuf wire schema (BSD-2-Clause).
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Controller-only Pipecat Assist raw-PCM and protobuf WebSocket routing.

Only the audio destination changes; satellite control/data sockets remain owned
by em_controller. Libraries and the format bridge ship with the controller.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import numpy as np
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from websockets.asyncio.client import connect

import em_resample

log = logging.getLogger("echomuse.workflow")
NATIVE = "home_assistant"
PIPECAT = "pipecat"
BACKOFF_RESET_SECONDS = 30.0
SPEAKER_RATE = 48000
PCM_RATES = (8000, 16000, 22050, 24000, 44100, 48000)

# How long a session may wait for the server, by what it is waiting for. Every
# server message re-arms the one that applies, so these are idle windows and
# not session lengths. A Pipecat server announces a user's speech only through
# partial transcripts (about every four words) and "thinking", so a person
# mid-sentence keeps the listening window open and a person who said nothing
# loses the session in LISTEN_IDLE_S. Native Assist ends a silent turn at 5s.
LISTEN_IDLE_S = 8.0     # mic open, nothing heard from the server
THINK_IDLE_S = 30.0     # mic gated, server working (matches the native TTS wait)
REPLY_IDLE_S = 10.0     # reply audio started, then the server went quiet
SESSION_MAX_S = 120.0   # whole conversation, continuations included
# After a recoverable pipeline error the reply may still arrive (a text-to-speech
# socket renewed and the request was re-sent). Wait this long for it, then end.
ERROR_AUDIO_S = 6.0
# ...but a pipeline that keeps reporting errors is not recovering: this many
# recoverable errors inside this many seconds is treated as fatal, and the
# connection is rebuilt as every error used to be.
ERROR_BURST_COUNT = 3
ERROR_BURST_S = 60.0
# "thinking" from the server (bot-llm-started, bot-tts-started, a tool call) can
# arrive just after the first reply audio. Ending the reply stream on it splits
# one sentence in two, and the device waits ~1s to prime the second stream: a
# 150ms word, a second of silence, then the rest. Audio this recent means the
# model is speaking, so the phase is noise; a real pause (a tool running after
# "let me check") is longer and still ends the stream.
THINK_SPLIT_S = 1.0
# Speech the server keeps producing while no session is open (it is dropped, so
# nobody hears it, but the model is still talking and answers the next wake from
# that state): ask it to stop, then give up on the connection and start fresh.
ORPHAN_STOP_S = 2.0
ORPHAN_RECYCLE_S = 6.0
# A wake this soon after sustained unsolicited speech would be answered with it.
# A stray chunk (the tail of a reply in flight) is not sustained.
ORPHAN_WAKE_S = 3.0
ORPHAN_SUSTAINED_S = 1.0
MAX_REPLY_SECONDS = 120  # reply audio queued ahead of the speaker, per reply

# What interrupts a reply. "wake": the microphone is closed to the server while
# the assistant speaks, and the Echo's own wake word (or the button) interrupts.
# "voice": the microphone stays open and the server's detector decides, which
# hears the speaker's own echo as the user.
# The Echo's endpointer says the talker stopped (wakeEndpoint). The server has
# no "user finished" message to be told, it ends a turn by hearing silence, so
# the uplink carries this much digital silence and is then closed. Closing it
# at once would leave the server's detector waiting for a quiet that never
# arrives, which is the fault the endpointer exists to cure (a TV it hears as
# speech). This is the ONLY place a device's own VAD-end reaches a pipecat
# session (device_endpoint, below) -- it never fires for native HA Assist,
# which has its own turn-ending path. Composed sessions use Silero with
# stop_secs=0.2, and realtime Gemini Live sessions use Silero
# at a FIXED stop_secs=0.3 regardless of barge-in profile
# (app/echomuse.py:vad_analyzer). 0.3s is the real floor; this constant
# must clear it with margin for every pipecat session, not just the
# composed ones. This path is controller-to-addon only (no Echo WiFi
# in the loop), so the margin is for VAD-processing/scheduling jitter, not
# link loss.
ENDPOINT_SILENCE_S = 0.4
FRAME_BYTES = 2560      # 80ms of 16kHz mono s16, the size the Echo sends
INTERRUPT_WAKE = "wake"
INTERRUPT_VOICE = "voice"


def barge_profile(config):
    """The speech-detection profile: stored, else the one that suits the mode.

    The profile sets how long the server must hear speech before it counts a
    user, and one-word commands (stop, play) are shorter than Balanced waits.
    With the microphone closed during replies there is no echo to trip a
    sensitive profile, so wake-word mode defaults to Responsive.
    """
    stored = config.get("barge_in_sensitivity")
    if stored:
        return stored
    return "balanced" if config.get("interrupt_by", INTERRUPT_WAKE) == INTERRUPT_VOICE else "responsive"


class ProtocolError(ValueError):
    """A safe, static diagnostic code (never a server response or credential)."""


def _frame_class():
    # Wire-compatible subset of Pipecat's BSD-2-Clause frames.proto:
    # https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/frames/frames.proto
    # Build with the already-required protobuf runtime, without installing the
    # Pipecat server SDK or a compiler into a running controller.
    spec = descriptor_pb2.FileDescriptorProto(name="echomuse_pipecat.proto", syntax="proto3")
    spec.package = "echomuse_pipecat"
    for name, fields in (
        ("Audio", (("audio", 3, 12), ("sample_rate", 4, 13), ("num_channels", 5, 13))),
        ("Message", (("data", 1, 9),)),
        ("Interruption", ()),
    ):
        msg = spec.message_type.add(name=name)
        for field, number, kind in fields:
            msg.field.add(name=field, number=number, type=kind)
    frame = spec.message_type.add(name="Frame")
    frame.oneof_decl.add(name="frame")
    for name, number, typename in (("audio", 2, "Audio"), ("message", 4, "Message"),
                                   ("interruption", 5, "Interruption")):
        frame.field.add(name=name, number=number, type=11,
                        type_name=f".echomuse_pipecat.{typename}", oneof_index=0)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(spec)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("echomuse_pipecat.Frame"))


Frame = _frame_class()


def transport(config):
    selected = config.get("transport", "auto")
    if selected != "auto":
        return selected
    path = urlsplit(config.get("endpoint", "")).path.rstrip("/")
    return "va_pipecat" if path.endswith("/api/assist/esphome") else "protobuf"


def public(config):
    return {k: v for k, v in config.items() if k not in {"api_key", "google_api_key", "_scheduler_url", "_scheduler_token"}} | {
        "transport": transport(config),
        "interrupt_by": config.get("interrupt_by", INTERRUPT_WAKE),
        "barge_in_sensitivity": barge_profile(config),
        "has_api_key": bool(config.get("api_key")),
        "has_google_api_key": bool(config.get("google_api_key")),
    }


def validate(body, previous):
    if not isinstance(body, dict):
        raise ValueError("Expected a configuration object")
    if set(body) - {"mode", "endpoint", "api_key", "google_api_key", "area", "target_device", "transport", "local_schedules", "barge_in_sensitivity", "interrupt_by"}:
        raise ValueError("Unknown voice workflow setting")
    result = {**previous, **body}
    if type(result.get("local_schedules", False)) is not bool:
        raise ValueError("Invalid local schedules setting")
    if result.get("interrupt_by", INTERRUPT_WAKE) not in (INTERRUPT_WAKE, INTERRUPT_VOICE):
        raise ValueError("Invalid interrupt mode")
    if result.get("barge_in_sensitivity", "balanced") not in ("responsive", "balanced", "low"):
        raise ValueError("Invalid barge-in sensitivity")
    if result.get("transport", "auto") not in ("auto", "protobuf", "va_pipecat"):
        raise ValueError("Unsupported Pipecat transport")
    if result.get("mode") not in (NATIVE, PIPECAT):
        raise ValueError("Choose Home Assistant Core or Pipecat Assist")
    for key in ("endpoint", "api_key", "google_api_key", "area", "target_device"):
        value = result.get(key, "")
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError(f"Invalid {key}")
    if result["mode"] == PIPECAT:
        url = urlsplit(result.get("endpoint", ""))
        if url.scheme not in ("ws", "wss") or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("Use a ws:// or wss:// Pipecat WebSocket endpoint without embedded credentials")
        _ = url.port  # reject malformed ports before any network I/O
    if result.get("local_schedules") and transport(result) != "va_pipecat":
        raise ValueError("Local scheduler tools require the Pipecat Assist EchoMuse adapter")
    return result


async def configure_google(config):
    """Apply an explicitly entered key through Pipecat Assist's config API.

    The upstream add-on does not read EchoMuse's custom provider-key header.
    Preserve the complete integration list and its redacted secrets.
    """
    if transport(config) != "va_pipecat" or not config.get("google_api_key"):
        return
    url = urlsplit(config["endpoint"])
    prefix = url.path.rstrip("/").removesuffix("/api/assist/esphome")
    api_url = urlunsplit(("https" if url.scheme == "wss" else "http",
                         url.netloc, prefix + "/api/assist/config", "", ""))
    token = config.get("api_key") or dict(parse_qsl(url.query)).get("token", "")
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(api_url, headers=headers, allow_redirects=False) as response:
            response.raise_for_status()
            data = await response.json()
        integrations = data["integrations"]
        gemini = next(item for item in integrations if item.get("kind") == "gemini")
        gemini.update(api_key=config["google_api_key"], enabled=True)
        async with session.put(api_url, headers=headers, json={"integrations": integrations},
                               allow_redirects=False) as response:
            response.raise_for_status()


async def handshake(config, device_id):
    headers = {"X-EchoMuse-Device": device_id,
               "X-EchoMuse-Area": config.get("area", ""),
               "X-EchoMuse-Target-Device": config.get("target_device", "")}
    if config.get("api_key"):
        headers["Authorization"] = "Bearer " + config["api_key"]
    if config.get("google_api_key") and transport(config) != "va_pipecat":
        headers["X-EchoMuse-Google-Api-Key"] = config["google_api_key"]
    if transport(config) == "va_pipecat":
        import em_db
        headers["X-EchoMuse-Satellite-Mac"] = await asyncio.to_thread(em_db.get_esphome_mac, device_id)
        headers["X-EchoMuse-Barge-In"] = barge_profile(config)
        headers["X-EchoMuse-Interrupt-By"] = config.get("interrupt_by", INTERRUPT_WAKE)
        if config.get("_scheduler_url") and config.get("_scheduler_token"):
            headers["X-EchoMuse-Scheduler-Url"] = config["_scheduler_url"]
            headers["X-EchoMuse-Scheduler-Token"] = config["_scheduler_token"]
            headers["X-EchoMuse-Local-Schedules"] = "true" if config.get("local_schedules") else "false"
            # The same source em_workflow_tools.handle()'s own response
            # envelope already uses for this -- without it, a clock-time
            # schedule parsed locally (never seeing an LLM, which is told
            # to ask when it doesn't know) silently resolves in UTC.
            if os.environ.get("TZ"):
                headers["X-EchoMuse-Timezone"] = os.environ["TZ"]
    # Header values must be HTTP-compatible. Keys never appear in URLs/logs.
    try:
        for value in headers.values():
            value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Endpoint credentials and mapping IDs must use ASCII") from None
    endpoint = config["endpoint"]
    if transport(config) == "va_pipecat":
        url = urlsplit(endpoint)
        query = dict(parse_qsl(url.query, keep_blank_values=True))
        query.setdefault("client_id", config.get("target_device") or device_id)
        endpoint = urlunsplit(url._replace(query=urlencode(query)))
    ws = await connect(endpoint, additional_headers=headers,
                       open_timeout=8, close_timeout=2, ping_interval=20,
                       ping_timeout=10, max_size=1024 * 1024)
    try:
        pong = await ws.ping()
        await asyncio.wait_for(pong, 5)
        if transport(config) == "va_pipecat":
            raw = await asyncio.wait_for(ws.recv(), 8)
            if not isinstance(raw, str):
                raise ProtocolError("expected_va_pipecat_hello")
            hello = json.loads(raw)
            if not isinstance(hello, dict):
                raise ProtocolError("invalid_va_pipecat_hello")
            audio = hello.get("audio", {})
            if (hello.get("type") != "hello" or hello.get("protocol") != "va-pipecat"
                    or hello.get("version") != 1
                    or audio.get("format") != "pcm_s16le"
                    or audio.get("input_sample_rate") != 16000
                    or audio.get("output_sample_rate") not in (8000,16000,22050,24000,44100,48000)
                    or audio.get("channels") != 1):
                raise ProtocolError("unsupported_va_pipecat_audio")
            if config.get("barge_in_sensitivity") and not hello.get("echomuse", {}).get("barge_in"):
                raise ProtocolError("echomuse_pipecat_adapter_required")
            if config.get("local_schedules") and not hello.get("echomuse", {}).get("scheduler"):
                raise ProtocolError("echomuse_pipecat_adapter_required")
            if config.get("_scheduler_url") and not hello.get("echomuse", {}).get("stop"):
                raise ProtocolError("echomuse_pipecat_adapter_required")
            ws.echomuse_audio_format = (audio["output_sample_rate"], audio["channels"])
            ws.echomuse_device_endpoint = hello.get("echomuse", {}).get("device_endpoint") is True
    except BaseException:
        await ws.close()
        raise
    return ws


async def retry_delay(seconds):
    await asyncio.sleep(seconds)


class Bridge:
    def __init__(self, device):
        self.device = device
        self.config = {"mode": NATIVE}
        self.queue = asyncio.Queue(maxsize=64)
        self.task = None
        self.ws = None
        self.status = "native"
        self.switching = False
        self.lock = asyncio.Lock()
        self.awake = False
        self.accept_audio = True
        self.session: int | None = None
        self.session_deadline = 0.0
        self.session_hard_deadline = 0.0
        self.deadline_reason = "listen_timeout"
        self.session_started = 0.0
        self.session_epoch = 0
        self.output_epoch = 0
        self.wake_sent_epoch = -1
        self.speaker_open = False
        self.button_capture = False
        self.orphan_since = None
        self.orphan_last = 0.0
        self.orphan_stopped = False
        self.last_error: str | None = None
        self.scene_task = None
        self.media_owned = False
        self.voice_owned = False
        self.session_lock = asyncio.Lock()
        self.output_queue = None
        self.playback_task = None
        self.playback_tasks = set()
        self.pending_phase = None
        self.last_reply_pcm = 0.0
        self.reply_audio = ReplyAudio()
        self.queued_bytes = 0
        self.endpointed = False
        # The session's timings for the Activity tab (see _turn_record).
        self.turn: dict | None = None
        self.turn_tasks = set()
        self.error_times: list[float] = []

    @property
    def active(self):
        return self.config["mode"] == PIPECAT

    @property
    def blocks_native(self):
        return self.active or self.switching

    def wire_config(self, config):
        if not self.active:
            return config
        # Keep the selected model and its on-device detector enabled. Only an
        # explicit button session temporarily uses diagnostic streaming: the
        # classifier stays running, and the original mode is restored at end.
        result = {**config, "aecEnabled": True}
        if self.button_capture:
            result["owwOnDevice"] = "shadow"
        return result

    def feed(self, pcm, session=None):
        if (self.switching or not self.active or not self.awake
                or self.device.muted or not self.accept_audio or self.reply_gated
                or self.endpointed or session != self.session or self.status not in {"connected", "connecting"}):
            return
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait((self.session_epoch, pcm))

    async def wake(self, session=None, *, age_ms=0, button=False, wake_info=None):
        if age_ms > 3000 or (session is not None and self.device.listen_router.is_closed(session)):
            if session is not None:
                await self.device.listen_close(session, "stale")
            return False
        if self.playback_tasks and not self.awake:
            await self.cancel_output(force=True)
        async with self.session_lock:
            return await self._wake(session, age_ms=age_ms, button=button, wake_info=wake_info)

    async def _wake(self, session=None, *, age_ms=0, button=False, wake_info=None):
        """Open only the numbered session announced by the device's wake."""
        if (not self.active or self.switching or self.device.muted
                or (not button and not self.device.wake_word_enabled)
                or age_ms > 3000
                or (session is not None and self.device.listen_router.is_closed(session))
                or self.status not in {"connected", "connecting"}):
            # Said out loud: a refused wake lights the ring and drops it again,
            # which reads as flicker, and nothing else records why.
            log.warning("[%s] Pipecat wake refused (session=%s status=%s switching=%s muted=%s "
                        "wake_word=%s age_ms=%s)", self.device.device_id, session, self.status,
                        self.switching, self.device.muted, self.device.wake_word_enabled, age_ms)
            if session is not None:
                await self.device.listen_close(session, "pipecat_unavailable")
            return False
        if (self.orphan_since is not None and self.ws is not None
                and self.orphan_last - self.orphan_since >= ORPHAN_SUSTAINED_S
                and asyncio.get_running_loop().time() - self.orphan_last < ORPHAN_WAKE_S):
            # The model is mid-runaway: whatever it is saying would play as the
            # answer to this wake (first audio 50ms after a wake, in the field,
            # where a real answer takes about 4s). Start a clean conversation
            # instead; the wake is refused and the connection rebuilt.
            log.warning("[%s] Pipecat wake refused: the server is still speaking on its own "
                        "(session=%s); reconnecting for a clean conversation",
                        self.device.device_id, session)
            self.orphan_since = None
            if session is not None:
                await self.device.listen_close(session, "pipecat_unavailable")
            await self.ws.close()
            return False
        self.orphan_since, self.orphan_stopped = None, False
        if self.awake:
            await self.interrupt()
            await self._end_session(notify=False)
        if not self.voice_owned:
            await self.device.voice_lock.acquire()
            self.voice_owned = True
        import em_player
        if not self.media_owned:
            # Native Assist uses this exact ownership/duck and deferred-command path.
            self.media_owned = True
            await em_player.interrupt(self.device.device_id)
        self.device.cancel_event.clear()
        self.session_epoch += 1
        self.awake = True
        self.accept_audio = True
        self.session = session
        self.endpointed = False
        self.session_started = asyncio.get_running_loop().time()
        self.session_hard_deadline = self.session_started + SESSION_MAX_S
        self.turn = {"t0": self.session_started, "wake": wake_info, "button": button,
                     "composed": bool(getattr(self.ws, "echomuse_device_endpoint", False)),
                     "vad_end": -1, "stt": -1, "stt_text": "", "first_audio": -1}
        self.arm(LISTEN_IDLE_S, "listen_timeout")
        self.device.listen_session = session
        if session is not None:
            # The audio may beat its wake on the separate data socket. Release
            # the router's bounded held frames in order, never another session.
            for pcm in self.device.listen_router.open(session, asyncio.get_running_loop().time()):
                self.feed(pcm, session)
            await self.device.listen_ack(session)
        if button:
            self.button_capture = True
            import em_db
            cfg = await asyncio.to_thread(em_db.get_effective_device_config, self.device.device_id)
            await self.device.mic_stop()
            await self.device.push_config(**cfg)
            await self.device.mic_start()
        await self.device.beam_lock()
        await self.device.workflow_phase("listening")
        return True

    @property
    def interrupt_effective(self):
        """How a reply can be interrupted on this Echo, which may differ from the setting.

        Only an Echo that detects its own wake word keeps scoring it while a
        reply plays. One that streams to the controller has nothing hearing
        the wake word during a session, so closing its microphone would leave
        the button as the only way in; it keeps the server's voice detection.
        """
        wanted = self.config.get("interrupt_by", INTERRUPT_WAKE)
        if wanted == INTERRUPT_WAKE and getattr(self.device, "private_listening", False):
            return INTERRUPT_WAKE
        return INTERRUPT_VOICE

    @property
    def reply_gated(self):
        """The microphone is closed to the server because a reply is being heard."""
        return (self.interrupt_effective == INTERRUPT_WAKE
                and (self.output_queue is not None or bool(self.playback_tasks)))

    def device_endpoint(self):
        """The Echo heard the talker stop: let the server's detector hear it too.

        Speech already queued goes first, then silence, then the uplink closes
        until the next reply has been heard. Without a warm reference the Echo
        sends nothing, and the server's own detection decides as before.
        """
        if (not self.active or not self.awake or self.endpointed or not self.accept_audio
                or self.reply_gated or self.device.muted
                or self.status not in {"connected", "connecting"}):
            return False
        self.endpointed = True
        if getattr(self.ws, "echomuse_device_endpoint", False):
            if self.queue.full():
                self.queue.get_nowait()
            # Ordered after captured speech, before endpoint silence. Change
            # the ring now rather than waiting for STT's transcript timeout.
            self.queue.put_nowait((self.session_epoch, None))
        frames = round(ENDPOINT_SILENCE_S * 1000 / 80)
        for _ in range(frames):
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait((self.session_epoch, bytes(FRAME_BYTES)))
        log.info("[%s] Pipecat session %s: the Echo heard the talker stop; sending %.1fs of "
                 "silence so the server ends the turn", self.device.device_id, self.session,
                 ENDPOINT_SILENCE_S)
        return True

    @property
    def speaking(self):
        """A reply is queued or still playing on the Echo."""
        return self.output_queue is not None or bool(self.playback_tasks)

    async def button(self):
        """An action-button tap: open a session, cut a reply short, or stop listening.

        The tap that interrupts speech keeps the session open for the answer.
        With nothing being said, there is nothing to interrupt, so the tap
        ends the session instead of leaving the microphone open, which is
        what the button does for a native Assist turn.
        """
        if not self.awake:
            await self.wake(button=True)
        elif self.speaking:
            await self.interrupt()
        else:
            await self.end_session(reason="button")

    @property
    def draining(self):
        """The server has finished a reply and the speaker is still playing it."""
        return self.output_queue is None and bool(self.playback_tasks)

    def arm(self, seconds, reason):
        """Set the idle deadline poll() enforces, and the name it is logged under."""
        self.session_deadline = asyncio.get_running_loop().time() + seconds
        self.deadline_reason = reason

    def touch(self):
        """Any server message restarts the idle window for what the session is waiting on."""
        if self.output_queue is not None:
            self.arm(REPLY_IDLE_S, "reply_stalled")
        elif not self.accept_audio:
            self.arm(THINK_IDLE_S, "think_timeout")
        else:
            self.arm(LISTEN_IDLE_S, "listen_timeout")

    async def end_session(self, session=None, *, reason="ended", notify=True, drain=False):
        if not drain and (session is None or session == self.session):
            await self.cancel_output()
        async with self.session_lock:
            await self._end_session(session, reason=reason, notify=notify, drain=drain)

    def _warm_resampler(self):
        """Load what the first reply needs in a thread, now, not on the event loop mid-answer."""
        if em_resample.warmed():
            return
        task = asyncio.create_task(asyncio.to_thread(em_resample.warm))
        self.turn_tasks.add(task)
        task.add_done_callback(lambda done: (self.turn_tasks.discard(done), done.cancelled() or done.exception()))

    def mark(self, key):
        """Stamp the first time a stage is reached. A clock read, once per stage."""
        if self.turn is not None and self.turn[key] < 0:
            self.turn[key] = int((asyncio.get_running_loop().time() - self.turn["t0"]) * 1000)

    def note_transcript(self, event):
        """Keep the user's final words and when they arrived, until the reply starts.

        The server already sends these (the text rides every final), so this
        costs a clock read and a decode per final. A long utterance gets
        several finals, each carrying the merged text, so the last one before
        the reply is the whole of what was heard.
        """
        turn = self.turn
        if (turn is None or event.get("role") != "user" or not event.get("final")
                or turn["first_audio"] >= 0):
            return
        try:
            text = base64.b64decode(event.get("text_b64", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return
        turn["stt_text"] = text
        turn["stt"] = int((asyncio.get_running_loop().time() - turn["t0"]) * 1000)

    def _turn_record(self, turn, reason):
        """The session as the Activity tab's row, in the shape a native turn has.

        Two pipelines share the protocol and only one has the stages. The
        composed one (Silero, streaming STT, an LLM, tools, TTS) has a
        transcript moment, so `stt_ms` is when the final words arrived. The
        live one (Gemini Live) hears and speaks in one step: its `stt_ms` is -1
        ("not reached"), and its transcript is Gemini's own side channel, kept
        for the text only. In both, first reply audio stands in for the TTS URL
        and the start of playback, so speech end to first audio is the number to
        read against a native row.
        """
        wake = turn["wake"] or {}
        if turn["button"]:
            trigger = "button"
        elif wake.get("score") is not None:
            trigger = f"wakeword-dev({wake['score']:.3f})"
        else:
            trigger = "wakeword"
        heard = turn["first_audio"] >= 0
        if reason in ("conversation_ended", "response_finished", "ended") and heard:
            outcome = "ok"
        elif not heard and turn["vad_end"] < 0:
            outcome = "no_speech"
        elif reason in ("think_timeout", "reply_stalled", "listen_timeout"):
            outcome = "timeout"
        else:
            outcome = "ok" if heard else reason
        total = int((asyncio.get_running_loop().time() - turn["t0"]) * 1000)
        composed = turn["composed"]
        return {
            "ts": time.time(), "trigger": trigger + (" · pipecat (composed)" if composed else " · pipecat (live)"),
            "wake_model": wake.get("model"), "wake_score": wake.get("score"),
            "wake_threshold": wake.get("threshold"), "noise_floor": wake.get("noise_floor"),
            "outcome": outcome, "total_ms": total,
            "vad_end_ms": turn["vad_end"], "stt_ms": turn["stt"] if composed else -1,
            "tts_url_ms": turn["first_audio"], "tts_fetch_ms": -1,
            "playback_ms": turn["first_audio"], "stt_text": turn["stt_text"],
        }

    def record_turn(self, turn, reason):
        """Write the row in the background: the next wake must not wait on a database."""
        record = getattr(self.device, "workflow_turn", None)
        if turn is None or record is None:
            return
        async def write():
            try:
                await record(self._turn_record(turn, reason))
            except Exception:
                log.warning("[%s] could not record the Pipecat turn", self.device.device_id, exc_info=True)
        task = asyncio.create_task(write())
        self.turn_tasks.add(task)
        task.add_done_callback(self.turn_tasks.discard)

    async def _end_session(self, session=None, *, reason="ended", notify=True, drain=False):
        if not self.awake or (session is not None and session != self.session):
            return
        self.awake = False  # close the uplink synchronously before any await
        turn, self.turn = self.turn, None
        log.info("[%s] Pipecat session %s ended: %s after %.1fs%s",
                 self.device.device_id, session if session is not None else self.session,
                 reason, asyncio.get_running_loop().time() - self.session_started,
                 " (draining reply)" if drain else "")
        if not drain:
            self.session_epoch += 1
        old_session, self.session = self.session, None
        while not self.queue.empty():
            self.queue.get_nowait()
        if old_session is not None:
            await self.device.listen_close(old_session, reason)
        if notify and self.ws and self.status == "connected" and transport(self.config) == "va_pipecat":
            try:
                await self.ws.send('{"type":"stop"}')
            except Exception:
                pass
        try:
            if drain:
                self.finish_output()
                if self.playback_tasks:
                    await asyncio.gather(*list(self.playback_tasks), return_exceptions=True)
            else:
                await self.cancel_output()
        finally:
            if self.media_owned:
                self.media_owned = False
                import em_player
                try:
                    await em_player.resume_interrupted(self.device.device_id)
                finally:
                    if self.voice_owned:
                        self.voice_owned = False
                        self.device.voice_lock.release()
        if self.button_capture:
            self.button_capture = False
            import em_db
            cfg = await asyncio.to_thread(em_db.get_effective_device_config, self.device.device_id)
            await self.device.mic_stop()
            await self.device.push_config(**cfg)
            await self.device.mic_start()
        await self.device.workflow_release()
        await self.device.workflow_phase("idle")
        self.record_turn(turn, reason)

    async def poll(self):
        """Keep wake events and device deadlines live during full-duplex audio."""
        if self.switching:
            await asyncio.sleep(.1)
            return
        if self.awake:
            now = asyncio.get_running_loop().time()
            if self.device.muted or not self.device.wake_word_enabled:
                await self.end_session(reason="muted")
            elif now >= self.session_hard_deadline > 0:
                await self.end_session(reason="session_max")
            elif now >= self.session_deadline and not self.draining:
                reason = self.deadline_reason
                if reason == "reply_stalled":
                    # Let what was already received finish; only the wait is over.
                    self.finish_output()
                    await self.end_session(reason=reason, drain=True)
                else:
                    await self.end_session(reason=reason)
        try:
            ev = await asyncio.wait_for(self.device.listen_wakes.get(), .2)
        except asyncio.TimeoutError:
            ev = None
        if ev is not None:
            await self.device.workflow_wake(ev)
        while not self.device.mic_queue.empty():
            self.device.mic_queue.get_nowait()
            self.device.frames_seen_this_connection = True

    async def stop(self):
        await self.end_session(reason="workflow_stopped")
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.ws:
            await self.ws.close()
            self.ws = None
        while not self.queue.empty():
            self.queue.get_nowait()

    async def apply(self, config, ws=None):
        self.switching = True
        try:
            await self._apply(config, ws)
        finally:
            self.switching = False

    async def _apply(self, config, ws=None):
        # Caller serializes apply/save with lock. Set the routing guard BEFORE
        # yielding: in-flight HA send loops must stop at their next iteration.
        was_active = self.active
        self.config = config
        import em_esphome
        em_esphome.cancel_voice_turn(self.device.device_id, abort_ha=True, reason="workflow_changed")
        self.device.cancel_event.set()
        await self.stop()
        # Wait for existing native turn cleanup before changing capture mode.
        async with self.device.voice_lock:
            for queue in (self.device.mic_queue, self.device.voice_queue):
                while not queue.empty():
                    queue.get_nowait()
            self.device.oww_paused.clear()
            self.device.oww_paused_since = None
            self.device.cancel_event.clear()
            self.device.listen_router.reset()
            await self.device.workflow_phase("idle")
            await self.device.send_control({"type": "speaker_flush"})
            await self.device.send_data(b"\x03")
            await self.device.mic_stop()
            if self.active:
                # Reapply the user's effective wake settings, including undoing
                # the forced detector disable from the first workflow build.
                import em_db
                cfg = await asyncio.to_thread(em_db.get_effective_device_config, self.device.device_id)
                await self.device.push_config(**cfg)
                self.ws = ws
                self.status = "connecting"
                self.task = asyncio.create_task(self.run())
            else:
                import em_db
                cfg = await asyncio.to_thread(em_db.get_effective_device_config, self.device.device_id)
                await self.device.push_config(**cfg)
                self.status = "native"
            await self.device.mic_start()
        if was_active or self.active:
            await self.device._set_speaking(False)

    async def run(self):
        delay = 1
        while self.active:
            sender = None
            started = asyncio.get_running_loop().time()
            try:
                if self.ws is None:
                    self.ws = await handshake(self.config, self.device.device_id)
                # A connection is not a wake. Warm the transport without sending
                # wake, microphone PCM, or unsolicited output to the satellite.
                self.status = "connected"
                self.last_error = None
                self._warm_resampler()
                sender = asyncio.create_task(self.send(self.ws))
                await self.receive(self.ws)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.status = "unavailable"
                if isinstance(error, ProtocolError):
                    self.last_error = str(error)
                else:
                    # Exception text can contain URLs, headers or model keys.
                    self.last_error = type(error).__name__
                log.warning("[%s] Pipecat audio unavailable (%s); retry in %ss",
                            self.device.device_id, self.last_error, delay)
            finally:
                if sender:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                await self.end_session(reason="pipecat_disconnected", notify=False)
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                while not self.queue.empty():
                    self.queue.get_nowait()
            self.status = "reconnecting"
            # An upgrade/pong followed immediately by a protocol or pipeline
            # failure is not recovery. Reset only after a stable connection.
            if asyncio.get_running_loop().time() - started >= BACKOFF_RESET_SECONDS:
                delay = 1
            await retry_delay(delay)
            delay = min(delay * 2, 30)

    async def send(self, ws):
        try:
            while True:
                epoch, pcm = await self.queue.get()
                if (self.device.muted or not self.awake or not self.accept_audio
                        or self.reply_gated or epoch != self.session_epoch):
                    continue
                if self.wake_sent_epoch != epoch:
                    if transport(self.config) == "va_pipecat":
                        await ws.send('{"type":"wake"}')
                    self.wake_sent_epoch = epoch
                if (not self.awake or self.device.muted or not self.accept_audio
                        or self.reply_gated or epoch != self.session_epoch):
                    continue
                if transport(self.config) == "va_pipecat":
                    if pcm is None:
                        await self.device.workflow_phase("thinking")
                        continue
                    await ws.send(pcm)
                    continue
                frame = Frame()
                frame.audio.audio = pcm
                frame.audio.sample_rate = 16000
                frame.audio.num_channels = 1
                await ws.send(frame.SerializeToString())
        except asyncio.CancelledError:
            raise
        except Exception:
            await ws.close()

    async def interrupt(self):
        self.output_epoch += 1
        self.endpointed = False
        if self.ws and self.status == "connected":
            try:
                if transport(self.config) == "va_pipecat":
                    await self.ws.send('{"type":"interrupt"}')
                else:
                    frame = Frame()
                    frame.interruption.SetInParent()
                    await self.ws.send(frame.SerializeToString())
            except Exception:
                pass  # reconnect loop owns audio failures
        await self.cancel_output(force=True)
        if self.awake:
            await self.device.workflow_phase("listening")

    def finish_output(self, phase=None):
        if phase:
            self.pending_phase = phase
        tail = self.reply_audio.end()
        queue, self.output_queue = self.output_queue, None
        if queue is not None:
            if tail:
                self.queued_bytes += len(tail)
                queue.put_nowait(tail)
            queue.put_nowait(None)

    async def cancel_output(self, force=False):
        had_audio = bool(self.playback_tasks)
        self.output_queue = None
        self.queued_bytes = 0
        self.reply_audio.end()
        self.pending_phase = None
        tasks = list(self.playback_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.playback_tasks.clear()
        self.playback_task = None
        if had_audio or force:
            await self.device.send_control({"type": "speaker_flush"})
            await self.device.send_data(b"\x03")
            await self.device._set_speaking(False)
        self.speaker_open = False

    def output_pcm(self, pcm):
        if self.output_queue is None:
            queue = asyncio.Queue()
            previous = self.playback_task
            epoch = (self.session_epoch, self.output_epoch)
            async def chunks():
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        return
                    self.queued_bytes = max(0, self.queued_bytes - len(chunk))
                    yield chunk
            async def play():
                try:
                    if previous:
                        await previous
                    if epoch != (self.session_epoch, self.output_epoch):
                        return
                    self.speaker_open = True
                    await self.device.workflow_playback(chunks())
                    if epoch == (self.session_epoch, self.output_epoch):
                        self.speaker_open = False
                        if self.awake and self.pending_phase:
                            phase, self.pending_phase = self.pending_phase, None
                            await self.device.workflow_phase(phase)
                        if self.awake:
                            # The user can only answer once the reply has been
                            # heard, so the listening window starts here and
                            # not when the server finished sending it.
                            self.endpointed = False
                            self.touch()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The transport owner handles diagnostics/reconnection.
                    if self.ws:
                        await self.ws.close()
                    raise
                finally:
                    self.playback_tasks.discard(asyncio.current_task())
            self.mark("first_audio")
            task = asyncio.create_task(play())
            # Retrieve errors even if the remote connection closes first.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self.playback_task = task
            self.playback_tasks.add(task)
            self.output_queue = queue
        # Bounded by seconds of audio, not by message count: a live model can
        # produce faster than the paced speaker consumes, and message sizes vary
        # from 10ms to a second, so a count either drops the link on a burst or
        # allows nothing useful.
        if self.queued_bytes + len(pcm) > MAX_REPLY_SECONDS * SPEAKER_RATE * 2:
            raise ProtocolError("speaker_backpressure")
        self.queued_bytes += len(pcm)
        self.last_reply_pcm = asyncio.get_running_loop().time()
        self.output_queue.put_nowait(pcm)

    async def on_event(self, event):
        """Apply one va-pipecat JSON message while a session is open."""
        if event.get("type") == "transcript":
            self.note_transcript(event)
        elif event.get("phase") == "thinking":
            # The server's own speech-stop is the first "thinking" of a turn, and
            # it is the end of speech a native row means by vad_end_ms. The Echo's
            # end-of-speech marker is later (it waits out its own quiet period),
            # so anchoring on it hid a streaming STT that had already finished.
            self.mark("vad_end")
        if event.get("type") == "interrupt":
            await self.cancel_output(force=True)
            await self.device.workflow_phase("listening")
            return
        phase = event.get("phase")
        if (phase == "listening" and self.endpointed
                and getattr(self.ws, "echomuse_device_endpoint", False)
                and not event.get("follow_up") and not event.get("continuation_question")):
            # A delayed VAD event cannot reopen an utterance the Echo ended.
            # A new wake clears endpointed; a real clarification is explicit.
            return
        if (phase == "thinking" and not event.get("terminal") and self.output_queue is not None
                and asyncio.get_running_loop().time() - self.last_reply_pcm < THINK_SPLIT_S):
            return
        normal_reply_done = (phase == "listening" and event.get("follow_up")
                             and not event.get("continuation_question"))
        if normal_reply_done or phase in {"idle", "thanks"} or event.get("terminal"):
            # Stop the uplink now; let the acknowledged speaker tail finish
            # before releasing music ownership and restoring the idle ring.
            await self.end_session(reason="conversation_ended", drain=True)
        elif phase in {"listening", "thinking"}:
            if phase == "thinking":
                self.accept_audio = False
                while not self.queue.empty():
                    self.queue.get_nowait()
            else:
                # Every "listening" reaching this branch wants audio: a
                # clarifying question (continuation_question) and a mid-turn
                # barge are the same signal from here. The addon's own VAD
                # emits this the instant it hears the user talk over
                # "thinking" (bot-interrupted only fires once TTS has
                # actually started, so a barge before that has no other
                # event). Without this the gate from the branch above stays
                # shut: the Echo keeps streaming the rest of that utterance,
                # every frame is dropped at feed()/send(), the addon's VAD
                # starves on the leading edge it already saw and force-stops
                # ~1s later with next to nothing transcribed, and the turn
                # dies silently -- measured 2026-10-18, a spoken "stop"
                # landing right as the previous turn entered "thinking".
                self.accept_audio = True
            if self.playback_tasks:
                self.finish_output(phase)
            else:
                await self.device.workflow_phase(phase)
        # "speaking" is raised by actual PCM, not speculative server state.

    async def unsolicited_audio(self, ws, now):
        """Server speech with no session open: stop it, and if it goes on, start over.

        A reply that was in flight when a session closed is a few frames and
        stops by itself. A model that keeps producing turns is a connection to
        discard: the next wake would otherwise be answered from that state.
        """
        if self.orphan_since is None or now - self.orphan_last > 1.0:
            self.orphan_since, self.orphan_stopped = now, False
        self.orphan_last = now
        elapsed = now - self.orphan_since
        if elapsed >= ORPHAN_RECYCLE_S:
            self.orphan_since = None
            log.warning("[%s] Pipecat kept speaking for %.1fs with no session open; "
                        "reconnecting for a clean conversation", self.device.device_id, elapsed)
            raise ProtocolError("unsolicited_pipecat_speech")
        if elapsed >= ORPHAN_STOP_S and not self.orphan_stopped:
            self.orphan_stopped = True
            log.warning("[%s] Pipecat is speaking with no session open (%.1fs); asking it to stop",
                        self.device.device_id, elapsed)
            try:
                await ws.send('{"type":"stop"}')
            except Exception:
                pass

    async def receive(self, ws):
        loop = asyncio.get_running_loop()
        raw_pcm = transport(self.config) == "va_pipecat"
        deadline = 0.0
        while True:
            try:
                # Only the generic protobuf transport has no end-of-reply
                # message, so only it infers one from a gap. va-pipecat says
                # when a reply ends; a pause inside a reply (a tool call, a
                # slow model) must not end the session, and a server that goes
                # quiet for good is caught by REPLY_IDLE_S in poll().
                timeout = (max(.001, deadline - loop.time())
                           if self.output_queue is not None and not raw_pcm else None)
                raw = await asyncio.wait_for(ws.recv(), timeout)
            except asyncio.TimeoutError:
                self.finish_output()
                await self.end_session(reason="response_finished", drain=True)
                continue
            if raw_pcm and isinstance(raw, str):
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ProtocolError("invalid_va_pipecat_event")
                if event.get("type") == "error":
                    now = loop.time()
                    self.error_times = [t for t in self.error_times if now - t < ERROR_BURST_S] + [now]
                    if event.get("recoverable") is not True or len(self.error_times) >= ERROR_BURST_COUNT:
                        raise ProtocolError("pipecat_pipeline_error")
                    # A vendor hiccup (a TTS socket closing on its idle timeout was
                    # the first one seen, 2026-10-19) is not a reason to drop the
                    # connection: that cost a rebuild of the whole pipeline and ended
                    # a turn whose answer was already written. Keep the connection,
                    # give the turn a short window for a late reply, then end it.
                    log.warning("[%s] Pipecat reported a recoverable error (%s); keeping the connection",
                                self.device.device_id, event.get("code"))
                    if self.awake and self.output_queue is None:
                        self.arm(ERROR_AUDIO_S, "error_recovery")
                    continue
                if self.awake:
                    await self.on_event(event)
                    if self.awake:
                        self.touch()
                continue
            if not self.awake:
                if raw_pcm and isinstance(raw, bytes):
                    await self.unsolicited_audio(ws, loop.time())
                continue
            if raw_pcm:
                rate, channels = ws.echomuse_audio_format
                pcm = self.reply_audio.push(raw, rate, channels)
            else:
                if not isinstance(raw, bytes):
                    raise ProtocolError("expected_protobuf_received_text")
                frame = Frame.FromString(raw)
                kind = frame.WhichOneof("frame")
                if kind == "interruption":
                    await self.cancel_output(force=True)
                    await self.device.workflow_phase("listening")
                    continue
                if kind != "audio":
                    continue
                audio = frame.audio
                pcm = self.reply_audio.push(audio.audio, audio.sample_rate, audio.num_channels)
            if pcm:
                self.accept_audio = True  # audio means the server is no longer thinking
                if self.interrupt_effective == INTERRUPT_WAKE:
                    # Whatever was captured before the reply began must not
                    # follow it to the server; the gate keeps new audio out.
                    while not self.queue.empty():
                        self.queue.get_nowait()
                deadline = loop.time() + .2
                self.output_pcm(pcm)
                self.touch()


def _mono_samples(pcm, rate, channels):
    if rate not in PCM_RATES or channels not in (1, 2):
        raise ValueError("Unsupported Pipecat PCM format")
    if len(pcm) % (2 * channels):
        raise ValueError("Unaligned Pipecat PCM frame")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    return samples


class ReplyAudio:
    """Converts one reply's PCM messages to the speaker's 48kHz mono, seamlessly.

    A reply arrives as many small messages and is one signal: the resampler
    keeps its filter across them and is dropped when the reply ends or is cut
    off. A format change mid-reply starts a new signal rather than mixing rates.
    """

    def __init__(self):
        self._format = None
        self._resampler = None

    def push(self, pcm, rate, channels):
        samples = _mono_samples(pcm, rate, channels)
        head = b""
        if self._format != (rate, channels):
            head = self.end()
            self._format = (rate, channels)
            self._resampler = em_resample.StreamResampler(rate, SPEAKER_RATE)
        return head + em_resample.to_int16(self._resampler.process(samples))

    def end(self):
        """Return the held tail of the reply (a few ms) and forget it."""
        resampler, self._resampler, self._format = self._resampler, None, None
        return em_resample.to_int16(resampler.flush()) if resampler else b""


def speaker_pcm(pcm, rate, channels):
    """One self-contained buffer to speaker format. Streams use ReplyAudio."""
    audio = ReplyAudio()
    return audio.push(pcm, rate, channels) + audio.end()
