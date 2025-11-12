# app.py — Bookster -> iCal generator (GitHub Actions build)
# ----------------------------------------------------------
# The GitHub Action imports `generate_and_write()` from this file.
# It fetches bookings (list), enriches each one from the detail endpoint,
# and writes two sets of .ics per property:
#   1) Full: IN, MID, 0UT  -> BO-API.ics, RR-API.ics, BB-API.ics
#   2) IN/OUT only         -> BO-API-INOUT.ics, RR-API-INOUT.ics, BB-API-INOUT.ics

from __future__ import annotations

import os
import typing as t
from datetime import date, datetime, timedelta

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse

# ---------- configuration ----------
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
PID_TO_FILENAMES: dict[str, tuple[str, str]] = {
    "158595": ("BO-API.ics", "BO-API-INOUT.ics"),
    "158596": ("RR-API.ics", "RR-API-INOUT.ics"),
    "158497": ("BB-API.ics", "BB-API-INOUT.ics"),
}

# Extras we care about (case-insensitive matching on the "name" field)
EXTRA_ALLOWLIST = (
    "pets", "pet", "dog", "dogs", "cat", "cats",
    "high chair", "infant cot", "cot", "twin beds", "twin bed", "twins",
)

AIRBNB_LABELS = {"adult", "child", "infant", "baby"}  # labels that often appear as "names"

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
    Return the calendar days covered by the stay PLUS an explicit 0UT day.
    Bookster's end_exclusive is the checkout day; we add a 0UT event on that day.
    """
    days: list[date] = []
    cur = arrival
    while cur < departure:
        days.append(cur)
        cur += timedelta(days=1)
    days.append(departure)  # explicit 0UT day
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
    Get full details for a single booking (reliable value/balance/lines/phones/party).
    """
    path = BOOKSTER_DETAIL_PATH_TMPL.format(id=booking_id)
    async with httpx.AsyncClient() as client:
        payload = await _bookster_get(client, path)
        if isinstance(payload, dict):
            return payload
        return {}

# ---------- party parsing ----------

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

def _parse_party_list(party: t.Any, lead_full_name: str) -> list[str]:
    """
    Build lines like:
      Guest 1: Alice Smith
      Guest 2: Adult (age 34)
      Guest 3: Child
    Rules:
      - If a "name" is literally Adult/Child/Infant/Baby, treat that as the category (not a name).
      - Use 'type' only if it is one of those age categories; ignore 'standard'.
      - Include age iff age > 0.
      - We always include the lead guest in the list EXCEPT when they are the only guest with any info.
    """
    def norm(s: str) -> str:
        return (s or "").strip()

    def join_name(fn: str, ln: str) -> str:
        fn, ln = norm(fn), norm(ln)
        full = (fn + " " + ln).strip()
        return full

    # Prepare raw entries as tuples (name, category, age)
    raw: list[tuple[str, str | None, int | None]] = []

    if isinstance(party, list) and party:
        for person in party:
            if not isinstance(person, dict):
                continue
            fn = norm(person.get("forename", ""))
            ln = norm(person.get("surname", ""))
            tcat = norm(person.get("type", "")).lower()
            age = person.get("age", 0)
            try:
                age = int(age)
            except Exception:
                age = 0

            # If the "name" itself is one of the labels, treat as category instead
            fn_l = fn.lower()
            ln_l = ln.lower()
            name_is_label = fn_l in AIRBNB_LABELS or ln_l in AIRBNB_LABELS

            category: str | None = None
            if tcat in AIRBNB_LABELS:
                category = tcat.capitalize()

            if name_is_label:
                # Determine category from whichever part is a label
                if fn_l in AIRBNB_LABELS:
                    category = fn.capitalize()
                    fn = ""  # remove from name
                if ln_l in AIRBNB_LABELS:
                    category = (ln.capitalize() if not category else category)
                    ln = ""  # remove from name

            full_name = join_name(fn, ln)
            if not full_name and category:
                display = category  # e.g., "Adult"
            elif full_name and category:
                # Name exists + category known; spec doesn't require showing both,
                # but keeping just the name is tidier.
                display = full_name
            else:
                display = full_name or ""  # may still be empty

            raw.append((display, category, age if age and age > 0 else None))

    # If party list is empty/invalid, try to construct list with at least the lead
    if not raw:
        raw = [(lead_full_name, None, None)]

    # Ensure lead guest appears as Guest 1 (use lead_full_name when possible)
    # Try to locate an entry matching lead name; otherwise, insert it at front.
    lead_index = None
    for i, (nm, _cat, _age) in enumerate(raw):
        if nm and nm.lower() == lead_full_name.lower():
            lead_index = i
            break
    if lead_index is None:
        raw.insert(0, (lead_full_name, None, None))
    elif lead_index != 0:
        raw.insert(0, raw.pop(lead_index))

    # Decide whether to suppress the whole section if only lead has info
    meaningful_others = any(
        (nm or cat or age) and (nm.lower() != lead_full_name.lower())
        for (nm, cat, age) in raw
    )
    if not meaningful_others:
        return []  # skip the Party details section entirely

    # Build lines "Guest N: ..."
    lines: list[str] = []
    for idx, (nm, cat, age) in enumerate(raw, start=1):
        label = nm if nm else (cat or "Guest")
        if age:
            line = f"Guest {idx}: {label} (age {age})"
        else:
            line = f"Guest {idx}: {label}"
        lines.append(line)
    return lines

# ---------- mapping ----------

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

    # Keep the raw party array for rendering the section
    party_raw = b.get("party") if isinstance(b.get("party"), list) else []

    return {
        "id": b.get("id"),
        "guest": guest,
        "arrival": arrival,
        "departure": departure,  # end_exclusive (checkout)
        "email": email,
        "phone": phone,
        "party": party,
        "party_raw": party_raw,
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
    Use "0UT" (zero) to force it to sort before IN in most calendar clients.
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

def render_calendar(bookings: list[dict], calendar_name: str | None = None, include_mid: bool = True) -> bytes:
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

        # Build optional Party details section lines (excluding emails)
        party_section = _parse_party_list(mapped.get("party_raw") or [], mapped["guest"])

        # days[0] is IN, days[-1] is 0UT, any between are MID
        for i, day in enumerate(days):
            kind = "IN" if i == 0 else ("OUT" if i == len(days) - 1 else "MID")
            if not include_mid and kind == "MID":
                continue

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
            if party_section:
                desc.append("Party details")
                desc.extend(party_section)
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
    Generate two .ics per property:
      - Full stays (IN, MID, 0UT)     -> BO-API.ics / RR-API.ics / BB-API.ics
      - Check-in/out only (IN & 0UT)  -> BO-API-INOUT.ics / RR-API-INOUT.ics / BB-API-INOUT.ics
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

            # Determine filenames (default to <pid>.ics if not mapped)
            full_name, inout_name = PID_TO_FILENAMES.get(pid, (f"{pid}.ics", f"{pid}-INOUT.ics"))

            # 3a) Full calendar (IN/MID/0UT)
            ics_full = render_calendar(enriched, cal_name, include_mid=True)
            full_path = os.path.join(outdir, full_name)
            with open(full_path, "wb") as f:
                f.write(ics_full)
            written.append(full_path)

            # 3b) IN/OUT-only calendar
            ics_inout = render_calendar(enriched, cal_name, include_mid=False)
            inout_path = os.path.join(outdir, inout_name)
            with open(inout_path, "wb") as f:
                f.write(ics_inout)
            written.append(inout_path)

        # index
        html = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
            "<h2>Full stays (IN, MID, 0UT)</h2>",
        ]
        for pid in property_ids:
            full_name, _ = PID_TO_FILENAMES.get(pid, (f"{pid}.ics", f"{pid}-INOUT.ics"))
            html.append(f"<p><a href='{full_name}'>{full_name}</a></p>")
        html.append("<h2>IN/OUT only</h2>")
        for pid in property_ids:
            _, inout_name = PID_TO_FILENAMES.get(pid, (f"{pid}.ics", f"{pid}-INOUT.ics"))
            html.append(f"<p><a href='{inout_name}'>{inout_name}</a></p>")

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
            full_name, inout_name = PID_TO_FILENAMES.get(pid, (f"{pid}.ics", f"{pid}-INOUT.ics"))
            for name in (full_name, inout_name):
                with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
                    f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write(f"<h1>Redroofs iCal Feeds</h1>\n<pre>{e}</pre>")
        return written
