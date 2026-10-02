"""Calendar feeds in, cook blocks out."""

from datetime import date, datetime

import icalendar

from sous_chef import calendars

FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:1
SUMMARY:Dinner with Sam
DTSTART;TZID=America/New_York:20261006T220000
DTEND;TZID=America/New_York:20261007T000000
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Trip to Portland
DTSTART;VALUE=DATE:20261009
DTEND;VALUE=DATE:20261011
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Climbing
DTSTART:20260928T180000
DTEND:20260928T200000
RRULE:FREQ=WEEKLY;BYDAY=MO
END:VEVENT
BEGIN:VEVENT
UID:4
SUMMARY:Reminder: renew passport
TRANSP:TRANSPARENT
DTSTART:20261007T180000
DTEND:20261007T183000
END:VEVENT
END:VCALENDAR
"""


def test_feed_events_in_local_time_with_recurrences_expanded():
    evs = calendars.parse_ics(FEED, date(2026, 10, 5), date(2026, 10, 12), "America/Los_Angeles")
    by = {e["summary"]: e for e in evs}
    # 10 pm New York is 7 pm Seattle
    assert by["Dinner with Sam"]["start"].startswith("2026-10-06T19:00")
    assert by["Trip to Portland"]["all_day"] and by["Trip to Portland"]["end"] == "2026-10-11"
    assert by["Climbing"]["start"].startswith("2026-10-05T18:00")   # weekly, expanded
    assert "Reminder: renew passport" not in by                     # marked free


def test_a_broken_feed_is_a_message_not_a_crash():
    events, problems = calendars.fetch_events(["http://127.0.0.1:9/nope.ics"],
                                              date(2026, 10, 5), date(2026, 10, 12), "UTC")
    assert events == [] and len(problems) == 1


def test_webcal_links_are_fetched_over_https():
    assert calendars._https("webcal://p01-caldav.icloud.com/x") == "https://p01-caldav.icloud.com/x"


def test_cook_events_round_trip_through_a_calendar_parser():
    ics = calendars.cook_events_ics([{
        "uid": "sous-chef-1-2@sous-chef", "start": datetime(2026, 10, 5, 18, 15),
        "end": datetime(2026, 10, 5, 19, 0), "summary": "Cook: Chana Masala, with spinach",
        "description": "Ingredients\n• 3 cans chickpeas; drained\n" + "long line " * 30,
    }])
    for line in ics.split("\r\n"):
        assert len(line.encode()) <= 75
    cal = icalendar.Calendar.from_ical(ics)
    ev = next(c for c in cal.walk("VEVENT"))
    assert str(ev["SUMMARY"]) == "Cook: Chana Masala, with spinach"
    assert ev.decoded("DTSTART") == datetime(2026, 10, 5, 18, 15)       # floating local time
    assert "3 cans chickpeas; drained" in str(ev["DESCRIPTION"])
    assert next(c for c in cal.walk("VALARM"))
