import os
import typing as t
from datetime import date, datetime
import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/api")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

def _auth_headers():
    return {"X-API-Key": BOOKSTER_API_KEY} if BOOKSTER_API_KEY else {}

def _to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.utcfromtimestamp(int(v)).date()
    except:
        pass
    try:
        return dtparse.parse(str(v)).date()
    except:
        return None

async def fetch_bookings(property_id: str) -> list[dict]:
    url = f"{BOOKSTER_API_BASE}/bookings"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"property_id": property_id}, headers=_auth_headers())
        r.raise_for_status()
        data = r.json()
    return data.get("data", data) if isinstance(data, dict) else data

def map_booking(b: dict):
    state = (b.get("state") or "").lower()
    if state in {"cancelled","canceled","void","rejected","tentative"}:
        return None
    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None
    name = f"{b.get('customer_forename','')} {b.get('customer_surname','')}".strip() or "Guest"
    email = b.get("customer_email") or None
    party = b.get("party_size") or None
    value = float(b.get("value") or 0)
    balance = float(b.get("balance") or 0)
    paid = max(0, value - balance)
    currency = b.get("currency","GBP")
    prop_name = b.get("entry_name")
    channel = b.get("syndicate_name")
    return {
        "arrival": arrival,
        "departure": departure,
        "name": name,
        "email": email,
        "party": party,
        "paid": paid,
        "currency": currency,
        "property": prop_name,
        "channel": channel,
        "id": b.get("id")
    }

def render_calendar(bookings, cal_name=None):
    cal = Calendar()
    cal.add("prodid", "-//Redroofs iCal//EN")
    cal.add("version", "2.0")
    if cal_name:
        cal.add("X-WR-CALNAME", cal_name)
    for raw in bookings:
        m = map_booking(raw)
        if not m:
            continue
        ev = Event()
        ev.add("summary", m["name"])
        ev.add("dtstart", m["arrival"])
        ev.add("dtend", m["departure"])
        desc = []
        if m["email"]: desc.append(f"Email: {m['email']}")
        if m["party"]: desc.append(f"Guests: {m['party']}")
        desc.append(f"Property: {m['property']}")
        desc.append(f"Channel: {m['channel']}")
        desc.append(f"Amount paid to us: {m['currency']} {m['paid']:.2f}")
        ev.add("description", "\n".join(desc))
        cal.add_component(ev)
    return cal.to_ical()

async def generate_and_write(property_ids, outdir):
    import asyncio
    import os
    os.makedirs(outdir, exist_ok=True)
    for pid in property_ids:
        bookings = await fetch_bookings(pid)
        name = pid
        ics = render_calendar(bookings, cal_name=name)
        with open(os.path.join(outdir, f"{pid}.ics"), "wb") as f:
            f.write(ics)
