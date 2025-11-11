# Minimal Bookster -> iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------------- Configuration ----------------
# Use the *app* host (per your test) because api.booksterhq.com returned 403 for your account
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKINGS_LIST_PATH = os.getenv("BOOKINGS_LIST_PATH", "booking/bookings.json")
BOOKING_DETAIL_PATH = os.getenv("BOOKING_DETAIL_PATH", "booking/bookings/{id}.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
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

async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict | list:
    # HTTP Basic with username 'x' and password = API key (per Bookster docs)
    r = await client.get(url, params=params or {}, auth=("x", BOOKSTER_API_KEY), follow_redirects=False, timeout=60)
    if r.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(f"Unexpected redirect {r.status_code} from {url}")
    r.raise_for_status()
    return r.json()

# ---------------- Bookster access ----------------

async def fetch_list_for_property(client: httpx.AsyncClient, property_id: str) -> list[dict]:
    """
    Strategy:
    1) Try server-side filter: ei={property_id}, st=confirmed
    2) If that returns 0, try ei={property_id} without st (some channels set state oddly)
    """
    base = BOOKSTER_API_BASE.rstrip("/")
    list_url = f"{base}/{BOOKINGS_LIST_PATH.lstrip('/')}"

    # Attempt 1: filter by entry + confirmed
    params = {"ei": property_id, "pp": 200, "st": "confirmed"}
    payload = await _get_json(client, list_url, params)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        return data

    # Attempt 2: filter by entry only
    params = {"ei": property_id, "pp": 200}
    payload = await _get_json(client, list_url, params)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        # If we removed st, we should filter out non-confirmed here
        data = [b for b in data if str(b.get("state", "")).lower() == "confirmed"]
        return data
    return []

async def fetch_detail(client: httpx.AsyncClient, booking_id: t.Union[str, int]) -> dict:
    base = BOOKSTER_API_BASE.rstrip("/")
    detail_url = f"{base}/{BOOKING_DETAIL_PATH.lstrip('/').format(id=booking_id)}"
    payload = await _get_json(client, detail_url)
    # Detail can come back as dict directly
    return payload if isinstance(payload, dict) else {}

# ---------------- Mapping ----------------

def _float_or_none(x) -> t.Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def enrich_with_detail(list_item: dict, detail: dict) -> dict:
    """Merge useful fields from detail into the list item, without mutating input."""
    d = dict(list_item)
    # Prefer detail contact fields when list ones are blank
    for k_src, k_dst in [
        ("customer_tel_mobile", "customer_mobile"),
        ("customer_tel_day", "customer_phone"),
    ]:
        if not d.get(k_dst) and detail.get(k_src):
            d[k_dst] = detail.get(k_src)

    # Prefer detail value/balance if list has zero/None
    for k in ("value", "balance", "currency"):
        v_list = d.get(k)
        v_detail = detail.get(k)
        if (v_list in (None, 0, "0", "0.0")) and v_detail not in (None, ""):
            d[k] = v_detail

    # Keep lines[] from detail (for extras)
    if isinstance(detail.get("lines"), list):
        d["lines"] = detail["lines"]
    return d

def map_booking_to_event_data(b: dict) -> dict | None:
    state = (b.get("state") or "").lower()
    if state not in {"confirmed"}:
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (first + " " + last).strip() or "Guest"

    # mobile + email (after enrichment these may be present)
    email = b.get("customer_email") or None
    mobile = b.get("customer_mobile") or b.get("customer_phone") or None

    # party size
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val not in (None, "") else None
    except Exception:
        party_total = None

    # money
    value = _float_or_none(b.get("value"))
    balance = _float_or_none(b.get("balance"))
    currency = (b.get("currency") or "").upper() or None

    # If still missing/zero, see if we can compute from lines
    if (value in (None, 0.0)) and isinstance(b.get("lines"), list):
        # Sum product+extra lines as a proxy for gross value
        vsum = 0.0
        for ln in b["lines"]:
            if isinstance(ln, dict) and ln.get("type") in {"product", "extra"}:
                vs = _float_or_none(ln.get("value"))
                qs = _float_or_none(ln.get("quantity") or 1)
                if vs is not None:
                    vsum += vs * (qs or 1.0)
        if vsum > 0:
            value = vsum

    paid = None
    if value is not None and balance is not None:
        paid = max(0.0, value - balance)

    # extras
    extras_list: list[str] = []
    if isinstance(b.get("lines"), list):
        for ln in b["lines"]:
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

        # NEW — add clickable booking link
        booking_id = mapped.get("reference")
        if booking_id:
            link = f"https://app.booksterhq.com/bookings/{booking_id}/view"
            lines.append(f"Booking: {link}")
        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()

# ---------------- GitHub Action entry ----------------

async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """
    Generate .ics files and write index.html. For each booking from the list,
    call the detail endpoint when we need better data (value/balance/mobile/extras).
    """
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    debug_lines: list[str] = []

    try:
        async with httpx.AsyncClient() as client:
            for pid in property_ids:
                # 1) list
                lst = await fetch_list_for_property(client, pid)
                debug_lines.append(f"PID {pid}: list_count={len(lst)}")

                # 2) enrich
                enriched: list[dict] = []
                for b in lst:
                    needs_detail = (
                        (not b.get("value")) or (b.get("value") in (0, "0", "0.0")) or
                        (not b.get("customer_mobile")) or
                        True  # also want extras from lines[]
                    )
                    if needs_detail and b.get("id"):
                        det = await fetch_detail(client, b["id"])
                        b2 = enrich_with_detail(b, det)
                        enriched.append(b2)
                    else:
                        enriched.append(b)
                debug_lines.append(f"PID {pid}: enriched={len(enriched)}")

                # 3) infer a property name for calendar title (from first enriched booking)
                prop_name = None
                for b in enriched:
                    if isinstance(b, dict) and b.get("entry_name"):
                        prop_name = b.get("entry_name")
                        break

                # 4) render & write
                ics_bytes = render_calendar(enriched, prop_name)
                path = os.path.join(outdir, f"{pid}.ics")
                with open(path, "wb") as f:
                    f.write(ics_bytes)
                written.append(path)

        # Build index.html (with optional debug)
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and debug_lines:
            html_lines.append("<hr><h2>Debug</h2><pre>")
            html_lines.extend(debug_lines)
            html_lines.append("</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        # Placeholder VCALENDAR + error on index page
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
