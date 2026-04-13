# Module 5 — Integrated Energy Analysis (Simulation Library)

## Purpose

This module serves a dual role:

1. **Simulation library** — `integrated_analysis.py` is imported by Module 6
   (`run_simulation.py`) and Module 9 (`battery_capacity_sweep.py`,
   `start_time_sensitivity.py`).  It contains the authoritative
   `simulate_season()` kernel and all helper functions.  When you run a sweep
   or a control-scenario analysis you are always running code from this file.

2. **Standalone script** — can be run directly for a quick single-year,
   single-configuration analysis with diagnostic plots.

The simulation determines whether the solar + battery system can reliably
power the irrigation pump **on the specific days and for the specific durations**
demanded by the Module 4 irrigation schedule.

Unlike Module 3 (which greedily accumulates all available pump hours), this
simulation enforces the weekly schedule: the pump only runs on designated
irrigation days and must accumulate exactly the target hours each day.  Days
where the system falls short are flagged as failures and counted against the
reliability metric.

**Reliability** is defined as the percentage of scheduled irrigation days on
which the pump delivers its full target run time.

### Control modes

The simulation supports two control strategies, selectable via `--continuous-run`:

| Mode | Flag | Behaviour |
|---|---|---|
| Solar-following (default) | *(none)* | Pump skips cloudy hours and resumes whenever solar + battery conditions improve.  Maximum flexibility; higher reliability. |
| Continuous-run (Tier 1) | `--continuous-run` | Once the pump starts for the day, it runs until energy fails.  It does **not** restart if conditions improve later.  Models a manual on/off switch. |

The continuous-run mode is the more conservative sizing assumption and is used
by default in the Module 9 control-scenario sweeps.

---

## Script

### `integrated_analysis.py`

```bash
python integrated_analysis.py                              # cassava 2018, 15 panels, 2 kWh
python integrated_analysis.py --year 2022
python integrated_analysis.py --crop tomato --year 2021
python integrated_analysis.py --panels 20 --battery-kwh 4
python integrated_analysis.py --year 2020 --panels 10 --battery-kwh 1
python integrated_analysis.py --continuous-run             # Tier 1: pump halts if energy fails mid-day
python integrated_analysis.py --year 2021 --battery-kwh 3 --continuous-run
```

Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `N_PANELS` | 15 | Solar panels (scales reference 15-panel CSVs) |
| `BATTERY_CAPACITY_KWH` | 2.0 kWh | Nameplate battery capacity |
| `BATTERY_MIN_SOC_PCT` | 10 % | Minimum allowed SoC |
| `PUMP_POWER_KW` | 1.263 kW | Constant AC draw when running |
| `MAX_DAILY_HRS` | 8.0 hr | Hard cap on pump hours per irrigation day |
| `INVERTER_EFF` | 96 % | DC → AC conversion efficiency |
| `BATTERY_CHARGE_EFF` | 95 % | Charge round-trip efficiency |
| `BATTERY_DISCHARGE_EFF` | 95 % | Discharge round-trip efficiency |
| `CONTINUOUS_RUN` | `False` | Enable Tier 1 continuous-run mode (see above) |

### Panel Count Scaling

The reference solar power CSVs in `1-solar-power/gen-power/` are always
generated for **15 panels**.  When you request a different panel count with
`--panels N`, the simulation scales hourly solar power by:

```python
panel_scale = n_panels / 15       # e.g. 20 panels → scale = 1.333
```

The denominator is always 15 (the reference count), never the module-level
`N_PANELS` constant.  This is important: Stage 1 does **not** need to be
re-run when sweeping panel counts.

---

## Inputs

```
../1-solar-power/gen-power/
  *_<year>_power.csv   Hourly DC power for the 15-panel reference array.
  *_<year+1>_power.csv Also needed (season crosses into the next calendar year).

../4-irrigation/results/
  weekly_<crop>_<year>.csv   Weekly schedule (irrigation days, target hours).
```

---

## Outputs

```
results/<crop>_<year>_p<N>_b<B>kWh/
  daily_energy_<tag>.csv    One row per day in the season:
    date              YYYY-MM-DD
    is_irr_day        1 if this is a scheduled irrigation day
    target_hrs        Pump hours required (from irrigation schedule)
    pump_hrs          Pump hours actually delivered
    met               1 if pump_hrs ≥ target_hrs, else 0
    solar_kwh         Solar energy generated this day [kWh]
    pump_kwh          Energy consumed by the pump this day [kWh]
    batt_start_kwh    Battery SoC at start of day [kWh]
    batt_end_kwh      Battery SoC at end of day [kWh]

  weekly_energy_<tag>.csv   Aggregated by week:
    week_start        Monday date
    irr_days          Number of irrigation days in the week
    days_met          Number of irrigation days fully served
    reliability_pct   days_met / irr_days × 100
    solar_kwh         Total solar generation for the week
    pump_kwh          Total pump energy consumed

images/
  P1_pump_hours.png
  P2_battery_soc.png
  P3_weekly_reliability.png
  P4_hourly_power_flow.png
```

The CSV folder name encodes the full configuration:
`cassava_2021_p15_b2kWh` → crop=cassava, season starting 2021, 15 panels, 2 kWh battery.

---

## Plots

**P1 — Daily Pump Hours**
Bar chart of daily pump hours throughout the growing season.  Irrigation days
are shown in blue (target met) or red (shortfall).  Non-irrigation days appear
in grey (battery charging days).  The dashed line shows the daily target.

**P2 — Battery State of Charge**
Line chart of battery SoC (%) over the full season.  Pump run events are
overlaid as tick marks.  Drops to the minimum SoC floor (10 %) indicate days
where energy was insufficient.  Recovery between irrigation events shows
whether the battery has time to recharge between scheduled days.

**P3 — Weekly Reliability**
Heatmap or bar chart of weekly reliability (% of irrigation days met per week).
Weeks coloured red had at least one partially unmet irrigation event.  Useful
for identifying seasonal patterns in failures (e.g., cloudy winter weeks).

**P4 — Hourly Power Flow**
Detailed view of a representative week showing hour-by-hour solar power
(yellow), pump load (blue), and battery charge/discharge (green/red).  Reveals
the interplay between solar availability and pump scheduling within a day.

---

## Reliability Interpretation

A result of **95 % reliability** means that in 95 % of all scheduled
irrigation days over the analysed season(s), the pump ran for its full target
duration.  The remaining 5 % of days received less water than the crop
required — most often because of multi-day cloudy spells that deplete the
battery before the next solar recharge.

Typical results for cassava at this site:

| Configuration | Approx. reliability |
|---|---|
| 10 panels, 1 kWh | ~75–85 % |
| 15 panels, 2 kWh | ~90–95 % |
| 20 panels, 4 kWh | ~97–99 % |

Use Module 6 (`--sweep-panels`, `--sweep-battery`) to generate a full
reliability matrix across configurations, and Module 7 to find the
minimum-cost configuration meeting your target.

For control-strategy sweeps (battery sizing under continuous-run, pump
start-time sensitivity), see Module 9 (`9-control-scenarios/`).
