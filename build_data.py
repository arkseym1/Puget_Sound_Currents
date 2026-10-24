#!/usr/bin/env python3
"""
Build the live viewer site: pulls NOAA SSCOFS hourly surface currents once
per hour of the timeline and writes compact data for every area, plus the
viewer page itself.

    site/index.html             the viewer (copied from web/index.html)
    site/data/regions.json      list of areas, colour scale, update time
    site/data/<area>.json       mesh, hourly currents, water level, depth contours

The browser interpolates between the hourly model outputs and animates the
flow, so the page is small and smooth.

    python build_data.py                     # today 5 AM to end of the latest forecast
    python build_data.py --files f1.nc ...   # local SSCOFS files (testing)
"""
import argparse
import base64
import json
import os
import shutil
from datetime import datetime, timezone

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.tri as mtri

import sscofs_westpoint as S

SITE_REGIONS = [  # (area id in sscofs_westpoint.REGIONS, tab name)
    ("westpoint", "West Point"),
    ("golden-gardens", "Golden Gardens"),
    ("richmond-edmonds", "Richmond–Edmonds"),
    ("useless-bay", "Double Bluff–Pt No Point"),
    ("admiralty-inlet", "Admiralty Inlet"),
    ("west-beach", "West Beach"),
]
DEPTH_FT = [10, 30, 60, 100, 200, 400, 600]
UV_SCALE = 1000          # stored as int16 mm/s
HERE = os.path.dirname(os.path.abspath(__file__))


def b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def depth_contours(g):
    fig, ax = plt.subplots()
    tri = mtri.Triangulation(g.lon, g.lat, g.tris)
    levels_m = [f / S.M_TO_FT for f in DEPTH_FT]
    if np.nanmax(g.h) <= levels_m[0]:
        plt.close(fig)
        return []
    cs = ax.tricontour(tri, g.h, levels=levels_m)
    out = []
    for ft, path in zip(DEPTH_FT, cs.get_paths()):
        v, c = path.vertices, path.codes
        if len(v) == 0:
            continue
        starts = list(np.where(c == 1)[0]) if c is not None else [0]
        lines = []
        for a, b in zip(starts, starts[1:] + [len(v)]):
            seg = v[a:b]
            if len(seg) >= 2:
                lines.append(np.round(seg.ravel(), 5).tolist())
        if lines:
            out.append({"ft": ft, "lines": lines})
    plt.close(fig)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="site")
    ap.add_argument("--start-hour", type=int, default=5, help="local hour today to start (default 5)")
    ap.add_argument("--source", choices=["auto", "dap", "s3"], default="auto")
    ap.add_argument("--files", nargs="+", help="local SSCOFS fields files instead of NOAA")
    a = ap.parse_args()

    if a.files:
        sources = [(os.path.basename(p), "local", p) for p in a.files]
        run_note = "local files"
    else:
        now = datetime.now(S.LOCAL_TZ)
        start = now.replace(hour=a.start_hour, minute=0, second=0, microsecond=0)
        start = start.astimezone(timezone.utc)
        loc = S.Locator(a.source)
        plan = S.plan_frames(loc, start, 120, 1)        # stops at the forecast's end
        sources = []
        for t, (c, kind, hh) in plan:
            hit = loc.find(c, kind, hh)
            if hit:
                sources.append((S.fname(c, kind, hh), hit[0], hit[1]))
            else:
                print("  missing", S.fname(c, kind, hh))
        if not sources:
            raise SystemExit("No model files available.")
        fc = [s[0] for s in sources if ".fields.f" in s[0]]
        run_note = ("forecast run " + fc[0].split(".")[2] + " " + fc[0].split(".")[1][1:3] + "Z"
                    if fc else "nowcast")

    grids, data, times = {}, {}, []
    for i, (name, kind, url) in enumerate(sources, 1):
        print(f"  [{i}/{len(sources)}] {name}", flush=True)
        ds, cleanup = S.open_any(kind, url, False)
        try:
            if not grids:
                for rid, _ in SITE_REGIONS:
                    S.REGION = S.REGIONS[rid]
                    try:
                        g = S.build_grid(ds, S.REGION["box"], margin=0.004)
                    except SystemExit as e:
                        print(f"    {rid}: skipped ({e})")
                        continue
                    grids[rid] = g
                    data[rid] = {"u": [], "v": [], "z": []}
                    print(f"    {rid}: {len(g.tris)} triangles, median edge {g.med_edge_m:.0f} m")
                if not grids:
                    raise SystemExit("No areas overlap the model mesh.")
                e0 = min(g.e0 for g in grids.values())
                e1 = max(g.e1 for g in grids.values())
            u_all = S.arr(ds.variables["u"][0, 0, e0:e1]).astype(np.float32)
            v_all = S.arr(ds.variables["v"][0, 0, e0:e1]).astype(np.float32)
            for rid, g in grids.items():
                idx = g.e0 - e0 + g.eloc
                data[rid]["u"].append(np.nan_to_num(u_all[idx]))
                data[rid]["v"].append(np.nan_to_num(v_all[idx]))
                z = float(S.arr(ds.variables["zeta"][0, g.wp_node:g.wp_node + 1])[0])
                data[rid]["z"].append(z)
            times.append(S.file_time(ds))
        finally:
            ds.close()
            if cleanup:
                os.remove(cleanup)

    order = np.argsort([t.timestamp() for t in times])
    keep, seen = [], set()
    for k in order:                       # drop duplicate valid times
        ts = int(times[k].timestamp())
        if ts not in seen:
            seen.add(ts)
            keep.append(k)
    epochs = [int(times[k].timestamp()) for k in keep]

    ddir = os.path.join(a.out, "data")
    os.makedirs(ddir, exist_ok=True)
    regions_index = []
    for rid, tab in SITE_REGIONS:
        if rid not in grids:
            continue
        R, g, d = S.REGIONS[rid], grids[rid], data[rid]
        # never let the colour scale cap the data: raise it in 0.5 kn steps if needed
        peak = max(float(np.hypot(d["u"][k], d["v"][k]).max()) for k in keep) * S.MS_TO_KN
        vmax = max(R["vmax"], np.ceil(peak * 2) / 2)
        print(f"  {rid}: peak {peak:.2f} kn, scale 0-{vmax:g} kn")
        q = lambda arrs: [b64(np.clip(np.round(arrs[k] * UV_SCALE), -32767, 32767)
                              .astype("<i2")) for k in keep]
        out = {
            "id": rid, "title": R["title"], "box": R["box"], "vmax": float(vmax),
            "ref": {"lon": R["ref"][0], "lat": R["ref"][1]},
            "labels": [{"name": n, "lon": x, "lat": y, "kind": kd} for n, x, y, kd in R["labels"]],
            "lon": b64(g.lon.astype("<f4")), "lat": b64(g.lat.astype("<f4")),
            "tris": b64(g.tris.astype("<u4").ravel()),
            "uvScale": UV_SCALE, "times": epochs,
            "u": q(d["u"]), "v": q(d["v"]),
            "zetaFt": [round(d["z"][k] * S.M_TO_FT, 2) for k in keep],
            "contours": depth_contours(g),
            "meshMedianM": round(g.med_edge_m),
        }
        path = os.path.join(ddir, rid + ".json")
        with open(path, "w") as f:
            json.dump(out, f, separators=(",", ":"))
        print(f"  wrote {path} ({os.path.getsize(path) / 1e6:.2f} MB)")
        regions_index.append({"id": rid, "tab": tab, "title": R["title"]})

    cmap = plt.get_cmap("turbo")
    turbo = ["#%02x%02x%02x" % tuple(int(255 * c) for c in cmap(i / 63)[:3]) for i in range(64)]
    stamp = datetime.now(timezone.utc)
    with open(os.path.join(ddir, "regions.json"), "w") as f:
        json.dump({"regions": regions_index, "turbo": turbo, "run": run_note,
                   "updated": int(stamp.timestamp()), "stamp": stamp.strftime("%Y%m%d%H%M")}, f)
    shutil.copy(os.path.join(HERE, "web", "index.html"), os.path.join(a.out, "index.html"))
    print(f"Built {len(regions_index)} areas, {len(epochs)} hours")


if __name__ == "__main__":
    main()
