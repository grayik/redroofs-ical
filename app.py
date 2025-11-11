# app.py — Bookster → iCal generator for GitHub Pages builds (no web server)
# The GitHub Action runs:
#   from app import generate_and_write
#
# What this produces:
# - One .ics per property ID (entry_id)
# - All-day events split by day:
#     IN: <Name> xN (<CODE>)    on arrival day
#     <Name> (<CODE>)           on middle days (if any)
#     OUT: <Name> (<CODE>)      on checkout day
# - Description includes email, mobile, party size, extras, channel, property,
#   amount paid to us (value - balance), and a booking link:
#     https://app.booksterhq.com/bookings/{id}/view

import os
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Any

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ================= Configuration =================

# Use the APP domain (works with your account and returns data)
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
)
BOOKSTER_BOOKINGS_PATH = os.getenv(
    "BOOKSTER_BOOKINGS_PATH",
    "booking/bookings.json",
)
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Turn on to print debug info into index.html (1 = on, else off)
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# Property code suffixes for titles
PROPERTY_CODES = {
    "Barn Owl Cabin": "BO",
    "Bumblebee Cabin": "BB",
    "Redroofs by the Woods": "RR",
}

# ================= Helpers =================


def _to_date(value: Any) -> Optional[date]:
    """Parse value into a date (UTC). Supports epoch seconds or strings."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(int(value)).date()
        except Exception:
            return None
    try:
        return dtparse.parse(str(value)).date()
    except Exception:
        return None


def _fmt_paid(value: Optional[float], balance: Optional[float]) -> Optional[str]:
    """Compute amount paid to us = value - balance. Clamp at 0 and format to 2 dp."""
    if value is None or balance is None:
        return None
    paid = round(max(0.0, float(value) - float(balance)), 2)
    return f"{paid:.2f}"


def _party_size(b: Dict[str, Any]) -> Optional[int]:
    p = b.get("party_size")
    try:
        if p is None or str(p).strip() == "":
            return None
        return int(p)
    except Exception:
        return None


def _mobile_from_booking(b: Dict[str, Any]) -> Optional[str]:
    # In your sample JSON this is "customer_tel_mobile"
    return (
        b.get("customer_tel_mobile")
        or b.get("customer_mobile")
        or b.get("customer_tel_day")
        or b.get("customer_tel_evening")
        or None
    )


def _extras_from_booking(b: Dict[str, Any]) -> List[str]:
    """Extract extras from lines[] with type 'extra'."""
    out: List[str] = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                out.append(f"{name} x{qty}" if qty else name)
    return out


def _property_code(entry_name: Optional[str], entry_id: Optional[str]) -> str:
    if entry_name:
        for key, code in PROPERTY_CODES.items():
            if entry_name.startswith(key):
                return code
    # Fallback by entry_id
    if str(entry_id) == "158595":
        return "BO"
    if str(entry_id) == "158497":
        return "BB"
    if str(entry_id) == "158596":
        return "RR"
    return "RR"  # safest default


# ================= Bookster access =================


async def _call_bookings(params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        # HTTP Basic auth: username 'x', password = API key (per Bookster docs)
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"Unexpected redirect {r.status_code} from {url}. Check base/path/credentials."
            )
        r.raise_for_status()
        return r.json()


async def fetch_bookings_for_property(property_id: str) -> List[Dict[str, Any]]:
    """Fetch bookings for one property (entry_id). Tries with/without st=confirmed."""
    # Attempt 1: server-side filter by entry + confirmed
    params1 = {"ei": property_id, "pp": 200, "st": "confirmed"}
    payload = await _call_bookings(params1)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        return data

    # Attempt 2: entry only, then client-side filter by state
    params2 = {"ei": property_id, "pp": 200}
    payload = await _call_bookings(params2)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [b for b in data if (b.get("state") or "").lower() == "confirmed"]

    # Fallback shapes, just in case
    if isinstance(payload, list):
        return [b for b in payload if (b.get("state") or "").lower() == "confirmed"]
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list):
                return [b for b in v if (b.get("state") or "").lower() == "confirmed"]

    return []


# ================= iCal rendering =================


def _title_for_day(name: str, code: str, day_kind: str, party: Optional[int]) -> str:
    """Build the summary/title for a given day."""
    # day_kind: "IN" | "MID" | "OUT"
    if day_kind == "IN":
        # Always include xN, even if N==1
        n = party if party is not None else 1
        return f"IN: {name} x{n} ({code})"
    if day_kind == "OUT":
        return f"OUT: {name} ({code})"
    # MID
    return f"{name} ({code})"


def _add_event(cal: Calendar, when: date, title: str, desc_lines: List[str], uid_key: str):
    ev = Event()
    ev.add("summary", title)
    # All-day single-day event on 'when'
    ev.add("dtstart", when)
    ev.add("dtend", when + timedelta(days=1))  # non-inclusive end is next day
    ev.add("uid", f"redroofs-{uid_key}-{when.isoformat()}")
    ev.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
    cal.add_component(ev)


def render_calendar(bookings: List[Dict[str, Any]], property_name: Optional[str] = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for b in bookings:
        if (b.get("state") or "").lower() != "confirmed":
            continue

        arrival = _to_date(b.get("start_inclusive"))
        checkout = _to_date(b.get("end_exclusive"))  # this *is* the checkout date per Bookster
        if not arrival or not checkout:
            continue

        first = (b.get("customer_forename") or "").strip()
        last = (b.get("customer_surname") or "").strip()
        display_name = (f"{first} {last}".strip() or "Guest")

        email = b.get("customer_email") or None
        mobile = _mobile_from_booking(b)
        party = _party_size(b)

        currency = (b.get("currency") or "").upper() or "GBP"
        paid_str = _fmt_paid(b.get("value"), b.get("balance"))
        extras = _extras_from_booking(b)

        entry_name = b.get("entry_name")
        entry_id = b.get("entry_id")
        code = _property_code(entry_name, entry_id)
        booking_id = b.get("id")
        booking_link = f"https://app.booksterhq.com/bookings/{booking_id}/view" if booking_id else None

        # Shared description
        desc: List[str] = []
        if email:
            desc.append(f"Email: {email}")
        if mobile:
            desc.append(f"Mobile: {mobile}")
        if party is not None:
            desc.append(f"Guests in party: {party}")
        if extras:
            desc.append("Extras: " + ", ".join(extras))
        if entry_name:
            desc.append(f"Property: {entry_name}")
        channel = b.get("syndicate_name")
        if channel:
            desc.append(f"Channel: {channel}")
        if paid_str is not None:
            desc.append(f"Amount paid to us: {currency} {paid_str}")
        if booking_link:
            desc.append(f"Booking: {booking_link}")

        # Build daily all-day events:
        # IN on arrival day
        _add_event(
            cal,
            arrival,
            _title_for_day(display_name, code, "IN", party),
            desc,
            uid_key=str(booking_id or display_name),
        )

        # MID days (arrival+1 ... checkout-1)
        mid_start = arrival + timedelta(days=1)
        mid_end_exclusive = checkout  # not included
        cur = mid_start
        while cur < mid_end_exclusive:
            _add_event(
                cal,
                cur,
                _title_for_day(display_name, code, "MID", party),
                desc,
                uid_key=str(booking_id or display_name),
            )
            cur += timedelta(days=1)

        # OUT on checkout day
        _add_event(
            cal,
            checkout,
            _title_for_day(display_name, code, "OUT", party),
            desc,
            uid_key=str(booking_id or display_name),
        )

    return cal.to_ical()


# ================= GitHub Action entry =================


async def generate_and_write(property_ids: List[str], outdir: str = "public") -> List[str]:
    """
    Generate .ics files and write index.html.
    On error, write placeholder feeds and show the error on index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: List[str] = []
    debug_lines: List[str] = []

    try:
        for pid in property_ids:
            # Fetch
            bookings = await fetch_bookings_for_property(pid)
            debug_lines.append(f"PID {pid}: fetched {len(bookings)} confirmed bookings")

            # Try to infer a nice calendar name from first result
            prop_name = None
            for b in bookings:
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break

            # Render and write
            ics_bytes = render_calendar(bookings, prop_name)
            path = os.path.join(outdir, f"{pid}.ics")
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

        # Index page
        lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and debug_lines:
            lines.append("<hr><pre>" + "\n".join(debug_lines) + "</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return written

    except Exception as e:
        # Minimal placeholder files so Pages still serves .ics
        placeholder = "\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Redroofs Bookster iCal//EN",
                "END:VCALENDAR",
                "",
            ]
        )
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(
                "<h1>Redroofs iCal Feeds</h1>\n"
                "<pre>Error generating feeds: " + str(e) + "</pre>"
            )
        return written
