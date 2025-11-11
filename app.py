# app.py — Bookster -> iCal (GitHub Pages build)
# Generates one .ics per property id into ./public
# Uses Basic auth: username "x", password = API key (per Bookster docs)

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse


# ---------- Config ----------
# IMPORTANT: Bookster works for you on the *app* host, not the *api* host.
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
).rstrip("/")
BOOKINGS_LIST_PATH = os.getenv(
    "BOOKINGS_LIST_PATH", "booking/bookings.json"
).lstrip("/")
BOOKING_DETAIL_PATH = os.getenv(
    "BOOKING_DETAIL_PATH", "booking/bookings/{id}.json"
)

BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"


# ---------- helpers ----------
def _to_date(v: t.Union[str, int, float, date, datetime, None]) -> t.Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, (int, float)):
        try:
            return datetime.utcfromtimestamp(int(v)).date()
        except Exception:
            return None
    try:
        return dtparse.parse(str(v)).date()
    except Exception:
        return None


# ---------- API calls ----------
async def list_bookings_for_entry(client: httpx.AsyncClient, entry_id: str) -> list[dict]:
    """
    First pass: list bookings for a property (entry).
    We try with server-side filter (ei). If that returns 0, we retry without filters.
    """
    url = f"{BOOKSTER_API_BASE}/{BOOKINGS_LIST_PATH}"
    # Attempt 1: server filter
    params1 = {"ei": entry_id, "pp": 200, "st": "confirmed"}
    r = await client.get(url, params=params1, auth=("x", BOOKSTER_API_KEY))
    r.raise_for_status()
    payload = r.json() or {}
    data = payload.get("data") if isinstance(payload, dict) else None
    count1 = (payload.get("meta") or {}).get("count", 0) if isinstance(payload, dict) else (len(payload) if isinstance(payload, list) else 0)

    if DEBUG_DUMP:
        print(f"PID {entry_id}: Attempt 1 params={params1} meta={payload.get('meta') if isinstance(payload, dict) else None} count={count1}")

    if isinstance(data, list) and data:
        return data

    # Attempt 2: broader list, then client-side filter
    params2 = {"pp": 200, "st": "confirmed"}
    r = await client.get(url, params=params2, auth=("x", BOOKSTER_API_KEY))
    r.raise_for_status()
    payload2 = r.json() or {}
    data2 = payload2.get("data") if isinstance(payload2, dict) else None
    items = data2 if isinstance(data2, list) else (payload2 if isinstance(payload2, list) else [])
    filtered = [b for b in items if str(b.get("entry_id")) == str(entry_id)]

    if DEBUG_DUMP:
        meta2 = payload2.get("meta") if isinstance(payload2, dict) else None
        vis2 = len(items)
        print(f"PID {entry_id}: Attempt 2 params={params2} meta={meta2} visible_total={vis2} filtered_for_pid={len(filtered)}")

    return filtered


async def fetch_booking_detail(client: httpx.AsyncClient, booking_id: str) -> dict:
    """
    Second pass: get detail per booking to enrich with extras and any phone fields.
    """
    path = BOOKING_DETAIL_PATH.format(id=booking_id)
    url = f"{BOOKSTER_API_BASE}/{path.lstrip('/')}"
    r = await client.get(url, auth=("x", BOOKSTER_API_KEY))
    r.raise_for_status()
    return r.json() or {}


# ---------- mapping ----------
def map_booking_to_event(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state in {"cancelled", "canceled", "void", "rejected", "tentative"}:
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    guest_name = (f"{first} {last}").strip() or "Guest"

    # Email present on list; mobile typically only present on detail
    email = b.get("customer_email") or None
    mobile = (
        b.get("customer_tel_mobile")
        or b.get("customer_mobile")
        or b.get("customer_tel_day")
        or b.get("customer_tel_evening")
        or b.get("customer_phone")
        or None
    )

    # party size (string or int)
    ps = b.get("party_size")
    try:
        party_total = int(ps) if ps is not None and str(ps).strip() != "" else None
    except Exception:
        party_total = None

    # money
    def _f(x):
        try:
            return float(x)
        except Exception:
            return None

    value = _f(b.get("value"))
    balance = _f(b.get("balance"))
    currency = (b.get("currency") or "").upper() or None
    paid = max(0.0, (value or 0.0) - (balance or 0.0)) if (value is not None and balance is not None) else None

    # extras — from detail: lines[] with type == "extra"
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


# ---------- iCal ----------
def render_calendar(bookings: list[dict], property_name: t.Optional[str] = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for raw in bookings:
        m = map_booking_to_event(raw)
        if not m:
            continue
        ev = Event()
        ev.add("summary", m["guest_name"])
        ev.add("dtstart", m["arrival"])
        ev.add("dtend", m["departure"])  # non-inclusive checkout
        uid = f"redroofs-{(m.get('reference') or m['guest_name'])}-{m['arrival'].isoformat()}"
        ev.add("uid", uid)
        lines = []
        if m.get("email"):
            lines.append(f"Email: {m['email']}")
        if m.get("mobile"):
            lines.append(f"Mobile: {m['mobile']}")
        if m.get("party_total"):
            lines.append(f"Guests in party: {m['party_total']}")
        if m.get("extras"):
            lines.append("Extras: " + ", ".join(m["extras"]))
        if m.get("property_name"):
            lines.append(f"Property: {m['property_name']}")
        if m.get("channel"):
            lines.append(f"Channel: {m['channel']}")
        if m.get("paid") is not None:
            amt = f"{m['paid']:.2f}"
            if m.get("currency"):
                amt = f"{m['currency']} {amt}"
            lines.append(f"Amount paid to us: {amt}")
        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()


# ---------- main entry for the workflow ----------
async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """
    1) List bookings for each property.
    2) Enrich each booking with its detail (to pick up mobile + extras).
    3) Render one .ics per property into outdir and write index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    debug_lines: list[str] = []

    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        for pid in property_ids:
            # Step 1: list
            listed = await list_bookings_for_entry(client, pid)
            debug_lines.append(f"PID {pid}: list_count={len(listed)}")

            # Step 2: enrich with detail
            enriched: list[dict] = []
            for b in listed:
                bid = str(b.get("id"))
                if not bid:
                    continue
                try:
                    detail = await fetch_booking_detail(client, bid)
                    # merge list fields with detail (detail wins)
                    merged = dict(b)
                    if isinstance(detail, dict):
                        merged.update(detail)
                    enriched.append(merged)
                except Exception as e:
                    if DEBUG_DUMP:
                        print(f"PID {pid}: detail fetch failed for {bid}: {e}")

            debug_lines.append(f"PID {pid}: enriched={len(enriched)}")

            # try to find a property name
            prop_name = None
            for b in (enriched or listed):
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break

            ics_bytes = render_calendar(enriched or listed, prop_name)
            path = os.path.join(outdir, f"{pid}.ics")
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

    # index.html
    html = [
        "<h1>Redroofs iCal Feeds</h1>",
        "<p>Feeds regenerate hourly.</p>",
    ]
    for pid in property_ids:
        html.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
    if DEBUG_DUMP:
        html.append("<hr><pre>Debug\n\n" + "\n".join(debug_lines) + "</pre>")
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(html))

    return written
