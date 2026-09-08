# Wits OC Lab · Live link observatory

A Linux application for recording and sharing the Optical Communications Lab's free-space laser-link measurements. The dashboard uses a restrained navy, gold and green palette, straight edges, and locally served assets. No Node build, cloud account, or external chart service is required.

```text
CW532-005 laser ── 150 m ── mirror ── 150 m ── PDP90A quadrant detector
                                                  │
                                           QPD breakout box
                                                  │ X / Y / SUM
                                           Hantek 6254BC
                                                  │ USB / libusb
                                          Linux PC + recorder
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
- Explicit simulation mode. Hardware errors never switch to synthetic data.

## Quick start on Linux

Python 3.12 or newer is required (for example Ubuntu 24.04). On Ubuntu/Pop!_OS with a suitable Python version:

```sh
sudo apt update
sudo apt install python3-venv python3-dev git libusb-1.0-0
./install.sh
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

`config.example.toml` selects **hardware** mode. Connect X to CH1, Y to CH2, and SUM to CH3. Close other scope applications before starting; only one process can claim the interface. The service will keep serving the dashboard while retrying an unavailable scope.

For another machine on the local network, pass `--host 0.0.0.0` and browse to the acquisition PC's IP address on port 8000. For public access use HTTPS as described below.

## Acquisition and scientific interpretation

### Bursts, rates and timestamps

The upstream driver supports four-channel **burst acquisition**, not gapless streaming. The defaults are 25,000 samples/s/channel, 4,096 samples/capture (163.84 ms), and a minimum 0.5 s interval between capture starts. USB transfer and processing may extend this interval. High sample rates increase the bandwidth within a burst, not the dashboard update rate. At the defaults the nominal waveform coverage is at most about 33% of wall time, before additional delays.

- `host_started_utc` / `host_started_ns`: host time just before invoking acquisition (including connection setup on the first attempt).
- `host_received_utc` / `host_received_ns`: host time immediately after the driver returns the capture.
- `sample_time_s`: time relative to the first sample in that burst, `sample_index / sample_rate_hz`. It is **not** an absolute UTC sample timestamp or a time across concatenated bursts.
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

Position statistics exclude samples where corrected SUM is below `minimum_sum_v`, or where **any mapped X/Y/SUM channel** reaches the driver's ADC rails (wire code ≤28 or ≥227). No valid samples produces `null` in JSON and empty position/RMS fields in CSV, never a false zero. A capture with some valid samples reports statistics over that valid subset alongside its valid/clipped/low-SUM fractions. The clipping flag only detects scope rail saturation, not saturation inside the detector or breakout box.

`wander_rms = sqrt(var(position_x) + var(position_y))` within each burst, relative to that burst's centroid. Until calibrated its unit is ratio (or V for the voltage model). The scatter plot shows the sequence of **burst centroids**; its spread is a different quantity from the within-burst RMS. Neither measurement automatically establishes atmospheric turbulence strength, scintillation index or Cn².

Before research collection, check the driver against a known voltage reference and the scope's calibration output, choose suitable channel ranges, check DC offsets/no clipping, and measure the detector/breakout position calibration at your measurement plane. The [driver's documented limitations](https://github.com/maccelarimatt/Hantek6254BC-Linux-Driver#current-limitations) apply. Configuration changes require a restart, creating a new session with new metadata.

### Supported scope settings

Sample rates: 25 k, 50 k, 125 k, 250 k, 500 k, 1.25 M, 2.5 M, 5 M, 12.5 M, 25 M, 50 M, 125 M and 250 M samples/s. Depth: 4,096, 8,192 or 16,384 samples/channel. Each channel's volts/div must be one of `0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10`. The application uses auto trigger for unattended capture. Probe factors must match the physical probes; direct BNC connections use 1.

## Data files, exports and reuse

The archive is `data_dir/recordings.sqlite3` (SQLite WAL mode). Each capture atomically commits its summary, a compressed NPZ waveform and session counters before it is published to viewers. SQLite uses `synchronous=FULL`; operating-system/filesystem/hardware durability still matters. Interrupted sessions are marked on restart; saved captures are retained. A process lock prevents multiple writers to the same data directory. Use a local disk, not a shared NFS filesystem.

| Download | Contents |
| --- | --- |
| `summary.csv` | One row per capture: source, IDs, host timestamps, acquisition timing, voltage means/std, position, radial RMS and quality fractions. |
| `raw.csv.gz` | One row per sample: capture ID, source, host timestamps, relative sample time, all four probe-corrected voltages and all four raw wire codes. Opens with pandas or gzip. |
| `metadata.json` | Session counts/state, driver revision, software version, channel mapping, acquisition settings, calibration, experiment description and reuse terms. |

Active-session downloads are bounded at the highest committed capture ID when that request starts; the response header `X-Export-Through-Capture-ID` identifies the boundary. Separate downloads of an active session can have different boundaries. Use a completed session for a matching summary/raw/metadata set. Exports stream in bounded batches without keeping a long-lived SQLite read transaction; at most two exports run at once (HTTP 429 otherwise).

```python
import pandas as pd

base = "https://YOUR-DOMAIN/api/sessions/SESSION-ID"
summary = pd.read_csv(base + "/summary.csv")
raw = pd.read_csv(base + "/raw.csv.gz", compression="gzip")
# Analyse separate bursts; do not assume adjacent capture IDs are contiguous samples.
first_burst = raw[raw.capture_id == raw.capture_id.iloc[0]]
```

Set `[experiment].data_license` and `contact` to your lab-approved research reuse terms before publishing. Public downloads do not themselves grant a particular licence; the default explicitly leaves terms unspecified. Metadata may be public, so include only information you intend to share. The visual identity is inspired by the [Wits OC Lab](https://www.wits.ac.za/oclab/); this application does not include or invent an official university crest.

Disk usage depends heavily on noise/compressibility, sample depth and capture frequency. At the defaults an uncompressed capture is roughly 180 KiB including time arrays; 172,800 captures/day would be about 30 GiB/day before compression and database overhead. Measure your actual database growth and provision space accordingly. Sessions rotate hourly, **the database file does not rotate**. Session rotation is organisational, not a retention policy. The recorder pauses below the configured free-space reserve and resumes when space is available. Nothing is automatically deleted. Store the database on a suitably sized volume and monitor it.

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

Interactive schema: `/docs`. All application endpoints are read-only; there are no public capture controls, deletion endpoints or settings mutations.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | Public experiment and acquisition configuration; excludes host and disk path. |
| `GET /api/live` | Latest committed capture preview and recorder state. |
| `GET /api/events` | Server-sent `snapshot` events every second, with recent capture summaries; bounded to 100 connections/process. |
| `GET /api/health` | HTTP 200 while recording fresh data, 503 while starting/disconnected/stale or unable to save. Demo is explicitly identified. |
| `GET /api/history?limit=600` | Latest summaries, oldest first; limit 1–7,200. |
| `GET /api/sessions?limit=20&offset=0` | Paginated archive, newest first; maximum page size 100. |
| `GET /api/sessions/{id}/summary.csv` | Capture summaries. |
| `GET /api/sessions/{id}/raw.csv.gz` | Full sample export. |
| `GET /api/sessions/{id}/metadata.json` | Session metadata. |

If USB fails, the website stays up and the last values are visibly marked stale/paused. If the browser loses the server, it reports connection loss rather than assuming recording stopped. The service journal contains the underlying USB/disk errors; public status messages omit local paths.

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
4. Install the provided udev rule, reload udev, and reconnect the scope as above. Install/start the service:

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

Tests exercise the driver API adapter, channel/probe/calibration mathematics, invalid and clipped samples, lossless export counts/values, snapshot boundaries, recording recovery, USB failure/retry, failed persistence, disk reserve, session rotation and single-writer locking. Hardware acquisition requires acceptance testing on your actual Hantek device; passing simulation tests does not establish voltage/position calibration.

The frontend is plain HTML/CSS/JavaScript with canvas plots. Runtime files live in `src/oclab/`; `config.example.toml` documents operator settings, and `deploy/` contains the optional service, USB permissions and proxy templates.

Optional browser checks start an isolated demo server, exercise live data, chart controls, downloads and offline/reconnect behaviour, and save desktop/mobile screenshots under `test-results/`:

```sh
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_smoke.py
# Or use an existing Chrome installation:
# .venv/bin/python tests/browser_smoke.py --browser /opt/google/chrome/chrome
```
