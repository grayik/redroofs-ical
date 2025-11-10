# app.py - minimal Bookster -> iCal generator for GitHub Pages builds
import os
import json
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"

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

async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> t.List[dict]:
    url = "%s/%s" % (BOOKSTER_API_BASE.rstrip("/"), BOOKSTER_BOOKINGS_PATH.lstrip("/"))
    params = {"ei": property_id, "pp": 200, "st": "confirmed"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        r.raise_for_status()
        payload = r.json()

    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, list):
        return payload
    return []

def _map(b: dict) -> t.Optional[dict]:
    state = (b.get("state") or "").lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative"):
        return None
    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None
    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    name = (first + " " + last).strip() or "Guest"
    # money
    try:
        value = float(b.get("value")) if b.get("value") is not None else None
    except Exception:
        value = None
    try:
        balance = float(b.get("balance")) if b.get("balance") is not None else None
    except Exception:
        balance = None
    paid = (value - balance) if (value is not None and balance is not None) else None
    # extras from lines
    extras = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                nm = ln.get("name") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                extras.append(("%s x%s" % (nm, qty)) if qty else nm)
    # party size
    party = None
    ps = b.get("party_size")
    try:
        if ps is not None and str(ps).strip() != "":
            party = int(ps)
    except Exception:
        party = None

    return {
        "arrival": arrival,
        "departure": departure,
        "guest_name": name,
        "email": b.get("customer_email") or None,
        "mobile": b.get("customer_mobile") or b.get("customer_phone") or None,
        "party_total": party,
        "extras": extras,
        "reference": b.get("id") or b.get("reference"),
        "property_name": b.get("entry_name"),
        "channel": b.get("syndicate_name"),
        "currency": (b.get("currency") or "").upper() or None,
        "paid": paid,
    }

def render_calendar(bookings: t.List[dict], property_name: t.Optional[str] = None) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", "%s - Guests" % property_name)
    for raw in bookings:
        m = _map(raw)
        if not m:
            continue
        ev = Event()
        ev.add("summary", m["guest_name"])
        ev.add("dtstart", m["arrival"])
        ev.add("dtend", m["departure"])
        uid = "redroofs-%s-%s" % ((m.get("reference") or m["guest_name"]), m["arrival"].isoformat())
        ev.add("uid", uid)
        lines = []
        if m.get("email"):
            lines.append("Email: %s" % m["email"])
        if m.get("mobile"):
            lines.append("Mobile: %s" % m["mobile"])
        if m.get("party_total"):
            lines.append("Guests in party: %s" % m["party_total"])
        if m.get("extras"):
            lines.append("Extras: " + ", ".join(m["extras"]))
        if m.get("property_name"):
            lines.append("Property: %s" % m["property_name"])
        if m.get("channel"):
            lines.append("Channel: %s" % m["channel"])
        if m.get("paid") is not None:
            amt = "%.2f" % m["paid"]
            if m.get("currency"):
                amt = "%s %s" % (m["currency"], amt)
            lines.append("Amount paid to us: %s" % amt)
        ev.add("description", "\n".join(lines) if lines else "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()

async def generate_and_write(property_ids: t.List[str], outdir: str = "public") -> t.List[str]:
    # Generate feeds and index; on error, write placeholders and error text.
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    error_texts: t.List[str] = []
    for pid in property_ids:
        try:
            bookings = await fetch_bookings_for_property(pid)
            # optional debug dumps
            if DEBUG_DUMP:
                with open(os.path.join(outdir, "debug-%s.json" % pid), "w", encoding="utf-8") as f:
                    json.dump(bookings[:3], f, indent=2)
            prop_name = None
            for b in bookings:
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break
            ics_bytes = render_calendar(bookings, prop_name)
            path = os.path.join(outdir, "%s.ics" % pid)
            with open(path, "wb") as f:
                f.write(ics_bytes)
            written.append(path)
        except Exception as e:
            error_texts.append("Property %s: %s" % (pid, str(e)))
            placeholder = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Redroofs//EN\nEND:VCALENDAR\n"
            with open(os.path.join(outdir, "%s.ics" % pid), "w", encoding="utf-8") as f:
                f.write(placeholder)
    # index page
    lines = ["<h1>Redroofs iCal Feeds</h1>", "<p>Feeds regenerate hourly.</p>"]
    for pid in property_ids:
        lines.append("<p><a href='%s.ics'>%s.ics</a></p>" % (pid, pid))
    if DEBUG_DUMP:
        lines.append("<hr><p>Debug JSON (first few bookings):</p>")
        for pid in property_ids:
            if os.path.exists(os.path.join(outdir, "debug-%s.json" % pid)):
                lines.append("<p><a href='debug-%s.json'>debug-%s.json</a></p>" % (pid, pid))
    if error_texts:
        lines.append("<hr><pre>%s</pre>" % ("\n".join(error_texts)))
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return written
