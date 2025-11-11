# Minimal Bookster -> iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse


# ---------------- Configuration ----------------
# Use the "app" host (you confirmed the "api" host returns 403 for you)
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1"
)
BOOKSTER_LIST_PATH = os.getenv(
    "BOOKSTER_BOOKINGS_PATH", "booking/bookings.json"
)
BOOKSTER_DETAIL_PATH_TMPL = "booking/bookings/{id}.json"
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Optional: write debug info on index.html when set to "1"
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
        return float(x)
    except Exception:
        return None


# ---------------- Bookster access ----------------
async def _get_json(url: str, params: dict | None = None) -> t.Any:
    # HTTP Basic auth: username 'x', password = API key (per Bookster docs)
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        # Block redirects to login (would signal bad path/host or auth)
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"Unexpected redirect {r.status_code} from {url}. "
                "Check BOOKSTER_API_BASE/paths and credentials."
            )
        r.raise_for_status()
        return r.json()


async def _list_bookings_for_property(entry_id: t.Union[int, str]) -> list[dict]:
    """
    List bookings for a property (entry).
    We DO NOT pass st=confirmed because that hid results for you earlier.
    We filter to confirmed on the client side.
    """
    base = BOOKSTER_API_BASE.rstrip("/")
    path = BOOKSTER_LIST_PATH.lstrip("/")
    url = f"{base}/{path}"

    # Ask server to filter by entry and to return plenty of results
    params = {"ei": str(entry_id), "pp": 200}

    payload = await _get_json(url, params)
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
        meta = payload.get("meta", {})
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        items = payload["results"]
        meta = payload.get("meta", {})
    elif isinstance(payload, list):
        items = payload
        meta = {}
    else:
        items, meta = [], {}

    # Client-side filter to confirmed
    confirmed = [b for b in items if (b.get("state") or "").lower() == "confirmed"]
    return confirmed


async def _get_booking_detail(booking_id: t.Union[int, str]) -> dict:
    base = BOOKSTER_API_BASE.rstrip("/")
    path = BOOKSTER_DETAIL_PATH_TMPL.format(id=str(booking_id)).lstrip("/")
    url = f"{base}/{path}"
    payload = await _get_json(url)
    # detail endpoint returns a dict (single object)
    return payload if isinstance(payload, dict) else {}


# ---------------- Mapping ----------------
def _map_to_event_data(list_item: dict, detail: dict | None) -> dict | None:
    """
    Combine list item + detail (detail may be None if fetch failed),
    and return a unified dict the calendar renderer expects.
    """
    src = {}
    if isinstance(list_item, dict):
        src.update(list_item)
    if isinstance(detail, dict):
        # detail values should override list values when present
        src.update({k: v for k, v in detail.items() if v not in (None, "", [])})

    # State
    state = (src.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative", "quote"):
        return None

    # Dates (as all-day)
    arrival = _to_date(src.get("start_inclusive"))
    departure = _to_date(src.get("end_exclusive"))
    if not arrival or not departure:
        return None

    # Guest
    first = (src.get("customer_forename") or "").strip()
    last = (src.get("customer_surname") or "").strip()
    display_name = (f"{first} {last}".strip() or "Guest").strip()

    email = src.get("customer_email") or None
    # Prefer mobile from detail naming used in docs: customer_tel_mobile
    mobile = (
        src.get("customer_tel_mobile")
        or src.get("customer_mobile")
        or src.get("customer_tel_day")
        or src.get("customer_tel_evening")
        or None
    )

    # Party
    party_val = src.get("party_size")
    try:
        party_total = int(party_val) if party_val not in (None, "") else None
    except Exception:
        party_total = None

    # Money
    value = _to_float(src.get("value"))
    balance = _to_float(src.get("balance"))
    currency = (src.get("currency") or "").upper() or None
    paid = None
    if value is not None and balance is not None:
        paid = max(0.0, value - balance)

    # Extras
    extras_list: list[str] = []
    lines = src.get("lines")
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
        "reference": src.get("id"),
        "property_name": src.get("entry_name"),
        "property_id": src.get("entry_id"),
        "channel": src.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
    }


# ---------------- iCal rendering ----------------
def _add_event(cal: Calendar, mapped: dict) -> None:
    ev = Event()
    ev.add("summary", mapped["guest_name"])  # title = guest name
    ev.add("dtstart", mapped["arrival"])     # all-day
    ev.add("dtend", mapped["departure"])     # checkout (non-inclusive)

    # UID for stability
    uid = f"redroofs-{mapped.get('reference') or mapped['guest_name']}-{mapped['arrival'].isoformat()}"
    ev.add("uid", uid)

    # Description
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
    if mapped.get("reference"):
        lines.append(f"Booking: https://app.booksterhq.com/bookings/{mapped['reference']}/view")

    ev.add("description", "\n".join(lines) if lines else "Guest booking")
    cal.add_component(ev)


def render_calendar(bookings: list[dict], property_name: str | None = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for mapped in bookings:
        if mapped:
            _add_event(cal, mapped)

    return cal.to_ical()


# ---------------- GitHub Action entry ----------------
async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """
    Generate .ics files and write index.html.
    We enrich each booking with the detail endpoint so mobile/value/balance are correct.
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    debug_lines: list[str] = []

    try:
        for pid in property_ids:
            # 1) List bookings for this entry
            listed = await _list_bookings_for_property(pid)
            debug_lines.append(f"PID {pid}: listed={len(listed)}")

            # 2) Enrich each with detail
            enriched: list[dict] = []
            for b in listed:
                bid = b.get("id")
                detail = await _get_booking_detail(bid) if bid else {}
                mapped = _map_to_event_data(b, detail)
                if mapped:
                    enriched.append(mapped)

            debug_lines.append(f"PID {pid}: enriched={len(enriched)}")

            # Infer property name (first booking’s entry_name) if present
            prop_name = None
            for m in enriched:
                if m.get("property_name"):
                    prop_name = m["property_name"]
                    break

            # 3) Render .ics
            ics_bytes = render_calendar(enriched, prop_name)
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
            html.append("<hr><pre>Debug\n" + "\n".join(debug_lines) + "</pre>")

        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html))

        return written

    except Exception as e:
        # Fallback: write placeholder calendars and surface the error on index.html
        placeholder = "\n".join(
            ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Redroofs Bookster iCal//EN", "END:VCALENDAR", ""]
        )
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + str(e) + "</pre>")
        return written
