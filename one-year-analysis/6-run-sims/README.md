# Module 6 — Master Simulation Orchestrator

## Purpose

Provides a single entry point for running any combination of the four analysis
stages with a unified CONFIG block.  Handles parameter sweeps across panel
counts and battery sizes to produce a reliability matrix, which feeds into
Module 7 for cost optimization.

This is the primary script to use when comparing system configurations or
running multi-year analyses.

---

## Script

### `run_simulation.py`

```bash
# Run all four stages with default config
python run_simulation.py

# Run only irrigation + integrated (most common for config sweeps)
python run_simulation.py --stages 3 4

# Override individual parameters
python run_simulation.py --panels 20 --battery-kwh 4 --crop tomato --year 2021

# Multi-year run
python run_simulation.py --years 2018 2019 2020 2021 2022 2023 2024

# Parameter sweep — generates reliability matrix CSV
python run_simulation.py --sweep-panels 10 12 15 18 20 --sweep-battery 1 2 3 4 5
```

---

## CONFIG Block

Edit the CONFIG dictionary at the top of the script to set defaults without
using flags.  All flags override CONFIG at runtime.

| CONFIG key | Default | Description |
|---|---|---|
| `years` | 2018–2024 | Years to analyse |
| `crop` | `cassava` | Crop type (see Module 4 for list) |
| `n_panels` | 15 | Number of solar panels |
| `panel_watt` | 405 W | Panel rated power |
| `battery_kwh` | 2.0 kWh | Battery nameplate capacity |
| `pump_power_kw` | 1.263 kW | Pump AC draw |
| `pump_flow_gpm` | 14.39 GPM | Flow rate |
| `farm_acres` | 0.78 | Farm area |
| `latitude_deg` | 24.96 °N | Site latitude |
| `tilt_deg` | 24 ° | Panel tilt |
| `performance_ratio` | 0.85 | Solar PR |
| `eff_rain_factor` | 0.80 | Effective rainfall fraction |

---

## Pipeline Stages

| Stage | Module | What it does |
|---|---|---|
| 1 | `1-solar-power/solar_analysis.py` | Compute hourly DC power from NSRDB |
| 2 | `3-operating-hours-available/battery_pump_analysis.py` | Greedy pump simulation |
| 3 | `4-irrigation/irrigation_schedule.py` | FAO-56 ET₀ and weekly schedule |
| 4 | `5-integrated-analysis/integrated_analysis.py` | Schedule-constrained reliability |

### When to run partial stages

**`--stages 3 4`** — *most common partial run.*
Use when testing different panel counts or battery sizes.  Stage 4 scales the
existing 15-panel solar CSVs internally — Stage 1 does not need to re-run.
Also use when climate data already exists in `4-irrigation/et-data/`.

**`--stages 4`** — integrated analysis only.
Use when irrigation schedules already exist in `4-irrigation/results/` and
you only want to re-run the energy simulation (e.g. after changing battery
capacity or pump power).

**`--stages 1 2`** — solar + greedy pump simulation.
Use to estimate total annual pump-hours without an irrigation schedule, as a
quick feasibility check before committing to crop-specific analysis.

**All stages (default)** — end-to-end run.
Use for a fresh analysis of a new site or crop, or after updating NSRDB files.

### Data directories by stage

| Stage | Reads from | Notes |
|---|---|---|
| 1 | `1-solar-power/NSRDB-raw/` | One raw CSV per year; never auto-downloaded |
| 2 | `1-solar-power/gen-power/` or Stage 1 output | Falls back to pre-generated reference CSVs |
| 3 | `4-irrigation/et-data/` | Must run `fetch_et_data.py` separately first |
| 4 | `1-solar-power/gen-power/`, `4-irrigation/results/` | Scales 15-panel CSVs for any panel count |

---

## Outputs

All outputs go to `output/<tag>/` inside this folder.  The tag encodes the
configuration, e.g. `p15_b2kWh_cassava_yr2018-2024`.

```
output/<tag>/
  gen-power/              Stage 1: hourly power CSVs (if Stage 1 ran)
  battery-pump/           Stage 2: greedy simulation CSVs
  images-stage2/          Stage 2: diagnostic plots
  irrigation/             Stage 3: weekly + daily schedule CSVs
  integrated/             Stage 4: daily + weekly energy CSVs
  images-stage4/          Stage 4: reliability plots

output/sweep/
  reliability_matrix.csv  Sweep results: rows = panels, cols = battery kWh
```

### Reliability Matrix

The sweep output is a CSV table where rows are panel counts and columns are
battery capacities.  Each cell contains the average reliability (% of
irrigation days met) across all analysed years.  Pass this file to Module 7
(`7-cost-analysis/cost_analysis.py`) for cost optimization.

Example output:
```
n_panels, batt_1kWh, batt_2kWh, batt_3kWh
10,        72.3,      81.4,      86.2
15,        88.7,      93.9,      96.4
20,        94.1,      97.8,      99.1
```

---

## Module Loading

`run_simulation.py` uses `importlib.util` to dynamically load sibling scripts
as Python modules without requiring them to be installed as packages.
Module-level constants (e.g. `N_PANELS`, `BATTERY_CAPACITY_KWH`) are
monkey-patched after loading, so all logic runs with the CONFIG values
regardless of the defaults inside each script.

This means you can also run each sibling script independently with its own
defaults, and `run_simulation.py` will not interfere.
