# app.py — Bookster → iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import json
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------------- Configuration ----------------
# Per Bookster docs: https://api.booksterhq.com/system/api/v1/booking/bookings.json
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
# Optional: some tenants require a client id filter ("ci")
BOOKSTER_CLIENT_ID = os.getenv("BOOKSTER_CLIENT_ID", "").strip() or None

# Debug: when "1", write debug.json with API responses to the published site
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
async def _fetch(client: httpx.AsyncClient, params: dict) -> dict:
    """
    Low-level GET that returns the parsed JSON payload (dict or list).
    Raises for HTTP errors; does not follow redirects (so we can catch auth mistakes).
    """
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    r = await client.get(
        url,
        params=params,
        auth=("x", BOOKSTER_API_KEY),          # username 'x', password = API key (per docs)
        headers={"Accept": "application/json"},
    )
    if r.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(
            f"Unexpected redirect ({r.status_code}) from {url}. "
            "Double-check BOOKSTER_API_BASE/BOOKSTER_BOOKINGS_PATH and credentials."
        )
    r.raise_for_status()
    return r.json()

def _extract_items(payload: t.Any) -> t.List[dict]:
    """
    Bookster list responses are typically:
      { "meta": {...}, "data": [ ... ] }
    but be resilient to other shapes.
    """
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

def _extract_meta(payload: t.Any) -> dict:
    return payload.get("meta", {}) if isinstance(payload, dict) else {}

async def fetch_bookings_for_property(property_id: str) -> t.List[dict]:
    """
    Fetch confirmed bookings for one entry/property.
    We try a few parameter patterns, because tenants can vary by config.
    """
    attempts_log: t.List[dict] = []

    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        # Attempt 1: server-side filter by entry_id (ei) + confirmed
        params1 = {"ei": property_id, "pp": 200, "st": "confirmed"}
        if BOOKSTER_CLIENT_ID:
            params1["ci"] = BOOKSTER_CLIENT_ID
        payload1 = await _fetch(client, params1)
        items1 = _extract_items(payload1)
        meta1 = _extract_meta(payload1)
        attempts_log.append({"attempt": 1, "params": params1, "meta": meta1, "count": len(items1)})
        if items1:
            return items1

        # Attempt 2: no server filter by entry, still confirmed — we’ll filter client-side
        params2 = {"pp": 200, "st": "confirmed"}
        if BOOKSTER_CLIENT_ID:
            params2["ci"] = BOOKSTER_CLIENT_ID
        payload2 = await _fetch(client, params2)
        items2_all = _extract_items(payload2)
        meta2 = _extract_meta(payload2)
        items2 = [i for i in items2_all if str(i.get("entry_id")) == str(property_id)]
        attempts_log.append({
            "attempt": 2, "params": params2, "meta": meta2,
            "visible_total": len(items2_all), "filtered_for_pid": len(items2),
        })
        if items2:
            return items2

        # Attempt 3: broadest — no state filter, client-side filter only
        params3 = {"pp": 200}
        if BOOKSTER_CLIENT_ID:
            params3["ci"] = BOOKSTER_CLIENT_ID
        payload3 = await _fetch(client, params3)
        items3_all = _extract_items(payload3)
        meta3 = _extract_meta(payload3)
        items3 = [i for i in items3_all if str(i.get("entry_id")) == str(property_id)]
        attempts_log.append({
            "attempt": 3, "params": params3, "meta": meta3,
            "visible_total": len(items3_all), "filtered_for_pid": len(items3),
        })

    # Optionally dump the attempts for debugging
    if DEBUG_DUMP:
        try:
            os.makedirs("public", exist_ok=True)
            with open("public/debug.json", "w", encoding="utf-8") as f:
                json.dump({"property_id": property_id, "attempts": attempts_log}, f, indent=2)
        except Exception:
            pass

    return items3  # may be empty

# ---------------- Mapping ----------------
def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative", "quote", "paymentreq", "paymentnak"):
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

    # extras: build from lines[] where type == "extra"
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
        ev = Event()
        ev.add("summary", mapped["guest_name"])   # title = guest name
        ev.add("dtstart", mapped["arrival"])      # all-day
        ev.add("dtend", mapped["departure"])      # checkout (non-inclusive)
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

# ---------------- GitHub Action entry ----------------
async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    Generate one .ics per property and an index.html.
    If an error occurs, write placeholder feeds and an error message in index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []

    try:
        # Write .ics for each property
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)
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

        # Build index.html (and optional debug dump link)
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP:
            if os.path.exists(os.path.join(outdir, "debug.json")):
                html_lines.append("<hr><p><a href='debug.json'>debug.json</a></p>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        # Minimal placeholder feeds and visible error
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
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + str(e) + "</pre>")
        return written
