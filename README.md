# Puget Sound Currents

Live, animated current maps for West Point, Golden Gardens, Richmond Beach–Edmonds
Double Bluff / Maxwelton / Point No Point, Admiralty Inlet and West Beach, from NOAA's Salish Sea and Columbia
River Operational Forecast System (SSCOFS), on the model's native triangle mesh.

A GitHub Actions job runs after each of NOAA's four daily model runs. It pulls the
hourly surface currents once (today 5 AM to the end of the latest 72-hour forecast)
and publishes compact data plus a viewer page to GitHub Pages. The viewer blends
between model hours in the browser, so time moves smoothly and the flow is shown as
moving streaks.

- `build_data.py` – fetches SSCOFS output and writes `site/data/*.json` and the viewer
- `web/index.html` – the viewer (canvas, no libraries)
- `sscofs_westpoint.py` – shared SSCOFS access, area definitions (`REGIONS`), and a
  standalone renderer for still images / GIFs (see the top of the file)
- `.github/workflows/currents.yml` – the schedule

To add an area: add it to `REGIONS` in `sscofs_westpoint.py` and to `SITE_REGIONS`
in `build_data.py`.

Model guidance only – not for navigation.
