"use strict";
const $ = (id) => document.getElementById(id);
const fmt = (n, digits = 3) => Number.isFinite(n) ? n.toFixed(digits) : "—";
const count = (n) => Number(n).toLocaleString("en-ZA");
const utc = (value) => new Date(value).toISOString().slice(11, 19);
const colours = ["#4778aa", "#b68042", "#2d8068"];
let config, latest, history = [], plotMode = "history", windowSeconds = 300, archiveOffset = 0;
let lastEventAt = 0, connection, signalPoints = [], signalBounds, archiveBusy = false;
let weather, windHistory = [];
const ago = (s) => Number.isFinite(s) ? `${fmt(s, s < 10 ? 1 : 0)} s ago` : "no data";

async function fetchJSON(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function addHistory(items) {
  const byId = new Map(history.map((s) => [s.capture_id, s]));
  for (const item of items) byId.set(item.capture_id, item);
  history = [...byId.values()].sort((a, b) => a.capture_id - b.capture_id).slice(-7200);
}

function renderSnapshot(snapshot) {
  const status = snapshot.status;
  lastEventAt = Date.now();
  const healthy = status.recording;
  document.body.classList.toggle("stale", !healthy);
  $("status-dot").className = `dot ${healthy ? "live" : "error"}`;
  $("status-dot").closest(".status-strip").classList.toggle("error", !healthy);
  $("connection-label").textContent = healthy ? (status.mode === "demo" ? "Demo running" : "Link recording") : status.state === "starting" ? "Connecting" : "Recording paused";
  $("status-message").textContent = status.message;
  $("mode-banner").hidden = status.mode !== "demo";
  $("record-state").textContent = healthy ? "Saving to disk" : "Recording paused";
  if (snapshot.latest) {
    latest = snapshot.latest;
    addHistory(snapshot.captures || [latest.summary]);
    const s = latest.summary;
    for (const axis of ["x", "y", "sum"]) {
      $(`${axis}-value`).textContent = fmt(s[`${axis}_mean_v`]);
      $(`${axis}-std`).textContent = `σ ${fmt(s[`${axis}_std_v`])} V`;
    }
    $("wander-value").textContent = fmt(s.wander_rms, 4);
    $("position-unit").textContent = s.position_unit;
    $("position-calibration").textContent = s.position_unit === "mm" ? "Calibrated" : "Uncalibrated";
    $("valid-fraction").textContent = `${fmt(s.valid_fraction * 100, 1)}%`;
    $("quality-note").textContent = s.clipped_fraction > 0 ? `${fmt(s.clipped_fraction * 100, 1)}% clipped · check ranges` : s.low_sum_fraction > 0 ? `${fmt(s.low_sum_fraction * 100, 1)}% below SUM threshold` : "No clipping or low-SUM flags";
    $("last-update").textContent = `${healthy ? "Updated" : "Last capture"} ${utc(s.host_received_utc)} UTC · ${fmt(status.last_capture_age_s, 0)} s ago`;
    $("centroid").textContent = `X ${fmt(s.position_x)} / Y ${fmt(s.position_y)}`;
    if (s.weather_status) {
      $("weather-capture").textContent = `Last capture: weather ${s.weather_status} · wind ${fmt(s.wind_mean_ms, 1)} ± ${fmt(s.wind_std_ms, 1)} m/s (n ${s.wind_n}) · TI ${fmt(s.wind_ti, 2)} · ${fmt(s.air_temp_c, 1)} °C`;
    }
  } else {
    $("last-update").textContent = "No capture yet";
  }
  if (snapshot.weather) renderWeather(snapshot.weather);
  drawCharts();
}

function renderWeather(w) {
  weather = w;
  const env = w.env || {}, wind = w.wind || {}, last = w.wind_last_1s;
  const live = w.enabled && !w.stale && w.state === "receiving";
  $("weather").classList.toggle("stale", !live);
  $("weather-tag").textContent = !w.enabled ? "DISABLED" : w.source === "demo" ? "SYNTHETIC · DEMO" : w.port ? `PICO W · ${w.port.split("/").pop()}` : "PICO W · NOT FOUND";
  $("weather-tag").classList.toggle("warn", w.enabled && !live);
  $("wx-wind").textContent = fmt(last ? last.mean_ms : wind.wind_ms, 1);
  $("wx-wind-note").textContent = wind.clip === 1 ? "ADC clipped · wind above range" : last ? `1 s mean · max ${fmt(last.max_ms, 1)} · ${last.n} samples` : "Latest sample";
  $("wx-air").textContent = fmt(env.baro_c, 1);
  // DHT11 humidity when it has one; otherwise BME280 (the DHT11 cannot read below about 20 %RH).
  const dhtRh = Number.isFinite(env.dht_rh), baroRh = Number.isFinite(env.baro_rh);
  $("wx-rh").textContent = fmt(dhtRh ? env.dht_rh : env.baro_rh, dhtRh ? 0 : 1);
  $("wx-rh-note").textContent = dhtRh ? `DHT11 · ${fmt(env.dht_c, 0)} °C · ±5 %RH` : baroRh ? `BME280 · ±3 %RH · DHT11 ${Number.isFinite(env.dht_c) ? "below range" : "no reading"}` : "DHT11 / BME280 · no reading";
  $("wx-pressure").textContent = fmt(env.baro_hpa, 1);
  $("wx-light").textContent = fmt(env.light_v, 2);
  $("wx-light-note").textContent = w.header?.light === "bpw34" ? `BPW34 · ${fmt(env.light_x, 1)} µA` : `LDR · ${Number.isFinite(env.light_x) ? fmt(env.light_x / 1000, 1) + " kΩ" : "—"}`;
  const skipped = w.counters?.malformed ? ` · ${count(w.counters.malformed)} malformed lines skipped` : "";
  $("weather-status").textContent = w.enabled ? `${w.message} · wind ${ago(w.wind_age_s)} · environment ${ago(w.env_age_s)}${skipped}` : w.message;
  if (last && (!windHistory.length || last.t_ns / 1e9 > windHistory[windHistory.length - 1].t + 0.5)) {
    windHistory.push({t: last.t_ns / 1e9, mean: last.mean_ms, max: last.max_ms});
    if (windHistory.length > 3600) windHistory = windHistory.slice(-3600);
  }
}

function offline() {
  document.body.classList.add("stale");
  $("status-dot").className = "dot error";
  $("status-dot").closest(".status-strip").classList.add("error");
  $("connection-label").textContent = "Connection lost";
  $("status-message").textContent = "Reconnecting to the service. Displayed values may be out of date.";
  $("record-state").textContent = "Recorder status unknown";
  $("weather").classList.add("stale");
  $("weather-status").textContent = "Weather status unknown while reconnecting to the service.";
}

function connect() {
  if (connection) connection.close();
  connection = new EventSource("/api/events");
  connection.addEventListener("snapshot", (event) => {
    try { renderSnapshot(JSON.parse(event.data)); } catch (error) { console.error(error); offline(); }
  });
  connection.onerror = offline;
}

function surface(id) {
  const canvas = $(id), rect = canvas.getBoundingClientRect(), scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * scale);
  canvas.height = Math.round(rect.height * scale);
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.font = "9px Arial";
  return {ctx, w: rect.width, h: rect.height};
}

function selectedHistory() {
  const end = history.length ? history[history.length - 1].host_received_ns / 1e9 : Date.now() / 1000;
  return history.filter((s) => s.source === config?.mode && s.session_id === latest?.summary.session_id && s.host_received_ns / 1e9 >= end - windowSeconds);
}

function drawSignal() {
  const {ctx, w, h} = surface("signal-chart");
  const margin = {l: 46, r: 17, t: 20, b: 31};
  const width = w - margin.l - margin.r, height = h - margin.t - margin.b;
  let points, xMin, xMax;
  if (plotMode === "waveform" && latest) {
    const p = latest.preview;
    points = p.time_s.map((t, i) => ({t: t * 1000, values: [p.x_v[i], p.y_v[i], p.sum_v[i]]}));
    xMin = 0; xMax = latest.summary.burst_duration_s * 1000;
  } else {
    points = selectedHistory().map((s) => ({t: s.host_received_ns / 1e9, values: [s.x_mean_v, s.y_mean_v, s.sum_mean_v], gap: s.since_previous_start_s, wind: s.wind_mean_ms}));
    xMax = points.length ? points[points.length - 1].t : Date.now() / 1000;
    xMin = xMax - windowSeconds;
  }
  signalPoints = points;
  signalBounds = {left: margin.l, width, min: xMin, max: xMax};
  $("signal-empty").hidden = points.length > 0;
  $("chart-count").textContent = plotMode === "waveform" ? `${points.length} preview points` : `${count(points.length)} captures`;
  let low = 0, high = 0;
  for (const point of points) for (const v of point.values) { low = Math.min(low, v); high = Math.max(high, v); }
  const padding = Math.max((high - low) * 0.13, 0.1);
  low -= padding; high += padding;
  const X = (t) => margin.l + (t - xMin) / (xMax - xMin) * width;
  const Y = (v) => margin.t + (high - v) / (high - low) * height;
  ctx.fillStyle = "#819097"; ctx.textAlign = "left"; ctx.fillText("V", 10, 12);
  for (let i = 0; i <= 4; i++) {
    const v = low + (high - low) * i / 4, y = Y(v);
    ctx.strokeStyle = "#e9eded"; ctx.beginPath(); ctx.moveTo(margin.l, y); ctx.lineTo(w - margin.r, y); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "right"; ctx.fillText(v.toFixed(2), margin.l - 9, y + 3);
  }
  for (let i = 0; i <= 4; i++) {
    const t = xMin + (xMax - xMin) * i / 4, x = X(t);
    ctx.strokeStyle = "#f0f2f1"; ctx.beginPath(); ctx.moveTo(x, margin.t); ctx.lineTo(x, h - margin.b); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center";
    ctx.fillText(plotMode === "waveform" ? `${t.toFixed(1)} ms` : utc(t * 1000), x, h - 10);
  }
  for (let channel = 0; channel < 3; channel++) {
    ctx.strokeStyle = colours[channel]; ctx.lineWidth = 1.5; ctx.beginPath();
    points.forEach((point, i) => {
      const hasGap = plotMode === "history" && i > 0 && (point.t - points[i - 1].t > Math.max(3 * (config?.capture_interval_s || 0.5), 2));
      if (!i || hasGap) ctx.moveTo(X(point.t), Y(point.values[channel]));
      else ctx.lineTo(X(point.t), Y(point.values[channel]));
    });
    ctx.stroke();
    if (points.length) {
      const p = points[points.length - 1]; ctx.fillStyle = colours[channel]; ctx.beginPath(); ctx.arc(X(p.t), Y(p.values[channel]), 2.5, 0, Math.PI * 2); ctx.fill();
    }
  }
}

function drawWander() {
  const {ctx, w, h} = surface("wander-chart");
  const points = selectedHistory().filter((s) => Number.isFinite(s.position_x) && Number.isFinite(s.position_y)).slice(-1800);
  $("wander-empty").hidden = points.length > 0;
  const side = Math.min(w - 76, h - 53), left = (w - side) / 2 + 8, top = 18;
  let extent = latest?.summary.position_unit === "mm" ? 0.1 : 0.05;
  for (const p of points) extent = Math.max(extent, Math.abs(p.position_x) * 1.15, Math.abs(p.position_y) * 1.15);
  const X = (v) => left + (v / extent + 1) * side / 2;
  const Y = (v) => top + (1 - v / extent) * side / 2;
  for (let i = -2; i <= 2; i++) {
    const v = extent * i / 2;
    ctx.strokeStyle = i === 0 ? "#cfd8d7" : "#eaf0ee"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(X(v), top); ctx.lineTo(X(v), top + side); ctx.moveTo(left, Y(v)); ctx.lineTo(left + side, Y(v)); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "center"; ctx.fillText(v.toFixed(2), X(v), top + side + 15);
    ctx.textAlign = "right"; ctx.fillText(v.toFixed(2), left - 7, Y(v) + 3);
  }
  ctx.fillStyle = "#819097"; ctx.textAlign = "left"; ctx.fillText(`Y (${latest?.summary.position_unit || "ratio"})`, left, 10);
  ctx.textAlign = "center"; ctx.fillText(`X (${latest?.summary.position_unit || "ratio"})`, left + side / 2, top + side + 29);
  points.forEach((p, i) => {
    ctx.globalAlpha = 0.12 + (i / points.length) * 0.55;
    ctx.fillStyle = "#2d8068"; ctx.beginPath(); ctx.arc(X(p.position_x), Y(p.position_y), 2, 0, Math.PI * 2); ctx.fill();
  });
  ctx.globalAlpha = 1;
  if (points.length) {
    const p = points[points.length - 1]; ctx.strokeStyle = "#1e624f"; ctx.fillStyle = "#2d8068";
    ctx.beginPath(); ctx.arc(X(p.position_x), Y(p.position_y), 3.5, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(X(p.position_x), Y(p.position_y), 7, 0, Math.PI * 2); ctx.stroke();
  }
}

function drawWind() {
  const {ctx, w, h} = surface("wind-chart");
  const margin = {l: 46, r: 17, t: 20, b: 31};
  const width = w - margin.l - margin.r, height = h - margin.t - margin.b;
  const xMax = windHistory.length ? windHistory[windHistory.length - 1].t : Date.now() / 1000, xMin = xMax - windowSeconds;
  const points = windHistory.filter((p) => p.t >= xMin);
  $("wind-empty").hidden = points.length > 0;
  let high = 5;
  for (const p of points) high = Math.max(high, p.max * 1.15);
  const X = (t) => margin.l + (t - xMin) / (xMax - xMin) * width;
  const Y = (v) => margin.t + (high - v) / high * height;
  ctx.fillStyle = "#819097"; ctx.textAlign = "left"; ctx.fillText("m/s", 10, 12);
  for (let i = 0; i <= 4; i++) {
    const v = high * i / 4, y = Y(v);
    ctx.strokeStyle = "#e9eded"; ctx.beginPath(); ctx.moveTo(margin.l, y); ctx.lineTo(w - margin.r, y); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "right"; ctx.fillText(v.toFixed(1), margin.l - 9, y + 3);
  }
  for (let i = 0; i <= 4; i++) {
    const t = xMin + (xMax - xMin) * i / 4, x = X(t);
    ctx.strokeStyle = "#f0f2f1"; ctx.beginPath(); ctx.moveTo(x, margin.t); ctx.lineTo(x, h - margin.b); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center"; ctx.fillText(utc(t * 1000), x, h - 10);
  }
  for (const [key, colour, lineWidth] of [["max", "#9fb3bf", 1], ["mean", "#3f6177", 1.5]]) {
    ctx.strokeStyle = colour; ctx.lineWidth = lineWidth; ctx.beginPath();
    points.forEach((p, i) => {
      // Missing seconds (disconnects) are gaps, never interpolated.
      if (!i || p.t - points[i - 1].t > 3) ctx.moveTo(X(p.t), Y(p[key])); else ctx.lineTo(X(p.t), Y(p[key]));
    });
    ctx.stroke();
  }
  if (points.length) {
    const p = points[points.length - 1]; ctx.fillStyle = "#3f6177"; ctx.beginPath(); ctx.arc(X(p.t), Y(p.mean), 2.5, 0, Math.PI * 2); ctx.fill();
  }
}

function drawCharts() { drawSignal(); drawWander(); drawWind(); }

function cell(row, content, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  if (content instanceof Node) td.append(content); else td.textContent = content;
  row.append(td); return td;
}

async function loadArchive() {
  if (archiveBusy) return;
  archiveBusy = true;
  $("refresh-archive").disabled = true;
  try {
    const data = await fetchJSON(`/api/sessions?limit=8&offset=${archiveOffset}`);
    const body = $("sessions-body"); body.replaceChildren();
    if (!data.items.length) {
      const row = document.createElement("tr");
      cell(row, "No recordings yet. Sessions appear when the first waveform is saved.", "empty-table").colSpan = 6; body.append(row);
    }
    for (const item of data.items) {
      const row = document.createElement("tr");
      const started = cell(row, new Date(item.started_utc).toISOString().slice(0, 19).replace("T", " "));
      const id = document.createElement("small"); id.textContent = item.id; started.append(id);
      cell(row, item.mode === "demo" ? "Demo" : "Hardware", `table-source ${item.mode}`);
      cell(row, item.status[0].toUpperCase() + item.status.slice(1), `session-state ${item.status}`);
      cell(row, count(item.capture_count)); cell(row, count(item.sample_count));
      const links = document.createElement("div"); links.className = "download-links";
      for (const [file, label] of [["summary.csv", "CSV ↓"], ["raw.csv.gz", "Raw ↓"], ["weather.csv", "Weather ↓"], ["metadata.json", "Metadata ↓"]]) {
        const a = document.createElement("a"); a.href = `/api/sessions/${encodeURIComponent(item.id)}/${file}`; a.textContent = label;
        a.setAttribute("aria-label", `Download ${file} for session ${item.id}`); a.download = `${item.id}-${file}`; links.append(a);
      }
      cell(row, links); body.append(row);
    }
    $("archive-count").textContent = data.total ? `${archiveOffset + 1}–${archiveOffset + data.items.length} of ${count(data.total)} sessions · times in UTC` : "0 sessions";
    $("previous-page").disabled = archiveOffset === 0;
    $("next-page").disabled = archiveOffset + data.items.length >= data.total;
  } catch (error) {
    $("archive-count").textContent = "Could not load the archive. Use Refresh archive to retry.";
  } finally { archiveBusy = false; $("refresh-archive").disabled = false; }
}

function setPlot(mode) {
  plotMode = mode;
  for (const [id, selected] of [["trend-tab", mode === "history"], ["waveform-tab", mode === "waveform"]]) {
    $(id).classList.toggle("selected", selected); $(id).setAttribute("aria-pressed", String(selected));
  }
  $("signal-caption").textContent = mode === "history" ? "Capture means · time in UTC · gaps are not interpolated" : "Reduced preview · full waveform retained in archive";
  $("chart-tooltip").hidden = true; drawCharts();
}

$("trend-tab").onclick = () => setPlot("history");
$("waveform-tab").onclick = () => setPlot("waveform");
$("time-window").onchange = (event) => { windowSeconds = Number(event.target.value); drawCharts(); };
$("refresh-archive").onclick = loadArchive;
$("previous-page").onclick = () => { if (!archiveBusy) { archiveOffset = Math.max(0, archiveOffset - 8); loadArchive(); } };
$("next-page").onclick = () => { if (!archiveBusy) { archiveOffset += 8; loadArchive(); } };
$("signal-chart").addEventListener("pointermove", (event) => {
  if (!signalPoints.length || !signalBounds) return;
  const x = event.clientX - event.target.getBoundingClientRect().left;
  const t = signalBounds.min + (x - signalBounds.left) / signalBounds.width * (signalBounds.max - signalBounds.min);
  const p = signalPoints.reduce((a, b) => Math.abs(a.t - t) < Math.abs(b.t - t) ? a : b);
  $("chart-tooltip").textContent = `${plotMode === "waveform" ? p.t.toFixed(2) + " ms" : utc(p.t * 1000) + " UTC"} | X ${fmt(p.values[0])} · Y ${fmt(p.values[1])} · SUM ${fmt(p.values[2])} V${p.wind !== undefined ? ` · wind ${fmt(p.wind, 1)} m/s` : ""}`;
  $("chart-tooltip").hidden = false;
});
$("signal-chart").addEventListener("pointerleave", () => { $("chart-tooltip").hidden = true; });
new ResizeObserver(drawCharts).observe($("signal-chart").parentElement);
new ResizeObserver(drawWander).observe($("wander-chart").parentElement);
new ResizeObserver(drawWind).observe($("wind-chart").parentElement);
setInterval(() => {
  $("clock").textContent = `${new Date().toISOString().slice(0, 10)} / ${utc(Date.now())} UTC`;
  if (lastEventAt && Date.now() - lastEventAt > 6000) offline();
}, 1000);
setInterval(loadArchive, 15000);
window.addEventListener("pagehide", () => connection?.close());
window.addEventListener("pageshow", (event) => { if (event.persisted) connect(); });

async function init() {
  drawCharts();
  try {
    config = await fetchJSON("/api/config");
    $("mode-banner").hidden = config.mode !== "demo";
    const s = config.scope, c = config.calibration, e = config.experiment;
    $("sample-rate").textContent = `${count(s.sample_rate)} Sa/s / channel`;
    $("burst-depth").textContent = `${count(s.depth)} samples`;
    [s.x_channel, s.y_channel, s.sum_channel].forEach((channel, i) => { document.querySelectorAll(".channel")[i].textContent = `CH${channel}`; });
    $("scatter-tag").textContent = c.x_scale_mm !== null ? "CALIBRATED · mm" : c.position_model === "voltage" ? "X · Y (V)" : "X/SUM · Y/SUM";
    $("laser-name").textContent = `${e.laser} · ${e.wavelength_nm} nm`;
    $("path-length").textContent = `${e.one_way_distance_m * 2} m round trip`;
    $("detector-name").textContent = e.detector;
    $("license-note").textContent = `Reuse terms: ${e.data_license}${e.contact ? " · Contact: " + e.contact : ""}`;
    document.querySelector(".intro").textContent = `A view into light travelling through the atmosphere. Live measurements from our ${e.one_way_distance_m * 2} m folded laser link.`;
    const diagramTexts = document.querySelectorAll(".link-diagram text");
    diagramTexts[1].textContent = `${e.one_way_distance_m} m · outbound`;
    diagramTexts[2].textContent = `${e.one_way_distance_m} m · return`;
    document.querySelector(".link-diagram").setAttribute("aria-label", `Laser to mirror: ${e.one_way_distance_m} metres. Mirror to quadrant photodiode: ${e.one_way_distance_m} metres.`);
    document.querySelector(".experiment-copy p:not(.eyebrow)").textContent = `A continuous-wave laser travels ${e.one_way_distance_m} m to a mirror and returns to a quadrant photodiode. The breakout box provides X, Y and SUM signals to the USB oscilloscope.`;
    const data = await fetchJSON("/api/history?limit=7200");
    // Only the current source and latest session are comparable under current calibration.
    const newestSession = data.items.at(-1)?.session_id;
    addHistory(data.items.filter((item) => item.session_id === newestSession && item.source === config.mode));
    drawCharts();
  } catch (error) { offline(); }
  try {
    const recent = await fetchJSON("/api/weather/history?seconds=900");
    windHistory = recent.wind.map((p) => ({t: p.t_ns / 1e9, mean: p.mean_ms, max: p.max_ms}));
    drawWind();
  } catch (error) { console.error(error); }
  connect(); loadArchive();
}
init();
