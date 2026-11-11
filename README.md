# Wits OC Lab · Live link observatory

A Linux application for recording and sharing the Optical Communications Lab's free-space laser-link measurements. The dashboard uses a restrained navy, gold and green palette, straight edges, and locally served assets. No Node build, cloud account, or external chart service is required.

```text
CW532-005 laser ── 150 m ── mirror ── 150 m ── PDP90A quadrant detector
                                                  │
                                           QPD breakout box
                                                  │ X / Y / SUM
                                           Hantek 6254BC
                                                  │ USB / libusb
                                          Linux PC + recorder ── USB serial ── Pico W weather station
                                             ├── SQLite archive
                                             └── HTTPS dashboard + downloads
```

The Linux PC must have direct USB access to the scope. The same PC serves the website; a reverse proxy makes it accessible over HTTPS. A website hosted on a separate cloud server cannot directly access the scope's USB port. Use the setup below on the acquisition PC, or supply a secure network route from your hosting server to it.

## What is included

- Direct integration with [maccelarimatt/Hantek6254BC-Linux-Driver](https://github.com/maccelarimatt/Hantek6254BC-Linux-Driver), pinned to commit `be5663df4f0a93e96c039071f9502c698da678a1`.
- CH1 → X, CH2 → Y, CH3 → SUM, configurable in TOML. All four acquired channels are archived.
- A responsive dashboard with live voltage means, within-burst radial wander RMS, signal history, waveform previews, a centroid scatter plot, connection health, and a paginated archive.
- Automatic recording without opening a browser; automatic USB reconnect; explicit low-disk and failed-write states; hourly session rotation.
- Durable SQLite transactions containing **every sample in every successfully recorded capture**, upstream ADC wire codes, host UTC timestamps, acquisition timing, and experiment/calibration metadata.
- Public CSV summaries, compressed raw CSV downloads, metadata JSON, and a documented read-only API.
- A Raspberry Pi Pico W weather station (cup anemometer, BMP280, DHT11, light sensor) recorded continuously in its own thread, with wind statistics and environment values attached to every capture.
- Explicit simulation mode. Hardware errors never switch to synthetic data.

## Quick start on Linux

On the acquisition PC, one command sets everything up and starts recording:

```sh
./run.sh            # or: ./run.sh --demo   ./run.sh --setup-only   ./run.sh --flash   ./run.sh --help
```

It is safe to run every time. It finds or installs Python 3.12+ (deadsnakes on Ubuntu/Pop!_OS), builds or repairs `.venv` when the dependencies change, creates `config.local.toml` if missing, installs the scope and Pico udev rules and `oclab`/`dialout` group membership (listing each `sudo` command first; new groups are applied with `sg`, so no logout is needed), checks that the Pico is streaming and offers to install its firmware if it is silent, then starts the app. Extra options such as `--host 0.0.0.0` pass through to `oclab`. The manual steps below do the same things individually.

Python 3.12 or newer is required (for example Ubuntu 24.04). On Ubuntu/Pop!_OS with a suitable Python version:

```sh
sudo apt update
sudo apt install python3-venv python3-dev git libusb-1.0-0
./install.sh
```

On Ubuntu/Pop!_OS 22.04, whose `python3` is 3.10, install Python 3.12 alongside it and point the installer at it:

```sh
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install python3.12 python3.12-venv python3.12-dev
PYTHON=python3.12 ./install.sh
```

The installer uses your system Python, including when launched from a Flatpak editor. It preserves an incompatible existing `.venv` in a timestamped `.venv.backup-*` directory before rebuilding. It is safe to run again to repair a missing package or stale launcher. Run it from your normal Linux terminal if your editor cannot access the host.

`requirements.lock` contains the tested runtime dependency versions, including the pinned driver. Alternatively, `.venv/bin/python -m pip install -e .` resolves versions within the ranges in `pyproject.toml` in an existing compatible environment.

If the launcher reports `ModuleNotFoundError: No module named 'oclab'`, run `./install.sh` again. This can happen when a virtual environment is created inside an editor/container with a different Python version from the terminal used to start the application. Do not share one virtual environment between those different runtimes.

Preview the entire application without hardware:

```sh
.venv/bin/oclab --mode demo --data-dir ./data/demo
```

Open **http://127.0.0.1:8000**. Demo recordings are synthetic and labelled in the dashboard, database and exports. Stop with Ctrl+C. Use separate data directories for demo and research recordings.

To use the oscilloscope:

```sh
cp config.example.toml config.local.toml
sudo groupadd -f oclab
sudo usermod -aG oclab "$USER"
sudo install -m 0644 deploy/70-oclab-hantek.rules /etc/udev/rules.d/70-oclab-hantek.rules
sudo udevadm control --reload-rules
```

Log out and back in so your new group membership takes effect, then unplug/reconnect the oscilloscope. Check device access:

```sh
.venv/bin/hantek6254bc doctor
.venv/bin/oclab --config config.local.toml
```

For the Pico W weather station, add yourself to `dialout` (the group that owns `/dev/ttyACM*`) and optionally install its udev rule, which also stops ModemManager briefly grabbing the port on Ubuntu/Pop!_OS:

```sh
sudo usermod -aG dialout "$USER"
sudo install -m 0644 deploy/71-oclab-pico-weather.rules /etc/udev/rules.d/71-oclab-pico-weather.rules
sudo udevadm control --reload-rules
```

Log out and back in for the group change, then replug the Pico. **Close Thonny (or any serial monitor) before starting**: it holds the port open and the recorder will report the station as unavailable. The firmware must be saved **on the Pico as `main.py`** (MicroPython only runs `main.py` automatically at power-up); a Pico without it, or one stopped by Thonny, sits at the `>>>` prompt and sends nothing, which the dashboard reports as "port open but silent". Replug the Pico after closing Thonny so `main.py` starts.

`config.example.toml` selects **hardware** mode. Connect X to CH1, Y to CH2, and SUM to CH3. Close other scope applications before starting; only one process can claim the interface. The service will keep serving the dashboard while retrying an unavailable scope.

For another machine on the local network, pass `--host 0.0.0.0` and browse to the acquisition PC's IP address on port 8000. For public access use HTTPS as described below.

## Acquisition and scientific interpretation

### Bursts, rates and timestamps

The upstream driver supports four-channel **burst acquisition**, not gapless streaming. The example configuration is set up for turbulence: 25,000 samples/s/channel within each burst, 16,384 samples per burst (0.655 s) and back-to-back captures (`capture_interval_s = 0`). Between bursts there is a gap for USB transfer and re-arming; the dashboard shows the measured **time coverage** (burst time / wall time), and `minutes.csv` reports it per minute. Processing takes about 1–2 % of each burst, so the gaps come from the scope link, not the PC.

### Operating frequency and anti-alias filtering

The scope cannot sample below 25 kS/s, so each burst is low-pass filtered on the PC and decimated to the **operating frequency**, `[scope].store_rate_hz` (default 2,500 samples/s per channel). Set it in the configuration or at start-up with `./run.sh --store-rate HZ` (and the scope rate with `--scope-rate HZ`); a change starts a new session with new metadata.

- `store_rate_hz` must divide the scope `sample_rate` (for 25 kS/s: 25000, 12500, 6250, 5000, 3125, 2500, 1250, 1000, 625, 500, ...). Setting it equal to `sample_rate` stores every scope sample unfiltered.
- The filter is a linear-phase Kaiser FIR: flat (±0.01 dB) to **0.4 × store_rate_hz**, at least 60 dB down from 0.5 × store_rate_hz, so nothing above the output Nyquist frequency aliases into the data. Usable bandwidth is therefore 0.4 × the operating frequency: 2,500 Sa/s gives 1 kHz, enough for content to 500 Hz with margin. For content up to *f*, choose `store_rate_hz` ≥ 2.5 *f*.
- Only fully filtered outputs are kept: (taps − 1) scope samples per burst are not stored (29 ms of 655 ms at 2,500 Sa/s). Stored sample *k* is at `t0_s + k / store_rate_hz` after the burst's first scope sample; `t0_s` is half the filter length.
- Averaging also reduces scope quantisation noise, so stored values keep sub-code resolution (int16, 1/64 ADC code).
- `clipped` marks stored samples whose filter window included an ADC-rail sample on X, Y or SUM.
- Each capture's `scintillation_index` = var(SUM)/mean(SUM)² over the valid stored samples. It is band-limited to the operating bandwidth and includes detector and scope noise; it is not by itself Cn².

Per-capture voltage means, position and wander statistics are still computed from every scope sample before decimation.

- `host_started_utc` / `host_started_ns`: host time just before invoking acquisition (including connection setup on the first attempt).
- `host_received_utc` / `host_received_ns`: host time immediately after the driver returns the capture.
- `sample_time_s`: time relative to the first scope sample in that burst, `t0_s + sample_index / store_rate_hz` (schema 3; `sample_index / sample_rate_hz` for older sessions). It is **not** an absolute UTC sample timestamp or a time across concatenated bursts.
- `acquisition_elapsed_s` and `since_previous_start_s`: monotonic host timing for acquisition latency and the interval from the previous successfully saved capture start. Failed attempts therefore leave an observable gap.

Synchronise the PC clock with NTP. The scope supplies no GPS/PPS or hardware UTC time through this driver. Do not concatenate raw files as if bursts were contiguous, or infer exact sample UTC times by adding relative time to a host timestamp. Spectral/turbulence analysis must account for sampling gaps, detector bandwidth, scope settings and calibration.

The browser receives updates every second, including the newly saved capture summaries. History retains up to 7,200 summaries in the browser; it displays the selected window within the current session so different calibration sessions are not mixed. The latest-burst preview keeps at most 256 points and can miss narrow peaks. **Downloads retain all waveform samples**, regardless of preview reduction. A reconnect replays up to 600 recent summaries; reload the page to fetch a longer history from disk.

### Voltage, position and wander

Archived `ch*_v` values are the driver's input voltages multiplied by the configured probe factor. Raw wire codes are archived unchanged. The voltage metrics are means and population standard deviations over the entire capture, including flagged samples. **SUM is not calibrated optical power in watts.**

The default position model assumes the breakout outputs unnormalised differential X and Y signals:

```text
corrected_sum = SUM_V - sum_offset_v
x_ratio = (X_V - x_offset_v) / corrected_sum
y_ratio = (Y_V - y_offset_v) / corrected_sum
x_mm = x_ratio * x_scale_mm     # only when both position scales are supplied
y_mm = y_ratio * y_scale_mm
```

Some breakout electronics already normalise their position outputs. In that case set `position_model = "voltage"`; the app uses offset-corrected X/Y directly and the optional scale is **mm/V**, rather than mm per unit ratio. Confirm the actual breakout circuit before interpreting position data. Offsets are in probe-corrected volts. Negative scales reverse an axis.

Position statistics exclude samples where corrected SUM is below `minimum_sum_v`, where **any mapped X/Y/SUM channel** reaches the driver's ADC rails (wire code ≤28 or ≥227), or, when `saturation_v` is set, where the detector itself is saturated (SUM above it, or |X| or |Y| above it; `saturated_fraction` per capture). For the Thorlabs PDP90A, set `saturation_v = 4.0` and use 1 V/div on all channels, which matches its ±4 V (X, Y) and 0–4 V (SUM) outputs; its position is x = Lx·ΔX/(2·SUM) with Lx = Ly = 10 mm, so `x_scale_mm = y_scale_mm = 5.0` with the difference-over-sum model. No valid samples produces `null` in JSON and empty position/RMS fields in CSV, never a false zero. A capture with some valid samples reports statistics over that valid subset alongside its valid/clipped/low-SUM fractions. The clipping flag only detects scope rail saturation, not saturation inside the detector or breakout box.

`wander_rms = sqrt(var(position_x) + var(position_y))` within each burst, relative to that burst's centroid. Until calibrated its unit is ratio (or V for the voltage model). The scatter plot shows the sequence of **burst centroids**; its spread is a different quantity from the within-burst RMS. Neither measurement automatically establishes atmospheric turbulence strength, scintillation index or Cn².

Before research collection, check the driver against a known voltage reference and the scope's calibration output, choose suitable channel ranges, check DC offsets/no clipping, and measure the detector/breakout position calibration at your measurement plane. The [driver's documented limitations](https://github.com/maccelarimatt/Hantek6254BC-Linux-Driver#current-limitations) apply. Configuration changes require a restart, creating a new session with new metadata.

### Weather station

A Raspberry Pi Pico W streams newline-terminated ASCII over USB CDC serial (VID 0x2E8A, 115200 baud). The firmware is [firmware/pico_weather/main.py](firmware/pico_weather/main.py) and its line format is fixed; the application only reads it:

```text
# pico-weather v2.1 wind=10Hz anemo=4-20mA shunt=165.0ohm range=30.0m/s light=ldr baro=BMP280 dht=yes
W,t_ms,v_adc,i_ma,wind_ms,clip                                  # 10 Hz
E,t_ms,dht_c,dht_rh,baro_hpa,baro_c,light_v,light_x[,baro_rh]   # 1 Hz; baro_rh (BME280 humidity) from firmware v3.1
```

- Firmware v3.0 pins: wind GP26 (4–20 mA across 165 Ω), LDR GP28 (LDR to 3V3, 1 kΩ to AGND; `light_x` = LDR ohms), BMP280/BME280 on I2C0 SDA GP20 at 0x76, and SCL/DHT11 on GP21/GP17, detected at start-up so either way round works. Constants (range, shunt, `LIGHT_MODE`, `R_LIGHT`) are at the top of `main.py`. Loop current below 3.6 mA (open loop) gives an empty wind field. The header is repeated every 60 s so a late-connecting host still records it.
- To install or update the firmware, close the app and Thonny, then copy it to the Pico as `main.py` and reset (Thonny: **File → Save as → Raspberry Pi Pico**, or `pip install mpremote` then `mpremote cp firmware/pico_weather/main.py :main.py + reset`).
- The reader runs in its own thread, separate from the scope. A scope USB failure does not stop weather logging, and a serial failure does not stop scope capture. Weather writes are covered by the same single-writer lock.
- The port is auto-detected by USB VID unless `[weather].port` is set (prefer a `/dev/serial/by-id/...` path if several RP2040 boards are attached). Unplugging, a closed port or 10 s without data (twice `stale_after_s`, at least 5 s) triggers automatic reconnection every `retry_interval_s`.
- Time basis: `host_received_ns` is `time.time_ns()` when each line is received, so it includes USB/serial buffering latency. The Pico `t_ms` uptime counter is kept as an auxiliary column; it wraps and is not UTC.
- Empty fields (missing sensor or failed read) are stored as null, never 0. Malformed or partial lines, which are common at connect time, are skipped and counted (`counters.malformed` in `/api/weather/live`).
- `# key=value` header lines are parsed and stored in `weather_headers` and the session metadata. The Pico prints its header at boot, so it is null if the Pico started before the recorder connected; reset the Pico to record it.
- Samples are kept in memory for live use and committed in one SQLite transaction about once per second. Writes pause below the disk reserve; up to about 10 minutes of unsaved samples are held, then the oldest are dropped and counted.
- In hardware mode weather data is never fabricated: an absent station gives `weather_status = "missing"` and null fields. Demo mode uses a labelled synthetic station (`source = "demo"`).

Each capture summary gains weather fields computed from samples with `host_received_ns − association_window_s < host time ≤ host_received_ns` of that capture:

| Field | Meaning |
| --- | --- |
| `wind_mean_ms`, `wind_std_ms`, `wind_max_ms`, `wind_n` | Mean, population std, maximum and count of non-null wind samples in the window. |
| `wind_ti` | Turbulence intensity `std/mean`; null when the mean is below 1 m/s. |
| `taylor_ok` | Mean ≥ 1 m/s and TI < 0.5 (a rough frozen-turbulence check). |
| `air_temp_c`, `pressure_hpa` | BMP280 temperature (the air-temperature reference) and station pressure, not reduced to sea level. |
| `dht_temp_c` | DHT11 temperature (about ±2 °C). |
| `rh_pct`, `rh_source` | Relative humidity: the DHT11 (±5 %RH) when it has a reading, otherwise the BME280 (±3 %RH, `baro_rh`). The DHT11 cannot measure below about 20 %RH and then reports nothing. `rh_source` records which sensor was used. |
| `light_v`, `ldr_ohm` | Light-sensor voltage and the firmware's `light_x` (LDR ohms with `light=ldr`, photocurrent µA with `light=bpw34`). |
| `weather_age_s` | Age of the environment sample used, relative to the capture. |
| `weather_status` | `ok` (wind and environment both within `stale_after_s`), `stale`, `missing` or `disabled`. |

**Limitations.** The cup anemometer has a distance constant of about 2–3 m, so its readings are smoothed and wind statistics are **not turbulence-resolving**; they describe mean conditions near the link, not the optical turbulence along it. **Cn² is not measured** by any of these sensors. The station measures at a single point. Light is an uncalibrated relative level. Session metadata (`schema_version` 2) records the header, sensor list, association rule and these caveats.

### Scope zero-level calibration

When the scope is opened, the driver reads the unit's own zero-level and gain tables from its EEPROM over USB. If that read fails, the driver silently uses generic values; on this unit that shifted **every** channel by about +40 ADC codes (+1.2 V at 1 V/div), so X ≈ Y ≈ SUM and a centred beam appeared in the top-right corner. The app therefore checks that the zero-level table loaded, with a value for every channel at its configured range, and reopens the scope up to five times if not. If it still fails, nothing is recorded and the dashboard says why; it retries every `retry_interval_s`. A missing gain table is logged as a warning only.

Leave the spare channel (CH4 by default) unconnected: each capture records its mean as `unused_channel_mean_v`, and the dashboard warns when it is more than 3 ADC codes from zero, which reveals a shared offset immediately.

### Supported scope settings

Sample rates: 25 k, 50 k, 125 k, 250 k, 500 k, 1.25 M, 2.5 M, 5 M, 12.5 M, 25 M, 50 M, 125 M and 250 M samples/s. Depth: 4,096, 8,192 or 16,384 samples/channel. Each channel's volts/div must be one of `0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10`. The application uses auto trigger for unattended capture. Probe factors must match the physical probes; direct BNC connections use 1.

## Data files, exports and reuse

The archive is `data_dir/recordings.sqlite3` (SQLite WAL mode). Each capture atomically commits its summary, a compressed NPZ waveform and session counters before it is published to viewers. SQLite uses `synchronous=FULL`; operating-system/filesystem/hardware durability still matters. Interrupted sessions are marked on restart; saved captures are retained. A process lock prevents multiple writers to the same data directory. Use a local disk, not a shared NFS filesystem.

| Download | Contents |
| --- | --- |
| `summary.csv` | One row per capture: source, IDs, host timestamps, acquisition timing, voltage means/std, position, radial RMS, quality fractions and, from schema version 2, the weather fields above (appended after the original columns). |
| `raw.csv.gz` | One row per stored sample: capture ID, source, host timestamps, relative sample time, all four probe-corrected voltages (filtered and decimated to the operating frequency) and the `clipped` flag. Sessions recorded before schema 3 contain every scope sample and the four raw wire codes instead. Opens with pandas or gzip. |
| `minutes.csv` | One row per UTC minute: captures, covered seconds and time coverage, mean and median scintillation index, mean wander RMS, SUM, valid fraction, wind, air temperature, humidity and pressure. |
| `session.npz` | The whole session for analysis in NumPy: `codes` (captures × 4 × samples, int16; volts = codes × `volts_per_count`), `clipped`, capture IDs and host times, `t0_s`, `store_rate_hz`, and per-minute mean one-sided spectra (`psd_freq_hz`, `psd_intensity` of SUM/mean, `psd_x`, `psd_y`) from fully valid bursts. A `readme` array describes the contents. About 80 MB per hour at the defaults; sessions over 1.5 GB are refused (keep hourly sessions). |
| `weather.csv` | Every wind (`kind=W`) and environment (`kind=E`) sample from the session start minus `association_window_s` to the session end, in host time order. Includes host timestamps, source, Pico `t_ms`, parsed fields and the raw line; fields that do not apply to a row, and missing sensor values, are empty. |
| `metadata.json` | Session counts/state, driver revision, software version, channel mapping, acquisition settings, calibration, experiment description and reuse terms. |

Active-session downloads are bounded at the highest committed capture ID when that request starts; the response header `X-Export-Through-Capture-ID` identifies the boundary. Separate downloads of an active session can have different boundaries. Weather exports for an active session end at the newest committed weather row; the headers `X-Export-Weather-From-NS` and `X-Export-Weather-Through-NS` give the boundaries. Weather is a continuous time series not tied to sessions, so adjacent sessions' weather exports overlap by the association window. Use a completed session for a matching summary/raw/metadata set. Exports stream in bounded batches without keeping a long-lived SQLite read transaction; at most two exports run at once (HTTP 429 otherwise).

```python
import pandas as pd

base = "https://YOUR-DOMAIN/api/sessions/SESSION-ID"
summary = pd.read_csv(base + "/summary.csv")
raw = pd.read_csv(base + "/raw.csv.gz", compression="gzip")
minutes = pd.read_csv(base + "/minutes.csv")
weather = pd.read_csv(base + "/weather.csv")
wind = weather[weather.kind == "W"]
# Analyse separate bursts; do not assume adjacent capture IDs are contiguous samples.
first_burst = raw[raw.capture_id == raw.capture_id.iloc[0]]
```

Set `[experiment].data_license` and `contact` to your lab-approved research reuse terms before publishing. Public downloads do not themselves grant a particular licence; the default explicitly leaves terms unspecified. Metadata may be public, so include only information you intend to share. The visual identity is inspired by the [Wits OC Lab](https://www.wits.ac.za/oclab/); this application does not include or invent an official university crest.

Disk usage depends on the operating frequency, signal noise and time coverage. At 2,500 Sa/s with four channels, a realistically noisy signal measured about 12 KiB per 0.655 s burst including database overhead: **at most about 1.7 GB/day** even with no gaps, and less in practice. Doubling the operating frequency roughly doubles this. Sessions recorded before schema 3 stored every scope sample as 64-bit floats (about 10 times more). Measure your actual database growth and provision space accordingly. Sessions rotate hourly, **the database file does not rotate**. Session rotation is organisational, not a retention policy. The recorder pauses below the configured free-space reserve and resumes when space is available. Nothing is automatically deleted. Store the database on a suitably sized volume and monitor it.

For a live backup use SQLite's backup API, not a standalone copy of the `.sqlite3` file while the recorder is writing:

```sh
.venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect("data/recordings.sqlite3") as source:
    with sqlite3.connect("recordings-backup.sqlite3") as destination:
        source.backup(destination, pages=256)
PY
```

Use a separate disk for backups where possible. To move the archive, stop the recorder first and move the entire data directory, including any `-wal`/`-shm` files. Point `data_dir` at the new location before restarting. There is no automatic pruning or remote replication.

## API

Interactive schema: `/docs`. Apart from the logging switch and session deletion below, all endpoints are read-only; there are no settings changes.

### Exploring and deleting recordings

The dashboard's **Explore recorded data** panel plots any saved session: **Over time** shows up to six per-capture variables (voltages, position, wander, scintillation, quality fractions, wind, temperature, humidity, pressure, light), one panel each on a shared UTC axis; **Y against X** plots one against another. Long sessions are averaged into at most 2,000 points (the shaded band spans each point's minimum to maximum); averaging never crosses a break, and breaks over 5 minutes (PC asleep, app stopped) are collapsed and labelled with their length. Hover for exact values; **PNG ↓** and **CSV ↓** save the plotted values. Each archive row has a **Plot** link.

On the acquisition PC (or with the control token), each archive row also has a checkbox and **Delete selected** permanently removes those sessions and their captures after confirmation. A session that is still recording cannot be deleted; stop logging first. Weather samples are a continuous time series shared with neighbouring sessions and are kept. SQLite reuses the freed space for new recordings; to shrink the file itself, stop the app and run `.venv/bin/python -c "import sqlite3; sqlite3.connect('data/recordings.sqlite3').execute('VACUUM')"`.

### Logging switch

The dashboard's **Logging** switch (in the status strip) starts and stops saving data. With logging off the scope and weather station keep running and the dashboard shows live data, but nothing is written to disk; this is useful while aligning the laser. Switching on starts a new session, and also saves the weather samples buffered in the previous association window, so the first capture's weather window is complete in the archive. Switching off closes the session as complete; stopping asks for confirmation. `record_on_start` sets the state at start-up (`false` in the example configuration); the switch is not remembered across restarts.

Who may switch logging:

- A browser **on the acquisition PC** connecting directly (loopback, no proxy headers) needs nothing else.
- Through the HTTPS proxy, the switch is locked unless `control_token` (16+ characters) is set in the configuration; the dashboard then asks for the token once per browser tab. Without a token, remote visitors only see the switch's state. The token is never included in `/api/config` or session metadata.
- Every change must carry the `X-OCLab-Control: 1` header and, when present, a same-site `Origin`, so other websites cannot switch logging from a visitor's browser.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | Public experiment and acquisition configuration; excludes host and disk path. |
| `GET /api/live` | Latest capture preview (saved, or live-only with `capture_id: null` while logging is off), recorder state including `logging`, and a `weather` block. |
| `GET /api/events` | Server-sent `snapshot` events every second, with recent capture summaries and weather status; bounded to 100 connections/process. |
| `GET /api/health` | HTTP 200 while the scope delivers fresh data (whether or not logging is on; see `logging`), 503 while starting/disconnected/stale or unable to save. Demo is explicitly identified. A separate `weather` block reports the station; it does not affect the status code. |
| `GET /api/control` | Whether this browser may use the logging switch, and the current logging state. |
| `GET /api/plot-fields` | Variables the explorer can plot, with labels and units. |
| `GET /api/sessions/{id}/series?fields=a,b&points=2000` | Per-capture values for 1–6 variables, binned to at most `points` (mean, min, max per bin); nulls stay null. |
| `DELETE /api/sessions/{id}` | Permanently deletes a finished session and its captures (409 while recording). Same requirements as `POST /api/logging`. |
| `POST /api/logging` | `{"enabled": true|false}` starts or stops saving. Requires `X-OCLab-Control: 1` and either a direct connection on the acquisition PC or `X-Control-Token`. |
| `GET /api/weather/live` | Latest W and E values, port, state, staleness, ages, last-second wind mean/max and line/error counters. |
| `GET /api/weather/history?seconds=300` | Wind in 1 s bins (mean, max, count) and every environment sample; 1–3,600 s. |
| `GET /api/history?limit=600` | Latest summaries, oldest first; limit 1–7,200. |
| `GET /api/sessions?limit=20&offset=0` | Paginated archive, newest first; maximum page size 100. |
| `GET /api/sessions/{id}/summary.csv` | Capture summaries. |
| `GET /api/sessions/{id}/raw.csv.gz` | Full stored-sample export. |
| `GET /api/sessions/{id}/minutes.csv` | Per-minute coverage, scintillation and weather. |
| `GET /api/sessions/{id}/session.npz` | Whole session as NumPy arrays with per-minute spectra (schema 3+; 409 for older sessions, 413 if over 1.5 GB). |
| `GET /api/sessions/{id}/weather.csv` | All weather samples for the session's time span. |
| `GET /api/sessions/{id}/metadata.json` | Session metadata. |

If USB fails, the website stays up and the last values are visibly marked stale/paused. The weather panel is marked stale independently of the scope. If the browser loses the server, it reports connection loss rather than assuming recording stopped. The service journal contains the underlying USB/disk errors; public status messages omit local paths.

## Run at boot and publish over HTTPS

These are deployment instructions, not actions performed automatically by the application. Use an acquisition PC with reliable power/network access and the USB scope attached. The application must run with **exactly one worker**; do not use Uvicorn `--reload`, multiple workers, or multiple service instances against the same scope.

1. Place this repository in `/opt/oclab` (for example, clone it there). Create its virtual environment **at that path** using the quick-start install commands; virtual environments are not portable between paths.
2. Create a service user and data/config directories:

   ```sh
   sudo groupadd -f oclab
   sudo useradd --system --gid oclab --home-dir /var/lib/oclab --no-create-home --shell /usr/sbin/nologin oclab
   sudo install -d -o oclab -g oclab /var/lib/oclab
   sudo install -d -m 0755 /etc/oclab
   sudo install -m 0644 /opt/oclab/config.example.toml /etc/oclab/config.toml
   ```

   If the `oclab` service user already exists, skip `useradd`. Ensure `/opt/oclab` and its virtual environment are readable/executable by the service user.

3. Edit `/etc/oclab/config.toml`: set `data_dir = "/var/lib/oclab"`, `mode = "hardware"`, and keep `host = "127.0.0.1"`. Set calibration, scope ranges and research reuse/contact information.
4. Install the provided udev rules (scope and, optionally, Pico), reload udev, and reconnect both devices as above. The service unit adds the `dialout` supplementary group so the service user can open the Pico's serial port. Install/start the service:

   ```sh
   sudo install -m 0644 /opt/oclab/deploy/oclab.service /etc/systemd/system/oclab.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now oclab
   sudo journalctl -u oclab -f
   ```

5. Install [Caddy for your Linux distribution](https://caddyserver.com/docs/install). Point a real DNS hostname to this PC's public address, arrange inbound TCP 80/443 and university network/router forwarding where needed, and substitute the hostname in `deploy/Caddyfile`. Use that site's block in `/etc/caddy/Caddyfile`, validate, then reload Caddy. Caddy automatically obtains certificates once DNS/network access is correct. The proxy forwards the event stream without buffering.
6. Verify `https://YOUR-DOMAIN/`, `/api/health`, an archive download, service restart, and unplug/replug recovery using the real instrument before unattended collection. Do not expose the data directory as a static web directory. Keep the application bound to loopback behind Caddy.

Public hosting needs your real domain and network access; those are not configured by this repository. If the university connection is behind CGNAT or blocks inbound traffic, arrange an approved reverse tunnel or university reverse proxy. A separate cloud collector/ingest protocol is not included.

## Development and verification

```sh
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Tests exercise the weather line parser (including empty, malformed and partial lines), weather association windows, serial disconnect/reconnect over a fake source and a real pty, weather exports, the driver API adapter, channel/probe/calibration mathematics, invalid and clipped samples, lossless export counts/values, snapshot boundaries, recording recovery, USB failure/retry, failed persistence, disk reserve, session rotation and single-writer locking. Hardware acquisition requires acceptance testing on your actual Hantek device; passing simulation tests does not establish voltage/position calibration.

The frontend is plain HTML/CSS/JavaScript with canvas plots. Runtime files live in `src/oclab/`; `config.example.toml` documents operator settings, and `deploy/` contains the optional service, USB permissions and proxy templates.

Optional browser checks start an isolated demo server, exercise live data, chart controls, downloads and offline/reconnect behaviour, and save desktop/mobile screenshots under `test-results/`:

```sh
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_smoke.py
# Or use an existing Chrome installation:
# .venv/bin/python tests/browser_smoke.py --browser /opt/google/chrome/chrome
```
