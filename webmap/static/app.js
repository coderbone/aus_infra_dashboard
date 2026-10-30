"use strict";

const POLL_MS = 30000;

const REGION_GEO = "/data/regions.geojson";
const LINES_GEO = "/data/lines.geojson";
const LIVE_URL = "/data/live";

// 24h playback: 288 frames at 5-min steps, shown at 24fps (12s total).
const PLAY_FPS = 24;
const PLAY_STEP_S = 300;
const PLAY_TOTAL = Math.round(24 * 3600 / PLAY_STEP_S); // 288
const PLAY_LOOKAHEAD = 48; // prefetch this many frames ahead of the playhead
const PLAY_CONCURRENCY = 6;

let playing = false;
let playIdx = 0;
let playTimes = [];
let playBuffer = new Map(); // index -> raw JSON text (parsed on apply, then freed)
let playInFlight = new Set();
let playRaf = null;
let playStart = 0;
let playAbort = null;

const state = {
  regions: {},
  interconnectors: {},
  batteries: {},
  fleet: [],
  hydro: [],
  coal: [],
  gas: [],
  renewables: [],
  reservoirs: {},
  units: {},
  fetchedAt: 0,
  historicalAt: null,
  prom: "ok",
};
let metric = "stored";
let showSchematic = true;
let showBatteries = true;
let showIdle = true;
let showSolar = true;
let showWind = true;
let showHydro = true;
let showCoal = true;
let showGas = true;
let showDistillate = true;
let showStatus = {
  operating: true,
  operational: true,
  commissioning: true,
  committed: true
};

// The toggle state the model's visibility gate reads, rebuilt from the live
// toggle globals on each call. The model never reads these globals itself -
// they travel in - so it stays a pure function of (payload, toggles).
function visToggles() {
  return { showIdle, showStatus };
}

const map = L.map("map", { zoomControl: true, attributionControl: false }).setView([-33.2, 145.0], 5);

const regionLayers = {}; // code -> path layer
const lineLayers = [];   // {feat, layer, arrow}
const batteryLayers = {}; // facility -> circle layer
const batteryArcs = {}; // facility -> commissioning progress arc (donut sector)
const renLayers = {}; // key -> marker
const hydroLayers = {}; // facility -> hexagon
const hydroRings = {}; // facility -> concentric inner hexagon (pumped storage only)
const coalLayers = {}; // facility -> square
const gasLayers = {}; // facility -> pentagon
const distillateLayers = {}; // facility -> triangle

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
// The SoC ramp is not stated here: it arrives with every live payload, from
// scrapers/soc_policy.py via the server, so the map and the dashboard's bar
// gauge interpolate across one list and neither can drift from the other.
// Populated in applySnapshot; battery drawing only happens after a payload.
let STOPS_SOC = [];
const STATUS_COLOR = {
  operating: "#8ca3ba",
  operational: "#8ca3ba",
  commissioning: "#7b6fae",
  committed: "#5a6b80",
};
// A commissioning facility is drawn as a dim "planned" disc with a bright arc
// covering the fraction that has actually been energised (peak stored MWh over
// registered storage capacity). The arc is a donut/sector band, coloured
// red-yellow-green by how charged the energised plant is right now relative to
// its own peak (see commissionColor). COMMISSION_BASE is the unbuilt remainder
// and COMMISSION_ARC the fallback arc colour when no current reading exists.
const COMMISSION_BASE = "#2b2740";
const COMMISSION_ARC = "#ab9bf0";
const COMMISSION_ARC_INNER = 0.58;
const LINE_COLORS = { low: "#5ec1e6", mid: "#f2c84b", high: "#ef5b3f", nodata: "#64758b", snowy: "#3dd6c7" };

// Utilisation -> colour, the scale the map line and the tooltip's flow bar
// share so the two can never disagree. Anything not finite has no rating to
// state (Model.flowBar returns null in that case too) and reads as no-data
// grey rather than as idle.
function lineColorFor(u) {
  if (typeof u !== "number" || !isFinite(u)) return LINE_COLORS.nodata;
  return u > 0.85 ? LINE_COLORS.high : u > 0.6 ? LINE_COLORS.mid : LINE_COLORS.low;
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
      layer.bindPopup("", { maxWidth: 300, className: "" });
      layer.on("click", () =>
        layer.setPopupContent(regionPopup(feat.properties.code, feat.properties.name)));
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
      weight: isSchematic ? 5.5 : (snow ? 2 : 1.2),
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
    const val = Model.regionMetricValue(state, metric, code);
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
  // Fossil is the model's one coal+gas+distillate rollup - the same number the
  // region fill paints and the popup ring's fossil bands sum to.
  const fossilMw = Model.regionFossilMw(state, code);
  if (fossilMw != null) bits.push("fossil " + fossilMw.toFixed(0) + " MW");
  if (typeof r.rooftop_mw === "number") bits.push("rooftop " + r.rooftop_mw.toFixed(0) + " MW");
  if (typeof r.import_mw === "number") bits.push("import " + r.import_mw.toFixed(0) + " MW");
  if (typeof r.export_mw === "number") bits.push("export " + r.export_mw.toFixed(0) + " MW");
  if (r.curtail) {
    for (const [fuel, mw] of Object.entries(r.curtail)) if (typeof mw === "number") bits.push("curtail " + fuel + " " + mw.toFixed(0) + " MW");
  }
  return bits.join(" &middot; ");
}

// Region ring colours. Generation and load read from separate ends of the
// palette so the two bands never share a hue by accident: cool slate/orange/
// sky/emerald/amber plus the storage purple in the generation band, teal for
// pumping in the load band. A curtailed fuel reuses its own hue as a stripe
// (see ringSwatch / regionRingSVG) so "spilled generation" reads as the same
// resource the region could have used, not a separate one.
const RING_COLOR = {
  coal: "#94a3b8", gas: "#fb923c", distillate: "#f472b6", hydro: "#38bdf8", wind: "#34d399",
  solar: "#fbbf24", battery: "#c084fc", pumps: "#2dd4bf",
  rooftop: "#f59e0b",
  imports: "#f472b6", exports: "#a3e635",
};

// The model returns each region-flow segment as {key, label, value}; colour is
// drawing, so it is attached here by key. A curtailed fuel reuses its own
// hue (see ringSwatch / regionRingSVG) so "spilled generation" reads as the
// same resource the region could have used, not a separate one.
function ringSegColor(seg) {
  if (seg.key === "wind_curt") return RING_COLOR.wind;
  if (seg.key === "solar_curt") return RING_COLOR.solar;
  return RING_COLOR[seg.key];
}

// A point on a circle whose 0 degrees is 12 o'clock, sweeping clockwise - the
// same convention as commissionArc, so ring segments start at the top.
function ringPoint(cx, cy, r, deg) {
  const a = deg * Math.PI / 180;
  return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
}

// SVG path for an annular wedge (donut segment), drawn as an outer clockwise arc
// closed back along an inner anti-clockwise arc.
function annularSector(cx, cy, rIn, rOut, a0, a1) {
  if (a1 - a0 <= 0) return "";
  const large = a1 - a0 > 180 ? 1 : 0;
  const [ox0, oy0] = ringPoint(cx, cy, rOut, a0);
  const [ox1, oy1] = ringPoint(cx, cy, rOut, a1);
  const [ix1, iy1] = ringPoint(cx, cy, rIn, a1);
  const [ix0, iy0] = ringPoint(cx, cy, rIn, a0);
  const n = (v) => v.toFixed(2);
  return "M" + n(ox0) + " " + n(oy0) +
    "A" + rOut + " " + rOut + " 0 " + large + " 1 " + n(ox1) + " " + n(oy1) +
    "L" + n(ix1) + " " + n(iy1) +
    "A" + rIn + " " + rIn + " 0 " + large + " 0 " + n(ix0) + " " + n(iy0) + "Z";
}

// Two concentric rings sharing one scale. The centre third of the radius is
// void; the middle band is generation, the outer band is loads. Each band is a
// stacked bar: a segment's sweep is value/max of a full turn, and max is the
// larger of the two totals, so whichever side is bigger fills its ring and the
// other visibly falls short. Segments are separated by a thin panel-coloured
// stroke; a curtailed fuel is filled with a diagonal stripe instead of a flat
// colour.
function regionRingSVG(flows) {
  const S = 176, c = S / 2, R = 78;
  const rGenIn = R / 3, rGenOut = 2 * R / 3;
  const panel = "#0f1b2d";
  const stripe = (id, color) =>
    '<pattern id="' + id + '" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
    '<rect width="6" height="6" fill="' + panel + '"/>' +
    '<line x1="0" y1="0" x2="0" y2="6" stroke="' + color + '" stroke-width="3"/></pattern>';
  let svg = '<svg class="rring" width="' + S + '" height="' + S + '" viewBox="0 0 ' + S + ' ' + S + '">';
  svg += '<defs>' + stripe("ringCurtW", RING_COLOR.wind) + stripe("ringCurtS", RING_COLOR.solar) + '</defs>';
  // Faint tracks so an empty (or short) band is still legible.
  svg += '<circle cx="' + c + '" cy="' + c + '" r="' + (rGenIn + rGenOut) / 2 +
    '" fill="none" stroke="rgba(214,226,240,0.08)" stroke-width="' + (rGenOut - rGenIn) + '"/>';
  svg += '<circle cx="' + c + '" cy="' + c + '" r="' + (rGenOut + R) / 2 +
    '" fill="none" stroke="rgba(214,226,240,0.08)" stroke-width="' + (R - rGenOut) + '"/>';
  const band = (segs, rIn, rOut) => {
    let a0 = 0, out = "";
    for (const s of segs) {
      const a1 = a0 + (flows.max > 0 ? s.value / flows.max * 360 : 0);
      const fill = s.key === "wind_curt" ? "url(#ringCurtW)"
        : s.key === "solar_curt" ? "url(#ringCurtS)" : s.color;
      out += '<path d="' + annularSector(c, c, rIn, rOut, a0, a1) + '" fill="' + fill +
        '" stroke="' + panel + '" stroke-width="1"/>';
      a0 = a1;
    }
    return out;
  };
  svg += band(flows.gen, rGenIn, rGenOut);
  svg += band(flows.load, rGenOut, R);
  return svg + '</svg>';
}

function ringSwatch(s) {
  const bg = s.key === "wind_curt" || s.key === "solar_curt"
    ? "repeating-linear-gradient(45deg," + s.color + "," + s.color + " 2px,#0f1b2d 2px,#0f1b2d 5px)"
    : s.color;
  return '<span class="rsw" style="background:' + bg + '"></span>';
}

function regionPopup(code, name) {
  const f = Model.regionFlows(state, code);
  for (const seg of f.gen) seg.color = ringSegColor(seg);
  for (const seg of f.load) seg.color = ringSegColor(seg);
  const list = (segs, sign) => segs.length
    ? segs.map((s) => '<div class="rrow">' + ringSwatch(s) + s.label + ': ' + sign + Math.round(s.value) + ' MW</div>').join("")
    : '<div class="rrow"><span style="color:#7d8fa5">no live data</span></div>';
  const total = (v, sign) => v > Model.IDLE_MW ? sign + Math.round(v) + ' MW' : '&ndash;';
  return '<div class="pop rpop"><div class="rhead"><b>' + (name || code) + '</b> <k>' + code + '</k></div>' +
    regionRingSVG(f) +
    '<div class="rsect"><div class="rtitle"><b>Generation</b> (' + total(f.totalGen, "") + ')</div>' +
    list(f.gen, "") + '</div>' +
    '<div class="rsect"><div class="rtitle"><b>Loads</b> (' + total(f.totalLoad, "-") + ')</div>' +
    list(f.load, "-") + '</div></div>';
}

/* ---------- lines ---------- */

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
    const u = f.utilisation;
    if (typeof u !== "number" || !isFinite(u)) {
      // Flowing, but the rating is unknown: draw it and show the direction
      // rather than pretending it is idle or saturated. There is no
      // denominator, so no utilisation is stated.
      e.layer.setStyle({ color: LINE_COLORS.nodata, weight: 2, opacity: 0.8, dashArray: null });
      updateLineTip(e, f);
      updateArrow(e, f);
      continue;
    }
    const loaded = Math.min(1.2, u);
    const col = lineColorFor(u);
    e.layer.setStyle({ color: col, weight: 1.5 + 3.5 * Math.min(loaded, 1), opacity: 0.95, dashArray: null });
    updateLineTip(e, f);
    updateArrow(e, f);
  }
}

function num(v, digits) {
  // NaN and infinities are numbers in JS, so test for them too - otherwise a
  // missing reading renders as the string "NaN".
  return typeof v === "number" && isFinite(v) ? v.toFixed(digits) : "n/a";
}

// The tooltip's flow bar: one fixed 200x18 SVG over the link's own signed
// range (-reverse .. zero .. +forward). The faint track is that whole range,
// the thin tick is zero - off centre on an asymmetric link, because both
// halves share one scale - the fill is the flow in the line's own utilisation
// colour, and the cased dashed ticks are the dispatch limits. Model.flowBar
// decides the geometry (and refuses to draw when the bar cannot be honest);
// this only draws it. Empty string when there is no bar.
function flowBarSVG(f) {
  const b = Model.flowBar(f);
  if (!b) return "";
  const W = 200, H = 18, y = 4, h = 10;
  const px = (frac) => frac * W;
  const n = (v) => v.toFixed(2);
  const at = (frac) => n(px(frac));
  let svg = '<svg class="fbar" width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '">';
  // Faint track so a zero or idle flow still shows the bar it sits on.
  svg += '<rect x="0" y="' + y + '" width="' + W + '" height="' + h +
    '" rx="2" fill="rgba(214,226,240,0.08)"/>';
  const a = Math.min(px(b.flow), px(b.zero));
  const w = Math.abs(px(b.flow) - px(b.zero));
  if (w > 0) {
    svg += '<rect x="' + n(a) + '" y="' + y + '" width="' + n(w) + '" height="' + h +
      '" rx="2" fill="' + lineColorFor(f.utilisation) + '"/>';
  }
  // Zero draws over the fill, so the origin of the scale is never swallowed.
  svg += '<line x1="' + at(b.zero) + '" y1="' + y + '" x2="' + at(b.zero) +
    '" y2="' + (y + h) + '" stroke="rgba(214,226,240,0.35)" stroke-width="1"/>';
  // Limits are cased: a light dashed tick over a panel-coloured line reads
  // over both the dark track and the coloured fill. An off-scale limit pins to
  // the bar's end (Model.flowBar), so the drawn x is inset by half a stroke -
  // at x=0/x=200 the viewBox edge would clip the tick away.
  const mark = (frac) => {
    const mx = n(Math.max(1.5, Math.min(W - 1.5, px(frac))));
    return '<line x1="' + mx + '" y1="1" x2="' + mx + '" y2="17" stroke="#0f1b2d" stroke-width="3"/>' +
      '<line x1="' + mx + '" y1="1" x2="' + mx + '" y2="17" stroke="#e8f0fa" stroke-width="1" stroke-dasharray="2 2"/>';
  };
  if (b.exp != null) svg += mark(b.exp);
  if (b.imp != null) svg += mark(b.imp);
  return svg + "</svg>";
}

function updateLineTip(e, f) {
  const p = e.feat.properties;
  const stale = Model.isStaleAge(f && f.age);
  let html = '<b>' + p.name + '</b><br>' + p.id;
  if (!f) { html += ' &middot; no data'; }
  else {
    const bar = flowBarSVG(f);
    if (bar) html += '<br>' + bar;
    html += '<br>flow ' + num(f.flow, 1) + ' MW';
    if (typeof f.metered === "number") html += ' &middot; metered ' + num(f.metered, 1) + ' MW';
    if (typeof f.utilisation === "number" && isFinite(f.utilisation))
      html += ' &middot; ' + (f.utilisation * 100).toFixed(0) + '% of nominal';
    html += '<br>nominal ' + num(f.nominal_forward, 0) +
            ' fwd &middot; ' + num(f.nominal_reverse, 0) + ' rev MW';
    html += '<br>limits ' + num(f.export_limit, 0) +
            ' &middot; ' + num(f.import_limit, 0) + ' MW';
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
  if (!(f && typeof f.flow === "number") || Math.abs(f.flow) < Model.IDLE_MW) { hideArrow(e); return; }
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

// The domain answers - visibility, idle, power, SoC, commissioning progress -
// come from the model (Model.*): this file only draws what it is told.

// A donut sector (annular wedge) sized in metres, drawn as a lat/lng polygon so
// it overlays the L.circle. Leaflet renders that circle as an ellipse whose
// semi-axes are r/111320 degrees of latitude and r/(111320*cos lat) of
// longitude, which is exactly the metre->degree scaling used here (and by
// renShape), so the two coincide. Sweeps clockwise from 12 o'clock.
function commissionArc(lat, lng, rMeters, fraction) {
  const dLat = 1 / 111320;
  const dLng = 1 / (111320 * Math.max(0.2, Math.cos(lat * Math.PI / 180)));
  const span = Math.max(0, Math.min(1, fraction)) * 360;
  const steps = Math.max(2, Math.round(span / 6));
  const outer = [], inner = [];
  for (let i = 0; i <= steps; i++) {
    const b = (span * i / steps) * Math.PI / 180;
    const cos = Math.cos(b), sin = Math.sin(b);
    outer.push([lat + rMeters * dLat * cos, lng + rMeters * dLng * sin]);
    inner.push([lat + rMeters * COMMISSION_ARC_INNER * dLat * cos,
                lng + rMeters * COMMISSION_ARC_INNER * dLng * sin]);
  }
  return outer.concat(inner.reverse());
}

function unitLiveStatus(fac) {
  for (const u of fac.units || []) {
    const m = state.batteries[u.code];
    if (m) return { unit: u.code, m };
  }
  return null;
}

// Marker radii are drawn in metres, so Leaflet reprojects them as the map zooms.
// At the pure metre size a marker doubles on screen for every zoom level, and by
// the time you are zoomed right in the big ones are hundreds of pixels across and
// swallow their neighbours. Damping the metre size by the zoom makes the on-screen
// growth sub-linear: markers still grow as you zoom in, so their relative sizes
// stay meaningful, but slower than the map, so units stay separable. Zoomed out
// they read a little larger than the pure metre size would give.
// MARKER_ZOOM_DAMP is the knob: 0 restores the old pure-metre scaling, 1 pins
// markers to a constant pixel size regardless of zoom.
const MARKER_ZOOM_REF = 6;    // zoom at which the metre scale is left untouched
const MARKER_ZOOM_DAMP = 0.6; // how hard the zoom growth is damped (see above)
const GFX_SCALE_MIN = 0.5;
const GFX_SCALE_MAX = 10.0;
const GFX_SCALE_STEP = 0.1;
const GFX_SCALE_DEF = 1.0;
let gfxScale = GFX_SCALE_DEF;


function viewScale() {
  // Tolerant of a missing map.getZoom so the geometry helpers stay usable (and
  // unit-testable) without a real Leaflet map.
  const z = map && typeof map.getZoom === "function" ? map.getZoom() : MARKER_ZOOM_REF;
  const g = (typeof gfxScale !== 'undefined' && isFinite(gfxScale)) ? gfxScale : 1.0;
  return Math.pow(2, -MARKER_ZOOM_DAMP * (z - MARKER_ZOOM_REF)) * g;
}

function radiusFor(mwh) {
  return Math.min(14800, 2800 + 12000 * mwh / (mwh + 3000)) * viewScale();
}

// AEMO sign convention: positive = discharging, negative = charging. The idle
// bar is the model's one threshold, so the label and the idle filter agree.
function rateLabel(mw) {
  if (typeof mw !== "number" || !isFinite(mw)) return "&ndash;";
  if (mw > Model.IDLE_MW) return '<span style="color:#ef4444">discharging ' + mw.toFixed(1) + " MW</span>";
  if (mw < -Model.IDLE_MW) return '<span style="color:#22c55e">charging ' + Math.abs(mw).toFixed(1) + " MW</span>";
  return "idle";
}

function removeBattery(facility) {
  const layer = batteryLayers[facility];
  if (layer) { map.removeLayer(layer); delete batteryLayers[facility]; }
  const arc = batteryArcs[facility];
  if (arc) { map.removeLayer(arc); delete batteryArcs[facility]; }
}

function applyBatteries() {
  for (const fac of state.fleet) {
    const loc = fac.location;
    let layer = batteryLayers[fac.facility];
    // One gate: the model's visibility answer already folds in the idle
    // filter, so there is no second idle check to drift from it.
    if (!loc || !loc.lat || !loc.lng || !showBatteries || !Model.facilityStatusVisible(fac, state, visToggles())) {
      removeBattery(fac.facility);
      continue;
    }
    const live = unitLiveStatus(fac);
    const fsoc = Model.facilitySoc(fac, state);
    const soc = fsoc ? fsoc.soc : null;
    const monitored = !!live || !!fsoc;
    const total = Model.facilityTotal(fac);
    const fp = Model.facilityPower(fac, state);
    const commissioning = Model.isCommissioning(fac);
    const commission = commissioning ? Model.facilityCommission(fac, state) : null;
    const ring = !commissioning && fp && Math.abs(fp.mw) > 5 ? true : false;
    const ringWeight = fp ? Math.min(6, 2 + Math.abs(fp.mw) / 20) : 0;
    const ringColor = fp && fp.mw < 0 ? "#22c55e" : (fp && fp.mw > 0 ? "#ef4444" : "#101a2b");
    const opts = commissioning ? {
      radius: radiusFor(total),
      weight: 1.4,
      color: "#6f64a8",
      dashArray: "4 3",
      fillColor: COMMISSION_BASE,
      fillOpacity: 0.55,
      className: "",
    } : {
      radius: radiusFor(total),
      weight: ring ? ringWeight : (fsoc ? 1.5 : (monitored ? 1.5 : 1)),
      color: ring ? ringColor : (fsoc ? "#101a2b" : (monitored ? "#101a2b" : "#3a4c63")),
      fillColor: soc != null && STOPS_SOC.length
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
      layer.setRadius(opts.radius); // setStyle does not touch an L.circle's radius
      layer.setStyle(opts);
      layer.setPopupContent(batteryPopup(fac));
    }
    layer._sizeR = opts.radius;
    try {
      const path = layer._path;
      if (path) {
        if (ring) path.classList.add("bat-ring-pulse");
        else path.classList.remove("bat-ring-pulse");
      }
    } catch (e) {}
    applyCommissionArc(fac.facility, commissioning, commission, loc, radiusFor(total));
    layer.setTooltipContent(batteryTip(fac, live, soc, total, commission));
  }
}

function commissionColor(commission) {
  // Red near empty, through yellow, to green near the facility's own peak. Falls
  // back to the flat commissioning purple only if no current reading exists.
  return commission && commission.fill != null ? ramp(commission.fill, STOPS_GEN) : COMMISSION_ARC;
}

function applyCommissionArc(facility, commissioning, commission, loc, radius) {
  const wantArc = commissioning && commission && commission.frac > 0;
  const existing = batteryArcs[facility];
  if (!wantArc) {
    if (existing) { map.removeLayer(existing); delete batteryArcs[facility]; }
    return;
  }
  const color = commissionColor(commission);
  const pts = commissionArc(loc.lat, loc.lng, radius, commission.frac);
  if (!existing) {
    batteryArcs[facility] = L.polygon(pts, {
      interactive: false,
      color: color,
      weight: 0,
      fillColor: color,
      fillOpacity: 0.95,
    }).addTo(map);
  } else {
    existing.setLatLngs(pts);
    existing.setStyle({ color: color, fillColor: color, fillOpacity: 0.95 });
  }
  const cur = batteryArcs[facility];
  if (cur) cur._sizeR = radius;
}

function batteryTip(fac, live, soc, total, commission) {
  const st = fac.units.map((u) => u.status).filter(Boolean)[0] || "";
  let html = '<b>' + fac.name + '</b> (' + fac.facility + ')<br>' +
             fac.region + ' &middot; ' + total.toFixed(0) + ' MWh &middot; ' + st;
  if (commission) {
    html += '<br><span style="color:' + commissionColor(commission) + '">commissioning: ' +
            (commission.frac * 100).toFixed(0) + '% energised';
    if (commission.fill != null) {
      html += ' &middot; now ' + (commission.fill * 100).toFixed(0) + '% of peak';
    }
    html += '</span>';
  }
  if (!live) html += ' &middot; <span style="color:#7d8fa5">unmonitored</span>';
  else {
    if (soc != null) html += '<br>SoC ' + (soc * 100).toFixed(0) + '%';
    if (typeof live.m.aemo_power_mw === "number") html += ' &middot; ' + live.m.aemo_power_mw.toFixed(0) + ' MW';
    if (typeof live.m.aemo_power_age_s === "number") html += ' &middot; ' + live.m.aemo_power_age_s.toFixed(0) + 's old';
  }
  return html;
}

function batteryPopup(fac) {
  const live = unitLiveStatus(fac);
  const total = Model.facilityTotal(fac);
  const fp = Model.facilityPower(fac, state);
  const fsoc = Model.facilitySoc(fac, state);
  const st = fac.units.map((u) => u.status).filter(Boolean)[0] || "";
  let rows = '<tr><th>unit</th><th class="num">cap</th><th class="num">SoC</th><th class="num">rate</th></tr>';
  for (const u of fac.units) {
    const soc = Model.unitSoc(u.code, u.capacity_mwh, state);
    const p = Model.unitPower(u.code, state);
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
  if (!state.fetchedAt) { timeEl.textContent = "no data yet"; promEl.textContent = ""; return; }
  // During 24h playback, show the historical frame's own timestamp with a
  // distinct style rather than the live age logic (which would always read
  // "down" since the frame is from the past).
  if (state.historicalAt) {
    const d = new Date(state.historicalAt * 1000);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    timeEl.textContent = hh + ":" + mm + " (history)";
    timeEl.className = "hist";
    promEl.textContent = "playing";
    promEl.className = "hist";
    return;
  }
  const age = Date.now() / 1000 - state.fetchedAt;
  const d = new Date(state.fetchedAt * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  timeEl.textContent = hh + ":" + mm + ":" + ss;
  timeEl.className = Model.headerFreshness(age);
  promEl.textContent = state.prom === "ok" ? "prometheus ok" : state.prom === "degraded" ? "degraded" : "down";
  promEl.className = state.prom === "ok" ? "fresh" : "down";
}

function legendGlyph(shape) {
  const fill = "#8ca3ba";
  const common = 'width="14" height="14" viewBox="0 0 14 14" fill="' + fill + '" stroke="#101a2b" stroke-width="1.3"';
  if (shape === "wind") return '<span class="glyph"><svg ' + common + '><polygon points="7,1 13,12 1,12"/></svg></span>';
  return '<span class="glyph"><svg ' + common + '><polygon points="7,1 13,7 7,13 1,7"/></svg></span>';
}

// A stand-in donut sector for the legend: a ~70% arc, matching the marker, filled
// with the same red-yellow-green ramp the arc colour uses.
function commissionArcGlyph() {
  const cx = 7, cy = 7, ro = 6, ri = 3.4, sweep = 0.7 * 2 * Math.PI, steps = 16;
  const pt = (r, th) => (cx + r * Math.sin(th)).toFixed(2) + ' ' + (cy - r * Math.cos(th)).toFixed(2);
  let d = 'M' + pt(ro, 0);
  for (let i = 1; i <= steps; i++) d += 'L' + pt(ro, sweep * i / steps);
  for (let i = steps; i >= 0; i--) d += 'L' + pt(ri, sweep * i / steps);
  const rgb = (c) => "rgb(" + c.join(",") + ")";
  return '<svg width="14" height="14" viewBox="0 0 14 14">' +
    '<defs><linearGradient id="cmgrad" x1="0" y1="1" x2="0" y2="0">' +
    '<stop offset="0" stop-color="' + rgb(STOPS_GEN[0].c) + '"/>' +
    '<stop offset="0.5" stop-color="' + rgb(STOPS_GEN[1].c) + '"/>' +
    '<stop offset="1" stop-color="' + rgb(STOPS_GEN[STOPS_GEN.length - 1].c) + '"/></linearGradient></defs>' +
    '<path d="' + d + 'Z" fill="url(#cmgrad)" stroke="#101a2b" stroke-width="0.8"/></svg>';
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
    '<div class="row"><span class="glyph">' + commissionArcGlyph() + '</span><span>commissioning (energised)</span></div>' +
    '<div class="row">' + legendGlyph("solar") + '<span>solar</span></div>' +
    '<div class="row">' + legendGlyph("wind") + '<span>wind</span></div>' +
    '<div class="row">' + hydroGlyph(false) + '<span>hydro</span></div>' +
    '<div class="row">' + hydroGlyph(true) + '<span>pumped hydro</span></div>' +
    '<div class="row">' + thermalGlyph(THERMAL_SHAPE.coal.sides, THERMAL_SHAPE.coal.offsetDeg) + '<span>coal</span></div>' +
    '<div class="row">' + thermalGlyph(THERMAL_SHAPE.gas.sides, THERMAL_SHAPE.gas.offsetDeg) + '<span>gas</span></div>' +
    '<div class="row">' + thermalGlyph(THERMAL_SHAPE.distillate.sides, THERMAL_SHAPE.distillate.offsetDeg) + '<span>distillate</span></div>' +
    '<div class="cap">commissioning: arc length = % capacity energised, colour = stored now vs peak (red&rarr;green)</div>' +
    '<div class="cap">solar/wind: shape = type, colour = generation (% of capacity)</div>' +
    '<div class="cap">hydro: colour = output (% of capacity), pulse = &ge;5% (floor 1 MW) generating or pumping</div>' +
    '<div class="cap">coal/gas: colour = generation (% of capacity)</div>';
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
  document.getElementById("tog-batteries").addEventListener("change", (ev) => {
    showBatteries = ev.target.checked;
    applyBatteries();
  });
  const togIdle = document.getElementById("tog-idle");
  if (togIdle) {
    togIdle.addEventListener("change", (ev) => {
      showIdle = ev.target.checked;
      applyBatteries();
      applyHydro();
      applyThermal("coal", state.coal, showCoal, coalLayers);
      applyThermal("gas", state.gas, showGas, gasLayers);
      applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
      applyRenewables();
    });
  }
  document.getElementById("tog-status-operating").addEventListener("change", (ev) => {
    showStatus.operating = ev.target.checked;
    applyBatteries();
    applyHydro();
    applyThermal("coal", state.coal, showCoal, coalLayers);
    applyThermal("gas", state.gas, showGas, gasLayers);
    applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
    applyRenewables();
  });
  document.getElementById("tog-status-operational").addEventListener("change", (ev) => {
    showStatus.operational = ev.target.checked;
    applyBatteries();
    applyHydro();
    applyThermal("coal", state.coal, showCoal, coalLayers);
    applyThermal("gas", state.gas, showGas, gasLayers);
    applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
    applyRenewables();
  });
  document.getElementById("tog-status-commissioning").addEventListener("change", (ev) => {
    showStatus.commissioning = ev.target.checked;
    applyBatteries();
    applyHydro();
    applyThermal("coal", state.coal, showCoal, coalLayers);
    applyThermal("gas", state.gas, showGas, gasLayers);
    applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
    applyRenewables();
  });
  document.getElementById("tog-status-committed").addEventListener("change", (ev) => {
    showStatus.committed = ev.target.checked;
    applyBatteries();
    applyHydro();
    applyThermal("coal", state.coal, showCoal, coalLayers);
    applyThermal("gas", state.gas, showGas, gasLayers);
    applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
    applyRenewables();
  });
  document.getElementById("tog-solar").addEventListener("change", (ev) => {
    showSolar = ev.target.checked;
    applyRenewables();
  });
  document.getElementById("tog-wind").addEventListener("change", (ev) => {
    showWind = ev.target.checked;
    applyRenewables();
  });
  document.getElementById("tog-hydro").addEventListener("change", (ev) => {
    showHydro = ev.target.checked;
    applyHydro();
  });
  document.getElementById("tog-coal").addEventListener("change", (ev) => {
    showCoal = ev.target.checked;
    applyThermal("coal", state.coal, showCoal, coalLayers);
  });
  document.getElementById("tog-gas").addEventListener("change", (ev) => {
    showGas = ev.target.checked;
    applyThermal("gas", state.gas, showGas, gasLayers);
  });
  document.getElementById("tog-distillate").addEventListener("change", (ev) => {
    showDistillate = ev.target.checked;
    applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
  });
  document.getElementById("play-24h").addEventListener("click", () => {
    if (playing) stopPlayback();
    else startPlayback();
  });
}

/* ---------- poll loop ---------- */

async function poll() {
  if (playing) return; // don't stomp historical playback with live data
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
  applySnapshot(data, false);
}

function applySnapshot(data, isHistorical) {
  state.regions = data.regions || {};
  state.interconnectors = data.interconnectors || {};
  state.batteries = data.batteries || {};
  state.units = data.units || {};
  state.fleet = data.fleet || [];
  state.renewables = data.renewables || [];
  state.hydro = data.hydro || [];
  state.coal = data.coal || [];
  state.gas = data.gas || [];
  state.distillate = data.distillate || [];
  state.reservoirs = data.reservoirs || {};
  state.fetchedAt = data.fetched_at;
  state.historicalAt = isHistorical ? (data.at || null) : null;
  state.prom = data.prometheus || "ok";
  if (data.soc_ramp) STOPS_SOC = data.soc_ramp;
  applyRegions();
  applyLines();
  // Draw the broad shapes (hydro, then thermal) before the narrower solar/wind/
  // battery ones; the final z-order is then set by restackMarkers() below, which
  // fronts by size, so this order only needs to be stable, not exact.
  applyHydro();
  applyThermal("coal", state.coal, showCoal, coalLayers);
  applyThermal("gas", state.gas, showGas, gasLayers);
  applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
  applyBatteries();
  applyRenewables();
  restackMarkers();
  updateReservoirs();
  updateHeader();
}

/* ---------- 24h playback ---------- */

async function prefetchFrames() {
  if (!playing) return;
  let launched = 0;
  for (let i = playIdx; i < Math.min(PLAY_TOTAL, playIdx + PLAY_LOOKAHEAD); i++) {
    if (playBuffer.has(i) || playInFlight.has(i)) continue;
    if (playInFlight.size >= PLAY_CONCURRENCY) break;
    playInFlight.add(i);
    launched++;
    const t = playTimes[i];
    fetch(LIVE_URL + "?t=" + t, { cache: "no-store", signal: playAbort.signal })
      .then((resp) => resp.text())
      .then((text) => { if (playing) playBuffer.set(i, text); })
      .catch(() => { /* frame fetch failed; playback will skip it */ })
      .finally(() => { playInFlight.delete(i); });
  }
  if (launched) setTimeout(prefetchFrames, 25);
}

function playbackTick() {
  if (!playing) return;
  const frameMs = 1000 / PLAY_FPS;
  const totalMs = PLAY_TOTAL * frameMs;
  // Hard stop on wall-clock elapsed, independent of whether every frame
  // actually loaded (a missing tail frame must not hang playback forever).
  if (Date.now() - playStart >= totalMs) { stopPlayback(); return; }
  const targetIdx = Math.min(PLAY_TOTAL - 1, Math.floor((Date.now() - playStart) / frameMs));
  // Apply the newest available frame up to the wall-clock target, freeing each
  // one after use so the buffer stays bounded.
  for (let i = playIdx; i <= targetIdx; i++) {
    const text = playBuffer.get(i);
    if (text === undefined) continue;
    playBuffer.delete(i);
    try {
      applySnapshot(JSON.parse(text), true);
    } catch (e) { /* malformed frame; skip */ }
    playIdx = i + 1;
  }
  if (playIdx >= PLAY_TOTAL) { stopPlayback(); return; }
  prefetchFrames();
  playRaf = requestAnimationFrame(playbackTick);
}

function startPlayback() {
  if (playing) return;
  playing = true;
  playIdx = 0;
  playBuffer.clear();
  playInFlight.clear();
  playAbort = new AbortController();
  const end = Math.floor(Date.now() / 1000 / PLAY_STEP_S) * PLAY_STEP_S;
  const start = end - 24 * 3600;
  playTimes = [];
  for (let i = 0; i < PLAY_TOTAL; i++) playTimes.push(start + i * PLAY_STEP_S);
  playStart = Date.now();
  const btn = document.getElementById("play-24h");
  btn.textContent = "Stop";
  btn.classList.add("playing");
  prefetchFrames();
  playRaf = requestAnimationFrame(playbackTick);
}

function stopPlayback() {
  if (!playing) return;
  playing = false;
  if (playAbort) playAbort.abort();
  if (playRaf) cancelAnimationFrame(playRaf);
  playBuffer.clear();
  playInFlight.clear();
  const btn = document.getElementById("play-24h");
  btn.textContent = "Play 24h";
  btn.classList.remove("playing");
  poll(); // restore live view
}

initGeometry().then(() => {
  wireControls();
  updateLegend();
  poll();
  setInterval(poll, POLL_MS);
});

map.on("zoomend", rescaleMarkers);

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
      // One status gate and one idle threshold, shared with every other
      // marker path via the model - this used to be an inline copy with its
      // own rules (issue #14).
      if (!Model.unitStatusPasses(u.status, showStatus)) continue;
      if (!showIdle && Model.renewableUnitIdle(u, state)) continue;
      const ft = u.fueltech_id || (u.code && u.code.includes("WF") ? "wind" : "solar");
      const isWind = (ft === "wind" || ft === "wind_farm");
      const isSolar = (ft === "solar" || ft === "solar_utility");
      if (isWind && !showWind) continue;
      if (isSolar && !showSolar) continue;
      const cap = u.capacity_mw || 0;
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
      const r = radiusFor(cap);
      const pts = renShape(isWind, loc.lat, loc.lng, r);
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
      layer._sizeR = r;
      layer.setPopupContent(renewablePopup(fac, u, isWind, cap, unitPow, measured, genFrac));
      layer.setTooltipContent(
        '<b>' + (u.code || fac.facility) + '</b> &middot; ' + (isWind ? "wind" : "solar") +
        (cap ? ' &middot; ' + cap.toFixed(0) + ' MW' : '') +
        (measured ? '<br>' + Math.round(unitPow) + ' MW (' + Math.round(genFrac * 100) + '%)' : ''));
    }
  }
  for (const k of existing) { if (renLayers[k]) { map.removeLayer(renLayers[k]); delete renLayers[k]; } }
}

/* ---------- hydro ---------- */

// The pulse decision (5% of capacity, 1 MW floor) lives in the model, as
// hydroAgg's `pulse`: one threshold owner with the rest of the idle bar.

// Pumped storage and classic hydro share one upstream fueltech (OpenNEM has no
// pumped_hydro), so the server tags each facility with a `pumped` flag derived
// from the pump DUIDs, stored-energy capacity and the facility description. The
// two get different shapes here: classic is a plain hexagon, pumped is the same
// hexagon inside a concentric outline.

// Regular n-gon sized in metres, using the same metre->degree scaling as
// renShape and commissionArc, so a hexagon of radius r covers the same footprint
// an L.circle(r) would. Vertex 0 sits at 12 o'clock (pointy-top) matching
// commissionArc's sweep origin, unless offsetDeg rotates it - coal passes 45 so
// its square stands on an edge instead of becoming a diamond like solar.
function polyPoints(lat, lng, rMeters, sides, scale, offsetDeg) {
  const r = rMeters * (scale || 1);
  const off = (offsetDeg || 0) * Math.PI / 180;
  const dLat = 1 / 111320;
  const dLng = 1 / (111320 * Math.max(0.2, Math.cos(lat * Math.PI / 180)));
  const pts = [];
  for (let i = 0; i < sides; i++) {
    const b = off + (i * 360 / sides) * Math.PI / 180;
    pts.push([lat + r * dLat * Math.cos(b), lng + r * dLng * Math.sin(b)]);
  }
  return pts;
}

function hydroTip(fac, agg) {
  const kind = fac.pumped ? "pumped hydro" : "hydro";
  let html = '<b>' + fac.name + '</b> (' + fac.facility + ')<br>' +
    fac.region + ' &middot; ' + kind + ' &middot; ' + agg.cap.toFixed(0) + ' MW';
  if (agg.status) html += ' &middot; ' + agg.status;
  if (!agg.measured) {
    html += ' &middot; <span style="color:#7d8fa5">no live data</span>';
    return html;
  }
  const lines = [];
  if (agg.gen > Model.IDLE_MW) lines.push('<span style="color:#6cd968">generating ' + agg.gen.toFixed(0) + ' MW</span>');
  if (agg.pump > Model.IDLE_MW) lines.push('<span style="color:#5ec1e6">pumping ' + agg.pump.toFixed(0) + ' MW</span>');
  html += '<br>' + (lines.length ? lines.join(' + ') : 'idle');
  if (agg.frac != null) html += ' &middot; ' + Math.round(agg.frac * 100) + '% of capacity';
  if (isFinite(agg.age)) html += ' &middot; ' + agg.age.toFixed(0) + 's old';
  return html;
}

function hydroPopup(fac, agg) {
  const kind = fac.pumped ? "pumped hydro" : "hydro";
  let rows = '<tr><th>unit</th><th>role</th><th class="num">cap</th><th class="num">now</th></tr>';
  for (const u of fac.units || []) {
    const m = state.units && state.units[u.code];
    const mw = m && typeof m.power_mw === "number" ? Math.max(0, m.power_mw) : null;
    rows += '<tr><td>' + u.code + '</td>' +
      '<td>' + (u.fueltech_id === "pumps" ? "pump" : "gen") + '</td>' +
      '<td class="num">' + (u.capacity_mw || 0).toFixed(1) + ' MW</td>' +
      '<td class="num">' + (mw != null ? mw.toFixed(1) + ' MW' : '&ndash;') + '</td></tr>';
  }
  rows += '<tr class="sum"><td>facility</td><td>' + (fac.pumped ? "pumped" : "classic") + '</td>' +
    '<td class="num">' + agg.cap.toFixed(0) + ' MW</td>' +
    '<td class="num">' + (agg.measured ? agg.activity.toFixed(0) + ' MW' : '&ndash;') + '</td></tr>';
  return '<div class="pop"><b>' + fac.name + '</b> <k>' + fac.region + '</k> &middot; ' + kind +
    '<br>facility ' + fac.facility + '<table>' + rows + '</table></div>';
}

function removeHydro(facility) {
  const layer = hydroLayers[facility];
  if (layer) { map.removeLayer(layer); delete hydroLayers[facility]; }
  const ring = hydroRings[facility];
  if (ring) { map.removeLayer(ring); delete hydroRings[facility]; }
}

// One marker per facility rather than per unit: Tumut 3's eight units all share
// a single coordinate, so per-unit polygons would stack invisibly. Generation
// and pumping are summed by the model (hydroAgg) - they come from different
// DUIDs (the `hydro` unit vs the sibling `pumps` unit) and read differently in
// the tooltip.
function applyHydro() {
  if (!state.hydro) state.hydro = [];
  for (const fac of state.hydro) {
    const loc = fac.location;
    const agg = Model.hydroAgg(fac, state);
    if (!showHydro || !loc || !loc.lat || !loc.lng || !Model.facilityStatusVisible(fac, state, visToggles())) {
      removeHydro(fac.facility);
      continue;
    }
    const fill = agg.measured
      ? ramp(agg.frac, STOPS_GEN)
      : (STATUS_COLOR[agg.status] || "#54657a");
    const r = radiusFor(agg.cap);
    const opts = {
      color: "#101a2b",
      weight: 1.5,
      fillColor: fill,
      fillOpacity: agg.measured ? 0.85 : 0.5,
    };
    const pts = polyPoints(loc.lat, loc.lng, r, 6, 1);
    let layer = hydroLayers[fac.facility];
    if (!layer) {
      layer = L.polygon(pts, opts).addTo(map);
      hydroLayers[fac.facility] = layer;
      layer.bindTooltip("", { direction: "top", className: "" });
      layer.bindPopup("", { maxWidth: 320, className: "" });
      layer.on("click", () => layer.setPopupContent(hydroPopup(fac, agg)));
    } else {
      layer.setLatLngs(pts);
      layer.setStyle(opts);
      layer.setPopupContent(hydroPopup(fac, agg));
    }
    layer._sizeR = r;
    // The pulse is on the stroke only (see .hydro-pulse), so the fill colour
    // that encodes output stays steady while the outline throbs.
    try {
      const path = layer._path;
      if (path) {
        if (agg.pulse) path.classList.add("hydro-pulse");
        else path.classList.remove("hydro-pulse");
      }
    } catch (e) {}
    // Pumped storage gets a second, smaller outline laid over the fill. It is
    // non-interactive so the tooltip and popup still belong to the outer shape,
    // and it is drawn after the outer polygon so it stays on top of its fill.
    const ringPts = fac.pumped ? polyPoints(loc.lat, loc.lng, r, 6, 0.55) : null;
    let ring = hydroRings[fac.facility];
    if (!ringPts) {
      if (ring) { map.removeLayer(ring); delete hydroRings[fac.facility]; }
    } else if (!ring) {
      hydroRings[fac.facility] = L.polygon(ringPts, {
        interactive: false,
        color: "#101a2b",
        weight: 1.3,
        fill: false,
        opacity: 0.9,
      }).addTo(map);
    } else {
      ring.setLatLngs(ringPts);
    }
    const liveRing = hydroRings[fac.facility];
    if (liveRing) liveRing._sizeR = r * 0.55;
    layer.setTooltipContent(hydroTip(fac, agg));
  }
}

/* ---------- coal / gas ---------- */

// Thermal plants are point sources with several units on one coordinate (four
// boilers at Bayswater), so they are aggregated per facility like hydro rather
// than drawn per unit like the (usually single-unit) solar and wind farms.
// Shape encodes the fuel - coal a square, gas a pentagon - and colour the output
// as a share of registered capacity, on the same red->yellow->green scale the
// other sources use. No pulse: unlike hydro these run as baseload, so a marker
// would be throbbing almost constantly and the motion would carry no signal.
const THERMAL_SHAPE = { coal: { sides: 4, offsetDeg: 45 }, gas: { sides: 5, offsetDeg: 0 }, distillate: { sides: 3, offsetDeg: 180 } };
const FUEL_LABEL = {
  coal_black: "black coal", coal_brown: "brown coal",
  gas_ccgt: "CCGT", gas_ocgt: "OCGT", gas_steam: "steam",
  gas_recip: "recip", gas_wcmg: "wcmg",
  distillate: "distillate",
};

function fuelSummary(fuels) {
  return Object.keys(fuels).map((k) => (FUEL_LABEL[k] || k) + (fuels[k] > 1 ? " x" + fuels[k] : "")).join(", ");
}

function thermalTip(kind, fac, agg) {
  let html = '<b>' + fac.name + '</b> (' + fac.facility + ')<br>' +
    fac.region + ' &middot; ' + kind + ' &middot; ' + agg.cap.toFixed(0) + ' MW';
  if (agg.status) html += ' &middot; ' + agg.status;
  if (!agg.measured) {
    html += ' &middot; <span style="color:#7d8fa5">no live data</span>';
    return html;
  }
  html += '<br>' + agg.gen.toFixed(0) + ' MW &middot; ' + Math.round(agg.frac * 100) + '% of capacity';
  if (isFinite(agg.age)) html += ' &middot; ' + agg.age.toFixed(0) + 's old';
  return html;
}

function thermalPopup(kind, fac, agg) {
  let rows = '<tr><th>unit</th><th class="num">cap</th><th class="num">now</th></tr>';
  for (const u of fac.units || []) {
    const m = state.units && state.units[u.code];
    const mw = m && typeof m.power_mw === "number" ? Math.max(0, m.power_mw) : null;
    rows += '<tr><td>' + u.code + '</td>' +
      '<td class="num">' + (u.capacity_mw || 0).toFixed(0) + ' MW</td>' +
      '<td class="num">' + (mw != null ? mw.toFixed(0) + ' MW' : '&ndash;') + '</td></tr>';
  }
  rows += '<tr class="sum"><td>facility</td>' +
    '<td class="num">' + agg.cap.toFixed(0) + ' MW</td>' +
    '<td class="num">' + (agg.measured ? agg.gen.toFixed(0) + ' MW' : '&ndash;') + '</td></tr>';
  const fs = fuelSummary(agg.fuels);
  return '<div class="pop"><b>' + fac.name + '</b> <k>' + fac.region + '</k> &middot; ' + kind +
    (fs ? ' (' + fs + ')' : '') +
    '<br>facility ' + fac.facility + '<table>' + rows + '</table></div>';
}

function applyThermal(kind, facs, show, layers) {
  for (const fac of facs || []) {
    const loc = fac.location;
    if (!show || !loc || !loc.lat || !loc.lng || !Model.facilityStatusVisible(fac, state, visToggles())) {
      const l = layers[fac.facility];
      if (l) { map.removeLayer(l); delete layers[fac.facility]; }
      continue;
    }
    const agg = Model.thermalAgg(fac, state);
    const fill = agg.measured
      ? ramp(agg.frac, STOPS_GEN)
      : (STATUS_COLOR[agg.status] || "#54657a");
    const spec = THERMAL_SHAPE[kind];
    const r = radiusFor(agg.cap);
    const pts = polyPoints(loc.lat, loc.lng, r, spec.sides, 1, spec.offsetDeg);
    const opts = {
      color: "#101a2b",
      weight: 1.5,
      fillColor: fill,
      fillOpacity: agg.measured ? 0.85 : 0.5,
    };
    let layer = layers[fac.facility];
    if (!layer) {
      layer = L.polygon(pts, opts).addTo(map);
      layers[fac.facility] = layer;
      layer.bindTooltip("", { direction: "top", className: "" });
      layer.bindPopup("", { maxWidth: 320, className: "" });
      layer.on("click", () => layer.setPopupContent(thermalPopup(kind, fac, agg)));
    } else {
      layer.setLatLngs(pts);
      layer.setStyle(opts);
      layer.setPopupContent(thermalPopup(kind, fac, agg));
    }
    layer._sizeR = r;
    layer.setTooltipContent(thermalTip(kind, fac, agg));
  }
}

/* ---------- zoom rescale ---------- */

// Marker geometry is rebuilt at the current zoom on every poll, but a zoom between
// polls would otherwise keep the size it was last drawn at (Leaflet just
// reprojects the metre polygons, which grows them 2x a level). Re-running the
// marker layers applies the damped size immediately. Only the marker layers are
// re-applied; region fills, lines and arrows do not depend on viewScale().
function rescaleMarkers() {
  applyRenewables();
  applyHydro();
  applyThermal("coal", state.coal, showCoal, coalLayers);
  applyThermal("gas", state.gas, showGas, gasLayers);
  applyThermal("distillate", state.distillate, showDistillate, distillateLayers);
  applyBatteries();
  restackMarkers();
}

/* ---------- marker z-order ---------- */

// Leaflet draws SVG vector layers in the order they were added, so a big plant
// sits on top of a small neighbour and can hide it. Re-fronting every marker by
// its drawn size after each pass puts the largest at the back and the smallest at
// the front, so the little ones stay visible. The decorative pumped-hydro outline
// and commissioning arc carry a size too (the ring is smaller than its hexagon,
// the arc ties with its circle) so they ride just above their own marker instead
// of being buried by it.
function restackMarkers() {
  const all = [];
  for (const dict of [batteryLayers, hydroLayers, coalLayers, gasLayers, distillateLayers, renLayers,
                      hydroRings, batteryArcs]) {
    for (const k in dict) {
      const l = dict[k];
      if (l && typeof l._sizeR === "number") all.push(l);
    }
  }
  all.sort((a, b) => b._sizeR - a._sizeR); // largest first -> ends up lowest
  for (const l of all) l.bringToFront();
}

/* ---------- reservoirs ---------- */

// Only the Snowy Hydro lakes are published, and each is a catchment-wide storage
// that maps to no single station, so they get their own card instead of a
// per-facility tooltip. Data comes from the reservoir-exporter, which the compose
// file runs with --all-reservoirs.
const RESERVOIR_ORDER = ["Lake Eucumbene", "Lake Jindabyne", "Tantangara Reservoir"];

// "Lake Eucumbene" -> "Eucumbene" (the card's heading already says where they are).
function shortReservoirName(name) {
  return String(name).replace(/^Lake\s+/i, "").replace(/\s+Reservoir$/i, "");
}

// The upstream publishes these once a day, so a reading is old by nature. Shown
// as a date rather than a live age, because "as of 15 Oct" is the honest framing
// for a daily figure and "% full, 12h old" would suggest a measurement cadence
// that does not exist.
function reservoirObserved(names) {
  let newest = 0;
  for (const n of names) {
    const t = (state.reservoirs[n] || {}).last_sample;
    if (typeof t === "number" && t > newest) newest = t;
  }
  if (!newest) return "";
  const d = new Date(newest * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return "observed " + d.getDate() + " " + months[d.getMonth()];
}

function updateReservoirs() {
  const el = document.getElementById("resv");
  const rowsEl = document.getElementById("resv-rows");
  const data = state.reservoirs || {};
  const names = RESERVOIR_ORDER.filter((n) => data[n]);
  for (const n of Object.keys(data)) if (names.indexOf(n) < 0) names.push(n);
  if (!names.length) { el.hidden = true; return; }
  let html = "";
  for (const n of names) {
    const d = data[n] || {};
    const lvl = typeof d.level === "number" ? d.level.toFixed(0) + "%" : "&ndash;";
    let delta = '<span class="resv-delta"></span>';
    if (typeof d.change_7d === "number" && Math.abs(d.change_7d) >= 0.05) {
      const up = d.change_7d > 0;
      delta = '<span class="resv-delta ' + (up ? "up" : "down") + '" title="' +
        (up ? "+" : "") + d.change_7d.toFixed(1) + ' pp over 7 days">' +
        (up ? "&uarr;" : "&darr;") + '</span>';
    }
    html += '<div class="resv-row"><span class="resv-name">' + shortReservoirName(n) + '</span>' +
      '<span class="resv-pct">' + lvl + '</span>' + delta + '</div>';
  }
  const observed = reservoirObserved(names);
  const capEl = document.getElementById("resv-cap");
  if (capEl) capEl.innerHTML = '% full, of gross storage &middot; arrow = 7-day change' +
    (observed ? '<br>' + observed : '');
  rowsEl.innerHTML = html;
  el.hidden = false;
}

// A small hexagon, matching the markers, with the pumped variant's inner outline.
function hydroGlyph(pumped) {
  const hex = (r) => {
    const pts = [];
    for (let i = 0; i < 6; i++) {
      const b = (i * 60) * Math.PI / 180;
      pts.push((7 + r * Math.sin(b)).toFixed(2) + ',' + (7 - r * Math.cos(b)).toFixed(2));
    }
    return pts.join(' ');
  };
  let svg = '<polygon points="' + hex(6) + '" fill="#8ca3ba" stroke="#101a2b" stroke-width="1.1"/>';
  if (pumped) svg += '<polygon points="' + hex(3.3) + '" fill="none" stroke="#101a2b" stroke-width="1.1"/>';
  return '<span class="glyph"><svg width="14" height="14" viewBox="0 0 14 14">' + svg + '</svg></span>';
}

// The coal square and gas pentagon, matching the markers' shape and rotation.
function thermalGlyph(sides, offsetDeg) {
  const r = 6, off = (offsetDeg || 0) * Math.PI / 180;
  const pts = [];
  for (let i = 0; i < sides; i++) {
    const b = off + i * 2 * Math.PI / sides;
    pts.push((7 + r * Math.sin(b)).toFixed(2) + ',' + (7 - r * Math.cos(b)).toFixed(2));
  }
  return '<span class="glyph"><svg width="14" height="14" viewBox="0 0 14 14">' +
    '<polygon points="' + pts.join(' ') + '" fill="#8ca3ba" stroke="#101a2b" stroke-width="1.1"/></svg></span>';
}


function updateGfxScaleLabel() {
  const el = document.getElementById('gfx-val');
  if (el) el.textContent = gfxScale.toFixed(2);
}

function bindGfxScale() {
  const s = document.getElementById('gfx-slider');
  if (s) {
    s.value = String(gfxScale);
    s.addEventListener('input', (e) => {
      gfxScale = Math.max(GFX_SCALE_MIN, Math.min(GFX_SCALE_MAX, parseFloat(e.target.value)));
      updateGfxScaleLabel();
      rescaleMarkers();
    });
    updateGfxScaleLabel();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bindGfxScale);
} else {
  bindGfxScale();
}

