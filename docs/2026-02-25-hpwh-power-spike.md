# HPWH Resistance Heating Power Spike — 2026-02-25

## Summary

A ~5 kW power spike occurred from approximately 17:30–18:25 Pacific,
raising whole-home consumption from ~3 kW to ~8 kW for about 50 minutes.
The cause was the EcoNet Heat Pump Water Heater (HPWH) switching from
heat-pump (compressor) mode to electric resistance heating.

## Timeline

All times US/Pacific. Data from 5-minute HA statistics (`sensor.econet_hpwh_power_2`).

| Time  | HPWH Power | Mode | Notes |
|-------|-----------|------|-------|
| 17:20 | 0 W | Off | |
| 17:25 | ~305 W | Heat pump | Compressor started |
| 17:30 | ~311 W | Heat pump | |
| 17:35 | ~5,306 W | Resistance | Switched to backup elements mid-interval |
| 17:40 | ~5,310 W | Resistance | Sustained draw |
| 17:45–18:20 | 5,316–5,413 W | Resistance | Gradual rise as elements heat up |
| 18:25 | ~712 W (avg) | Shutting down | Dropped to 0 within interval |
| 18:30 | 0 W | Off | |

## Whole-Home Impact

From `sensor.c1akc_site_metering_site` (lead SPAN panel):

- **Before spike (17:20):** ~2,980 W mean
- **During spike (17:40):** ~8,207 W mean
- **After spike (18:30):** ~2,690 W mean
- **Delta:** +5,227 W — matches the HPWH resistance draw

Solar production was negligible after 17:30 (sunset). Battery was idle (~0 W).
The entire spike was supplied by grid import.

## Explanation

Heat pump water heaters have two heating modes:

- **Heat-pump (compressor):** ~300 W, efficient (COP ~3), slower recovery
- **Resistance elements (backup):** ~5.3 kW, inefficient (COP ~1), fast recovery

The unit started in heat-pump mode at 17:25, then escalated to resistance
heating at 17:35. This happens when:

1. Water temperature is too far below setpoint for the heat pump alone
2. The unit is set to "High Demand" or "Electric" mode instead of "Heat Pump Only"
3. Ambient air temperature around the unit is too low for efficient heat-pump operation

## Data Sources

- `sensor.econet_hpwh_power_2` — HPWH power draw (from EcoNet integration)
- `sensor.c1akc_site_metering_site` — whole-home power (SPAN panel nt-2143 site metering)
- `sensor.c1akc_site_metering_grid` — grid import power (SPAN panel nt-2143)
- `sensor.c1akc_site_metering_pv` — solar production (SPAN panel nt-2143)
- `sensor.c1akc_site_metering_battery` — battery power (SPAN panel nt-2143)
