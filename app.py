# app.py — Bookster -> iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# -------- Configuration --------
# Use your working base by default:
# You said "app.booksterhq.com" returns data for you.
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Write extra info to index.html (1 to enable)
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"


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


async def _get_json(url: str, params: dict, debug_lines: list[str]) -> dict | list:
    """
    GET JSON with Bookster Basic auth (username 'x', password = API key).
    If we get a 401/403/redirect, try swapping between app/api hostnames once.
    """
    def swap_host(u: str) -> str:
        if "//app.booksterhq.com" in u:
            return u.replace("//app.booksterhq.com", "//api.booksterhq.com")
        if "//api.booksterhq.com" in u:
            return u.replace("//api.booksterhq.com", "//app.booksterhq.com")
        return u

    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        # First attempt
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        debug_lines.append(f"HTTP {r.status_code} GET {r.request.url}")
        if r.status_code in (200,):
            return r.json()

        # Fallback attempt (swap app<->api once)
        if r.status_code in (401, 403, 301, 302, 303, 307, 308):
            alt = swap_host(url)
            if alt != url:
                r2 = await client.get(alt, params=params, auth=("x", BOOKSTER_API_KEY))
                debug_lines.append(f"HTTP {r2.status_code} (fallback) GET {r2.request.url}")
                r2.raise_for_status()
                return r2.json()

        r.raise_for_status()
        return r.json()  # not reached


async def fetch_bookings_for_property(property_id: t.Union[int, str], debug_lines: list[str]) -> list[dict]:
    """
    Fetch confirmed bookings for an entry (property).
    Server-filter by `ei` (entry_id). If the server returns no data, we
    also try a second request without `ei` and then filter client-side.
    """
    base = BOOKSTER_API_BASE.rstrip("/")
    path = BOOKSTER_BOOKINGS_PATH.lstrip("/")
    url = f"{base}/{path}"

    # Attempt 1: server filter
    p1 = {"ei": str(property_id), "pp": 200, "st": "confirmed"}
    payload = await _get_json(url, p1, debug_lines)
    items = []
    meta = {}
    if isinstance(payload, dict):
        meta = payload.get("meta") or {}
        data = payload.get("data")
        if isinstance(data, list):
            items = data
        elif isinstance(payload.get("results"), list):
            items = payload["results"]
    elif isinstance(payload, list):
        items = payload

    debug_lines.append(f"PID {property_id}: Attempt 1 (server filter) params={p1} meta={meta or {}} count={len(items)}")

    if items:
        return items

    # Attempt 2: no server filter, client-side filter
    p2 = {"pp": 100, "st": "confirmed"}
    payload2 = await _get_json(url, p2, debug_lines)
    items2 = []
    meta2 = {}
    if isinstance(payload2, dict):
        meta2 = payload2.get("meta") or {}
        data2 = payload2.get("data")
        if isinstance(data2, list):
            items2 = data2
        elif isinstance(payload2.get("results"), list):
            items2 = payload2["results"]
        else:
            # look for any list
            for v in payload2.values():
                if isinstance(v, list):
                    items2 = v
                    break
    elif isinstance(payload2, list):
        items2 = payload2

    pid = str(property_id)
    filtered = [i for i in items2 if str(i.get("entry_id")) == pid]
    debug_lines.append(
        f"PID {property_id}: Attempt 2 (no server filter) params={p2} meta={meta2 or {}} "
        f"visible_total={len(items2)} filtered_for_pid={len(filtered)}"
    )
    return filtered


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

    # extras from lines[] of type "extra" if present
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
        ev.add("dtstart", mapped["arrival"])
        ev.add("dtend", mapped["departure"])
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


async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """
    Generate one .ics per property to outdir and write index.html with debug info.
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    debug_lines: list[str] = []

    try:
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid, debug_lines)
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

        # index.html with links + debug
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP:
            html_lines.append("<hr><pre>Debug\n")
            html_lines.extend(debug_lines)
            html_lines.append("</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        # Write minimal calendars and show error on index
        placeholder = "\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Redroofs//EN",
            "END:VCALENDAR",
            "",
        ])
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(f"<h1>Redroofs iCal Feeds</h1>\n<pre>Error: {e}</pre>")
        return written
