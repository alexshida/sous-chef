"""Calendars in and out, without signing in to anything.

In: the private iCal feed URLs that Google Calendar and iCloud both provide.
Google's is "Secret address in iCal format" (Settings → your calendar →
Integrate calendar). iCloud's is the link from sharing a calendar as a Public
Calendar. Both are read-only and are fetched by this machine, never the phone.

Out: an .ics file of cook blocks. Opened on an iPhone it offers "Add All" to
whichever calendar you choose — iCloud or Google alike. Times are written as
floating local time, so they land at 6:15 pm wherever the phone is.

Two-way sync (EventKit) is what the native app will add; this is the version
that needs no OAuth and no app-specific passwords.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_CACHE: dict[tuple[str, str], tuple[float, list[dict], str | None]] = {}
_TTL = 600
_FAIL_TTL = 120


class FeedError(RuntimeError):
    pass


def _https(url: str) -> str:
    url = url.strip()
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def parse_ics(text: str | bytes, start: date, end: date, tz: str) -> list[dict]:
    """Events overlapping [start, end), recurring ones expanded, in local time."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(text)
    zone = ZoneInfo(tz)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        summary = str(ev.get("SUMMARY", "")).strip() or "(busy)"
        if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT" and "dinner" not in summary.lower():
            continue   # "free" events — a reminder, a birthday — don't block an evening
        s = ev.decoded("DTSTART")
        e = ev.decoded("DTEND") if ev.get("DTEND") else None
        if isinstance(s, datetime):
            s = _local(s, zone)
            e = _local(e, zone) if isinstance(e, datetime) else s + timedelta(hours=1)
            out.append({"summary": summary, "start": s.isoformat(), "end": e.isoformat(),
                        "all_day": False})
        else:
            e = e if isinstance(e, date) else s + timedelta(days=1)
            out.append({"summary": summary, "start": s.isoformat(), "end": e.isoformat(),
                        "all_day": True})
    return sorted(out, key=lambda x: x["start"])


def _local(dt: datetime, zone: ZoneInfo) -> datetime:
    if dt.tzinfo is None:          # floating time: already local
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def fetch_events(urls: list[str], start: date, end: date, tz: str) -> tuple[list[dict], list[str]]:
    """Events from every feed, and a readable problem for each feed that failed."""
    import httpx

    events: list[dict] = []
    problems: list[str] = []
    for url in [u for u in urls if u.strip()]:
        key = (url, start.isoformat())
        hit = _CACHE.get(key)
        # Failures are remembered briefly too: every screen asks for the week,
        # and a dead feed must not cost a 15-second timeout on each of them.
        if hit and _time.time() - hit[0] < (_TTL if hit[2] is None else _FAIL_TTL):
            events += hit[1]
            if hit[2]:
                problems.append(hit[2])
            continue
        label = _label(url)
        try:
            r = httpx.get(_https(url), timeout=10, follow_redirects=True)
            r.raise_for_status()
            evs, problem = parse_ics(r.content, start, end, tz), None
        except httpx.HTTPStatusError as e:
            evs, problem = [], (f"{label}: the feed answered {e.response.status_code} — "
                                "check the link is the private/public iCal address.")
        except Exception as e:  # network, or not a calendar at all
            evs, problem = [], f"{label}: {type(e).__name__}: {e}"
        _CACHE[key] = (_time.time(), evs, problem)
        events += evs
        if problem:
            problems.append(problem)
    return events, problems


def _label(url: str) -> str:
    u = _https(url)
    if "google.com" in u:
        return "Google Calendar feed"
    if "icloud.com" in u:
        return "iCloud feed"
    return u.split("/")[2] if "//" in u else "calendar feed"


# ── export ───────────────────────────────────────────────────

def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545 folding: lines over 75 octets continue on a line starting with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def _floating(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def cook_events_ics(events: list[dict], *, calendar_name: str = "Cooking",
                    alarm_min: int = 15) -> str:
    """An .ics with one event per cook block.

    `events` items: uid, start (naive datetime), end, summary, description.
    UIDs are stable per plan and recipe, so importing again updates the event
    rather than adding a duplicate.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sous-chef//meal plan//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_escape(calendar_name)}"]
    for ev in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_floating(ev['start'])}",
            f"DTEND:{_floating(ev['end'])}",
            f"SUMMARY:{_escape(ev['summary'])}",
            f"DESCRIPTION:{_escape(ev['description'])}",
        ]
        if alarm_min:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(ev['summary'])}",
                      f"TRIGGER:-PT{alarm_min}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
