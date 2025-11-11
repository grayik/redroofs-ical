"""
Redroofs → iCal (static generator for GitHub Pages)
- Fetch bookings from Bookster (list + per-id details)
- Compute correct "amount paid" and include mobile + extras
- Produce all-day events with IN / MID / OUT titles and property codes
"""

import os
import typing as t
from datetime import date, datetime, timedelta

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------- Config ----------
# IMPORTANT: Use the "app" host (you found api.* returns 403 for your key)
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
).rstrip("/")
BOOKINGS_LIST_PATH = os.getenv(
    "BOOKSTER_BOOKINGS_PATH",
    "booking/bookings.json",
).lstrip("/")
BOOKING_DETAIL_PATH_TMPL = "booking/bookings/{id}.json"

BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# Property code suffixes for titles
PROPERTY_CODE_BY_NAME = {
    "Redroofs by the Woods": "RR",
    "Barn Owl Cabin at Redroofs": "BO",
    "Bumblebee Cabin at Redroofs": "BB",
}

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


def _amount_paid(value: t.Optional[float], balance: t.Optional[float]) -> t.Optional[float]:
    """Paid = max(0, value - balance) when both are present. If only value exists and balance is 0/None, treat paid=value."""
    if value is None and balance is None:
        return None
    if balance is None:
        balance = 0.0
    if value is None:
        # No total value → we can't compute a 'paid' number
        return None
    return max(0.0, float(value) - float(balance))


def _property_code(name: str | None) -> str:
    if not name:
        return ""
    return PROPERTY_CODE_BY_NAME.get(name, name[:2].upper())


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None) -> t.Any:
    """GET JSON from Bookster with Basic auth (username 'x', password = API key)."""
    url = f"{BOOKSTER_API_BASE}/{path.lstrip('/')}"
    r = await client.get(url, params=params or {}, auth=("x", BOOKSTER_API_KEY), timeout=60, follow_redirects=False)
    # Guard against accidental redirects (often auth/base mismatch)
    if r.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(f"Redirect {r.status_code} at {url} (check BOOKSTER_API_BASE/paths and credentials)")
    r.raise_for_status()
    return r.json()


# ---------- Fetch & enrich ----------

async def fetch_list_for_property(client: httpx.AsyncClient, property_id: str) -> list[dict]:
    """
    Get bookings for a single property (entry).
    Some environments return 0 if we pass st=confirmed (we saw this), so we:
      1) Try with st=confirmed
      2) Try without st
    Then filter by state='confirmed' client-side.
    """
    debug_lines: list[str] = []

    # Attempt 1: with st=confirmed
    params1 = {"ei": property_id, "pp": 200, "st": "confirmed"}
    payload = await _get_json(client, BOOKINGS_LIST_PATH, params1)
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    count = int(meta.get("count", 0) or 0)
    debug_lines.append(f"Attempt 1 params={params1} meta={meta} count={count}")

    items: list[dict] = []
    if count and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]

    # Fallback Attempt 2: no st filter
    if not items:
        params2 = {"ei": property_id, "pp": 200}
        payload2 = await _get_json(client, BOOKINGS_LIST_PATH, params2)
        meta2 = payload2.get("meta", {}) if isinstance(payload2, dict) else {}
        visible_total = int(meta2.get("count", 0) or 0)
        data2 = payload2.get("data", []) if isinstance(payload2, dict) else []
        confirmed = [b for b in data2 if str(b.get("state", "")).lower() == "confirmed"]
        debug_lines.append(
            f"Attempt 2 params={params2} meta={meta2} visible_total={visible_total} confirmed_after_filter={len(confirmed)}"
        )
        items = confirmed

    if DEBUG_DUMP:
        return items, debug_lines
    return items


async def fetch_detail_for_ids(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    """Fetch per-booking details; return dict id -> detail."""
    results: dict[str, dict] = {}

    async def _one(bid: str):
        try:
            path = BOOKING_DETAIL_PATH_TMPL.format(id=bid)
            data = await _get_json(client, path)
            results[bid] = data if isinstance(data, dict) else {}
        except Exception:
            results[bid] = {}

    # Keep concurrency modest
    sem = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    # We already have a client; just schedule tasks
    tasks = [_one(bid) for bid in ids]
    # Execute in small batches to be polite
    for i in range(0, len(tasks), 10):
        batch = tasks[i : i + 10]
        await asyncio.gather(*batch)
    return results


# ---------- Mapping to our internal structure ----------

def map_booking(basic: dict, detail: dict | None) -> dict | None:
    state = (basic.get("state") or "").lower()
    if state not in {"confirmed"}:
        return None

    arrival = _to_date(basic.get("start_inclusive") if detail is None else detail.get("start_inclusive", basic.get("start_inclusive")))
    departure = _to_date(basic.get("end_exclusive") if detail is None else detail.get("end_exclusive", basic.get("end_exclusive")))
    if not arrival or not departure:
        return None

    # Guest name
    first = (detail or {}).get("customer_forename") or basic.get("customer_forename") or ""
    last = (detail or {}).get("customer_surname") or basic.get("customer_surname") or ""
    guest_name = (f"{first} {last}".strip()) or "Guest"

    # Contact: prefer detail fields
    email = (detail or {}).get("customer_email") or basic.get("customer_email") or None
    mobile = (
        (detail or {}).get("customer_tel_mobile")
        or (detail or {}).get("customer_mobile")
        or basic.get("customer_tel_mobile")
        or basic.get("customer_mobile")
        or None
    )

    # Party size
    party_val = (detail or {}).get("party_size", basic.get("party_size"))
    try:
        party_total = int(party_val) if party_val not in (None, "") else None
    except Exception:
        party_total = None

    # Money: prefer detail’s value/balance if present
    def _to_float(x):
        try:
            return float(x)
        except Exception:
            return None

    value = _to_float((detail or {}).get("value", basic.get("value")))
    balance = _to_float((detail or {}).get("balance", basic.get("balance")))
    paid = _to_float((detail or {}).get("paid"))  # if API ever provides direct
    if paid is None:
        paid = _amount_paid(value, balance)

    # Extras (from detail.lines of type 'extra')
    extras_list: list[str] = []
    lines = (detail or {}).get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(f"{name} x{qty}" if qty else name)

    return {
        "id": str(basic.get("id") or (detail or {}).get("id")),
        "arrival": arrival,
        "departure": departure,  # checkout day (non-inclusive when used as dtend for a span)
        "guest_name": guest_name,
        "email": email,
        "mobile": mobile,
        "party_total": party_total,
        "extras": extras_list,
        "property_name": (detail or {}).get("entry_name", basic.get("entry_name")),
        "property_id": (detail or {}).get("entry_id", basic.get("entry_id")),
        "channel": (detail or {}).get("syndicate_name", basic.get("syndicate_name")),
        "currency": (detail or {}).get("currency", basic.get("currency")),
        "paid": paid,
    }


# ---------- Event building (IN/MID/OUT all-day) ----------

def _single_day_event(cal: Calendar, summary: str, day: date, desc_lines: list[str], uid_suffix: str):
    ev = Event()
    ev.add("summary", summary)
    # all-day single day: DTSTART=day, DTEND=day+1
    ev.add("dtstart", day)
    ev.add("dtend", day + timedelta(days=1))
    ev.add("uid", uid_suffix)
    ev.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
    cal.add_component(ev)


def render_calendar(bookings: list[dict], calendar_name: str | None = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if calendar_name:
        cal.add("X-WR-CALNAME", f"{calendar_name} – Guests")

    for b in bookings:
        pname = b.get("property_name")
        code = _property_code(pname)
        guest = b["guest_name"]
        party = b.get("party_total")
        arrival = b["arrival"]
        checkout = b["departure"]  # this is the checkout *date*

        # Description
        lines: list[str] = []
        if b.get("email"):
            lines.append(f"Email: {b['email']}")
        if b.get("mobile"):
            lines.append(f"Mobile: {b['mobile']}")
        if party:
            lines.append(f"Guests in party: {party}")
        if b.get("extras"):
            lines.append("Extras: " + ", ".join(b["extras"]))
        if pname:
            lines.append(f"Property: {pname}")
        if b.get("channel"):
            lines.append(f"Channel: {b['channel']}")
        if b.get("paid") is not None:
            amt = f"{b['paid']:.2f}"
            if b.get("currency"):
                amt = f"{b['currency']} {amt}"
            lines.append(f"Amount paid to us: {amt}")
        if b.get("id"):
            lines.append(f"Booking: https://app.booksterhq.com/bookings/{b['id']}/view")

        # Title rules:
        #  IN on arrival: "IN: Name xN (CODE)"  (always include x1)
        #  MID on nights strictly between arrival and checkout: "Name (CODE)"
        #  OUT on checkout day: "OUT: Name (CODE)"
        in_title = f"IN: {guest}{(' x' + str(party)) if party else ' x1'} ({code})"
        out_title = f"OUT: {guest} ({code})"
        mid_title = f"{guest} ({code})"

        # IN day
        _single_day_event(
            cal,
            in_title,
            arrival,
            lines,
            f"redroofs-{b['id']}-IN-{arrival.isoformat()}",
        )

        # MID days (if any)
        cur = arrival + timedelta(days=1)
        while cur < checkout:
            _single_day_event(
                cal,
                mid_title,
                cur,
                lines,
                f"redroofs-{b['id']}-MID-{cur.isoformat()}",
            )
            cur += timedelta(days=1)

        # OUT day
        _single_day_event(
            cal,
            out_title,
            checkout,
            lines,
            f"redroofs-{b['id']}-OUT-{checkout.isoformat()}",
        )

    return cal.to_ical()


# ---------- GitHub Action entry ----------

import asyncio
import pathlib


async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """Generate per-property .ics files and an index.html. Shows debug if DEBUG_DUMP=1."""
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    debug_chunks: list[str] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for pid in property_ids:
            # 1) list
            if DEBUG_DUMP:
                items, attempts = await fetch_list_for_property(client, pid)
                for line in attempts:
                    debug_chunks.append(f"PID {pid}: {line}")
            else:
                items = await fetch_list_for_property(client, pid)

            # 2) enrich by id
            ids = [str(b.get("id")) for b in items if b.get("id")]
            details_by_id = await fetch_detail_for_ids(client, ids)

            # 3) map → internal
            mapped: list[dict] = []
            for base in items:
                det = details_by_id.get(str(base.get("id")), {})
                mb = map_booking(base, det)
                if mb:
                    mapped.append(mb)

            # Use first booking's entry_name as calendar title fallback
            cal_name = None
            for base in items:
                n = base.get("entry_name")
                if n:
                    cal_name = n
                    break

            ics_bytes = render_calendar(mapped, cal_name)
            (out / f"{pid}.ics").write_bytes(ics_bytes)
            written.append(str(out / f"{pid}.ics"))
            if DEBUG_DUMP:
                debug_chunks.append(f"PID {pid}: list_count={len(items)}; enriched={len(mapped)}")

    # Index
    html = ["<h1>Redroofs iCal Feeds</h1>", "<p>Feeds regenerate hourly.</p>"]
    for pid in property_ids:
        html.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
    if DEBUG_DUMP and debug_chunks:
        html.append("<hr><pre>")
        html.extend(debug_chunks)
        html.append("</pre>")
    (out / "index.html").write_text("\n".join(html), encoding="utf-8")

    return written
