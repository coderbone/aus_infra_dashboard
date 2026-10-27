// The web map's model layer: the domain logic over the /data/live payload,
// pure - every function is a function of (payload state, toggle state) and
// touches neither the DOM nor Leaflet - so webmap/test_model.js can pin it in
// Node with no browser at all.
//
// app.js is the draw layer: it owns the map, the layer registries, the colours
// and the popup/tooltip HTML, and asks this module what to draw. The payload
// field names this module reads are declared in PAYLOAD_FIELDS and pinned by
// webmap/test_server.py against the server's own contract (server.LIVE_PAYLOAD),
// so a rename fails a test on both sides of the seam.
//
// Loads as a classic script in the browser (global `Model`) and as a CommonJS
// module in Node; there is no build step in this repo and there does not need
// to be one.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Model = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // A facility is idle below this output. One owner: the idle filter, the
  // status gate's idle pass, the renewable per-unit filter, the ring's band
  // floor and the "is it flowing" labels all read it here, so tightening or
  // loosening the bar is one edit.
  const IDLE_MW = 0.5;

  // How old a reading may be before the UI stops presenting it as live. One
  // table: the header's fresh/stale/down classes and the interconnector
  // tooltip's "stale" flag read it, nothing else hard-codes a cutoff.
  const STALENESS = {
    interconnector_stale_s: 900,
    header_fresh_s: 120,
    header_stale_s: 600,
  };

  // Every payload field this module reads, as a dotted path into the /data/live
  // payload: `*` steps into a wildcard member (a region/unit map or a facility
  // list's elements). webmap/test_server.py resolves each path against
  // server.LIVE_PAYLOAD, so a field renamed on either side of the seam fails
  // there instead of blanking a map layer in production.
  //
  // Deliberately absent: `regions.*.fossil_mw`. Fossil generation is the
  // coal+gas+distillate rollup below (GLOSSARY.md, "Fossil") - the server
  // still publishes its own rollup as part of the contract, but the map reads
  // one implementation of fossil, this one, so the region fill and the popup
  // ring cannot disagree.
  const PAYLOAD_FIELDS = [
    "regions.*.stored_mwh",
    "regions.*.rooftop_mw",
    "regions.*.import_mw",
    "regions.*.export_mw",
    "regions.*.curtail.*",
    "units.*.power_mw",
    "units.*.age_s",
    "batteries.*.inf_stored_mwh",
    "batteries.*.inf_age_s",
    "batteries.*.peak_stored_mwh",
    "batteries.*.aemo_power_mw",
    "batteries.*.aemo_power_age_s",
    "fleet.*.region",
    "fleet.*.kind",
    "fleet.*.units.*.code",
    "fleet.*.units.*.capacity_mwh",
    "fleet.*.units.*.status",
    "renewables.*.region",
    "renewables.*.kind",
    "renewables.*.units.*.code",
    "renewables.*.units.*.status",
    "renewables.*.units.*.fueltech_id",
    "hydro.*.region",
    "hydro.*.kind",
    "hydro.*.units.*.code",
    "hydro.*.units.*.capacity_mw",
    "hydro.*.units.*.status",
    "hydro.*.units.*.fueltech_id",
    "coal.*.region",
    "coal.*.kind",
    "coal.*.units.*.code",
    "coal.*.units.*.capacity_mw",
    "coal.*.units.*.status",
    "coal.*.units.*.fueltech_id",
    "gas.*.region",
    "gas.*.kind",
    "gas.*.units.*.code",
    "gas.*.units.*.capacity_mw",
    "gas.*.units.*.status",
    "gas.*.units.*.fueltech_id",
    "distillate.*.region",
    "distillate.*.kind",
    "distillate.*.units.*.code",
    "distillate.*.units.*.capacity_mw",
    "distillate.*.units.*.status",
    "distillate.*.units.*.fueltech_id",
  ];

  /* ---------- status gate ---------- */

  // The one status gate: a dispatch unit's status spelling against the four
  // status toggles. A spelling the toggles do not know is not the toggles'
  // business and passes - the facility gate still requires a known selected
  // status or a facility-level flag before showing anything, so the two
  // callers keep their own policy on unknown spellings while sharing this one
  // mapping. The renewable draw path used to carry its own copy of this gate
  // that treated operating and operational as one combined bucket; the gate is
  // now per spelling everywhere, so each toggle owns exactly its spelling.
  function unitStatusPasses(status, showStatus) {
    const st = (status || "").toLowerCase();
    if (st === "operating") return !!showStatus.operating;
    if (st === "operational") return !!showStatus.operational;
    if (st === "commissioning") return !!showStatus.commissioning;
    if (st === "committed") return !!showStatus.committed;
    return true;
  }

  // Whether a unit's status is one the toggles select on. The facility gate
  // counts only known spellings toward "some unit is selected"; an all-unknown
  // facility falls through to its facility-level flags.
  function unitStatusSelected(status, showStatus) {
    const st = (status || "").toLowerCase();
    if (st !== "operating" && st !== "operational" &&
        st !== "commissioning" && st !== "committed") return false;
    return unitStatusPasses(st, showStatus);
  }

  // The kinds the idle toggle applies to. A facility of another kind (or of
  // none) is never idle-filtered.
  const IDLE_KINDS = ["battery", "hydro", "coal", "gas", "distillate", "renewable"];

  // A facility is "committed" when none of its units are yet active. Committed
  // facilities are built but not operating, so they are hidden when the toggle
  // is off while remaining available when thinking about future capacity.
  function isCommitted(fac) {
    const units = fac.units || [];
    return units.length > 0 && units.every((u) => u.status === "committed");
  }

  function isCommissioning(fac) {
    return (fac.units || []).some((u) => u.status === "commissioning");
  }

  /* ---------- live readings ---------- */

  function unitPower(code, state) {
    // AEMO's 5-min Dispatch_SCADA MW is the only per-unit power source now: the
    // OE exporter retired its `power` family on 2026-10-20 once AEMO coverage
    // proved a superset. Positive is discharging, negative charging.
    const m = state.batteries[code];
    if (!m || typeof m.aemo_power_mw !== "number") return null;
    return {
      mw: m.aemo_power_mw,
      src: "aemo",
      age: typeof m.aemo_power_age_s === "number" ? m.aemo_power_age_s : Infinity,
    };
  }

  function facilityPower(fac, state) {
    let sumMw = 0;
    let has = false;
    for (const u of fac.units) {
      const p = unitPower(u.code, state);
      if (p) { sumMw += p.mw; has = true; }
    }
    return has ? { mw: sumMw } : null;
  }

  /* ---------- idle semantics ---------- */

  function isIdleFacility(fac, state) {
    if (!fac || !fac.units) return false;
    // Batteries are idle if not charging/discharging significantly
    if (fac.kind === "battery") {
      let sumMw = 0;
      let has = false;
      for (const u of fac.units || []) {
        const p = unitPower(u.code, state);
        if (p && typeof p.mw === "number") { sumMw += p.mw; has = true; }
      }
      if (!has) {
        const fp = facilityPower(fac, state);
        if (!fp || typeof fp.mw !== "number") return true;
        return Math.abs(fp.mw) < IDLE_MW;
      }
      return Math.abs(sumMw) < IDLE_MW;
    }
    // For generators, check if they're producing significant power
    if (fac.kind === "renewable") {
      let gen = 0, measured = false;
      for (const u of fac.units) {
        const m = state.units && state.units[u.code];
        if (!m || typeof m.power_mw !== "number") continue;
        measured = true;
        gen += Math.max(0, m.power_mw);
      }
      if (!measured) return true;
      return gen < IDLE_MW;
    }
    if (fac.kind === "hydro") {
      let activity = 0, measured = false;
      for (const u of fac.units || []) {
        const m = state.units && state.units[u.code];
        if (!m || typeof m.power_mw !== "number") continue;
        measured = true;
        activity += Math.abs(m.power_mw);
      }
      if (!measured) {
        const agg = hydroAgg(fac, state);
        if (agg && agg.measured) return (agg.activity || 0) < IDLE_MW;
        return false;
      }
      return activity < IDLE_MW;
    }
    if (fac.kind === "coal" || fac.kind === "gas" || fac.kind === "distillate") {
      const agg = thermalAgg(fac, state);
      if (!agg.measured) {
        // If we don't have live data, don't hide based on idle
        return false;
      }
      return agg.gen < IDLE_MW;
    }
    // Fallback: check any unit power
    let anyMw = null;
    for (const u of fac.units) {
      const p = unitPower(u.code, state);
      if (p && typeof p.mw === "number") { anyMw = p.mw; break; }
    }
    if (anyMw !== null) return Math.abs(anyMw) < IDLE_MW;
    return false;
  }

  // The renewable draw path is per unit (each unit gets its own marker), so its
  // idle question is per unit and its own: a unit whose live MW is known and
  // below the idle bar is idle; a unit with no live reading is kept, because
  // absence of data is not evidence of idleness. Facility-level renewable idle
  // (isIdleFacility above) deliberately answers the opposite for unmeasured
  // facilities - a facility nothing reports for is not drawing anything worth
  // keeping when idle is filtered - and both policies share IDLE_MW.
  function renewableUnitIdle(unit, state) {
    const m = state.units && state.units[unit.code];
    const mw = m && m.power_mw;
    if (typeof mw !== "number") return false;
    return Math.abs(mw) < IDLE_MW;
  }

  // The status gate and the idle filter, once, for every facility-kind marker.
  // `toggles` is {showIdle, showStatus}.
  function facilityStatusVisible(fac, state, toggles) {
    if (!fac || !fac.units) return false;
    // Apply idle filter first - applies to units of any fuel type
    if (!toggles.showIdle && IDLE_KINDS.indexOf(fac.kind) >= 0) {
      if (isIdleFacility(fac, state)) return false;
    }
    // If any unit is in a selected status, show. Also handle facilities that
    // are all committed.
    for (const u of fac.units) {
      if (unitStatusSelected(u.status, toggles.showStatus)) return true;
    }
    // If no unit matched known statuses, check by facility-level flags
    if (isCommitted(fac) && toggles.showStatus.committed) return true;
    if (isCommissioning(fac) && toggles.showStatus.commissioning) return true;
    return false;
  }

  /* ---------- state of charge ---------- */

  function unitSoc(code, capacityMwh, state) {
    // AEMO's live inferred SOC (daily anchor + integrated 5-min SCADA) is the
    // only per-unit SOC now; the OE exporter retired its measured ratio on
    // 2026-10-20. Inference is flagged so the UI can label it as an estimate.
    // The denominator is the unit's registered storage capacity (MWh) - never
    // its registered power capacity, which is a units error that still
    // produces a plausible number (GLOSSARY.md, "Registered capacity").
    const m = state.batteries[code];
    if (!m || typeof m.inf_stored_mwh !== "number" || !(capacityMwh > 0)) return null;
    return {
      soc: m.inf_stored_mwh / capacityMwh,
      src: "inferred",
      age: typeof m.inf_age_s === "number" ? m.inf_age_s : Infinity,
    };
  }

  function facilitySoc(fac, state) {
    let wsum = 0, w = 0;
    for (const u of fac.units) {
      const s = unitSoc(u.code, u.capacity_mwh, state);
      if (!s) continue;
      wsum += s.soc * u.capacity_mwh;
      w += u.capacity_mwh;
    }
    if (!w) return null;
    return { soc: wsum / w, src: "inferred" };
  }

  // Two numbers drive a commissioning facility's marker:
  //   frac  peak stored energy over the commissioning window / registered
  //         storage capacity - how much of the battery has been energised.
  //   fill  current inferred stored energy / peak stored so far - how charged
  //         the energised plant is right now, as a fraction of the most it
  //         has ever held.
  // Null when the unit publishes no peak series (e.g. the WEM facilities), in
  // which case no arc is drawn.
  function facilityCommission(fac, state) {
    let peak = 0, cur = 0, cap = 0, anyPeak = false, anyCur = false;
    for (const u of fac.units || []) {
      const c = u.capacity_mwh || 0;
      if (c <= 0) continue;
      cap += c;
      const m = state.batteries[u.code];
      if (m && typeof m.peak_stored_mwh === "number") { peak += m.peak_stored_mwh; anyPeak = true; }
      if (m && typeof m.inf_stored_mwh === "number") { cur += m.inf_stored_mwh; anyCur = true; }
    }
    if (!anyPeak || cap <= 0) return null;
    const frac = Math.max(0, Math.min(1, peak / cap));
    const fill = anyCur && peak > 0 ? Math.max(0, Math.min(1, cur / peak)) : null;
    return { frac, fill };
  }

  function facilityTotal(fac) {
    let total = 0;
    for (const u of fac.units) total += u.capacity_mwh || 0;
    return total;
  }

  /* ---------- facility aggregates ---------- */

  // Output at or above which a hydro marker pulses. A station trickling a few
  // MW out of a 1,500 MW scheme is not "running", so the bar is 5% of
  // registered capacity - but never below 1 MW, since 5% of a 1.4 MW
  // mini-hydro is noise.
  const HYDRO_PULSE_FRAC = 0.05;
  const HYDRO_PULSE_MIN_MW = 1;

  // One aggregate per hydro facility: generation and pumping summed separately
  // (they come from different DUIDs - the `hydro` unit vs the sibling `pumps`
  // unit), plus the pulse decision and the freshest unit age.
  function hydroAgg(fac, state) {
    let gen = 0, pump = 0, cap = 0, measured = false, age = Infinity;
    for (const u of fac.units || []) {
      cap += u.capacity_mw || 0;
      const m = state.units && state.units[u.code];
      if (!m || typeof m.power_mw !== "number") continue;
      const mw = Math.max(0, m.power_mw);
      measured = true;
      if (u.fueltech_id === "pumps") pump += mw; else gen += mw;
      if (typeof m.age_s === "number" && m.age_s < age) age = m.age_s;
    }
    const activity = gen + pump;
    const frac = cap > 0 ? Math.max(0, Math.min(1, activity / cap)) : null;
    const pulse = measured && activity >= Math.max(HYDRO_PULSE_MIN_MW, HYDRO_PULSE_FRAC * cap);
    const status = (fac.units || []).map((u) => u.status).filter(Boolean)[0] || "";
    return { gen, pump, cap, activity, frac, measured, pulse, age, status };
  }

  // One aggregate per thermal facility: generation as a share of registered
  // capacity, plus the fueltech tally the popup summarises.
  function thermalAgg(fac, state) {
    let cap = 0, gen = 0, measured = false, age = Infinity;
    const fuels = {};
    for (const u of fac.units || []) {
      cap += u.capacity_mw || 0;
      fuels[u.fueltech_id] = (fuels[u.fueltech_id] || 0) + 1;
      const m = state.units && state.units[u.code];
      if (!m || typeof m.power_mw !== "number") continue;
      measured = true;
      gen += Math.max(0, m.power_mw);
      if (typeof m.age_s === "number" && m.age_s < age) age = m.age_s;
    }
    const frac = cap > 0 ? Math.max(0, Math.min(1, gen / cap)) : null;
    const status = (fac.units || []).map((u) => u.status).filter(Boolean)[0] || "";
    return { cap, gen, measured, frac, age, status, fuels };
  }

  /* ---------- region aggregation ---------- */

  // Sum the positive output of a region's facilities, optionally filtered to
  // one fueltech; null (rather than 0) when nothing in the region reported.
  function genSum(state, code, facs, pick) {
    let sum = 0, any = false;
    for (const fac of facs || []) {
      if (fac.region !== code) continue;
      for (const u of fac.units || []) {
        if (pick && !pick(u)) continue;
        const m = state.units && state.units[u.code];
        if (!m || typeof m.power_mw !== "number") continue;
        any = true;
        sum += Math.max(0, m.power_mw);
      }
    }
    return any ? sum : null;
  }

  // The fossil rollup, once. GLOSSARY.md, "Fossil": coal, gas and distillate
  // - the set that rolls up into a region's fossil generation. Each band is
  // the region's positive live output for that bucket, floored at the idle bar
  // so a trickling unit is not drawn as a category; a bucket nothing reported
  // for is absent rather than zero. The region fill's fossil metric and the
  // popup ring's fossil bands are both this list, so they agree by
  // construction - there is no second inclusion rule to drift.
  function regionFossilBands(state, code) {
    const bands = [];
    const put = (key, label, facs) => {
      const value = genSum(state, code, facs);
      if (typeof value === "number" && isFinite(value) && value > IDLE_MW) {
        bands.push({ key, label, value });
      }
    };
    put("coal", "Coal", state.coal);
    put("gas", "Gas", state.gas);
    put("distillate", "Distillate", state.distillate);
    return bands;
  }

  // The region's fossil generation in MW: the sum of the fossil bands the
  // popup ring draws, or null when no fossil unit in the region reported.
  function regionFossilMw(state, code) {
    const bands = regionFossilBands(state, code);
    if (!bands.length) return null;
    return bands.reduce((a, s) => a + s.value, 0);
  }

  // Everything the region popup needs, aggregated from data already on the
  // page: the per-unit live MW in state.units (all generators and loads,
  // keyed by fleet unit code - see docs/adr/0001), the freshest battery
  // power (unitPower: AEMO 5-min SCADA) and the per-fuel curtailment the
  // AEMO exporter publishes per region. Generation and load are kept apart;
  // within each, a fuel total can net several units. A category with no live
  // series is left out rather than drawn as zero. Segments carry a key, a
  // label and a value; colours belong to the draw layer and are attached by
  // the caller.
  function regionFlows(state, code) {
    const gen = [], load = [];
    const add = (arr, key, label, value) => {
      if (typeof value !== "number" || !isFinite(value) || value <= IDLE_MW) return;
      const hit = arr.find((s) => s.key === key);
      if (hit) hit.value += value; else arr.push({ key, label, value });
    };
    const unitMw = (code) => {
      const m = state.units && state.units[code];
      return m && typeof m.power_mw === "number" ? m.power_mw : null;
    };
    // Fossil comes from the one rollup, so the ring's coal+gas+distillate
    // bands are the region fill's fossil number by construction.
    for (const band of regionFossilBands(state, code)) {
      add(gen, band.key, band.label, band.value);
    }
    add(gen, "hydro", "Hydro", genSum(state, code, state.hydro, (u) => u.fueltech_id === "hydro"));
    add(gen, "wind", "Wind", genSum(state, code, state.renewables, (u) => u.fueltech_id === "wind" || u.fueltech_id === "wind_farm"));
    add(gen, "solar", "Solar", genSum(state, code, state.renewables, (u) => u.fueltech_id === "solar" || u.fueltech_id === "solar_utility"));
    const rooftopMw = (state.regions[code] || {}).rooftop_mw;
    add(gen, "rooftop", "Rooftop solar", (typeof rooftopMw === "number" && isFinite(rooftopMw) && rooftopMw >= 0) ? rooftopMw : null);
    // Imports from other regions count as supply, so they sit in the
    // generation band; exports are demand and sit in the load band. The
    // AEMO exporter derives both per region from the interconnector flows.
    const region = state.regions[code] || {};
    add(gen, "imports", "Imports", region.import_mw);

    // Batteries are bidirectional: discharging is generation, charging is
    // load. unitPower picks the freshest of the two power sources, exactly as
    // the markers do, so the ring agrees with what is on the map.
    let batGen = 0, batLoad = 0, batAny = false;
    for (const fac of state.fleet || []) {
      if (fac.region !== code) continue;
      for (const u of fac.units || []) {
        const p = unitPower(u.code, state);
        if (!p || typeof p.mw !== "number") continue;
        batAny = true;
        if (p.mw > 0) batGen += p.mw; else batLoad += -p.mw;
      }
    }
    add(gen, "battery", "Battery", batAny ? batGen : null);
    add(load, "battery", "Battery charge", batAny ? batLoad : null);

    // Pumps are the load units behind pumped hydro; AEMO signs them negative.
    let pumpLoad = 0, pumpAny = false;
    for (const fac of state.hydro || []) {
      if (fac.region !== code) continue;
      for (const u of fac.units || []) {
        if (u.fueltech_id !== "pumps") continue;
        const mw = unitMw(u.code);
        if (typeof mw !== "number") continue;
        pumpAny = true;
        pumpLoad += Math.max(0, -mw);
      }
    }
    add(load, "pumps", "Pumps", pumpAny ? pumpLoad : null);
    add(load, "exports", "Exports", region.export_mw);

    // Per-region curtailment, added to the generation band as unrealised
    // output.
    const cur = (state.regions[code] || {}).curtail || {};
    add(gen, "wind_curt", "Wind curtailed", cur.wind);
    add(gen, "solar_curt", "Solar curtailed", cur.solar);

    const totalGen = gen.reduce((a, s) => a + s.value, 0);
    const totalLoad = load.reduce((a, s) => a + s.value, 0);
    return { gen, load, totalGen, totalLoad, max: Math.max(totalGen, totalLoad) };
  }

  // The region fill's value for the selected metric. Fossil is the one
  // rollup (regionFossilMw), not the server's separately-computed field, so
  // the fill and the popup ring cannot disagree.
  function regionMetricValue(state, metric, code) {
    const r = state.regions[code];
    if (!r) return null;
    if (metric === "stored") return typeof r.stored_mwh === "number" ? r.stored_mwh : null;
    if (metric === "fossil") return regionFossilMw(state, code);
    if (metric === "curtail") {
      const c = r.curtail || {};
      let sum = 0, any = false;
      for (const v of Object.values(c)) { if (typeof v === "number") { sum += v; any = true; } }
      return any ? sum : null;
    }
    return null;
  }

  /* ---------- staleness ---------- */

  function isStaleAge(ageS) {
    return typeof ageS === "number" && ageS > STALENESS.interconnector_stale_s;
  }

  function headerFreshness(ageS) {
    if (ageS <= STALENESS.header_fresh_s) return "fresh";
    if (ageS <= STALENESS.header_stale_s) return "stale";
    return "down";
  }

  return {
    IDLE_MW,
    STALENESS,
    PAYLOAD_FIELDS,
    unitStatusPasses,
    unitStatusSelected,
    isCommissioning,
    unitPower,
    facilityPower,
    isIdleFacility,
    renewableUnitIdle,
    facilityStatusVisible,
    unitSoc,
    facilitySoc,
    facilityCommission,
    facilityTotal,
    hydroAgg,
    thermalAgg,
    regionFossilBands,
    regionFossilMw,
    regionFlows,
    regionMetricValue,
    isStaleAge,
    headerFreshness,
  };
});
