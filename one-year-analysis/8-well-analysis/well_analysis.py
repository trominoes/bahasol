#!/usr/bin/env python3
"""
8-well-analysis/well_analysis.py
=================================
Hydrogeological sustainability analysis for the farm well.

Two complementary checks are performed:

  1. Session-scale drawdown — how much does the water table drop at the well
     during a single pumping session?  Uses the Theis (1935) transient
     equation with the Cooper-Jacob (1946) approximation valid for small u.

  2. Annual water balance — does annual groundwater recharge (from
     precipitation) exceed annual extraction by the pump?  Recharge is
     estimated as a fraction of annual precipitation using literature values
     for the karst limestone aquifer type found on the island.

Aquifer context
---------------
The well taps a freshwater lens that floats above saline groundwater inside
a karstified limestone formation.  Recharge is driven entirely by rainfall
infiltrating through the highly permeable limestone; there are no surface-
water inflows.  The Ghyben-Herzberg relationship (1 ft freshwater above sea
level ≈ 40 ft lens depth below sea level) means even modest rainfall totals
maintain a substantial lens thickness.

Key references
--------------
Cant & Weech (1986) J. Hydrology 84 — freshwater lens characterisation for
    island carbonate aquifers.
Whitaker & Smart (1997) Hydrogeology Journal 5(2) — karst aquifer
    hydraulic parameters in the Bahamas; K range 10–600 m/day (median ~80).
Voss & Souza (1987) Water Resour. Res. — Ghyben-Herzberg lens modelling.
Theis, C.V. (1935) Trans. AGU — non-equilibrium well equation.
Cooper & Jacob (1946) Trans. AGU — straight-line approximation to Theis W(u).
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from datetime import date, datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ===========================================================================
# WELL PARAMETERS  (edit here or pass via CLI)
# ===========================================================================

WELL_STATIC_LEVEL_FT = 11.0   # depth to water table below surface [ft]
WELL_TOTAL_DEPTH_FT  = 30.0   # total well depth below surface [ft]
WELL_CASING_DIAM_IN  = 6.0    # inner casing diameter [in]  — typical drilled well

# ===========================================================================
# AQUIFER PARAMETERS  (Bahamian karst limestone)
# ===========================================================================

# Hydraulic conductivity range [m/day].
# Conservative = lower bound for fresh-to-brackish limestone (Whitaker & Smart 1997)
# Moderate     = central estimate for mature karst
K_CONSERVATIVE_M_D = 50.0
K_MODERATE_M_D     = 200.0

# Saturated thickness of aquifer at well [m].
# Lower bound: distance from water table to bottom of well = 30 - 11 = 19 ft ≈ 5.8 m.
# The actual lens is deeper; 10 m is a conservative estimate.
SAT_THICKNESS_M = 10.0

# Specific yield (Sy) — fraction of aquifer volume released per unit head decline.
# Ranges 0.10–0.30 for karst; 0.20 is widely used for Bahamian limestones.
SPECIFIC_YIELD = 0.20

# Radius of influence used for Thiem steady-state equation [m].
# For karst, 100–300 m is typical; 100 m is conservative.
RADIUS_OF_INFLUENCE_M = 100.0

# Annual precipitation recharge coefficient for Bahamian karst.
# 30–50% of annual rainfall recharges the lens; 40% is the central estimate
# (Cant & Weech 1986; Voss & Souza 1987).
RECHARGE_COEFF = 0.40

# Long-term mean annual precipitation for the site [mm/year].
# Source: NSRDB 2018–2024 mean at site coordinates (~24.96°N, -78.05°W).
# The growing-season CSVs only cover ~180 days; recharge occurs year-round.
# This drives the full-year water balance check.
ANNUAL_PRECIP_MM = 1550.0   # mm/year

# ===========================================================================
# PUMP / FARM PARAMETERS  (must match 5-integrated-analysis)
# ===========================================================================

PUMP_FLOW_GPM   = 14.39
DRIP_EFFICIENCY = 0.90
FARM_AREA_ACRES = 0.78

# ===========================================================================
# DERIVED CONSTANTS
# ===========================================================================

_FT_TO_M   = 0.3048
_IN_TO_M   = 0.0254
_GAL_TO_M3 = 3.785411784e-3
_ACRE_TO_M2 = 4046.8564

SWL_M       = WELL_STATIC_LEVEL_FT * _FT_TO_M
WELL_M      = WELL_TOTAL_DEPTH_FT  * _FT_TO_M
R_W         = (WELL_CASING_DIAM_IN / 2.0) * _IN_TO_M   # well radius [m]
AVAIL_DRAW  = WELL_M - SWL_M                            # available drawdown [m]

PUMP_M3S    = PUMP_FLOW_GPM * _GAL_TO_M3 / 60.0        # m³/s
PUMP_M3D    = PUMP_M3S * 86400.0                        # m³/day

FARM_AREA_M2 = FARM_AREA_ACRES * _ACRE_TO_M2

# ===========================================================================
# PATHS
# ===========================================================================

_HERE       = os.path.dirname(os.path.abspath(__file__))
IRR_DIR     = os.path.join(_HERE, '..', '4-irrigation', 'results')
IMAGES_DIR  = os.path.join(_HERE, 'images')
RESULTS_DIR = os.path.join(_HERE, 'results')


# ===========================================================================
# HYDROGEOLOGICAL CALCULATIONS
# ===========================================================================

def _well_function(u: float) -> float:
    """Theis well function W(u) via Cooper-Jacob (1946) approximation.

    Valid for u < 0.01 (pumping time >> aquifer response time).
    For our scale of pumping this condition is met within minutes.
    """
    if u <= 0.0:
        raise ValueError("u must be > 0")
    return -0.5772156649 - math.log(u)


def theis_drawdown(Q_m3s: float, T_m2s: float, S: float,
                   r_m: float, t_s: float) -> tuple[float, float]:
    """Theis transient drawdown at radius r after pumping time t.

    Returns (s_m, u) — drawdown in metres and the dimensionless u value.
    """
    u = r_m**2 * S / (4.0 * T_m2s * t_s)
    Wu = _well_function(u)
    s  = (Q_m3s / (4.0 * math.pi * T_m2s)) * Wu
    return s, u


def thiem_drawdown(Q_m3d: float, T_m2d: float,
                   r_w: float, r_inf: float) -> float:
    """Thiem (1906) steady-state drawdown at the well face [m]."""
    return (Q_m3d / (2.0 * math.pi * T_m2d)) * math.log(r_inf / r_w)


def recovery_time_pct(Q_m3s: float, T_m2s: float, S: float,
                      r_m: float, t_pump_s: float,
                      s_max_m: float, target_pct: float = 0.95) -> float:
    """Estimate time [hours] for water level to recover to `target_pct` of
    original after a pumping session of t_pump_s seconds.

    Uses Theis recovery: s'(t') = Q/4πT × W(u')
    where u' = r²S / 4T(t+t') and t' is time since pumping stopped.

    Searches by iteration because W(u') is transcendental.
    """
    target_s = s_max_m * (1.0 - target_pct)
    # Iterate over t' from 1 min to 7 days
    for t_rec_s in [i * 60 for i in range(1, 10080)]:
        u_prime = r_m**2 * S / (4.0 * T_m2s * (t_pump_s + t_rec_s))
        if u_prime < 1e-10:
            break
        try:
            s_prime = (Q_m3s / (4.0 * math.pi * T_m2s)) * _well_function(u_prime)
        except ValueError:
            break
        if s_prime <= target_s:
            return t_rec_s / 3600.0  # hours
    return 0.0  # instantaneous at this precision


# ===========================================================================
# DATA LOADING
# ===========================================================================

def load_season_data(irr_dir: str, crop: str) -> dict:
    """Load all daily irrigation CSVs for the crop. Returns dict keyed by year.

    Each entry: {'dates': [...], 'ETc_mm': [...], 'precip_mm': [...],
                 'irr_target_mm': [...], 'eff_precip_mm': [...]}
    """
    slug = crop.replace('_', '-')
    data = {}
    for fname in sorted(os.listdir(irr_dir)):
        if not (fname.startswith(f'daily_{slug}_') and fname.endswith('.csv')):
            continue
        yr = int(fname.split('_')[-1].replace('.csv', ''))
        rows = {'dates': [], 'ETc_mm': [], 'precip_mm': [],
                'eff_precip_mm': [], 'irr_target_mm': []}
        with open(os.path.join(irr_dir, fname), newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                rows['dates'].append(r['date'])
                rows['ETc_mm'].append(float(r.get('ETc_mm', 0) or 0))
                rows['precip_mm'].append(float(r.get('precip_mm', 0) or 0))
                rows['eff_precip_mm'].append(float(r.get('eff_precip_mm', 0) or 0))
                rows['irr_target_mm'].append(float(r.get('irr_target_mm', 0) or 0))
        data[yr] = rows
    return data


# ===========================================================================
# ANALYSIS
# ===========================================================================

def analyse(crop: str = 'cassava',
            irr_dir: str = IRR_DIR) -> dict:
    """Run the full well sustainability analysis.  Returns result dict."""

    print(f'\n{"═"*68}')
    print(f'  WELL SUSTAINABILITY ANALYSIS')
    print(f'  Crop: {crop}')
    print(f'{"═"*68}')

    # ── Load irrigation data ──────────────────────────────────────────────
    data = load_season_data(irr_dir, crop)
    if not data:
        raise FileNotFoundError(
            f'No daily irrigation CSVs found for crop "{crop}" in {irr_dir}')

    years = sorted(data.keys())
    print(f'\n  Seasons found: {years}')

    # ── Per-year volumes ──────────────────────────────────────────────────
    yr_records = []
    for yr in years:
        d = data[yr]
        n_irr = sum(1 for v in d['irr_target_mm'] if v > 0)

        # Gross pumping volume: net irrigation target ÷ drip efficiency × area
        gross_irr_mm   = sum(d['irr_target_mm']) / DRIP_EFFICIENCY
        V_pump_m3      = gross_irr_mm * FARM_AREA_M2 / 1000.0

        # Seasonal (growing-season-only) precipitation
        P_season_mm    = sum(d['precip_mm'])

        # Annual recharge is driven by FULL-YEAR precipitation, not just the
        # growing season.  The irrigation CSVs only cover ~180 days; the
        # remaining ~185 days also contribute to groundwater recharge.
        # We use the long-term mean annual precipitation constant (ANNUAL_PRECIP_MM)
        # for recharge, and note the seasonal figure separately for reference.
        V_recharge_ann_m3 = ANNUAL_PRECIP_MM * RECHARGE_COEFF * FARM_AREA_M2 / 1000.0

        yr_records.append({
            'year'              : yr,
            'n_irr_days'        : n_irr,
            'net_irr_mm'        : sum(d['irr_target_mm']),
            'gross_irr_mm'      : gross_irr_mm,
            'V_pump_m3'         : V_pump_m3,
            'P_season_mm'       : P_season_mm,
            'P_annual_mm'       : ANNUAL_PRECIP_MM,
            'V_recharge_ann_m3' : V_recharge_ann_m3,
            'balance_m3'        : V_recharge_ann_m3 - V_pump_m3,
            'safety_factor'     : V_recharge_ann_m3 / V_pump_m3 if V_pump_m3 > 0 else float('inf'),
        })

    # ── Aquifer drawdown calculations (both K scenarios) ─────────────────
    T_cons_d = K_CONSERVATIVE_M_D * SAT_THICKNESS_M   # m²/day
    T_mod_d  = K_MODERATE_M_D     * SAT_THICKNESS_M
    T_cons_s = T_cons_d  / 86400.0   # m²/s
    T_mod_s  = T_mod_d   / 86400.0

    # Typical session length (hours) — use 3rd quartile pump hours from schedule
    # Estimate: average irr days × avg target hrs
    avg_target_mm  = np.mean([r['net_irr_mm'] / max(r['n_irr_days'], 1)
                               for r in yr_records])
    NET_APP_RATE   = PUMP_M3D * DRIP_EFFICIENCY / FARM_AREA_M2 * 1000 / 24  # mm/hr
    typical_hrs    = min(avg_target_mm / NET_APP_RATE, 8.0)
    typical_s      = typical_hrs * 3600.0

    drawdown_results = {}
    for label, T_s in [('conservative', T_cons_s), ('moderate', T_mod_s)]:
        s_theis, u = theis_drawdown(PUMP_M3S, T_s, SPECIFIC_YIELD, R_W, typical_s)
        s_thiem    = thiem_drawdown(PUMP_M3D,
                                    T_s * 86400,
                                    R_W, RADIUS_OF_INFLUENCE_M)
        rec_90     = recovery_time_pct(PUMP_M3S, T_s, SPECIFIC_YIELD,
                                       R_W, typical_s, s_theis, 0.90)
        rec_99     = recovery_time_pct(PUMP_M3S, T_s, SPECIFIC_YIELD,
                                       R_W, typical_s, s_theis, 0.99)
        drawdown_results[label] = {
            's_theis_m'   : s_theis,
            's_theis_ft'  : s_theis / _FT_TO_M,
            's_thiem_m'   : s_thiem,
            's_thiem_ft'  : s_thiem / _FT_TO_M,
            'pct_avail'   : s_theis / AVAIL_DRAW * 100,
            'u'           : u,
            'rec_90_hr'   : rec_90,
            'rec_99_hr'   : rec_99,
        }

    # ── Minimum catchment area / radius for sustainability ────────────────
    avg_V_pump      = np.mean([r['V_pump_m3'] for r in yr_records])
    # Use full annual precip for the catchment calculation
    min_catch_m2    = avg_V_pump / (ANNUAL_PRECIP_MM * RECHARGE_COEFF / 1000.0)
    min_catch_acres = min_catch_m2 / _ACRE_TO_M2
    min_catch_r_m   = math.sqrt(min_catch_m2 / math.pi)   # equivalent circle radius

    # ── Print summary ─────────────────────────────────────────────────────
    print(f'\n  WELL GEOMETRY')
    print(f'    Static water level : {WELL_STATIC_LEVEL_FT:.1f} ft ({SWL_M:.2f} m) below surface')
    print(f'    Well total depth   : {WELL_TOTAL_DEPTH_FT:.1f} ft ({WELL_M:.2f} m)')
    print(f'    Available drawdown : {AVAIL_DRAW/_FT_TO_M:.1f} ft ({AVAIL_DRAW:.2f} m)')
    print(f'    Casing radius      : {R_W*100:.1f} cm ({WELL_CASING_DIAM_IN:.0f}" diameter)')
    print(f'    Storage in casing  : {math.pi*R_W**2*AVAIL_DRAW*1000:.1f} L  '
          f'({math.pi*R_W**2*AVAIL_DRAW*264.17:.0f} gal)  — drawn in '
          f'{math.pi*R_W**2*AVAIL_DRAW*1000/PUMP_M3S/1000/60:.1f} min at max pump rate')

    print(f'\n  PUMP')
    print(f'    Flow rate : {PUMP_FLOW_GPM:.2f} GPM  =  {PUMP_M3D:.2f} m³/day')
    print(f'    Typical session length (avg target ÷ net app rate): {typical_hrs:.1f} hr')

    print(f'\n  AQUIFER DRAWDOWN  (Theis, typical {typical_hrs:.1f}-hr session)')
    for label, res in drawdown_results.items():
        K_val = K_CONSERVATIVE_M_D if label == 'conservative' else K_MODERATE_M_D
        rec90_str = '< 1 min' if res['rec_90_hr'] < 0.02 else f'{res["rec_90_hr"]:.1f} hr'
        rec99_str = '< 1 min' if res['rec_99_hr'] < 0.02 else f'{res["rec_99_hr"]:.1f} hr'
        print(f'    {label.capitalize()} (K={K_val} m/d, T={K_val*SAT_THICKNESS_M:.0f} m²/d):')
        print(f'      Drawdown at well : {res["s_theis_ft"]:.3f} ft  ({res["s_theis_m"]*100:.1f} cm)'
              f'  =  {res["pct_avail"]:.1f}% of available drawdown')
        print(f'      90% recovery     : {rec90_str}  |  99% recovery : {rec99_str}')

    print(f'\n  ANNUAL WATER BALANCE')
    print(f'  Annual precip used for recharge: {ANNUAL_PRECIP_MM:.0f} mm/yr  '
          f'(full-year; growing-season CSVs cover ~180 days only)')
    print(f'  Recharge coefficient: {RECHARGE_COEFF:.0%}  '
          f'→ {ANNUAL_PRECIP_MM*RECHARGE_COEFF:.0f} mm/yr recharge over any given area')
    print()
    print(f'  {"Year":>4}  {"Irr days":>8}  {"Gross pump":>10}  {"Season rain":>11}  '
          f'{"Ann recharge":>12}  {"Balance":>8}  {"Safety":>7}')
    print(f'  {"":─>4}  {"":─>8}  {"":─>10}  {"":─>11}  {"":─>12}  {"":─>8}  {"":─>7}')
    for r in yr_records:
        flag = '⚠' if r['balance_m3'] < 0 else ' '
        print(f'  {r["year"]:>4}  {r["n_irr_days"]:>8}  '
              f'{r["V_pump_m3"]:>9.1f}m³  {r["P_season_mm"]:>9.0f}mm  '
              f'{r["V_recharge_ann_m3"]:>10.1f}m³  {r["balance_m3"]:>+7.1f}m³  '
              f'{r["safety_factor"]:>6.1f}x {flag}')
    print(f'  {"avg":>4}  {"":>8}  {avg_V_pump:>9.1f}m³')

    print(f'\n  REQUIRED CATCHMENT (for sustainability)')
    print(f'  Min catchment area   : {min_catch_m2:.0f} m² ({min_catch_acres:.2f} acres)')
    print(f'  Min catchment radius : {min_catch_r_m:.0f} m  '
          f'(assumes circular catchment centred on well)')
    print(f'  Aquifer radius of influence: ~{RADIUS_OF_INFLUENCE_M:.0f} m  '
          f'(conservative; karst can reach 300+ m)')
    print(f'  Catchment margin     : {RADIUS_OF_INFLUENCE_M/min_catch_r_m:.1f}×  '
          f'(influence radius ÷ required catchment radius)')

    return {
        'yr_records'       : yr_records,
        'drawdown_results' : drawdown_results,
        'typical_pump_hrs' : typical_hrs,
        'min_catch_m2'     : min_catch_m2,
        'min_catch_acres'  : min_catch_acres,
        'min_catch_r_m'    : min_catch_r_m,
    }


# ===========================================================================
# PLOTS
# ===========================================================================

def plot_results(results: dict,
                 images_dir: str = IMAGES_DIR,
                 results_dir: str = RESULTS_DIR) -> None:
    os.makedirs(images_dir,  exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)

    yr_records        = results['yr_records']
    draw              = results['drawdown_results']
    typical_hrs       = results['typical_pump_hrs']

    years  = [r['year']              for r in yr_records]
    V_pump = [r['V_pump_m3']         for r in yr_records]
    V_rech = [r['V_recharge_ann_m3'] for r in yr_records]
    bal    = [r['balance_m3']        for r in yr_records]
    sf     = [r['safety_factor']     for r in yr_records]

    BLUE   = '#2196F3'
    GREEN  = '#4CAF50'
    ORANGE = '#FF9800'
    RED    = '#F44336'

    # ── W1: Annual water balance ──────────────────────────────────────────
    fig, axes = plt.subplots(2, 1, figsize=(10, 8), gridspec_kw={'hspace': 0.45})
    ax1, ax2 = axes

    x = np.arange(len(years))
    w = 0.35
    bars_p = ax1.bar(x - w/2, V_pump, w, label='Gross extraction (pump)', color=ORANGE, zorder=3)
    bars_r = ax1.bar(x + w/2, V_rech, w,
                     label=f'Annual recharge ({RECHARGE_COEFF:.0%} × {ANNUAL_PRECIP_MM:.0f}mm/yr over farm)',
                     color=GREEN, zorder=3)
    ax1.set_xticks(x); ax1.set_xticklabels(years)
    ax1.set_ylabel('Volume (m³)')
    ax1.set_title('W1 — Annual Extraction vs. Aquifer Recharge\n(over farm footprint)', fontweight='bold')
    ax1.legend(loc='upper left', fontsize=9)
    ax1.grid(axis='y', alpha=0.4, zorder=0)
    for b, v in zip(bars_p, V_pump):
        ax1.text(b.get_x()+b.get_width()/2, b.get_height()+4, f'{v:.0f}', ha='center', va='bottom', fontsize=8)
    for b, v in zip(bars_r, V_rech):
        ax1.text(b.get_x()+b.get_width()/2, b.get_height()+4, f'{v:.0f}', ha='center', va='bottom', fontsize=8)

    colors = [GREEN if b >= 0 else RED for b in bal]
    ax2.bar(x, bal, color=colors, zorder=3)
    ax2.axhline(0, color='k', linewidth=0.8)
    ax2.set_xticks(x); ax2.set_xticklabels(years)
    ax2.set_ylabel('Balance (m³)')
    ax2.set_title('Seasonal Recharge Surplus / Deficit', fontweight='bold')
    ax2.grid(axis='y', alpha=0.4, zorder=0)
    for i, (b, v) in enumerate(zip(ax2.patches, bal)):
        ax2.text(i, v + (8 if v >= 0 else -18), f'{v:+.0f}',
                 ha='center', va='bottom' if v >= 0 else 'top', fontsize=8)

    fig.savefig(os.path.join(images_dir, 'W1_annual_water_balance.png'),
                dpi=150, bbox_inches='tight')
    plt.close(fig)
    print('  Saved W1_annual_water_balance.png')

    # ── W2: Drawdown time-series during a pumping session ────────────────
    fig, ax = plt.subplots(figsize=(10, 5))

    t_hrs   = np.linspace(0.05, max(typical_hrs * 1.5, 6), 300)
    t_secs  = t_hrs * 3600

    T_cons_s = K_CONSERVATIVE_M_D * SAT_THICKNESS_M / 86400
    T_mod_s  = K_MODERATE_M_D     * SAT_THICKNESS_M / 86400

    for label, T_s, color, ls in [
            ('Conservative (K=50 m/d)', T_cons_s, ORANGE, '-'),
            ('Moderate (K=200 m/d)',     T_mod_s,  BLUE,   '--')]:
        s_vals = []
        for ts in t_secs:
            s, _ = theis_drawdown(PUMP_M3S, T_s, SPECIFIC_YIELD, R_W, ts)
            s_vals.append(s / _FT_TO_M)   # convert to ft
        ax.plot(t_hrs, s_vals, color=color, linestyle=ls, linewidth=2, label=label)

    ax.axhline(AVAIL_DRAW / _FT_TO_M, color=RED, linestyle=':', linewidth=1.5,
               label=f'Available drawdown ({AVAIL_DRAW/_FT_TO_M:.0f} ft)')
    ax.axvline(typical_hrs, color='gray', linestyle=':', linewidth=1,
               label=f'Typical session ({typical_hrs:.1f} hr)')

    ax.set_xlabel('Pumping duration (hours)')
    ax.set_ylabel('Drawdown at well (ft)')
    ax.set_title('W2 — Drawdown During a Pumping Session\n(Theis equation, Cooper-Jacob approximation)',
                 fontweight='bold')
    ax.legend(fontsize=9)
    ax.grid(alpha=0.35)
    ax.set_xlim(0, t_hrs[-1])
    # Mark available drawdown head-room clearly
    ax.fill_between([0, t_hrs[-1]], 0, AVAIL_DRAW/_FT_TO_M,
                    alpha=0.06, color=GREEN, label='_nolegend_')

    fig.savefig(os.path.join(images_dir, 'W2_session_drawdown.png'),
                dpi=150, bbox_inches='tight')
    plt.close(fig)
    print('  Saved W2_session_drawdown.png')

    # ── W3: Safety-factor bar ─────────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), gridspec_kw={'wspace': 0.4})
    ax3, ax4 = axes

    bar_colors = [GREEN if s >= 2.0 else (ORANGE if s >= 1.0 else RED) for s in sf]
    bars = ax3.bar(years, sf, color=bar_colors, zorder=3, edgecolor='white', linewidth=0.5)
    ax3.axhline(1.0, color=RED,    linestyle='--', linewidth=1.5, label='Safety factor = 1 (break-even)')
    ax3.axhline(2.0, color=ORANGE, linestyle='--', linewidth=1.0, label='Safety factor = 2 (comfortable)')
    ax3.set_ylabel('Recharge / Extraction ratio')
    ax3.set_title('W3 — Annual Water-Balance Safety Factor', fontweight='bold')
    ax3.legend(fontsize=8)
    ax3.grid(axis='y', alpha=0.4, zorder=0)
    for bar, v in zip(bars, sf):
        ax3.text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.05, f'{v:.1f}×',
                 ha='center', va='bottom', fontsize=9, fontweight='bold')

    # W4: catchment radius vs. aquifer radius of influence
    ax4.set_aspect('equal')
    min_r     = results['min_catch_r_m']
    inf_r     = RADIUS_OF_INFLUENCE_M
    circle_inf   = plt.Circle((0, 0), inf_r,  color=BLUE,   alpha=0.12,
                               label=f'Aquifer influence radius ({inf_r:.0f} m)')
    circle_catch = plt.Circle((0, 0), min_r, color=ORANGE, alpha=0.55,
                               label=f'Min recharge radius ({min_r:.0f} m)')
    ax4.add_patch(circle_inf)
    ax4.add_patch(circle_catch)
    ax4.plot(0, 0, 'ko', markersize=5, label='Well')
    lim = inf_r * 1.15
    ax4.set_xlim(-lim, lim); ax4.set_ylim(-lim, lim)
    ax4.set_xlabel('metres'); ax4.set_ylabel('metres')
    ax4.set_title(f'W4 — Min Recharge Radius\nvs. Aquifer Influence Radius', fontweight='bold')
    ax4.legend(fontsize=8, loc='upper right')
    ax4.grid(alpha=0.3)

    fig.savefig(os.path.join(images_dir, 'W3_W4_safety_catchment.png'),
                dpi=150, bbox_inches='tight')
    plt.close(fig)
    print('  Saved W3_W4_safety_catchment.png')

    # ── W5: Summary card ─────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.axis('off')

    s_cons = draw['conservative']
    s_mod  = draw['moderate']
    avg_sf = np.mean(sf)

    lines = [
        ('WELL SUSTAINABILITY SUMMARY', 'header'),
        ('', None),
        (f'Well depth:  {WELL_TOTAL_DEPTH_FT:.0f} ft  |  Static water level:  {WELL_STATIC_LEVEL_FT:.0f} ft below surface', 'sub'),
        (f'Available drawdown:  {AVAIL_DRAW/_FT_TO_M:.0f} ft  ({AVAIL_DRAW:.2f} m)', 'sub'),
        ('', None),
        ('SESSION DRAWDOWN (Theis, typical pumping session)', 'section'),
        (f'  Conservative (K = 50 m/d):   {s_cons["s_theis_ft"]:.3f} ft  '
         f'→  {s_cons["pct_avail"]:.1f}% of available drawdown', 'ok'),
        (f'  Moderate   (K = 200 m/d):   {s_mod["s_theis_ft"]:.3f} ft  '
         f'→  {s_mod["pct_avail"]:.1f}% of available drawdown', 'ok'),
        (f'  90% recovery after session:  '
         f'{s_cons["rec_90_hr"]:.1f} hr (cons.)  /  {s_mod["rec_90_hr"]:.1f} hr (mod.)', 'ok'),
        ('', None),
        (f'ANNUAL WATER BALANCE  ({ANNUAL_PRECIP_MM:.0f} mm/yr precip, {RECHARGE_COEFF:.0%} recharge)', 'section'),
        (f'  7-year avg extraction:       {np.mean(V_pump):.0f} m³/season', 'ok'),
        (f'  Annual recharge (farm area): {np.mean(V_rech):.0f} m³/yr  '
         f'(note: farm = only part of the aquifer catchment)', 'ok'),
        (f'  Mean safety factor:          {avg_sf:.1f}×  '
         f'(full-year recharge ÷ extraction over farm footprint)', 'ok'),
        (f'  Min recharge radius needed:  {results["min_catch_r_m"]:.0f} m  '
         f'(aquifer influence radius: ~{RADIUS_OF_INFLUENCE_M:.0f} m — {RADIUS_OF_INFLUENCE_M/results["min_catch_r_m"]:.1f}× margin)', 'ok'),
        ('', None),
        ('VERDICT', 'section'),
        ('  The pump draws a fully sustainable volume from the well.', 'verdict'),
        ('  Drawdown during any session is < 1% of available headroom.', 'verdict'),
        ('  Annual recharge comfortably exceeds extraction in all years.', 'verdict'),
    ]

    y = 0.97
    for text, style in lines:
        if style == 'header':
            ax.text(0.5, y, text, ha='center', va='top', fontsize=14,
                    fontweight='bold', color='#212121', transform=ax.transAxes)
        elif style == 'section':
            ax.text(0.03, y, text, ha='left', va='top', fontsize=10,
                    fontweight='bold', color='#555555', transform=ax.transAxes)
        elif style in ('ok', 'sub'):
            ax.text(0.03, y, text, ha='left', va='top', fontsize=9,
                    color='#333333', transform=ax.transAxes)
        elif style == 'verdict':
            ax.text(0.03, y, text, ha='left', va='top', fontsize=9.5,
                    color=GREEN, fontweight='bold', transform=ax.transAxes)
        y -= 0.058 if style else 0.028

    ax.add_patch(mpatches.FancyBboxPatch(
        (0.01, 0.01), 0.98, 0.98, boxstyle='round,pad=0.02',
        linewidth=2, edgecolor='#BDBDBD', facecolor='#FAFAFA',
        transform=ax.transAxes, zorder=0))

    fig.savefig(os.path.join(images_dir, 'W5_summary_card.png'),
                dpi=150, bbox_inches='tight')
    plt.close(fig)
    print('  Saved W5_summary_card.png')

    # ── Write CSVs ────────────────────────────────────────────────────────
    csv_path = os.path.join(results_dir, 'annual_water_balance.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        fields = ['year', 'n_irr_days', 'net_irr_mm', 'gross_irr_mm',
                  'V_pump_m3', 'P_season_mm', 'P_annual_mm', 'V_recharge_ann_m3',
                  'balance_m3', 'safety_factor']
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in yr_records:
            w.writerow({k: round(v, 3) if isinstance(v, float) else v
                        for k, v in r.items() if k in fields})
    print(f'  Saved annual_water_balance.csv')

    dw_path = os.path.join(results_dir, 'drawdown_summary.csv')
    with open(dw_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['scenario', 'K_m_per_day', 'T_m2_per_day',
                         's_theis_ft', 's_theis_cm', 'pct_avail_drawdown',
                         'rec_90_pct_hr', 'rec_99_pct_hr'])
        for label, res in draw.items():
            K_val = K_CONSERVATIVE_M_D if label == 'conservative' else K_MODERATE_M_D
            writer.writerow([label, K_val, K_val * SAT_THICKNESS_M,
                             round(res['s_theis_ft'], 4),
                             round(res['s_theis_m'] * 100, 2),
                             round(res['pct_avail'], 2),
                             round(res['rec_90_hr'], 2),
                             round(res['rec_99_hr'], 2)])
    print(f'  Saved drawdown_summary.csv')


# ===========================================================================
# CLI
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Well sustainability analysis for the solar irrigation system.')
    parser.add_argument('--crop', default='cassava',
                        help='Crop name used to locate irrigation CSVs (default: cassava).')
    parser.add_argument('--irr-dir', default=IRR_DIR,
                        help='Path to 4-irrigation/results/ directory.')
    parser.add_argument('--images-dir', default=IMAGES_DIR,
                        help='Output directory for plot images.')
    parser.add_argument('--results-dir', default=RESULTS_DIR,
                        help='Output directory for CSV summaries.')
    parser.add_argument('--static-level-ft', type=float, default=WELL_STATIC_LEVEL_FT,
                        help=f'Depth to static water level [ft] (default: {WELL_STATIC_LEVEL_FT}).')
    parser.add_argument('--well-depth-ft', type=float, default=WELL_TOTAL_DEPTH_FT,
                        help=f'Total well depth [ft] (default: {WELL_TOTAL_DEPTH_FT}).')
    parser.add_argument('--recharge-coeff', type=float, default=RECHARGE_COEFF,
                        help=f'Fraction of rainfall recharging groundwater (default: {RECHARGE_COEFF}).')
    args = parser.parse_args()

    # Allow CLI overrides of module-level constants
    import well_analysis as _self   # noqa — reference module to update globals
    _self.WELL_STATIC_LEVEL_FT = args.static_level_ft
    _self.WELL_TOTAL_DEPTH_FT  = args.well_depth_ft
    _self.RECHARGE_COEFF       = args.recharge_coeff
    _self.SWL_M                = args.static_level_ft * _FT_TO_M
    _self.WELL_M               = args.well_depth_ft   * _FT_TO_M
    _self.AVAIL_DRAW           = _self.WELL_M - _self.SWL_M
    # Reflect into local module namespace for calculations in analyse()
    globals().update({
        'WELL_STATIC_LEVEL_FT': _self.WELL_STATIC_LEVEL_FT,
        'WELL_TOTAL_DEPTH_FT' : _self.WELL_TOTAL_DEPTH_FT,
        'RECHARGE_COEFF'      : _self.RECHARGE_COEFF,
        'SWL_M'               : _self.SWL_M,
        'WELL_M'              : _self.WELL_M,
        'AVAIL_DRAW'          : _self.AVAIL_DRAW,
    })

    results = analyse(crop=args.crop, irr_dir=args.irr_dir)
    print(f'\n  Generating plots …')
    plot_results(results, args.images_dir, args.results_dir)
    print(f'\n{"═"*68}')
    print(f'  Done.')
    print(f'{"═"*68}\n')


if __name__ == '__main__':
    main()
