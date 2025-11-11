# app.py — Bookster → iCal generator for GitHub Pages
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse


# ================= Configuration =================
# IMPORTANT: Bookster returns data for you on the *app* host.
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
)
BOOKSTER_BOOKINGS_PATH = os.getenv(
    "BOOKSTER_BOOKINGS_PATH",
    "booking/bookings.json",
)
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"   # set to 1 in Actions if you want debug in index.html


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


# ================= Bookster access =================
async def _get_json(url: str, params: dict) -> dict:
    # Bookster auth: HTTP Basic (username='x', password=API key)
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        # If someone accidentally points to the wrong host and it 403/redirects, surface it
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"Unexpected redirect {r.status_code} for {url} → {r.headers.get('Location')}")
        r.raise_for_status()
        return r.json()


def _normalize_items(payload: t.Any) -> t.List[dict]:
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        # last resort: any list value
        for v in payload.values():
            if isinstance(v, list):
                return v
        return []
    if isinstance(payload, list):
        return payload
    return []


async def fetch_bookings_for_property(property_id: t.Union[int, str], debug: t.List[str]) -> t.List[dict]:
    """
    Fetch bookings for a single property (entry).
    Strategy:
      • Attempt 1: server-side filter with ei=<entry_id>, st=confirmed
      • Attempt 2: server-side filter with ei only (all states), then client-filter confirmed
    """
    base = BOOKSTER_API_BASE.rstrip("/")
    path = BOOKSTER_BOOKINGS_PATH.lstrip("/")
    url = f"{base}/{path}"

    # Attempt 1 — filter on server, confirmed only
    params1 = {"ei": str(property_id), "pp": 200, "st": "confirmed"}
    payload1 = await _get_json(url, params1)
    items1 = _normalize_items(payload1)
    meta1 = payload1.get("meta", {})
    debug.append(f"PID {property_id}: Attempt 1 (server filter) params={params1} meta={meta1} count={len(items1)}")
    if items1:
        return items1

    # Attempt 2 — broader fetch for that entry, then client filter confirmed
    params2 = {"ei": str(property_id), "pp": 200}
    payload2 = await _get_json(url, params2)
    items2 = _normalize_items(payload2)
    meta2 = payload2.get("meta", {})
    visible_total = len(items2)
    items2 = [i for i in items2 if (i.get("state") or "").lower() == "confirmed"]
    debug.append(
        f"PID {property_id}: Attempt 2 (no state filter) params={params2} meta={meta2} "
        f"visible_total={visible_total} confirmed_only={len(items2)}"
    )
    return items2


# ================= Mapping =================
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

    # extras from lines[] of type "extra"
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


# ================= iCal rendering =================
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
        ev.add("summary", mapped["guest_name"])
        ev.add("dtstart", mapped["arrival"])     # all-day
        ev.add("dtend", mapped["departure"])    # checkout (non-inclusive)
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


# ================= GitHub Action entry =================
async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    Generate one .ics per property and an index.html.
    If an exception occurs, write placeholder .ics files and show the error on index.html.
    """
    import traceback

    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    debug_lines: t.List[str] = []

    try:
        # Generate per-property feeds
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

        # Build index.html
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP:
            html_lines.append("<hr><h2>Debug</h2><pre>" + "\n".join(debug_lines) + "</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        # Placeholder calendar files on failure
        placeholder = "\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Redroofs Bookster iCal//EN",
            "END:VCALENDAR",
            "",
        ])
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + str(e) + "\n" + traceback.format_exc() + "</pre>")
        return written
