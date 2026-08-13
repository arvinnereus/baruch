"""Google Calendar integration via the calendar's private ICS URL
(Google Calendar → Settings → [calendar] → 'Secret address in iCal format').
Read-only, no OAuth. Parses today's events incl. simple daily/weekly recurrences."""
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CACHE_TTL = 300
_caches: dict = {}  # url -> {"at": datetime, "events": [...]}

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _unfold(text: str) -> list[str]:
    """ICS line unfolding: continuation lines start with space/tab."""
    out = []
    for ln in text.splitlines():
        if ln[:1] in (" ", "\t") and out:
            out[-1] += ln[1:]
        else:
            out.append(ln)
    return out


def _parse_dt(prop: str, value: str, default_tz):
    """Parse DTSTART/DTEND property with optional TZID. Returns (datetime|date, all_day)."""
    tzm = re.search(r"TZID=([^;:]+)", prop)
    tz = ZoneInfo(tzm.group(1)) if tzm else None
    if re.fullmatch(r"\d{8}", value):  # all-day
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return None, False
    d, t, z = m.groups()
    dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:8]),
                  int(t[:2]), int(t[2:4]), int(t[4:6]))
    if z:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.replace(tzinfo=tz or default_tz)
    return dt, False


def _fetch(url: str) -> str:
    if url.startswith("file://"):  # for tests
        return Path(url[7:]).read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "Baruch/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def _attendee_name(line: str) -> str | None:
    m = re.search(r"CN=([^;:]+)", line)
    if m:
        name = m.group(1).strip('"')
        if "@" not in name:
            return name
        return name.split("@")[0]
    m = re.search(r"mailto:([^@\s]+)@", line, re.I)
    return m.group(1) if m else None


def parse_events(ics_text: str, local_tz=None):
    """Return raw event dicts from an ICS body."""
    local_tz = local_tz or datetime.now().astimezone().tzinfo
    events, cur = [], None
    for ln in _unfold(ics_text):
        if ln.startswith("BEGIN:VEVENT"):
            cur = {"attendees": [], "rrule": None, "exdates": set(), "all_day": False}
        elif ln.startswith("END:VEVENT") and cur is not None:
            if cur.get("start") is not None:
                events.append(cur)
            cur = None
        elif cur is None:
            continue
        elif ln.startswith("DTSTART"):
            prop, _, val = ln.partition(":")
            cur["start"], cur["all_day"] = _parse_dt(prop, val.strip(), local_tz)
        elif ln.startswith("DTEND"):
            prop, _, val = ln.partition(":")
            cur["end"], _ = _parse_dt(prop, val.strip(), local_tz)
        elif ln.startswith("SUMMARY"):
            cur["title"] = ln.partition(":")[2].strip().replace("\\,", ",")
        elif ln.startswith("UID"):
            cur["uid"] = ln.partition(":")[2].strip()
        elif ln.startswith("RRULE"):
            cur["rrule"] = ln.partition(":")[2].strip()
        elif ln.startswith("EXDATE"):
            for v in ln.partition(":")[2].split(","):
                cur["exdates"].add(v.strip()[:8])
        elif ln.startswith("ATTENDEE") or ln.startswith("ORGANIZER"):
            name = _attendee_name(ln)
            if name and name not in cur["attendees"]:
                cur["attendees"].append(name)
        elif ln.startswith("STATUS:CANCELLED"):
            cur["cancelled"] = True
    return events


def _occurs_today(ev, today: date) -> datetime | None:
    """Return today's start datetime if the event occurs today. Handles simple
    DAILY/WEEKLY RRULEs including COUNT (hospital systems export one-off
    appointments as FREQ=DAILY;COUNT=1!) and UNTIL. Google expands most
    modified instances as separate VEVENTs."""
    start = ev["start"]
    if ev.get("cancelled") or ev["all_day"]:
        return None
    start = start.astimezone()  # normalize to local tz so .date() is the local day
    sdate = start.date()
    if sdate == today:
        return start
    rr = ev.get("rrule")
    if not rr or sdate > today:
        return None
    if today.strftime("%Y%m%d") in ev["exdates"]:
        return None
    until = re.search(r"UNTIL=(\d{8})", rr)
    if until and today > date(int(until.group(1)[:4]), int(until.group(1)[4:6]),
                              int(until.group(1)[6:8])):
        return None
    freq_m = re.search(r"FREQ=(\w+)", rr)
    freq = freq_m.group(1) if freq_m else ""
    interval = int(re.search(r"INTERVAL=(\d+)", rr).group(1)) if "INTERVAL=" in rr else 1
    count_m = re.search(r"COUNT=(\d+)", rr)
    count = int(count_m.group(1)) if count_m else None
    delta_days = (today - sdate).days
    todays_start = datetime.combine(today, start.timetz())

    if freq == "DAILY":
        if delta_days % interval != 0:
            return None
        occurrence_idx = delta_days // interval  # 0-based
        if count is not None and occurrence_idx >= count:
            return None
        return todays_start

    if freq == "WEEKLY":
        byday = re.search(r"BYDAY=([^;]+)", rr)
        days = [d for d in (byday.group(1).split(",") if byday else
                            [WEEKDAYS[sdate.weekday()]]) if d in WEEKDAYS]
        if WEEKDAYS[today.weekday()] not in days or (delta_days // 7) % interval != 0:
            return None
        if count is not None:
            # count occurrences from series start through today (inclusive)
            occ = 0
            d = sdate
            while d <= today:
                if WEEKDAYS[d.weekday()] in days and \
                        ((d - sdate).days // 7) % interval == 0:
                    occ += 1
                    if d == today:
                        break
                d += timedelta(days=1)
            if occ > count:
                return None
        return todays_start

    return None  # MONTHLY/YEARLY etc. not supported — better to miss than phantom


def today_events_all(urls: list[str]):
    """Today's meetings across MULTIPLE calendars, merged and deduplicated
    (same title + start time counts as one event)."""
    out, seen = [], set()
    for u in urls:
        try:
            evs = today_events(u)
        except Exception:
            continue  # one broken calendar must not kill the others
        for e in evs:
            key = (e.get("title"), e.get("start_hm"))
            if key not in seen:
                seen.add(key)
                out.append(e)
    out.sort(key=lambda e: e["start_epoch"])
    return out


def today_events(url: str):
    """Today's meetings for one calendar, sorted by start, cached 5 min."""
    now = datetime.now().astimezone()
    c = _caches.get(url)
    if c and (now - c["at"]).total_seconds() < CACHE_TTL:
        return c["events"]
    events = parse_events(_fetch(url))
    today = now.date()
    out, seen = [], set()
    for ev in events:
        st = _occurs_today(ev, today)
        if not st:
            continue
        dur_s = int((ev["end"] - ev["start"]).total_seconds()) \
            if ev.get("end") and not ev["all_day"] else 3600
        key = (ev.get("title"), st.strftime("%H%M"))
        if key in seen:  # recurring master + expanded instance duplicate
            continue
        seen.add(key)
        out.append({"uid": ev.get("uid", ""), "title": ev.get("title", "Meeting"),
                    "start": st.astimezone().isoformat(),
                    "start_hm": st.astimezone().strftime("%H:%M"),
                    "end_hm": (st + timedelta(seconds=dur_s)).astimezone().strftime("%H:%M"),
                    "attendees": ev["attendees"][:12],
                    "start_epoch": int(st.timestamp())})
    out.sort(key=lambda e: e["start_epoch"])
    _caches[url] = {"at": now, "events": out}
    return out
