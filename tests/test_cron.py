"""valet.shared.cron: parsing crontab(5)'s five fields, the next run across months, leap years and DST, and the
plain-language description the UI shows."""
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from valet.shared import cron

NY = ZoneInfo("America/New_York")
LISBON = ZoneInfo("Europe/Lisbon")


def at(*args, tz=UTC):
    return datetime(*args, tzinfo=tz)


def nxt(expr, after, tz=UTC):
    return cron.parse(expr).next_after(after, tz)


# ---------- fields ----------

def test_fields_values_ranges_lists_steps():
    c = cron.parse("0,30 9-17/2 1,15 */3 1-5")
    assert c.minutes == (0, 30)
    assert c.hours == (9, 11, 13, 15, 17)
    assert c.days == (1, 15)
    assert c.months == (1, 4, 7, 10)
    assert c.weekdays == (1, 2, 3, 4, 5)
    assert not c.dom_star and not c.dow_star


def test_a_value_with_a_step_runs_to_the_end_of_the_field():
    assert cron.parse("5/20 * * * *").minutes == (5, 25, 45)
    assert cron.parse("* 20/2 * * *").hours == (20, 22)


def test_names_any_case_and_sunday_is_0_or_7():
    c = cron.parse("0 0 * JAN,jul Mon-wed")
    assert c.months == (1, 7) and c.weekdays == (1, 2, 3)
    assert cron.parse("0 0 * * 7").weekdays == (0,)
    assert cron.parse("0 0 * * 5-7").weekdays == (0, 5, 6)
    assert cron.parse("0 0 * * sun").weekdays == (0,)
    assert cron.parse("0 0 * dec *").months == (12,)


def test_macros():
    assert cron.parse("@daily").minutes == (0,) and cron.parse("@daily").hours == (0,)
    assert cron.parse("@HOURLY").hours == tuple(range(24))
    assert cron.parse("@weekly").weekdays == (0,)
    assert cron.parse("@monthly").days == (1,)
    y, a = cron.parse("@yearly"), cron.parse("@annually")
    assert (y.minutes, y.hours, y.days, y.months, y.weekdays) == (a.minutes, a.hours, a.days, a.months, a.weekdays)
    assert cron.parse("@midnight").describe() == "Every day at 00:00"


def test_spaces_around_and_between_fields():
    assert cron.parse("  0   8 *  * *  ").hours == (8,)


@pytest.mark.parametrize("expr, says", [
    ("", "five fields"),
    ("* * * *", "five fields"),
    ("* * * * * *", "five fields"),
    ("60 * * * *", "minute: 60 is out of range (0-59)"),
    ("* 24 * * *", "hour: 24 is out of range (0-23)"),
    ("* * 0 * *", "day of month: 0 is out of range (1-31)"),
    ("* * * 13 *", "month: 13 is out of range (1-12)"),
    ("* * * * 8", "day of week: 8 is out of range (0-7)"),
    ("5-1 * * * *", "the range 5-1 runs backwards"),
    ("*/0 * * * *", "a step of 0"),
    ("*/61 * * * *", "a step of 61"),
    ("1,,2 * * * *", "an empty item"),
    ("a * * * *", "minute: 'a' isn't a number"),
    ("* * * foo *", "month: 'foo' isn't a number or a name like jan"),
    ("* * * * monday", "isn't a number or a name like sun"),
    ("0 0 * * mon-", "isn't a value, a range or *"),
    ("-5 * * * *", "isn't a value"),
    ("@reboot", "not one of @yearly"),
    ("0 0 31 4 *", "never run"),
    ("0 0 30 2 *", "never run"),
    ("0 0 31 2,4,6 *", "never run"),
    ("0 0 " + "1," * 120 + "1 * *", "at most 200 characters"),
])
def test_what_isnt_a_schedule(expr, says):
    err = cron.check(expr)
    assert err and says in err, err
    with pytest.raises(cron.CronError):
        cron.parse(expr)


def test_a_rare_day_is_fine():
    assert cron.check("0 0 29 2 *") is None          # every leap year
    assert cron.check("0 0 31 2,3 *") is None        # March has a 31st
    assert cron.check("0 0 31 4 1") is None          # April 31st or Mondays: the Mondays happen
    assert cron.check(42) is not None


# ---------- next runs ----------

def test_next_is_strictly_after():
    assert nxt("0 8 * * *", at(2026, 11, 1, 7, 59)) == at(2026, 11, 1, 8, 0)
    assert nxt("0 8 * * *", at(2026, 11, 1, 8, 0)) == at(2026, 11, 2, 8, 0)
    assert nxt("0 8 * * *", at(2026, 11, 1, 8, 0, 30)) == at(2026, 11, 2, 8, 0)
    assert nxt("* * * * *", at(2026, 11, 1, 8, 0, 59, 999)) == at(2026, 11, 1, 8, 1)


def test_next_rolls_over_hours_days_months_and_years():
    assert nxt("*/15 * * * *", at(2026, 12, 31, 23, 50)) == at(2027, 1, 1, 0, 0)
    assert nxt("30 * * * *", at(2026, 1, 31, 23, 31)) == at(2026, 2, 1, 0, 30)
    assert nxt("0 0 1 * *", at(2026, 12, 5)) == at(2027, 1, 1)
    assert nxt("0 9 * jan *", at(2026, 2, 1)) == at(2027, 1, 1, 9)
    assert nxt("0 0 31 * *", at(2026, 4, 1)) == at(2026, 5, 31)


def test_leap_day():
    assert nxt("0 0 29 2 *", at(2026, 3, 1)) == at(2028, 2, 29)
    assert nxt("0 0 29 2 *", at(2096, 3, 1)) == at(2104, 2, 29)      # 2100 isn't a leap year


def test_weekdays():
    # 2026-11-01 is a Sunday.
    assert nxt("30 9 * * 1-5", at(2026, 11, 1)) == at(2026, 11, 2, 9, 30)
    assert nxt("30 9 * * 1-5", at(2026, 11, 6, 10)) == at(2026, 11, 9, 9, 30)
    assert nxt("0 12 * * sat,sun", at(2026, 11, 2)) == at(2026, 11, 7, 12)
    assert nxt("0 12 * * 7", at(2026, 11, 2)) == at(2026, 11, 8, 12)


def test_both_day_fields_restricted_means_either():
    c = cron.parse("0 9 13 * 5")        # the 13th, or any Friday
    runs = c.upcoming(at(2026, 11, 1), UTC, 4)
    assert runs == [at(2026, 11, 6, 9), at(2026, 11, 13, 9), at(2026, 11, 20, 9), at(2026, 11, 27, 9)]
    runs = cron.parse("0 9 1 * 1").upcoming(at(2026, 11, 28), UTC, 3)     # the 1st, or Mondays
    assert runs == [at(2026, 11, 30, 9), at(2026, 12, 1, 9), at(2026, 12, 7, 9)]


def test_a_day_field_starting_with_a_star_makes_both_count():
    c = cron.parse("0 9 */2 * 1")       # odd days of the month that are also Mondays
    assert c.dom_star and not c.dow_star
    runs = c.upcoming(at(2026, 11, 1), UTC, 3)
    assert runs == [at(2026, 11, 9, 9), at(2026, 11, 23, 9), at(2026, 12, 7, 9)]
    assert all(r.isoweekday() == 1 and r.day % 2 == 1 for r in runs)
    assert cron.parse("0 9 * * */2").weekdays == (0, 2, 4, 6)


def test_upcoming_counts():
    runs = cron.parse("0 */6 * * *").upcoming(at(2026, 11, 1, 1), UTC, 5)
    assert [r.hour for r in runs] == [6, 12, 18, 0, 6]


def test_times_are_wall_clock_in_the_zone():
    # 08:00 in Lisbon is 08:00 UTC in winter and 07:00 UTC in summer.
    assert nxt("0 8 * * *", at(2027, 1, 10), LISBON) == at(2027, 1, 10, 8, tz=LISBON)
    assert nxt("0 8 * * *", at(2027, 1, 10), LISBON).astimezone(UTC) == at(2027, 1, 10, 8)
    assert nxt("0 8 * * *", at(2027, 7, 10), LISBON).astimezone(UTC) == at(2027, 7, 10, 7)
    r = nxt("0 8 * * *", at(2026, 11, 1, 12), NY)
    # 12:00 UTC on 2026-11-01 is 07:00 in New York (the clocks went back at 06:00 UTC).
    assert r.tzinfo is NY and (r.hour, r.minute) == (8, 0) and r.date().isoformat() == "2026-11-01"


def test_spring_forward_runs_a_skipped_time_once_just_after():
    # New York, 2026-03-08: 02:00 EST becomes 03:00 EDT.
    r = nxt("30 2 * * *", at(2026, 3, 8, 1, tz=NY), NY)
    assert r.astimezone(UTC) == at(2026, 3, 8, 7, 30) and (r.hour, r.minute) == (3, 30)
    assert nxt("30 2 * * *", r, NY) == at(2026, 3, 9, 2, 30, tz=NY)
    runs = cron.parse("*/30 * * * *").upcoming(at(2026, 3, 8, 1, 15, tz=NY), NY, 4)
    assert [(x.hour, x.minute) for x in runs] == [(1, 30), (3, 0), (3, 30), (4, 0)]
    assert len({x.astimezone(UTC) for x in runs}) == 4


def test_fall_back_runs_a_repeated_time_once():
    # New York, 2026-11-01: 02:00 EDT becomes 01:00 EST; 01:30 happens twice.
    c = cron.parse("30 1 * * *")
    first = c.next_after(at(2026, 11, 1, 0, tz=NY), NY)
    assert first.astimezone(UTC) == at(2026, 11, 1, 5, 30)      # 01:30 EDT
    assert c.next_after(first, NY).astimezone(UTC) == at(2026, 11, 2, 6, 30)
    # Starting inside the repeated hour (01:10 EST): the first 01:30 has passed; it isn't run again.
    inside = datetime(2026, 11, 1, 1, 10, tzinfo=NY, fold=1)
    assert inside.astimezone(UTC) == at(2026, 11, 1, 6, 10)
    assert c.next_after(inside, NY).astimezone(UTC) == at(2026, 11, 2, 6, 30)
    runs = cron.parse("0 * * * *").upcoming(at(2026, 11, 1, 0, 30, tz=NY), NY, 3)
    assert [x.astimezone(UTC) for x in runs] == [at(2026, 11, 1, 5), at(2026, 11, 1, 7), at(2026, 11, 1, 8)]


def test_lisbon_changes():
    # Europe/Lisbon: 2027-03-28 01:00 WET -> 02:00 WEST; 2026-10-25 02:00 WEST -> 01:00 WET.
    r = nxt("30 1 * * *", at(2027, 3, 27, 12, tz=LISBON), LISBON)
    assert r.astimezone(UTC) == at(2027, 3, 28, 1, 30) and r.hour == 2
    r = nxt("30 1 * * *", at(2026, 10, 24, 12, tz=LISBON), LISBON)
    assert r.astimezone(UTC) == at(2026, 10, 25, 0, 30)


def test_a_long_search_is_quick():
    import time
    t = time.monotonic()
    for _ in range(50):
        cron.parse("0 0 29 2 1").next_after(at(2026, 1, 1), UTC)
        cron.parse("0 0 29 2 *").next_after(at(2097, 1, 1), UTC)
    assert time.monotonic() - t < 2


# ---------- plain words ----------

@pytest.mark.parametrize("expr, words", [
    ("0 8 * * *", "Every day at 08:00"),
    ("30 9 * * 1-5", "Weekdays at 09:30"),
    ("0 10 * * sat,sun", "Weekends at 10:00"),
    ("0 8 * * 1", "Mondays at 08:00"),
    ("0 8 * * 1,3,5", "Mondays, Wednesdays and Fridays at 08:00"),
    ("0 8 * * 1-3", "Monday to Wednesday at 08:00"),
    ("*/15 * * * *", "Every 15 minutes"),
    ("* * * * *", "Every minute"),
    ("5 * * * *", "Every hour at :05"),
    ("0 * * * *", "Every hour, on the hour"),
    ("0 */2 * * *", "Every 2 hours at :00"),
    ("0 8,20 * * *", "Every day at 08:00 and 20:00"),
    ("*/10 9-17 * * 1-5", "Every 10 minutes from 09:00 to 17:59 on weekdays"),
    ("0 0 1 * *", "On the 1st of every month at 00:00"),
    ("15 10 1,15 * *", "On the 1st and 15th of every month at 10:15"),
    ("0 0 1 1 *", "On the 1st of January at 00:00"),
    ("0 9 1 * 1", "On the 1st of every month and on Mondays at 09:00"),
    ("7 3 * jan,jul *", "Every day at 03:07 in January and July"),
    ("0 12 * 6-8 sat,sun", "Weekends at 12:00 from June to August"),
    ("0 0 22 11 *", "On the 22nd of November at 00:00"),
    ("@weekly", "Sundays at 00:00"),
])
def test_descriptions(expr, words):
    assert cron.parse(expr).describe() == words
    assert cron.humanize(expr) == words


def test_humanize_leaves_a_bad_expression_as_it_is():
    assert cron.humanize("nope") == "nope"


def test_every_description_is_one_line_for_odd_schedules():
    for expr in ["0,15,30,45,50 1,5,9 * * *", "1-10 * * * *", "* 1,5 * * *", "0 9 */2 * 1", "0 0 1-7 * *"]:
        words = cron.parse(expr).describe()
        assert words and "\n" not in words and words[0].isupper(), (expr, words)


# ---------- zones ----------

def test_zone(monkeypatch):
    assert cron.zone("Europe/Lisbon") == LISBON
    assert cron.zone_name(cron.zone("Europe/Lisbon")) == "Europe/Lisbon"
    with pytest.raises(cron.CronError, match="isn't a time zone"):
        cron.zone("Mars/Olympus")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert cron.zone_name(cron.zone()) == "Asia/Tokyo"
    monkeypatch.setenv("TZ", "not a zone")
    assert cron.zone() is not None          # the machine's own (/etc/localtime), or its current offset
    assert cron.zone_name(timezone(timedelta(hours=2)))
