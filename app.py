# app.py — Bookster ➜ iCal (GitHub Pages build)
# Generates .ics files per property and an index.html
# Uses list endpoint then enriches each booking via details endpoint to get mobile + extras

import os
import typing as t
from datetime import date, datetime

import asyncio
import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ================= Configuration =================
# Use the APP domain (works for you) and the system API path
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKINGS_LIST_PATH = os.getenv("BOOKINGS_LIST_PATH", "booking/bookings.json")
BOOKING_DETAIL_PATH = os.getenv("BOOKING_DETAIL_PATH", "booking/bookings/{id}.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Emit a debug section on index.html when set to "1"
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# Modest concurrency for details fetches (be nice to the API)
DETAILS_CONCURRENCY = int(os.getenv("DETAILS_CONCURRENCY", "5"))

# ================= Helpers =================

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

def _best_mobile(b: dict) -> t.Optional[str]:
    # Try likely phone fields from details payload first, then list payload fallbacks
    for key in (
        "customer_tel_mobile",
        "customer_tel_day",
        "customer_tel_evening",
        "customer_mobile",
        "customer_phone",
        "customer_tel",
    ):
        val = b.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None

# ================= Bookster access =================

async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> t.Any:
    # Bookster auth: username 'x', password = API key
    r = await client.get(url, params=params or {}, auth=("x", BOOKSTER_API_KEY), follow_redirects=False, timeout=60)
    if r.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(f"Unexpected redirect {r.status_code} from {url}")
    r.raise_for_status()
    return r.json()

async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> list[dict]:
    """List confirmed bookings for one property (entry)."""
    list_url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKINGS_LIST_PATH.lstrip('/')}"
    params = {"ei": str(property_id), "pp": 200, "st": "confirmed"}  # filter by entry_id
    async with httpx.AsyncClient() as client:
        payload = await _get_json(client, list_url, params)
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

async def fetch_booking_detail(booking_id: t.Union[int, str]) -> dict:
    """Get full details for one booking (to read phones + lines/extras)."""
    detail_url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKING_DETAIL_PATH.lstrip('/').replace('{id}', str(booking_id))}"
    async with httpx.AsyncClient() as client:
        payload = await _get_json(client, detail_url, None)
    # Some APIs return the object directly; others wrap it. Handle both:
    if isinstance(payload, dict):
        return payload
    return {}

async def enrich_with_details(bookings: list[dict]) -> list[dict]:
    """Fetch details for each booking (limited concurrency), merge back minimal fields we need."""
    sem = asyncio.Semaphore(DETAILS_CONCURRENCY)

    async def one(b: dict) -> dict:
        async with sem:
            try:
                details = await fetch_booking_detail(b.get("id"))
            except Exception:
                details = {}
        # Merge relevant fields without clobbering existing non-empty values
        merged = dict(b)
        # Ensure phone fields from details are available
        for k in (
            "customer_tel_mobile",
            "customer_tel_day",
            "customer_tel_evening",
            "customer_mobile",
            "customer_phone",
            "customer_tel",
        ):
            if k in details and details.get(k):
                merged[k] = details[k]
        # Copy lines if present (for extras)
        if isinstance(details.get("lines"), list):
            merged["lines"] = details["lines"]
        return merged

    return await asyncio.gather(*(one(b) for b in bookings))

# ================= Mapping & iCal =================

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
    mobile = _best_mobile(b)

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

    # extras from details.lines[] of type "extra"
    extras_list: list[str] = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(f"{name} x{qty}" if qty else name)

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

def render_calendar(bookings: list[dict], property_name: t.Optional[str] = None) -> bytes:
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
        uid = "redroofs-%s-%s" % ((mapped.get("reference") or mapped["guest_name"]), mapped["arrival"].isoformat())
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
        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()

# ================= GitHub Action entry =================

async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """Generate .ics files and write index.html.
    On error, write placeholder feeds and show the error on index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    try:
        debug_lines: list[str] = []
        # For each property: list bookings, then enrich with details
        for pid in property_ids:
            list_items = await fetch_bookings_for_property(pid)
            enriched = await enrich_with_details(list_items)
            # Try to infer a friendly calendar name
            prop_name = None
            for b in (enriched or list_items):
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break
            ics_bytes = render_calendar(enriched or list_items, prop_name)
            path = os.path.join(outdir, "%s.ics" % pid)
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

            if DEBUG_DUMP:
                # small debug summary
                meta_count = len(list_items)
                debug_lines.append(f"PID {pid}: list_count={meta_count}, enriched={len(enriched)}")

        # Build index.html
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append("<p><a href='%s.ics'>%s.ics</a></p>" % (pid, pid))
        if DEBUG_DUMP and debug_lines:
            html_lines.append("<hr><pre>" + "\n".join(debug_lines) + "</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        err_text = "Error generating feeds: %s" % str(e)
        placeholder = "\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Redroofs Bookster iCal//EN",
            "END:VCALENDAR",
            "",
        ])
        for pid in property_ids:
            with open(os.path.join(outdir, "%s.ics" % pid), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>%s</pre>" % err_text)
        return written
