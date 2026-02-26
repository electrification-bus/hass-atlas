# ED Power Sankey — Untracked Consumption Analysis

**Date:** 2026-02-26
**Context:** HA Energy Dashboard "Current power flow" Sankey shows a significant
"Untracked consumption" bar (~20% of total site consumption).

## Live Readings (approx 3:15 PM Pacific)

### Sources (into Home)

| Source  | Power   |
|---------|---------|
| Grid    | 1128 W  |
| Solar   | 1918 W  |
| Battery | 0 W     |
| **Total** | **~3046 W** |

### Panel Upstream Lug Power

| Panel         | Upstream Lug | Downstream Lug |
|---------------|-------------|----------------|
| c1akc (main)  | 1123 W      | 1816 W         |
| c1c46 (sub)   | 2463 W      | —              |
| c192x (sub)   | 1861 W      | —              |

### Tracked Circuit Consumption (leaf circuits, negative = consuming)

| Panel  | Circuit Sum | Dominant Loads |
|--------|------------|----------------|
| c1akc  | -108 W     | HVAC-Air-Handler -62W, Studio-HVAC -10W (PV generates +1905W) |
| c1c46  | -639 W     | Studio -414W, Ground-Floor-Outlets -90W, Ground-Floor-Lights -59W |
| c192x  | -1667 W    | Server-Rack-2 -580W, Server-Rack-1 -526W, AV-Rack-2 -145W |
| **Total** | **~2414 W** | |

### Gap

**~632 W untracked** (~20% of site consumption)

## Likely Sources of Untracked Power

1. **Tesla Powerwall parasitic draw** (~50-100W) — positioned UPSTREAM of the
   main panel, not on any SPAN circuit CT.  Monitored by the Tesla/Powerwall
   integration but not included in SPAN's device_consumption.  This power is
   real consumption but invisible to SPAN's circuit-level metering.

2. **CT measurement error accumulation** — SPAN circuit CTs have reduced accuracy
   at low power levels.  With ~40+ circuits, many reading near-zero with noise
   of +/-5W, the aggregate error can be 100-200W.

3. **Bus bar and wiring losses** — power dissipated between the upstream lug CTs
   and the individual circuit CTs.  Typically small but nonzero.

4. **Timing skew** — readings are not perfectly synchronized across 3 panels.
   The power Sankey displays instantaneous values that may represent different
   moments, amplifying apparent discrepancies.

5. **PV inverter consumption** — Enphase microinverters consume a small amount
   of power (~0.5W each) even when generating.  With multiple inverters this
   can add up to a few watts but is negligible.

## Notes

- The Powerwall integration tracks battery power (charge/discharge) as an
  energy source in ED, but its parasitic/standby draw is not separately
  reported as a device_consumption entity.  This is a permanent gap.
- The 20% figure is high but consistent with the combination of factors above,
  especially given the relatively low total consumption (~3 kW) where
  measurement errors are proportionally larger.
- At higher consumption levels (e.g., HVAC running, water heater on), the
  untracked percentage would likely drop to 5-10% as the fixed-error components
  become a smaller fraction of the total.
