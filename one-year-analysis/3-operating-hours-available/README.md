# Module 3 — Operating Hours Available (Greedy Simulation)

## Purpose

Answers the question: **ignoring any irrigation schedule, how many total pump
hours can the solar + battery system deliver per year?**

This greedy simulation runs the pump whenever the solar + battery system has
enough energy, up to a configurable daily cap.  It is not constrained to
specific irrigation days or target volumes — it simply accumulates all
available pump time.  The result is an upper bound on pump availability and
reveals failure modes (cloudy spells, battery depletion, etc.).

Compare this with Module 5 (integrated analysis), which enforces the
schedule-constrained demand.

---

## Scripts

### `battery_pump_analysis.py`

Main simulation.  Reads hourly solar power, runs the battery state-of-charge
model, and determines pump operation hour by hour using a greedy dispatch rule.

```bash
python battery_pump_analysis.py                     # default: 2018, 15 panels, 2 kWh
python battery_pump_analysis.py --year 2022
python battery_pump_analysis.py --battery-kwh 4.0 --panels 20
python battery_pump_analysis.py --schedule-days 1 3 5  # Mon/Wed/Fri only
```

Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `BATTERY_CAPACITY_KWH` | 2.0 kWh | Nameplate capacity |
| `BATTERY_MIN_SOC_PCT` | 10 % | Depth-of-discharge limit |
| `BATTERY_MAX_SOC_PCT` | 100 % | Full charge ceiling |
| `BATTERY_CHARGE_EFF` | 95 % | AC→stored kWh efficiency |
| `BATTERY_DISCHARGE_EFF` | 95 % | Stored kWh→AC efficiency |
| `PUMP_POWER_KW` | 1.263 kW | Constant AC draw when running |
| `MAX_HOURS_PER_DAY` | 6 hr | Daily pump cap |
| `MIN_SOLAR_FOR_DISCHARGE` | 0.10 kW | Minimum AC solar before battery discharges |

### `day_diagnostic.py`

Plots the hour-by-hour power flow for a single selected day.  Useful for
debugging unexpected pump outages or battery behaviour.

```bash
python day_diagnostic.py --year 2021 --month 3 --day 15
```

---

## Inputs

```
../1-solar-power/gen-power/
  *_<year>_power.csv     Hourly DC power for the reference 15-panel array.
                         Only the P_dc_kW column is used.
```

---

## Outputs

```
battery-pump/
  <site>_<year>_system.csv    Hourly simulation log:
    datetime           Timestamp
    P_dc_kW            Solar DC power [kW]
    P_solar_ac_kW      Solar AC power after inverter losses [kW]
    battery_soc_kWh    Battery state of charge at end of hour [kWh]
    pump_on            1 if pump ran this hour, else 0
    pump_hrs_today     Cumulative pump hours on this calendar day
    failure            'low_soc' | 'no_solar' | '' — why pump did not run

images/
  P1_annual_pump_hours.png   Total pump hours per month, stacked by cause
  P2_battery_soc.png         Battery SoC over the year with pump events overlaid
  P3_energy_balance.png      Daily solar generation vs. pump consumption
  P4_failure_analysis.png    Days with failures, colour-coded by failure type
```

---

## Plots

**P1 — Annual Pump Hours**
Bar chart of monthly pump-hours.  Longer bars in winter/spring reflect longer
daylight hours at 25 °N.  Low bars may indicate cloud cover or seasonal shading.

**P2 — Battery SoC**
Line chart of battery state of charge (%) throughout the year.  Pump
operation events are shown as tick marks on the x-axis.  Extended periods near
the minimum SoC threshold indicate cloudy spells where the battery is unable
to recover overnight.

**P3 — Energy Balance**
Daily comparison of solar generation (kWh, blue) versus pump consumption (kWh,
orange).  The gap represents energy stored in or exported from the battery.

**P4 — Failure Analysis**
Calendar heatmap showing days when the greedy pump could not run at all (dark)
or ran fewer than target hours (medium), with colour distinguishing battery
depletion from insufficient solar irradiance.

---

## Interpreting Results

A typical result for 15 panels + 2 kWh battery at this site is roughly
**1,500–2,000 pump hours per year** in greedy mode.  The cassava irrigation
schedule requires approximately 400–600 pump hours per season (Sep–May).
This headroom means the constraint is not total energy but scheduling: can
the system deliver pump hours on the specific days demanded?  Module 5
answers that question.
