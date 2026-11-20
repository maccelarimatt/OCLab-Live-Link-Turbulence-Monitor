"""Animated GIF of the beam moving on the detector plane over an exported night.

Usage (the commands that produced the committed GIFs):
    python tools/animate_beam_wander.py datasets/2026-11-11_night
    python tools/animate_beam_wander.py datasets/2026-11-19_night --exclude-before 2026-11-19T18:16Z

Needs numpy, pandas, h5py, matplotlib and pillow. Writes <dataset>/plots/09_beam_wander.gif.

The frames are spread evenly over the captures that have a beam position (same rule as
plot_wander_weather.py: >= 50 unsaturated stored samples), not over clock time, so long saturated
stretches are skipped rather than shown as empty frames; the clock and the timeline say where each
frame sits in the night. Each frame shows every 16th stored position sample (2500/16 = 156 per
second) of its captures as a grey cloud, and the centre of every capture so far as a dot coloured
by time of night.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_wander_weather import (GRID, INK, INK2, MUTED, SURFACE, exclude_before, local,  # noqa: E402
                                 night_label, style, wander_from_waveforms)

SAMPLES_EVERY = 16


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--exclude-before", metavar="UTC", help="drop positions before this UTC time")
    ap.add_argument("--seconds", type=float, default=10.0, help="length of one loop (default 10)")
    ap.add_argument("--fps", type=int, default=20)
    args = ap.parse_args()
    folder = args.dataset
    style()

    print("Reading positions from the waveforms…", file=sys.stderr)
    wan, xs, ys = wander_from_waveforms(folder, samples_every=SAMPLES_EVERY)
    exclude_before(wan, args.exclude_before)
    t_all = local(wan.host_started_ns)
    ok = wan.centroid_x_mm.notna().to_numpy()
    order = np.argsort(wan.host_started_ns.to_numpy()[ok], kind="stable")
    idx = np.flatnonzero(ok)[order]
    t = t_all[idx]
    cx, cy = wan.centroid_x_mm.to_numpy()[idx], wan.centroid_y_mm.to_numpy()[idx]
    sx, sy = xs[idx], ys[idx]

    E = pd.read_csv(folder / "weather.csv.gz", usecols=["kind", "host_received_ns", "light_v"])
    E = E[E.kind == "E"]
    light = pd.Series(E.light_v.to_numpy(), index=local(E.host_received_ns)).resample("1min").mean()
    dawn = light[(light > 0.1) & (light.index.hour < 12)].index[0]

    # Square view around where the beam actually went (0.2-99.8 percentile of all samples).
    px, py = sx[np.isfinite(sx)], sy[np.isfinite(sy)]
    lo = np.array([np.percentile(px, 0.2), np.percentile(py, 0.2)])
    hi = np.array([np.percentile(px, 99.8), np.percentile(py, 99.8)])
    mid, half = (lo + hi) / 2, (hi - lo).max() / 2 * 1.12

    # Timeline: per-minute radial wander over the whole recording.
    rad = pd.Series(wan.wander_radial_mm.to_numpy(), index=t_all).resample("1min").median()
    t0, t1 = t_all.min(), t_all.max()
    tmap = matplotlib.colors.ListedColormap(plt.get_cmap("plasma")(np.linspace(0.0, 0.85, 256)))
    norm = matplotlib.colors.Normalize(mdates.date2num(t0), mdates.date2num(t1))
    cnum = mdates.date2num(t.to_pydatetime())

    # ---- figure, built once; each frame updates artists
    fig = plt.figure(figsize=(7.2, 9.0), dpi=100)
    ax = fig.add_axes([0.13, 0.29, 0.82, 0.555])
    tl = fig.add_axes([0.13, 0.085, 0.82, 0.115])
    cbar = fig.add_axes([0.13, 0.065, 0.82, 0.013])
    fig.text(0.04, 0.978, "Beam wander on the detector", fontsize=15, color=INK, va="top")
    fig.text(0.04, 0.947, f"{night_label(t_all)}  ·  {len(t):,} captures with ≥50 unsaturated samples "
             f"({len(t) / len(t_all):.0%} of all)", fontsize=9.5, color=INK2, va="top")
    clock = fig.text(0.95, 0.982, "", fontsize=20, color=INK, va="top", ha="right")
    info = fig.text(0.04, 0.918, "", fontsize=9.5, color=INK2, va="top", linespacing=1.5)

    ax.set_xlim(mid[0] - half, mid[0] + half)
    ax.set_ylim(mid[1] - half, mid[1] + half)
    ax.set_aspect("equal")
    ax.set_xlabel("X, mm from detector centre")
    ax.set_ylabel("Y, mm from detector centre")
    trail = ax.scatter([], [], s=5, c=[], cmap=tmap, norm=norm, linewidths=0, alpha=0.7)
    cloud = ax.scatter([], [], s=3, color=INK, alpha=0.18, linewidths=0)
    here = ax.scatter([], [], s=90, facecolors="none", edgecolors=INK, linewidths=1.6)

    tl.plot(rad.index, rad.to_numpy(), color=INK2, linewidth=1)
    tl.set_xlim(t0, t1)
    tl.set_ylim(0, np.nanpercentile(rad, 99.5) * 1.15)
    tl.set_title("Radial wander (mm), 1-minute median  ·  gaps = detector saturated", fontsize=9.5, pad=4)
    tl.tick_params(labelsize=8.5)
    tl.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    tl.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    tl.tick_params(axis="x", labelbottom=False)
    tl.axvline(dawn, color=MUTED, linewidth=0.8)
    cursor = tl.axvspan(t0, t0, color=INK, alpha=0.25, linewidth=0)
    needle = tl.axvline(t0, color=INK, linewidth=1.4)
    grad = np.linspace(0, 1, 512)[None, :]
    cbar.imshow(grad, aspect="auto", cmap=tmap, extent=[mdates.date2num(t0), mdates.date2num(t1), 0, 1])
    cbar.set_xlim(mdates.date2num(t0), mdates.date2num(t1))
    cbar.set_yticks([])
    cbar.grid(False)
    cbar.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    cbar.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    cbar.tick_params(labelsize=8.5, length=0)
    for s in cbar.spines.values():
        s.set_visible(False)
    cbar.set_xlabel("Local time (SAST)  ·  dot colour = time of night", fontsize=9)

    hold = 500                                     # last frame held, inside the requested length
    step = int(round(100 / args.fps)) * 10         # GIF frame timing is in 10 ms units
    n_frames = int((args.seconds * 1000 - hold) // step) + 1
    groups = np.array_split(np.arange(len(t)), n_frames)
    frames = []
    print(f"Rendering {n_frames} frames…", file=sys.stderr)
    for k, g in enumerate(groups):
        if len(g) == 0:
            continue
        a, b = g[0], g[-1]
        trail.set_offsets(np.c_[cx[:b + 1], cy[:b + 1]])
        trail.set_array(cnum[:b + 1])
        gx, gy = sx[g].ravel(), sy[g].ravel()
        keep = np.isfinite(gx)
        cloud.set_offsets(np.c_[gx[keep], gy[keep]])
        here.set_offsets([[np.nanmean(cx[g]), np.nanmean(cy[g])]])
        clock.set_text(f"{t[b]:%H:%M} SAST")
        lines = [f"{t[a]:%H:%M} to {t[b]:%H:%M}, {len(g)} captures  ·  wander "
                 f"X {np.nanmedian(wan.wander_x_mm.to_numpy()[idx[g]]):.3f} mm, "
                 f"Y {np.nanmedian(wan.wander_y_mm.to_numpy()[idx[g]]):.3f} mm (median)",
                 "after first daylight: positions pulled toward centre" if t[b] >= dawn else ""]
        info.set_text("\n".join(lines))
        x0, x1 = mdates.date2num(t[a]), mdates.date2num(t[b])
        cursor.set_x(x0)
        cursor.set_width(x1 - x0)
        needle.set_xdata([t[b], t[b]])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=SURFACE)
        frames.append(Image.open(buf).convert("RGB"))
        if k % 50 == 0:
            print(f"  frame {k}/{n_frames}", file=sys.stderr)
    plt.close(fig)

    # One shared palette keeps colours steady from frame to frame and the file small.
    pal = frames[len(frames) // 2].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    durations = [step] * (len(q) - 1) + [hold]
    out = folder / "plots" / "09_beam_wander.gif"
    q[0].save(out, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=True)
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(q)} frames, "
          f"{sum(durations) / 1000:.1f} s)", file=sys.stderr)


if __name__ == "__main__":
    main()
