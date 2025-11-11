# app.py — Bookster → iCal generator for GitHub Pages builds (copy-paste ready)

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ======== Config ========
# IMPORTANT: you said the "app." host returns data; so use that:
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKINGS_LIST_PATH = os.getenv("BOOKINGS_LIST_PATH", "booking/bookings.json")
BOOKING_DETAILS_PATH_TMPL = os.getenv("BOOKING_DETAILS_PATH_TMPL", "booking/bookings/{id}.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# ======== Helpers ========
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

def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")

# ======== Bookster API calls ========
async def _bookster_get_json(url: str, params: dict | None = None) -> t.Any:
    # Auth per docs: HTTP Basic with username 'x' and password = API key
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params or {}, auth=("x", BOOKSTER_API_KEY))
        # If you ever see redirects here, something is off (bad base/path)
        r.raise_for_status()
        return r.json()

async def fetch_bookings_for_property(entry_id: str) -> list[dict]:
    """
    List confirmed bookings for a property (entry).
    Uses 'ei' filter and returns up to 200 newest.
    """
    url = _join_url(BOOKSTER_API_BASE, BOOKINGS_LIST_PATH)
    params = {"ei": entry_id, "pp": 200, "st": "confirmed"}
    payload = await _bookster_get_json(url, params)

    items: list[dict]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        items = payload["results"]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    return items

async def fetch_booking_details(booking_id: t.Union[str, int]) -> dict:
    """
    Pull details for one booking (for mobile + extras via lines[]).
    """
    path = BOOKING_DETAILS_PATH_TMPL.format(id=str(booking_id))
    url = _join_url(BOOKSTER_API_BASE, path)
    payload = await _bookster_get_json(url)
    return payload if isinstance(payload, dict) else {}

# ======== Mapping ========
def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative", "quote"):
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (first + " " + last).strip() or "Guest"

    # Email is present in list payload
    email = b.get("customer_email") or None

    # Mobile may not be in list payload; a later enrich pass can fill it
    mobile = b.get("customer_tel_mobile") or b.get("customer_tel_day") or b.get("customer_tel_evening") or None

    # Party size (string or int)
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if str(party_val).strip() != "" else None
    except Exception:
        party_total = None

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
        paid = max(0.0, value - balance)

    # Extras list may be filled during enrich step (from details lines[])
    extras_list = b.get("_extras_list") or []

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

# ======== iCal rendering ========
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
        ev.add("summary", mapped["guest_name"])
        ev.add("dtstart", mapped["arrival"])     # all-day
        ev.add("dtend", mapped["departure"])     # checkout (non-inclusive)
        uid = f"redroofs-{(mapped.get('reference') or mapped['guest_name'])}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)

        lines: list[str] = []
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
        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)

    return cal.to_ical()

# ======== GitHub Action entry ========
async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """
    Generate one .ics per property, enriching each booking with details
    to get customer_tel_mobile and extras (lines of type 'extra').
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    debug_lines: list[str] = []
    try:
        for pid in property_ids:
            # Step 1: list
            bookings = await fetch_bookings_for_property(pid)
            list_count = len(bookings)

            # Step 2: enrich each booking with details (mobile, extras)
            enriched = 0
            for b in bookings:
                try:
                    details = await fetch_booking_details(b.get("id"))
                    # mobile:
                    b["customer_tel_mobile"] = (
                        details.get("customer_tel_mobile")
                        or details.get("customer_tel_day")
                        or details.get("customer_tel_evening")
                        or b.get("customer_tel_mobile")
                    )
                    # extras from lines[]
                    extras_list: list[str] = []
                    lines = details.get("lines")
                    if isinstance(lines, list):
                        for ln in lines:
                            if isinstance(ln, dict) and ln.get("type") == "extra":
                                name = ln.get("name") or ln.get("title") or "Extra"
                                qty = ln.get("quantity") or ln.get("qty")
                                extras_list.append(f"{name} x{qty}" if qty else name)
                    b["_extras_list"] = extras_list
                    enriched += 1
                except Exception:
                    # If a single details call fails, continue gracefully
                    b["_extras_list"] = b.get("_extras_list", [])

            # Render and write
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
                debug_lines.append(f"PID {pid}: list_count={list_count}, enriched={enriched}")

        # index.html
        html = ["<h1>Redroofs iCal Feeds</h1>", "<p>Feeds regenerate hourly.</p>"]
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
        # Write minimal placeholder files and an error message on index.html
        placeholder = "\n".join(
            ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Redroofs Bookster iCal//EN", "END:VCALENDAR", ""]
        )
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>Error generating feeds: " + str(e) + "</pre>")
        return written
