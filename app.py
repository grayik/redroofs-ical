# app.py — Bookster → iCal generator for GitHub Pages builds
# The workflow runs: from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse


# ========= Configuration =========
# Per Bookster docs: https://api.booksterhq.com/system/api/v1/booking/bookings.json
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
# Set DEBUG_DUMP=1 in the workflow env to get detailed diagnostics
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"


# ========= Helpers =========
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


# ========= Bookster access =========
async def _bookster_get(client: httpx.AsyncClient, params: dict) -> dict:
    """
    Perform a GET to the bookings endpoint with Basic auth (username='x', password=API key).
    Returns parsed JSON (dict or list). Raises for HTTP errors.
    """
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY), headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> t.Tuple[t.List[dict], str]:
    """
    Fetch bookings for one property (entry). Returns (items, debug_text).

    Strategy:
    1) Hit with server-side filter ei={entry_id}. (per docs)
    2) If zero items, fetch a page WITHOUT filters to verify data visibility,
       then filter client-side by entry_id so we can confirm if anything is visible at all.
    3) Include a verbose debug trace (when DEBUG_DUMP=1).
    """
    debug_lines: t.List[str] = []
    items: t.List[dict] = []
    pid = str(property_id)

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        # Attempt 1 — server-side filter (recommended)
        params1 = {"ei": pid, "pp": 200, "st": "confirmed"}
        try:
            payload1 = await _bookster_get(client, params1)
            if isinstance(payload1, dict):
                data1 = payload1.get("data") or payload1.get("results") or []
            elif isinstance(payload1, list):
                data1 = payload1
            else:
                data1 = []
            items = data1 or []
            if DEBUG_DUMP:
                meta1 = payload1.get("meta") if isinstance(payload1, dict) else None
                debug_lines.append(f"Attempt 1 (server filter) params={params1} meta={meta1} count={len(items)}")
        except Exception as e:
            debug_lines.append(f"Attempt 1 error: {e}")

        # Attempt 2 — fall back to unfiltered fetch to test visibility, then filter locally
        if not items:
            try:
                params2 = {"pp": 100, "st": "confirmed"}
                payload2 = await _bookster_get(client, params2)
                if isinstance(payload2, dict):
                    data2 = payload2.get("data") or payload2.get("results") or []
                elif isinstance(payload2, list):
                    data2 = payload2
                else:
                    data2 = []
                all_items = data2 or []
                filtered = [b for b in all_items if str(b.get("entry_id")) == pid]
                if DEBUG_DUMP:
                    meta2 = payload2.get("meta") if isinstance(payload2, dict) else None
                    debug_lines.append(
                        f"Attempt 2 (no server filter) params={params2} meta={meta2} "
                        f"visible_total={len(all_items)} filtered_for_pid={len(filtered)}"
                    )
                items = filtered
            except Exception as e:
                debug_lines.append(f"Attempt 2 error: {e}")

    return items, "\n".join(debug_lines)


# ========= Mapping =========
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
    display_name = (f"{first} {last}").strip() or "Guest"

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

    extras_list: t.List[str] = []
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


# ========= iCal rendering =========
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
        ev = Event()
        ev.add("summary", mapped["guest_name"])      # title = guest name
        ev.add("dtstart", mapped["arrival"])         # all-day
        ev.add("dtend", mapped["departure"])         # checkout (non-inclusive)
        uid = f"redroofs-{(mapped.get('reference') or mapped['guest_name'])}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)

        lines: t.List[str] = []
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


# ========= GitHub Action entry =========
async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    Generate one .ics per property and write index.html.
    If an error occurs, write placeholder feeds and show the error on index.html.
    """
    import traceback

    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    debug_sections: t.List[str] = []

    try:
        # Build each feed
        for pid in property_ids:
            bookings, dbg = await fetch_bookings_for_property(pid)
            if DEBUG_DUMP:
                debug_sections.append(f"PID {pid}:\n{dbg or '(no debug)'}")

            # Try to infer a friendly name from the first booking, if present
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

        # Build index.html
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")

        if DEBUG_DUMP and debug_sections:
            html_lines.append("<hr><h2>Debug</h2><pre>")
            html_lines.extend([_escape_html(sec) for sec in debug_sections])
            html_lines.append("</pre>")

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
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + _escape_html(err_text) + "</pre>")
        return written


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
