# app.py - Bookster -> iCal (static generation for GitHub Pages)

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ----- Configuration -----
# Use the working host you verified:
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKINGS_LIST_PATH = os.getenv("BOOKSTER_BOOKINGS_LIST_PATH", "booking/bookings.json")
BOOKING_DETAIL_PATH_TMPL = os.getenv("BOOKSTER_BOOKING_DETAIL_PATH_TMPL", "booking/bookings/{id}.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# ----- Helpers -----
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

# ----- API calls -----
async def _request_json(path: str, params: dict | None = None) -> t.Any:
    """GET JSON with Basic auth (username 'x', password = API key)."""
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params or {}, auth=("x", BOOKSTER_API_KEY))
        # Treat redirects as errors (usually auth/host mismatch)
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"Unexpected redirect {r.status_code} from {url}")
        r.raise_for_status()
        return r.json()

async def list_bookings_for_entry(entry_id: str) -> list[dict]:
    """List bookings for a property (entry). Do not pass st=confirmed (server gives 0)."""
    # Attempt 1: server filter by entry id only
    payload = await _request_json(BOOKINGS_LIST_PATH, {"ei": entry_id, "pp": 200})
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        data = payload if isinstance(payload, list) else []
    # Client-side filter: keep only confirmed
    rows = [b for b in data if isinstance(b, dict) and (b.get("state") or "").lower() == "confirmed"]
    return rows

async def get_booking_detail(booking_id: str | int) -> dict:
    """Fetch full booking details (for mobile and extras)."""
    path = BOOKING_DETAIL_PATH_TMPL.replace("{id}", str(booking_id))
    payload = await _request_json(path)
    if isinstance(payload, dict):
        return payload
    return {}

# ----- Mapping -----
def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state != "confirmed":
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display = (first + " " + last).strip() or "Guest"

    email = b.get("customer_email") or None

    # Party size may be string or int
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val not in (None, "") else None
    except Exception:
        party_total = None

    # Money
    def _f(x):
        try:
            return float(x)
        except Exception:
            return None

    value = _f(b.get("value"))
    balance = _f(b.get("balance"))
    currency = (b.get("currency") or "").upper() or None
    paid = None
    if value is not None and balance is not None:
        paid = max(0.0, value - balance)

    return {
        "arrival": arrival,
        "departure": departure,
        "guest_name": display,
        "email": email,
        "party_total": party_total,
        "reference": b.get("id") or b.get("reference"),
        "property_name": b.get("entry_name"),
        "property_id": b.get("entry_id"),
        "channel": b.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
    }

def enrich_with_detail(mapped: dict, detail: dict) -> dict:
    """Add mobile and extras from detail payload."""
    # Mobile fallbacks from docs
    mobile = (
        detail.get("customer_tel_mobile")
        or detail.get("customer_tel_day")
        or detail.get("customer_tel_evening")
        or None
    )
    if mobile:
        mapped["mobile"] = mobile

    # Extras from lines where type == "extra"
    extras: list[str] = []
    lines = detail.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras.append(f"{name} x{qty}" if qty else name)
    if extras:
        mapped["extras"] = extras
    return mapped

# ----- iCal generation -----
def render_calendar(bookings: list[dict], property_name: str | None = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} - Guests")

    for m in bookings:
        ev = Event()
        ev.add("summary", m["guest_name"])
        ev.add("dtstart", m["arrival"])
        ev.add("dtend", m["departure"])
        uid_src = m.get("reference") or m["guest_name"]
        ev.add("uid", f"redroofs-{uid_src}-{m['arrival'].isoformat()}")
        lines: list[str] = []
        if m.get("email"):
            lines.append(f"Email: {m['email']}")
        if m.get("mobile"):
            lines.append(f"Mobile: {m['mobile']}")
        if m.get("party_total"):
            lines.append(f"Guests in party: {m['party_total']}")
        if m.get("extras"):
            lines.append("Extras: " + ", ".join(m["extras"]))
        if m.get("property_name"):
            lines.append(f"Property: {m['property_name']}")
        if m.get("channel"):
            lines.append(f"Channel: {m['channel']}")
        if m.get("paid") is not None:
            amt = f"{m['paid']:.2f}"
            if m.get("currency"):
                amt = f"{m['currency']} {amt}"
            lines.append(f"Amount paid to us: {amt}")
        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()

# ----- GitHub Action entry -----
async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """
    1) List bookings for each entry (without st filter)
    2) Client-side filter confirmed
    3) Enrich each with booking detail (mobile, extras)
    4) Write one .ics per property and a simple index.html
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    debug_lines: list[str] = []

    try:
        for pid in property_ids:
            # Step 1: list + client filter
            base_list = await list_bookings_for_entry(pid)
            debug_lines.append(f"PID {pid}: list_count={len(base_list)}")

            # Step 2: map and enrich
            mapped: list[dict] = []
            for row in base_list:
                m = map_booking_to_event_data(row)
                if not m:
                    continue
                detail = await get_booking_detail(m.get("reference"))
                m = enrich_with_detail(m, detail)
                mapped.append(m)
            debug_lines.append(f"PID {pid}: enriched={len(mapped)}")

            # Grab a property name if we have one
            prop_name = None
            for m in mapped:
                if m.get("property_name"):
                    prop_name = m["property_name"]
                    break

            # Step 3: write .ics
            ics_bytes = render_calendar(mapped, prop_name)
            path = os.path.join(outdir, f"{pid}.ics")
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

        # Step 4: index.html
        lines = ["<h1>Redroofs iCal Feeds</h1>", "<p>Feeds regenerate hourly.</p>"]
        for pid in property_ids:
            lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP:
            lines.append("<hr><pre>Debug\n\n" + "\n".join(debug_lines) + "</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return written

    except Exception as e:
        # Fallback: write placeholder VCALENDAR and error to index
        placeholder = "\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Redroofs Bookster iCal//EN",
            "END:VCALENDAR",
            ""
        ])
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + str(e) + "</pre>")
        return written
