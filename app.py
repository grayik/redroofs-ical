# app.py — Bookster → iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------------- Configuration ----------------
# Use app.booksterhq.com because api.booksterhq.com returned 403 in your tests.
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Optional: show debug section on index.html if set to "1"
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# Timezone & fixed check-in/out times
TZ = ZoneInfo("Europe/London")
CHECKIN_T = time(15, 0)   # 15:00
CHECKOUT_T = time(10, 0)  # 10:00


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
    """
    Fetch bookings for one property (entry).
    Auth: HTTP Basic with username 'x', password = API key (per Bookster docs).
    We ask for up to 200 items and filter client-side to confirmed.
    """
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    # Server-side filter by entry (ei). Some accounts ignore extra params, so we fall back to client-side filtering.
    params_primary = {"ei": str(property_id), "pp": 200}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params_primary, auth=("x", BOOKSTER_API_KEY))
        # Follow your earlier observation: app.booksterhq.com works; prevent redirects to login etc.
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"Unexpected redirect {r.status_code} from {url}")
        r.raise_for_status()
        payload = r.json()

    # Normalise list
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        items = payload["results"]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    # Client-side filter: this ensures we only emit confirmed bookings and the right property
    pid = str(property_id)
    filtered = []
    for b in items:
        if not isinstance(b, dict):
            continue
        if str(b.get("entry_id")) != pid:
            continue
        state = (b.get("state") or "").lower()
        if state == "confirmed":
            filtered.append(b)

    return filtered


# ---------------- Mapping ----------------
def _extract_mobile(b: dict) -> t.Optional[str]:
    # Try several possible fields; prefer explicit mobile if available
    for key in (
        "customer_tel_mobile",
        "customer_mobile",
        "customer_tel_day",
        "customer_tel_evening",
        "customer_phone",
    ):
        val = b.get(key)
        if val:
            return str(val)
    return None


def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    # Dates
    arrival_d = _to_date(b.get("start_inclusive"))
    # Bookster's end_exclusive is the actual CHECK-OUT date (non-inclusive).
    # We'll use it directly and attach a 10:00 time.
    departure_d = _to_date(b.get("end_exclusive"))

    if not arrival_d:
        return None

    # If departure is missing or invalid, assume at least one night stay.
    if not departure_d or departure_d <= arrival_d:
        departure_d = arrival_d + timedelta(days=1)

    # Guest name
    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (f"{first} {last}".strip()) or "Guest"

    # Party size (as int if possible)
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else 1
    except Exception:
        party_total = 1

    # Email & mobile
    email = b.get("customer_email") or None
    mobile = _extract_mobile(b)

    # Money
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
        # Amount paid = value - balance (guard against negatives)
        paid = max(0.0, value - balance)

    # Extras from lines[] where type == "extra"
    extras_list: t.List[str] = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(f"{name} x{qty}" if qty else name)

    # Build timed datetimes in Europe/London
    dtstart = datetime.combine(arrival_d, CHECKIN_T, tzinfo=TZ)
    dtend = datetime.combine(departure_d, CHECKOUT_T, tzinfo=TZ)

    return {
        "dtstart": dtstart,
        "dtend": dtend,
        "guest_name": display_name,
        "party_total": party_total,
        "email": email,
        "mobile": mobile,
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
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for raw in bookings:
        mapped = map_booking_to_event_data(raw)
        if not mapped:
            continue

        # Title: "Guest Name xN" (include x1)
        title = f"{mapped['guest_name']} x{mapped['party_total']}"
        ev = Event()
        ev.add("summary", title)
        ev.add("dtstart", mapped["dtstart"])
        ev.add("dtend", mapped["dtend"])

        # UID
        uid = f"redroofs-{mapped.get('reference') or mapped['guest_name']}-{mapped['dtstart'].date().isoformat()}"
        ev.add("uid", uid)

        # Description block
        lines: t.List[str] = []
        if mapped.get("email"):
            lines.append(f"Email: {mapped['email']}")
        if mapped.get("mobile"):
            lines.append(f"Mobile: {mapped['mobile']}")
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

        # Clickable booking link
        if mapped.get("reference"):
            lines.append(f"Booking: https://app.booksterhq.com/bookings/{mapped['reference']}/view")

        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)

    return cal.to_ical()


# ---------------- GitHub Action entry ----------------
async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    Generate one .ics per property and a simple index.html.
    On error, write placeholder calendars and an error message on index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    try:
        all_debug: t.List[str] = []
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)

            # Try to infer a calendar name from first booking
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
                all_debug.append(f"PID {pid}: count={len(bookings)}")

        # Build index.html
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and all_debug:
            html_lines.append("<hr><pre>Debug\n" + "\n".join(all_debug) + "</pre>")

        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        err_text = "Error generating feeds: " + str(e)
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
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + err_text + "</pre>")
        return written
