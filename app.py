"""
Redroofs — Bookster ➜ iCal feed generator (per property)
--------------------------------------------------------
A tiny FastAPI app that exposes an iCal feed for *each* property.
Each event spans from arrival (DTSTART) to checkout (DTEND, non-inclusive)
with the *guest name* as the event title and key details in the description.

How to run
----------
1) `pip install fastapi uvicorn httpx icalendar python-dateutil python-dotenv`
2) Create a `.env` file with:
   BOOKSTER_API_BASE=https://app.booksterhq.com/api
   BOOKSTER_API_KEY=your_api_key_here
3) `uvicorn app:app --host 0.0.0.0 --port 8080`
4) Subscribe your calendar app to: `https://YOUR_HOST/calendar/{property_id}.ics`

Notes
-----
- Bookster’s detailed API docs are available once your user is flagged as a developer.
  The example below shows the *shape* and best practices. Update the endpoint paths
  and field names to match your account’s API version.
- DTEND is set to the checkout date (all‑day, non-inclusive), which is the right
  way to show multi‑day stays in iCal/CalDAV.
- We mark events as all‑day by supplying `datetime.date` values.
- Add authentication headers exactly as your Bookster API expects (Bearer/API key/etc.).
"""

from __future__ import annotations

import os
import typing as t
from datetime import date, datetime

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from icalendar import Calendar, Event
from dateutil import parser as dtparse
from dotenv import load_dotenv

load_dotenv()

BOOKSTER_API_BASE = os.getenv("BOOKSTER_API_BASE", "https://app.booksterhq.com/api")
BOOKSTER_API_KEY = os.getenv("BOOKSTER_API_KEY", "")

# ---- Helpers ---------------------------------------------------------------

def _auth_headers() -> dict:
    """Return auth headers for Bookster API.
    Adjust to whatever your tenant requires (e.g., Bearer token, X-API-KEY, etc.).
    """
    if not BOOKSTER_API_KEY:
        return {}
    # Common patterns — uncomment the one that applies to your account, or customise
    # return {"Authorization": f"Bearer {BOOKSTER_API_KEY}"}
    return {"X-API-Key": BOOKSTER_API_KEY}


def _to_date(value: t.Union[str, int, float, date, datetime, None]) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    # Handle epoch seconds (as in Bookster sample: 1762992000)
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(int(value)).date()
        except Exception:
            return None
    try:
        return dtparse.parse(str(value)).date()
    except Exception:
        return None


# ---- Bookster API access ---------------------------------------------------

async def fetch_bookings_for_property(property_id: t.Union[int, str]) -> list[dict]:
    """Fetch bookings for a given property.

    This version assumes the API returns a payload shaped like:
    {"meta": {...}, "data": [ ...booking objects... ]}
    and uses `entry_id` on each item to denote the property/entry.
    Filter server-side by property_id if your API supports it; otherwise we
    filter client-side as a fallback.
    """
    url = f"{BOOKSTER_API_BASE}/bookings"
    params = {
        "property_id": property_id,  # keep if your API supports it; harmless otherwise
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params, headers=_auth_headers())
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Bookster API error: {r.text}")
        payload = r.json()

    # Normalise list
    if isinstance(payload, dict) and "data" in payload:
        items = payload.get("data", [])
    elif isinstance(payload, dict) and "results" in payload:
        items = payload.get("results", [])
    else:
        items = payload if isinstance(payload, list) else []

    # If server didn't filter by property, do it here when possible
    if items and any("entry_id" in i for i in items):
        items = [i for i in items if str(i.get("entry_id")) == str(property_id) or not property_id]

    return items


def map_booking_to_event_data(b: dict) -> dict | None:
    """Map Bookster booking (per provided sample) to calendar fields.

    Expected keys from sample JSON:
      - state: "confirmed" | "rejected" | "cancelled" ...
      - customer_forename, customer_surname, customer_email
      - start_inclusive (epoch seconds at 00:00), end_exclusive (epoch seconds at 00:00)
      - party_size (string number)
      - entry_name (property name), entry_id (property id), syndicate_name (channel)
      - id (booking id)
      - value (total), balance (remaining), currency
    """
    state = (b.get("state") or "").lower()
    if state in {"cancelled", "canceled", "void", "rejected", "tentative"}:
        return None

    arrival = _to_date(b.get("start_inclusive"))
    departure = _to_date(b.get("end_exclusive"))
    if not arrival or not departure:
        return None

    first = (b.get("customer_forename") or "").strip()
    last = (b.get("customer_surname") or "").strip()
    display_name = (f"{first} {last}" if (first or last) else "Guest").strip()

    email = b.get("customer_email") or None
    mobile = b.get("customer_mobile") or b.get("customer_phone") or None

    # party_size may be a string
    party_val = b.get("party_size")
    try:
        party_total = int(party_val) if party_val is not None and str(party_val).strip() != "" else None
    except Exception:
        party_total = None

    # Money fields
    def _to_decimal(x):
        try:
            return float(x)
        except Exception:
            return None
    value = _to_decimal(b.get("value"))
    balance = _to_decimal(b.get("balance"))
    currency = (b.get("currency") or "").upper() or None
    paid = None
    if value is not None and balance is not None:
        paid = max(0.0, value - balance)

    # Extras not present in sample; look for a few likely keys
    extras_raw = b.get("extras") or b.get("add_ons") or []
    extras_list: list[str] = []
    if isinstance(extras_raw, list):
        for x in extras_raw:
            if isinstance(x, str):
                extras_list.append(x)
            elif isinstance(x, dict):
                name = x.get("name") or x.get("title") or x.get("code")
                qty = x.get("quantity") or x.get("qty")
                extras_list.append(f"{name} x{qty}" if (name and qty) else (name or "Extra"))

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


# ---- iCal generation -------------------------------------------------------

def render_calendar(bookings: list[dict], property_name: str | None = None) -> bytes:
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
        ev.add("summary", mapped["guest_name"])  # Title = guest name

        # All‑day multi‑day event
        ev.add("dtstart", mapped["arrival"])   # VALUE=DATE implied for date objects
        ev.add("dtend", mapped["departure"])   # non‑inclusive checkout

        # Optional: unique ID is helpful for calendar clients
        uid = f"redroofs-{mapped.get('reference') or mapped['guest_name']}-{mapped['arrival'].isoformat()}"
        ev.add("uid", uid)

        # Description block        lines = []
        # Primary guest details
        if mapped.get("email"):
            lines.append(f"Email: {mapped['email']}")
        if mapped.get("mobile"):
            lines.append(f"Mobile: {mapped['mobile']}")
        if mapped.get("party_total"):
            lines.append(f"Guests in party: {mapped['party_total']}")
        if mapped.get("extras"):
            lines.append("Extras: " + ", ".join(mapped["extras"]))
        # Property + channel
        if mapped.get("property_name"):
            lines.append(f"Property: {mapped['property_name']}")
        if mapped.get("channel"):
            lines.append(f"Channel: {mapped['channel']}")
        # Amount paid to us
        if mapped.get("paid") is not None:
            amt = f"{mapped['paid']:.2f}"
            if mapped.get("currency"):
                amt = f"{mapped['currency']} {amt}"
            lines.append(f"Amount paid to us: {amt}")
        ev.add("description", "
".join(lines) or "Guest booking") "\n".join(lines) or "Guest booking")

        cal.add_component(ev)

    return cal.to_ical()


# ---- FastAPI app -----------------------------------------------------------

app = FastAPI(title="Redroofs Bookster → iCal")

# Simple in-memory cache so calendar clients don't hammer Bookster
_CACHE: dict[str, dict] = {}
_CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # 15 minutes default

def _cache_get(key: str) -> bytes | None:
    item = _CACHE.get(key)
    if not item:
        return None
    if (datetime.utcnow().timestamp() - item["ts"]) > _CACHE_TTL_SECONDS:
        return None
    return item["bytes"]

def _cache_set(key: str, data: bytes) -> None:
    _CACHE[key] = {"bytes": data, "ts": datetime.utcnow().timestamp()}


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/calendar/{property_id}.ics")
async def calendar(property_id: str, property_name: str | None = None):
    """Return an iCal feed for a single property.

    Subscribe in Google/Apple/Outlook with the full URL to this endpoint.
    Optionally pass `?property_name=Cabin+No.1` to name the calendar.
    """
    cache_key = f"prop:{property_id}:{property_name or ''}"
    cached = _cache_get(cache_key)
    if cached:
        return Response(content=cached, media_type="text/calendar", headers={"Cache-Control": "public, max-age=300"})

    bookings = await fetch_bookings_for_property(property_id)
    ics_bytes = render_calendar(bookings, property_name)
    _cache_set(cache_key, ics_bytes)

    headers = {
        "Content-Type": "text/calendar; charset=utf-8",
        "Cache-Control": "public, max-age=300",  # 5 minutes
    }
    return Response(content=ics_bytes, media_type="text/calendar", headers=headers)


# ---- Optional: combine multiple properties into one feed -------------------

@app.post("/calendar/batch.ics")
async def calendar_batch(property_ids: list[str], calendar_name: str | None = "Redroofs – All Guests"):
    """POST a JSON body with a list of property IDs to produce a combined feed.
    Example body: {"property_ids": ["cottage123", "cabinA", "cabinB"]}
    """
    cache_key = f"batch:{','.join(property_ids)}:{calendar_name or ''}"
    cached = _cache_get(cache_key)
    if cached:
        return Response(content=cached, media_type="text/calendar")

    all_bookings: list[dict] = []
    for pid in property_ids:
        all_bookings.extend(await fetch_bookings_for_property(pid))
    ics_bytes = render_calendar(all_bookings, calendar_name)
    _cache_set(cache_key, ics_bytes)
    return Response(content=ics_bytes, media_type="text/calendar")


# ---- Optional: hourly pre-generation hooks ---------------------------------

async def generate_and_write(property_ids: list[str], outdir: str = "/tmp/cal") -> list[str]:
    """Generate .ics files for given property IDs and write to disk.
    Returns list of file paths written.
    Useful for cron/Lambda jobs that publish static files (e.g., to S3).
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    for pid in property_ids:
        bookings = await fetch_bookings_for_property(pid)
        # We can use entry_name of first booking as calendar name, as a fallback
        prop_name = None
        for b in bookings:
            if b.get("entry_name"):
                prop_name = b.get("entry_name")
                break
        ics_bytes = render_calendar(bookings, prop_name)
        path = os.path.join(outdir, f"{pid}.ics")
        with open(path, "wb") as f:
            f.write(ics_bytes)
        written.append(path)
    return written


@app.post("/refresh")
async def refresh(property_ids: list[str]):
    """Force-refresh cache & (optionally) write .ics files to disk.
    Intended to be called by a scheduler (cron/Cloud Scheduler)."""
    # Invalidate cache
    for pid in property_ids:
        for key in list(_CACHE.keys()):
            if key.startswith(f"prop:{pid}:"):
                _CACHE.pop(key, None)
    # Re-generate into cache
    for pid in property_ids:
        bookings = await fetch_bookings_for_property(pid)
        ics_bytes = render_calendar(bookings)
        _cache_set(f"prop:{pid}:", ics_bytes)
    return {"status": "refreshed", "count": len(property_ids)}


# GitHub Pages + Actions (hourly static iCal)

Below is a drop‑in GitHub Actions workflow and tiny `requirements.txt` to publish hourly to **GitHub Pages**. Use together with the three property IDs you provided.

## Files to add to your repo

**`requirements.txt`**
```
httpx
icalendar
python-dateutil
python-dotenv
```

**`.github/workflows/build-ical.yml`**
```yaml
name: Build iCal feeds hourly

on:
  schedule:
    - cron: "0 * * * *"   # hourly, UTC
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: write  # allow pushing to gh-pages
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Generate .ics files
        env:
          BOOKSTER_API_BASE: ${{ secrets.BOOKSTER_API_BASE }}
          BOOKSTER_API_KEY:  ${{ secrets.BOOKSTER_API_KEY }}
        run: |
          python - << 'PY'
          import os, asyncio
          from pathlib import Path
          from app import generate_and_write

          OUT = Path("public")
          OUT.mkdir(parents=True, exist_ok=True)

          # Your three properties (ID -> Display Name)
          PROPS = {
            "158595": "Barn Owl Cabin",
            "158596": "Redroofs by the Woods",
            "158497": "Bumblebee Cabin",
          }

          os.environ.setdefault("BOOKSTER_API_BASE", os.getenv("BOOKSTER_API_BASE","https://app.booksterhq.com/api"))
          os.environ.setdefault("BOOKSTER_API_KEY", os.getenv("BOOKSTER_API_KEY",""))

          # Write feeds
          paths = asyncio.run(generate_and_write(list(PROPS.keys()), str(OUT)))

          # Simple index page with names
          html = ["<h1>Redroofs iCal feeds</h1>"]
          for pid, name in PROPS.items():
            html.append(f"<p><a href='{pid}.ics'>{name}</a></p>")
          (OUT/"index.html").write_text("
".join(html), encoding="utf-8")
          PY

      - name: Publish to gh-pages
        run: |
          set -e
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          rm -rf .git/worktrees/gh-pages || true
          git fetch origin gh-pages || true
          git worktree add -B gh-pages gh-pages origin/gh-pages || git worktree add -B gh-pages gh-pages
          rsync -av --delete public/ gh-pages/
          cd gh-pages
          git add .
          git commit -m "Update iCal feeds" || echo "No changes"
          git push origin gh-pages
```

## One‑time setup checklist
1) Create a **GitHub account** (github.com → Sign up). Choose a username you’re happy to share in URLs.
2) Click **New repository** → Name it (e.g., `redroofs-ical`) → Public (free) → Create.
3) On the repo page: **Add file** → **Upload files**. Upload your `app.py` (this file), plus `requirements.txt` (above). Click **Commit**.
4) Create folders: In the repo, add a new file at the path `.github/workflows/build-ical.yml` with the YAML above (GitHub will auto‑create folders). Commit.
5) Add **secrets**: Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:
   - `BOOKSTER_API_BASE` = `https://app.booksterhq.com/api`
   - `BOOKSTER_API_KEY` = *your key*
6) Enable **Pages**: Repo → **Settings** → **Pages** → **Build and deployment** → **Source** = **Deploy from a branch** → **Branch** = `gh-pages` / `/ (root)` → Save. (First run will create the branch.)
7) Trigger first build: Repo → **Actions** → open the workflow → **Run workflow** (or wait for the top of the hour).

After the first successful run, your feeds will be live at:
```
https://<your-username>.github.io/<your-repo>/158595.ics   (Barn Owl Cabin)
https://<your-username>.github.io/<your-repo>/158596.ics   (Redroofs by the Woods)
https://<your-username>.github.io/<your-repo>/158497.ics   (Bumblebee Cabin)
```

Tip: Add a custom domain later via **Pages → Custom domain** (CNAME).


---

## Deployment URLs for **grayik/redroofs-ical**
Once your first GitHub Actions run succeeds and GitHub Pages is enabled on `gh-pages`, your iCal feeds will be here:

```
https://grayik.github.io/redroofs-ical/158595.ics   (Barn Owl Cabin)
https://grayik.github.io/redroofs-ical/158596.ics   (Redroofs by the Woods)
https://grayik.github.io/redroofs-ical/158497.ics   (Bumblebee Cabin)
```

These links are also included on the generated `index.html` at:
```
https://grayik.github.io/redroofs-ical/
```
