# Minimal Bookster -> iCal generator for GitHub Pages builds
# The workflow runs:  from app import generate_and_write

import os
import typing as t
from datetime import date, datetime

import httpx
from icalendar import Calendar, Event
from dateutil import parser as dtparse


# ================= Configuration =================
# Per Bookster docs: https://api.booksterhq.com/system/api/v1/booking/bookings.json
BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://api.booksterhq.com/system/api/v1")
BOOKSTER_BOOKINGS_PATH = os.getenv("BOOKSTER_BOOKINGS_PATH", "booking/bookings.json")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# Turn on to show debug info on index.html (never commit secrets)
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
async def _fetch_page(
    client: httpx.AsyncClient,
    params: dict,
) -> dict:
    """Fetch a single page and return parsed JSON (dict). Raises for HTTP errors."""
    url = f"{BOOKSTER_API_BASE.rstrip('/')}/{BOOKSTER_BOOKINGS_PATH.lstrip('/')}"
    r = await client.get(
        url,
        params=params,
        auth=("x", BOOKSTER_API_KEY),  # Basic auth: username 'x', password = API key
        headers={"Accept": "application/json"},
        follow_redirects=False,
        timeout=60,
    )
    if r.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(
            f"Unexpected redirect ({r.status_code}) from Bookster. "
            f"Check BOOKSTER_API_BASE/BOOKSTER_BOOKINGS_PATH and credentials."
        )
    r.raise_for_status()
    return r.json()


async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> t.List[dict]:
    """
    Fetch bookings for one entry/property.
    Strategy:
      1) Try server-side filter by entry (ei=<id>), no state filter (pull all).
      2) If the API ignores 'ei' or returns no 'meta', still collect what we can.
      3) Paginate via meta.total_pages when available.
      4) Client-side filter to confirmed state only.
    """
    items: t.List[dict] = []
    debug_lines: t.List[str] = []
    base_params = {"ei": str(property_id), "pp": 100}  # page size 100

    async with httpx.AsyncClient() as client:
        # First attempt: with server-side 'ei'
        p = 1
        while True:
            params = dict(base_params)
            params["p"] = p
            payload = await _fetch_page(client, params)
            meta = payload.get("meta") if isinstance(payload, dict) else None
            data = []
            if isinstance(payload, dict):
                if isinstance(payload.get("data"), list):
                    data = payload["data"]
                elif isinstance(payload.get("results"), list):
                    data = payload["results"]
                elif isinstance(payload.get("bookings"), list):
                    data = payload["bookings"]

            if DEBUG_DUMP:
                debug_lines.append(
                    f"Attempt 1 (server filter) page={p} params={params} "
                    f"meta={meta!r} batch_count={len(data)}"
                )

            items.extend(data or [])
            if not meta:
                # If no meta, break after first page (API variant without paging)
                break
            total_pages = meta.get("total_pages") or 1
            if p >= int(total_pages):
                break
            p += 1

        # Fallback: If nothing came back, try without 'ei' and filter client-side.
        if not items:
            p = 1
            while True:
                params = {"pp": 100, "p": p}  # no server-side filters
                payload = await _fetch_page(client, params)
                meta = payload.get("meta") if isinstance(payload, dict) else None
                data = []
                if isinstance(payload, dict):
                    if isinstance(payload.get("data"), list):
                        data = payload["data"]
                    elif isinstance(payload.get("results"), list):
                        data = payload["results"]
                    elif isinstance(payload.get("bookings"), list):
                        data = payload["bookings"]

                filtered = [
                    i for i in (data or [])
                    if str(i.get("entry_id")) == str(property_id)
                ]

                if DEBUG_DUMP:
                    vis_total = len(data or [])
                    debug_lines.append(
                        f"Attempt 2 (no server filter) page={p} params={params} "
                        f"meta={meta!r} visible_total={vis_total} filtered_for_pid={len(filtered)}"
                    )

                items.extend(filtered)
                if not meta:
                    break
                total_pages = meta.get("total_pages") or 1
                if p >= int(total_pages):
                    break
                p += 1

    # Client-side state filter: only confirmed
    confirmed = [i for i in items if str(i.get("state", "")).lower() == "confirmed"]

    # Attach debug to environment (picked up in index builder)
    os.environ["APP_DEBUG_LINES"] = "\n".join(debug_lines)

    return confirmed


# ================= Mapping =================
def map_booking_to_event_data(b: dict) -> t.Optional[dict]:
    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (first + " " + last).strip() or "Guest"

    email = b.get("customer_email") or None
    mobile = b.get("customer_mobile") or b.get("customer_phone") or None

    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else None
    except Exception:
        party_total = None

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

    # extras from lines[] of type "extra" if present
    extras_list: t.List[str] = []
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
        ev.add("summary", mapped["guest_name"])  # title
        ev.add("dtstart", mapped["arrival"])     # all-day
        ev.add("dtend", mapped["departure"])     # checkout
        uid = f"redroofs-{(mapped.get('reference') or mapped['guest_name'])}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)

        lines: t.List[str] = []
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
    If an error occurs, write placeholder feeds and show the error on index.html.
    """
    os.makedirs(outdir, exist_ok=True)
    written: t.List[str] = []
    try:
        # Write one .ics per property
        all_debug: t.List[str] = []
        for pid in property_ids:
            bookings = await fetch_bookings_for_property(pid)
            # collect debug lines from env (attached by fetch function)
            dbg = os.environ.get("APP_DEBUG_LINES")
            if dbg:
                all_debug.append(f"PID {pid}:\n{dbg}")

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

        # Build index.html
        html_lines = [
            "<h1>Redroofs iCal Feeds</h1>",
            "<p>Feeds regenerate hourly.</p>",
        ]
        for pid in property_ids:
            html_lines.append(f"<p><a href='{pid}.ics'>{pid}.ics</a></p>")
        if DEBUG_DUMP and all_debug:
            html_lines.append("<hr><pre>Debug\n\n" + "\n\n".join(all_debug) + "</pre>")
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(html_lines))
        return written

    except Exception as e:
        err_text = "Error generating feeds: " + str(e)
        placeholder = "\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Redroofs Bookster iCal//EN",
                "END:VCALENDAR",
                "",
            ]
        )
        for pid in property_ids:
            with open(os.path.join(outdir, f"{pid}.ics"), "w", encoding="utf-8") as f:
                f.write(placeholder)
        with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Redroofs iCal Feeds</h1>\n<pre>" + err_text + "</pre>")
        return written
