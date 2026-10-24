#!/usr/bin/env python3
"""Build the phone-friendly site: one viewer page per day (daylight hours)
for today and the next two days, plus an index page linking them."""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
OUT = "site"
DAYS = int(os.environ.get("DAYS", 3))
FIRST_HOUR = int(os.environ.get("FIRST_HOUR", 7))     # local time
LAST_HOUR = int(os.environ.get("LAST_HOUR", 20))
SMOOTH = os.environ.get("SMOOTH", "2")

os.makedirs(OUT, exist_ok=True)
today = datetime.now(TZ).date()
pages = []
for d in range(DAYS):
    day = today + timedelta(days=d)
    name = f"day-{day:%Y-%m-%d}"
    cmd = [sys.executable, "sscofs_westpoint.py",
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
        print(f"No page for {day} (outside the model's forecast window or data missing)")

if not pages:
    sys.exit("Nothing was built.")

now = datetime.now(TZ)
items = "\n".join(
    f'<a class="day" href="{n}.html"><b>{"Today" if d == today else f"{d:%A}"}</b>'
    f'<span>{d:%b %d} &middot; {FIRST_HOUR % 12 or 12} {"AM" if FIRST_HOUR < 12 else "PM"}'
    f'&ndash;{LAST_HOUR % 12 or 12} {"AM" if LAST_HOUR < 12 else "PM"}</span></a>'
    for d, n in pages)
html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>West Point Currents</title>
<style>
body{{margin:0;background:#101418;color:#dde;font:16px -apple-system,system-ui,sans-serif}}
.wrap{{max-width:600px;margin:0 auto;padding:20px 16px}}
h1{{font-size:24px;margin:0 0 4px}} .sub{{color:#889;font-size:13px;margin-bottom:20px}}
.day{{display:flex;justify-content:space-between;align-items:center;background:#1c242e;
color:#fff;text-decoration:none;padding:16px;border-radius:10px;margin:10px 0}}
.day span{{color:#9ab;font-size:14px}}
.note{{color:#778;font-size:12px;margin-top:24px;line-height:1.5}}
</style></head><body><div class="wrap">
<h1>West Point Currents</h1>
<div class="sub">Updated {now:%a %b %d, %I:%M %p %Z}</div>
{items}
<p class="note">NOAA SSCOFS model (Salish Sea forecast system), drawn on the model's own
~100&nbsp;m mesh. Refreshed after each of NOAA's four daily model runs. Model guidance
only &ndash; not for navigation.</p>
</div></body></html>"""
with open(os.path.join(OUT, "index.html"), "w") as f:
    f.write(html)
print("Built", len(pages), "day pages")
