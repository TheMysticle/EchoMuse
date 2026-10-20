"""Small deterministic English time parser. Reject ambiguity, never guess dates.

Verbatim copy of custom_components/echomuse_schedule/language.py (pure Python:
only re/calendar/datetime/zoneinfo, no Home Assistant imports), so the same
parsing used by the native HA Assist handler backs EchoMuse's own fast-path
scheduler tools here. Keep the two in sync by hand; do not fork the logic.
"""
import re
from calendar import monthrange
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WORDS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
WORDS.update(dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10))))
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
ORDINALS = dict(zip(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split(),
    range(1, 21)))
ORDINALS.update({ten + " " + name: value + day
    for ten, value in list(WORDS.items()) if value >= 20
    for name, day in list(ORDINALS.items())[:9]})
ORDINALS.update(dict(zip("thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth".split(), range(30, 100, 10))))
DAY_OF_MONTH = re.compile(
    r"\b(?:on\s+(?:the\s+)?|the\s+)(\d+(?:st|nd|rd|th)?|"
    + "|".join(re.escape(word).replace(r"\ ", "[ -]") for word in sorted(ORDINALS, key=len, reverse=True))
    + r")\b(?=\s*(?:$|[,.]|(?:at|to|on|today|tomorrow|every|each|in|after|"
    + "|".join(DAYS) + r")\b))")


MONTHS = "january february march april may june july august september october november december".split()
NAMED_DATE = re.compile(
    r"\b(?:on\s+|for\s+)?(?:the\s+)?(\d+(?:st|nd|rd|th)?|"
    + "|".join(re.escape(word).replace(r"\ ", "[ -]") for word in sorted(ORDINALS, key=len, reverse=True))
    + r")\s+(?:of\s+)?(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b", re.I)


def named_date(text, now):
    matches = list(NAMED_DATE.finditer(text))
    if not matches:
        return None, text
    if len(matches) != 1:
        raise ValueError("Please give one date for that schedule")
    match = matches[0]
    word = match[1].lower().replace("-", " ")
    day = ORDINALS.get(word)
    if day is None:
        day = int(re.match(r"\d+", word)[0])
    month = MONTHS.index(match[2].lower()) + 1
    year = int(match[3]) if match[3] else now.year
    try:
        date = now.replace(year=year, month=month, day=day)
        if not match[3] and date.date() < now.date():
            date = date.replace(year=year+1)
    except ValueError as err:
        raise ValueError("That calendar date does not exist") from err
    return date, text[:match.start()] + " " + text[match.end():]


def validate_calendar(text):
    if re.search(r"\b(next (week|month|year)|january|february|march|april|june|july|august|september|october|november|december)\b|\b(?:in|of) may\b|\bmay \d|\b\d{4}-\d{2}-\d{2}\b", text.lower()):
        raise ValueError("Use today, tomorrow, a weekday, a day of the month, or an explicit dashboard date for that schedule")


def calendar_day(text):
    """Extract a day-of-month clause before number/clock normalization."""
    matches = list(DAY_OF_MONTH.finditer(text.lower()))
    if not matches:
        return None, text
    if len(matches) != 1:
        raise ValueError("Please give one date for that schedule")
    match = matches[0]
    word = match[1].replace("-", " ")
    day = ORDINALS.get(word)
    if day is None:
        day = int(re.match(r"\d+", word)[0])
    if not 1 <= day <= 31:
        raise ValueError("The day of the month must be between 1 and 31")
    return day, text[:match.start()] + " " + text[match.end():]


CLOCK_GAP = "That local time does not exist because the clocks change; please choose another time"


def resolve_day(text, now):
    """Name a spoken day (today, tomorrow, a weekday, "the 24th") as what
    EchoMuseList's `date` accepts; the next such day counting today."""
    text = (text or "").strip().lower()
    if text in ("today", "tomorrow"):
        return text
    if text in DAYS:
        # DAYS is Sunday-first; datetime.weekday() is Monday-first.
        ahead = (DAYS.index(text) + 6 - now.weekday()) % 7
        return (now + timedelta(days=ahead)).date().isoformat()
    day, _ = calendar_day(text)
    if day is None:
        return None
    for ahead in range(62):
        candidate = now + timedelta(days=ahead)
        if candidate.day == day:
            return candidate.date().isoformat()
    return None


def require_local_time(due):
    """Reject a wall-clock time the zone skips (the hour lost when clocks go forward)."""
    if datetime.fromtimestamp(due.timestamp(), due.tzinfo).replace(tzinfo=None) != due.replace(tzinfo=None):
        raise ValueError(CLOCK_GAP)
    return due


def next_calendar_day(now, day, hour, minute):
    """Next occurrence of this day and local clock, skipping short months."""
    year, month = now.year, now.month
    for _ in range(13):
        if day <= monthrange(year, month)[1]:
            due = now.replace(year=year, month=month, day=day,
                              hour=hour, minute=minute, second=0, microsecond=0)
            if due > now:
                return require_local_time(due)
        month = month % 12 + 1
        year += month == 1
    raise ValueError("Could not resolve that calendar date")


class Clarification(ValueError):
    """A missing scheduling detail, rather than a failed device operation."""
    def __init__(self, question, field):
        super().__init__(question)
        self.field = field


def normalize_clock(text):
    text = numbers(text)
    # STT engines vary in punctuation and spacing for spoken clock times.
    text = re.sub(r"(?<![a-z])([ap])\s*\.\s*m\.?", r"\1m", text)
    text = re.sub(r"\b([ap])\s+m\b", r"\1m", text)
    text = re.sub(r"(?<=\d)\.(?=\d{2}(?:\s*(?:am|pm)\b|\b))", ":", text)
    # Resolve named anchors before relative clock expressions.
    text = re.sub(r"\b(midday|noon)\b", "12:00pm", text)
    text = re.sub(r"\bmidnight\b", "12:00am", text)
    amount = r"(half|quarter|\d{1,2}(?: minutes?)?)"
    anchor = r"(\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?)"
    def offset(match):
        count = 30 if match[1] == "half" else 15 if match[1] == "quarter" else int(match[1].split()[0])
        if not 1 <= count <= 59:
            raise ValueError("Minutes past or to the hour must be between 1 and 59")
        clock = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", match[3])
        hour, minute, meridian = int(clock[1]), int(clock[2] or 0), clock[3]
        if minute > 59 or hour > 23 or (meridian and not 1 <= hour <= 12):
            raise ValueError("Invalid clock time")
        if not meridian and 1 <= hour <= 12:
            if re.search(r"\b(morning|am)\b", text):
                meridian = "am"
            elif re.search(r"\b(afternoon|evening|tonight|pm)\b", text):
                meridian = "pm"
            else:
                raise Clarification("Did you mean AM or PM?", "meridian")
        absolute = (hour % 12 + (12 if meridian == "pm" else 0)) if meridian else hour
        absolute = (absolute*60 + minute + (count if match[2] in ("past", "after") else -count)) % 1440
        return f"{(absolute//60)%12 or 12}:{absolute%60:02d}" + ("pm" if absolute >= 720 else "am")
    text = re.sub(amount + r" (past|after|to|before) " + anchor, offset, text)
    return text


def clock_match(text):
    # "for 3 days" is a duration inside the task, not a clock time.
    return re.search(r"(?:\b(?:at|for)\s+|^\s*)(\d{1,2})(?!\s*(?:seconds?|minutes?|hours?|days?|weeks?)\b)(?:(?::|\s+)(\d{2}))?\s*(am|pm)?(?:\b|$)(?!:)", text)


def numbers(text):
    def convert(m):
        words = m.group().replace("-", " ").split()
        total = current = 0
        for word in words:
            if word == "and":
                continue
            if word == "hundred":
                current = max(1, current) * 100
            elif word == "thousand":
                total += max(1, current) * 1000
                current = 0
            else:
                current += WORDS[word]
        return str(total + current)
    pattern = r"\b(?:" + "|".join([*WORDS, "hundred", "thousand"]) + r")\b"
    return re.sub(pattern + r"(?:(?:[ -])(?:and[ ])?" + pattern + r")*", convert, text.lower())


def duration(text):
    if re.search(r"(?<![a-z])-\s*\d|\b(minus|negative)\b", text.lower()):
        raise ValueError("Duration must be positive")
    text = text.lower().replace("-", " ")
    text = re.sub(r"\b(an?|one) (hour|minute|second|day|week) and (a )?half\b", r"1.5 \2", text)
    text = re.sub(r"\bhalf (an? )?(hour|minute|day)\b", r"0.5 \2", text)
    text = re.sub(r"\bquarter (of )?(an? )?hour\b", "15 minutes", text)
    text = re.sub(r"\b(an?|one) (?=(hour|minute|second|day|week)\b)", "1 ", text)
    text = numbers(text)
    text = re.sub(r"(\d+) and a half (hours?|minutes?|days?)", lambda m: str(int(m[1]) + 0.5) + " " + m[2], text)
    text = re.sub(r"(\d+) (hours?|minutes?|days?) and (a )?half", lambda m: str(int(m[1]) + 0.5) + " " + m[2], text)
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)\s*(seconds?|minutes?|hours?|days?|weeks?)\b", text))
    if not matches:
        raise Clarification("For how long? Say, for example, five minutes", "duration")
    factors = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}
    seconds = sum(float(m[1]) * factors[m[2].rstrip("s")] for m in matches)
    if seconds < 1 or seconds > 315360000 or seconds != int(seconds):
        raise ValueError("Duration must be whole seconds between one second and 3650 days")
    # Remove the duration and only the connectors that belong to it ("in",
    # "after", "for" before it, "and" between two of them). Stripping every
    # such word left in the text turned "take salt and pepper" into
    # "take salt pepper" and "call mum in London" into "call mum London".
    unit = r"\d+(?:\.\d+)?\s*(?:seconds?|minutes?|hours?|days?|weeks?)\b"
    remaining = re.sub(r"(?:\b(?:in|after|for)\s+)?(?:" + unit + r"(?:\s+and(?=\s+\d))?\s*)+", " ", text)
    return int(seconds), " ".join(remaining.split())


def starts_with_duration(text):
    """Whether the text opens with a duration ("five minutes", "an hour and a half")."""
    return bool(re.match(
        r"(?:\d+(?:\.\d+)?|an?|half|quarter)(?:\s+(?:and|a|an|of|half|\d+(?:\.\d+)?))*"
        r"\s+(?:seconds?|minutes?|hours?|days?|weeks?)\b", numbers(text)))


def reminder_message(text):
    """Remove English command connectors left around an extracted task."""
    text = " ".join(text.split())
    text = re.sub(r"^(?:(?:to|for|named|called)(?:\s+|$))+", "", text, flags=re.I)
    if text:
        # A time repeated in response to the message question is still a
        # scheduling detail, not the task. Keep asking without saving it.
        probe = normalize_clock(text)
        clock = clock_match(probe)
        if clock:
            rest = probe[:clock.start()] + " " + probe[clock.end():]
            rest = re.sub(r"\b(?:today|tomorrow|tonight|in|the|morning|afternoon|evening|on|at|for|"
                          + "|".join(DAYS) + r")\b", " ", rest)
            if not rest.strip(" .,!?:;"):
                text = ""
    if not text.strip(" .,!?:;"):
        raise Clarification("What should I remind you to do?", "message")
    return text


def parse_item(kind, request, zone, now=None):
    now = now or datetime.now(ZoneInfo(zone))
    now = now.astimezone(ZoneInfo(zone))
    raw = " ".join(request.strip().split())
    low = raw.lower()
    date, raw = named_date(raw, now)
    low = raw.lower()
    validate_calendar(low)
    item = {"kind": kind, "label": kind, "timezone": zone}
    # Explicit sound IDs can be selected without a mutable name catalogue.
    sound = re.search(r"\s+(?:with|using) (?:the )?sound ([a-f0-9]{64})$", low)
    if sound:
        item["sound"] = sound[1]
        raw, low = raw[:sound.start()], low[:sound.start()]
    day, raw = calendar_day(raw)
    low = raw.lower()
    if date is not None and day is not None:
        raise ValueError("Please give one date for that schedule")
    if (day is not None or date is not None) and (kind == "timer" or re.search(
            r"\b(today|tomorrow|every|each|" + "|".join(DAYS) + r")\b|\b(in|after)\s+(?!(?:the )?(?:morning|afternoon|evening)\b)", low)):
        raise ValueError("Use one calendar date and clock time for that schedule")
    rule = None
    recurrence = re.search(r"\b(?:every|each) (weekday[s]?|weekend[s]?|day|morning|(?:sunday|monday|tuesday|wednesday|thursday|friday|saturday)(?: and (?:sunday|monday|tuesday|wednesday|thursday|friday|saturday))*)\b", low)
    if recurrence:
        spec = recurrence[1]
        days = [1, 2, 3, 4, 5] if spec.startswith("weekday") else [0, 6] if spec.startswith("weekend") else list(range(7)) if spec in ("day", "morning") else [DAYS.index(d) for d in spec.split(" and ")]
        rule = {"days": days}
        raw = raw[:recurrence.start()] + raw[recurrence.end():]
        low = raw.lower()
        if spec == "morning" and not re.search(r"\b(am|pm|morning|afternoon|evening)\b", low):
            raw += " morning"
            low = raw.lower()
    interval = re.search(r"\b(?:every|each) (.+)$", low)
    if interval:
        seconds, rest = duration(interval[1])
        if rest:
            raise ValueError("Unsupported recurrence; use daily, weekdays, selected days, or an interval")
        rule = {"interval": seconds, "hour": 0, "minute": 0}
        raw, low = raw[:interval.start()], low[:interval.start()]
        item.update(rule=rule, due=int(now.timestamp()) + seconds)
        if kind == "reminder":
            item["message"] = reminder_message(raw)
        item["label"] = item.get("message") or kind
        return item
    # The LAST "in"/"after" that opens a duration: an earlier one belongs to
    # the task ("call mum in London in five minutes").
    relative = None
    for found in re.finditer(r"\b(in|after)\s+(?!(?:the )?(?:morning|afternoon|evening|midnight|noon|midday)\b)", low):
        if starts_with_duration(low[found.end():]):
            relative = found
    if relative:
        seconds, rest = duration(low[relative.end():])
        # Support "in five minutes remind me to ..." handled by grammar slot.
        message = (raw[:relative.start()] + " " + rest).strip()
        item["duration"] = seconds
    elif kind == "timer":
        seconds, message = duration(raw)
        item["duration"] = seconds
    else:
        # Clock clauses may precede or follow reminder text.
        normalized = normalize_clock(low)
        clock = clock_match(normalized)
        if not clock:
            raise Clarification("What time? Say, for example, 8am", "time")
        hour, minute = int(clock[1]), int(clock[2] or 0)
        meridian = clock[3]
        if not meridian:
            if "morning" in normalized:
                meridian = "am"
            elif "afternoon" in normalized or "evening" in normalized or "tonight" in normalized:
                meridian = "pm"
            elif 1 <= hour <= 12:
                raise Clarification("Did you mean AM or PM?", "meridian")
        if minute > 59 or hour > 23 or (meridian and not 1 <= hour <= 12):
            raise ValueError("Invalid clock time")
        if meridian:
            hour = hour % 12 + (12 if meridian.startswith("p") else 0)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if date is not None:
            due = date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                raise ValueError("That date and time have already passed")
            require_local_time(due)
        elif day is not None:
            due = next_calendar_day(now, day, hour, minute)
        elif "tomorrow" in normalized:
            due += timedelta(days=1)
        elif rule is None:
            weekday = next((d for d in DAYS if re.search(r"\b" + d + r"\b", normalized)), None)
            if weekday:
                pyday = (DAYS.index(weekday) + 6) % 7
                due += timedelta(days=(pyday - now.weekday()) % 7)
                if due <= now:
                    due += timedelta(days=7)
            elif due <= now:
                if "today" in normalized:
                    raise ValueError("That time has already passed today")
                due += timedelta(days=1)
        if rule is not None:
            rule.update(hour=hour, minute=minute)
            # Firmware computes calendar recurrence, including DST.
            due = None
        else:
            require_local_time(due)
        item["due"] = int(due.timestamp()) if due else 0
        message = normalized[:clock.start()] + " " + normalized[clock.end():]
        message = re.sub(r"\b(?:in the|this) (?:morning|afternoon|evening)\b", " ", message)
        message = re.sub(r"\b(today|tomorrow|morning|afternoon|evening|tonight|on|at|" + "|".join(DAYS) + r")\b", " ", message)
    if rule:
        item["rule"] = rule
    if kind == "reminder":
        message = reminder_message(message)
        item["message"] = message
    else:
        message = re.sub(r"^\s*(to|named|called|for)\s+", "", message).strip()
        message = " ".join(message.split())
    item["label"] = message or kind
    return item
