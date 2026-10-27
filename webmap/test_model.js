// Characterization tests for the web map's model layer (webmap/static/model.js).
// Pure Node - no DOM, no Leaflet, no browser: the model is a function of
// (payload state, toggle state) and these tests are the seam that pins it.
//
// Expected values are worked examples of the behaviour the map shipped with
// (the logic formerly inlined in app.js), so the extraction into model.js is
// pinned rather than re-invented. Run with:
//
//     node --test webmap/test_model.js
//
// The payload field names the model reads are pinned against the server's own
// contract by webmap/test_server.py (ModelPayloadContractTest), not here: that
// pin has to see both sides of the seam and only Python can.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Model = require("./static/model.js");

/* ---------- fixtures ---------- */

// A minimal live payload in the shape server.build_live produces: the groups
// the model reads, with just enough members to exercise each rule.
function emptyState() {
  return {
    regions: {},
    interconnectors: {},
    batteries: {},
    units: {},
    reservoirs: {},
    fleet: [],
    renewables: [],
    hydro: [],
    coal: [],
    gas: [],
    distillate: [],
  };
}

function unit(code, extra) {
  return Object.assign({ code, status: "operating", capacity_mw: 100, capacity_mwh: 0, fueltech_id: "battery" }, extra);
}

function fac(kind, region, units, extra) {
  return Object.assign({ facility: kind.toUpperCase() + "1", name: kind + " one", region, location: { lat: -33.0, lng: 145.0 }, kind, units }, extra);
}

const ALL_ON = { showIdle: true, showStatus: { operating: true, operational: true, commissioning: true, committed: true } };

/* ---------- the fossil rollup: one implementation ---------- */

// The worked example: three fossil buckets in NSW1 with the inclusion rules
// the popup ring shipped with - negatives clamp to zero, sub-idle-bar output
// is not a category, unmeasured units do not count, other regions do not
// leak in - and TAS1, with no fossil plant, stays empty.
function fossilState() {
  const s = emptyState();
  s.regions = { NSW1: {}, TAS1: {} };
  s.coal = [fac("coal", "NSW1", [
    unit("COAL1", { fueltech_id: "coal_black", capacity_mw: 500 }),
    unit("COAL2", { fueltech_id: "coal_black", capacity_mw: 500 }),
    unit("COAL3", { fueltech_id: "coal_black", capacity_mw: 500 }),
  ])];
  s.gas = [fac("gas", "NSW1", [unit("GAS1", { fueltech_id: "gas_ccgt", capacity_mw: 300 })])];
  s.distillate = [fac("distillate", "NSW1", [unit("DIST1", { fueltech_id: "distillate", capacity_mw: 50 })])];
  s.coal.push(fac("coal", "QLD1", [unit("COALQ", { fueltech_id: "coal_black", capacity_mw: 900 })]));
  s.units = {
    COAL1: { power_mw: 400 },
    COAL2: { power_mw: -50 },   // pumping/aux load: clamps to 0
    // COAL3 unmeasured: contributes nothing
    GAS1: { power_mw: 300.2 },
    DIST1: { power_mw: 0.3 },   // below the idle bar: not a category
    COALQ: { power_mw: 999 },   // wrong region for NSW1
  };
  return s;
}

test("fossil bands: coal, gas and distillate of one region, positives only, floored at the idle bar", () => {
  const bands = Model.regionFossilBands(fossilState(), "NSW1");
  assert.deepEqual(bands.map((b) => b.key), ["coal", "gas"]);
  assert.equal(bands[0].value, 400);      // 400 + max(0, -50)
  assert.equal(bands[1].value, 300.2);
});

test("fossil MW is the sum of the fossil bands, and null when none survive", () => {
  assert.equal(Model.regionFossilMw(fossilState(), "NSW1"), 700.2);
  assert.equal(Model.regionFossilMw(fossilState(), "TAS1"), null);
});

test("region fill fossil and the popup ring agree by construction", () => {
  // The one thing criterion 1 of #14 asks for: the fill's fossil metric is
  // not a second computation - it is the ring's fossil bands summed.
  const s = fossilState();
  const fill = Model.regionMetricValue(s, "fossil", "NSW1");
  const flows = Model.regionFlows(s, "NSW1");
  const ringFossil = flows.gen
    .filter((seg) => seg.key === "coal" || seg.key === "gas" || seg.key === "distillate")
    .reduce((a, seg) => a + seg.value, 0);
  assert.equal(fill, Model.regionFossilMw(s, "NSW1"));
  assert.equal(fill, ringFossil);
});

test("the fill no longer reads the server's separately-rolled fossil field", () => {
  // regions.fossil_mw stays in the payload contract, but the map's fossil
  // metric is the model rollup: a payload whose fossil_mw disagrees with its
  // drawn buckets fills from the buckets.
  const s = fossilState();
  s.regions.NSW1.fossil_mw = 12345;
  assert.equal(Model.regionMetricValue(s, "fossil", "NSW1"), 700.2);
});

test("stored and curtail metrics are unchanged reads of the region record", () => {
  const s = emptyState();
  s.regions = { NSW1: { stored_mwh: 2500, curtail: { wind: 3, solar: 2 } } };
  assert.equal(Model.regionMetricValue(s, "stored", "NSW1"), 2500);
  assert.equal(Model.regionMetricValue(s, "curtail", "NSW1"), 5);
  assert.equal(Model.regionMetricValue(s, "stored", "NSW2"), null); // unknown region
  assert.equal(Model.regionMetricValue(s, "none", "NSW1"), null);
});

/* ---------- region flows ---------- */

test("region flows: generation and load bands, totals, and the shared max scale", () => {
  const s = fossilState();
  s.regions.NSW1 = { rooftop_mw: 5, import_mw: 12, export_mw: 7, curtail: { wind: 3, solar: 2 } };
  s.fleet = [fac("battery", "NSW1", [unit("BAT1"), unit("BAT2")])];
  s.batteries = { BAT1: { aemo_power_mw: 100 }, BAT2: { aemo_power_mw: -40 } };
  s.hydro = [fac("hydro", "NSW1", [
    unit("HYD1", { fueltech_id: "hydro", capacity_mw: 200 }),
    unit("PUMP1", { fueltech_id: "pumps", capacity_mw: 50 }),
  ], { pumped: true })];
  s.units.HYD1 = { power_mw: 50 };
  s.units.PUMP1 = { power_mw: -30 };
  s.renewables = [
    fac("renewable", "NSW1", [unit("WF1", { fueltech_id: "wind", capacity_mw: 40 })]),
    fac("renewable", "NSW1", [unit("SF1", { fueltech_id: "solar_utility", capacity_mw: 20 })]),
  ];
  s.units.WF1 = { power_mw: 20 };
  s.units.SF1 = { power_mw: 10 };

  const f = Model.regionFlows(s, "NSW1");
  const gen = Object.fromEntries(f.gen.map((seg) => [seg.key, seg.value]));
  const load = Object.fromEntries(f.load.map((seg) => [seg.key, seg.value]));

  assert.deepEqual(gen, {
    coal: 400, gas: 300.2,               // fossil bands, from the one rollup
    hydro: 50, wind: 20, solar: 10,      // fueltech-picked bucket sums
    rooftop: 5, imports: 12,             // region record
    battery: 100,                        // discharging units are generation
    wind_curt: 3, solar_curt: 2,         // curtailment, per fuel
  });
  assert.deepEqual(load, {
    battery: 40,                         // charging units are load
    pumps: 30,                           // pump DUIDs, AEMO signs them negative
    exports: 7,
  });
  assert.equal(f.totalGen, 400 + 300.2 + 50 + 20 + 10 + 5 + 12 + 100 + 3 + 2);
  assert.equal(f.totalLoad, 40 + 30 + 7);
  assert.equal(f.max, f.totalGen);
});

test("region flows: a category nothing reported for is absent, not zero", () => {
  const s = emptyState();
  s.regions = { NSW1: { rooftop_mw: -1 } }; // negative rooftop is not generation
  const f = Model.regionFlows(s, "NSW1");
  assert.deepEqual(f.gen, []);
  assert.deepEqual(f.load, []);
  assert.equal(f.max, 0);
});

/* ---------- the status gate ---------- */

test("status gate: a unit's spelling against its own toggle, case-insensitively", () => {
  const on = { operating: true, operational: true, commissioning: true, committed: true };
  assert.equal(Model.unitStatusPasses("operating", on), true);
  assert.equal(Model.unitStatusPasses("OPERATING", on), true);
  assert.equal(Model.unitStatusPasses("operating", { ...on, operating: false }), false);
  assert.equal(Model.unitStatusPasses("operational", { ...on, operational: false }), false);
  assert.equal(Model.unitStatusPasses("commissioning", { ...on, commissioning: false }), false);
  assert.equal(Model.unitStatusPasses("committed", { ...on, committed: false }), false);
  // operating and operational are two toggles, not one bucket: the renewable
  // draw path's inline gate used to show an "operating" unit whenever either
  // was checked; the unified gate gives each spelling its own toggle.
  assert.equal(Model.unitStatusPasses("operating", { ...on, operating: false, operational: true }), false);
  // A spelling the toggles do not know is not their business.
  assert.equal(Model.unitStatusPasses("trial", { ...on, operating: false }), true);
  assert.equal(Model.unitStatusPasses("", { ...on, committed: false }), true);
  assert.equal(Model.unitStatusPasses(undefined, { ...on, committed: false }), true);
});

test("status gate: only a known selected spelling counts a unit as selected", () => {
  const on = { operating: true, operational: true, commissioning: true, committed: true };
  assert.equal(Model.unitStatusSelected("operating", on), true);
  assert.equal(Model.unitStatusSelected("trial", on), false);
  assert.equal(Model.unitStatusSelected(undefined, on), false);
});

test("facility visibility: idle filter first, then statuses, then facility flags", () => {
  const s = emptyState();
  s.batteries = { BAT1: { aemo_power_mw: 100 } };
  s.units = { HYD1: { power_mw: 50 }, R1: { power_mw: 40 } };
  const bat = fac("battery", "NSW1", [unit("BAT1")]);

  assert.equal(Model.facilityStatusVisible(bat, s, ALL_ON), true);
  // Status off hides it even though it is pumping.
  assert.equal(Model.facilityStatusVisible(bat, s, { ...ALL_ON, showStatus: { ...ALL_ON.showStatus, operating: false } }), false);
  // Idle on: an idle facility still shows.
  s.batteries.BAT1.aemo_power_mw = 0;
  assert.equal(Model.facilityStatusVisible(bat, s, ALL_ON), true);
  // Idle off: the idle filter runs before the status gate and hides it.
  assert.equal(Model.facilityStatusVisible(bat, s, { ...ALL_ON, showIdle: false }), false);
  // No units at all: never visible.
  assert.equal(Model.facilityStatusVisible(fac("battery", "NSW1", []), s, ALL_ON), false);
  assert.equal(Model.facilityStatusVisible(null, s, ALL_ON), false);
});

test("facility visibility: all-committed and any-commissioning facilities answer to their toggles", () => {
  const s = emptyState();
  const committed = fac("battery", "NSW1", [unit("BAT1", { status: "committed" })]);
  const commissioning = fac("gas", "NSW1", [
    unit("GAS1", { status: "operating", fueltech_id: "gas_ccgt" }),
    unit("GAS2", { status: "commissioning", fueltech_id: "gas_ccgt" }),
  ]);
  const offCommitted = { ...ALL_ON, showStatus: { ...ALL_ON.showStatus, committed: false } };
  const offCommissioning = { ...ALL_ON, showStatus: { ...ALL_ON.showStatus, commissioning: false } };

  assert.equal(Model.facilityStatusVisible(committed, s, ALL_ON), true);
  assert.equal(Model.facilityStatusVisible(committed, s, offCommitted), false);
  assert.equal(Model.facilityStatusVisible(commissioning, s, ALL_ON), true);
  assert.equal(Model.facilityStatusVisible(commissioning, s, offCommissioning), true); // still has an operating unit
  const allCommissioning = fac("gas", "NSW1", [unit("GAS2", { status: "commissioning", fueltech_id: "gas_ccgt" })]);
  assert.equal(Model.facilityStatusVisible(allCommissioning, s, offCommissioning), false);
});

test("facility visibility: a facility of unknown status spellings hides without a facility flag", () => {
  const s = emptyState();
  const trial = fac("battery", "NSW1", [unit("BAT1", { status: "trial" })]);
  assert.equal(Model.facilityStatusVisible(trial, s, ALL_ON), false);
});

/* ---------- idle semantics: one threshold ---------- */

test("the idle threshold is one number every idle question shares", () => {
  assert.equal(Model.IDLE_MW, 0.5);
});

test("battery idle: the freshest unit power, summed, against the idle bar", () => {
  const s = emptyState();
  const bat = fac("battery", "NSW1", [unit("BAT1"), unit("BAT2")]);
  s.batteries = { BAT1: { aemo_power_mw: 0.4 }, BAT2: { aemo_power_mw: -0.4 } };
  assert.equal(Model.isIdleFacility(bat, s), true);   // |0| < 0.5: charge and discharge net
  s.batteries = { BAT1: { aemo_power_mw: 1.0 }, BAT2: { aemo_power_mw: -0.2 } };
  assert.equal(Model.isIdleFacility(bat, s), false);  // |0.8| >= 0.5
  s.batteries = { BAT1: { aemo_power_mw: 0.4 }, BAT2: { aemo_power_mw: 0.2 } };
  assert.equal(Model.isIdleFacility(bat, s), false);  // 0.6 >= 0.5
  s.batteries = { BAT1: { aemo_power_mw: 0.4 }, BAT2: { aemo_power_mw: -0.6 } };
  assert.equal(Model.isIdleFacility(bat, s), true);   // |−0.2| < 0.5
});

test("battery idle: no live power at all is idle", () => {
  const s = emptyState();
  const bat = fac("battery", "NSW1", [unit("BAT1")]);
  s.batteries = {}; // no readings
  assert.equal(Model.isIdleFacility(bat, s), true);
});

test("renewable idle: unmeasured facilities are idle, measured ones answer to the bar", () => {
  const s = emptyState();
  const ren = fac("renewable", "NSW1", [unit("SF1", { fueltech_id: "solar" })]);
  assert.equal(Model.isIdleFacility(ren, s), true);          // nothing measured
  s.units = { SF1: { power_mw: 0.4 } };
  assert.equal(Model.isIdleFacility(ren, s), true);          // below the bar
  s.units.SF1.power_mw = 0.5;
  assert.equal(Model.isIdleFacility(ren, s), false);         // at the bar
  s.units.SF1.power_mw = -3;                                 // negatives clamp to 0, so still idle
  assert.equal(Model.isIdleFacility(ren, s), true);
});

test("thermal idle: unmeasured facilities are never idle-hidden", () => {
  const s = emptyState();
  const coal = fac("coal", "NSW1", [unit("COAL1", { fueltech_id: "coal_black" })]);
  assert.equal(Model.isIdleFacility(coal, s), false);        // no live data: keep
  s.units = { COAL1: { power_mw: 0.4 } };
  assert.equal(Model.isIdleFacility(coal, s), true);
  s.units.COAL1.power_mw = 0.5;
  assert.equal(Model.isIdleFacility(coal, s), false);
});

test("renewable draw path: per-unit idle, and unmeasured units are kept", () => {
  const s = emptyState();
  const u = unit("WF1", { fueltech_id: "wind" });
  assert.equal(Model.renewableUnitIdle(u, s), false);        // unmeasured: kept
  s.units = { WF1: { power_mw: 0.4 } };
  assert.equal(Model.renewableUnitIdle(u, s), true);
  s.units.WF1.power_mw = -0.6;
  assert.equal(Model.renewableUnitIdle(u, s), false);        // |−0.6| >= 0.5
});

/* ---------- state of charge ---------- */

// ERB01's real registered storage: 1997 MWh, the figure the SoC denominator
// is pinned against on the server side too (SoCPolicyTest).
test("unit SoC: inferred stored MWh over registered storage capacity", () => {
  const s = emptyState();
  s.batteries = { ERB01: { inf_stored_mwh: 998.5, inf_age_s: 120 } };
  const soc = Model.unitSoc("ERB01", 1997, s);
  assert.equal(soc.soc, 0.5);
  assert.equal(soc.src, "inferred");
  assert.equal(soc.age, 120);
  assert.equal(Model.unitSoc("ERB01", 0, s), null);          // no denominator
  assert.equal(Model.unitSoc("ERB01", -5, s), null);
  assert.equal(Model.unitSoc("NOPE", 1997, s), null);
  s.batteries.ERB01 = { inf_age_s: 120 };                     // no stored reading
  assert.equal(Model.unitSoc("ERB01", 1997, s), null);
  s.batteries.ERB01 = { inf_stored_mwh: 100 };                // no age: Infinity
  assert.equal(Model.unitSoc("ERB01", 1997, s).age, Infinity);
});

test("facility SoC: the capacity-weighted mean across the units that have one", () => {
  const s = emptyState();
  const bat = fac("battery", "NSW1", [
    unit("B1", { capacity_mwh: 100 }),
    unit("B2", { capacity_mwh: 300 }),
  ]);
  s.batteries = { B1: { inf_stored_mwh: 20 }, B2: { inf_stored_mwh: 180 } };
  // (0.2*100 + 0.6*300) / 400 = 0.5
  assert.equal(Model.facilitySoc(bat, s).soc, 0.5);
  s.batteries = {}; // no readings at all
  assert.equal(Model.facilitySoc(bat, s), null);
});

test("commissioning facility: energised fraction and fill against the facility's own peak", () => {
  const s = emptyState();
  const bat = fac("battery", "NSW1", [unit("B1", { status: "commissioning", capacity_mwh: 1000 })]);
  s.batteries = { B1: { peak_stored_mwh: 250, inf_stored_mwh: 125 } };
  const c = Model.facilityCommission(bat, s);
  assert.equal(c.frac, 0.25);
  assert.equal(c.fill, 0.5);
  // A peak beyond registered capacity is a full charge, not over-unity.
  s.batteries.B1.peak_stored_mwh = 2000;
  assert.equal(Model.facilityCommission(bat, s).frac, 1);
  // No peak series: no arc.
  s.batteries = { B1: { inf_stored_mwh: 125 } };
  assert.equal(Model.facilityCommission(bat, s), null);
});

test("facility total: registered storage capacity in MWh", () => {
  const bat = fac("battery", "NSW1", [unit("B1", { capacity_mwh: 500 }), unit("B2", { capacity_mwh: 300 })]);
  assert.equal(Model.facilityTotal(bat), 800);
});

/* ---------- facility aggregates ---------- */

test("hydro aggregate: generation and pumping summed separately, pulse at 5% with a 1 MW floor", () => {
  const s = emptyState();
  const hec = fac("hydro", "NSW1", [
    unit("HEC1", { fueltech_id: "hydro", capacity_mw: 1500 }),
    unit("HEC2", { fueltech_id: "pumps", capacity_mw: 500 }),
  ], { pumped: true });
  s.units = { HEC1: { power_mw: 800, age_s: 90 }, HEC2: { power_mw: 300, age_s: 60 } };
  let agg = Model.hydroAgg(hec, s);
  assert.equal(agg.gen, 800);
  assert.equal(agg.pump, 300);              // positive pump output counts as pumping
  assert.equal(agg.activity, 1100);
  assert.equal(agg.cap, 2000);
  assert.equal(agg.frac, 1100 / 2000);
  assert.equal(agg.measured, true);
  assert.equal(agg.pulse, true);            // 1100 >= max(1, 0.05*2000)
  assert.equal(agg.age, 60);                // freshest unit
  // A pump unit draining (negative SCADA) contributes nothing to the pump sum.
  s.units.HEC2.power_mw = -300;
  assert.equal(Model.hydroAgg(hec, s).pump, 0);
  // Below the pulse bar: trickling is not running.
  s.units = { HEC1: { power_mw: 4, age_s: 90 } };
  agg = Model.hydroAgg(hec, s);
  assert.equal(agg.pulse, false);           // 4 < max(1, 0.05*2000)=100
  // The 1 MW floor: 5% of a 10 MW scheme is 0.5 MW, but the floor is 1 MW.
  const mini = fac("hydro", "NSW1", [unit("M1", { fueltech_id: "hydro", capacity_mw: 10 })]);
  s.units = { M1: { power_mw: 1 } };
  assert.equal(Model.hydroAgg(mini, s).pulse, true);
  s.units.M1.power_mw = 0.9;
  assert.equal(Model.hydroAgg(mini, s).pulse, false);
});

test("thermal aggregate: generation as a share of capacity, fuels tallied whether measured or not", () => {
  const s = emptyState();
  const station = fac("coal", "NSW1", [
    unit("BAY1", { fueltech_id: "coal_black", capacity_mw: 600 }),
    unit("BAY2", { fueltech_id: "coal_black", capacity_mw: 600 }),
    unit("BAY3", { fueltech_id: "coal_brown", capacity_mw: 600 }),
  ]);
  s.units = { BAY1: { power_mw: 500, age_s: 80 }, BAY2: { power_mw: -10, age_s: 70 } };
  const agg = Model.thermalAgg(station, s);
  assert.equal(agg.cap, 1800);
  assert.equal(agg.gen, 500);               // negatives clamp to zero
  assert.equal(agg.frac, 500 / 1800);
  assert.equal(agg.measured, true);
  assert.equal(agg.age, 70);
  assert.deepEqual(agg.fuels, { coal_black: 2, coal_brown: 1 });
});

/* ---------- staleness: one table ---------- */

test("staleness: the header's fresh/stale/down cutoffs", () => {
  assert.equal(Model.headerFreshness(0), "fresh");
  assert.equal(Model.headerFreshness(120), "fresh");          // at the bar
  assert.equal(Model.headerFreshness(121), "stale");
  assert.equal(Model.headerFreshness(600), "stale");
  assert.equal(Model.headerFreshness(601), "down");
  assert.deepEqual(Model.STALENESS, {
    interconnector_stale_s: 900,
    header_fresh_s: 120,
    header_stale_s: 600,
  });
});

test("staleness: the interconnector tooltip's stale flag", () => {
  assert.equal(Model.isStaleAge(900), false);
  assert.equal(Model.isStaleAge(901), true);
  assert.equal(Model.isStaleAge(undefined), false);
  assert.equal(Model.isStaleAge(null), false);
});

/* ---------- the payload pin's declaration ---------- */

test("PAYLOAD_FIELDS is the model's declared reads, and fossil_mw is deliberately not among them", () => {
  assert.ok(Array.isArray(Model.PAYLOAD_FIELDS));
  assert.ok(Model.PAYLOAD_FIELDS.every((p) => typeof p === "string" && p.includes(".")));
  assert.ok(Model.PAYLOAD_FIELDS.includes("regions.*.stored_mwh"));
  assert.ok(Model.PAYLOAD_FIELDS.includes("batteries.*.aemo_power_mw"));
  assert.ok(!Model.PAYLOAD_FIELDS.some((p) => p.includes("fossil_mw")));
});
