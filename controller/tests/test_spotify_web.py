import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import em_spotify_web as w  # noqa: E402

CID = "0123456789abcdef0123456789abcdef"
REDIR = "http://127.0.0.1:8888/callback"


# ── PKCE: RFC 7636 appendix B vector ────────────────────────────────────────

def test_challenge_matches_rfc7636_vector():
    v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert w.challenge_for(v) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_is_in_the_rfc_alphabet_and_length_and_unique():
    seen = {w.new_verifier() for _ in range(50)}
    assert len(seen) == 50
    for v in seen:
        assert re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", v)
        w.challenge_for(v)


@pytest.mark.parametrize("bad", ["", "x" * 42, "x" * 129, "a b" + "c" * 50, "é" * 50])
def test_challenge_refuses_a_verifier_the_rfc_forbids(bad):
    with pytest.raises(w.AuthError):
        w.challenge_for(bad)


def test_challenge_has_no_padding():
    assert "=" not in w.challenge_for(w.new_verifier())


# ── inputs ──────────────────────────────────────────────────────────────────

def test_client_id_is_32_hex_and_normalised():
    assert w.validate_client_id("  " + CID.upper() + " ") == CID
    for bad in ("", "abc", CID + "0", "g" * 32):
        with pytest.raises(w.AuthError):
            w.validate_client_id(bad)


@pytest.mark.parametrize("ok", [
    "http://127.0.0.1:8888/callback", "http://127.0.0.1/callback",
    "http://[::1]:9/cb", "https://example.com/cb"])
def test_redirect_accepts_what_spotify_accepts(ok):
    assert w.validate_redirect_uri(ok) == ok


@pytest.mark.parametrize("bad", [
    "http://localhost:8888/callback", "http://example.com/cb", "ftp://127.0.0.1/",
    "", "127.0.0.1:8888/callback", "http://127.0.0.1:99999/cb",
    "http://127.0.0.1:8888/cb?x=1", "http://127.0.0.1:8888/cb#f",
    "http://u:p@127.0.0.1/cb"])
def test_redirect_refuses_what_spotify_refuses(bad):
    with pytest.raises(w.AuthError):
        w.validate_redirect_uri(bad)


def test_authorize_url_carries_pkce_and_no_secret():
    u = urlparse(w.authorize_url(CID, REDIR, "CH", "ST"))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "accounts.spotify.com"
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == REDIR and q["state"] == "ST"
    assert "client_secret" not in q and "code_verifier" not in q
    assert q["scope"] == "user-read-private"


def test_token_body_never_holds_a_secret_and_refuses_other_grants():
    b = w.token_request_body("refresh_token", refresh_token="r", client_id=CID)
    assert "client_secret" not in b
    with pytest.raises(ValueError):
        w.token_request_body("client_credentials")


# ── paste-back ──────────────────────────────────────────────────────────────

def test_parse_callback_takes_a_full_url_or_a_bare_query():
    assert w.parse_callback(f"{REDIR}?code=AB&state=S", "S", REDIR) == "AB"
    assert w.parse_callback("?code=AB&state=S", "S", REDIR) == "AB"
    assert w.parse_callback("code=AB&state=S", "S", REDIR) == "AB"
    assert w.parse_callback(f"  {REDIR}?code=AB&state=S\n", "S", REDIR) == "AB"


def test_parse_callback_decodes_the_code():
    assert w.parse_callback(f"{REDIR}?code=A%2FB&state=S", "S", REDIR) == "A/B"


@pytest.mark.parametrize("pasted,expect", [
    ("", "paste"),
    (f"{REDIR}?error=access_denied&state=S", "declined"),
    (f"{REDIR}?error=server_error&state=S", "server_error"),
    (f"{REDIR}?state=S", "no code"),
    (f"{REDIR}?code=AB&state=OTHER", "different or expired"),
    (f"{REDIR}?code=AB", "different or expired"),
    ("http://127.0.0.1:9999/callback?code=AB&state=S", "not the redirect URI"),
    ("https://evil.example/callback?code=AB&state=S", "not the redirect URI"),
])
def test_parse_callback_errors_say_what_to_do(pasted, expect):
    with pytest.raises(w.AuthError, match=expect):
        w.parse_callback(pasted, "S", REDIR)


def test_parse_callback_refuses_when_no_attempt_is_open():
    with pytest.raises(w.AuthError):
        w.parse_callback(f"{REDIR}?code=AB&state=", "", REDIR)


def test_parse_callback_survives_a_non_ascii_state():
    with pytest.raises(w.AuthError):
        w.parse_callback(f"{REDIR}?code=AB&state=%C3%A9", "S", REDIR)


# ── records and state ───────────────────────────────────────────────────────

def _resp(**kw):
    base = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            "scope": "user-read-private"}
    base.update(kw)
    return base


def test_new_record_needs_both_tokens():
    assert w.new_record(CID, REDIR, _resp(), 1000)["expires_at"] == 4600
    for drop in ("access_token", "refresh_token"):
        r = _resp()
        del r[drop]
        with pytest.raises(w.AuthError):
            w.new_record(CID, REDIR, r, 0)


def test_refresh_without_a_new_refresh_token_keeps_the_old_one():
    rec = w.new_record(CID, REDIR, _resp(), 0)
    out = w.apply_refresh(rec, {"access_token": "AT2", "expires_in": 100}, 5000)
    assert out["refresh_token"] == "RT" and out["access_token"] == "AT2"
    assert out["expires_at"] == 5100 and out["connected_at"] == 0


def test_refresh_with_a_rotated_refresh_token_takes_it():
    rec = w.new_record(CID, REDIR, _resp(), 0)
    assert w.apply_refresh(rec, _resp(refresh_token="RT2"), 1)["refresh_token"] == "RT2"


def test_refresh_without_an_access_token_is_refused():
    rec = w.new_record(CID, REDIR, _resp(), 0)
    with pytest.raises(w.AuthError):
        w.apply_refresh(rec, {"expires_in": 1}, 0)


def test_needs_refresh_leaves_a_skew():
    rec = w.new_record(CID, REDIR, _resp(expires_in=3600), 0)
    assert not w.needs_refresh(rec, 3600 - w.ACCESS_SKEW_S - 1)
    assert w.needs_refresh(rec, 3600 - w.ACCESS_SKEW_S)
    assert w.needs_refresh({}, 0)


def test_mark_reauth_drops_the_tokens_and_keeps_the_app():
    rec = w.mark_reauth(w.new_record(CID, REDIR, _resp(), 7))
    assert "access_token" not in rec and "refresh_token" not in rec
    assert rec["client_id"] == CID and rec["reauth_required"]
    assert w.public_status(rec, 8)["state"] == "reauth_required"


def test_status_ages_through_the_six_months():
    rec = w.new_record(CID, REDIR, _resp(), 0)
    day = 86400
    assert w.public_status(None, 0) == {"state": "disconnected"}
    assert w.public_status(rec, 149 * day)["state"] == "connected"
    assert w.public_status(rec, 150 * day)["state"] == "expiring"
    assert w.public_status(rec, 180 * day)["state"] == "reauth_required"


def test_public_status_never_carries_a_token_even_one_added_later():
    rec = w.new_record(CID, REDIR, _resp(), 0)
    rec["future_secret"] = "SHH"
    text = json.dumps(w.public_status(rec, 1))
    for s in ("AT", "RT", "SHH", "access_token", "refresh_token"):
        assert s not in text


# ── errors ──────────────────────────────────────────────────────────────────

def test_invalid_grant_means_reconnect():
    msg, reauth = w.describe_token_error(
        400, {"error": "invalid_grant", "error_description": "Refresh token revoked"})
    assert reauth and "connect again" in msg


def test_other_token_errors_do_not_discard_the_connection():
    for status, body in [
        (400, {"error": "invalid_client"}),
        (400, {"error": "invalid_request", "error_description": "Invalid redirect URI"}),
        (429, None), (503, None), (400, "garbage")]:
        msg, reauth = w.describe_token_error(status, body)
        assert not reauth and msg


def test_api_errors_are_actionable():
    assert "quota" in w.describe_api_error(429, {"reason": "QUOTA_EXCEEDED"})
    assert "rate" in w.describe_api_error(429, {})
    assert "connect again" in w.describe_api_error(401, {})
    assert "User Management" in w.describe_api_error(
        403, {"error": {"message": "User not registered in the Developer Dashboard"}})
    assert "Premium" in w.describe_api_error(
        403, {"error": {"message": "Active premium subscription required"}})
    assert w.describe_api_error(500, None)
    assert "404" in w.describe_api_error(404, None)


# ── storage ─────────────────────────────────────────────────────────────────

def test_save_load_clear_round_trip(tmp_path):
    p = tmp_path / "spotify" / "web_auth.json"
    assert w.load(p) is None
    rec = w.new_record(CID, REDIR, _resp(), 1)
    w.save(p, rec)
    assert w.load(p) == rec
    assert not list(p.parent.glob("*.tmp"))
    w.clear(p)
    w.clear(p)
    assert w.load(p) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_record_file_is_private(tmp_path):
    p = tmp_path / "web_auth.json"
    w.save(p, {"a": 1})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_corrupt_file_reads_as_disconnected(tmp_path):
    p = tmp_path / "web_auth.json"
    p.write_text("{nope")
    assert w.load(p) is None
    p.write_text("[1]")
    assert w.load(p) is None


def test_state_path_sits_beside_the_database(tmp_path):
    assert w.state_path(str(tmp_path / "x.db")) == (
        tmp_path.resolve() / "spotify" / "web_auth.json")


# ── em_api wiring, checked from source (aiohttp is not imported in tests) ───

def _api_source():
    return (Path(__file__).resolve().parent.parent / "em_api.py").read_text("utf-8")


def _func_src(name):
    import ast
    src = _api_source()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node, ast.get_source_segment(src, node)
    raise AssertionError(name)


@pytest.mark.parametrize("name", [
    "_get_spotify_web", "_post_spotify_web_start",
    "_post_spotify_web_finish", "_post_spotify_web_disconnect"])
def test_web_endpoints_are_admin_only_and_routed(name):
    node, _ = _func_src(name)
    assert any("require_admin" in __import__("ast").unparse(d) for d in node.decorator_list)
    assert re.search(rf"add_(get|post)\(.*\s{name}\)", _api_source())


def test_nothing_in_the_web_section_logs_a_credential():
    import ast
    for name in ("spotify_web_access_token", "_post_spotify_web_finish",
                 "_post_spotify_web_start", "_spotify_web_token_call"):
        node, _ = _func_src(name)
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == "log"):
                text = ast.unparse(call)
                for secret in ("token", "code", "verifier", "resp", "body", "rec"):
                    assert not re.search(rf"\b{secret}\b", text.replace("reconnect", "")), text


def test_status_endpoint_returns_only_the_public_view():
    _, src = _func_src("_get_spotify_web")
    assert "public_status" in src and "access_token" not in src


def test_a_dead_refresh_token_is_marked_not_retried():
    _, src = _func_src("spotify_web_access_token")
    assert "mark_reauth" in src and "reauth" in src


def test_the_module_is_in_the_image():
    root = Path(__file__).resolve().parent.parent
    assert "COPY em_spotify_web.py ." in (root / "Dockerfile").read_text("utf-8")
