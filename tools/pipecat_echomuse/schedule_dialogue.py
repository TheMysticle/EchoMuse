"""Pure helpers from custom_components/echomuse_schedule/dialogue.py.

Only the parts with no Home Assistant dependency: merging a follow-up answer
back into a pending request (`complete_request`), and building the spoken
confirmation for a created item (`created_speech`/`duration_speech`). The
native handler's `Dialogues` class drives HA's `assist_satellite.ask_question`
service, which does not exist here -- pipecat's own continuation mechanism
replaces it (see fast_path.py), so that class is not reused.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from schedule_language import Clarification, numbers, normalize_clock, clock_match

CANCEL = {"cancel", "cancel it", "never mind", "nevermind", "forget it", "stop"}


def complete_request(request, answer, field):
    answer = answer.strip().rstrip(".?!")
    low = numbers(answer)
    if field == "meridian":
        if re.fullmatch(r"(?:in the )?(?:a\.?\s*m\.?|morning)", low):
            return request + " in the morning"
        if re.fullmatch(r"(?:in the )?(?:p\.?\s*m\.?|afternoon|evening)", low):
            return request + " in the evening"
        # A full correction ('8am') supersedes the ambiguous clock, but keeps
        # its day, recurrence, reminder message, and name.
        normalized = numbers(request)
        plain = re.search(r"(?:\b(?:at|for)\s+|^\s*)((?:half|quarter|\d+(?: minutes?)?) (?:past|after|to|before) \d+(?::\d{2})?|\d{1,2}(?:(?::|\.)\d{2})?)(?!\w)", normalized)
        if plain and re.search(r"\d|noon|midday|midnight", low):
            return normalized[:plain.start(1)] + re.sub(r"^at\s+", "", low) + normalized[plain.end(1):]
        raise Clarification("Did you mean AM or PM?", "meridian")
    if field == "time":
        # Bare clock answers are deliberately not global custom sentences.
        if not re.search(r"\d|noon|midday|midnight|half|quarter", low):
            raise Clarification("What time? Say, for example, 8am", "time")
        return request + " " + (answer if re.match(r"at\b", low) else "at " + answer)
    if field == "duration":
        return request + " " + answer
    # Put the task before its time clause, including "every two hours".
    # The answer may already start with "to"; add that connector only once.
    when = re.sub(r"^\s*(?:to\s+)+", "", request, flags=re.I).strip()
    probe = normalize_clock(when)
    clock = clock_match(probe)
    if clock and clock.start() == 0:
        # Once a task precedes a bare clock, the clock needs an introducer.
        when = re.sub(r"^for\s+", "at ", when, flags=re.I)
        if re.match(r"\d", probe):
            when = "at " + when
    else:
        when = re.sub(r"^for\s+", "", when, flags=re.I)
    from schedule_language import reminder_message
    return "to " + reminder_message(answer) + " " + when


def duration_speech(seconds):
    seconds = int(seconds)
    parts = []
    for unit, length in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        count, seconds = divmod(seconds, length)
        if count:
            parts.append(f"{count} {unit}{'s' if count != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


def created_speech(kind, item, result):
    created = next((i for i in result["snapshot"]["items"] if i["id"] == item.get("id")), None)
    if created is None:
        created = result["snapshot"]["items"][-1]
    if kind == "timer":
        label = created.get("label", "timer")
        name = "Your timer" if label.lower() == "timer" else f"Your {label} timer"
        text = f"{name} is set for {duration_speech(created.get('duration') or item.get('duration', 0))}."
    else:
        zone = ZoneInfo(created.get("timezone", item.get("timezone", "UTC")))
        now = datetime.now(zone)
        rule = created.get("rule")
        if rule and rule.get("interval"):
            when = "every " + duration_speech(rule["interval"])
        elif created.get("due") or rule:
            due = datetime.fromtimestamp(created["due"], zone) if created.get("due") else now.replace(hour=rule["hour"], minute=rule["minute"])
            clock = due.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
            if rule:
                days = rule.get("days", [])
                recurrence = "every day" if len(days) == 7 else "every weekday" if set(days) == {1,2,3,4,5} else "every weekend" if set(days) == {0,6} else "every " + " and ".join(["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"][d] for d in days)
                when = f"at {clock} {recurrence}"
            else:
                offset = (due.date() - now.date()).days
                day = "today" if offset == 0 else "tomorrow" if offset == 1 else "on " + due.strftime("%A %d %B")
                when = f"at {clock} {day}"
        else:
            when = "in " + duration_speech(created.get("duration") or item.get("duration", 0))
        if kind == "reminder":
            text = f"Okay, I'll remind you to {created.get('message') or item.get('message', '')} {when}."
        else:
            text = f"Your alarm is set {when}."
    if result.get("warning"):
        text += " I'll play a sound if the spoken reminder isn't ready."
    return text
