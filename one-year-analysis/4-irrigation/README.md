# Module 4 — Irrigation Scheduling (FAO-56 ET₀)

## Purpose

Computes daily crop water demand using the FAO-56 Penman-Monteith reference
evapotranspiration (ET₀) method and derives a weekly irrigation schedule
matched to the pump's flow capacity.

Three scripts work in sequence:

1. `fetch_et_data.py` — download climate data and compute daily ET₀.
2. `irrigation_schedule.py` — apply crop coefficients, compute net irrigation
   demand, and produce a weekly pump schedule.
3. `plot_irrigation.py` — generate six diagnostic plots per crop per season.

---

## Scripts

### `fetch_et_data.py`  *(run once per site)*

Fetches data from two APIs and computes daily FAO-56 ET₀ for each year:

- **NSRDB PSM3** — hourly solar radiation and air temperature.
- **Open-Meteo ERA5 Historical** — hourly relative humidity and precipitation
  (no API key required).

```bash
python fetch_et_data.py                  # 2018–2024, default paths
python fetch_et_data.py --year 2022
python fetch_et_data.py --nsrdb-dir /path/to/NSRDB-raw
```

Output: `et-data/et_data_<year>.csv` with 22 columns including all
intermediate ET₀ terms for verification.

**This script only needs to be run once**, unless new NSRDB years are added.
The Open-Meteo API is called automatically for humidity and precipitation.

### `irrigation_schedule.py`

Reads ET data CSVs, applies the FAO-56 crop coefficient (Kc) curve, computes
effective rainfall, and generates a weekly irrigation schedule.  Prints a
per-season report and saves CSVs.

```bash
python irrigation_schedule.py                        # cassava 2018
python irrigation_schedule.py --year 2021
python irrigation_schedule.py --crop tomato --year 2020
python irrigation_schedule.py --years 2018 2019 2020 2021 2022 2023 2024
```

Available crops (from FAO-56 Tables 11 & 12): `cassava`, `tomato`, `pepper`,
`cucumber`, `lettuce`, `cabbage`, `watermelon`, `sweet_potato`, `eggplant`,
`squash`, `beans`.

Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `FARM_AREA_ACRES` | 0.78 acres | Total irrigated area |
| `PUMP_FLOW_GPM` | 14.39 GPM | Pump flow rate |
| `DRIP_EFFICIENCY` | 90 % | Fraction of pumped water to roots |
| `EFF_RAIN_FACTOR` | 80 % | Fraction of rainfall entering root zone |
| Season start | Sep 1 | Planting date (year Y) |
| Season end | May 31 | Harvest date (year Y+1) |

### `plot_irrigation.py`

Generates six diagnostic PNG plots per crop per season (7 years × 6 = 42
plots for all years).

```bash
python plot_irrigation.py                        # cassava, all years
python plot_irrigation.py --crop tomato --year 2021
```

---

## Inputs

```
../1-solar-power/NSRDB-raw/
  *_<year>.csv           NSRDB hourly files (read by fetch_et_data.py for
                         temperature and solar radiation columns).

et-data/                 Written by fetch_et_data.py:
  et_data_<year>.csv     Daily climate + ET₀ data for each year.
```

---

## Outputs

```
et-data/
  et_data_2018.csv … et_data_2024.csv
    date              YYYY-MM-DD
    T_max_C, T_min_C  Daily max/min air temperature [°C]
    RH_mean_pct       Mean relative humidity [%]
    u2_m_s            Wind speed at 2 m height [m/s]
    Rs_MJ_m2          Incoming solar radiation [MJ/m²/day]
    Rn_MJ_m2          Net radiation [MJ/m²/day]
    ET0_mm            Reference ET₀ by FAO-56 Penman-Monteith [mm/day]
    precip_mm         Total daily precipitation [mm]
    … (22 columns total, including all intermediate terms)

results/
  weekly_<crop>_<year>.csv    One row per week of the growing season:
    week_start        Monday date of the week
    ETc_mm            Crop ET demand for the week [mm]
    rain_mm           Total precipitation [mm]
    net_irr_mm        Net irrigation needed (ETc − eff_rain, ≥ 0) [mm]
    irr_hrs           Pump hours required to deliver net_irr_mm
    irr_days_week     Number of irrigation days scheduled this week
    irrigate_days     Comma-separated list of weekday numbers (1=Mon … 7=Sun)

  daily_<crop>_<year>.csv     One row per day of the growing season:
    date, ET0_mm, Kc, ETc_mm, rain_mm, eff_rain_mm, net_irr_mm

results/images/<crop>_<year>/
  P1_water_balance.png
  P2_kc_et_curves.png
  P3_weekly_schedule.png
  P4_precip_heatmap.png
  P5_monthly_balance.png
  P6_season_dashboard.png
```

---

## FAO-56 Penman-Monteith ET₀

The reference evapotranspiration is computed as:

```
         0.408 Δ (Rn − G) + γ [900/(T+273)] u₂ (es − ea)
ET₀ =  ─────────────────────────────────────────────────────
                    Δ + γ (1 + 0.34 u₂)
```

where Δ is the slope of the saturation vapour pressure curve, γ is the
psychrometric constant, Rn is net radiation, G ≈ 0 (daily), T is mean
temperature, u₂ is 2-m wind speed, es and ea are saturation and actual vapour
pressure.

Wind speed from NSRDB is at 10 m; it is corrected to 2 m with:

```
u₂ = u₁₀ × (4.87 / ln(67.8 × 10 − 5.42))  =  u₁₀ × 0.748
```

---

## Plots

**P1 — Water Balance**
Stacked area chart showing cumulative ETc (blue), effective rainfall (green),
and net irrigation (orange) day by day.  Large storm events appear as spikes
on the rainfall area; periods of low rainfall drive high net irrigation
demand.

**P2 — Kc and ET Curves**
Dual-axis line chart.  The left axis shows ET₀ (grey) and ETc = Kc × ET₀
(blue) in mm/day.  The right axis shows the Kc curve (green) with growth
stage shading (initial → development → mid-season → late).

**P3 — Weekly Schedule**
Bar chart with one group per week.  Bars represent weekly ETc, rainfall, and
net irrigation volume.  Dot markers show pump capacity for the scheduled
irrigation days.  Red ✕ marks weeks where irrigation demand exceeds what the
pump can deliver in the allowed days.

**P4 — Precipitation Heatmap**
Month-by-month calendar grid where each cell is one day, coloured by rainfall
depth.  Storm events (≥25 mm/day) are circled.  Reveals seasonality of
rainfall and clustering of dry spells.

**P5 — Monthly Balance**
Grouped bar chart comparing monthly totals of ETc, effective rainfall, and net
irrigation.  Useful for identifying months that consistently require the most
pump time.

**P6 — Season Dashboard**
Four-panel summary figure: (1) Kc timeline strip, (2) water balance pie chart
(rain vs. irrigation fraction), (3) weekly demand heatmap, (4) monthly pump
hours bar chart.
