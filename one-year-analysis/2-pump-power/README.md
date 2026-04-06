# Module 2 — Pump Power Characterization

## Purpose

Documents the hydraulic design of the irrigation pump and calculates the AC
power draw for the chosen operating point.  This is a one-time engineering
calculation — the output (pump power in kW) becomes a fixed input to every
other module.

The module also sizes the Variable Frequency Drive (VFD) required to soft-start
the pump from a battery-backed hybrid inverter, and evaluates partial-speed
operation scenarios.

---

## Scripts

### `compute_design_power.py`

Computes the pump operating point from first principles using the Darcy-Weisbach
head-loss model.  Outputs a table of flow rate, dynamic head, hydraulic power,
shaft power, and AC input power across the operating range.

```bash
python compute_design_power.py
```

No arguments required.  Edit the constants at the top of the file to change
pipe diameter, drip line length, or system head.

Key parameters:

| Parameter | Value | Description |
|---|---|---|
| Flow rate | 14.39 GPM | Design irrigation flow |
| Total dynamic head | ≈ 45 ft | Static lift + friction + drip pressure |
| Pump efficiency | ≈ 60 % | Hydraulic efficiency at design point |
| Motor efficiency | ≈ 88 % | AC motor efficiency |
| Power factor | 0.85 | Motor power factor (for VA sizing) |

---

## Inputs

None.  All inputs are physical constants defined inside the script.

---

## Outputs

```
full_power_flow_table.csv    Tabulated pump operating curve:
  flow_gpm         Flow rate [US gal/min]
  head_ft          Total dynamic head [ft]
  P_hydraulic_kW   Useful hydraulic power [kW]
  P_shaft_kW       Shaft power (hydraulic / pump_eff) [kW]
  P_ac_kW          AC input power (shaft / motor_eff / PF) [kW]

pump-results.md              Written summary of the design point selection
                             and VFD sizing rationale.

images/                      Head-capacity curve and power curve plots.
```

---

## Reading the Results

The design AC power at the 14.39 GPM operating point is **1.263 kW**.  This
is the value used throughout the rest of the analysis as `PUMP_POWER_KW`.

The VFD is sized at ≥ 1.5 kW to allow a 20 % head-room margin above the
peak shaft demand and to accommodate motor starting transients.

### Head-Capacity Curve Plot

Shows flow rate (x-axis) versus total dynamic head (y-axis) for the system
curve (rising parabola) overlaid on the pump curve (declining curve from
manufacturer data sheet).  The operating point is their intersection.

### Power Curve Plot

Shows AC input power versus flow rate.  The design operating point is
highlighted.  The VFD can throttle flow to lower power levels if needed
(e.g., during cloudy periods with limited battery).

---

## Notes on VFD Operation

A VFD allows the pump to run at partial speed, reducing both flow and power
consumption.  At 70 % speed the pump draws roughly 35 % of rated power
(affinity laws: P ∝ speed³).  The current pipeline models the pump as an
**all-or-nothing** load (full speed or off) because drip irrigation systems
require minimum pressure to open emitters.  The VFD is still required for:

1. Soft-starting from battery to avoid inrush current tripping the inverter.
2. Overcoming the hybrid inverter's limited surge capacity.
3. Future partial-flow operation if emitter minimum pressure is re-evaluated.
