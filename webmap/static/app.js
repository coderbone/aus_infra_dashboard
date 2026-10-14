"use strict";

const POLL_MS = 30000;

const REGION_GEO = "/data/regions.geojson";
const LINES_GEO = "/data/lines.geojson";
const LIVE_URL = "/data/live";

const state = {
  regions: {},
  interconnectors: {},
  batteries: {},
  fleet: [],
  fetchedAt: 0,
  prom: "unknown",
};
let metric = "stored";
let showSchematic = true;
let showMonitored = true;
let showUnmonitored = true;
let showSolar = true;
let showWind = true;

const map = L.map("map", { zoomControl: true, attributionControl: false }).setView([-33.2, 145.0], 5);

const regionLayers = {}; // code -> path layer
const lineLayers = [];   // {feat, layer, arrow}
const batteryLayers = {}; // facility -> circle layer
const renLayers = {}; // key -> marker

/* ---------- colour helpers ---------- */

function ramp(value, stops) {
  if (value == null || !isFinite(value)) return stops[0].c;
  let lo = stops[0], hi = stops[stops.length - 1];
  for (let i = 0; i < stops.length - 1; i++) {
    if (value <= stops[i + 1].v) { lo = stops[i]; hi = stops[i + 1]; break; }
  }
  const span = hi.v - lo.v || 1;
  const t = Math.max(0, Math.min(1, (value - lo.v) / span));
  const col = [0, 1, 2].map((ch) => Math.round(lo.c[ch] + (hi.c[ch] - lo.c[ch]) * t));
  return "rgb(" + col.join(",") + ")";
}

const STOPS_STORED = [
  { v: 0, c: [16, 26, 51] }, { v: 2000, c: [18, 58, 107] },
  { v: 4000, c: [29, 106, 143] }, { v: 6000, c: [61, 156, 94] },
  { v: 8000, c: [201, 217, 79] }, { v: 10000, c: [242, 200, 75] },
];
const STOPS_FOSSIL = [
  { v: 0, c: [43, 47, 58] }, { v: 1500, c: [107, 74, 50] },
  { v: 3000, c: [164, 101, 58] }, { v: 5000, c: [208, 138, 62] },
  { v: 6500, c: [239, 91, 63] }, { v: 8000, c: [255, 63, 47] },
];
const STOPS_CURTAIL = [
  { v: 0, c: [27, 38, 64] }, { v: 1000, c: [53, 73, 122] },
  { v: 2000, c: [106, 77, 160] }, { v: 3000, c: [169, 68, 127] },
  { v: 4000, c: [225, 59, 59] },
];
const STOPS_GEN = [
  { v: 0, c: [217, 48, 70] }, { v: 0.5, c: [224, 179, 67] }, { v: 1, c: [94, 193, 107] },
];
const STOPS_SOC = [
  { v: 0, c: [217, 48, 70] }, { v: 0.25, c: [229, 119, 60] },
  { v: 0.5, c: [224, 179, 67] }, { v: 0.75, c: [156, 200, 79] },
  { v: 1, c: [94, 193, 107] },
];
const STATUS_COLOR = {
  operating: "#8ca3ba",
  operational: "#8ca3ba",
  commissioning: "#7b6fae",
  committed: "#5a6b80",
};
const LINE_COLORS = { low: "#5ec1e6", mid: "#f2c84b", high: "#ef5b3f", nodata: "#64758b", snowy: "#3dd6c7" };

function regionMetricValue(code) {
  const r = state.regions[code];
  if (!r) return null;
  if (metric === "stored") return typeof r.stored_mwh === "number" ? r.stored_mwh : null;
  if (metric === "fossil") return typeof r.fossil_mw === "number" ? r.fossil_mw : null;
  if (metric === "curtail") {
    const c = r.curtail || {};
    let sum = 0, any = false;
    for (const v of Object.values(c)) { if (typeof v === "number") { sum += v; any = true; } }
    return any ? sum : null;
  }
  return null;
}

/* ---------- geometry init ---------- */

async function initGeometry() {
  const [regions, lines] = await Promise.all([
    fetch(REGION_GEO).then((r) => r.json()),
    fetch(LINES_GEO).then((r) => r.json()),
  ]);

  L.geoJSON(regions, {
    style: () => ({
      color: "#2d4a6b",
      weight: 1,
      fillColor: "#16283f",
      fillOpacity: 0.55,
    }),
    onEachFeature: (feat, layer) => {
      regionLayers[feat.properties.code] = layer;
      layer.bindTooltip("", { direction: "top", className: "" });
    },
  }).addTo(map);

  for (const feat of lines.features) {
    const p = feat.properties;
    const isSchematic = p.kind === "schematic";
    const live = p.kind !== "schematic";
    const snow = p.kind === "snowy";
    const latlngs = feat.geometry.coordinates.map((c) => [c[1], c[0]]);
    const layer = L.polyline(latlngs, {
      color: snow ? LINE_COLORS.snowy : LINE_COLORS.nodata,
      weight: snow ? 2 : 1.2,
      opacity: 0.9,
      dashArray: snow ? null : "5 5",
      interactive: !!live,
    });
    layer.bindTooltip("", { sticky: true, className: "" });
    layer.addTo(map);
    const arrow = L.marker(layer.getCenter(), {
      icon: L.divIcon({ className: "", iconSize: [14, 14], iconAnchor: [7, 7], html: "" }),
      interactive: false,
    });
    const entry = { feat, layer, arrow, live, snow };
    lineLayers.push(entry);
    if (p.kind !== "schematic") arrow.addTo(map);
  }
  applyLines();
  applyBatteries();
}

/* ---------- regions ---------- */

function applyRegions() {
  for (const [code, layer] of Object.entries(regionLayers)) {
    const val = regionMetricValue(code);
    layer.setStyle({
      fillColor: metric === "none" ? "#16283f" : (val == null ? "#1b2740" : ramp(val, stopsForMetric())),
    });
    const r = state.regions[code] || {};
    layer.setTooltipContent(regionTip(code, r));
  }
}
const stopsForMetric = () => (metric === "stored" ? STOPS_STORED : metric === "fossil" ? STOPS_FOSSIL : STOPS_CURTAIL);

function regionTip(code, r) {
  const bits = ['<b>' + code + '</b>'];
  if (typeof r.stored_mwh === "number") bits.push("stored " + r.stored_mwh.toFixed(0) + " MWh");
  if (typeof r.fossil_mw === "number") bits.push("fossil " + r.fossil_mw.toFixed(0) + " MW");
  if (r.curtail) {
    for (const [fuel, mw] of Object.entries(r.curtail)) if (typeof mw === "number") bits.push("curtail " + fuel + " " + mw.toFixed(0) + " MW");
  }
  return bits.join(" &middot; ");
}

/* ---------- lines ---------- */

function lineUtilization(f) {
  const ex = Math.abs(f.export_limit), im = Math.abs(f.import_limit);
  const lim = Math.max(ex, im, 0.0001);
  return Math.abs(f.flow) / lim;
}

function applyLines() {
  for (const e of lineLayers) {
    if (e.snow) continue;
    const p = e.feat.properties;
    if (p.kind === "schematic") {
      const isOn = !!e.layer._map;
      if (showSchematic && !isOn) e.layer.addTo(map);
      if (!showSchematic && isOn) map.removeLayer(e.layer);
      continue;
    }
    const f = state.interconnectors[p.id];
    const has = !!(f && typeof f.flow === "number");
    if (!has) {
      e.layer.setStyle({ color: LINE_COLORS.nodata, weight: 1.2, opacity: 0.6, dashArray: "5 5" });
      updateLineTip(e, null);
      hideArrow(e);
      continue;
    }
    const u = Math.min(1.2, lineUtilization(f));
    const col = u > 0.85 ? LINE_COLORS.high : u > 0.6 ? LINE_COLORS.mid : LINE_COLORS.low;
    e.layer.setStyle({ color: col, weight: 1.5 + 3.5 * Math.min(u, 1), opacity: 0.95, dashArray: null });
    updateLineTip(e, f);
    updateArrow(e, f);
  }
}

function updateLineTip(e, f) {
  const p = e.feat.properties;
  const stale = f && typeof f.age === "number" && f.age > 900;
  let html = '<b>' + p.name + '</b><br>' + p.id;
  if (!f) { html += ' &middot; no data'; }
  else {
    html += '<br>flow ' + f.flow.toFixed(1) + ' MW';
    if (typeof f.metered === "number") html += ' &middot; metered ' + f.metered.toFixed(1) + ' MW';
    html += '<br>export ' + (typeof f.export_limit === "number" ? f.export_limit.toFixed(0) : "n/a") +
            ' &middot; import ' + (typeof f.import_limit === "number" ? f.import_limit.toFixed(0) : "n/a") + ' MW';
    if (stale) html += ' &middot; <span style="color:#e0a63c">stale</span>';
    else if (typeof f.age === "number") html += ' &middot; ' + f.age.toFixed(0) + 's old';
  }
  e.layer.setTooltipContent(html);
}

function segmentMidpoint(coords, fraction) {
  let dist = 0;
  const segs = [];
  for (let i = 1; i < coords.length; i++) {
    const a = L.latLng(coords[i - 1][1], coords[i - 1][0]);
    const b = L.latLng(coords[i][1], coords[i][0]);
    const d = a.distanceTo(b);
    segs.push({ a, b, d });
    dist += d;
  }
  let target = dist * fraction;
  for (const s of segs) {
    if (target <= s.d || s.d === 0) {
      const t = s.d === 0 ? 0 : target / s.d;
      return { lat: s.a.lat + (s.b.lat - s.a.lat) * t, lng: s.a.lng + (s.b.lng - s.a.lng) * t, deg: bearing(s.a, s.b) };
    }
    target -= s.d;
  }
  const last = segs[segs.length - 1];
  return { lat: last.b.lat, lng: last.b.lng, deg: bearing(last.a, last.b) };
}

function bearing(a, b) {
  const dLat = (b.lat - a.lat) * Math.PI / 180;
  const dLon = (b.lng - a.lng) * Math.PI / 180;
  const y = Math.sin(dLon) * Math.cos(b.lat * Math.PI / 180);
  const x = Math.cos(a.lat * Math.PI / 180) * Math.sin(b.lat * Math.PI / 180) -
            Math.sin(a.lat * Math.PI / 180) * Math.cos(b.lat * Math.PI / 180) * Math.cos(dLon);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

function updateArrow(e, f) {
  if (!(f && typeof f.flow === "number") || Math.abs(f.flow) < 0.5) { hideArrow(e); return; }
  const mid = segmentMidpoint(e.feat.geometry.coordinates, 0.5);
  let deg = mid.deg;
  if (f.flow < 0) deg = (deg + 180) % 360;
  e.arrow.setLatLng(L.latLng(mid.lat, mid.lng));
  e.arrow.setIcon(L.divIcon({
    className: "",
    iconSize: [14, 14],
    iconAnchor: [7, 7],
    html: '<div style="transform:rotate(' + deg + 'deg);font-size:14px;line-height:14px;color:#ffe9b3;text-shadow:0 0 3px #000">&#9650;</div>',
  }));
  if (!e.arrow._map) e.arrow.addTo(map);
}

function hideArrow(e) {
  if (e.arrow._map) map.removeLayer(e.arrow);
}

/* ---------- batteries ---------- */

function facilityTotal(fac) {
  let total = 0;
  for (const u of fac.units) total += u.capacity_mwh || 0;
  return total;
}

function unitLiveStatus(fac) {
  for (const u of fac.units || []) {
    const m = state.batteries[u.code];
    if (m) return { unit: u.code, m };
  }
  return null;
}

function radiusFor(mwh) { return Math.min(14800, 2800 + 12000 * mwh / (mwh + 3000)); }

function unitPower(code) {
  const m = state.batteries[code];
  if (!m) return null;
  const oe = (typeof m.power_mw === "number")
    ? { mw: m.power_mw, src: "oe", age: typeof m.age_s === "number" ? m.age_s : Infinity }
    : null;
  const aemo = (typeof m.aemo_power_mw === "number")
    ? { mw: m.aemo_power_mw, src: "aemo", age: typeof m.aemo_power_age_s === "number" ? m.aemo_power_age_s : Infinity }
    : null;
  if (!oe && !aemo) return null;
  if (!aemo) return oe;
  if (!oe) return aemo;
  return oe.age <= aemo.age ? oe : aemo;
}
function facilityPower(fac) {
  let sumMw = 0;
  let has = false;
  for (const u of fac.units) {
    const p = unitPower(u.code);
    if (p) { sumMw += p.mw; has = true; }
  }
  return has ? { mw: sumMw } : null;
}

// AEMO sign convention: positive = discharging, negative = charging.
function rateLabel(mw) {
  if (typeof mw !== "number" || !isFinite(mw)) return "&ndash;";
  if (mw > 0.5) return '<span style="color:#ef4444">discharging ' + mw.toFixed(1) + " MW</span>";
  if (mw < -0.5) return '<span style="color:#22c55e">charging ' + Math.abs(mw).toFixed(1) + " MW</span>";
  return "idle";
}

function unitSoc(code, capacityMwh) {
  const m = state.batteries[code];
  if (!m) return null;
  const measured = typeof m.soc_ratio === "number"
    ? { soc: m.soc_ratio, src: "measured", age: typeof m.age_s === "number" ? m.age_s : Infinity }
    : null;
  const inferred = (typeof m.inf_stored_mwh === "number" && capacityMwh > 0)
    ? { soc: m.inf_stored_mwh / capacityMwh, src: "inferred", age: typeof m.inf_age_s === "number" ? m.inf_age_s : Infinity }
    : null;
  if (!measured && !inferred) return null;
  if (!inferred) return measured;
  if (!measured) return inferred;
  return measured.age <= inferred.age ? measured : inferred;
}

function facilitySoc(fac) {
  let wsum = 0, w = 0, src = null;
  for (const u of fac.units) {
    const s = unitSoc(u.code, u.capacity_mwh);
    if (!s) continue;
    wsum += s.soc * u.capacity_mwh;
    w += u.capacity_mwh;
    if (s.src === "measured") src = "measured";
    else if (src !== "measured") src = "inferred";
  }
  if (!w) return null;
  return { soc: wsum / w, src };
}

function applyBatteries() {
  for (const fac of state.fleet) {
    const loc = fac.location;
    let layer = batteryLayers[fac.facility];
    if (!loc || !loc.lat || !loc.lng) {
      if (layer) { map.removeLayer(layer); delete batteryLayers[fac.facility]; }
      continue;
    }
    const live = unitLiveStatus(fac);
    const s = facilitySoc(fac);
    const monitored = !!live || !!s;
    if (monitored && !showMonitored) {
      if (layer) { map.removeLayer(layer); delete batteryLayers[fac.facility]; }
      continue;
    }
    if (!monitored && !showUnmonitored) {
      if (layer) { map.removeLayer(layer); delete batteryLayers[fac.facility]; }
      continue;
    }
    const total = facilityTotal(fac);
    const soc = s ? s.soc : (live && typeof live.m.soc_ratio === "number" ? live.m.soc_ratio : null);
    const fp = facilityPower(fac);
    const ring = fp && Math.abs(fp.mw) > 5 ? true : false;
    const ringWeight = fp ? Math.min(6, 2 + Math.abs(fp.mw) / 20) : 0;
    const ringColor = fp && fp.mw < 0 ? "#22c55e" : (fp && fp.mw > 0 ? "#ef4444" : "#101a2b");
    const opts = {
      radius: radiusFor(total),
      weight: ring ? ringWeight : (s ? 1.5 : (monitored ? 1.5 : 1)),
      color: ring ? ringColor : (s ? "#101a2b" : (monitored ? "#101a2b" : "#3a4c63")),
      fillColor: soc != null
        ? ramp(soc, STOPS_SOC)
        : (STATUS_COLOR[fac.units[0].status] || "#54657a"),
      fillOpacity: soc != null ? 0.85 : 0.45,
      className: ring ? "bat-ring-pulse" : "",
    };
    const pos = L.latLng(loc.lat, loc.lng);
    if (!layer) {
      layer = L.circle(pos, opts);
      batteryLayers[fac.facility] = layer;
      layer.bindTooltip("", { direction: "top", className: "" });
      layer.bindPopup("", { maxWidth: 320, className: "" });
      layer.on("click", () => layer.setPopupContent(batteryPopup(fac)));
      layer.addTo(map);
    } else {
      layer.setLatLng(pos);
      layer.setStyle(opts);
      layer.setPopupContent(batteryPopup(fac));
    }
    try {
      const path = layer._path;
      if (path) {
        if (ring) path.classList.add("bat-ring-pulse");
        else path.classList.remove("bat-ring-pulse");
      }
    } catch (e) {}
    layer.setTooltipContent(batteryTip(fac, live, soc, total));
  }
}

function batteryTip(fac, live, soc, total) {
  const st = fac.units.map((u) => u.status).filter(Boolean)[0] || "";
  let html = '<b>' + fac.name + '</b> (' + fac.facility + ')<br>' +
             fac.region + ' &middot; ' + total.toFixed(0) + ' MWh &middot; ' + st;
  if (!live) html += ' &middot; <span style="color:#7d8fa5">unmonitored</span>';
  else {
    if (soc != null) html += '<br>SoC ' + (soc * 100).toFixed(0) + '%';
    if (typeof live.m.power_mw === "number") html += ' &middot; ' + live.m.power_mw.toFixed(0) + ' MW';
    if (typeof live.m.age_s === "number") html += ' &middot; ' + live.m.age_s.toFixed(0) + 's old';
  }
  return html;
}

function batteryPopup(fac) {
  const live = unitLiveStatus(fac);
  const total = facilityTotal(fac);
  const fp = facilityPower(fac);
  const fsoc = facilitySoc(fac);
  const st = fac.units.map((u) => u.status).filter(Boolean)[0] || "";
  let rows = '<tr><th>unit</th><th class="num">cap</th><th class="num">SoC</th><th class="num">rate</th></tr>';
  for (const u of fac.units) {
    const soc = unitSoc(u.code, u.capacity_mwh);
    const p = unitPower(u.code);
    rows += '<tr><td>' + u.code + '</td>' +
      '<td class="num">' + (u.capacity_mwh || 0).toFixed(0) + ' MWh</td>' +
      '<td class="num">' + (soc != null ? (soc.soc * 100).toFixed(0) + '%' : '&ndash;') + '</td>' +
      '<td class="num">' + rateLabel(p ? p.mw : undefined) + '</td></tr>';
  }
  rows += '<tr class="sum"><td>facility</td>' +
    '<td class="num">' + total.toFixed(0) + ' MWh</td>' +
    '<td class="num">' + (fsoc ? (fsoc.soc * 100).toFixed(0) + '%' : '&ndash;') + '</td>' +
    '<td class="num">' + rateLabel(fp ? fp.mw : undefined) + '</td></tr>';
  return '<div class="pop"><b>' + fac.name + '</b> <k>' + fac.region + '</k>' +
    (st ? ' &middot; ' + st : '') +
    (live ? '' : ' &middot; <span style="color:#7d8fa5">unmonitored</span>') +
    '<br>facility ' + fac.facility +
    '<table>' + rows + '</table></div>';
}

/* ---------- header / legend ---------- */

function updateHeader() {
  const timeEl = document.getElementById("st-time");
  const promEl = document.getElementById("st-prom");
  const age = Date.now() / 1000 - state.fetchedAt;
  if (!state.fetchedAt) { timeEl.textContent = "no data yet"; promEl.textContent = ""; return; }
  const d = new Date(state.fetchedAt * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  timeEl.textContent = hh + ":" + mm + ":" + ss;
  timeEl.className = age <= 120 ? "fresh" : age <= 600 ? "stale" : "down";
  promEl.textContent = state.prom === "ok" ? "prometheus ok" : state.prom === "degraded" ? "degraded" : "down";
  promEl.className = state.prom === "ok" ? "fresh" : "down";
}

function legendGlyph(shape) {
  const fill = "#8ca3ba";
  const common = 'width="14" height="14" viewBox="0 0 14 14" fill="' + fill + '" stroke="#101a2b" stroke-width="1.3"';
  if (shape === "wind") return '<span class="glyph"><svg ' + common + '><polygon points="7,1 13,12 1,12"/></svg></span>';
  return '<span class="glyph"><svg ' + common + '><polygon points="7,1 13,7 7,13 1,7"/></svg></span>';
}

function updateLegend() {
  const el = document.getElementById("legend");
  if (metric === "none") { el.innerHTML = ""; return; }
  const stops = stopsForMetric();
  const units = metric === "stored" ? "MWh" : metric === "fossil" ? "MW" : "MW";
  const label = metric === "stored" ? "stored" : metric === "fossil" ? "fossil" : "curtail";
  let rows = "";
  for (const s of stops) {
    rows += '<div class="row"><span class="sw" style="background:' + ramp(s.v, stops) + '"></span>' +
            '<span>' + s.v.toLocaleString() + ' ' + units + '</span></div>';
  }
  el.innerHTML = '<h4>' + label + '</h4>' + rows +
    '<div class="row"><span class="sw" style="background:#8ca3ba"></span><span>battery (live)</span></div>' +
    '<div class="row"><span class="sw" style="background:#54657a"></span><span>battery (status)</span></div>' +
    '<div class="row">' + legendGlyph("solar") + '<span>solar</span></div>' +
    '<div class="row">' + legendGlyph("wind") + '<span>wind</span></div>' +
    '<div class="cap">solar/wind: shape = type, colour = generation (% of capacity)</div>';
}

/* ---------- controls ---------- */

function wireControls() {
  document.getElementById("region-metric").addEventListener("change", (ev) => {
    metric = ev.target.value;
    applyRegions();
    updateLegend();
  });
  document.getElementById("tog-schematic").addEventListener("change", (ev) => {
    showSchematic = ev.target.checked;
    applyLines();
  });
  document.getElementById("tog-monitored").addEventListener("change", (ev) => {
    showMonitored = ev.target.checked;
    applyBatteries();
  });
  document.getElementById("tog-unmonitored").addEventListener("change", (ev) => {
    showUnmonitored = ev.target.checked;
    applyBatteries();
  });
  document.getElementById("tog-solar").addEventListener("change", (ev) => {
    showSolar = ev.target.checked;
    applyRenewables();
  });
  document.getElementById("tog-wind").addEventListener("change", (ev) => {
    showWind = ev.target.checked;
    applyRenewables();
  });
}

/* ---------- poll loop ---------- */

async function poll() {
  let data;
  try {
    const resp = await fetch(LIVE_URL, { cache: "no-store" });
    data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || ("HTTP " + resp.status));
  } catch (err) {
    document.getElementById("st-time").textContent = "failed: " + err.message;
    document.getElementById("st-time").className = "down";
    return;
  }
  state.regions = data.regions || {};
  state.interconnectors = data.interconnectors || {};
  state.batteries = data.batteries || {};
  state.units = data.units || {};
  state.fleet = data.fleet || [];
  state.renewables = data.renewables || [];
  state.fetchedAt = data.fetched_at;
  state.prom = data.prometheus || "ok";
  applyRegions();
  applyLines();
  applyBatteries();
  applyRenewables();
  updateHeader();
}

initGeometry().then(() => {
  wireControls();
  updateLegend();
  poll();
  setInterval(poll, POLL_MS);
});

function renMarkerKey(fac, unit) {
  return (fac.facility || fac.name || "fac") + ":" + (unit.code || "u");
}

// Solar/wind are drawn as vector polygons sized in metres (like the battery
// L.circle), so they reproject and scale with zoom instead of sitting at a fixed
// pixel size. Wind is a triangle pointing up, solar a diamond.
function renShape(isWind, lat, lng, rMeters) {
  const dLat = rMeters / 111320;
  const dLng = rMeters / (111320 * Math.max(0.2, Math.cos(lat * Math.PI / 180)));
  if (isWind) {
    return [
      [lat + dLat, lng],
      [lat - dLat * 0.5, lng - dLng],
      [lat - dLat * 0.5, lng + dLng],
    ];
  }
  return [
    [lat + dLat, lng],
    [lat, lng + dLng],
    [lat - dLat, lng],
    [lat, lng - dLng],
  ];
}

function renewablePopup(fac, u, isWind, cap, unitPow, measured, genFrac) {
  const rows = [
    '<tr><td>type</td><td class="num">' + (isWind ? "wind" : "solar") + '</td></tr>',
    '<tr><td>capacity</td><td class="num">' + (cap > 0 ? cap.toFixed(1) + " MW" : "&ndash;") + '</td></tr>',
  ];
  if (measured && typeof unitPow === "number") {
    rows.push('<tr><td>generation</td><td class="num">' + unitPow.toFixed(1) + ' MW</td></tr>');
    if (cap > 0) {
      rows.push('<tr><td>of capacity</td><td class="num">' + Math.round(genFrac * 100) + '%</td></tr>');
    }
  } else {
    rows.push('<tr><td>generation</td><td class="num" style="color:#7d8fa5">no live data</td></tr>');
  }
  return '<div class="pop"><b>' + (u.code || fac.facility) + '</b> <k>' + (fac.name || "") + '</k><br>' +
    (fac.region || "") + ' &middot; ' + (isWind ? "wind" : "solar") +
    '<table>' + rows.join("") + '</table></div>';
}

function applyRenewables() {
  if (!state.renewables) state.renewables = [];
  const existing = new Set(Object.keys(renLayers));
  for (const fac of state.renewables) {
    const loc = fac.location;
    if (!loc || !loc.lat || !loc.lng) continue;
    for (const u of fac.units || []) {
      const ft = u.fueltech_id || (u.code && u.code.includes("WF") ? "wind" : "solar");
      const isWind = (ft === "wind" || ft === "wind_farm");
      const isSolar = (ft === "solar" || ft === "solar_utility");
      if (isWind && !showWind) continue;
      if (isSolar && !showSolar) continue;
      const cap = u.capacity_mw || u.capacity || 0;
      const key = renMarkerKey(fac, u);
      existing.delete(key);
      // Only the unit's own measured MW (aemo_unit_power_mw, keyed by DUID) drives
      // the marker. We deliberately do NOT fall back to the region-level curtailment
      // estimate, and do not pulse on it: per-facility curtailment is unavailable,
      // so a region value cannot show which facilities inside the region are
      // curtailed. If AEMO ever publishes per-facility curtailment, scrape it and
      // restore both the fallback colour and the pulsing — see scrapers/NOTES.md.
      const unitPow = (state.units && state.units[u.code] && state.units[u.code].power_mw);
      const measured = typeof unitPow === 'number' && cap > 0;
      const genFrac = measured ? Math.max(0, Math.min(1, unitPow / cap)) : null;
      const fill = measured ? ramp(genFrac, STOPS_GEN) : "#54657a";
      const pts = renShape(isWind, loc.lat, loc.lng, radiusFor(cap));
      const opts = {
        color: "#101a2b",
        weight: 1.5,
        fillColor: fill,
        fillOpacity: measured ? 0.85 : 0.5,
      };
      let layer = renLayers[key];
      if (!layer) {
        layer = L.polygon(pts, opts).addTo(map);
        renLayers[key] = layer;
        layer.bindTooltip("", { direction: "top" });
        layer.bindPopup("", { maxWidth: 320 });
      } else {
        layer.setLatLngs(pts);
        layer.setStyle(opts);
      }
      layer.setPopupContent(renewablePopup(fac, u, isWind, cap, unitPow, measured, genFrac));
      layer.setTooltipContent(
        '<b>' + (u.code || fac.facility) + '</b> &middot; ' + (isWind ? "wind" : "solar") +
        (cap ? ' &middot; ' + cap.toFixed(0) + ' MW' : '') +
        (measured ? '<br>' + Math.round(unitPow) + ' MW (' + Math.round(genFrac * 100) + '%)' : ''));
    }
  }
  for (const k of existing) { if (renLayers[k]) { map.removeLayer(renLayers[k]); delete renLayers[k]; } }
}

