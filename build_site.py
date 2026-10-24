#!/usr/bin/env python3
"""Build the phone-friendly site: for each region, one viewer page per day
(daylight hours) for today and the next two days, plus an index page."""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
OUT = "site"
REGIONS = [  # (region id for sscofs_westpoint.py --region, heading on the index page)
    ("westpoint", "West Point"),
    ("useless-bay", "Double Bluff · Maxwelton · Point No Point"),
]
DAYS = int(os.environ.get("DAYS", 3))
FIRST_HOUR = int(os.environ.get("FIRST_HOUR", 7))     # local time
LAST_HOUR = int(os.environ.get("LAST_HOUR", 20))
SMOOTH = os.environ.get("SMOOTH", "2")


def hr(h):
    return f'{h % 12 or 12} {"AM" if h < 12 else "PM"}'


os.makedirs(OUT, exist_ok=True)
today = datetime.now(TZ).date()
sections = []
for region, heading in REGIONS:
    pages = []
    for d in range(DAYS):
        day = today + timedelta(days=d)
        name = f"{region}-day-{day:%Y-%m-%d}"
        cmd = [sys.executable, "sscofs_westpoint.py", "--region", region,
               "--start", f"{day:%Y-%m-%d} {FIRST_HOUR:02d}:00",
               "--hours", str(LAST_HOUR - FIRST_HOUR),
               "--smooth", SMOOTH, "--out", OUT, "--name", name,
               "--html-only", "--back-link", "index.html"]
        print("::group::" + " ".join(cmd[1:]), flush=True)
        ok = subprocess.run(cmd).returncode == 0
        print("::endgroup::", flush=True)
        if ok and os.path.exists(os.path.join(OUT, name + ".html")):
            pages.append((day, name))
        else:
            print(f"No page for {region} {day} (outside the forecast window or data missing)")
    if pages:
        links = "\n".join(
            f'<a class="day" href="{n}.html"><b>{"Today" if d == today else f"{d:%A}"}</b>'
            f'<span>{d:%b %d} &middot; {hr(FIRST_HOUR)}&ndash;{hr(LAST_HOUR)}</span></a>'
            for d, n in pages)
        sections.append(f"<h2>{heading}</h2>\n{links}")

if not sections:
    sys.exit("Nothing was built.")

now = datetime.now(TZ)
html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Puget Sound Currents</title>
<style>
body{{margin:0;background:#101418;color:#dde;font:16px -apple-system,system-ui,sans-serif}}
.wrap{{max-width:600px;margin:0 auto;padding:20px 16px}}
h1{{font-size:24px;margin:0 0 4px}} .sub{{color:#889;font-size:13px;margin-bottom:8px}}
h2{{font-size:17px;margin:26px 0 6px;color:#cfe}}
.day{{display:flex;justify-content:space-between;align-items:center;background:#1c242e;
color:#fff;text-decoration:none;padding:16px;border-radius:10px;margin:10px 0}}
.day span{{color:#9ab;font-size:14px}}
.note{{color:#778;font-size:12px;margin-top:24px;line-height:1.5}}
</style></head><body><div class="wrap">
<h1>Puget Sound Currents</h1>
<div class="sub">Updated {now:%a %b %d, %I:%M %p %Z}</div>
{chr(10).join(sections)}
<p class="note">NOAA SSCOFS model (Salish Sea forecast system), drawn on the model's own
triangle mesh. Refreshed after each of NOAA's four daily model runs. Model guidance
only &ndash; not for navigation.</p>
</div></body></html>"""
with open(os.path.join(OUT, "index.html"), "w") as f:
    f.write(html)
print("Built", len(sections), "regions")
