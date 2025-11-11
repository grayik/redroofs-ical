# app.py — static generator: Bookster -> iCal (all-day, IN/OUT split)
# Used by the GitHub Actions workflow (no web server needed).

import os
import typing as t
from datetime import date, datetime, timedelta

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ======== Configuration ========
# Use the APP domain (returns data) rather than API domain (403s in your case)
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# Known property ids -> short codes for titles
PROPERTY_CODES = {
    "158595": "BO",  # Barn Owl Cabin
    "158596": "RR",  # Redroofs by the Woods
    "158497": "BB",  # Bumblebee Cabin
}

# ======== helpers ========
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


# ======== Bookster access ========
async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> t.List[dict]:
    """
    Fetch bookings for one property (entry) using Basic auth:
      username='x', password=<API key>  (per Bookster docs)
    We DO NOT trust server-side 'st=confirmed' filter (often zero). We:
      1) Try with st=confirmed (cheap if it works)
      2) Fallback to no 'st' (then filter locally by state=='confirmed')
    """
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    pid = str(property_id)

    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        # Attempt 1: server filter by entry and confirmed
        params1 = {"ei": pid, "pp": 200, "st": "confirmed"}
        r = await client.get(url, params=params1, auth=("x", BOOKSTER_API_KEY))
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"Unexpected redirect {r.status_code} from {url}")
        r.raise_for_status()
        payload = r.json()
        items = _extract_list(payload)
        if _count_meta(payload) == 0 or not items:
            # Attempt 2: server filter by entry only; we’ll filter confirmed locally
            params2 = {"ei": pid, "pp": 200}
            r2 = await client.get(url, params=params2, auth=("x", BOOKSTER_API_KEY))
            r2.raise_for_status()
            payload2 = r2.json()
            items2 = _extract_list(payload2)
            items = [b for b in (items2 or []) if (b.get("state") or "").lower() == "confirmed"]

    return items or []


def _extract_list(payload: t.Any) -> t.List[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, list):
        return payload
    return []


def _count_meta(payload: t.Any) -> int:
    if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
        c = payload["meta"].get("count")
        try:
            return int(c)
        except Exception:
            return 0
    return 0


# ======== mapping ========
def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative", "quote", "paymentreq", "paymentnak"):
        return None

    # Dates (Bookster provides epoch seconds for start_inclusive / end_exclusive)
    arrival = _to_date(b.get("start_inclusive"))
    bookster_end_exclusive = _to_date(b.get("end_exclusive"))
    if not arrival or not bookster_end_exclusive:
        return None

    # For split-all-days, we need the actual checkout day to appear on the calendar.
    # Bookster's end_exclusive *is* the checkout date (non-inclusive time), so we include it as an OUT day.
    checkout_day = bookster_end_exclusive  # keep as-is; we'll render OUT on this day

    # Names
    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (first + " " + last).strip() or "Guest"

    # Contacts
    email = b.get("customer_email") or None
    mobile = (
        b.get("customer_tel_mobile")
        or b.get("customer_mobile")
        or b.get("customer_tel_day")
        or b.get("customer_tel_evening")
        or b.get("customer_phone")
        or None
    )

    # Party size
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else None
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
        # Paid = total value - remaining balance (matches your earlier checks)
        paid = max(0.0, value - balance)

    # Extras: prefer lines[] entries with type == "extra"
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
        "checkout": checkout_day,
        "guest_name": display_name,
        "email": email,
        "mobile": mobile,
        "party_total": party_total,
        "extras": extras_list,
        "reference": str(b.get("id") or b.get("reference") or ""),
        "property_name": b.get("entry_name"),
        "property_id": str(b.get("entry_id") or ""),
        "channel": b.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
    }


# ======== iCal rendering (split all-day events with IN/OUT) ========
def _add_all_day(ev: Event, day: date) -> None:
    """
    Add all-day by setting DTSTART=day and DTEND=day+1 (RFC5545 all-day semantics).
    """
    ev.add("dtstart", day)                  # VALUE=DATE from date object
    ev.add("dtend", day + timedelta(days=1))


def _title_for_day(mapped: dict, current_day: date) -> str:
    """
    Title rules:
      - First day: "IN: Name xN (CODE)"
      - Middle days: "Name (CODE)"
      - Checkout day: "OUT: Name (CODE)"
      - Always include x1 on the IN day.
      - CODE is BO/RR/BB based on property id.
    """
    name = mapped["guest_name"]
    pid = mapped.get("property_id") or ""
    code = PROPERTY_CODES.get(str(pid), "RR")
    party = mapped.get("party_total") or 1

    if current_day == mapped["arrival"]:
        return f"IN: {name} x{party} ({code})"
    if current_day == mapped["checkout"]:
        return f"OUT: {name} ({code})"
    return f"{name} ({code})"


def render_calendar_split_days(bookings: t.List[dict], property_name: t.Optional[str] = None) -> bytes:
    """
    Create all-day events for each calendar day, including an OUT event on checkout day.
    """
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for raw in bookings:
        mapped = map_booking_to_event_data(raw)
        if not mapped:
            continue

        # Day loop: from arrival up to and including checkout day
        day = mapped["arrival"]
        while day <= mapped["checkout"]:
            ev = Event()
            ev.add("summary", _title_for_day(mapped, day))
            _add_all_day(ev, day)

            # UID per-day so updates replace correctly in client calendars
            uid = f"redroofs-{mapped.get('reference')}-{day.isoformat()}"
            ev.add("uid", uid)

            # Description details (once per event; same info each day)
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

            # Booking link
            if mapped.get("reference"):
                bid = mapped["reference"]
                lines.append(f"Booking: https://app.booksterhq.com/bookings/{bid}/view")

            ev.add("description", "\n".join(lines) if lines else "Guest booking")
            cal.add_component(ev)

            day += timedelta(days=1)

    return cal.to_ical()


# ======== GitHub Action entry ========
async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    """
    Generate one .ics per property and a simple index.html.
    On error, create placeholder feeds and surface the error in index.html.
    """
    import traceback

    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    debug_lines: t.List[str] = []

    try:
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)
            prop_name = None
            for b in bookings:
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break

            ics_bytes = render_calendar_split_days(bookings, prop_name)
            path = os.path.join(outdir, f"{pid}.ics")
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

            if DEBUG_DUMP:
                debug_lines.append(f"PID {pid}: events={len(bookings)}")

        # Build index.html
        html = ["<h1>Redroofs iCal Feeds</h1>", "<p>Feeds regenerate hourly.</p>"]
        for pid in property_ids:
            html.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and debug_lines:
            html.append("<hr><pre>" + "\n".join(debug_lines) + "</pre>")

        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html))

        return written

    except Exception as e:
        # Placeholders + error message
        placeholder = "\n".join(
            ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Redroofs Bookster iCal//EN", "END:VCALENDAR", ""]
        )
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>Error: " + str(e) + "</pre>")
        return written
