# app.py — Bookster -> iCal generator (GitHub Actions build)
# ----------------------------------------------------------
# The GitHub Action imports `generate_and_write()` from this file.
# It fetches bookings (list), enriches each one from the detail endpoint,
# and writes one .ics per property with all-day events split into IN/MID/OUT.

from __future__ import annotations

import os
import typing as t
from datetime import date, datetime, timedelta

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------- configuration ----------
# You observed api.booksterhq.com gave 403 for browsing, while app.booksterhq.com returned data.
BOOKSTER_API_BASE = os.getenv(
    "BOOKSTER_API_BASE",
    "https://app.booksterhq.com/system/api/v1",
).rstrip("/")
BOOKSTER_LIST_PATH = os.getenv(
    "BOOKSTER_BOOKINGS_PATH",
    "booking/bookings.json",
).lstrip("/")
BOOKSTER_DETAIL_PATH_TMPL = "booking/bookings/{id}.json"
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Toggle basic debug notes on index.html
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

# Property codes for suffix in titles
PROPERTY_CODES: dict[str, str] = {
    "158596": "RR",  # Redroofs by the Woods
    "158595": "BO",  # Barn Owl Cabin
    "158497": "BB",  # Bumblebee Cabin
}

# Extras we care about (case-insensitive matching on the "name" field)
EXTRA_ALLOWLIST = (
    "pets", "pet", "dog", "dogs", "cat", "cats",
    "high chair", "infant cot", "cot", "twin beds", "twin bed", "twins"
)

# ---------- helpers ----------

def _to_date(v: t.Union[str, int, float, date, datetime, None]) -> date | None:
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

def _best_phone(b: dict) -> str | None:
    """
    Prefer mobile, but fall back to day/evening or other fields seen in feeds.
    """
    for key in (
        "customer_tel_mobile",
        "customer_tel_day",
        "customer_tel_evening",
        "customer_mobile",
        "customer_phone",
    ):
        val = (b.get(key) or "").strip()
        if val:
            return val
    return None

def _party_size(b: dict) -> int | None:
    v = b.get("party_size")
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(v)
    except Exception:
        return None

def _amount_paid(value: t.Any, balance: t.Any) -> float | None:
    try:
        v = float(value)
        bal = float(balance)
        paid = v - bal
        if abs(paid) < 1e-9:
            paid = 0.0
        return max(0.0, paid)
    except Exception:
        return None

def _prop_code(entry_id: t.Any, entry_name: str | None) -> str:
    code = PROPERTY_CODES.get(str(entry_id or ""), "")
    if code:
        return code
    name = (entry_name or "").lower()
    if "redroofs by the woods" in name or "redroofs" in name:
        return "RR"
    if "barn owl" in name:
        return "BO"
    if "bumblebee" in name:
        return "BB"
    return "RR"  # sensible default

def _expand_stay_days(arrival: date, departure: date) -> list[date]:
    """
    Return the calendar days covered by the stay PLUS an explicit OUT day.
    Bookster's end_exclusive is the checkout day; we add an OUT event on that day.
    """
    days: list[date] = []
    cur = arrival
    while cur < departure:
        days.append(cur)
        cur += timedelta(days=1)
    days.append(departure)  # explicit OUT day
    return days

# ---------- API calls ----------

async def _bookster_get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | list:
    url = f"{BOOKSTER_API_BASE}/{path.lstrip('/')}"
    r = await client.get(
        url,
        params=params or {},
        auth=("x", BOOKSTER_API_KEY),  # Basic auth per Bookster docs
        follow_redirects=False,
        timeout=60,
    )
    if r.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(f"Unexpected redirect from Bookster ({r.status_code}) for {url}")
    r.raise_for_status()
    return r.json()

async def fetch_list_for_property(property_id: str) -> list[dict]:
    """
    Fetch a booking list for an entry (property). We DO NOT pass st=confirmed
    because you saw zero results in that scenario. We client-filter instead.
    """
    async with httpx.AsyncClient() as client:
        payload = await _bookster_get(client, BOOKSTER_LIST_PATH, {"ei": property_id, "pp": 200})
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        return [b for b in items if (b.get("state") or "").lower() == "confirmed"]

async def fetch_detail(booking_id: t.Union[str, int]) -> dict:
    """
    Get full details for a single booking (reliable value/balance/lines/phones).
    """
    path = BOOKSTER_DETAIL_PATH_TMPL.format(id=booking_id)
    async with httpx.AsyncClient() as client:
        payload = await _bookster_get(client, path)
        if isinstance(payload, dict):
            return payload
        return {}

# ---------- mapping ----------

def _filter_extras(lines: t.Any) -> list[str]:
    """
    Keep only Pets / High Chair / Infant Cot / Twin Beds (case-insensitive).
    If qty present and truthy, append ' x{qty}'.
    """
    picked: list[str] = []
    if isinstance(lines, list):
        for ln in lines:
            if not (isinstance(ln, dict) and (ln.get("type") == "extra")):
                continue
            name = (ln.get("name") or ln.get("title") or "").strip()
            if not name:
                continue
            lname = name.lower()
            if any(key in lname for key in EXTRA_ALLOWLIST):
                qty = ln.get("quantity") or ln.get("qty")
                picked.append(f"{name} x{qty}" if qty else name)
    return picked

def map_booking_to_event_data(b: dict) -> dict | None:
    state = (b.get("state") or "").lower()
    if state != "confirmed":
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    guest = (f"{first} {last}".strip()) or "Guest"

    email = (b.get("customer_email") or "").strip() or None
    phone = _best_phone(b)
    party = _party_size(b)

    extras = _filter_extras(b.get("lines"))

    value = b.get("value")
    balance = b.get("balance")
    paid = _amount_paid(value, balance)

    entry_id = b.get("entry_id")
    entry_name = b.get("entry_name")
    channel = b.get("syndicate_name")
    currency = (b.get("currency") or "").upper() or None

    return {
        "id": b.get("id"),
        "guest": guest,
        "arrival": arrival,
        "departure": departure,  # end_exclusive (checkout)
        "email": email,
        "phone": phone,
        "party": party,
        "extras": extras,
        "value": value,
        "balance": balance,
        "paid": paid,
        "currency": currency,
        "entry_id": entry_id,
        "entry_name": entry_name,
        "channel": channel,
    }

# ---------- iCal rendering ----------

def _title_for_day(kind: str, guest: str, party: int | None, code: str) -> str:
    """
    kind: "IN", "MID", "OUT"
    Only show xN on IN (and include x1).
    Always append property code in parentheses.
    """
    if kind == "IN":
        suffix = f" x{party}" if party is not None else ""
        return f"IN: {guest}{suffix} ({code})"
    if kind == "OUT":
        return f"0UT: {guest} ({code})"
    return f"{guest} ({code})"  # MID

def _booking_url(booking_id: t.Union[str, int, None]) -> str | None:
    if not booking_id:
        return None
    return f"https://app.booksterhq.com/bookings/{booking_id}/view"

def _add_event(cal: Calendar, when: date, title: str, desc_lines: list[str], uid: str) -> None:
    ev = Event()
    ev.add("summary", title)
    ev.add("dtstart", when)  # VALUE=DATE (all-day)
    ev.add("dtend", when + timedelta(days=1))  # next day (non-inclusive end)
    ev.add("uid", uid)
    ev.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
    cal.add_component(ev)

def render_calendar(bookings: list[dict], calendar_name: str | None = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if calendar_name:
        cal.add("X-WR-CALNAME", f"{calendar_name} – Guests")

    for b in bookings:
        mapped = map_booking_to_event_data(b)
        if not mapped:
            continue

        code = _prop_code(mapped["entry_id"], mapped["entry_name"])
        days = _expand_stay_days(mapped["arrival"], mapped["departure"])

        # days[0] is IN, days[-1] is OUT, any between are MID
        for i, day in enumerate(days):
            kind = "IN" if i == 0 else ("OUT" if i == len(days) - 1 else "MID")
            title = _title_for_day(kind, mapped["guest"], mapped["party"], code)

            desc: list[str] = []
            if mapped["email"]:
                desc.append(f"Email: {mapped['email']}")
            if mapped["phone"]:
                desc.append(f"Mobile: {mapped['phone']}")
            if mapped["party"] is not None:
                desc.append(f"Guests in party: {mapped['party']}")
            if mapped["extras"]:
                desc.append("Extras: " + ", ".join(mapped["extras"]))
            if mapped["entry_name"]:
                desc.append(f"Property: {mapped['entry_name']}")
            if mapped["channel"]:
                desc.append(f"Channel: {mapped['channel']}")
            if mapped["paid"] is not None:
                amt = f"{mapped['paid']:.2f}"
                if mapped["currency"]:
                    amt = f"{mapped['currency']} {amt}"
                desc.append(f"Amount paid to us: {amt}")
            link = _booking_url(mapped["id"])
            if link:
                desc.append(f"Booking: {link}")

            uid = f"redroofs-{mapped['id']}-{kind}-{day.isoformat()}"
            _add_event(cal, day, title, desc, uid)

    return cal.to_ical()

# ---------- GitHub Action entry ----------

async def generate_and_write(property_ids: list[str], outdir: str = "public") -> list[str]:
    """
    Generate one .ics per property. Steps:
      1) List bookings by ei (entry_id), no server-side state filter.
      2) Client-filter to confirmed.
      3) Enrich each booking via detail endpoint (reliable value/balance/lines/phones).
      4) Render to calendar, write <pid>.ics and an index.html.
    """
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    debug_lines: list[str] = []

    try:
        for pid in property_ids:
            # 1) base list
            base = await fetch_list_for_property(pid)
            debug_lines.append(f"PID {pid}: base_list={len(base)}")

            # 2) enrich each booking
            enriched: list[dict] = []
            for item in base:
                bid = item.get("id")
                if not bid:
                    continue
                detail = await fetch_detail(bid)
                merged = {**item, **detail}  # detail overrides list item
                enriched.append(merged)
            debug_lines.append(f"PID {pid}: enriched={len(enriched)}")

            # Sort by arrival to keep calendars tidy
            enriched.sort(key=lambda x: (_to_date(x.get("start_inclusive")) or date.min))

            # infer calendar name
            cal_name = None
            for b in enriched:
                if b.get("entry_name"):
                    cal_name = b["entry_name"]
                    break
            cal_name = cal_name or pid

            ics_bytes = render_calendar(enriched, cal_name)
            path = os.path.join(outdir, f"{pid}.ics")
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)

        # index
        html = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and debug_lines:
            html.append("<hr><pre>")
            html.extend(debug_lines)
            html.append("</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html))

        return written

    except Exception as e:
        # Failsafe: write placeholder feeds + the error to the index
        placeholder = "\n".join(
            ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Redroofs Bookster iCal//EN", "END:VCALENDAR", ""]
        )
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(f"<h1>Redroofs iCal Feeds</h1>\n<pre>{e}</pre>")
        return written
