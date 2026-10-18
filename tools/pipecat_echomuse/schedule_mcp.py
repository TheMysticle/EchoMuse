"""Adapt a parsed schedule item (schedule_language.parse_item's own shape) to
the EchoMuseCreate MCP tool's argument shape (controller/em_workflow_tools.py).

Two different consumers want two different shapes for the same thing. The
native HA handler's `parse_item()` output (kind, label, timezone, plus
duration OR due OR rule) is sent straight to the controller's
`/api/devices/{id}/schedules` REST endpoint, which accepts it as-is. Pipecat's
add-on has no access to that endpoint -- only to the per-device scheduler MCP
server, whose EchoMuseCreate tool wants `duration_seconds` OR an RFC3339
`when` (+ `timezone`), plus `repeat_days`/`repeat_seconds` for recurrence.
`em_workflow_tools.command()` is the authoritative target shape; read it
before changing this.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from schedule_language import require_local_time


def _next_weekday_occurrence(now, days, hour, minute):
    """Next local time matching one of `days` (schedule_language.DAYS indices:
    Sunday=0..Saturday=6, the SAME encoding EchoMuseCreate's `repeat_days`
    uses -- Monday=1..Sunday=0) at `hour`:`minute`, strictly after `now`."""

    target_weekdays = {(day + 6) % 7 for day in days}
    for offset in range(8):
        candidate_date = (now + timedelta(days=offset)).date()
        if candidate_date.weekday() not in target_weekdays:
            continue
        candidate = now.replace(
            year=candidate_date.year, month=candidate_date.month, day=candidate_date.day,
            hour=hour, minute=minute, second=0, microsecond=0,
        )
        if candidate > now:
            return require_local_time(candidate)
    raise ValueError("Could not resolve that calendar recurrence")


def to_mcp_args(kind, item, now):
    """Convert `parse_item()`'s item into EchoMuseCreate's own argument shape.

    `now` is a tz-aware datetime in the item's own timezone, used only to
    anchor a calendar-recurrence's first occurrence (parse_item leaves
    `due` at 0 for that case -- the device computes the calendar recurrence
    itself, but the MCP tool still needs ONE concrete first occurrence to
    derive `rule.hour`/`rule.minute` from).
    """

    args = {"kind": kind, "timezone": item["timezone"]}
    label = item.get("label")
    if label and label != kind:
        args["label"] = label
    if kind == "reminder":
        args["message"] = item.get("message") or item.get("label") or ""

    rule = item.get("rule")
    if rule and rule.get("interval"):
        # parse_item anchors `due` at now + interval for this case already;
        # an identical duration_seconds reproduces the same first occurrence.
        args["duration_seconds"] = rule["interval"]
        args["repeat_seconds"] = rule["interval"]
        return args
    if rule and rule.get("days") is not None:
        zone = ZoneInfo(item["timezone"])
        anchor = _next_weekday_occurrence(now.astimezone(zone), rule["days"], rule["hour"], rule["minute"])
        args["when"] = anchor.isoformat()
        args["repeat_days"] = rule["days"]
        return args
    if "duration" in item:
        args["duration_seconds"] = item["duration"]
        return args
    zone = ZoneInfo(item["timezone"])
    args["when"] = datetime.fromtimestamp(item["due"], zone).isoformat()
    return args
