# app.py — Minimal Bookster → iCal generator for GitHub Pages builds
#
# The GitHub Action imports:  from app import generate_and_write
# No web server here. It just fetches bookings and writes .ics files.

import os
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse


# ===================== Configuration =====================

# Per Bookster docs:
#   GET https://api.booksterhq.com/system/api/v1/booking/bookings.json
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# If set to "1", we’ll write extra info to index.html to help diagnose issues.
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"


# ===================== Helpers =====================

def _to_date(value):
    """Coerce epoch seconds / ISO string / date/datetime → date or None."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    # epoch seconds
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.utcfromtimestamp(int(value)).date()
        except Exception:
            return None
    # ISO-ish string
    try:
        return dtparse.parse(str(value)).date()
    except Exception:
        return None


# ===================== Bookster access =====================

async def fetch_bookings_for_property(property_id):
    """
    Fetch confirmed bookings for a single property (entry) using ei=entry_id.
    Auth per docs: Basic auth, username 'x', password = API key.
    Returns a list of booking dicts (may be empty).
    """
    url = BOOKSTER_API_BASE.rstrip("/") + "/" + BOOKSTER_BOOKINGS_PATH.lstrip("/")

    # Parameters per docs (ei = entry_id). We also request more per page.
    params = {
        "ei": str(property_id),   # entry_id
        "st": "confirmed",        # only confirmed bookings
        "pp": 200,                # results per page
    }

    # Some tenants may need "ci" (client id). If you know it, you can add it:
    # if os.getenv("BOOKSTER_CLIENT_ID"):
    #     params["ci"] = os.getenv("BOOKSTER_CLIENT_ID")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        # Basic auth: username 'x', password = API key
        r = await client.get(
            url,
            params=params,
            auth=("x", BOOKSTER_API_KEY),
            headers={"Accept": "application/json"}
        )
        r.raise_for_status()
        payload = r.json()

    # Normalise to a list
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        items = payload["results"]
    elif isinstance(payload, list):
        items = payload
    else:
        # Try any list value inside a dict (very defensive)
        items = []
        if isinstance(payload, dict):
            for v in payload.values():
                if isinstance(v, list):
                    items = v
                    break

    return items or []


def map_booking_to_event_data(b):
    """Map a single booking dict → fields we need for iCal. Return None to skip."""
    state = (b.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative", "quote", "paymentnak"):
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    guest_name = (first + " " + last).strip() or "Guest"

    email = b.get("customer_email") or None
    mobile = b.get("customer_mobile") or b.get("customer_phone") or None

    # party size
    party_total = None
    ps = b.get("party_size")
    try:
        if ps not in (None, ""):
            party_total = int(ps)
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

    # extras from lines[] of type "extra" if present
    extras = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras.append(f"{name} x{qty}" if qty else name)

    return {
        "arrival": arrival,
        "departure": departure,
        "guest_name": guest_name,
        "email": email,
        "mobile": mobile,
        "party_total": party_total,
        "extras": extras,
        "reference": b.get("id") or b.get("reference"),
        "property_name": b.get("entry_name"),
        "property_id": b.get("entry_id"),
        "channel": b.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
    }


# ===================== iCal rendering =====================

def render_calendar(bookings, property_name=None):
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
        ev.add("summary", mapped["guest_name"])   # event title
        ev.add("dtstart", mapped["arrival"])      # all-day start date
        ev.add("dtend", mapped["departure"])      # checkout (non-inclusive)
        uid = f"redroofs-{(mapped.get('reference') or mapped['guest_name'])}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)

        desc_lines = []
        if mapped.get("email"):
            desc_lines.append(f"Email: {mapped['email']}")
        if mapped.get("mobile"):
            desc_lines.append(f"Mobile: {mapped['mobile']}")
        if mapped.get("party_total"):
            desc_lines.append(f"Guests in party: {mapped['party_total']}")
        if mapped.get("extras"):
            desc_lines.append("Extras: " + ", ".join(mapped["extras"]))
        if mapped.get("property_name"):
            desc_lines.append(f"Property: {mapped['property_name']}")
        if mapped.get("channel"):
            desc_lines.append(f"Channel: {mapped['channel']}")
        if mapped.get("paid") is not None:
            amt = f"{mapped['paid']:.2f}"
            if mapped.get("currency"):
                amt = f"{mapped['currency']} {amt}"
            desc_lines.append(f"Amount paid to us: {amt}")

        ev.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
        cal.add_component(ev)

    return cal.to_ical()


# ===================== GitHub Action entry =====================

async def generate_and_write(property_ids, outdir="public"):
    """
    Generate one .ics per property ID and write index.html.
    On error, we leave placeholder .ics and show the error text on index.html.
    """
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written = []
    debug_lines = []

    try:
        # Generate .ics per property
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)

            # Pick a property name if present (for calendar display)
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
                meta_count = None
                if isinstance(bookings, list):
                    meta_count = len(bookings)
                debug_lines.append(f"PID {pid}: bookings returned = {meta_count}")

        # Build index.html
        html = []
        html.append("<h1>Redroofs iCal Feeds</h1>")
        html.append("<p>Feeds regenerate hourly.</p>")
        for pid in property_ids:
            html.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and debug_lines:
            html.append("<hr><pre>")
            html.extend(debug_lines)
            html.append("</pre>")

        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html))

        return written

    except Exception as e:
        # Placeholder files (valid but empty VCALENDAR) + error on index
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
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + str(e) + "\n" + traceback.format_exc() + "</pre>")
        return written


# Optional local test:
# If you want to run this locally (not in Actions), set:
#   export BOOKSTER_API_KEY=... 
#   python -c "import asyncio, app; asyncio.run(app.generate_and_write(['158595','158596','158497'],'./public'))"
if __name__ == "__main__":
    import asyncio
    pids_env = os.getenv("PROPERTY_IDS", "")
    pids = [p.strip() for p in pids_env.split(",") if p.strip()] or ["158595", "158596", "158497"]
    asyncio.run(generate_and_write(pids, "./public"))
