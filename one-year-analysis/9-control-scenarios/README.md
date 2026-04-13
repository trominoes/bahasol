# Module 9 — Control Scenarios

## Purpose

Answers two hardware-sizing questions that depend on the **control strategy**
used to operate the irrigation pump:

1. **Battery sizing** — for a given control mode, how many unfulfilled
   irrigation days result at each battery nameplate capacity?  Where is the
   practical minimum battery size?

2. **Start-time sensitivity** — how much does reliability change as the farmer
   moves the pump start hour earlier or later in the morning?

Both analyses run over all seven historical seasons (2018–2024) and two
representative crops (cassava and tomato).  They call the `simulate_season()`
function directly from the Module 5 simulation library
(`5-integrated-analysis/integrated_analysis.py`) and read pre-computed CSVs
from Modules 1 and 4 — no re-running of the full pipeline is required.

---

## Control Modes

### Solar-following (default in Module 6)

The pump skips hours where solar + battery energy is insufficient and
**resumes** whenever conditions improve.  Maximises reliability; models an
automated controller or an attentive operator.

### Continuous-run — Tier 1 (default in this module)

Once the pump starts for the day, it runs until energy fails.  If conditions
fail in a subsequent hour, the pump **does not restart** for the rest of the
day — even if afternoon solar recovers.  Models a simple manual on/off switch
operated by a farmer who starts the pump and leaves.

Use `--solar-following` on either script to run the baseline solar-following
mode for comparison.

---

## Scripts

### `battery_capacity_sweep.py`

Sweeps battery nameplate capacity from 0 kWh to 10 kWh (configurable) and
reports unfulfilled irrigation days per season for each (crop, year, capacity)
combination.

```bash
# Default: continuous-run mode, both crops, 2018–2024, with degradation
python battery_capacity_sweep.py

# Custom sweep range
python battery_capacity_sweep.py --max-kwh 15 --step 0.5

# Solar-following mode (monotone comparison baseline)
python battery_capacity_sweep.py --solar-following

# Without battery degradation factor
python battery_capacity_sweep.py --no-degradation
```

The unfulfilled-days curve is monotonically non-increasing: more battery
capacity means at least as much energy is available at every point during
the day, so unfulfilled days can only decrease (or stay flat) as battery
size grows.

---

### `start_time_sensitivity.py`

Sweeps pump start hour from 5 AM to 14:00 and reports irrigation fulfillment
rate for each hour.  Shows the trade-off between starting early (capturing
more morning sun) and starting late (skipping low-irradiance hours that might
trigger a continuous-run halt).

```bash
# Default: continuous-run mode, both crops, 2018–2024
python start_time_sensitivity.py

# Solar-following baseline
python start_time_sensitivity.py --solar-following

# Specific year
python start_time_sensitivity.py --year 2021
```

---

## Inputs

These scripts read from pre-computed outputs of earlier modules.  No API calls
or re-computation of solar or ET data is needed.

```
../1-solar-power/gen-power/
  *_<year>_power.csv       Reference 15-panel hourly DC power (from Module 1).

../4-irrigation/results/
  daily_<crop>_<year>.csv  SWD-based daily irrigation targets (from Module 4).
```

---

## Outputs

```
results/battery-sweep/
  battery_sweep_results_cr.csv    Continuous-run sweep: one row per
                                  (crop, year, nameplate_kwh)
  battery_sweep_results_sf.csv    Solar-following sweep (if run with
                                  --solar-following)

results/start-time/
  start_time_results_cr.csv       Continuous-run start-time sweep
  start_time_results_sf.csv       Solar-following start-time sweep

images/battery-sweep/
  battery_sweep_cr.png            Two-panel figure (cassava + tomato):
                                  unfulfilled days vs. nameplate capacity.
                                  Per-year faint lines + bold 7-year mean.
  battery_sweep_sf.png            Same for solar-following mode.

images/start-time/
  start_time_cr.png               Fulfillment rate vs. pump start hour.
  start_time_sf.png               Same for solar-following mode.
```

### CSV columns — battery sweep

| Column | Description |
|---|---|
| `crop` | Crop name |
| `year` | Planting year (season starts Sep 1 of this year) |
| `control_mode` | `'continuous-run'` or `'solar-following'` |
| `battery_nameplate_kwh` | Nameplate battery capacity tested [kWh] |
| `battery_eff_kwh` | Effective capacity after degradation factor |
| `irrigation_days` | Total scheduled irrigation days in the season |
| `unfulfilled_days` | Days where pump_hrs < target_hrs − 0.5 hr |
| `fulfilled_days` | `irrigation_days − unfulfilled_days` |
| `fulfillment_pct` | `fulfilled_days / irrigation_days × 100` |

### CSV columns — start-time sweep

| Column | Description |
|---|---|
| `crop` | Crop name |
| `year` | Planting year |
| `control_mode` | `'continuous-run'` or `'solar-following'` |
| `pump_start_hour` | Hour tested (0–23 local time) |
| `irrigation_days` | Total scheduled irrigation days |
| `unfulfilled_days` | Days not fully met |
| `fulfillment_pct` | Fulfillment rate [%] |

---

## Relationship to Other Modules

| Module | Role |
|---|---|
| Module 5 | Simulation library (`simulate_season()`, `load_solar_power()`, `load_daily_targets()`) |
| Module 1 | Provides pre-computed hourly solar power CSVs |
| Module 4 | Provides pre-computed daily irrigation target CSVs |
| Module 6 | `run_simulation.py --continuous-run` lets you reproduce individual seasons to cross-check these sweep results |

To manually verify a sweep data point, run:

```bash
cd ../6-run-sims
python run_simulation.py --stages 4 --continuous-run --battery-kwh 2 --year 2021 --crop cassava
```

This produces a `daily_energy_cassava_2021_p15_b2kWh_cr.csv` with hour-by-hour
detail that you can compare against the corresponding row in
`battery_sweep_results_cr.csv`.
