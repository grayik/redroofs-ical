# Minimal Bookster -> iCal generator for GitHub Pages builds
# The GitHub Action runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------------- Configuration ----------------
# You reported app.booksterhq.com works (api.booksterhq.com blocked 403 for you)
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
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


def _to_float(x: t.Any) -> t.Optional[float]:
    try:
        # Strings like "88.07" or numbers are fine
        return float(x)
    except Exception:
        return None


# ---------------- Bookster access ----------------

async def _get_bookings(params: dict) -> dict:
    """Low-level GET that returns the raw JSON dict."""
    url = "%s/%s" % (BOOKSTER_API_BASE.rstrip("/"), BOOKSTER_BOOKINGS_PATH.lstrip("/"))
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        # Per docs: Basic auth with username 'x' and password = API key
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError("Unexpected redirect %s from %s" % (r.status_code, url))
        r.raise_for_status()
        return r.json()


def _extract_list(payload: t.Any) -> t.List[dict]:
    """Normalise list out of various possible top-level shapes."""
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        if isinstance(payload.get("bookings"), list):
            return payload["bookings"]
        # Fallback: first list value we can find
        for v in payload.values():
            if isinstance(v, list):
                return v
        return []
    if isinstance(payload, list):
        return payload
    return []


async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> t.List[dict]:
    """
    Fetch bookings for one property (entry).
    Strategy:
      1) Try server-side filter with ei=<property_id> AND st=confirmed.
      2) If count == 0, try without st (some accounts don't support st on list).
      3) If we had to drop st, filter confirmed on the client.
    """
    pid = str(property_id)

    # Attempt 1: server filter for confirmed
    p1 = {"ei": pid, "pp": 200, "st": "confirmed"}
    payload1 = await _get_bookings(p1)
    items1 = _extract_list(payload1)
    meta1 = payload1["meta"] if isinstance(payload1, dict) and "meta" in payload1 else {"count": len(items1)}
    if DEBUG_DUMP:
        print(f"PID {pid}: Attempt 1 params={p1} meta={meta1} count={len(items1)}")
    if items1:
        return items1

    # Attempt 2: server filter only by entry, no state
    p2 = {"ei": pid, "pp": 200}
    payload2 = await _get_bookings(p2)
    items2 = _extract_list(payload2)
    meta2 = payload2["meta"] if isinstance(payload2, dict) and "meta" in payload2 else {"count": len(items2)}
    if DEBUG_DUMP:
        print(f"PID {pid}: Attempt 2 params={p2} meta={meta2} count={len(items2)}")
    if items2:
        # Filter confirmed client-side
        items2c = [b for b in items2 if (b.get("state") or "").lower() == "confirmed"]
        if DEBUG_DUMP:
            print(f"PID {pid}: client-filtered confirmed={len(items2c)}")
        return items2c

    # Attempt 3: no server filter (fallback)
    p3 = {"pp": 100}
    payload3 = await _get_bookings(p3)
    items3 = _extract_list(payload3)
    meta3 = payload3["meta"] if isinstance(payload3, dict) and "meta" in payload3 else {"count": len(items3)}
    if DEBUG_DUMP:
        print(f"PID {pid}: Attempt 3 params={p3} meta={meta3} visible_total={len(items3)}")
    items3p = [b for b in items3 if str(b.get("entry_id")) == pid]
    items3pc = [b for b in items3p if (b.get("state") or "").lower() == "confirmed"]
    if DEBUG_DUMP:
        print(f"PID {pid}: filtered_for_pid={len(items3p)} confirmed_after_filter={len(items3pc)}")
    return items3pc


# ---------------- Mapping ----------------

def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    # Ignore non-confirmed here, just in case
    state = (b.get("state") or "").lower()
    if state not in ("confirmed",):
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (first + " " + last).strip() or "Guest"

    # EMAIL
    email = b.get("customer_email") or None

    # MOBILE — your sample uses `customer_tel_mobile`
    mobile = (
        b.get("customer_tel_mobile")
        or b.get("customer_mobile")
        or b.get("customer_phone")
        or b.get("customer_tel_day")
        or b.get("customer_tel_evening")
    ) or None

    # PARTY SIZE (can be string or int)
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else None
    except Exception:
        party_total = None

    # MONEY
    value = _to_float(b.get("value"))
    balance = _to_float(b.get("balance"))
    if balance is None:
        balance = 0.0
    paid = None
    if value is not None:
        paid = max(0.0, value - (balance or 0.0))
    currency = (b.get("currency") or "").upper() or None

    # EXTRAS from lines[] of type "extra"
    extras_list: t.List[str] = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(f"{name} x{qty}" if qty else name)

    # Booking link
    booking_id = b.get("id")
    booking_url = None
    if booking_id:
        booking_url = f"https://app.booksterhq.com/bookings/{booking_id}/view"

    return {
        "arrival": arrival,
        "departure": departure,
        "guest_name": display_name,
        "email": email,
        "mobile": mobile,
        "party_total": party_total,
        "extras": extras_list,
        "reference": booking_id,
        "property_name": b.get("entry_name"),
        "property_id": b.get("entry_id"),
        "channel": b.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
        "booking_url": booking_url,
    }


# ---------------- iCal rendering ----------------

def _format_amount(currency: t.Optional[str], amount: t.Optional[float]) -> t.Optional[str]:
    if amount is None:
        return None
    amt = f"{amount:.2f}"
    return f"{currency} {amt}" if currency else amt


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
        # Title: guest name only (your current design)
        ev.add("summary", mapped["guest_name"])

        # All-day arrival -> checkout (checkout is non-inclusive)
        ev.add("dtstart", mapped["arrival"])
        ev.add("dtend", mapped["departure"])

        # UID
        uid = f"redroofs-{(mapped.get('reference') or mapped['guest_name'])}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)

        # Description lines (email, mobile, party, extras, property, channel, amount paid, booking link)
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
        amt_str = _format_amount(mapped.get("currency"), mapped.get("paid"))
        if amt_str is not None:
            lines.append(f"Amount paid to us: {amt_str}")
        if mapped.get("booking_url"):
            lines.append(f"Booking: {mapped['booking_url']}")

        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)

    return cal.to_ical()


# ---------------- GitHub Action entry ----------------

async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    Generate one .ics per property and write an index.html.
    On error, write placeholder feeds and show the error on index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    try:
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

        # Simple index page
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        if DEBUG_DUMP:
            html_lines.append("<h2>Debug</h2>")
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")

        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        # Minimal placeholder so calendar clients still see a valid VCALENDAR
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
