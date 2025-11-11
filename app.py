# Minimal Bookster -> iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse


# ================= Configuration =================
# IMPORTANT: use the "app" domain (you observed the "api" domain 403's for you).
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
)
BOOKSTER_BOOKINGS_PATH = os.getenv(
    "BOOKSTER_BOOKINGS_PATH",
    "booking/bookings.json",
)
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Turn on to show detailed fetch attempts on index.html
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"


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
async def _bookster_get(params: dict) -> dict:
    """
    Low-level GET with Basic auth.
    Per Bookster docs: username must be literal 'x', password is the API key.
    """
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        r.raise_for_status()
        return r.json()


def _extract_list(payload: t.Any) -> t.List[dict]:
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        # last-ditch: return first list found
        for v in payload.values():
            if isinstance(v, list):
                return v
        return []
    if isinstance(payload, list):
        return payload
    return []


async def fetch_bookings_for_property(property_id: t.Union[int, str], debug_lines: t.List[str]) -> t.List[dict]:
    """
    Fetch *confirmed* bookings for one property (entry).
    Strategy:
      1) Ask server to filter by property and 'confirmed' (fast path).
      2) If empty, fetch confirmed (no entry filter) and client-filter by entry.
      3) If still empty, fetch all states for entry and client-filter state=confirmed.
    """
    pid = str(property_id)

    # Attempt 1 — server filter by entry + confirmed
    p1 = {"ei": pid, "pp": 200, "st": "confirmed"}
    payload1 = await _bookster_get(p1)
    items1 = _extract_list(payload1)
    meta1 = payload1.get("meta", {})
    debug_lines.append(f"PID {pid}: Attempt 1 params={p1} meta={meta1} count={len(items1)}")
    if items1:
        return items1

    # Attempt 2 — confirmed only, client-filter by entry
    p2 = {"pp": 200, "st": "confirmed"}
    payload2 = await _bookster_get(p2)
    items2_all = _extract_list(payload2)
    items2 = [i for i in items2_all if str(i.get("entry_id")) == pid]
    meta2 = payload2.get("meta", {})
    debug_lines.append(
        f"PID {pid}: Attempt 2 params={p2} meta={meta2} visible_total={len(items2_all)} filtered_for_pid={len(items2)}"
    )
    if items2:
        return items2

    # Attempt 3 — entry only, any state; then client-filter to confirmed
    p3 = {"ei": pid, "pp": 200}
    payload3 = await _bookster_get(p3)
    items3_all = _extract_list(payload3)
    items3 = [i for i in items3_all if (i.get("state") or "").lower() == "confirmed"]
    meta3 = payload3.get("meta", {})
    debug_lines.append(
        f"PID {pid}: Attempt 3 params={p3} meta={meta3} visible_total={len(items3_all)} confirmed_after_filter={len(items3)}"
    )
    return items3


# ================= Mapping =================
def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    # skip non-confirmed
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

    # email + mobile (Bookster often uses 'customer_tel_mobile' in details)
    email = b.get("customer_email") or None
    mobile = (
        b.get("customer_tel_mobile")  # details endpoint name
        or b.get("customer_mobile")   # sometimes present
        or b.get("customer_phone")    # fallback
        or None
    )

    # party size may be string or int
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val not in (None, "") else None
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

    # extras: try lines[] of type "extra" first; otherwise 'extras' list if present
    extras_list: t.List[str] = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(f"{name} x{qty}" if qty else name)
    elif isinstance(b.get("extras"), list):
        for x in b["extras"]:
            if isinstance(x, str):
                extras_list.append(x)
            elif isinstance(x, dict):
                name = x.get("name") or x.get("title") or x.get("code") or "Extra"
                qty = x.get("quantity") or x.get("qty")
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
        ev.add("summary", mapped["guest_name"])      # title
        ev.add("dtstart", mapped["arrival"])         # all-day
        ev.add("dtend", mapped["departure"])         # checkout
        uid = f"redroofs-{(mapped.get('reference') or mapped['guest_name'])}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)
        lines = []
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
    Generate .ics files and write index.html.
    On error, write placeholder feeds and show the error on index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    debug_lines: t.List[str] = []

    try:
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid, debug_lines)
            # infer property name from first booking (if present)
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
            debug_lines.append(f"PID {pid}: list_count={len(bookings)}; enriched={sum(1 for b in bookings if (b.get('state') or '').lower()=='confirmed')}")

        # index.html
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and debug_lines:
            html_lines.append("<hr><pre>Debug\n\n" + "\n".join(debug_lines) + "</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        # placeholder VCALENDAR for each property
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
        # index.html with error
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>Error generating feeds: " + str(e) + "</pre>")
        return written
