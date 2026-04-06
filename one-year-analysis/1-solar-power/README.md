# Module 1 — Solar Power Generation

## Purpose

Converts raw NSRDB PSM3 hourly irradiance data into estimated DC power output
for the photovoltaic array.  The model accounts for panel temperature
derating, angle-of-incidence losses, and a system performance ratio that
captures wiring, mismatch, and soiling losses.

This module produces the **reference solar power CSVs** that all downstream
modules rely on.  The reference array is always 15 panels × 405 W.  Modules
that sweep different panel counts scale these CSVs proportionally rather than
re-running Stage 1.

---

## Scripts

### `solar_analysis.py`

Core power model.  Reads one NSRDB annual CSV, applies the PV model, and
writes a hourly power CSV.

```bash
python solar_analysis.py                            # default: 2018, 15 panels
python solar_analysis.py --year 2022
python solar_analysis.py --panels 20 --tilt 30
```

Key parameters (edit at the top of the file or override via flags):

| Parameter | Default | Description |
|---|---|---|
| `N_PANELS` | 15 | Number of panels in the array |
| `PANEL_RATED_POWER_W` | 405 W | Rated power at STC |
| `PANEL_EFFICIENCY` | 20.7 % | Module efficiency |
| `TILT_DEG` | 24 ° | Tilt from horizontal |
| `AZIMUTH_DEG` | 180 ° | Panel facing direction (180 = due south) |
| `PERFORMANCE_RATIO` | 0.85 | Wiring + mismatch + soiling losses |
| `TEMP_COEFF_PMAX` | −0.35 %/°C | Power drop per °C above STC |
| `NOCT` | 45 °C | Nominal operating cell temperature |

### `solar_statistics.py`

Reads all annual power CSVs and produces summary statistics (peak power,
annual energy, capacity factor, seasonal variation).

```bash
python solar_statistics.py
```

### `process_NSRDB.py`

Utility script for inspecting and pre-processing raw NSRDB CSV files (column
headers, timezone offsets, missing values).  Run this if you add a new year
of NSRDB data and something looks wrong.

```bash
python process_NSRDB.py --year 2023
```

---

## Inputs

```
NSRDB-raw/
  *.csv          One file per year (2018–2024), downloaded from
                 https://nsrdb.nrel.gov/data-viewer
                 Site ID 4469509  |  24.96 °N  −78.05 °W  |  UTC−5
```

Each NSRDB file contains hourly columns including: `GHI`, `DNI`, `DHI`,
`Temperature`, `Wind Speed`, `Surface Albedo`, and a timestamp.

---

## Outputs

```
gen-power/
  <site>_<year>_power.csv    One row per hour; key columns:
    datetime       ISO-format timestamp (local time, UTC−5)
    GHI            Global horizontal irradiance [W/m²]
    DNI            Direct normal irradiance [W/m²]
    DHI            Diffuse horizontal irradiance [W/m²]
    P_dc_kW        Estimated DC power output of the full array [kW]
    T_cell_C       Estimated cell temperature [°C]

images/
  Annual and seasonal plots of irradiance and power output.
```

---

## Plots

The images produced by `solar_statistics.py` show:

- **Annual energy yield** — bar chart of total DC kWh per year.
- **Monthly average power** — how production shifts through the seasons.
- **Peak-hour distribution** — histogram of hours with P_dc > threshold.
- **Diurnal profile** — average hourly power by month, showing sunrise/sunset
  and midday peak.

---

## Physical Model Notes

**Plane-of-array irradiance** is computed from GHI/DNI/DHI using the
Perez transposition model.  **Cell temperature** is estimated as:

```
T_cell = T_ambient + (NOCT − 20) / 800 × GHI
```

**DC power** derates linearly with cell temperature above 25 °C:

```
P_dc = N_panels × P_stc × (1 + temp_coeff × (T_cell − 25)) × PR
```

The performance ratio (PR = 0.85) represents a 15 % combined loss from
wiring resistance, module mismatch, soiling, and inverter clipping.
