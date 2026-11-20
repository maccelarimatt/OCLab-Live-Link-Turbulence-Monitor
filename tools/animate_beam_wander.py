"""Animated GIF of the beam tracing its path on the detector over an exported night.

Usage (the commands that produced the committed GIFs):
    python tools/animate_beam_wander.py datasets/2026-11-11_night
    python tools/animate_beam_wander.py datasets/2026-11-19_night --exclude-before 2026-11-19T18:16Z

Needs numpy, pandas, h5py, matplotlib and pillow. Writes <dataset>/plots/09_beam_wander.gif.

Each capture with a position (same rule as plot_wander_weather.py: >= 50 unsaturated stored samples)
is split into --bins equal stretches (4 by default, about 0.16 s each) and the mean position and SUM
of each stretch is drawn as a faint dot. The line traces the beam's path through those points as a
trailing running mean over --smooth seconds of data (5 by default), so it shows how the beam moved
while the dots show the faster wander around it. The line breaks where consecutive captures are more
than 5 s apart (a saturated stretch). Line and dots are coloured by received SUM (usable samples
only, 0.5 to 4 V) and fade out over --trail frames.

A cursor sweeps through the captures with a position at an even rate, so long saturated stretches
are skipped instead of shown as empty frames; the clock and the SUM timeline below say where in the
night each frame is.
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
from matplotlib.collections import LineCollection
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_wander_weather import (C, INK, INK2, MUTED, SUM_MAX, SUM_MIN, SURFACE,  # noqa: E402
                                 exclude_before, local, night_label, style, wander_from_waveforms)

MAX_JOIN_S = 5.0     # do not join captures further apart than this

# Received intensity: one green ramp (the SUM colour of the other graphs), light = weak, dark = strong.
SUM_CMAP = matplotlib.colors.LinearSegmentedColormap.from_list(
    "sum", ["#a6dcc4", C["sum"], "#063d2c"])
SUM_NORM = matplotlib.colors.Normalize(SUM_MIN, SUM_MAX)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--exclude-before", metavar="UTC", help="drop positions before this UTC time")
    ap.add_argument("--seconds", type=float, default=10.0, help="length of one loop (default 10)")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--bins", type=int, default=4, help="points per capture on the line (default 4)")
    ap.add_argument("--trail", type=float, default=6, help="frames over which the line fades (default 6)")
    ap.add_argument("--smooth", type=float, default=5.0, help="seconds of data averaged along the line (default 5)")
    args = ap.parse_args()
    folder = args.dataset
    style()

    print("Reading positions from the waveforms…", file=sys.stderr)
    wan, bx, by, bs = wander_from_waveforms(folder, bins=args.bins)
    exclude_before(wan, args.exclude_before)
    t_all = local(wan.host_started_ns)
    ok = wan.centroid_x_mm.notna().to_numpy()
    order = np.argsort(wan.host_started_ns.to_numpy()[ok], kind="stable")
    idx = np.flatnonzero(ok)[order]
    n_cap = len(idx)
    t = t_all[idx]
    ns = wan.host_started_ns.to_numpy()[idx]

    # Flatten to one point sequence: capture-major, stretch-minor.
    P = args.bins
    X, Y, S = bx[idx].ravel(), by[idx].ravel(), bs[idx].ravel()
    cap_of = np.repeat(np.arange(n_cap), P)
    finite = np.isfinite(X) & np.isfinite(Y)
    # Segment i joins point i to i+1: both finite, and same capture or close consecutive captures.
    gap_ok = np.ones(len(X) - 1, bool)
    boundary = (np.arange(1, len(X)) % P) == 0
    cap_gap = np.r_[np.diff(ns) / 1e9, np.inf]                  # gap after each capture
    gap_ok[boundary] = cap_gap[cap_of[:-1][boundary]] <= MAX_JOIN_S
    # The line: trailing mean over --smooth seconds within each run of joined captures, so it traces
    # the beam's path; the unsmoothed points are drawn as faint dots around it.
    run = np.r_[0, np.cumsum(~gap_ok)]
    w = max(1, int(round(args.smooth / np.median(np.diff(ns) / 1e9) * P)))
    roll = lambda v: pd.Series(v).groupby(run).transform(
        lambda r: r.rolling(w, min_periods=max(1, w // 3)).mean()).to_numpy()
    LX, LY, LS = roll(X), roll(Y), roll(S)
    lfin = np.isfinite(LX) & np.isfinite(LY)
    seg_ok = lfin[:-1] & lfin[1:] & gap_ok
    seg_s = (LS[:-1] + LS[1:]) / 2

    E = pd.read_csv(folder / "weather.csv.gz", usecols=["kind", "host_received_ns", "light_v"])
    E = E[E.kind == "E"]
    light = pd.Series(E.light_v.to_numpy(), index=local(E.host_received_ns)).resample("1min").mean()
    dawn = light[(light > 0.1) & (light.index.hour < 12)].index[0]

    # Square view around where the beam went (0.2-99.8 percentile of the line's points).
    lo = np.array([np.nanpercentile(X, 0.2), np.nanpercentile(Y, 0.2)])
    hi = np.array([np.nanpercentile(X, 99.8), np.nanpercentile(Y, 99.8)])
    mid, half = (lo + hi) / 2, (hi - lo).max() / 2 * 1.12

    # SUM over the whole recording, from every capture (all samples, as in graph 06).
    cap = pd.read_csv(folder / "captures.csv.gz", usecols=["capture_id", "sum_mean_v"])
    cap = wan[["capture_id"]].merge(cap, on="capture_id", how="left")
    sm = pd.Series(cap.sum_mean_v.to_numpy(), index=t_all).resample("1min")
    s_mean, s_lo, s_hi = sm.mean(), sm.min(), sm.max()
    t0, t1 = t_all.min(), t_all.max()

    # ---- figure, built once; each frame updates artists
    fig = plt.figure(figsize=(7.2, 9.0), dpi=100)
    ax = fig.add_axes([0.13, 0.295, 0.80, 0.53])
    tl = fig.add_axes([0.13, 0.07, 0.80, 0.135])
    cb = fig.add_axes([0.945, 0.07, 0.016, 0.135])
    fig.text(0.04, 0.978, "Beam wander on the detector", fontsize=15, color=INK, va="top")
    gap = np.median(np.diff(ns)) / 1e9
    fig.text(0.04, 0.947, f"{night_label(t_all)}  ·  colour = received SUM  ·  fades over ~"
             f"{args.trail * (n_cap - 1) / (args.seconds * args.fps) * gap / 60:.0f} min of data\n"
             f"line = {args.smooth:g} s running mean of position  ·  dots = {gap / P:.2f} s means",
             fontsize=9.5, color=INK2, va="top", linespacing=1.5)
    clock = fig.text(0.95, 0.982, "", fontsize=20, color=INK, va="top", ha="right")
    info = fig.text(0.04, 0.893, "", fontsize=9.5, color=INK, va="top", linespacing=1.5)

    ax.set_xlim(mid[0] - half, mid[0] + half)
    ax.set_ylim(mid[1] - half, mid[1] + half)
    ax.set_aspect("equal")
    ax.set_xlabel("X, mm from detector centre")
    ax.set_ylabel("Y, mm from detector centre")
    dots = ax.scatter([], [], s=5, linewidths=0)
    trace = LineCollection([], linewidths=2.0, capstyle="round", joinstyle="round")
    ax.add_collection(trace)
    head = ax.scatter([], [], s=70, linewidths=1.6, edgecolors=INK, zorder=3)

    tl.fill_between(s_mean.index, s_lo.to_numpy(), s_hi.to_numpy(), color=C["sum"], alpha=0.2, linewidth=0)
    tl.plot(s_mean.index, s_mean.to_numpy(), color=C["sum"], linewidth=1.1)
    tl.axhline(4.0, color=INK2, linewidth=0.8)
    tl.set_xlim(t0, t1)
    tl.set_ylim(-0.1, 4.4)
    tl.set_yticks([0, 2, 4])
    tl.set_title("Received SUM (V), 1-minute mean and range  ·  4 V = detector limit", fontsize=9.5, pad=4)
    tl.tick_params(labelsize=8.5)
    tl.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    tl.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    tl.set_xlabel("Local time (SAST)", fontsize=9)
    tl.axvline(dawn, color=MUTED, linewidth=0.8)
    window = tl.axvspan(t0, t0, color=INK, alpha=0.18, linewidth=0)
    needle = tl.axvline(t0, color=INK, linewidth=1.4)
    # Colour key on the same SUM scale as the timeline's y-axis.
    cb.imshow(np.linspace(SUM_MIN, SUM_MAX, 256)[:, None], aspect="auto", origin="lower",
              cmap=SUM_CMAP, norm=SUM_NORM, extent=[0, 1, SUM_MIN, SUM_MAX])
    cb.set_ylim(*tl.get_ylim())
    cb.set_xticks([])
    cb.yaxis.tick_right()
    cb.set_yticks([0.5, 2, 4])
    cb.tick_params(labelsize=8, length=0)
    cb.grid(False)
    for sp in cb.spines.values():
        sp.set_visible(False)

    hold = 500                                     # last frame held, inside the requested length
    step = int(round(100 / args.fps)) * 10         # GIF frame timing is in 10 ms units
    n_frames = int((args.seconds * 1000 - hold) // step) + 1
    cursor = np.linspace(0, n_cap - 1, n_frames)   # capture index at the head of the line, per frame
    per_frame = (n_cap - 1) / (n_frames - 1)
    span = args.trail * per_frame                  # captures in the visible trail
    frames = []
    print(f"Rendering {n_frames} frames, {per_frame:.0f} captures per frame…", file=sys.stderr)
    for k, c in enumerate(cursor):
        last = int(c)
        first = max(0, int(np.floor(c - span)))
        a, b = first * P, (last + 1) * P - 1           # segment index range
        segs = np.arange(a, b)
        segs = segs[seg_ok[segs]]
        age = (c - cap_of[segs]) / span                # 0 = newest, 1 = fully faded
        alpha = np.clip(1 - age, 0, 1) ** 1.6
        rgba = SUM_CMAP(SUM_NORM(seg_s[segs]))
        rgba[:, 3] = alpha
        trace.set_segments(np.stack([np.c_[LX[segs], LY[segs]], np.c_[LX[segs + 1], LY[segs + 1]]], axis=1))
        trace.set_color(rgba)
        pts = np.arange(first * P, (last + 1) * P)
        pts = pts[finite[pts]]
        d_rgba = SUM_CMAP(SUM_NORM(S[pts]))
        d_rgba[:, 3] = 0.35 * np.clip(1 - (c - cap_of[pts]) / span, 0, 1) ** 1.6
        dots.set_offsets(np.c_[X[pts], Y[pts]])
        dots.set_facecolors(d_rgba)
        hp = np.flatnonzero(lfin[first * P:(last + 1) * P])
        if len(hp):
            j = first * P + hp[-1]
            head.set_offsets([[LX[j], LY[j]]])
            head.set_facecolors([SUM_CMAP(SUM_NORM(LS[j]))])
        clock.set_text(f"{t[last]:%H:%M} SAST")
        recent = slice(max(0, last - int(np.ceil(per_frame))), last + 1)
        lines = [f"{t[recent.start]:%H:%M} to {t[last]:%H:%M}:  SUM "
                 f"{np.nanmean(S[recent.start * P:recent.stop * P]):.2f} V (usable samples)"
                 f"  ·  wander X {np.nanmedian(wan.wander_x_mm.to_numpy()[idx[recent]]):.3f} mm, "
                 f"Y {np.nanmedian(wan.wander_y_mm.to_numpy()[idx[recent]]):.3f} mm",
                 "after first daylight: positions pulled toward centre" if t[last] >= dawn else ""]
        info.set_text("\n".join(lines))
        x0, x1 = mdates.date2num(t[first]), mdates.date2num(t[last])
        window.set_x(x0)
        window.set_width(x1 - x0)
        needle.set_xdata([t[last], t[last]])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=SURFACE)
        frames.append(Image.open(buf).convert("RGB"))
        if k % 50 == 0:
            print(f"  frame {k}/{n_frames}", file=sys.stderr)
    plt.close(fig)

    # One shared palette keeps colours steady from frame to frame and the file small.
    sample = frames[:: max(1, len(frames) // 12)]
    sheet = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
    for i, f in enumerate(sample):
        sheet.paste(f, (0, i * f.height))
    pal = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    durations = [step] * (len(q) - 1) + [hold]
    out = folder / "plots" / "09_beam_wander.gif"
    q[0].save(out, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=True)
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(q)} frames, "
          f"{sum(durations) / 1000:.1f} s)", file=sys.stderr)


if __name__ == "__main__":
    main()
