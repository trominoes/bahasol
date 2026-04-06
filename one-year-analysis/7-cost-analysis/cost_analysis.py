"""
cost_analysis.py
================
Finds the minimum-cost solar + battery system configuration that meets a
user-specified irrigation reliability threshold (default 95 %).

System architecture note — hybrid inverter vs. VFD
---------------------------------------------------
A hybrid inverter (e.g. Growatt SPF 3000TL, EG4 3000EHV, Deye SUN-3K-SG04LP1)
is the central energy-management device.  It performs three jobs in one unit:

  1. MPPT solar charge control (DC panels → battery / AC load)
  2. Battery charge / discharge management (bidirectional DC↔AC)
  3. AC power output for connected loads (the pump)

A Variable Frequency Drive (VFD) is a separate motor controller that converts
fixed-frequency AC to variable-frequency AC, primarily to (a) limit motor
inrush current at start-up and (b) control pump speed.  Because this analysis
models the pump as an all-or-nothing load (full speed or off), a VFD's
speed-control benefit is not used.  The remaining concern — inrush current —
is handled by the hybrid inverter's surge capacity: a 3 kW unit typically
delivers 6 kW peak (~26 A at 230 V), which comfortably absorbs the 5–7×
inrush of a 1.263 kW pump.  Many irrigation pumps also include built-in
soft-start electronics.

Conclusion: a standalone VFD is not modelled as a required cost.  If pump
motor protection or future variable-speed operation becomes a requirement, a
dedicated soft-starter (~$100–150) or VFD (~$350–600) may be added to
COST_MISC_USD or as its own line item.

Cost model summary
------------------
  Fixed costs (required in every configuration):
    Pump (1.5 kW nominal)           ~$1,000
    Hybrid inverter (3 kW)          ~$1,200  (includes MPPT + battery mgmt)
    Wiring + conduit                ~$  500
    Miscellaneous / contingency     ~$  300

  Variable costs:
    Extra panels beyond 15          $180 / panel
    Battery (LiFePO4 48 V)         capacity-tiered (see battery_cost())

  Panel mounts, drip irrigation system: NOT included.  These are assumed to
  be either already on hand, locally sourced, or outside the scope of the
  electrical system hardware shipped to the site.

  Shipping (origin → destination, LTL freight):
    Class 85 LTL, CWT-based pricing, fuel surcharge, liftgate.
    See shipping_cost() for sourcing rationale.

Usage
-----
    # Run with default reliability threshold (95 %) and sweep CSV:
    python cost_analysis.py

    # Use a specific reliability threshold:
    python cost_analysis.py --reliability 90

    # Provide a custom reliability matrix CSV:
    python cost_analysis.py --matrix-csv ../6-run-sims/output/sweep/reliability_matrix.csv

    # Run built-in cost sweep (no pre-computed matrix needed):
    python cost_analysis.py --panels 10 12 15 18 20 --battery 1 2 3 4 5

    # Show all configurations above threshold, sorted by cost:
    python cost_analysis.py --show-all

Outputs
-------
    results/
      cost_breakdown_optimal.csv   Itemised cost for the optimal config
      all_configs_above_<N>pct.csv All valid configs sorted by total cost

    images/
      C1_reliability_heatmap.png   Reliability % as a function of panels + battery
      C2_cost_heatmap.png          Total system cost as a function of panels + battery
      C3_cost_breakdown.png        Stacked bar: cost breakdown for top configs
      C4_reliability_vs_cost.png   Scatter of reliability vs. cost (Pareto frontier)
      C5_optimal_summary.png       Single-page summary of the optimal configuration
"""

import argparse
import csv
import math
import os
import sys
from itertools import product as iterproduct

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as ticker
import numpy as np

# ===========================================================================
# COST MODEL PARAMETERS
# ===========================================================================
# All costs in USD (2024–2025 retail).  Edit to reflect current prices/quotes.

# ── Fixed hardware (every configuration) ────────────────────────────────────
COST_PUMP_USD      = 1_000.0  # 1.5 kW centrifugal pump (e.g. Grundfos CM5)
COST_INVERTER_USD  = 1_200.0  # 3 kW hybrid inverter with MPPT (e.g. EG4 3000EHV
                               # ~$900–1,300 on Amazon/Sol-Ark/EG4Electronics)
COST_WIRING_USD    =   500.0  # DC wiring, conduit, MC4 connectors, combiner box
COST_MISC_USD      =   300.0  # breakers, fuses, labels, cable ties, contingency
                               # Note: add ~$100–150 here if a soft-starter is needed

# ── Variable hardware ────────────────────────────────────────────────────────
COST_PANEL_ABOVE_15_USD = 180.0  # per panel beyond the free first 15
                                  # Based on 2024 wholesale pricing for
                                  # 400–410 W monocrystalline modules
                                  # (e.g. Jinko JKM405M-54HL4-V, ~$0.30–0.45/W
                                  # bulk; ~$140–200 per panel retail)

# ── Panel count at which panels become free ──────────────────────────────────
FREE_PANEL_LIMIT = 15

# ── Battery cost — capacity-tiered model ─────────────────────────────────────
# Source: 2024 retail pricing for 48 V LiFePO4 rack/server batteries.
# Key products used for calibration:
#   EG4 48V 100Ah (4.8 kWh server rack)   : ~$1,000–1,100  → ~$208–229/kWh
#   Renogy 48V 100Ah (4.8 kWh)            : ~$1,100–1,300  → ~$229–271/kWh
#   EG4 48V 200Ah (9.6 kWh)               : ~$2,100–2,300  → ~$219–240/kWh
#   LiTime 48V 50Ah  (2.4 kWh)            : ~$  650–750    → ~$271–313/kWh
#   DIY EVE/CALB LiFePO4 cells + BMS      : ~$100–140/kWh  (not retail-ready)
#
# Note: LiFePO4 batteries are sold in discrete module sizes.  The simulation
# parameterises battery as a continuous kWh value; the tiered pricing below
# approximates the real cost curve across that range.
#
# Tier boundaries (nameplate kWh):
BATTERY_TIER_BREAKPOINTS = [0.0,  3.0,  7.0]   # lower edges of each tier
BATTERY_TIER_RATES_USD   = [330,  255,  220]    # $/kWh for each tier
#
# Interpretation:
#   0–3 kWh  : $330/kWh  (small modules; premium per-kWh cost for low capacity)
#   3–7 kWh  : $255/kWh  (standard 100 Ah rack modules; best value range)
#   >7 kWh   : $220/kWh  (multiple modules or 200 Ah units; volume benefit)

# ── Component weights (kg) — for shipping estimate ───────────────────────────
WEIGHT_PANEL_KG         = 21.5   # per 405-W monocrystalline panel (mfr datasheets)
WEIGHT_BATTERY_PER_KWH  = 11.0   # kg per kWh nameplate, LiFePO4 48 V
                                   # (EG4 48V 100Ah = 46.3 kg / 4.8 kWh ≈ 9.6 kg/kWh;
                                   #  Renogy 48V 100Ah = 52 kg / 4.8 kWh ≈ 10.8 kg/kWh;
                                   #  use 11.0 kg/kWh as conservative estimate)
WEIGHT_INVERTER_KG      = 12.0   # 3 kW hybrid inverter
                                   # (EG4 3000EHV ≈ 10.5 kg; Growatt SPF3000 ≈ 12 kg)
WEIGHT_PUMP_KG          = 15.0   # 1.5 kW pump assembly + motor

# ── Shipping parameters — LTL freight ────────────────────────────────────────
# Route: specified origin → destination (configure below)
# Method: Less-Than-Truckload (LTL) freight, NMFC Class 85
#
# NMFC classification:
#   Solar panels   : NMFC 155050, typically Class 85 (density ~8–12 pcf)
#   Inverter/charger: Class 85 (electronics, packaged)
#   LiFePO4 battery : Class 85–92.5 (UN 3480/3481, packaged)
#   Pump / motor   : Class 85 (machinery, packaged)
#
# Rate calibration:
#   Long-haul LTL Class 85 (~2,500–3,000 miles) published tariff CWT rates
#   (XPO, Old Dominion, Saia): ~$250–450/cwt.  With standard business-account
#   discount (40–55% off tariff), effective rate ≈ $120–200/cwt.
#   Use $140/cwt for shipments ≤500 lbs, $110/cwt above (density discount).
#
#   Fuel surcharge: American Transportation Research Institute (ATRI) national
#   average diesel fuel surcharge index typically runs 20–28%.  Use 25%.
#
#   Liftgate / residential delivery: $100–175 for farm/residential sites.
#
# Sources:
#   - NMFC Item 155050 (solar panels): FreightClass.com cross-reference
#   - CWT rate calibration: FreightQuote.com, uShip.com quotes (2024)
#   - Fuel surcharge index: ATRI 2024 Operational Costs report
#   - Carrier tariff discounts: industry standard 45–55% off published tariff
#     for non-spot-market shippers

SHIP_CWT_RATE_SMALL     = 140.0   # $/cwt for ≤500 lbs (≈227 kg)
SHIP_CWT_RATE_LARGE     = 110.0   # $/cwt for >500 lbs (density discount)
SHIP_WEIGHT_BREAK_LBS   = 500.0   # weight break point [lbs]
SHIP_FUEL_SURCHARGE_PCT = 0.25    # 25% fuel + accessorial surcharge
SHIP_LIFTGATE_USD       = 125.0   # liftgate / residential delivery fee
SHIP_MINIMUM_USD        = 250.0   # LTL minimum shipment charge

# ── Default sweep ranges (used when no matrix CSV is provided) ───────────────
DEFAULT_PANELS_SWEEP  = [10, 12, 15, 18, 20]
DEFAULT_BATTERY_SWEEP = [1, 2, 3, 4, 5]

# ── Default paths ────────────────────────────────────────────────────────────
_HERE            = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_MATRIX  = os.path.join(_HERE, '..', '6-run-sims', 'output',
                                 'sweep', 'reliability_matrix.csv')
_RESULTS_DIR     = os.path.join(_HERE, 'results')
_IMAGES_DIR      = os.path.join(_HERE, 'images')


# ===========================================================================
# COST MODEL
# ===========================================================================

def battery_cost(battery_kwh: float) -> float:
    """
    Capacity-tiered LiFePO4 battery cost.

    Uses three price tiers calibrated to 2024 retail pricing for 48 V
    server-rack / deep-cycle LiFePO4 modules (see BATTERY_TIER_* constants
    and their inline source comments at the top of this file).

    Parameters
    ----------
    battery_kwh : float
        Nameplate battery capacity in kWh.

    Returns
    -------
    float
        Estimated battery purchase cost in USD.
    """
    if battery_kwh <= 0.0:
        return 0.0

    breakpoints = BATTERY_TIER_BREAKPOINTS  # [0, 3, 7]
    rates       = BATTERY_TIER_RATES_USD    # [330, 255, 220]

    cost = 0.0
    remaining = battery_kwh
    for i, bp in enumerate(breakpoints):
        # Width of this tier (kWh)
        next_bp = breakpoints[i + 1] if i + 1 < len(breakpoints) else float('inf')
        tier_width = next_bp - bp
        chunk = min(remaining, tier_width)
        cost += chunk * rates[i]
        remaining -= chunk
        if remaining <= 0.0:
            break

    # If remaining > 0, we're past the last breakpoint — apply last rate
    if remaining > 0.0:
        cost += remaining * rates[-1]

    return cost


def shipping_cost(total_weight_kg: float) -> float:
    """
    Estimate LTL freight cost for the given shipment weight.

    Uses a CWT (dollars per hundred pounds) model calibrated to Class 85 LTL
    rates for long-haul shipments (~2,500–3,000 miles), with a fuel surcharge
    and a liftgate fee for farm/residential delivery.

    See SHIP_* constants and their source comments at the top of this file
    for the rate calibration methodology and references.

    Parameters
    ----------
    total_weight_kg : float
        Combined weight of all shipped components in kg.

    Returns
    -------
    float
        Estimated shipping cost in USD, not less than SHIP_MINIMUM_USD.
    """
    weight_lbs = total_weight_kg * 2.20462
    weight_cwt = weight_lbs / 100.0

    # CWT rate with weight-break discount
    if weight_lbs <= SHIP_WEIGHT_BREAK_LBS:
        base_rate_per_cwt = SHIP_CWT_RATE_SMALL
    else:
        # Blended: first 500 lbs at small rate, remainder at large rate
        base_rate_per_cwt = (
            (SHIP_WEIGHT_BREAK_LBS / 100.0 * SHIP_CWT_RATE_SMALL +
             (weight_lbs - SHIP_WEIGHT_BREAK_LBS) / 100.0 * SHIP_CWT_RATE_LARGE)
            / weight_cwt
        )

    freight  = weight_cwt * base_rate_per_cwt
    fuel     = freight * SHIP_FUEL_SURCHARGE_PCT
    liftgate = SHIP_LIFTGATE_USD

    total = freight + fuel + liftgate
    return max(total, SHIP_MINIMUM_USD)


def component_weight(n_panels: int, battery_kwh: float) -> float:
    """
    Total shipped weight in kg for a given configuration.

    Includes panels, battery modules, hybrid inverter, and pump.
    Wiring and miscellaneous items are assumed to be packaged with the above
    or are lightweight enough to be shipped locally (excluded from freight calc).

    Parameters
    ----------
    n_panels : int
        Total number of solar panels.
    battery_kwh : float
        Battery nameplate capacity in kWh.

    Returns
    -------
    float
        Total shipped weight in kg.
    """
    return (n_panels    * WEIGHT_PANEL_KG +
            battery_kwh * WEIGHT_BATTERY_PER_KWH +
            WEIGHT_INVERTER_KG +
            WEIGHT_PUMP_KG)


def system_cost(n_panels: int, battery_kwh: float) -> dict:
    """
    Compute the itemised and total system cost for a given configuration.

    Parameters
    ----------
    n_panels : int
        Total number of solar panels in the array.
    battery_kwh : float
        Battery nameplate capacity in kWh.

    Returns
    -------
    dict with keys:
        pump, inverter, wiring, misc  — fixed costs
        panels_extra  — cost of panels beyond the free limit
        battery       — battery purchase cost (tiered)
        shipping      — estimated LTL freight
        total         — sum of all items
        weight_kg     — total shipped weight
    """
    cost_pump     = COST_PUMP_USD
    cost_inverter = COST_INVERTER_USD
    cost_wiring   = COST_WIRING_USD
    cost_misc     = COST_MISC_USD

    extra_panels      = max(0, n_panels - FREE_PANEL_LIMIT)
    cost_panels_extra = extra_panels * COST_PANEL_ABOVE_15_USD
    cost_battery      = battery_cost(battery_kwh)

    weight    = component_weight(n_panels, battery_kwh)
    cost_ship = shipping_cost(weight)

    total = (cost_pump + cost_inverter + cost_wiring + cost_misc +
             cost_panels_extra + cost_battery + cost_ship)

    return {
        'pump'         : cost_pump,
        'inverter'     : cost_inverter,
        'wiring'       : cost_wiring,
        'misc'         : cost_misc,
        'panels_extra' : cost_panels_extra,
        'battery'      : cost_battery,
        'shipping'     : cost_ship,
        'total'        : total,
        'weight_kg'    : weight,
    }


# ===========================================================================
# RELIABILITY MATRIX I/O
# ===========================================================================

def load_reliability_matrix(csv_path: str) -> dict:
    """
    Load a reliability matrix CSV produced by run_simulation.py sweep mode.

    Expected format:
        n_panels, batt_1kWh, batt_2kWh, ...
        10,        72.3,      81.4, ...

    Returns
    -------
    dict  {(n_panels: int, battery_kwh: float): reliability_pct: float}
    """
    matrix = {}
    with open(csv_path, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            n_p = int(row['n_panels'])
            for key, val in row.items():
                if key == 'n_panels':
                    continue
                try:
                    batt = float(key.replace('batt_', '').replace('kWh', ''))
                    rel  = float(val) if val not in ('', 'None', 'N/A') else None
                    matrix[(n_p, batt)] = rel
                except ValueError:
                    pass
    return matrix


def build_synthetic_matrix(panels_sweep: list, battery_sweep: list) -> dict:
    """
    Build a placeholder reliability matrix when no pre-computed CSV exists.

    Uses a simple monotonic model calibrated roughly to expected site
    performance.  NOT physically rigorous — for cost-structure illustration
    only.  Run run_simulation.py --sweep-panels ... --sweep-battery ... to
    replace this with real values.
    """
    matrix = {}
    for n_p, b_k in iterproduct(panels_sweep, battery_sweep):
        x = (n_p / 15.0) * 0.6 + (b_k / 3.0) * 0.4
        rel = 100.0 / (1.0 + math.exp(-6.0 * (x - 0.85)))
        matrix[(n_p, b_k)] = round(rel, 1)
    return matrix


# ===========================================================================
# OPTIMISATION
# ===========================================================================

def find_optimal(matrix: dict, threshold_pct: float) -> list:
    """
    Return all configurations meeting or exceeding threshold_pct, sorted by
    total system cost ascending.
    """
    results = []
    for (n_p, b_k), rel in matrix.items():
        if rel is None:
            continue
        if rel >= threshold_pct:
            breakdown = system_cost(n_p, b_k)
            results.append({
                'n_panels'      : n_p,
                'battery_kwh'   : b_k,
                'reliability'   : rel,
                'cost_breakdown': breakdown,
                'total_cost'    : breakdown['total'],
            })
    results.sort(key=lambda r: r['total_cost'])
    return results


# ===========================================================================
# REPORT
# ===========================================================================

def print_report(candidates: list, threshold_pct: float, show_all: bool = False):
    """Print a formatted cost + reliability report to stdout."""
    print(f'\n{"═"*72}')
    print(f'  COST ANALYSIS  —  Reliability threshold: {threshold_pct:.0f}%')
    print(f'{"═"*72}')

    if not candidates:
        print(f'\n  ✗  No configuration meets the {threshold_pct:.0f}% reliability threshold.')
        print('     Try lowering --reliability or expanding the panel/battery sweep.')
        return

    optimal = candidates[0]
    print(f'\n  ★  OPTIMAL CONFIGURATION')
    print(f'     {optimal["n_panels"]} panels  ×  405 W  '
          f'+  {optimal["battery_kwh"]:.1f} kWh LiFePO4 battery')
    print(f'     Reliability : {optimal["reliability"]:.1f}%')
    print(f'     Total cost  : ${optimal["total_cost"]:,.0f}')

    cb = optimal['cost_breakdown']
    print(f'\n  Cost breakdown:')
    print(f'     Pump (1.5 kW)              ${cb["pump"]:>8,.0f}')
    print(f'     Hybrid inverter (3 kW)    ${cb["inverter"]:>8,.0f}')
    print(f'     Wiring + conduit          ${cb["wiring"]:>8,.0f}')
    print(f'     Misc / contingency        ${cb["misc"]:>8,.0f}')
    print(f'     Extra panels (>15)        ${cb["panels_extra"]:>8,.0f}')
    print(f'     Battery ({optimal["battery_kwh"]:.1f} kWh)        ${cb["battery"]:>8,.0f}')
    print(f'     Shipping ({cb["weight_kg"]:.0f} kg)        ${cb["shipping"]:>8,.0f}')
    print(f'     {"─"*38}')
    print(f'     TOTAL                     ${cb["total"]:>8,.0f}')
    print(f'\n  Battery pricing note:')
    print(f'     Tiered model: $330/kWh (0–3 kWh) → $255/kWh (3–7 kWh) → $220/kWh (>7 kWh)')
    print(f'     Based on 2024 retail: EG4/Renogy/LiTime 48V LiFePO4 rack batteries')
    print(f'  Shipping pricing note:')
    print(f'     LTL Class 85, CWT rate + 25% fuel surcharge + $125 liftgate')
    print(f'     Calibrated to XPO/ODFL/Saia long-haul tariffs with ~50% discount')

    n_show = len(candidates) if show_all else min(5, len(candidates))
    if len(candidates) > 1:
        print(f'\n  All configurations meeting {threshold_pct:.0f}% threshold '
              f'(showing top {n_show} by cost):')
        print(f'  {"Panels":>6}  {"Battery":>9}  {"Reliability":>12}  {"Total Cost":>12}')
        print(f'  {"─"*6}  {"─"*9}  {"─"*12}  {"─"*12}')
        for c in candidates[:n_show]:
            print(f'  {c["n_panels"]:>6}  {c["battery_kwh"]:>8.1f}kWh'
                  f'  {c["reliability"]:>11.1f}%'
                  f'  ${c["total_cost"]:>10,.0f}')
    print(f'{"═"*72}')


# ===========================================================================
# PLOTS
# ===========================================================================

def _pivot(matrix: dict, panels: list, battery: list) -> np.ndarray:
    """Build a 2-D array: rows = panels (ascending), cols = battery (ascending)."""
    arr = np.full((len(panels), len(battery)), np.nan)
    for i, n_p in enumerate(panels):
        for j, b_k in enumerate(battery):
            v = matrix.get((n_p, b_k))
            if v is not None:
                arr[i, j] = v
    return arr


def _cost_pivot(panels: list, battery: list) -> np.ndarray:
    arr = np.zeros((len(panels), len(battery)))
    for i, n_p in enumerate(panels):
        for j, b_k in enumerate(battery):
            arr[i, j] = system_cost(n_p, b_k)['total']
    return arr


def make_plots(matrix: dict, candidates: list, threshold_pct: float,
               panels_list: list, battery_list: list, images_dir: str):
    """Generate all five diagnostic cost-analysis plots."""
    os.makedirs(images_dir, exist_ok=True)
    panels_sorted  = sorted(set(n for n, _ in matrix))
    battery_sorted = sorted(set(b for _, b in matrix))

    rel_grid  = _pivot(matrix, panels_sorted, battery_sorted)
    cost_grid = _cost_pivot(panels_sorted, battery_sorted)

    # ── C1: Reliability heatmap ──────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    im = ax.imshow(rel_grid, aspect='auto', origin='lower',
                   cmap='RdYlGn', vmin=60, vmax=100)
    ax.set_xticks(range(len(battery_sorted)))
    ax.set_xticklabels([f'{b:.1f} kWh' for b in battery_sorted])
    ax.set_yticks(range(len(panels_sorted)))
    ax.set_yticklabels([f'{p} panels' for p in panels_sorted])
    ax.set_xlabel('Battery capacity')
    ax.set_ylabel('Number of panels')
    ax.set_title(f'Irrigation Reliability (%) — ≥{threshold_pct:.0f}% shown in bold')
    fig.colorbar(im, ax=ax, label='Reliability (%)')
    for i in range(len(panels_sorted)):
        for j in range(len(battery_sorted)):
            v = rel_grid[i, j]
            if not np.isnan(v):
                weight = 'bold' if v >= threshold_pct else 'normal'
                colour = 'white' if v < 75 else 'black'
                ax.text(j, i, f'{v:.0f}%', ha='center', va='center',
                        fontsize=9, fontweight=weight, color=colour)
    fig.tight_layout()
    fig.savefig(os.path.join(images_dir, 'C1_reliability_heatmap.png'), dpi=150)
    plt.close(fig)
    print(f'  Saved C1_reliability_heatmap.png')

    # ── C2: Cost heatmap ─────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    im = ax.imshow(cost_grid / 1000, aspect='auto', origin='lower',
                   cmap='YlOrRd_r')
    ax.set_xticks(range(len(battery_sorted)))
    ax.set_xticklabels([f'{b:.1f} kWh' for b in battery_sorted])
    ax.set_yticks(range(len(panels_sorted)))
    ax.set_yticklabels([f'{p} panels' for p in panels_sorted])
    ax.set_xlabel('Battery capacity')
    ax.set_ylabel('Number of panels')
    ax.set_title('Total System Cost (USD thousands)')
    fig.colorbar(im, ax=ax, label='Cost ($k)')
    for i in range(len(panels_sorted)):
        for j in range(len(battery_sorted)):
            ax.text(j, i, f'${cost_grid[i,j]/1000:.1f}k',
                    ha='center', va='center', fontsize=9, color='black')
    fig.tight_layout()
    fig.savefig(os.path.join(images_dir, 'C2_cost_heatmap.png'), dpi=150)
    plt.close(fig)
    print(f'  Saved C2_cost_heatmap.png')

    # ── C3: Cost breakdown for top configurations ────────────────────────────
    top_n = min(6, len(candidates))
    if top_n == 0:
        print('  Skipping C3 (no candidates above threshold)')
    else:
        top = candidates[:top_n]
        labels   = [f'{c["n_panels"]}p / {c["battery_kwh"]:.1f}kWh' for c in top]
        cats     = ['pump', 'inverter', 'wiring', 'misc', 'panels_extra',
                    'battery', 'shipping']
        cat_lbl  = ['Pump', 'Inverter', 'Wiring', 'Misc', 'Extra panels',
                    'Battery', 'Shipping']
        colours  = ['#2196F3', '#9C27B0', '#795548', '#9E9E9E',
                    '#F44336', '#00BCD4', '#FFC107']

        fig, ax = plt.subplots(figsize=(10, 6))
        bottoms = np.zeros(top_n)
        x = np.arange(top_n)
        for cat, lbl, col in zip(cats, cat_lbl, colours):
            vals = np.array([c['cost_breakdown'][cat] for c in top])
            ax.bar(x, vals, bottom=bottoms, label=lbl, color=col)
            bottoms += vals

        for i, c in enumerate(top):
            ax.text(i, c['total_cost'] + 30, f'{c["reliability"]:.0f}%\nrel.',
                    ha='center', va='bottom', fontsize=8, color='#333333')

        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=15, ha='right')
        ax.set_ylabel('Cost (USD)')
        ax.set_title(f'Cost Breakdown — Top {top_n} Configurations '
                     f'(≥{threshold_pct:.0f}% reliability, sorted by cost)')
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(
            lambda v, _: f'${v:,.0f}'))
        ax.legend(loc='upper right', fontsize=8, ncol=2)
        fig.tight_layout()
        fig.savefig(os.path.join(images_dir, 'C3_cost_breakdown.png'), dpi=150)
        plt.close(fig)
        print(f'  Saved C3_cost_breakdown.png')

    # ── C4: Reliability vs. Cost scatter (Pareto frontier) ───────────────────
    fig, ax = plt.subplots(figsize=(8, 5))
    all_configs = []
    for (n_p, b_k), rel in matrix.items():
        if rel is None:
            continue
        cost = system_cost(n_p, b_k)['total']
        all_configs.append((cost, rel, n_p, b_k))

    costs_all = [c[0] for c in all_configs]
    rels_all  = [c[1] for c in all_configs]
    above     = [c[1] >= threshold_pct for c in all_configs]

    ax.scatter(costs_all, rels_all,
               c=['#4CAF50' if a else '#F44336' for a in above],
               s=60, zorder=3, alpha=0.85)
    ax.axhline(threshold_pct, color='navy', linestyle='--', linewidth=1.2,
               label=f'{threshold_pct:.0f}% threshold')
    for cost, rel, n_p, b_k in all_configs:
        ax.annotate(f'{n_p}p/{b_k:.1f}k', (cost, rel),
                    textcoords='offset points', xytext=(4, 2),
                    fontsize=7, color='#444444')
    ax.set_xlabel('Total System Cost (USD)')
    ax.set_ylabel('Reliability (%)')
    ax.set_title('Reliability vs. Cost — all configurations\n'
                 '(green = meets threshold, red = below threshold)')
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(
        lambda v, _: f'${v:,.0f}'))
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(images_dir, 'C4_reliability_vs_cost.png'), dpi=150)
    plt.close(fig)
    print(f'  Saved C4_reliability_vs_cost.png')

    # ── C5: Optimal summary card ─────────────────────────────────────────────
    if candidates:
        opt = candidates[0]
        cb  = opt['cost_breakdown']

        fig, axes = plt.subplots(1, 2, figsize=(11, 5),
                                 gridspec_kw={'width_ratios': [1, 1.4]})

        # Left: text summary
        ax_txt = axes[0]
        ax_txt.axis('off')
        batt_rate_note = (
            '$330/kWh' if opt['battery_kwh'] <= 3.0 else
            '$255/kWh' if opt['battery_kwh'] <= 7.0 else
            '$220/kWh'
        )
        summary_lines = [
            ('OPTIMAL BUILD', 14, 'bold', '#1565C0'),
            ('', 8, 'normal', 'black'),
            (f'{opt["n_panels"]} solar panels × 405 W', 11, 'bold', '#1B5E20'),
            (f'{opt["battery_kwh"]:.1f} kWh LiFePO4 battery', 11, 'bold', '#1B5E20'),
            ('', 6, 'normal', 'black'),
            (f'Reliability: {opt["reliability"]:.1f}%', 13, 'bold',
             '#2E7D32' if opt['reliability'] >= threshold_pct else '#B71C1C'),
            (f'Total cost:  ${opt["total_cost"]:,.0f}', 13, 'bold', '#1565C0'),
            ('', 8, 'normal', 'black'),
            ('── Component costs ──', 9, 'normal', '#555555'),
            (f'  Pump:          ${cb["pump"]:>8,.0f}', 9, 'normal', 'black'),
            (f'  Inverter:      ${cb["inverter"]:>8,.0f}', 9, 'normal', 'black'),
            (f'  Wiring:        ${cb["wiring"]:>8,.0f}', 9, 'normal', 'black'),
            (f'  Misc:          ${cb["misc"]:>8,.0f}', 9, 'normal', 'black'),
            (f'  Extra panels:  ${cb["panels_extra"]:>8,.0f}', 9, 'normal', 'black'),
            (f'  Battery:       ${cb["battery"]:>8,.0f}', 9, 'normal', 'black'),
            (f'  ({batt_rate_note}, tiered LiFePO4)', 8, 'italic', '#555555'),
            (f'  Shipping:      ${cb["shipping"]:>8,.0f}', 9, 'normal', 'black'),
            (f'  ({cb["weight_kg"]:.0f} kg, Class 85 LTL)', 8, 'italic', '#555555'),
        ]
        y = 0.97
        for text, size, weight, colour in summary_lines:
            style = 'italic' if weight == 'italic' else 'normal'
            wt    = 'normal' if weight == 'italic' else weight
            ax_txt.text(0.04, y, text, transform=ax_txt.transAxes,
                        fontsize=size, fontweight=wt, fontstyle=style,
                        color=colour, va='top',
                        family='monospace' if '  ' in text else 'sans-serif')
            y -= 0.048 if size >= 11 else 0.038

        # Right: pie chart of cost breakdown
        ax_pie = axes[1]
        pie_labels  = ['Pump', 'Inverter', 'Wiring', 'Misc',
                       'Extra panels', 'Battery', 'Shipping']
        pie_vals    = [cb['pump'], cb['inverter'], cb['wiring'], cb['misc'],
                       cb['panels_extra'], cb['battery'], cb['shipping']]
        pie_colours = ['#2196F3', '#9C27B0', '#795548', '#9E9E9E',
                       '#F44336', '#00BCD4', '#FFC107']
        filtered = [(l, v, c) for l, v, c in zip(pie_labels, pie_vals, pie_colours)
                    if v > 0]
        pie_labels_f, pie_vals_f, pie_colours_f = zip(*filtered)

        wedges, _, autotexts = ax_pie.pie(
            pie_vals_f, labels=None, colors=pie_colours_f,
            autopct=lambda p: f'{p:.0f}%' if p > 4 else '',
            startangle=90, pctdistance=0.75)
        ax_pie.legend(wedges, pie_labels_f, loc='lower right',
                      fontsize=7, ncol=2, bbox_to_anchor=(1.0, -0.05))
        ax_pie.set_title('Cost Allocation', fontsize=10)

        fig.suptitle('BahaSol — Optimal System Configuration', fontsize=13,
                     fontweight='bold', y=1.01)
        fig.tight_layout()
        fig.savefig(os.path.join(images_dir, 'C5_optimal_summary.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)
        print(f'  Saved C5_optimal_summary.png')


# ===========================================================================
# CSV OUTPUT
# ===========================================================================

def save_results(candidates: list, results_dir: str, threshold_pct: float):
    """Save cost breakdown and full candidate list to CSVs."""
    os.makedirs(results_dir, exist_ok=True)

    if candidates:
        opt    = candidates[0]
        cb     = opt['cost_breakdown']
        fields = ['item', 'cost_usd']
        rows   = [
            ('pump',         cb['pump']),
            ('inverter',     cb['inverter']),
            ('wiring',       cb['wiring']),
            ('misc',         cb['misc']),
            ('extra_panels', cb['panels_extra']),
            ('battery',      cb['battery']),
            ('shipping',     cb['shipping']),
            ('TOTAL',        cb['total']),
        ]
        path = os.path.join(results_dir, 'cost_breakdown_optimal.csv')
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(fields)
            w.writerows(rows)
        print(f'  Saved cost_breakdown_optimal.csv')

    all_path = os.path.join(results_dir,
                            f'all_configs_above_{threshold_pct:.0f}pct.csv')
    fields = ['n_panels', 'battery_kwh', 'reliability_pct', 'total_cost_usd',
              'pump', 'inverter', 'wiring', 'misc',
              'panels_extra', 'battery', 'shipping', 'weight_kg']
    with open(all_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in candidates:
            cb = c['cost_breakdown']
            w.writerow({
                'n_panels'        : c['n_panels'],
                'battery_kwh'     : c['battery_kwh'],
                'reliability_pct' : round(c['reliability'], 2),
                'total_cost_usd'  : round(c['total_cost'], 2),
                'pump'            : cb['pump'],
                'inverter'        : cb['inverter'],
                'wiring'          : cb['wiring'],
                'misc'            : cb['misc'],
                'panels_extra'    : cb['panels_extra'],
                'battery'         : cb['battery'],
                'shipping'        : cb['shipping'],
                'weight_kg'       : round(cb['weight_kg'], 1),
            })
    print(f'  Saved {os.path.basename(all_path)}')


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description='BahaSol cost analysis — find minimum-cost system '
                    'meeting a reliability threshold.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Examples:\n'
            '  python cost_analysis.py                              '
            '# 95% threshold, auto-load sweep CSV\n'
            '  python cost_analysis.py --reliability 90\n'
            '  python cost_analysis.py --panels 10 12 15 18 20 '
            '--battery 1 2 3 4 5\n'
            '  python cost_analysis.py --show-all\n'
        ),
    )
    parser.add_argument('--reliability', type=float, default=95.0,
                        help='Minimum reliability threshold in percent (default 95).')
    parser.add_argument('--matrix-csv', default=None,
                        help='Path to reliability_matrix.csv from run_simulation.py sweep.')
    parser.add_argument('--panels', type=int, nargs='+', default=None,
                        help='Panel counts to sweep (when no matrix CSV exists).')
    parser.add_argument('--battery', type=float, nargs='+', default=None,
                        help='Battery capacities [kWh] to sweep.')
    parser.add_argument('--show-all', action='store_true',
                        help='Print all configurations above threshold, not just top 5.')
    parser.add_argument('--no-plots', action='store_true',
                        help='Skip plot generation.')
    args = parser.parse_args()

    threshold = args.reliability

    # ── Load or build reliability matrix ─────────────────────────────────────
    matrix_path = args.matrix_csv or _DEFAULT_MATRIX
    if os.path.exists(matrix_path):
        print(f'  Loading reliability matrix: {matrix_path}')
        matrix = load_reliability_matrix(matrix_path)
    else:
        panels_sweep  = args.panels  or DEFAULT_PANELS_SWEEP
        battery_sweep = args.battery or DEFAULT_BATTERY_SWEEP
        print(f'  NOTE: No reliability matrix CSV found at:')
        print(f'        {matrix_path}')
        print(f'  Using synthetic reliability estimates for cost structure demo.')
        print(f'  Run: python ../6-run-sims/run_simulation.py --sweep-panels '
              f'{" ".join(str(p) for p in panels_sweep)} '
              f'--sweep-battery {" ".join(str(b) for b in battery_sweep)}')
        print()
        matrix = build_synthetic_matrix(panels_sweep, battery_sweep)

    if args.panels or args.battery:
        ps = args.panels  or sorted(set(n for n, _ in matrix))
        bs = args.battery or sorted(set(b for _, b in matrix))
        matrix = {k: v for k, v in matrix.items() if k[0] in ps and k[1] in bs}

    panels_list  = sorted(set(n for n, _ in matrix))
    battery_list = sorted(set(b for _, b in matrix))

    print(f'  Configurations in matrix: {len(matrix)}')
    print(f'  Panels: {panels_list}')
    print(f'  Battery: {battery_list} kWh')

    # ── Optimise ─────────────────────────────────────────────────────────────
    candidates = find_optimal(matrix, threshold)
    print_report(candidates, threshold, show_all=args.show_all)

    # ── Save outputs ──────────────────────────────────────────────────────────
    print(f'\n  Writing results …')
    save_results(candidates, _RESULTS_DIR, threshold)

    if not args.no_plots:
        print(f'  Generating plots …')
        make_plots(matrix, candidates, threshold,
                   panels_list, battery_list, _IMAGES_DIR)

    if candidates:
        print(f'\n  Done.  Optimal: {candidates[0]["n_panels"]} panels, '
              f'{candidates[0]["battery_kwh"]:.1f} kWh battery — '
              f'${candidates[0]["total_cost"]:,.0f} total.')


if __name__ == '__main__':
    main()
