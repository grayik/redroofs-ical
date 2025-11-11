# app.py — Bookster → iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------- Configuration ----------
# Use the working host you confirmed:
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
).rstrip("/")

BOOKINGS_LIST_PATH = os.getenv(
    "BOOKINGS_LIST_PATH",
    "booking/bookings.json",
).lstrip("/")

BOOKING_DETAIL_PATH_TMPL = os.getenv(
    "BOOKING_DETAIL_PATH_TMPL",
    "booking/bookings/{id}.json",
)

BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# ---------- Helpers ----------
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

# ---------- Bookster access ----------
async def list_confirmed_bookings_for_entry(
    client: httpx.AsyncClient, entry_id: t.Union[str, int]
) -> t.List[dict]:
    """
    GET /booking/bookings.json?ei=<entry_id>&st=confirmed&pp=200
    """
    url = f"{BOOKSTER_API_BASE}/{BOOKINGS_LIST_PATH}"
    params = {"ei": str(entry_id), "st": "confirmed", "pp": 200}
    r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY), follow_redirects=False)
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, list):
        return payload
    return []

async def get_booking_detail(client: httpx.AsyncClient, booking_id: t.Union[str, int]) -> dict:
    """
    GET /booking/bookings/{id}.json
    """
    path = BOOKING_DETAIL_PATH_TMPL.format(id=str(booking_id))
    url = f"{BOOKSTER_API_BASE}/{path.lstrip('/')}"
    r = await client.get(url, auth=("x", BOOKSTER_API_KEY), follow_redirects=False)
    r.raise_for_status()
    return r.json()

def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative", "quote"):
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    # Name
    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    guest_name = (first + " " + last).strip() or "Guest"

    # Contact
    email = b.get("customer_email") or None

    # Mobile can appear under several keys; detail response shows "customer_tel_mobile"
    mobile = (
        b.get("customer_tel_mobile")
        or b.get("customer_mobile")
        or b.get("customer_phone")
        or None
    )

    # Party size
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
    paid = max(0.0, (value or 0.0) - (balance or 0.0)) if (value is not None and balance is not None) else None

    # Extras: detail response includes "lines" with type="extra"
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
        "guest_name": guest_name,
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

# ---------- iCal rendering ----------
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
        ev.add("summary", mapped["guest_name"])   # Event title
        ev.add("dtstart", mapped["arrival"])      # All-day start
        ev.add("dtend", mapped["departure"])      # Non-inclusive checkout
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

# ---------- GitHub Action entry ----------
async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    For each property (entry_id):
      1) list confirmed bookings
      2) fetch detail for each booking (to get mobile + extras)
      3) write {pid}.ics
    Also writes index.html (and a debug section if DEBUG_DUMP=1).
    """
    import asyncio
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    debug_lines: t.List[str] = []

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            for pid in property_ids:
                # 1) list confirmed
                lst = await list_confirmed_bookings_for_entry(client, pid)
                debug_lines.append(f"PID {pid}: list_count={len(lst)}")

                # 2) fetch detail per booking (concurrently but politely)
                detail_ids = [b.get("id") for b in lst if isinstance(b, dict) and b.get("id") is not None]
                details: t.List[dict] = []
                # small concurrency
                sem = asyncio.Semaphore(6)

                async def _fetch_one(bid):
                    async with sem:
                        try:
                            return await get_booking_detail(client, bid)
                        except Exception:
                            return None

                if detail_ids:
                    res = await asyncio.gather(*[_fetch_one(bid) for bid in detail_ids])
                    details = [d for d in res if isinstance(d, dict)]

                debug_lines.append(f"PID {pid}: enriched={len(details)}")

                # Prefer enriched data when available; fall back to list data
                source = details if details else lst

                # infer property name from any record
                prop_name = None
                for b in source:
                    if isinstance(b, dict) and b.get("entry_name"):
                        prop_name = b.get("entry_name")
                        break

                ics_bytes = render_calendar(source, prop_name)
                path = os.path.join(outdir, f"{pid}.ics")
                with open(path, "wb") as f:
                    f.write(ics_bytes)
                written.append(path)

        # Build index.html
        html = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP:
            html.append("<hr><h2>Debug</h2><pre>")
            html.extend(debug_lines)
            html.append("</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html))

        return written

    except Exception as e:
        # Write placeholders and show error
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
            f.write("<h1>Redroofs iCal Feeds</h1>\n"
                    "<p>Placeholder build due to error.</p>\n"
                    f"<pre>{str(e)}</pre>")
        return written
