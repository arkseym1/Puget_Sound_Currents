#!/usr/bin/env python3
"""
sscofs_westpoint.py - animated current maps for West Point (Seattle) and other
Puget Sound spots (--region), from
NOAA's Salish Sea and Columbia River Operational Forecast System (SSCOFS),
drawn on the model's own triangular mesh (no resampling to a coarse grid).

What it shows
  * Current speed, coloured per mesh triangle (native ~100 m model resolution)
  * Streamlines of the flow, so eddies and reversals stand out
  * Depth contours from the model's bathymetry
  * Water level at the region's reference point over the window, with the current frame marked

Data
  NOAA publishes SSCOFS four times a day (03, 09, 15, 21 UTC). Each run has
  hourly forecast files out to 72 h ("f001".."f072") and hourly nowcast files
  for the previous 6 h ("n000".."n006"). Future times use the latest run's
  forecast; past times use nowcasts, so you can look back at a session you
  already rode (as far back as NOAA keeps the files).

  By default only the surface-layer currents are pulled through NOAA's
  OPeNDAP server (a few MB per hour). If that server is unreachable it falls
  back to downloading whole files from NOAA's public AWS bucket
  (~210 MB per hour of data, cached and deleted afterwards).

Install (once):
    pip install numpy matplotlib netCDF4 tzdata

Examples:
    python sscofs_westpoint.py                                  # next 12 h
    python sscofs_westpoint.py --start "2026-10-25 09:00" --hours 8
    python sscofs_westpoint.py --start "2026-10-20 13:00" --hours 4   # a past session
    python sscofs_westpoint.py --layer depthavg --mesh          # depth-averaged, show mesh
    python sscofs_westpoint.py --files a.nc b.nc                # local SSCOFS files
    python sscofs_westpoint.py --region useless-bay             # Double Bluff / Maxwelton / Point No Point

Outputs (in --out, default ./westpoint_output):
    westpoint_<start>.html   scrub/play viewer (open in any browser, works on phone)
    westpoint_<start>.gif    looping animation
    westpoint_<start>.mp4    if ffmpeg is installed

Model guidance only - not for navigation. SSCOFS is a good model, but
~100 m mesh spacing near shore means features smaller than a few hundred
metres (tight eddies right off the beach) are only partly resolved.
"""

import argparse
import base64
import io
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.tri as mtri
from matplotlib import animation

try:
    import netCDF4
except ImportError:
    sys.exit("netCDF4 is required:  pip install netCDF4")

try:
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception:      # Windows has no built-in time zone database
    sys.exit("Time zone data missing. Run:  pip install tzdata")
CYCLES = (3, 9, 15, 21)
MS_TO_KN = 1.943844
M_TO_FT = 3.28084
R_EARTH = 6371000.0
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "sscofs_westpoint")

DAP_BASE = "https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/SSCOFS/MODELS"
S3_BASE = "https://noaa-nos-ofs-pds.s3.amazonaws.com/sscofs/netcdf"

# Map areas. box = (lon0, lon1, lat0, lat1); ref = point where water level is
# plotted; label kinds: "ref" (star), "spot" (dot), "water", "land".
REGIONS = {
    "westpoint": dict(
        title="West Point",
        box=(-122.495, -122.385, 47.635, 47.695),
        ref=(-122.4357, 47.6619),
        vmax=2.0,
        labels=[("West Point", -122.4357, 47.6619, "ref"),
                ("Shilshole Bay", -122.412, 47.684, "water"),
                ("Discovery Park", -122.418, 47.657, "land"),
                ("Puget Sound", -122.475, 47.645, "water")]),
    "useless-bay": dict(
        title="Double Bluff / Maxwelton / Point No Point",
        box=(-122.565, -122.375, 47.890, 48.000),
        ref=(-122.452, 47.943),
        vmax=2.5,
        labels=[("Double Bluff Beach", -122.514, 47.979, "spot"),
                ("Maxwelton", -122.448, 47.939, "ref"),
                ("Point No Point", -122.5266, 47.9122, "spot"),
                ("Sunlight Beach", -122.482, 47.989, "spot"),
                ("Useless Bay", -122.495, 47.968, "water"),
                ("Puget Sound", -122.475, 47.915, "water"),
                ("Whidbey Island", -122.405, 47.965, "land")]),
    "golden-gardens": dict(
        title="Golden Gardens",
        box=(-122.440, -122.380, 47.672, 47.708),
        ref=(-122.4055, 47.6915),
        vmax=2.0,
        labels=[("Golden Gardens", -122.4055, 47.6915, "ref"),
                ("North Beach", -122.392, 47.7015, "spot"),
                ("Shilshole Marina", -122.4075, 47.680, "water"),
                ("Puget Sound", -122.428, 47.690, "water")]),
    "richmond-edmonds": dict(
        title="Richmond Beach to Edmonds",
        box=(-122.445, -122.360, 47.755, 47.816),
        ref=(-122.3865, 47.7665),
        vmax=2.0,
        labels=[("Richmond Beach", -122.3865, 47.7665, "ref"),
                ("Point Wells", -122.3975, 47.7785, "spot"),
                ("Marina Beach Park", -122.3935, 47.8045, "spot"),
                ("Edmonds", -122.376, 47.812, "land"),
                ("Woodway", -122.375, 47.790, "land"),
                ("Puget Sound", -122.425, 47.785, "water")]),
}
REGION = REGIONS["westpoint"]      # set from --region in main()


# --------------------------------------------------------------------------
# Locating files
# --------------------------------------------------------------------------

def fname(cycle, kind, hh):
    return f"sscofs.t{cycle:%H}z.{cycle:%Y%m%d}.fields.{kind}{hh:03d}.nc"


def url_patterns(cycle, kind, hh, source):
    f = fname(cycle, kind, hh)
    dap = [f"{DAP_BASE}/{cycle:%Y}/{cycle:%m}/{cycle:%d}/{f}",
           f"{DAP_BASE}/{cycle:%Y%m}/{f}"]
    s3 = [f"{S3_BASE}/{cycle:%Y}/{cycle:%m}/{cycle:%d}/{f}",
          f"{S3_BASE}/{cycle:%Y%m}/{f}"]
    out = []
    if source in ("auto", "dap"):
        out += [("dap", u) for u in dap]
    if source in ("auto", "s3"):
        out += [("s3", u) for u in s3]
    return out


def http_ok(url, method="GET", timeout=20):
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if method == "GET":
                r.read(256)
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


class Locator:
    """Finds where a given SSCOFS file lives, remembering which URL pattern works."""

    def __init__(self, source):
        self.source = source
        self.good = None          # index of the pattern that worked last
        self.seen = {}

    def find(self, cycle, kind, hh):
        key = (cycle, kind, hh)
        if key in self.seen:
            return self.seen[key]
        pats = url_patterns(cycle, kind, hh, self.source)
        order = list(range(len(pats)))
        if self.good is not None and self.good < len(pats):
            order.remove(self.good)
            order.insert(0, self.good)
        hit = None
        for i in order:
            kind_, url = pats[i]
            ok = http_ok(url + ".dds") if kind_ == "dap" else http_ok(url, "HEAD")
            if ok:
                self.good = i
                hit = (kind_, url)
                break
        self.seen[key] = hit
        return hit


def recent_cycles(now_utc, back_hours=96):
    t = now_utc.replace(minute=0, second=0, microsecond=0)
    out = []
    for h in range(back_hours + 1):
        c = t - timedelta(hours=h)
        if c.hour in CYCLES:
            out.append(c)
    return out


def latest_cycle(loc, now_utc):
    for c in recent_cycles(now_utc, 48):
        # require the end of the forecast so a run that is still uploading is skipped
        if loc.find(c, "f", 72) and loc.find(c, "f", 1):
            return c
    return None


def plan_frames(loc, start_utc, n_hours, step):
    """Return [(valid_time_utc, (cycle, kind, hh)), ...] for each requested hour."""
    now = datetime.now(timezone.utc)
    print("Looking for the latest SSCOFS run ...")
    latest = latest_cycle(loc, now)
    if latest is None:
        sys.exit("Couldn't find any SSCOFS run in the last 48 h. Check your internet "
                 "connection, or try --source s3 / --source dap.")
    print(f"  latest run: {latest:%Y-%m-%d %H}Z "
          f"({latest.astimezone(LOCAL_TZ):%a %b %d %I:%M %p %Z})")
    horizon = latest + timedelta(hours=72)
    plan = []
    t = start_utc
    end = start_utc + timedelta(hours=n_hours)
    while t <= end:
        if t > horizon:
            print(f"  note: stopping at {horizon.astimezone(LOCAL_TZ):%a %I %p} "
                  f"- the forecast only reaches 72 h past the latest run.")
            break
        if t > latest:
            plan.append((t, (latest, "f", int((t - latest).total_seconds() // 3600))))
        else:
            # nowcast: smallest cycle at or after t; n006 is the cycle time itself
            c = t.replace(minute=0, second=0, microsecond=0)
            while c.hour not in CYCLES:
                c += timedelta(hours=1)
            plan.append((t, (c, "n", 6 - int((c - t).total_seconds() // 3600))))
        t += timedelta(hours=step)
    return plan


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def download(url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, url.rsplit("/", 1)[-1])
    if os.path.exists(path):
        return path
    print(f"    downloading {url.rsplit('/', 1)[-1]} (~210 MB) ...", flush=True)
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(tmp, path)
    return path


def arr(v):
    a = v[:] if not hasattr(v, "shape") or v.ndim == 0 else v
    return np.ma.filled(np.ma.asarray(a), np.nan)


@dataclass
class Grid:
    lon: np.ndarray          # subset node lon
    lat: np.ndarray
    h: np.ndarray            # depth at subset nodes (m)
    tris: np.ndarray         # (n,3) local node indices
    e0: int                  # contiguous global element range to read
    e1: int
    eloc: np.ndarray         # positions of our elements inside [e0:e1]
    wp_node: int             # global node index nearest the region reference point
    min_edge_m: float
    med_edge_m: float


def build_grid(ds, box, margin=0.02):
    lon = arr(ds.variables["lon"][:]).astype(float)
    lat = arr(ds.variables["lat"][:]).astype(float)
    lon = np.where(lon > 180, lon - 360, lon)
    nv = np.asarray(ds.variables["nv"][:]).astype(np.int64)
    if nv.shape[0] != 3:
        nv = nv.T
    nv = nv.T - 1                                   # (nele, 3), 0-based
    h = arr(ds.variables["h"][:]).astype(float)

    lon0, lon1, lat0, lat1 = box
    clon = lon[nv].mean(axis=1)
    clat = lat[nv].mean(axis=1)
    sel = np.where((clon > lon0 - margin) & (clon < lon1 + margin) &
                   (clat > lat0 - margin) & (clat < lat1 + margin))[0]
    if sel.size == 0:
        sys.exit("No model elements inside the map box - check --box.")
    nodes, inv = np.unique(nv[sel], return_inverse=True)
    tris = inv.reshape(-1, 3)

    rx, ry = REGION["ref"]
    d = (lon - rx) ** 2 * np.cos(np.radians(ry)) ** 2 + (lat - ry) ** 2
    wp = int(np.argmin(d))

    # mesh spacing inside the box, for the record
    sl, sa = lon[nodes], lat[nodes]
    k = np.cos(np.radians(REGION["ref"][1]))
    edges = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        dx = (sl[tris[:, a]] - sl[tris[:, b]]) * k
        dy = sa[tris[:, a]] - sa[tris[:, b]]
        edges.append(np.radians(np.hypot(dx, dy)) * R_EARTH)
    edges = np.concatenate(edges)
    inbox = ((clon[sel] > lon0) & (clon[sel] < lon1) &
             (clat[sel] > lat0) & (clat[sel] < lat1))
    e_in = np.concatenate([e[inbox] for e in np.split(edges, 3)]) if inbox.any() else edges

    e0, e1 = int(sel.min()), int(sel.max()) + 1
    return Grid(lon[nodes], lat[nodes], h[nodes], tris, e0, e1, sel - e0, wp,
                float(np.min(e_in)), float(np.median(e_in)))


def file_time(ds):
    tv = ds.variables["time"]
    t = netCDF4.num2date(np.atleast_1d(tv[:])[0], tv.units,
                         only_use_cftime_datetimes=False,
                         only_use_python_datetimes=True)
    return t.replace(tzinfo=timezone.utc)


def read_frame(ds, g, layer):
    if layer == "surface":
        u = arr(ds.variables["u"][0, 0, g.e0:g.e1])
        v = arr(ds.variables["v"][0, 0, g.e0:g.e1])
    else:
        u = arr(ds.variables["ua"][0, g.e0:g.e1])
        v = arr(ds.variables["va"][0, g.e0:g.e1])
    z = float(arr(ds.variables["zeta"][0, g.wp_node:g.wp_node + 1])[0])
    return u[g.eloc].astype(float), v[g.eloc].astype(float), z


@dataclass
class Frame:
    t: datetime
    u: np.ndarray
    v: np.ndarray
    zeta: float
    label: str


def open_any(kind, url, keep):
    if kind == "dap" or kind == "local":
        return netCDF4.Dataset(url), None
    path = download(url)
    return netCDF4.Dataset(path), (None if keep else path)


def load_frames(sources, box, layer, keep):
    """sources: list of (label, kind, url_or_path). Returns grid, frames."""
    grid, frames = None, []
    for i, (label, kind, url) in enumerate(sources, 1):
        print(f"  [{i}/{len(sources)}] {label}", flush=True)
        ds, cleanup = open_any(kind, url, keep)
        try:
            if grid is None:
                grid = build_grid(ds, box)
                print(f"    mesh in view: {len(grid.tris)} triangles, edge length "
                      f"median {grid.med_edge_m:.0f} m, min {grid.min_edge_m:.0f} m")
            u, v, z = read_frame(ds, grid, layer)
            frames.append(Frame(file_time(ds), u, v, z, label))
        finally:
            ds.close()
            if cleanup:
                os.remove(cleanup)
    frames.sort(key=lambda f: f.t)
    return grid, frames


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

def interp_frames(frames, sub):
    if sub <= 1 or len(frames) < 2:
        return frames
    out = []
    for a, b in zip(frames[:-1], frames[1:]):
        for k in range(sub):
            w = k / sub
            out.append(Frame(a.t + (b.t - a.t) * w, a.u * (1 - w) + b.u * w,
                             a.v * (1 - w) + b.v * w, a.zeta * (1 - w) + b.zeta * w,
                             a.label if k == 0 else "blended between model hours"))
    out.append(frames[-1])
    return out


class Renderer:
    def __init__(self, g, box, vmax, show_mesh, layer, all_frames):
        self.g, self.box, self.vmax, self.mesh, self.layer = g, box, vmax, show_mesh, layer
        self.tri = mtri.Triangulation(g.lon, g.lat, g.tris)
        self.k = np.cos(np.radians(REGION["ref"][1]))
        lon0, lon1, lat0, lat1 = box
        nx = 260
        ny = int(nx * (lat1 - lat0) / ((lon1 - lon0) * self.k))
        self.X, self.Y = np.meshgrid(np.linspace(lon0, lon1, nx), np.linspace(lat0, lat1, ny))
        self.bary = self._barycentric()
        # element -> node averaging matrix for streamline interpolation
        n = len(g.lon)
        self.cnt = np.zeros(n)
        np.add.at(self.cnt, g.tris.ravel(), 1)
        self.ts = [f.t for f in all_frames]
        self.zs = [f.zeta for f in all_frames]

    def _barycentric(self):
        """For each streamline-grid point: containing triangle's nodes and weights."""
        g, X, Y = self.g, self.X, self.Y
        x0, y0 = X[0, 0], Y[0, 0]
        dx, dy = X[0, 1] - x0, Y[1, 0] - y0
        ny, nx = X.shape
        idx = np.full((ny, nx, 3), -1, dtype=np.int64)
        w = np.zeros((ny, nx, 3))
        P = np.stack([g.lon[g.tris], g.lat[g.tris]], -1)       # (nt,3,2)
        i0 = np.clip(np.floor((P[..., 0].min(1) - x0) / dx).astype(int), 0, nx - 1)
        i1 = np.clip(np.ceil((P[..., 0].max(1) - x0) / dx).astype(int), 0, nx - 1)
        j0 = np.clip(np.floor((P[..., 1].min(1) - y0) / dy).astype(int), 0, ny - 1)
        j1 = np.clip(np.ceil((P[..., 1].max(1) - y0) / dy).astype(int), 0, ny - 1)
        for t in np.where((i1 >= i0) & (j1 >= j0))[0]:
            (ax_, ay), (bx, by), (cx, cy) = P[t]
            det = (by - cy) * (ax_ - cx) + (cx - bx) * (ay - cy)
            if abs(det) < 1e-16:
                continue
            xs = X[j0[t]:j1[t] + 1, i0[t]:i1[t] + 1]
            ys = Y[j0[t]:j1[t] + 1, i0[t]:i1[t] + 1]
            l1 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / det
            l2 = ((cy - ay) * (xs - cx) + (ax_ - cx) * (ys - cy)) / det
            l3 = 1 - l1 - l2
            m = (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
            if not m.any():
                continue
            jj, ii = np.nonzero(m)
            jj += j0[t]
            ii += i0[t]
            idx[jj, ii] = g.tris[t]
            w[jj, ii, 0], w[jj, ii, 1], w[jj, ii, 2] = l1[m], l2[m], l3[m]
        return idx, w

    def grid_interp(self, node_vals):
        idx, w = self.bary
        out = (node_vals[np.maximum(idx, 0)] * w).sum(-1)
        out[idx[..., 0] < 0] = np.nan
        return out

    def to_nodes(self, ev):
        s = np.zeros(len(self.g.lon))
        np.add.at(s, self.g.tris.ravel(), np.repeat(ev, 3))
        return s / np.maximum(self.cnt, 1)

    def draw(self, f, dpi=110):
        g = self.g
        lon0, lon1, lat0, lat1 = self.box
        # size the figure so the map keeps true proportions with no dead space
        W, mw = 8.0, 6.3                                   # inches: figure, map width
        mh = mw * (lat1 - lat0) / ((lon1 - lon0) * self.k)
        top, gap, th, bot = 0.85, 0.55, 1.05, 0.55          # title, gap, tide panel, footer
        H = top + mh + gap + th + bot
        fig = plt.figure(figsize=(W, H), facecolor="#101418")
        ax = fig.add_axes([0.6 / W, (bot + th + gap) / H, mw / W, mh / H])
        cax = fig.add_axes([(0.6 + mw + 0.2) / W, (bot + th + gap) / H, 0.18 / W, mh / H])
        tax = fig.add_axes([0.9 / W, bot / H, (mw - 0.3) / W, th / H])
        self.H = H
        ax.set_facecolor("#5b5a45")                          # land = outside the mesh

        spd = np.hypot(f.u, f.v) * MS_TO_KN
        pc = ax.tripcolor(self.tri, facecolors=spd, cmap="turbo", vmin=0, vmax=self.vmax,
                          edgecolors="k" if self.mesh else "none",
                          linewidth=0.15 if self.mesh else 0)
        cs = ax.tricontour(self.tri, g.h, levels=[5, 10, 20, 50, 100, 150, 200],
                           colors="white", linewidths=0.5, alpha=0.45)
        ax.clabel(cs, fmt="%d m", fontsize=6, colors="white")

        # streamlines: element velocities -> nodes -> fine grid (display only;
        # lon component scaled so directions are true with the map's aspect)
        U = self.grid_interp(self.to_nodes(f.u / self.k))
        V = self.grid_interp(self.to_nodes(f.v))
        ax.streamplot(self.X, self.Y, U, V, density=2.2, color="white",
                      linewidth=0.6, arrowsize=0.7)

        for name, x, y, kind in REGION["labels"]:
            if lon0 < x < lon1 and lat0 < y < lat1:
                if kind in ("ref", "spot"):
                    ax.plot(x, y, marker="*" if kind == "ref" else "o",
                            ms=10 if kind == "ref" else 5, color="white", mec="k", zorder=5)
                    ax.annotate(name, (x, y), xytext=(6, -12), textcoords="offset points",
                                color="white", fontsize=9, weight="bold")
                else:
                    ax.text(x, y, name, color="white" if kind == "water" else "#e8e2c8",
                            fontsize=8, ha="center", alpha=0.85, style="italic")

        ax.set_xlim(lon0, lon1)
        ax.set_ylim(lat0, lat1)
        ax.set_aspect(1 / self.k)
        ax.tick_params(colors="#9aa", labelsize=7)
        for s in ax.spines.values():
            s.set_color("#445")

        # scale bar 500 m
        bar = 500 / (R_EARTH * self.k) * 180 / np.pi
        bx, by = lon0 + (lon1 - lon0) * 0.05, lat0 + (lat1 - lat0) * 0.04
        ax.plot([bx, bx + bar], [by, by], color="white", lw=2)
        ax.text(bx + bar / 2, by + (lat1 - lat0) * 0.012, "500 m", color="white",
                fontsize=7, ha="center")

        cb = fig.colorbar(pc, cax=cax)
        cb.set_label("current speed (knots)", color="#ccd")
        cb.ax.tick_params(colors="#ccd", labelsize=8)

        lt = f.t.astimezone(LOCAL_TZ)
        lyr = "surface layer" if self.layer == "surface" else "depth-averaged"
        fig.text(0.6 / W, 1 - 0.45 / H, f"{lt:%a %b %d  %I:%M %p %Z}", color="white", fontsize=16,
                 weight="bold")
        fig.text(0.6 / W, 1 - 0.72 / H, f"{REGION['title']} - SSCOFS {lyr} currents, native mesh  "
                 f"({f.label})", color="#aab", fontsize=8.5)

        # water level panel
        tl = [t.astimezone(LOCAL_TZ) for t in self.ts]
        tax.set_facecolor("#101418")
        tax.plot(tl, np.array(self.zs) * M_TO_FT, color="#7fc8ff", lw=1.5)
        tax.axvline(lt, color="#ffcc33", lw=1.5)
        tax.plot([lt], [f.zeta * M_TO_FT], "o", color="#ffcc33")
        tax.set_ylabel("water level (ft)", color="#ccd", fontsize=8)
        tax.tick_params(colors="#9aa", labelsize=7)
        span = (self.ts[-1] - self.ts[0]).total_seconds() / 3600 if len(self.ts) > 1 else 1
        iv = max(1, int(np.ceil(span / 6)))
        tax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, iv), tz=LOCAL_TZ))
        tax.xaxis.set_major_formatter(mdates.DateFormatter("%a %I %p", tz=LOCAL_TZ))
        for s in tax.spines.values():
            s.set_color("#445")
        fig.text(0.5, 0.12 / H, "Model guidance from NOAA SSCOFS - not for navigation",
                 color="#778", fontsize=7, ha="center")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()


HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ Currents</title>
<style>
body{margin:0;background:#101418;color:#dde;font:14px -apple-system,system-ui,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:12px 16px}
img{width:100%;height:auto;display:block;border-radius:6px}
.ctl{display:flex;gap:10px;align-items:center;margin:10px 0}
input[type=range]{flex:1}
button{background:#2a3440;color:#fff;border:0;border-radius:6px;padding:8px 14px;font-size:14px}
#lab{min-width:150px;text-align:right;font-variant-numeric:tabular-nums}
.note{color:#889;font-size:12px}
</style></head><body><div class="wrap">__BACK__
<div class="ctl"><button id="pl">Play</button><input id="sl" type="range" min="0" max="__N__" value="0">
<span id="lab"></span></div>
<img id="im" alt="current map">
<p class="note">Drag the slider or use the arrow keys. Speed is coloured per model triangle;
white lines are streamlines. Run __RUN__. NOAA SSCOFS model guidance, not for navigation.</p>
</div><script>
const F=__FRAMES__, L=__LABELS__;
const im=document.getElementById('im'), sl=document.getElementById('sl'),
lab=document.getElementById('lab'), pl=document.getElementById('pl');
let i=0, timer=null;
function show(k){i=(k+F.length)%F.length; im.src='data:image/png;base64,'+F[i]; sl.value=i; lab.textContent=L[i];}
sl.oninput=()=>show(+sl.value);
pl.onclick=()=>{ if(timer){clearInterval(timer);timer=null;pl.textContent='Play';}
 else {timer=setInterval(()=>show(i+1),__MS__);pl.textContent='Pause';} };
document.onkeydown=e=>{ if(e.key==='ArrowRight')show(i+1); if(e.key==='ArrowLeft')show(i-1); };
show(0);
</script></body></html>"""


def write_outputs(pngs, frames, out_dir, stem, run_note, fps, html_only=False, back=None):
    os.makedirs(out_dir, exist_ok=True)
    labels = [f.t.astimezone(LOCAL_TZ).strftime("%a %b %d %I:%M %p") for f in frames]
    html = (HTML.replace("__FRAMES__", "[" + ",".join(
                '"' + base64.b64encode(p).decode() + '"' for p in pngs) + "]")
                .replace("__LABELS__", "[" + ",".join(f'"{s}"' for s in labels) + "]")
                .replace("__N__", str(len(pngs) - 1))
                .replace("__MS__", str(int(1000 / fps)))
                .replace("__RUN__", run_note)
                .replace("__TITLE__", REGION["title"])
                .replace("__BACK__", f'<a href="{back}" style="color:#7fc8ff">&larr; All days</a>'
                         if back else ""))
    hp = os.path.join(out_dir, stem + ".html")
    with open(hp, "w") as fh:
        fh.write(html)
    written = [hp]
    if html_only:
        return written

    from PIL import Image
    imgs = [Image.open(io.BytesIO(p)).convert("RGB") for p in pngs]
    gp = os.path.join(out_dir, stem + ".gif")
    pal = [im.quantize(colors=200, method=Image.Quantize.MEDIANCUT) for im in imgs]
    pal[0].save(gp, save_all=True, append_images=pal[1:], duration=int(1000 / fps), loop=0)
    written.append(gp)

    if animation.writers.is_available("ffmpeg"):
        mp = os.path.join(out_dir, stem + ".mp4")
        fig = plt.figure(figsize=(imgs[0].width / 100, imgs[0].height / 100), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        art = ax.imshow(np.asarray(imgs[0]))
        w = animation.FFMpegWriter(fps=fps, extra_args=["-pix_fmt", "yuv420p",
                                                         "-preset", "veryfast"])
        with w.saving(fig, mp, dpi=100):
            for im in imgs:
                art.set_data(np.asarray(im))
                w.grab_frame()
        plt.close(fig)
        written.append(mp)
    return written


# --------------------------------------------------------------------------

def parse_local(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            pass
    sys.exit(f"Couldn't read --start {s!r}; use e.g. \"2026-10-25 09:00\" (local time).")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", help='local start time, e.g. "2026-10-25 09:00" (default: now)')
    ap.add_argument("--hours", type=int, default=12, help="length of window (default 12)")
    ap.add_argument("--step", type=int, default=1, help="hours between model frames (default 1)")
    ap.add_argument("--smooth", type=int, default=3,
                    help="frames per model hour, blended in time for smoother playback "
                         "(default 3; 1 = model hours only)")
    ap.add_argument("--layer", choices=["surface", "depthavg"], default="surface")
    ap.add_argument("--region", choices=sorted(REGIONS), default="westpoint",
                    help="map area (default westpoint)")
    ap.add_argument("--vmax", type=float, help="top of speed scale, knots (default per region)")
    ap.add_argument("--box", type=float, nargs=4, metavar=("LON0", "LON1", "LAT0", "LAT1"),
                    help="custom map extent (overrides the region's)")
    ap.add_argument("--mesh", action="store_true", help="draw the model's triangle mesh")
    ap.add_argument("--source", choices=["auto", "dap", "s3"], default="auto")
    ap.add_argument("--files", nargs="+", help="use local SSCOFS fields files instead")
    ap.add_argument("--keep-downloads", action="store_true")
    ap.add_argument("--fps", type=float, default=1, help="playback frames per second")
    ap.add_argument("--out", default="westpoint_output")
    ap.add_argument("--name", help="output file name stem (default westpoint_<start>)")
    ap.add_argument("--html-only", action="store_true", help="skip the GIF and MP4")
    ap.add_argument("--back-link", help="URL for an 'All days' link at the top of the viewer")
    a = ap.parse_args()

    global REGION
    REGION = REGIONS[a.region]
    box = tuple(a.box) if a.box else REGION["box"]
    vmax = a.vmax or REGION["vmax"]
    if a.files:
        sources = [(os.path.basename(p), "local", p) for p in a.files]
        run_note = "from local files"
    else:
        start = (parse_local(a.start) if a.start else datetime.now(LOCAL_TZ))
        start = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        loc = Locator(a.source)
        plan = plan_frames(loc, start, a.hours, a.step)
        sources = []
        for t, (c, kind, hh) in plan:
            hit = loc.find(c, kind, hh)
            name = fname(c, kind, hh)
            if not hit:
                print(f"  skipping {t.astimezone(LOCAL_TZ):%a %I %p}: {name} not found "
                      f"(older than NOAA's archive, or not uploaded yet)")
                continue
            sources.append((name, hit[0], hit[1]))
        if not sources:
            sys.exit("No model files available for that window.")
        if sources[0][1] == "s3":
            print(f"  OPeNDAP unavailable - downloading full files from AWS "
                  f"(~{0.21 * len(sources):.1f} GB total, deleted afterwards).")
        cyc = sorted({f"{s[0].split('.')[2]} {s[0].split('.')[1][1:3]}Z" for s in sources})
        run_note = "runs " + ", ".join(cyc)

    print("Reading model output ...")
    grid, frames = load_frames(sources, box, a.layer, a.keep_downloads)
    shown = interp_frames(frames, a.smooth)
    r = Renderer(grid, box, vmax, a.mesh, a.layer, frames)
    print(f"Drawing {len(shown)} frames ...")
    pngs = [r.draw(f) for f in shown]
    stem = a.name or a.region + "_" + frames[0].t.astimezone(LOCAL_TZ).strftime("%Y%m%d_%H%M")
    for p in write_outputs(pngs, shown, a.out, stem, run_note, a.fps, a.html_only, a.back_link):
        print("  wrote", p)


if __name__ == "__main__":
    main()
