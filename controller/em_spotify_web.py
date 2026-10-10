"""
Spotify Web API authorisation (Authorization Code with PKCE), as pure logic.

The Echo plays through librespot, which needs no Web API. Finding what to
play by NAME does, and that needs a token for the user's own Spotify
Developer app. This module holds everything about that token that can be
decided without a network: the PKCE pair, the authorise URL, reading the
address the user pastes back, the stored record, what state it is in, and
what an error from Spotify means. The HTTP lives in `em_api.py`.

Spec: RFC 7636 (PKCE) and Spotify's "Authorization Code with PKCE Flow".

* No client secret exists anywhere. PKCE does not use one, and the dashboard
  never asks for one, so there is nothing for the browser to leak.
* The redirect goes to a loopback address nothing listens on. The user
  copies the address bar back, so the controller needs no inbound route from
  the internet and the code never crosses a third party.
* Spotify refuses `localhost`; the loopback IP literal is required.
* Tokens are never logged, never in an event, never in a support bundle:
  `public_status` is the only view of a record that leaves the process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Search and resolve only. Playback is done by the Echo's own engine, so no
# playback scope is requested: a token that cannot control a player cannot be
# misused to.
SCOPES = ("user-read-private",)

# A pending authorisation is abandoned after this long.
PENDING_TTL_S = 600

# Spotify documents that refresh tokens expire without saying when. The
# handoff's working figure is six months; the state turns to "expiring" a
# month before so the user reconnects on their own terms rather than finding
# voice play dead.
REFRESH_LIFETIME_S = 180 * 86400
REFRESH_WARN_S = 150 * 86400

# Renew an access token this long before it lapses, so a request that starts
# just inside the window does not fail halfway.
ACCESS_SKEW_S = 60

STATE_FILE = "web_auth.json"
ASSETS_SUBDIR = "spotify"

_CLIENT_ID = re.compile(r"^[0-9a-f]{32}$")
# RFC 7636 section 4.1: 43-128 characters of unreserved URL characters.
_VERIFIER_CHARS = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


class AuthError(ValueError):
    """Something the user can act on. The message is shown as written."""


# ── PKCE ────────────────────────────────────────────────────────────────────


def new_verifier() -> str:
    """43 characters from 32 random bytes, the shortest the RFC permits."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def challenge_for(verifier: str) -> str:
    """BASE64URL(SHA256(verifier)) without padding, RFC 7636 section 4.2."""
    if not _VERIFIER_CHARS.match(verifier):
        raise AuthError("the PKCE verifier is not 43-128 unreserved characters")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def new_state() -> str:
    return secrets.token_urlsafe(24)


# ── Inputs ──────────────────────────────────────────────────────────────────


def validate_client_id(text: str) -> str:
    cid = (text or "").strip().lower()
    if not _CLIENT_ID.match(cid):
        raise AuthError(
            "the Client ID is 32 letters and digits, copied from the app's "
            "Settings page on developer.spotify.com/dashboard"
        )
    return cid


def validate_redirect_uri(text: str) -> str:
    """
    A redirect URI Spotify will accept: https, or http on an IP loopback
    literal. `localhost` is refused by Spotify since April 2025, so it is
    refused here with the reason instead of failing at their consent page.
    """
    uri = (text or "").strip()
    try:
        u = urlparse(uri)
        host = u.hostname
        u.port  # raises on a non-numeric port
    except ValueError:
        raise AuthError("the redirect URI is not a valid address") from None
    if u.scheme not in ("http", "https") or not host:
        raise AuthError("the redirect URI must start with http:// or https://")
    if host == "localhost":
        raise AuthError(
            "Spotify no longer accepts localhost; use http://127.0.0.1:8888/callback"
        )
    if u.scheme == "http" and host not in ("127.0.0.1", "::1"):
        raise AuthError(
            "plain http is only allowed for 127.0.0.1 or [::1]; "
            "use http://127.0.0.1:8888/callback"
        )
    if u.query or u.fragment or u.username or u.password:
        raise AuthError("the redirect URI must not carry a query, fragment or login")
    return uri


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": " ".join(SCOPES),
    })


def parse_callback(pasted: str, expected_state: str, redirect_uri: str) -> str:
    """
    The authorisation code from the address the user pasted back.

    The browser lands on a dead loopback page, so the user has only the
    address bar. Accepts the whole URL or just its query string. Checks, in
    order of what the user most likely got wrong: Spotify's own refusal, the
    address being the wrong one, a stale or foreign `state`.
    """
    text = (pasted or "").strip()
    if not text:
        raise AuthError("paste the address from the browser after approving")
    query = urlparse(text).query if "?" in text else text.lstrip("?")
    params = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}

    err = params.get("error")
    if err:
        if err == "access_denied":
            raise AuthError("access was declined on Spotify's page; start again and choose Agree")
        raise AuthError(f"Spotify refused the request ({err})")

    got = urlparse(text)
    if got.netloc:
        want = urlparse(redirect_uri)
        if (got.scheme, got.netloc, got.path) != (want.scheme, want.netloc, want.path):
            raise AuthError(
                "that address is not the redirect URI; copy the address bar of "
                f"the page the browser ended on, which begins {redirect_uri}"
            )

    code = params.get("code")
    if not code:
        raise AuthError("the address has no code; copy it after clicking Agree")
    state = params.get("state", "")
    if not expected_state or not secrets.compare_digest(state.encode(), expected_state.encode()):
        raise AuthError(
            "this address belongs to a different or expired attempt; start again"
        )
    return code


def token_request_body(grant: str, **fields: str) -> dict:
    """Form fields for the token endpoint. Never contains a client secret."""
    if grant not in ("authorization_code", "refresh_token"):
        raise ValueError(grant)
    return {"grant_type": grant, **fields}


# ── The stored record ───────────────────────────────────────────────────────


def new_record(client_id: str, redirect_uri: str, resp: dict, now: float) -> dict:
    """Build the record from the first token response."""
    refresh = resp.get("refresh_token")
    if not resp.get("access_token") or not refresh:
        raise AuthError("Spotify's reply carried no tokens; start again")
    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "access_token": resp["access_token"],
        "refresh_token": refresh,
        "expires_at": now + float(resp.get("expires_in", 3600)),
        "connected_at": now,
        "scope": resp.get("scope", ""),
        "reauth_required": False,
    }


def apply_refresh(record: dict, resp: dict, now: float) -> dict:
    """
    The record after a refresh. A refresh response may omit the refresh
    token, in which case the existing one stays valid; dropping it would
    disconnect the user at the next renewal.
    """
    if not resp.get("access_token"):
        raise AuthError("Spotify's reply carried no access token")
    out = dict(record)
    out["access_token"] = resp["access_token"]
    out["expires_at"] = now + float(resp.get("expires_in", 3600))
    if resp.get("refresh_token"):
        out["refresh_token"] = resp["refresh_token"]
    if resp.get("scope"):
        out["scope"] = resp["scope"]
    out["reauth_required"] = False
    return out


def needs_refresh(record: dict, now: float) -> bool:
    return now >= float(record.get("expires_at", 0)) - ACCESS_SKEW_S


def mark_reauth(record: dict) -> dict:
    """
    The refresh token was refused. The tokens are dropped, not kept: a dead
    credential is worth nothing and sitting on disk it is only risk. The
    client id and redirect URI stay so reconnecting is one click.
    """
    return {
        "client_id": record.get("client_id", ""),
        "redirect_uri": record.get("redirect_uri", ""),
        "connected_at": record.get("connected_at", 0),
        "reauth_required": True,
    }


def public_status(record: dict | None, now: float) -> dict:
    """
    What the dashboard may see. Built field by field from an allowlist so a
    token added to the record later cannot leak by default.
    """
    if not record:
        return {"state": "disconnected"}
    out = {
        "client_id": record.get("client_id", ""),
        "redirect_uri": record.get("redirect_uri", ""),
    }
    if record.get("reauth_required") or not record.get("refresh_token"):
        out["state"] = "reauth_required"
        out["detail"] = (
            "Spotify stopped accepting the saved connection (refresh tokens "
            "expire, and revoking the app does the same). Connect again."
        )
        return out
    age = now - float(record.get("connected_at", now))
    out["connected_at"] = record.get("connected_at")
    if age >= REFRESH_LIFETIME_S:
        out["state"] = "reauth_required"
        out["detail"] = "The connection is older than six months and Spotify may refuse it. Connect again."
    elif age >= REFRESH_WARN_S:
        out["state"] = "expiring"
        out["detail"] = "The connection is nearly six months old. Reconnect soon to avoid voice play stopping."
    else:
        out["state"] = "connected"
    return out


# ── Errors ──────────────────────────────────────────────────────────────────


def describe_token_error(status: int, body: object) -> tuple[str, bool]:
    """
    (message, reauth) for a failed token request. `reauth` means the stored
    refresh token is dead and only a new authorisation helps.
    """
    err = body.get("error") if isinstance(body, dict) else None
    desc = body.get("error_description") if isinstance(body, dict) else None
    if err == "invalid_grant":
        return (
            "Spotify refused the code or saved connection"
            + (f" ({desc})" if isinstance(desc, str) else "")
            + "; connect again",
            True,
        )
    if err == "invalid_client":
        return "Spotify does not recognise that Client ID; check it on the app's Settings page", False
    if err == "invalid_request" and isinstance(desc, str) and "redirect" in desc.lower():
        return (
            "the redirect URI differs from the one registered on the app; "
            "add exactly this address under Redirect URIs and save",
            False,
        )
    if status == 429:
        return "Spotify is rate limiting this app; wait a minute and try again", False
    if status >= 500:
        return "Spotify's service had an error; try again shortly", False
    return f"Spotify refused the request ({err or status})", False


def describe_api_error(status: int, body: object) -> str:
    """An actionable reason for a failed Web API call."""
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message", "") if isinstance(err, dict) else ""
    reason = body.get("reason") if isinstance(body, dict) else None
    if status == 429 and reason == "QUOTA_EXCEEDED":
        return "this app has used its Spotify development-mode quota; try later"
    if status == 429:
        return "Spotify is rate limiting this app; wait a minute"
    if status == 401:
        return "the Spotify connection is no longer valid; connect again"
    if status == 403:
        low = msg.lower()
        if "not registered" in low or "user not" in low:
            return (
                "this Spotify account is not on the app's User Management list; "
                "add it on developer.spotify.com/dashboard (development mode allows 5 users)"
            )
        if "premium" in low:
            return "the app's owner needs an active Spotify Premium subscription"
        return "Spotify refused the request" + (f" ({msg})" if msg else "")
    if status >= 500:
        return "Spotify's service had an error; try again shortly"
    return f"Spotify returned {status}" + (f" ({msg})" if msg else "")


# ── Storage ─────────────────────────────────────────────────────────────────


def state_path(db_path: str | None = None) -> Path:
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent / ASSETS_SUBDIR / STATE_FILE


def load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(path: Path, record: dict) -> None:
    """mkstemp creates the file 0600; the rename keeps readers off a half record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
