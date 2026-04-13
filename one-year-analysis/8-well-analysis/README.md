# 8 — Well Sustainability Analysis

Checks whether the irrigation pump draws a sustainable volume of water from the
farm well, using two complementary tests: session-scale aquifer drawdown and
annual water-balance against groundwater recharge.

---

## Purpose

The pump draws from a shallow well that taps the island's **freshwater lens** — a
body of fresh groundwater that floats atop saline formation water inside a
karst (limestone) aquifer.  Two questions matter:

1. **Does the pump lower the water level enough during a session to threaten dry-pump conditions?**
2. **Does annual extraction outpace natural recharge, depleting the lens over time?**

---

## Aquifer background

The island sits on karstified limestone with very high hydraulic conductivity
(K ≈ 50–600 m/day, median ~80 m/day — Whitaker & Smart 1997).  The freshwater
lens is recharged exclusively by rainfall infiltrating through the permeable
surface.  No streams or rivers supply the aquifer.

The [Ghyben-Herzberg relation](https://en.wikipedia.org/wiki/Ghijben%E2%80%93Herzberg_principle)
governs lens thickness: for every metre of freshwater head above sea level, the
lens extends ~40 m below sea level.  This means even moderate rainfall totals
maintain a substantial body of fresh groundwater.

**Well geometry:**

| Parameter | Value |
|---|---|
| Static water level (SWL) | 11 ft below surface |
| Well total depth | 30 ft below surface |
| Available drawdown | 19 ft (5.8 m) |
| Casing diameter (assumed) | 6 inches |
| Volume in casing | ~57 L — exhausted in < 1 min at full pump rate |

The tiny casing volume means the pump's supply depends entirely on aquifer
inflow, not stored water in the pipe.  This is expected and normal for drilled
wells in karst.

---

## Method

### Session-scale drawdown — Theis (1935) equation

The Theis non-equilibrium equation models transient drawdown at the well:

$$s(r,t) = \frac{Q}{4\pi T} \cdot W(u), \quad u = \frac{r^2 S}{4Tt}$$

The Cooper-Jacob (1946) approximation `W(u) ≈ −0.5772 − ln(u)` applies when
`u < 0.05`, which is satisfied within minutes for this aquifer and flow rate.

Two hydraulic conductivity scenarios are modelled (both from Whitaker & Smart
1997 for Bahamian karst):

| Scenario | K (m/day) | T (m²/day) |
|---|---|---|
| Conservative | 50 | 500 |
| Moderate | 200 | 2,000 |

Specific yield Sy = 0.20 (common for unconfined karst).

### Annual water balance

$$V_{\text{recharge}} = P_{\text{season}} \times f_{\text{recharge}} \times A_{\text{farm}}$$

where $f_{\text{recharge}}$ = 0.40 (40% of rainfall recharges groundwater —
Cant & Weech 1986; Voss & Souza 1987).

Extraction volume is computed from the actual SWD-based irrigation targets in
the module 4 CSVs, grossed up by drip efficiency to get true pump volume.

---

## Inputs

| Source | What it provides |
|---|---|
| `4-irrigation/results/daily_<crop>_<year>.csv` | Per-day irrigation targets, rainfall, ETc |
| Well parameters (hardcoded) | SWL = 11 ft, depth = 30 ft |
| Aquifer parameters (literature) | K range, Sy = 0.20, recharge coeff = 0.40 |
| Pump parameters (from module 5) | Q = 14.39 GPM, drip efficiency = 0.90 |

---

## Outputs

### Images (`images/`)

| File | Content |
|---|---|
| `W1_annual_water_balance.png` | Side-by-side bars: gross extraction vs. recharge, and net balance by year |
| `W2_session_drawdown.png` | Drawdown at the well during a pumping session for both K scenarios |
| `W3_W4_safety_catchment.png` | Annual safety factor (recharge÷extraction) and minimum catchment area diagram |
| `W5_summary_card.png` | One-page summary of key findings |

### CSVs (`results/`)

| File | Content |
|---|---|
| `annual_water_balance.csv` | Per-year: extraction volume, seasonal rainfall, recharge, balance, safety factor |
| `drawdown_summary.csv` | Drawdown, percent of available drawdown, and recovery time for each K scenario |

---

## Usage

```bash
# Default: cassava crop, all years
cd 8-well-analysis
python well_analysis.py

# Override well geometry or recharge coefficient
python well_analysis.py --static-level-ft 11 --well-depth-ft 30 --recharge-coeff 0.40

# Change crop (uses 4-irrigation results for that crop)
python well_analysis.py --crop tomato
```

---

## Key parameters to adjust

| Parameter | Default | When to change |
|---|---|---|
| `--static-level-ft` | 11 | If the water table level changes seasonally |
| `--well-depth-ft` | 30 | If well depth is measured or changed |
| `--recharge-coeff` | 0.40 | Literature range 0.30–0.50 for Bahamian karst |
| `K_CONSERVATIVE_M_D` / `K_MODERATE_M_D` | 50 / 200 | Edit in script if a pump test provides site-specific K |

---

## References

- Cant, R.V. & Weech, P.S. (1986). A review of the geology and hydrology of the Bahamas. *Journal of Hydrology* 84, 253–267.
- Whitaker, F.F. & Smart, P.L. (1997). Groundwater circulation and geochemistry of a karstified bank-marginal fracture system, South Andros Island, Bahamas. *Hydrogeology Journal* 5(2), 4–22.
- Voss, C.I. & Souza, W.R. (1987). Variable density flow and solute transport simulation of regional aquifers. *Water Resources Research* 23(10), 1851–1866.
- Theis, C.V. (1935). The relation between the lowering of the piezometric surface and the rate and duration of discharge of a well using ground-water storage. *Trans. Am. Geophys. Union* 16, 519–524.
- Cooper, H.H. & Jacob, C.E. (1946). A generalized graphical method for evaluating formation constants and summarizing well-field history. *Trans. Am. Geophys. Union* 27(4), 526–534.
