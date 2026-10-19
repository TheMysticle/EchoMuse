"""
Spotify routes: the on-device Connect engine (what the controller holds, what
a device has, installing it, the feasibility probe, transport commands) and
the controller's Web API connection (PKCE, paste-back).

The install logic is em_spotify_install, shared with the connect-time
reconcile; the planning and the protocol are em_spotify_assets, em_spotify
and em_spotify_web, which are pure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import tempfile
import time
from typing import Optional

import aiohttp
from aiohttp import web

import em_auth as auth
import em_http
import em_shell
import em_spotify
import em_spotify_assets
import em_spotify_install
import em_spotify_web
import em_state
import em_tasks

log = logging.getLogger("echomuse.api")


def register(app: web.Application) -> None:
    # On-device Spotify Connect engine (device/spotify/). Uploaded here
    # rather than shipped in the firmware: the engine is ~4MB and an OTA
    # writes both A/B slots, so folding it in would double every update's
    # payload whether or not Spotify changed.
    app.router.add_get("/api/spotify/engine",                _get_spotify_engine)
    app.router.add_post("/api/spotify/engine/upload",        _post_spotify_engine_upload)
    app.router.add_delete("/api/spotify/engine/{name}",      _delete_spotify_engine)
    app.router.add_get("/api/devices/{id}/spotify/engine",   _get_device_spotify_engine)
    app.router.add_post("/api/devices/{id}/spotify/engine",  _post_device_spotify_engine)
    app.router.add_post("/api/devices/{id}/spotify/probe",   _post_device_spotify_probe)
    app.router.add_post("/api/devices/{id}/spotify/cmd",     _post_device_spotify_cmd)
    app.router.add_get("/api/spotify/web",                   _get_spotify_web)
    app.router.add_post("/api/spotify/web/start",            _post_spotify_web_start)
    app.router.add_post("/api/spotify/web/finish",           _post_spotify_web_finish)
    app.router.add_post("/api/spotify/web/disconnect",       _post_spotify_web_disconnect)


@auth.require_auth
async def _get_spotify_engine(request: web.Request) -> web.Response:
    """
    GET /api/spotify/engine

    What the controller holds, and where it will install it.
    """
    stored = em_spotify_install.stored_artefacts()
    return em_http.ok({
        "artefacts":  [a.as_json() for a in stored.values()],
        "expected":   list(em_spotify_assets.ARTEFACTS),
        "device_dir": em_spotify_assets.DEVICE_DIR,
    })


@auth.require_admin
async def _post_spotify_engine_upload(request: web.Request) -> web.Response:
    """
    POST /api/spotify/engine/upload (multipart: field name "binary")

    Store a locally built `em-spotify` or `em-spotify-probe`.

    The upload is validated by ELF HEADER rather than filename. Every way of
    getting this wrong — a host build, a 64-bit build, a PIE, a dynamically
    linked build, a truncated transfer — produces the SAME symptom on the
    device, a process that exits immediately, and the header is the only
    thing that distinguishes them while the operator is still looking at the
    dashboard. `em_spotify_assets.validate_upload` names which one it is.
    """
    try:
        reader = await request.multipart()
        field  = await reader.next()
        if field is None or field.name != "binary":
            return em_http.error("invalid_upload", "Expected multipart field 'binary'", 400)
        data = await field.read()
        try:
            name = em_spotify_assets.validate_upload(field.filename or "", data)
        except em_spotify_assets.BadArtefact as e:
            # 400 and the reason verbatim: this is an operator who has just
            # built something, and the message is what tells them which
            # build step to repeat.
            return em_http.error("bad_artefact", str(e), 400)

        directory = em_spotify_assets.assets_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # tmp + rename, so a push that reads the file concurrently can never
        # see a half-written binary — the same discipline as a wake model.
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, directory / name)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        md5 = em_spotify_assets.md5_hex(data)
        log.info(f"[api] Spotify artefact stored: {name} "
                 f"({len(data):,} bytes, md5 {md5[:8]}…)")
        return em_http.ok({"artefact": {"name": name, "size": len(data), "md5": md5}},
                   status=201)
    except web.HTTPException:
        # aiohttp's own, raised by the transport before this handler sees a
        # byte. The middleware re-raises these deliberately; swallowing one
        # into a 500 is how a size limit presented as "an internal error".
        raise
    except Exception as e:
        log.error(f"[api] Spotify artefact upload error: {e}")
        return em_http.error("upload_failed", str(e), 500)


@auth.require_admin
async def _delete_spotify_engine(request: web.Request) -> web.Response:
    """
    DELETE /api/spotify/engine/{name}

    Remove a stored artefact. Does not touch any device: a device keeps
    running what it has, which is the right behaviour for a control that
    only says what the controller will hand out next.
    """
    name = em_spotify_assets.safe_artefact_name(request.match_info["name"])
    if name is None:
        return em_http.error("not_found", "No such artefact", 404)
    path = em_spotify_assets.assets_dir() / name
    if not path.is_file():
        return em_http.error("not_found", "No such artefact", 404)
    path.unlink()
    log.info(f"[api] Spotify artefact deleted: {name}")
    return em_http.ok({"deleted": name})


@auth.require_auth
async def _get_device_spotify_engine(request: web.Request) -> web.Response:
    """
    GET /api/devices/{id}/spotify/engine

    What this device has installed, and what would be pushed.
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", f"Device not connected: {device_id}", 409)

    stored = {n: a.md5 for n, a in em_spotify_install.stored_artefacts().items()}
    on_device, answered = await em_spotify_install.device_md5s(live)
    return em_http.ok({
        "stored":    stored,
        "installed": on_device if answered else None,
        "pending":   em_spotify_assets.plan_install(stored, on_device)
                     if answered else None,
        "shell_answered": answered,
    })


@auth.require_admin
async def _post_device_spotify_engine(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/spotify/engine

    Install the stored artefacts on this device. Body may carry
    `{"only": ["em-spotify-probe"]}` to push one of them, and `{"force":
    true}` to re-push something whose md5 already matches.
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", f"Device not connected: {device_id}", 409)

    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    force = bool(body.get("force"))
    only = body.get("only")
    wanted = em_spotify_assets.ARTEFACTS
    if only is not None:
        if not isinstance(only, list) or not all(isinstance(x, str) for x in only):
            return em_http.error("bad_request", "`only` must be a list of names", 400)
        unknown = [x for x in only if x not in em_spotify_assets.ARTEFACTS]
        if unknown:
            return em_http.error("bad_request", f"Unknown artefact: {unknown[0]}", 400)
        wanted = tuple(n for n in em_spotify_assets.ARTEFACTS if n in only)

    stored = em_spotify_install.stored_artefacts()
    if not any(n in stored for n in wanted):
        return em_http.error(
            "nothing_stored",
            "No Spotify engine has been uploaded yet — build it with "
            "device/spotify/compile.sh and upload it on the Integrations tab.",
            409)

    md5s = {n: a.md5 for n, a in stored.items()}
    on_device, answered = await em_spotify_install.device_md5s(live)
    if not answered:
        # Failure to LOOK is not evidence of absence. Refused rather than
        # pushed blind, because the shell plane is usually just not up yet
        # and the retry costs the operator one click.
        return em_http.error(
            "shell_unavailable",
            "The device's shell plane did not answer, so what it already has "
            "is unknown. Try again in a few seconds.",
            503)

    plan = list(wanted) if force else em_spotify_assets.plan_install(
        md5s, on_device, wanted)
    plan = [n for n in plan if n in stored]
    if not plan:
        return em_http.ok({"installed": [], "already_current": True})

    em_tasks.spawn(em_spotify_install.install_engine(live, device_id, plan, stored))
    return em_http.ok({"started": True, "installing": plan}, status=202)


@auth.require_admin
async def _post_device_spotify_probe(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/spotify/probe

    Run the feasibility probe on the device and return what it printed.

    Admin because it executes a binary over the root shell. The probe itself
    writes nothing, changes no mixer control and opens no listening socket
    (device/spotify/src/probe.rs), so it is safe beside the running server —
    but "the operator asked for this" is the only reason it should ever run.
    """
    device_id = request.match_info["id"]
    live = em_state.devices.get(device_id)
    if live is None:
        return em_http.error("device_offline", f"Device not connected: {device_id}", 409)

    path = em_spotify_assets.device_path(em_spotify_assets.PROBE)
    # Sentinel so "the probe printed nothing" and "the shell never answered"
    # are different findings. Without it a device whose shell was down would
    # read as a probe that failed silently, which points the investigation at
    # the binary instead of at the link.
    out = await em_shell.run(
        live,
        f"if [ -x {shlex.quote(path)} ]; then {shlex.quote(path)} 2>&1; "
        f"else echo __EM_NO_PROBE__; fi; echo {em_shell.OK_MARK}",
        timeout=60.0,
    )
    if em_shell.OK_MARK not in out:
        return em_http.error(
            "shell_unavailable",
            "The device's shell plane did not answer. Try again in a few "
            "seconds.",
            503)
    text = out.split(em_shell.OK_MARK)[0].strip()
    if "__EM_NO_PROBE__" in text:
        return em_http.error(
            "not_installed",
            f"The probe is not installed at {path}. Upload it on the "
            f"Integrations tab and press Install.",
            409)

    # The probe exits non-zero on a failed check, but `_shell_run` does not
    # carry an exit status — so the verdict comes from the probe's own last
    # line, which it prints precisely so this is readable rather than
    # inferred.
    passed = "PASS" in text.splitlines()[-1] if text else False
    log.info(f"[{device_id}] Spotify probe: {'pass' if passed else 'fail'}")
    return em_http.ok({"output": text, "passed": passed})


@auth.require_admin
async def _post_device_spotify_cmd(request: web.Request) -> web.Response:
    """
    POST /api/devices/{id}/spotify/cmd  {"cmd": "...", "fields": {...}}

    One transport or load command for the Echo's Spotify engine, validated by
    em_spotify before it goes on the wire. The device's reply is returned; a
    failure carries the engine's own reason.
    """
    live = em_state.devices.get(request.match_info["id"])
    if live is None:
        return em_http.error("device_offline", "The Echo is offline", 409)
    if not getattr(live, "spotify_capable", False):
        return em_http.error("unsupported", "Spotify runs on emOS only", 409)
    try:
        body = await request.json()
    except Exception:
        return em_http.error("bad_request", "Expected a JSON body", 400)
    if not isinstance(body, dict):
        return em_http.error("bad_request", "Expected a JSON object", 400)
    try:
        fields = em_spotify.validate(body.get("cmd"), body.get("fields"))
    except em_spotify.BadCommand as e:
        return em_http.error("bad_command", str(e), 400)
    try:
        reply = await live.spotify_request(body["cmd"], fields)
    except asyncio.TimeoutError:
        return em_http.error("timeout", "The Echo did not answer", 504)
    if reply.get("error"):
        return em_http.error("spotify_failed", str(reply["error"]), 409)
    return em_http.ok({"ok": True})


# ── Spotify Web API connection (PKCE, paste-back) ───────────────────────────
#
# One connection per controller. The record holds tokens and lives in a 0600
# file beside the database; only `em_spotify_web.public_status` leaves this
# process, and nothing here logs a token, a code or a verifier.

_spotify_web_pending: Optional[dict] = None
_spotify_web_lock = asyncio.Lock()


async def _spotify_web_record() -> Optional[dict]:
    return await asyncio.to_thread(
        em_spotify_web.load, em_spotify_web.state_path())


async def _spotify_web_token_call(form: dict) -> tuple[int, object]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                em_spotify_web.TOKEN_URL, data=form,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = None
                return resp.status, body
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise em_spotify_web.AuthError(
            f"could not reach Spotify ({type(e).__name__}); check the "
            "controller's internet access") from None


async def spotify_web_access_token() -> str:
    """
    A valid Web API access token, refreshed when it is about to lapse.

    Raises AuthError with something to act on when there is no connection or
    it needs re-authorising. A refused refresh token turns the record into
    "reconnect required" so the dashboard says so, instead of retrying a dead
    credential on every voice request.
    """
    async with _spotify_web_lock:
        rec = await _spotify_web_record()
        if not rec or not rec.get("refresh_token"):
            if rec and rec.get("reauth_required"):
                raise em_spotify_web.AuthError(
                    "the Spotify connection expired; connect again on Integrations")
            raise em_spotify_web.AuthError(
                "Spotify is not connected; connect it on Integrations")
        now = time.time()
        if not em_spotify_web.needs_refresh(rec, now):
            return rec["access_token"]
        status, body = await _spotify_web_token_call(em_spotify_web.token_request_body(
            "refresh_token", refresh_token=rec["refresh_token"],
            client_id=rec["client_id"]))
        if status != 200 or not isinstance(body, dict):
            msg, reauth = em_spotify_web.describe_token_error(status, body)
            if reauth:
                await asyncio.to_thread(
                    em_spotify_web.save, em_spotify_web.state_path(),
                    em_spotify_web.mark_reauth(rec))
                log.warning("[spotify] Web API connection refused; reconnect required")
            raise em_spotify_web.AuthError(msg)
        rec = em_spotify_web.apply_refresh(rec, body, now)
        await asyncio.to_thread(em_spotify_web.save, em_spotify_web.state_path(), rec)
        return rec["access_token"]


@auth.require_admin
async def _get_spotify_web(request: web.Request) -> web.Response:
    """GET /api/spotify/web — connection state, never a token."""
    rec = await _spotify_web_record()
    return em_http.ok(em_spotify_web.public_status(rec, time.time()))


@auth.require_admin
async def _post_spotify_web_start(request: web.Request) -> web.Response:
    """
    POST /api/spotify/web/start {client_id, redirect_uri}

    Returns the Spotify consent URL to open. The PKCE verifier stays here.
    """
    global _spotify_web_pending
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return em_http.error("bad_request", "Expected a JSON object", 400)
    try:
        client_id = em_spotify_web.validate_client_id(body.get("client_id", ""))
        redirect = em_spotify_web.validate_redirect_uri(body.get("redirect_uri", ""))
    except em_spotify_web.AuthError as e:
        return em_http.error("bad_request", str(e), 400)
    verifier = em_spotify_web.new_verifier()
    state = em_spotify_web.new_state()
    _spotify_web_pending = {
        "client_id": client_id, "redirect_uri": redirect,
        "verifier": verifier, "state": state, "at": time.time(),
    }
    return em_http.ok({"authorize_url": em_spotify_web.authorize_url(
        client_id, redirect, em_spotify_web.challenge_for(verifier), state)})


@auth.require_admin
async def _post_spotify_web_finish(request: web.Request) -> web.Response:
    """POST /api/spotify/web/finish {redirect_url} — exchange the pasted code."""
    global _spotify_web_pending
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return em_http.error("bad_request", "Expected a JSON object", 400)
    pending = _spotify_web_pending
    now = time.time()
    if not pending or now - pending["at"] > em_spotify_web.PENDING_TTL_S:
        _spotify_web_pending = None
        return em_http.error("expired", "That attempt timed out or was never started; start again", 409)
    try:
        code = em_spotify_web.parse_callback(
            body.get("redirect_url", ""), pending["state"], pending["redirect_uri"])
    except em_spotify_web.AuthError as e:
        # A wrong paste is the likeliest slip and nothing was spent, so the
        # attempt stays open for a second try.
        return em_http.error("bad_callback", str(e), 400)
    # A code is single use, so the attempt ends here whatever happens next.
    _spotify_web_pending = None
    try:
        status, resp = await _spotify_web_token_call(em_spotify_web.token_request_body(
            "authorization_code", code=code, redirect_uri=pending["redirect_uri"],
            client_id=pending["client_id"], code_verifier=pending["verifier"]))
        if status != 200 or not isinstance(resp, dict):
            msg, _ = em_spotify_web.describe_token_error(status, resp)
            return em_http.error("token_failed", msg, 400)
        rec = em_spotify_web.new_record(
            pending["client_id"], pending["redirect_uri"], resp, now)
    except em_spotify_web.AuthError as e:
        return em_http.error("token_failed", str(e), 502)
    async with _spotify_web_lock:
        await asyncio.to_thread(em_spotify_web.save, em_spotify_web.state_path(), rec)
    log.info("[spotify] Web API connected")
    return em_http.ok(em_spotify_web.public_status(rec, now))


@auth.require_admin
async def _post_spotify_web_disconnect(request: web.Request) -> web.Response:
    """POST /api/spotify/web/disconnect — forget the stored connection."""
    global _spotify_web_pending
    _spotify_web_pending = None
    async with _spotify_web_lock:
        await asyncio.to_thread(em_spotify_web.clear, em_spotify_web.state_path())
    return em_http.ok({"state": "disconnected"})
