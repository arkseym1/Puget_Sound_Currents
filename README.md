# West Point Currents

Animated current maps for the water off West Point (Seattle), from NOAA's
Salish Sea and Columbia River Operational Forecast System (SSCOFS), drawn on
the model's native ~100 m triangle mesh.

A GitHub Actions job runs after each of NOAA's four daily model runs and
publishes a phone-friendly site with today and the next two days
(7 AM–8 PM). Open the site from the repo's **About → website** link.

- `sscofs_westpoint.py` – fetches SSCOFS output and renders the maps (also runs on its own; see the top of the file)
- `build_site.py` – builds the daily pages and the index
- `.github/workflows/currents.yml` – the schedule

Settings you can change in the workflow's `env` or in `build_site.py`:
`DAYS`, `FIRST_HOUR`, `LAST_HOUR`, `SMOOTH`.

Model guidance only – not for navigation.
