# Overnight laser-link recording, 19 to 20 November 2026

A 13-hour continuous recording from the Wits Optical Communications Lab free-space laser link in Johannesburg, with weather measured at the station. Unlike the night of 11 to 12 November, the position detector gave usable beam positions for almost the whole dark part of the night. Read the limitations below before using it, in particular the barometer dropouts.

| | |
|---|---|
| Recording | 2026-11-19 18:04:01 to 2026-11-20 07:15:10 UTC (20:04 to 09:15 local, SAST = UTC+2) |
| Captures | 66,133 bursts in 14 hourly sessions (the last one 11 minutes long), no gaps; bursts cover about 91 % of the time |
| Link | 532 nm continuous-wave laser, 150 m to a mirror and 150 m back (300 m path) |
| Detector | Thorlabs PDP90A lateral-effect position sensor: X, Y and SUM outputs |
| Digitiser | Hantek 6254BC USB oscilloscope, 8-bit, 25 kS/s, 16,384 samples per burst (0.655 s), 1 V/div |
| Stored data | Each burst low-pass filtered and decimated to 2,500 samples/s per channel (0 to 1 kHz band), 1,602 samples per burst |
| Weather | Raspberry Pi Pico W station: cup anemometer (10 Hz), BMP280 pressure and temperature, DHT11 humidity, light sensor (1 Hz) |

Recorder settings, calibration and weather-station firmware (v3.2) were identical to the night of 11 to 12 November.

## Limitations

- **Detector saturation.** The PDP90A saturates when SUM exceeds 4 V. Across the whole recording 56 % of samples were saturated. Before first light, 98 % of captures (43,190 of 44,009) have position and scintillation values, but SUM sat close to the limit: about 46 % of samples were saturated between 18:16 and 00:00 UTC and 17 % between 00:00 and 02:51 UTC. From 03:22 UTC daylight saturated the detector completely and no positions exist after that.
- **Start of the night.** For the first 12 minutes (18:04 to about 18:16 UTC) SUM was mostly below 0.5 V, so the beam was largely off the detector or blocked, presumably while the link was being set up. Positions and wander from these minutes are not representative.
- **Biased positions.** Where a capture was partly saturated, position and scintillation use only its unsaturated samples, which are the moments of weaker signal. Treat them as indicative, especially before 00:00 UTC.
- **Daylight.** From first light (about 02:51 UTC) ambient light adds to the detector signal and pulls the measured position toward the centre. No optical filter was fitted.
- **Barometer dropouts.** From 21:12 UTC the BMP280 intermittently failed to read: 5,740 of 47,475 environment samples (12 %) have empty pressure and BMP280 temperature, mostly between 00:30 and 02:30 UTC and between 03:30 and 05:00 UTC. Seven further readings are corrupted and kept as recorded: 790.94 hPa (21:12:33), 1035.81 hPa with 193.3 °C (01:02:51), −71.63 hPa (03:36:08), −101.44 hPa with 173.46 °C (03:36:29), −92.42 hPa (04:02:22), 1036.48 hPa (04:14:27) and 953.34 hPa (04:22:25 UTC). Nine captures in `captures.csv.gz` carry one of these values; discard pressure outside about 815 to 845 hPa or temperature above 50 °C. The graphs leave them out. The previous night had no dropouts, so suspect the sensor's I²C wiring.
- **8-bit digitiser.** One scope step is 32 mV; stored values keep sub-step resolution from the filtering. One step on X or Y is about 0.04 mm of position at SUM = 4 V.
- **Position scale** is from the PDP90A manual (10 mm sensor length, so 5 mm per unit of X/SUM). It has not been checked against a translation stage.
- **Bursts are not contiguous.** There is a gap of about 0.06 s between bursts. Do not concatenate bursts as one continuous series.
- **Timestamps** are host-computer UTC times (NTP-synchronised) when each burst was requested and received. There is no hardware timestamp.
- **Anemometer.** A cup anemometer responds slowly (distance constant about 2 to 3 m), so wind statistics do not resolve turbulence. The zero was measured (3.72 mA at rest); the 30 m/s full-scale range is the sensor's nominal value and has not been checked against a reference.
- **Humidity** is from a DHT11 (±5 %RH), which is unreliable below 20 %RH; readings fell below that from about 07:00 UTC.
- **Temperature and light** are measured at the station, at one point. Light is an uncalibrated relative level.
- **Cn² is not measured** by any of these instruments.

## Files

| File | Contents | Size |
|---|---|---|
| `waveforms/<session>.h5` | Every stored sample of every capture, one file per hourly session (14 files) | 292 MB |
| `captures.csv.gz` | One row per capture: timestamps, voltage statistics, position, wander, scintillation index, quality fractions and the weather associated with that capture | 12 MB |
| `weather.csv.gz` | Every weather sample: `kind` = `W` (wind, 10 Hz) or `E` (environment, 1 Hz), including the raw line from the station | 13 MB |
| `sessions.json` | Session records with the full settings, calibration and weather-station metadata | 0.1 MB |
| `plots/` | Five overview graphs of the night | 2 MB |
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

with h5py.File("waveforms/20261120T010405Z-3f892cbe.h5") as f:
    volts = f["codes"][...] * f.attrs["volts_per_count"][:, None]   # (captures, 4, 1602)
    clipped = f["clipped"][...]
    rate, t0 = f.attrs["store_rate_hz"], f.attrs["t0_s"]

x, y, total = volts[:, 0], volts[:, 1], volts[:, 2]
valid = (total >= 0.5) & (total <= 4.0) & ~clipped
x_mm = np.where(valid, 5 * x / np.where(valid, total, 1), np.nan)

captures = pd.read_csv("captures.csv.gz")
weather = pd.read_csv("weather.csv.gz")
wind = weather[weather.kind == "W"]
env = weather[weather.kind == "E"]
env = env[env.baro_hpa.between(815, 845)]   # drop the corrupted barometer readings
```

MATLAB (`h5read`), Julia (`HDF5.jl`) and R (`rhdf5`) read the same files.

## How this was produced

The recorder and its documentation are in this repository; the README there describes the acquisition, the anti-alias filter and every column. This folder was written by:

```sh
python tools/export_dataset.py --data-dir data --out datasets/2026-11-19_night --from 2026-11-19T17:00 --to 2026-11-20T08:00
```

The graphs in `plots/` show one-minute values over the whole night.

## Reuse

Reuse terms have not been specified. Contact the Wits Optical Communications Lab before reusing or redistributing this data.
