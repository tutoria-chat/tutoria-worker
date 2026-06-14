"""
iCalendar (.ics) feed importer — fetch a calendar's secret iCal URL (Google,
Outlook, Apple, any RFC-5545 feed) and turn its VEVENTs into the same candidate
event shape the calendar review screen uses: {title, eventType, date, time, description}.

Dependency-free: a small line-unfolding VEVENT parser, no external iCal library.
Times are normalized to America/São_Paulo wall-clock to match the review table
(which treats date/time as Brasília local before converting to UTC on confirm).
"""
import logging
import re
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Brasília is UTC-3 (no DST since 2019). UTC ("...Z") timestamps are shifted by this.
_SP_OFFSET = timedelta(hours=-3)

EVENT_TYPES = {"test", "assignment", "holiday", "field_event", "other"}

# Keyword → CourseEvent type (pt/en/es); summaries are messy, the professor edits anyway.
_TYPE_HINTS = [
    ("test", ["prova", "exam", "test", "avalia", "simulado", "examen"]),
    ("assignment", ["entrega", "trabalho", "assignment", "due", "homework", "tarea", "tcc", "projeto"]),
    ("holiday", ["feriado", "recesso", "holiday", "férias", "ferias", "break", "feriado"]),
    ("field_event", ["visita", "excursão", "excursao", "palestra", "field", "trip", "evento", "seminário", "seminario"]),
]


def _guess_type(summary: str) -> str:
    low = summary.lower()
    for etype, words in _TYPE_HINTS:
        if any(w in low for w in words):
            return etype
    return "other"


def _unfold(text: str) -> List[str]:
    """RFC-5545 line unfolding: a line starting with space/tab continues the previous."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", " ").replace("\\N", " ")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
        .strip()
    )


def _parse_dtstart(raw_value: str, params: str) -> Dict[str, str]:
    """
    Return {"date": "YYYY-MM-DD", "time": "HH:MM"} in São Paulo wall-clock.
    Handles VALUE=DATE (all-day), UTC ("...Z"), and local/floating datetimes.
    """
    value = raw_value.strip()
    is_date_only = "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value) is not None

    if is_date_only:
        m = re.match(r"(\d{4})(\d{2})(\d{2})", value)
        if not m:
            return {}
        return {"date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "time": ""}

    m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z)?", value)
    if not m:
        return {}
    y, mo, d, hh, mm, _ss, zulu = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(hh), int(mm))
    if zulu == "Z":  # UTC → shift to São Paulo
        dt = dt + _SP_OFFSET
    return {"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M")}


def parse_ics(text: str) -> List[Dict[str, Any]]:
    """Parse an .ics document into candidate calendar events (skips items with no date)."""
    events: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    in_event = False

    for line in _unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            in_event, cur = True, {}
            continue
        if upper.startswith("END:VEVENT"):
            in_event = False
            title = _unescape(cur.get("summary", ""))
            dt = cur.get("dt", {})
            if title and dt.get("date"):
                events.append({
                    "title": title[:255],
                    "eventType": _guess_type(title),
                    "date": dt["date"],
                    "time": dt.get("time", ""),
                    "description": _unescape(cur.get("description", ""))[:2000],
                })
            cur = {}
            continue
        if not in_event or ":" not in line:
            continue

        name_part, _, value = line.partition(":")
        name, _, params = name_part.partition(";")
        name = name.upper()
        if name == "SUMMARY":
            cur["summary"] = value
        elif name == "DESCRIPTION":
            cur["description"] = value
        elif name == "DTSTART":
            cur["dt"] = _parse_dtstart(value, params)

    return events


def fetch_ics(url: str, timeout: int = 20) -> str:
    """Fetch an .ics feed over HTTPS. Returns the raw text."""
    if not url.lower().startswith("https://"):
        # Google/Outlook/Apple all serve secret iCal feeds over https; require it.
        # (webcal:// is just https under the hood — normalize it.)
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        else:
            raise ValueError("Calendar URL must be an https:// (or webcal://) link")

    req = urllib.request.Request(url, headers={"User-Agent": "TutorIA-Calendar-Import/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (staff-supplied feed URL)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")
