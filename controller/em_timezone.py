"""The controller's own timezone as an IANA name, for the ack.

An Echo has no zone of its own and its log reads in UTC, so the ack names ours
beside `time_ms` and the firmware dresses its timestamps in it. Where the name
comes from, in order: `TZ`, `/etc/timezone` (Debian and Ubuntu write it), then
the `/etc/localtime` symlink's target under a zoneinfo directory. A name that
`zoneinfo` cannot load is not sent, and no answer is None: the Echo then keeps
UTC, which is what an older controller gives it. Guessing a zone would put a
wrong hour in every line, which is worse than a log that says nothing about it.
"""
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _valid(name):
    name = (name or "").strip().lstrip(":")
    if not name or name.startswith("/") or ".." in name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def _from_localtime(path):
    try:
        target = os.path.realpath(path)
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker in target:
        return _valid(target.split(marker, 1)[1])
    return None


def local_zone_name(env=None, etc_timezone="/etc/timezone", etc_localtime="/etc/localtime"):
    env = os.environ if env is None else env
    name = _valid(env.get("TZ"))
    if name:
        return name
    try:
        with open(etc_timezone, encoding="ascii") as handle:
            name = _valid(handle.readline())
    except (OSError, UnicodeDecodeError):
        name = None
    return name or _from_localtime(etc_localtime)


LOCAL_ZONE = local_zone_name()


def ack_fields():
    """`tz` for the ack, or nothing: absent reads as "no opinion" on the Echo."""
    return {"tz": LOCAL_ZONE} if LOCAL_ZONE else {}
