"""Graphs of the received signal, beam wander (X and Y separately) and weather for an exported night.

Usage (the commands that produced the committed graphs):
    python tools/plot_wander_weather.py datasets/2026-11-11_night
    python tools/plot_wander_weather.py datasets/2026-11-19_night --exclude-before 2026-11-19T18:16Z

--exclude-before drops wander and centroid for captures started before that UTC time; use it for
minutes the dataset README calls unrepresentative (on 19 November, link set-up until 18:16 UTC).

Needs numpy, pandas, h5py and matplotlib. Writes into <dataset>/plots/:
    06_sum_wind_light.png         SUM signal, wind speed and light level
    07_wander_xy_weather.png      beam wander X and Y with every weather-station measurement
    08_wander_vs_weather.png      beam wander X and Y against wind speed and air temperature
and <dataset>/plots/wander_xy_per_capture.csv.gz, the per-capture X/Y wander behind them.

Wander X and Y are the population standard deviations of 5*X/SUM and 5*Y/SUM (mm) within each
burst, over stored samples with 0.5 V <= SUM <= 4.0 V that are not clipped (the rule in the
dataset README). sqrt(wanderX^2 + wanderY^2) is the radial wander, comparable with wander_rms in
captures.csv.gz, which the recorder computed from the full 25 kS/s burst.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SAST = "Africa/Johannesburg"
MM_PER_UNIT = 5.0           # PDP90A: 10 mm sensor length, 5 mm per unit of X/SUM
SUM_MIN, SUM_MAX = 0.5, 4.0
MIN_VALID = 50              # stored samples (20 ms) needed before a capture's wander is used
BIN_MIN_VALID = 10          # valid samples needed in one stretch of a capture (animation)

INK, INK2, MUTED = "#1d1d1b", "#55554f", "#8a8a84"
GRID, SURFACE = "#e6e6e1", "#fcfcfa"
C = {"x": "#2f78d8", "y": "#e8692f", "sum": "#18a878", "wind": "#6a55d8",
     "temp": "#d84848", "rh": "#2a9bb0", "pres": "#7a6a52", "light": "#c8860a"}


# ---------------------------------------------------------------- data

def wander_from_waveforms(folder: Path, bins: int = 0):
    """Per-capture X/Y wander and centroid (mm) from the stored waveforms.

    With bins > 0 also splits each capture into `bins` equal stretches of time and returns the mean
    X and Y position (mm) and mean SUM (V) of the valid samples in each, as three float32 arrays
    (captures, bins); NaN where a stretch has fewer than BIN_MIN_VALID valid samples or the capture
    has no wander value.
    """
    rows, keep = [], []
    for path in sorted((folder / "waveforms").glob("*.h5")):
        with h5py.File(path) as f:
            vpc = f.attrs["volts_per_count"]
            ix, iy, isum = (int(f.attrs[k]) - 1 for k in ("x_channel", "y_channel", "sum_channel"))
            clipped = f["clipped"][...]
            ids = f["capture_id"][...]
            start = f["host_started_ns"][...]
            n = len(ids)
            for a in range(0, n, 2000):            # chunked: a session is ~800 MB as float64
                b = min(n, a + 2000)
                codes = f["codes"][a:b]
                x = codes[:, ix] * vpc[ix]
                y = codes[:, iy] * vpc[iy]
                s = codes[:, isum] * vpc[isum]
                valid = (s >= SUM_MIN) & (s <= SUM_MAX) & ~clipped[a:b]
                safe = np.where(valid, s, 1.0)
                px = np.where(valid, MM_PER_UNIT * x / safe, np.nan)
                py = np.where(valid, MM_PER_UNIT * y / safe, np.nan)
                nv = valid.sum(axis=1)
                with np.errstate(invalid="ignore"), _quiet():
                    sx, sy = np.nanstd(px, axis=1), np.nanstd(py, axis=1)
                    cx, cy = np.nanmean(px, axis=1), np.nanmean(py, axis=1)
                for v in (sx, sy, cx, cy):
                    v[nv < MIN_VALID] = np.nan
                rows.append(pd.DataFrame({"capture_id": ids[a:b], "host_started_ns": start[a:b],
                                          "n_valid_stored": nv, "wander_x_mm": sx, "wander_y_mm": sy,
                                          "centroid_x_mm": cx, "centroid_y_mm": cy}))
                if bins:
                    w = px.shape[1] // bins
                    ps = np.where(valid, s, np.nan)
                    shape = (len(px), bins, w)
                    cnt = valid[:, :bins * w].reshape(shape).sum(axis=2)
                    bad = (cnt < BIN_MIN_VALID) | (nv < MIN_VALID)[:, None]
                    with _quiet():
                        means = [np.nanmean(v[:, :bins * w].reshape(shape), axis=2) for v in (px, py, ps)]
                    keep.append(np.stack([np.where(bad, np.nan, m) for m in means]).astype(np.float32))
        print(f"  {path.name}: {n} captures", file=sys.stderr)
    out = pd.concat(rows, ignore_index=True)
    out["wander_radial_mm"] = np.hypot(out.wander_x_mm, out.wander_y_mm)
    if bins:
        bx, by, bs = np.concatenate(keep, axis=1)
        return out, bx, by, bs
    return out


POSITION_COLUMNS = ["wander_x_mm", "wander_y_mm", "wander_radial_mm", "centroid_x_mm", "centroid_y_mm"]


def exclude_before(wan: pd.DataFrame, before: str | None) -> np.ndarray:
    """Blank position columns for captures started before `before` (UTC); returns the mask."""
    if not before:
        return np.zeros(len(wan), bool)
    cut = pd.Timestamp(before)
    cut = cut.tz_localize("UTC") if cut.tzinfo is None else cut.tz_convert("UTC")
    mask = (wan.host_started_ns < cut.value).to_numpy()
    wan.loc[mask, POSITION_COLUMNS] = np.nan
    return mask


def night_label(t: pd.DatetimeIndex) -> str:
    a, b = t.min(), t.max()
    if a.month == b.month:
        return f"night of {a.day} to {b.day} {b:%B %Y}"
    return f"night of {a.day} {a:%B} to {b.day} {b:%B %Y}"


def clean_environment(E: pd.DataFrame) -> pd.DataFrame:
    """Blank BMP280 readings that are dropouts or corrupted (pressure outside 815-845 hPa, T > 50 °C)."""
    E = E.copy()
    bad = ~E.baro_hpa.between(815, 845) | (E.baro_c > 50)
    E.loc[bad, ["baro_hpa", "baro_c"]] = np.nan
    return E


class _quiet:
    def __enter__(self):
        import warnings
        self._w = warnings.catch_warnings()
        self._w.__enter__()
        warnings.simplefilter("ignore", RuntimeWarning)

    def __exit__(self, *exc):
        self._w.__exit__(*exc)


def local(ns: pd.Series) -> pd.DatetimeIndex:
    # Naive local wall-clock times: matplotlib then labels the axis in SAST without tz juggling.
    return pd.to_datetime(ns.to_numpy(), utc=True).tz_convert(SAST).tz_localize(None)


def per_minute(t, values: pd.Series, how: str) -> pd.DataFrame:
    s = pd.Series(values.to_numpy(), index=t)
    g = s.resample("1min")
    if how == "median":
        out = pd.DataFrame({"mid": g.median(), "lo": g.quantile(0.25), "hi": g.quantile(0.75)})
    else:
        out = pd.DataFrame({"mid": g.mean(), "lo": g.min(), "hi": g.max()})
    out["n"] = g.count()
    out.loc[out.n == 0, ["mid", "lo", "hi"]] = np.nan
    return out


# ---------------------------------------------------------------- drawing

def style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.size": 10.5, "axes.titlesize": 11, "axes.titleweight": "normal",
        "axes.titlelocation": "left", "axes.titlepad": 6, "axes.titlecolor": INK,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False,
        "xtick.major.size": 0, "ytick.major.size": 0, "lines.linewidth": 1.6,
    })


def series(ax, m: pd.DataFrame, color: str, title: str, band=True):
    if band:
        ax.fill_between(m.index, m.lo, m.hi, color=color, alpha=0.2, linewidth=0)
    ax.plot(m.index, m.mid, color=color)
    ax.set_title(title)


def decorate(fig, axes, title, subtitle, dawn, xlim, night):
    for ax in axes:
        ax.axvline(dawn, color=INK2, linewidth=0.9)
        ax.set_xlim(*xlim)
        ax.xaxis.set_major_locator(mdates.HourLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[0].annotate(f"first daylight {dawn:%H:%M}", (dawn, 1.0), xycoords=("data", "axes fraction"),
                     xytext=(0, 14), textcoords="offset points", ha="center", color=INK2, fontsize=9.5)
    axes[-1].set_xlabel(f"Local time (SAST), {night}")
    h = fig.get_figheight()
    fig.text(0.035, 1 - 0.15 / h, title, fontsize=15, color=INK, va="top")
    fig.text(0.035, 1 - 0.5 / h, subtitle, fontsize=9.5, color=INK2, va="top", linespacing=1.6)
    # Leave room for the subtitle lines, then the daylight label, then the first panel title.
    fig.subplots_adjust(top=1 - (0.5 + 0.22 * (subtitle.count("\n") + 1) + 0.75) / h)


def stats_line(name, v, unit, fmt="{:.1f}"):
    v = v.dropna()
    f = lambda z: fmt.format(z)
    return f"{name}: {f(v.min())} to {f(v.max())} {unit}, mean {f(v.mean())}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--exclude-before", metavar="UTC", help="drop wander before this UTC time, e.g. 2026-11-19T18:16Z")
    args = ap.parse_args()
    folder = args.dataset
    plots = folder / "plots"
    plots.mkdir(exist_ok=True)
    style()

    print("Reading captures and weather…", file=sys.stderr)
    cap = pd.read_csv(folder / "captures.csv.gz",
                      usecols=["capture_id", "host_started_ns", "sum_mean_v", "wander_rms"])
    wx = pd.read_csv(folder / "weather.csv.gz",
                     usecols=["kind", "host_received_ns", "wind_ms", "dht_rh", "baro_hpa", "baro_c", "light_v"])
    print("Computing X/Y wander from the waveforms…", file=sys.stderr)
    wan = wander_from_waveforms(folder)
    dropped = exclude_before(wan, args.exclude_before)
    if dropped.any():
        print(f"Excluded wander for {dropped.sum()} captures before {args.exclude_before}", file=sys.stderr)
    wan.to_csv(plots / "wander_xy_per_capture.csv.gz", index=False, float_format="%.6g")
    cap = cap.merge(wan.drop(columns="host_started_ns"), on="capture_id", how="left")

    both = cap.dropna(subset=["wander_rms", "wander_radial_mm"])
    r = np.corrcoef(both.wander_rms, both.wander_radial_mm)[0, 1]
    print(f"Check: {len(both)} captures with both; radial wander from stored waveforms vs recorder "
          f"wander_rms: r = {r:.3f}, median ratio {np.median(both.wander_radial_mm / both.wander_rms):.3f}",
          file=sys.stderr)

    tc = local(cap.host_started_ns)
    W = wx[wx.kind == "W"]
    E = clean_environment(wx[wx.kind == "E"])
    tw, te = local(W.host_received_ns), local(E.host_received_ns)

    m_sum = per_minute(tc, cap.sum_mean_v, "mean")
    m_wx = per_minute(tc, cap.wander_x_mm, "median")
    m_wy = per_minute(tc, cap.wander_y_mm, "median")
    m_wind = per_minute(tw, W.wind_ms, "mean")
    m_temp = per_minute(te, E.baro_c, "mean")
    m_rh = per_minute(te, E.dht_rh, "mean")
    m_p = per_minute(te, E.baro_hpa, "mean")
    m_light = per_minute(te, E.light_v, "mean")

    # First daylight: first minute with light above 0.1 V (the definition behind the README's 02:53 UTC).
    dawn = m_light.mid[(m_light.mid > 0.1) & (m_light.index.hour < 12)].index[0]
    xlim = (min(tc.min(), tw.min()) - pd.Timedelta("5min"), max(tc.max(), tw.max()) + pd.Timedelta("5min"))
    span = f"{tc.min():%d %b %H:%M} to {tc.max():%d %b %H:%M} SAST"
    night = night_label(tc)

    # ---- 06: SUM, wind, light
    fig, axes = plt.subplots(3, 1, figsize=(13, 9.5), sharex=True)
    fig.subplots_adjust(left=0.035, right=0.985, top=0.885, bottom=0.07, hspace=0.42)
    series(axes[0], m_sum, C["sum"], "Received SUM signal, CH3 (V)")
    axes[0].axhline(4.0, color=INK2, linewidth=0.9)
    axes[0].text(1.0, 4.0, "detector limit 4 V ", transform=axes[0].get_yaxis_transform(),
                 ha="right", va="top", fontsize=9, color=INK2)
    axes[0].set_ylim(-0.1, 4.4)
    series(axes[1], m_wind, C["wind"], "Wind speed, cup anemometer (m/s)")
    axes[1].set_ylim(bottom=0)
    series(axes[2], m_light, C["light"], "Light level at the station (V, uncalibrated): higher is brighter", band=False)
    decorate(fig, axes, "Received signal, wind and light",
             f"{len(cap):,} captures, {span}  ·  line = 1-minute mean, band = lowest to highest value in that minute  ·  "
             + stats_line("wind", W.wind_ms, "m/s"), dawn, xlim, night)
    fig.savefig(plots / "06_sum_wind_light.png", dpi=150)
    plt.close(fig)

    # ---- 07: wander X, Y + weather
    have = cap.wander_x_mm.notna()
    fig, axes = plt.subplots(7, 1, figsize=(13, 17), sharex=True,
                             gridspec_kw={"height_ratios": [1.25, 1.25, 1, 1, 1, 1, 0.8]})
    fig.subplots_adjust(left=0.045, right=0.985, top=0.925, bottom=0.04, hspace=0.5)
    ymax = np.nanmax([m_wx.hi.max(), m_wy.hi.max()]) * 1.05
    series(axes[0], m_wx, C["x"], "Beam wander X within each 0.65 s burst, RMS (mm)")
    series(axes[1], m_wy, C["y"], "Beam wander Y within each 0.65 s burst, RMS (mm)")
    for ax in axes[:2]:
        ax.set_ylim(0, ymax)
    series(axes[2], m_wind, C["wind"], "Wind speed (m/s)")
    axes[2].set_ylim(bottom=0)
    gaps = "  ·  gaps = sensor dropouts" if E.baro_hpa.isna().mean() > 0.01 else ""
    series(axes[3], m_temp, C["temp"], "Air temperature, BMP280 (°C)" + gaps, band=False)
    series(axes[4], m_rh, C["rh"], "Relative humidity, DHT11 (%RH, ±5 %RH; unreliable below 20 %RH)", band=False)
    series(axes[5], m_p, C["pres"], "Station pressure, BMP280 (hPa)" + gaps, band=False)
    series(axes[6], m_light, C["light"], "Light level (V, uncalibrated)", band=False)
    on = m_wx.mid.notna()

    def during(m):
        return m.mid[on.reindex(m.index, fill_value=False)]

    decorate(fig, axes, "Beam wander in X and Y with the weather station",
             f"Wander only where the burst had ≥{MIN_VALID} unsaturated stored samples "
             f"({have.mean():.0%} of captures, 0 to 1 kHz band); blank stretches are a saturated detector"
             + (f"; link set-up before {local(pd.Series([wan.host_started_ns[dropped].max()]))[0]:%H:%M} excluded"
                if dropped.any() else "") + "\n"
             "Wander: line = 1-minute median, band = middle half of captures  ·  "
             "weather: 1-minute mean (wind band = min to max)\n"
             "Weather while wander was measured:  "
             + "   ·   ".join([stats_line("wind", during(m_wind), "m/s"),
                               stats_line("air", during(m_temp), "°C"),
                               stats_line("RH", during(m_rh), "%", "{:.0f}"),
                               stats_line("pressure", during(m_p), "hPa")]), dawn, xlim, night)
    fig.savefig(plots / "07_wander_xy_weather.png", dpi=150)
    plt.close(fig)

    # ---- 08: wander vs wind and temperature, per minute, night-time only (before first light)
    dfm = pd.DataFrame({"wx": m_wx.mid, "wy": m_wy.mid, "wind": m_wind.mid, "temp": m_temp.mid}).dropna()
    dfm = dfm[dfm.index < dawn]
    fig, axes = plt.subplots(2, 2, figsize=(13, 9.5), sharey=True)
    fig.subplots_adjust(left=0.065, right=0.885, top=0.86, bottom=0.07, hspace=0.38, wspace=0.08)
    # Colour = time of night. Plasma has monotonic lightness (readable in greyscale and for CVD);
    # the palest top end is cut so late points stay visible on the light surface.
    tmap = matplotlib.colors.ListedColormap(plt.get_cmap("plasma")(np.linspace(0.0, 0.85, 256)))
    tnum = mdates.date2num(dfm.index.to_pydatetime())
    norm = matplotlib.colors.Normalize(tnum.min(), tnum.max())
    for i, (col, name) in enumerate([("wx", "X"), ("wy", "Y")]):
        for j, (drv, label) in enumerate([("wind", "Wind speed, 1-minute mean (m/s)"),
                                          ("temp", "Air temperature, 1-minute mean (°C)")]):
            ax = axes[i, j]
            sc = ax.scatter(dfm[drv], dfm[col], c=tnum, cmap=tmap, norm=norm, s=22,
                            edgecolors=SURFACE, linewidths=0.5)   # surface ring separates overlaps
            rho = dfm[[drv, col]].rank().corr().iloc[0, 1]
            what = "wind speed" if drv == "wind" else "air temperature"
            ax.set_title(f"Wander {name} (mm) against {what}   ·   Spearman ρ = {rho:+.2f}")
            ax.set_xlabel(label)
    axes[0, 0].set_ylim(bottom=0)
    cax = fig.add_axes([0.915, 0.07, 0.016, 0.79])
    cb = fig.colorbar(sc, cax=cax)
    cb.outline.set_visible(False)
    cb.ax.yaxis.set_major_locator(mdates.HourLocator())
    cb.ax.yaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    cb.ax.invert_yaxis()                                        # earliest at the top, reading down
    cb.ax.tick_params(length=0, colors=INK2)
    cb.set_label("Time of each minute, local (SAST)", color=INK2)
    fig.text(0.035, 0.985, "Beam wander against wind and temperature", fontsize=15, color=INK, va="top")
    fig.text(0.035, 0.945, f"One dot per minute with measurable wander, before first daylight ({len(dfm)} minutes); "
             "wander = 1-minute median  ·  colour = time (scale at right)\nCorrelation only: temperature fell through the night, so it also tracks "
             "time of night  ·  a cup anemometer does not resolve turbulence",
             fontsize=9.5, color=INK2, va="top", linespacing=1.6)
    fig.savefig(plots / "08_wander_vs_weather.png", dpi=150)
    plt.close(fig)
    print(f"Wrote 06, 07 and 08 into {plots}", file=sys.stderr)


if __name__ == "__main__":
    main()
