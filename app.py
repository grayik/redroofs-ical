# Minimal Bookster -> iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------------- Configuration ----------------
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# ---------------- Helpers ----------------

def _to_date(value: t.Union[str, int, float, date, datetime, None]) -> t.Optional[date]:
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

# ---------------- Bookster access ----------------

async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> t.List[dict]:
    """Fetch confirmed bookings for one property (entry).

    Uses Basic auth with username "x" and password = API key.
    Filters with ei (entry_id), requests up to 200 results.
    """
    url = "%s/%s" % (BOOKSTER_API_BASE.rstrip("/"), BOOKSTER_BOOKINGS_PATH.lstrip("/"))
    params = {"ei": property_id, "pp": 200, "st": "confirmed"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError("Unexpected redirect %s from %s" % (r.status_code, url))
        r.raise_for_status()
        payload = r.json()

    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        items = payload["results"]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return items

# ---------------- Mapping ----------------

def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative"):
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (first + " " + last).strip() or "Guest"

    email = b.get("customer_email") or None
    mobile = b.get("customer_mobile") or b.get("customer_phone") or None

    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else None
    except Exception:
        party_total = None

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

    extras_list = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(("%s x%s" % (name, qty)) if qty else name)

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

# ---------------- iCal rendering ----------------

def render_calendar(bookings: t.List[dict], property_name: t.Optional[str] = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", "%s - Guests" % property_name)

    for raw in bookings:
        mapped = map_booking_to_event_data(raw)
        if not mapped:
            continue
        ev = Event()
        ev.add("summary", mapped["guest_name"])  # title
        ev.add("dtstart", mapped["arrival"])     # all-day
        ev.add("dtend", mapped["departure"])     # checkout
        uid = "redroofs-%s-%s" % (
            (mapped.get("reference") or mapped["guest_name"]),
            mapped["arrival"].isoformat(),
        )
        ev.add("uid", uid)
        lines = []
        if mapped.get("email"):
            lines.append("Email: %s" % mapped["email"])
        if mapped.get("mobile"):
            lines.append("Mobile: %s" % mapped["mobile"])
        if mapped.get("party_total"):
            lines.append("Guests in party: %s" % mapped["party_total"])
        if mapped.get("extras"):
            lines.append("Extras: " + ", ".join(mapped["extras"]))
        if mapped.get("property_name"):
            lines.append("Property: %s" % mapped["property_name"])
        if mapped.get("channel"):
            lines.append("Channel: %s" % mapped["channel"])
        if mapped.get("paid") is not None:
            amt = "%.2f" % mapped["paid"]
            if mapped.get("currency"):
                amt = "%s %s" % (mapped["currency"], amt)
            lines.append("Amount paid to us: %s" % amt)
        ev.add("description", "
".join(lines) if lines else "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()

# ---------------- GitHub Action entry ----------------

async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """Generate .ics files and write index.html.
    On error, write placeholder feeds and show the error on index.html.
    """
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    try:
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)
            prop_name = None
            for b in bookings:
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break
            ics_bytes = render_calendar(bookings, prop_name)
            path = os.path.join(outdir, "%s.ics" % pid)
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds below are regenerated hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append("<p><a href='%s.ics'>%s.ics</a></p>" % (pid, pid))
        if DEBUG_DUMP:
            html_lines.append("<hr><pre>DEBUG_DUMP is enabled.</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("
".join(html_lines))
        return written

    except Exception as e:
        err_text = "Error generating feeds: %s" % str(e)
        placeholder = "
".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Redroofs//EN",
            "END:VCALENDAR",
            "",
        ])
        for pid in property_ids:
            with open(os.path.join(outdir, "%s.ics" % pid), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>
<pre>%s</pre>" % err_text)
        return written
