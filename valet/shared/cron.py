"""Cron schedules for triggers (`schedule: "0 8 * * *"` in agent.yaml): crontab(5)'s five fields, when they next
fire in a time zone, and what they mean in plain words.

    minute hour day-of-month month day-of-week
    0-59   0-23 1-31         1-12  0-7 (0 and 7 are Sunday)
                             jan-dec  sun-sat

Each field is `*`, a value, a range `a-b`, or a list of those separated by commas, and any of them may take a step
(`*/15`, `9-17/2`, `5/10`, which is `5-59/10`). Month and weekday names are their first three letters, in any case.
`@hourly`, `@daily` (`@midnight`), `@weekly`, `@monthly` and `@yearly` (`@annually`) stand for the usual lines.

As in Vixie cron, when both day fields are restricted a day matches if *either* does (`0 9 1 * 1` is the 1st of
the month and every Monday); a day field that starts with `*` doesn't count as restricted (`*/2` included), and then
both must match.

Times are wall-clock times in the zone: a run in the hour that a change to summer time skips happens once, right
after the change; a time that the change back repeats runs once, the first time."""
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
          "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}
# (name, low, high, names) per field, in order.
FIELDS = (("minute", 0, 59, None), ("hour", 0, 23, None), ("day of month", 1, 31, None),
          ("month", 1, 12, MONTHS), ("day of week", 0, 7, DAYS))
MAX_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)    # per month, in a leap year
SEARCH_YEARS = 9            # the longest gap between two runs that can fire: Feb 29 (2096 -> 2104)
MAX_LENGTH = 200
_ITEM = re.compile(r"^(\*|[a-z0-9]+(?:-[a-z0-9]+)?)(?:/(\d+))?$")


class CronError(ValueError):
    """A schedule that doesn't parse, or never fires; the message is shown to the owner as is."""


@dataclass(frozen=True)
class Cron:
    expr: str                       # as written (stripped)
    minutes: tuple[int, ...]        # sorted
    hours: tuple[int, ...]
    days: tuple[int, ...]           # of the month
    months: tuple[int, ...]
    weekdays: tuple[int, ...]       # 0 = Sunday .. 6
    dom_star: bool                  # the day-of-month field starts with `*`
    dow_star: bool

    def day_matches(self, d: date) -> bool:
        dom = d.day in self.days
        dow = d.isoweekday() % 7 in self.weekdays
        return (dom and dow) if (self.dom_star or self.dow_star) else (dom or dow)

    def next_after(self, after: datetime, tz: tzinfo) -> datetime | None:
        """The first run strictly after `after` (an aware datetime), as an aware datetime in `tz`; None if there is
        none within SEARCH_YEARS (parse() refuses schedules that never fire, so only a bug gets there)."""
        start = after.astimezone(UTC)
        t = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=366 * SEARCH_YEARS)
        while t < limit:
            if t.month not in self.months:
                t = datetime(t.year + (t.month == 12), t.month % 12 + 1, 1)
            elif not self.day_matches(t.date()):
                t = datetime(t.year, t.month, t.day) + timedelta(days=1)
            elif t.hour not in self.hours:
                h = next((h for h in self.hours if h > t.hour), None)
                t = datetime(t.year, t.month, t.day) + timedelta(days=1) if h is None else t.replace(hour=h, minute=0)
            elif t.minute not in self.minutes:
                m = next((m for m in self.minutes if m > t.minute), None)
                t = t.replace(minute=0) + timedelta(hours=1) if m is None else t.replace(minute=m)
            else:
                # fold=0: a time the change to summer time skips is read with the offset before it (so it lands
                # just after the change); a repeated time is its first occurrence. A wall time that's still not
                # after `after` (we started inside a repeated hour) moves on.
                at = t.replace(tzinfo=tz).astimezone(UTC)
                if at > start:
                    return at.astimezone(tz)
                t += timedelta(minutes=1)
        return None

    def upcoming(self, after: datetime, tz: tzinfo, count: int) -> list[datetime]:
        out: list[datetime] = []
        t = after
        while len(out) < count and (t := self.next_after(t, tz)) is not None:
            out.append(t)
        return out

    def describe(self) -> str:
        """What it means, in plain words: "Every day at 08:00", "Weekdays at 09:30", "Every 15 minutes"."""
        return describe(self)


def _value(text: str, lo: int, hi: int, names: tuple[str, ...] | None, field: str) -> int:
    if text.isdigit():
        v = int(text)
    elif names and text in names:
        v = names.index(text) + (1 if names is MONTHS else 0)
    else:
        what = "a number" + (f" or a name like {names[0]}" if names else "")
        raise CronError(f"{field}: {text!r} isn't {what}")
    if not lo <= v <= hi:
        raise CronError(f"{field}: {v} is out of range ({lo}-{hi})")
    return v


def _field(text: str, lo: int, hi: int, names: tuple[str, ...] | None, field: str) -> set[int]:
    out: set[int] = set()
    for item in text.split(","):
        m = _ITEM.match(item)
        if not m:
            raise CronError(f"{field}: {item or 'an empty item'!r} isn't a value, a range or *")
        base, step = m.group(1), m.group(2)
        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            a, b = (_value(x, lo, hi, names, field) for x in base.split("-"))
            if a > b:
                raise CronError(f"{field}: the range {base} runs backwards")
        else:
            a = _value(base, lo, hi, names, field)
            b = hi if step else a           # `5/10` is `5-59/10`
        n = int(step) if step else 1
        if n < 1 or n > hi - lo + 1:
            raise CronError(f"{field}: a step of {step} doesn't fit {lo}-{hi}")
        out.update(range(a, b + 1, n))
    return out


@lru_cache(maxsize=512)
def parse(expr: str) -> Cron:
    """A schedule -> Cron. CronError if it doesn't parse or can never fire (April 31st)."""
    if not isinstance(expr, str):
        raise CronError("a schedule is text, like \"0 8 * * *\"")
    text = expr.strip()
    if len(text) > MAX_LENGTH:
        raise CronError(f"a schedule is at most {MAX_LENGTH} characters")
    line = MACROS.get(text.lower(), text) if text.startswith("@") else text
    if line.startswith("@"):
        raise CronError(f"{text}: not one of {', '.join(MACROS)}")
    parts = line.lower().split()
    if len(parts) != 5:
        raise CronError("five fields, separated by spaces: minute hour day-of-month month day-of-week "
                        "(like \"0 8 * * *\": every day at 08:00), or @hourly, @daily, @weekly, @monthly")
    sets = [_field(p, lo, hi, names, name) for p, (name, lo, hi, names) in zip(parts, FIELDS)]
    weekdays = {d % 7 for d in sets[4]}
    cron = Cron(expr=text, minutes=tuple(sorted(sets[0])), hours=tuple(sorted(sets[1])),
                days=tuple(sorted(sets[2])), months=tuple(sorted(sets[3])), weekdays=tuple(sorted(weekdays)),
                dom_star=parts[2].startswith("*"), dow_star=parts[4].startswith("*"))
    # Only the day of the month can make a schedule impossible (weekdays recur every week): when it alone decides,
    # or both must match, some chosen month must have some chosen day.
    if cron.dow_star or cron.dom_star:
        if not any(d <= MAX_DAYS[m - 1] for m in cron.months for d in cron.days):
            raise CronError(f"{text}: those days never happen in those months, so it would never run")
    return cron


def check(expr: str) -> str | None:
    """Why `expr` isn't a schedule, or None."""
    try:
        parse(expr)
    except CronError as e:
        return str(e)
    return None


# ---------- time zones ----------

def zone(name: str | None = None) -> tzinfo:
    """`name` (an IANA zone like Europe/Lisbon), else the TZ environment variable, else the machine's own zone
    (/etc/localtime), else its current fixed offset. CronError for a name that isn't a zone."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise CronError(f"{name!r} isn't a time zone (an IANA name like Europe/Lisbon)") from None
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        try:
            return ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    return datetime.now().astimezone().tzinfo or UTC


def zone_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


# ---------- plain words ----------

def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _run(values: tuple[int, ...]) -> tuple[int, int, int] | None:
    """(first, last, step) if the values are an arithmetic series of 3 or more (or 2 consecutive ones)."""
    if len(values) < 2:
        return None
    step = values[1] - values[0]
    if any(b - a != step for a, b in zip(values, values[1:])):
        return None
    return (values[0], values[-1], step) if len(values) > 2 or step == 1 else None


def _times(c: Cron) -> str:
    """The time-of-day part: "at 08:00", "every 15 minutes", "every hour at :05"..."""
    all_min, all_hours = len(c.minutes) == 60, len(c.hours) == 24
    hrun, mrun = _run(c.hours), _run(c.minutes)
    if all_min and all_hours:
        return "every minute"
    if all_hours:
        if mrun and mrun[0] == 0 and mrun[2] > 1 and 60 % mrun[2] == 0 and mrun[1] + mrun[2] == 60:
            return f"every {mrun[2]} minutes"
        if c.minutes == (0,):
            return "every hour, on the hour"
        if len(c.minutes) <= 4:
            return "every hour at " + _and([f":{m:02}" for m in c.minutes])
        return "every hour at minutes " + _list(c.minutes)
    if len(c.minutes) * len(c.hours) <= 6:
        return "at " + _and([f"{h:02}:{m:02}" for h in c.hours for m in c.minutes])
    if hrun and hrun[2] == 1:
        span = f"from {hrun[0]:02}:00 to {hrun[1]:02}:59"
        if all_min:
            return "every minute " + span
        if mrun and mrun[0] == 0 and 60 % mrun[2] == 0 and mrun[1] + mrun[2] == 60:
            return f"every {mrun[2]} minutes " + span
        if len(c.minutes) <= 4:
            return "every hour at " + _and([f":{m:02}" for m in c.minutes]) + " " + span
    if hrun and hrun[0] == 0 and 24 % hrun[2] == 0 and hrun[1] + hrun[2] == 24 and len(c.minutes) == 1:
        return f"every {hrun[2]} hours at :{c.minutes[0]:02}"
    if all_min:
        return f"every minute of hours {_list(c.hours)}"
    return f"at minutes {_list(c.minutes)} of hours {_list(c.hours)}"


def _list(values: tuple[int, ...]) -> str:
    run = _run(values)
    if run and run[2] == 1:
        return f"{run[0]} to {run[1]}"
    if run:
        return f"{run[0]} to {run[1]}, every {run[2]}"
    return _and([str(v) for v in values])


def _weekdays(days: tuple[int, ...]) -> str:
    """"weekdays", "Mondays", "Monday to Wednesday", "Mondays and Fridays"."""
    if days == (1, 2, 3, 4, 5):
        return "weekdays"
    if days == (0, 6):
        return "weekends"
    run = _run(days)
    if run and run[2] == 1 and len(days) >= 3:
        return f"{DAY_NAMES[run[0]]} to {DAY_NAMES[run[1]]}"
    return _and([DAY_NAMES[d] + "s" for d in days])


def _monthdays(days: tuple[int, ...]) -> str:
    """What follows "on": "the 1st", "the 1st and 15th", "the 1st to the 7th", "every other day from the 1st"."""
    run = _run(days)
    if run and len(days) > 3:
        if run[2] == 1:
            return f"the {_ordinal(run[0])} to the {_ordinal(run[1])}"
        every = "every other day" if run[2] == 2 else f"every {_ordinal(run[2])} day"
        return f"{every} from the {_ordinal(run[0])}"
    return "the " + _and([_ordinal(d) for d in days])


def _months(months: tuple[int, ...]) -> str:
    run = _run(months)
    if run and run[2] == 1 and len(months) >= 3:
        return f"{MONTH_NAMES[run[0] - 1]} to {MONTH_NAMES[run[1] - 1]}"
    return _and([MONTH_NAMES[m - 1] for m in months])


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def describe(c: Cron) -> str:
    times = _times(c)
    dom, dow = len(c.days) < 31, len(c.weekdays) < 7
    either = not (c.dom_star or c.dow_star)         # Vixie's rule: a day matches if either field does
    if either and not (dom and dow):
        dom = dow = False                           # one of them takes every day
    all_months = len(c.months) == 12
    of = "every month" if all_months else _months(c.months)
    run = _run(c.months)
    month_run = run and run[2] == 1 and len(c.months) >= 3
    tail = "" if all_months else (" from " if month_run else " in ") + _months(c.months)
    if dom and dow and either:
        day = f"on {_monthdays(c.days)} of every month and on {_weekdays(c.weekdays)}"
    elif dom and dow:
        day = f"on {_monthdays(c.days)} of {of}, if it's a {' or '.join(DAY_NAMES[d] for d in c.weekdays)},"
        tail = ""
    elif dom:
        day, tail = f"on {_monthdays(c.days)} of {of}", ""
    elif dow:
        day = _weekdays(c.weekdays)
    else:
        day = ""
    if times.startswith("at "):
        text = f"{_cap(day)} {times}" if day else f"Every day {times}"
    elif dow and not dom:
        text = f"{_cap(times)} on {day}"
    else:
        text = f"{_cap(times)} {day.rstrip(',')}" if day else _cap(times)
    return text + tail


def humanize(expr: str) -> str:
    """describe(parse(expr)), or the expression itself if it doesn't parse."""
    try:
        return parse(expr).describe()
    except CronError:
        return expr
