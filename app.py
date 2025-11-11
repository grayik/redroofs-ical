# Minimal Bookster -> iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime, timedelta

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------------- Configuration ----------------
# Use the 'app' host (your tenant returns data here) per your testing
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
).rstrip("/")
BOOKSTER_BOOKINGS_PATH = os.getenv(
    "BOOKSTER_BOOKINGS_PATH",
    "booking/bookings.json",
).lstrip("/")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# Property code suffixes for titles
PROPERTY_CODES = {
    "158596": "RR",  # Redroofs by the Woods
    "158595": "BO",  # Barn Owl Cabin
    "158497": "BB",  # Bumblebee Cabin
}

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

def _safe_float(x) -> t.Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _amount_paid(value, balance) -> t.Optional[float]:
    v = _safe_float(value)
    b = _safe_float(balance)
    if v is None or b is None:
        return None
    return round(max(0.0, v - b), 2)

def _property_code(entry_id: t.Union[str, int]) -> str:
    key = str(entry_id) if entry_id is not None else ""
    return PROPERTY_CODES.get(key, "").strip() or "RR"  # default to RR if unknown

# ---------------- Bookster access ----------------

async def _get_json(url: str, params: dict) -> dict:
    """
    Make a GET with Basic auth (username='x', password=API key) and return JSON.
    Raises for HTTP errors or unexpected redirects.
    """
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        # Some tenants redirect if auth/base is wrong
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"Unexpected redirect {r.status_code} from {url}. "
                "Check BOOKSTER_API_BASE/BOOKSTER_BOOKINGS_PATH and credentials."
            )
        r.raise_for_status()
        return r.json()

async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> t.List[dict]:
    """
    Fetch bookings for one property (entry). We:
      1) Try server filter with ei=<property_id> (no state filter).
      2) Filter to 'confirmed' client-side.
    """
    url = f"{BOOKSTER_API_BASE}/{BOOKSTER_BOOKINGS_PATH}"

    # Attempt: server-side by entry_id only
    params = {"ei": str(property_id), "pp": 200}
    payload = await _get_json(url, params)

    # Normalise array
    if isinstance(payload, dict):
        items = payload.get("data") if isinstance(payload.get("data"), list) else (
            payload.get("results") if isinstance(payload.get("results"), list) else []
        )
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    # Client-side keep only confirmed
    confirmed = [b for b in items if (str(b.get("state") or "").lower() == "confirmed")]
    return confirmed

# ---------------- Mapping ----------------

def map_booking_to_base_fields(b: dict) -> t.Optional[dict]:
    """
    Extracts/normalises the fields we need from a single booking.
    Returns None for bookings that can't be represented (e.g., missing dates).
    """
    arrival = _to_date(b.get("start_inclusive"))
    departure_exclusive = _to_date(b.get("end_exclusive"))
    if not arrival or not departure_exclusive:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (f"{first} {last}".strip() or "Guest").strip()

    email = (b.get("customer_email") or "").strip() or None
    mobile = (
        (b.get("customer_tel_mobile") or b.get("customer_mobile") or b.get("customer_phone") or "").strip()
        or None
    )

    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else None
    except Exception:
        party_total = None

    value = b.get("value")
    balance = b.get("balance")
    paid = _amount_paid(value, balance)
    currency = (b.get("currency") or "").upper() or None

    # Extras via lines[] of type "extra"
    extras_list: t.List[str] = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = (ln.get("name") or ln.get("title") or "Extra").strip()
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(f"{name} x{qty}" if qty else name)

    entry_id = b.get("entry_id")
    prop_code = _property_code(entry_id)

    return {
        "arrival": arrival,
        "departure_exclusive": departure_exclusive,  # Bookster's "end_exclusive" (checkout day)
        "guest_name": display_name,
        "email": email,
        "mobile": mobile,
        "party_total": party_total,
        "extras": extras_list,
        "reference": b.get("id") or b.get("reference"),
        "property_name": b.get("entry_name"),
        "property_id": entry_id,
        "channel": b.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
        "prop_code": prop_code,
    }

# ---------------- iCal rendering ----------------

def _add_single_day_event(cal: Calendar, the_date: date, summary: str, desc_lines: t.List[str], ref: str):
    ev = Event()
    # All-day single-day: DTSTART = day, DTEND = day+1
    ev.add("dtstart", the_date)
    ev.add("dtend", the_date + timedelta(days=1))
    ev.add("summary", summary)
    uid = f"redroofs-{ref}-{the_date.isoformat()}"
    ev.add("uid", uid)
    ev.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
    cal.add_component(ev)

def render_calendar(bookings: t.List[dict], property_name: t.Optional[str] = None) -> bytes:
    """
    For each booking we create multiple all-day events:
      - IN day (arrival date):  "IN: Name xN (CODE)"
      - Middle days:            "Name (CODE)"
      - OUT day (checkout day): "OUT: Name (CODE)"
    """
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for b in bookings:
        m = map_booking_to_base_fields(b)
        if not m:
            continue

        arrival = m["arrival"]
        checkout = m["departure_exclusive"]  # Bookster's end_exclusive is the checkout day
        name = m["guest_name"]
        code = m["prop_code"]
        ref = str(m.get("reference") or f"{name}-{arrival.isoformat()}")

        # Description used across all split events
        desc_lines: t.List[str] = []
        if m.get("email"):
            desc_lines.append(f"Email: {m['email']}")
        if m.get("mobile"):
            desc_lines.append(f"Mobile: {m['mobile']}")
        if m.get("party_total") is not None:
            desc_lines.append(f"Guests in party: {m['party_total']}")
        if m.get("extras"):
            desc_lines.append("Extras: " + ", ".join(m["extras"]))
        if m.get("property_name"):
            desc_lines.append(f"Property: {m['property_name']}")
        if m.get("channel"):
            desc_lines.append(f"Channel: {m['channel']}")
        if m.get("paid") is not None:
            amt = f"{m['paid']:.2f}"
            if m.get("currency"):
                amt = f"{m['currency']} {amt}"
            desc_lines.append(f"Amount paid to us: {amt}")
        if m.get("reference"):
            # Link to booking in console
            desc_lines.append(f"Booking: https://app.booksterhq.com/bookings/{m['reference']}/view")

        # IN day: arrival
        party = m["party_total"] if m["party_total"] is not None else 1
        in_title = f"IN: {name} x{party} ({code})"
        _add_single_day_event(cal, arrival, in_title, desc_lines, f"{ref}-IN")

        # Middle days (if any): the days fully between arrival and checkout
        d = arrival + timedelta(days=1)
        while d < checkout:
            mid_title = f"{name} ({code})"
            _add_single_day_event(cal, d, mid_title, desc_lines, f"{ref}-MID-{d.isoformat()}")
            d += timedelta(days=1)

        # OUT day: checkout day itself
        out_title = f"OUT: {name} ({code})"
        _add_single_day_event(cal, checkout, out_title, desc_lines, f"{ref}-OUT")

    return cal.to_ical()

# ---------------- GitHub Action entry ----------------

async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    Generate .ics files and write index.html.
    On error, write placeholder feeds and show the error on index.html.
    """
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    debug_lines: t.List[str] = []

    try:
        for pid in property_ids:
            # Fetch and render for each property
            bookings = await fetch_bookings_for_property(pid)
            # Derive a name (if any booking has entry_name)
            prop_name = None
            for b in bookings:
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break

            ics_bytes = render_calendar(bookings, prop_name)
            path = os.path.join(outdir, f"{pid}.ics")
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

            if DEBUG_DUMP:
                debug_lines.append(f"PID {pid}: rendered_events_from={len(bookings)}")

        # Build index.html
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and debug_lines:
            html_lines.append("<hr><pre>Debug\n" + "\n".join(debug_lines) + "</pre>")

        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))

        return written

    except Exception as e:
        err_text = "Error generating feeds: " + str(e)
        placeholder = "\n".join(
            ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Redroofs Bookster iCal//EN", "END:VCALENDAR", ""]
        )
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + err_text + "</pre>")
        return written
