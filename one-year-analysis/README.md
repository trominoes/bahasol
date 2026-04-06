# BahaSol — Solar-Powered Drip Irrigation Feasibility Analysis

## Project Overview

This analysis evaluates whether a solar-powered drip irrigation system can
reliably supply a small farm's water needs across a full growing season in a
tropical maritime climate.  The system pairs a photovoltaic array with a
LiFePO4 battery bank and a hybrid inverter to power a centrifugal irrigation
pump using only renewable energy.

The analysis covers seven years of historical weather data (2018–2024) to
capture inter-annual variability in solar irradiance and crop water demand.
The goal is to find the minimum-cost system configuration — number of panels,
battery capacity, and associated hardware — that meets a user-specified
irrigation reliability threshold (default 95 % of scheduled irrigation days
fully served).

---

## Analysis Pipeline

The analysis is organized as a seven-module pipeline, each in a numbered
folder.  Modules may be run independently or chained together via the master
script in `6-run-sims/`.

```
1-solar-power/          Hourly DC power output from the PV array
2-pump-power/           Pump operating characteristics and hardware sizing
3-operating-hours-      Greedy battery+pump simulation (how many hours can
  available/              the pump run each year regardless of schedule?)
4-irrigation/           FAO-56 ET₀, crop water demand, weekly schedule
5-integrated-analysis/  Schedule-constrained energy simulation (reliability)
6-run-sims/             Master orchestrator — sweep parameters, compare configs
7-cost-analysis/        Component costs, shipping, and minimum-cost optimization
```

Each folder contains its own `README.md` with purpose, usage instructions,
input/output descriptions, and a guide to the diagnostic plots.

---

## Quick Start

### Prerequisites

Python 3.9+ with the following packages:

```
pip install matplotlib numpy pandas requests
```

### Running the full pipeline

```bash
cd 6-run-sims
python run_simulation.py                         # all stages, default config
python run_simulation.py --stages 3 4            # irrigation + reliability only
python run_simulation.py --crop tomato --year 2021
python run_simulation.py --sweep-panels 10 12 15 18 20 --sweep-battery 1 2 3
```

### Running individual modules

```bash
# Fetch climate data (one-time setup, calls NSRDB + Open-Meteo APIs)
cd 4-irrigation && python fetch_et_data.py

# Generate irrigation schedule for a specific year/crop
cd 4-irrigation && python irrigation_schedule.py --year 2021 --crop cassava

# Run integrated energy/reliability analysis
cd 5-integrated-analysis && python integrated_analysis.py --year 2021 --panels 15

# Find minimum-cost configuration meeting a reliability threshold
cd 7-cost-analysis && python cost_analysis.py --reliability 95
```

---

## Data Sources

| Dataset | Source | Coverage |
|---|---|---|
| Hourly solar irradiance (GHI/DNI/DHI) | NSRDB PSM3 v3 | 2018–2024 |
| Hourly air temperature | NSRDB PSM3 v3 | 2018–2024 |
| Hourly relative humidity | Open-Meteo ERA5 Historical | 2018–2024 |
| Hourly precipitation | Open-Meteo ERA5 Historical | 2018–2024 |
| Hourly wind speed (10 m) | NSRDB PSM3 v3 | 2018–2024 |
| Crop coefficients (Kc) | FAO-56 Tables 11 & 12 | — |

NSRDB files are downloaded manually from https://nsrdb.nrel.gov/data-viewer.
Open-Meteo data is fetched automatically at runtime with no API key required.

---

## Key System Parameters

| Parameter | Default | Description |
|---|---|---|
| Pump power | 1.263 kW | AC draw (all-or-nothing load) |
| Drip efficiency | 90 % | Fraction of pumped water reaching roots |
| Panel rated power | 405 W | Per panel at STC |
| Panel tilt | 24 ° | From horizontal |
| Battery chemistry | LiFePO4 48 V | Deep-cycle, ≥10 % SoC floor |
| Charge / discharge eff. | 95 % / 95 % | Round-trip ≈ 90 % |
| Growing season | Sep 1 → May 31 | Cross-year (9 months) |
| Default crop | Cassava | FAO-56 Kc curve |

All parameters are adjustable in `6-run-sims/run_simulation.py` CONFIG block
or via command-line flags.

---

## Module Data Flow

```
NSRDB raw CSVs
      │
      ▼
1-solar-power          →  gen-power/*_power.csv  (reference: 15 panels)
                                │
      ┌─────────────────────────┴────────────────────────┐
      ▼                                                   ▼
3-operating-hours-available                    5-integrated-analysis
(greedy pump hours)                            (schedule-constrained)
                                                          ▲
4-irrigation  →  et-data/ + results/  ─────────────────────┘
                                                          │
                                               6-run-sims/output/sweep/
                                               reliability_matrix.csv
                                                          │
                                                          ▼
                                               7-cost-analysis
                                               (minimum-cost config)
```
