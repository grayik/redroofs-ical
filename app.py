# app.py - minimal and line-break-safe

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(int(value)).date()
        except:
            return None
    try:
        return dtparse.parse(str(value)).date()
    except:
        return None


async def fetch_bookings_for_property(property_id):
    url = BOOKSTER_API_BASE.rstrip("/") + "/" + BOOKSTER_BOOKINGS_PATH.lstrip("/")
#    params = {"ei": property_id, "pp": 200, "st": "confirmed"}
    params = {"ei": property_id, "pp": 200}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        r = await client.get(url, params=params, auth=("x", BOOKSTER_API_KEY))
        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError("Authentication redirect - check API key and endpoint.")
        r.raise_for_status()
        payload = r.json()
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, list):
        return payload
    return []


def map_booking(b):
    state = str(b.get("state", "")).lower()
    if state in ("cancelled", "canceled", "void", "rejected", "tentative"):
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    name = (first + " " + last).strip() or "Guest"

    email = b.get("customer_email") or None
    mobile = b.get("customer_mobile") or b.get("customer_phone") or None

    try:
        party_total = int(b.get("party_size"))
    except:
        party_total = None

    def f(x):
        try:
            return float(x)
        except:
            return None
    value = f(b.get("value"))
    balance = f(b.get("balance"))
    paid = None
    if value is not None and balance is not None:
        paid = max(0.0, value - balance)

    extras = []
    lines = b.get("lines")
    if isinstance(lines, list):
        for ln in lines:
            if isinstance(ln, dict) and ln.get("type") == "extra":
                nm = ln.get("name") or "Extra"
                qty = ln.get("quantity") or ln.get("qty")
                if qty:
                    extras.append(nm + " x" + str(qty))
                else:
                    extras.append(nm)

    return {
        "arrival": arrival,
        "departure": departure,
        "name": name,
        "email": email,
        "mobile": mobile,
        "party": party_total,
        "extras": extras,
        "property_name": b.get("entry_name"),
        "property_id": b.get("entry_id"),
        "channel": b.get("syndicate_name"),
        "currency": (b.get("currency") or "").upper() or None,
        "paid": paid,
        "ref": b.get("id") or b.get("reference") or name,
    }


def render_calendar(bookings, property_name=None):
    cal = Calendar()
    cal.add("prodid", "-//Redroofs Bookster iCal//EN")
    cal.add("version", "2.0")
    if property_name:
        cal.add("X-WR-CALNAME", property_name + " - Guests")

    for b in bookings:
        data = map_booking(b)
        if not data:
            continue
        ev = Event()
        ev.add("summary", data["name"])
        ev.add("dtstart", data["arrival"])
        ev.add("dtend", data["departure"])
        uid = "redroofs-%s-%s" % (data["ref"], data["arrival"].isoformat())
        ev.add("uid", uid)
        desc = []
        if data["email"]:
            desc.append("Email: " + data["email"])
        if data["mobile"]:
            desc.append("Mobile: " + data["mobile"])
        if data["party"]:
            desc.append("Guests in party: " + str(data["party"]))
        if data["extras"]:
            desc.append("Extras: " + ", ".join(data["extras"]))
        if data["property_name"]:
            desc.append("Property: " + data["property_name"])
        if data["channel"]:
            desc.append("Channel: " + data["channel"])
        if data["paid"] is not None:
            amt = (data["currency"] + " " if data["currency"] else "") + ("%.2f" % data["paid"])
            desc.append("Amount paid to us: " + amt)
        ev.add("description", "\n".join(desc) if desc else "Guest booking")
        cal.add_component(ev)
    return cal.to_ical()


async def generate_and_write(property_ids, outdir="public"):
    import traceback
    os.makedirs(outdir, exist_ok=True)
    written = []
    try:
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)
            prop_name = None
            for b in bookings:
                if isinstance(b, dict) and b.get("entry_name"):
                    prop_name = b.get("entry_name")
                    break
            data = render_calendar(bookings, prop_name)
            p = os.path.join(outdir, pid + ".ics")
            with open(p, "wb") as f:
                f.write(data)
            written.append(p)

        lines = ["<h1>Redroofs iCal Feeds</h1>", "<p>Feeds regenerate hourly.</p>"]
        for pid in property_ids:
            lines.append("<p><a href='%s.ics'>%s.ics</a></p>" % (pid, pid))
        if DEBUG_DUMP:
            lines.append("<hr><pre>DEBUG_DUMP on</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return written

    except Exception as e:
        msg = "Error: " + str(e)
        placeholder = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Redroofs//EN\nEND:VCALENDAR\n"
        for pid in property_ids:
            with open(os.path.join(outdir, pid + ".ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + msg + "</pre>")
        return written
