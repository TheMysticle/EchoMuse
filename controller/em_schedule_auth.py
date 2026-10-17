"""Revocable bearer keys limited to one satellite's schedule API.

The ingress dashboard issues keys; only hashes persist in the controller DB.
These keys never authenticate dashboard, shell, device config or auth routes.
"""
import asyncio
import base64
import binascii
from functools import wraps
import hashlib
import secrets

from aiohttp import web
import em_auth as auth
import em_db as db

PREFIX = "emsched."
ROUTES = {
    ("GET", "/api/schedules"),
    ("POST", "/api/devices/{id}/schedules"),
    ("POST", "/api/devices/{id}/schedule-assets"),
}


def issue(device_id):
    encoded = base64.urlsafe_b64encode(device_id.encode()).decode().rstrip("=")
    token = PREFIX + encoded + "." + secrets.token_urlsafe(48)
    db.set_config("schedule_key:" + device_id, hashlib.sha256(token.encode()).hexdigest())
    return token


def revoke(device_id):
    db.set_config("schedule_key:" + device_id, None)


def _resolve(token):
    if not token.startswith(PREFIX) or len(token) > 1024:
        return None
    try:
        _, encoded, secret = token.split(".")
        device_id = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeError, binascii.Error):
        return None
    if not secret or not device_id:
        return None
    stored = db.get_config("schedule_key:" + device_id)
    if not stored or not secrets.compare_digest(stored, hashlib.sha256(token.encode()).hexdigest()):
        return None
    device = db.get_device(device_id)
    return device_id if device is not None and device["approved"] else None


async def authorize(request):
    route = request.match_info.route.resource
    if route is None or (request.method, route.canonical) not in ROUTES:
        return None
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token.startswith(PREFIX):
        return None
    satellite = await asyncio.to_thread(_resolve, token)
    if satellite is not None and ("id" not in request.match_info or request.match_info["id"] == satellite):
        return satellite
    return None


def _access(handler, admin):
    session_handler = (auth.require_admin if admin else auth.require_auth)(handler)

    @wraps(handler)
    async def wrapper(request):
        satellite = request.get("schedule_satellite") or await authorize(request)
        if satellite is not None:
            request["schedule_satellite"] = satellite
            return await handler(request)
        return await session_handler(request)
    return wrapper


def require_auth(handler):
    return _access(handler, False)


def require_admin(handler):
    return _access(handler, True)
