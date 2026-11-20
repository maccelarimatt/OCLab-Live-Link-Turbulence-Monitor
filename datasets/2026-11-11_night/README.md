# Overnight laser-link recording, 11 to 12 November 2026

A 12.5-hour continuous recording from the Wits Optical Communications Lab free-space laser link in Johannesburg, with weather measured at the station. Read the limitations below before using it: the position detector was saturated for most of the night.

| | |
|---|---|
| Recording | 2026-11-11 17:40:05 to 2026-11-12 06:13:38 UTC (19:40 to 08:13 local, SAST = UTC+2) |
| Captures | 63,248 bursts in 13 hourly sessions, no gaps; bursts cover about 92 % of the time |
| Link | 532 nm continuous-wave laser, 150 m to a mirror and 150 m back (300 m path) |
| Detector | Thorlabs PDP90A lateral-effect position sensor: X, Y and SUM outputs |
| Digitiser | Hantek 6254BC USB oscilloscope, 8-bit, 25 kS/s, 16,384 samples per burst (0.655 s), 1 V/div |
| Stored data | Each burst low-pass filtered and decimated to 2,500 samples/s per channel (0 to 1 kHz band), 1,602 samples per burst |
| Weather | Raspberry Pi Pico W station: cup anemometer (10 Hz), BMP280 pressure and temperature, DHT11 humidity, light sensor (1 Hz) |

## Limitations

- **Detector saturation.** The PDP90A saturates when SUM exceeds 4 V. About 74 % of all samples were saturated: almost all of 17:40 to 21:30 UTC (laser power too high) and from about 03:40 UTC onward (daylight). Position and scintillation values exist for only 40 % of captures, mostly between 21:30 and 03:30 UTC.
- **Biased positions.** Where a capture was partly saturated, position and scintillation use only its unsaturated samples, which are the moments of weaker signal. Treat them as indicative.
- **Daylight.** From first light (about 02:53 UTC) ambient light adds to the detector signal and pulls the measured position toward the centre. No optical filter was fitted.
- **8-bit digitiser.** One scope step is 32 mV; stored values keep sub-step resolution from the filtering. One step on X or Y is about 0.04 mm of position at SUM = 4 V.
- **Position scale** is from the PDP90A manual (10 mm sensor length, so 5 mm per unit of X/SUM). It has not been checked against a translation stage.
- **Bursts are not contiguous.** There is a gap of about 0.06 s between bursts. Do not concatenate bursts as one continuous series.
- **Timestamps** are host-computer UTC times (NTP-synchronised) when each burst was requested and received. There is no hardware timestamp.
- **Anemometer.** A cup anemometer responds slowly (distance constant about 2 to 3 m), so wind statistics do not resolve turbulence. The zero was measured (3.72 mA at rest); the 30 m/s full-scale range is the sensor's nominal value and has not been checked against a reference.
- **Humidity** is from a DHT11 (±5 %RH), which is unreliable below 20 %RH; readings early in the night were below that.
- **Temperature and light** are measured at the station, at one point. Light is an uncalibrated relative level.
- **Cn² is not measured** by any of these instruments.

## Files

| File | Contents | Size |
|---|---|---|
| `waveforms/<session>.h5` | Every stored sample of every capture, one file per hourly session (13 files) | 242 MB |
| `captures.csv.gz` | One row per capture: timestamps, voltage statistics, position, wander, scintillation index, quality fractions and the weather associated with that capture | 10 MB |
| `weather.csv.gz` | Every weather sample: `kind` = `W` (wind, 10 Hz) or `E` (environment, 1 Hz), including the raw line from the station | 12 MB |
| `sessions.json` | Session records with the full settings, calibration and weather-station metadata | 0.1 MB |
| `plots/` | Eight graphs of the night (01 to 08) and an animation (09), plus `wander_xy_per_capture.csv.gz` | 8 MB |
| `checksums.sha256` | SHA-256 of the data files; check with `sha256sum -c checksums.sha256` | |

All waveform data is lossless with respect to the recorder's archive: each HDF5 file was read back and compared with the archive when it was written.

### Waveform files (HDF5)

| Dataset | Shape | Meaning |
|---|---|---|
| `codes` | (captures, 4, 1602) int16 | Scope ADC codes × 64 about mid-scale, after filtering and decimation |
| `clipped` | (captures, 1602) bool | True where an X, Y or SUM sample under the filter reached the scope's limits |
| `capture_id` | (captures,) int64 | Matches `capture_id` in `captures.csv.gz` |
| `host_started_ns`, `host_received_ns` | (captures,) int64 | Host UTC time in nanoseconds when the burst was requested and received |

File attributes give `store_rate_hz` (2500), `t0_s`, `volts_per_count` (one per channel), the channel mapping (`x_channel` = 1, `y_channel` = 2, `sum_channel` = 3; channel 4 was unconnected and is a zero reference) and the full session metadata as JSON.

- Volts = `codes × volts_per_count[channel]`. No offsets are subtracted.
- Sample `k` of a capture was taken `t0_s + k / store_rate_hz` seconds after that burst's first scope sample.
- Position in mm = `5 × X / SUM` and `5 × Y / SUM`, valid only where SUM is between 0.5 V and 4.0 V and `clipped` is false.

```python
import h5py, numpy as np, pandas as pd

with h5py.File("waveforms/20261111T234006Z-99bcb89f.h5") as f:
    volts = f["codes"][...] * f.attrs["volts_per_count"][:, None]   # (captures, 4, 1602)
    clipped = f["clipped"][...]
    rate, t0 = f.attrs["store_rate_hz"], f.attrs["t0_s"]

x, y, total = volts[:, 0], volts[:, 1], volts[:, 2]
valid = (total >= 0.5) & (total <= 4.0) & ~clipped
x_mm = np.where(valid, 5 * x / np.where(valid, total, 1), np.nan)

captures = pd.read_csv("captures.csv.gz")
weather = pd.read_csv("weather.csv.gz")
wind = weather[weather.kind == "W"]
```

MATLAB (`h5read`), Julia (`HDF5.jl`) and R (`rhdf5`) read the same files.

## How this was produced

The recorder and its documentation are in this repository; the README there describes the acquisition, the anti-alias filter and every column. This folder was written by:

```sh
python tools/export_dataset.py --data-dir data --out datasets/2026-11-11_night --from 2026-11-11T17:00 --to 2026-11-12T07:00
```

The graphs in `plots/` show one-minute values over the whole night.

Graphs 01 to 05 come from the export. Graphs 06 to 09 and `plots/wander_xy_per_capture.csv.gz` were made afterwards with:

```sh
python tools/plot_wander_weather.py datasets/2026-11-11_night
python tools/animate_beam_wander.py datasets/2026-11-11_night
```

- `06_sum_wind_light.png`: SUM signal, wind speed and light level.
- `07_wander_xy_weather.png`: beam wander in X and Y separately, with every weather measurement.
- `08_wander_vs_weather.png`: wander X and Y against wind speed and air temperature, one dot per minute before first daylight, coloured by time of night.
- `09_beam_wander.gif`: a 10-second loop of the beam moving on the detector over the night.

Wander X and Y here are the standard deviations of 5·X/SUM and 5·Y/SUM within each burst, from the stored 0 to 1 kHz waveforms, for bursts with at least 50 unsaturated samples. They come out at about half the recorder's `wander_rms` (radial, full 25 kS/s band), which also includes digitiser noise up to 12.5 kHz, so do not compare the two directly.

## Reuse

Reuse terms have not been specified. Contact the Wits Optical Communications Lab before reusing or redistributing this data.
