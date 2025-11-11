# app.py — FastAPI (Option A: live on-demand) with split IN/MID/OUT all-day events
from __future__ import annotations

import os
import typing as t
from datetime import date, datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from icalendar import Calendar, Event
from dateutil import parser as dtparse
from dotenv import load_dotenv

load_dotenv()

# --- Config ------------------------------------------------------------------
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Your three property IDs -> short codes for title suffix
PROPERTY_CODES = {
    "158595": "BO",  # Barn Owl Cabin
    "158596": "RR",  # Redroofs by the Woods
    "158497": "BB",  # Bumblebee Cabin
}

# Optional small cache (seconds)
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # 15 minutes
_CACHE: dict[str, dict] = {}


# --- Helpers -----------------------------------------------------------------
def _to_date(value: t.Union[str, int, float, date, datetime, None]) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        # epoch seconds
        try:
            return datetime.utcfromtimestamp(int(value)).date()
        except Exception:
            return None
    try:
        return dtparse.parse(str(value)).date()
    except Exception:
        return None


def _cache_get(key: str) -> bytes | None:
    item = _CACHE.get(key)
    if not item:
        return None
    if (datetime.utcnow().timestamp() - item["ts"]) > CACHE_TTL_SECONDS:
        return None
    return item["bytes"]


def _cache_set(key: str, data: bytes) -> None:
    _CACHE[key] = {"bytes": data, "ts": datetime.utcnow().timestamp()}


# --- Bookster access ---------------------------------------------------------
async def fetch_bookings_for_property(property_id: str) -> list[dict]:
    """
    Fetch bookings for an entry (property) using Bookster's system API.
    Auth: HTTP Basic with username 'x' and password = API key.
    We prefer using `st=confirmed` but some tenants return 0 for that filter,
    so we try (1) with st, (2) without st and filter locally.
    """
    base = BOOKSTER_API_BASE.rstrip("/")
    path = BOOKSTER_BOOKINGS_PATH.lstrip("/")
    url = f"{base}/{path}"

    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        # Attempt 1: server-side filter by entry & confirmed
        params1 = {"ei": property_id, "pp": 200, "st": "confirmed"}
        r = await client.get(url, params=params1, auth=("x", BOOKSTER_API_KEY))
        if r.status_code in (301, 302, 303, 307, 308):
            raise HTTPException(status_code=502, detail="Bookster redirected: check base/path or credentials.")
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict) and payload["meta"].get("count"):
            items = payload.get("data") or payload.get("results") or []
            return items

        # Attempt 2: server-side filter only by entry, local filter by state
        params2 = {"ei": property_id, "pp": 200}
        r2 = await client.get(url, params=params2, auth=("x", BOOKSTER_API_KEY))
        r2.raise_for_status()
        payload2 = r2.json()
        if isinstance(payload2, dict):
            items2 = payload2.get("data") or payload2.get("results") or []
        elif isinstance(payload2, list):
            items2 = payload2
        else:
            items2 = []

        # Local filter for confirmed
        items2 = [b for b in items2 if str(b.get("state", "")).lower() == "confirmed"]
        return items2


def map_booking_to_core(b: dict) -> dict | None:
    """
    Normalize booking to the fields we need for calendar generation.
    """
    state = (b.get("state") or "").lower()
    if state in {"cancelled", "canceled", "void", "rejected", "tentative"}:
        return None

    arrival = _to_date(b.get("start_inclusive"))
    # Bookster's end_exclusive is the checkout date already; keep as-is
    checkout = _to_date(b.get("end_exclusive"))
    if not arrival or not checkout:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    guest_name = (f"{first} {last}".strip() or "Guest")

    email = b.get("customer_email") or None
    mobile = b.get("customer_tel_mobile") or b.get("customer_mobile") or b.get("customer_tel_day") or None

    # party_size may be string or int
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

    # extras — derive from lines[] of type "extra" if present
    extras_list: list[str] = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                name = ln.get("name") or ln.get("title") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras_list.append(f"{name} x{qty}" if qty else name)

    entry_id = str(b.get("entry_id") or "")
    entry_name = b.get("entry_name")
    code = PROPERTY_CODES.get(entry_id)
    if not code and isinstance(entry_name, str):
        lower = entry_name.lower()
        if "barn owl" in lower:
            code = "BO"
        elif "bumblebee" in lower:
            code = "BB"
        elif "redroofs" in lower:
            code = "RR"

    return {
        "arrival": arrival,
        "checkout": checkout,  # all-day checkout (own day)
        "guest_name": guest_name,
        "email": email,
        "mobile": mobile,
        "party_total": party_total,
        "extras": extras_list,
        "reference": str(b.get("id") or b.get("reference") or ""),
        "property_name": entry_name,
        "property_id": entry_id,
        "channel": b.get("syndicate_name"),
        "currency": currency,
        "paid": paid,
        "code": code or "",
    }


# --- Calendar rendering ------------------------------------------------------
def render_calendar(bookings: list[dict], property_name: str | None = None) -> bytes:
    """
    Generate an iCal where each booking is split into:
      - Arrival day (IN: ... xN (CODE))
      - Middle days (Guest (CODE)) (one per night between)
      - Checkout day (OUT: Guest (CODE))
    All are all-day events. We also embed booking details in description.
    """
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", f"{property_name} – Guests")

    for raw in bookings:
        m = map_booking_to_core(raw)
        if not m:
            continue

        # Unpack
        guest = m["guest_name"]
        arrival: date = m["arrival"]
        checkout: date = m["checkout"]  # this is already the day they leave (own all-day)
        code = m["code"]
        party = m["party_total"]

        # Build common description lines
        desc_lines: list[str] = []
        if m.get("email"):
            desc_lines.append(f"Email: {m['email']}")
        if m.get("mobile"):
            desc_lines.append(f"Mobile: {m['mobile']}")
        if m.get("party_total"):
            desc_lines.append(f"Guests in party: {m['party_total']}")
        if m.get("extras"):
            desc_lines.append("Extras: " + ", ".join(m["extras"]))
        if m.get("property_name"):
            desc_lines.append(f"Property: {m['property_name']}")
        if m.get("channel"):
            desc_lines.append(f"Channel: {m['channel']}")
        if m.get("paid") is not None:
            amt = f"{m['paid']:.2f}"
            if m.get("currency"):
                amt = f"{m['currency']} {amt}"
            desc_lines.append(f"Amount paid to us: {amt}")
        # Link to booking in Bookster (if we have an ID)
        if m.get("reference"):
            link = f"https://app.booksterhq.com/bookings/{m['reference']}/view"
            desc_lines.append(f"Booking: {link}")

        # 1) Arrival day event (IN: ...)
        title_in = f"IN: {guest}"
        if party:
            title_in += f" x{party}"
        if code:
            title_in += f" ({code})"
        ev_in = Event()
        ev_in.add("summary", title_in)
        ev_in.add("dtstart", arrival)
        ev_in.add("dtend", arrival + timedelta(days=1))  # all-day, non-inclusive next day
        ev_in.add("uid", f"redroofs-in-{m['reference']}-{arrival.isoformat()}")
        ev_in.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
        cal.add_component(ev_in)

        # 2) Middle days (Guest (CODE)) — from arrival+1 up to (checkout-1)
        current = arrival + timedelta(days=1)
        last_night = checkout - timedelta(days=1)
        while current <= last_night - timedelta(days=0):
            # Only create if there actually is a middle night
            if current < checkout and current > arrival:
                title_mid = f"{guest}"
                if code:
                    title_mid += f" ({code})"
                ev_mid = Event()
                ev_mid.add("summary", title_mid)
                ev_mid.add("dtstart", current)
                ev_mid.add("dtend", current + timedelta(days=1))
                ev_mid.add("uid", f"redroofs-mid-{m['reference']}-{current.isoformat()}")
                ev_mid.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
                cal.add_component(ev_mid)
            current += timedelta(days=1)

        # 3) Checkout day (OUT: ...)
        title_out = f"OUT: {guest}"
        if code:
            title_out += f" ({code})"
        ev_out = Event()
        ev_out.add("summary", title_out)
        ev_out.add("dtstart", checkout)
        ev_out.add("dtend", checkout + timedelta(days=1))
        ev_out.add("uid", f"redroofs-out-{m['reference']}-{checkout.isoformat()}")
        ev_out.add("description", "\n".join(desc_lines) if desc_lines else "Guest booking")
        cal.add_component(ev_out)

    return cal.to_ical()


# --- FastAPI app -------------------------------------------------------------
app = FastAPI(title="Redroofs Bookster → iCal (Option A live)")

@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}

@app.get("/calendar/{property_id}.ics")
async def calendar(property_id: str, property_name: str | None = None):
    """
    iCal feed for a single property.
    Subscribe this URL in Google/Apple/Outlook. We cache results briefly.
    """
    cache_key = f"prop:{property_id}:{property_name or ''}"
    cached = _cache_get(cache_key)
    if cached:
        return Response(content=cached, media_type="text/calendar", headers={"Cache-Control": "public, max-age=300"})

    bookings = await fetch_bookings_for_property(property_id)
    # If not provided, try to derive a name from first booking
    if not property_name:
        for b in bookings:
            if isinstance(b, dict) and b.get("entry_name"):
                property_name = b["entry_name"]
                break
    ics_bytes = render_calendar(bookings, property_name)
    _cache_set(cache_key, ics_bytes)

    return Response(
        content=ics_bytes,
        media_type="text/calendar",
        headers={
            "Content-Type": "text/calendar; charset=utf-8",
            "Cache-Control": "public, max-age=300",
        },
    )
