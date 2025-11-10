"""
Minimal Bookster → iCal generator for GitHub Pages builds
---------------------------------------------------------
This file contains only the code the GitHub Action needs (no web server).
The workflow calls `from app import generate_and_write`.
"""
from __future__ import annotations

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# ----------------- helpers -----------------

def _auth_headers() -> dict:
    # Not used for Basic auth; kept for future header-based auth if needed.
    return {}


def _to_date(value: t.Union[str, int, float, date, datetime, None]) -> date | None:
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

# ------------- Bookster access ------------

async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> list[dict]:
    """Fetch bookings for a given property.
    Expects a payload like: {"meta": {...}, "data": [ ... ]}
    Filters by entry_id client-side if needed.
    """
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    params = {"property_id": property_id}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        # HTTP Basic with username 'x' and password = API key (per Bookster docs)
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"Auth/URL redirect from Bookster ({r.status_code}). Check base/path and credentials.")
        r.raise_for_status()
        payload = r.json()

    if isinstance(payload, dict) and "data" in payload:
        items = payload.get("data", [])
    elif isinstance(payload, dict) and "results" in payload:
        items = payload.get("results", [])
    else:
        items = payload if isinstance(payload, list) else []

    if items and any("entry_id" in i for i in items):
        items = [i for i in items if str(i.get("entry_id")) == str(property_id) or not property_id]
    return items


def map_booking_to_event_data(b: dict) -> dict | None:
    state = (b.get("state") or "").lower()
    if state in {"cancelled", "canceled", "void", "rejected", "tentative"}:
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (f"{first} {last}" if (first or last) else "Guest").strip()

    email = b.get("customer_email") or None
    mobile = b.get("customer_mobile") or b.get("customer_phone") or None

    # party size may be string
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else None
    except Exception:
        party_total = None

    # money
    def _to_float(x):
        try:
            return float(x)
        except Exception:
            return None
    value = _to_float(b.get("value"))
    balance = _to_float(b.get("balance"))
    currency = (b.get("currency") or "").upper() or None
    paid = None
    if value is not None and balance is not None:
        paid = max(0.0, value - balance)

    extras_raw = b.get("extras") or b.get("add_ons") or []
    extras_list: list[str] = []
    if isinstance(extras_raw, list):
        for x in extras_raw:
            if isinstance(x, str):
                extras_list.append(x)
            elif isinstance(x, dict):
                name = x.get("name") or x.get("title") or x.get("code")
                qty = x.get("quantity") or x.get("qty")
                extras_list.append(f"{name} x{qty}" if (name and qty) else (name or "Extra"))

    return {
        "arrival": arrival,
        "departure": departure,
        "guest_name": display_name,
        "email": email,
        "mobile": mobile,
        "party_total": party_total,
        "extras": extras_list,
        "reference": b.get("id") or b.get("reference"),
        "property_name": b.get("entry_name"),
        "property_id": b.get("entry_id"),
        "channel": b.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
    }

# ------------- iCal rendering ------------

def render_calendar(bookings: list[dict], property_name: str | None = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for raw in bookings:
        mapped = map_booking_to_event_data(raw)
        if not mapped:
            continue
        ev = Event()
        ev.add("summary", mapped["guest_name"])  # title = guest name
        ev.add("dtstart", mapped["arrival"])     # all-day
        ev.add("dtend", mapped["departure"])     # checkout (non-inclusive)
        uid = f"redroofs-{mapped.get('reference') or mapped['guest_name']}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)
        lines = []
        if mapped.get("email"):
            lines.append(f"Email: {mapped['email']}")
        if mapped.get("mobile"):
            lines.append(f"Mobile: {mapped['mobile']}")
        if mapped.get("party_total"):
            lines.append(f"Guests in party: {mapped['party_total']}")
        if mapped.get("extras"):
            lines.append("Extras: " + ", ".join(mapped["extras"]))
        if mapped.get("property_name"):
            lines.append(f"Property: {mapped['property_name']}")
        if mapped.get("channel"):
            lines.append(f"Channel: {mapped['channel']}")
        if mapped.get("paid") is not None:
            amt = f"{mapped['paid']:.2f}"
            if mapped.get("currency"):
                amt = f"{mapped['currency']} {amt}"
            lines.append(f"Amount paid to us: {amt}")
        ev.add("description", "\n".join(lines) or "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()

# ------------- GitHub Action entry -------

async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """Generate .ics files. If an error occurs, write placeholder feeds and a clear status message."""
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    try:
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)
            prop_name = None
            for b in bookings:
                if b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break
            ics_bytes = render_calendar(bookings, prop_name)
            path = os.path.join(outdir, f"{pid}.ics")
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)
        # success index
        html_lines = ["<h1>Redroofs iCal Feeds</h1>"]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        (os.path.join(outdir, "index.html"))
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("".join(html_lines))
        return written

    except Exception as e:
        err = f"Error generating feeds: {e}

" + traceback.format_exc()
        # create placeholder feeds
        placeholder = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Redroofs//EN
END:VCALENDAR
"""
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(f"<h1>Redroofs iCal Feeds</h1><pre>{err}</pre>")
        return written
