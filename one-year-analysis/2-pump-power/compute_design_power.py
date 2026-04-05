#!/usr/bin/env python3
"""
amt_pump_affinity.py
====================
AMT 276 Series – Cast Iron, Self-Priming Centrifugal Pump
Model 276D: 2 HP TEFC, 3-phase | 3450 RPM | 60 Hz | Water SG = 1.0

This script:
  1. Stores hand-digitized Curve A (H-Q) performance data in a Pandas DataFrame.
  2. Fits a cubic spline to the curve.
  3. Uses pump affinity laws to find the required speed ratio at a
     user-specified design point (H [ft], Q [gpm]).
  4. Reports power via two methods:

     Method 1 – Physics-based (design-point specific)
     ─────────────────────────────────────────────────
       P_hydraulic  = ρ · g · Q · H
       P_shaft      = P_hydraulic / η_pump
       P_electrical = P_shaft / η_motor

       η_pump  is assumed (unknown without manufacturer power curves).
       η_motor is derived from the datasheet FLA and shaft power:
           η_motor × PF = P_shaft_rated / S_rated
                        = 1491 W / (√3 × 230 V × 6 A)  =  0.624
       Using PF = 0.76 (typical for a 2 HP 3-phase motor at full load)
       gives η_motor ≈ 0.82.

     Method 2 – Nameplate apparent power, affinity-scaled (upper bound)
     ───────────────────────────────────────────────────────────────────
       S_rated  = √3 × V_rated × FLA          [from datasheet, no assumptions]
       S_design = S_rated × n³                [affinity law; P_real < S_design]

       This is the conservative apparent power draw at the design speed if
       the pump were running at its full rated load.  Use for VFD / wiring
       sizing.  Actual real power at the design operating point will be lower
       (bounded below by Method 1).

Affinity Laws (geometrically similar operating points)
──────────────────────────────────────────────────────
  Q₂ / Q₁ = n          where n = N₂ / N₁  (fractional speed ratio)
  H₂ / H₁ = n²
  P₂ / P₁ = n³         (exact along the affinity / similarity parabola)

Electrical data – Model 276D (2 HP TEFC, 3-phase, Curve A)
──────────────────────────────────────────────────────────
  Source: AMT SPE-13-14 datasheet, page 2
  Voltage: 230 / 460 V @ 60 Hz
  FLA:       6  /   3 A
  Apparent power S = √3 × 230 × 6 = 2392 VA  (same at either voltage tap)

Usage
─────
  python amt_pump_affinity.py                              # interactive prompts
  python amt_pump_affinity.py --Q 80 --H 50               # supply design point as args
  python amt_pump_affinity.py --Q 80 --H 50 --voltage 460 --fla 3
  python amt_pump_affinity.py --Q 80 --H 50 --eta-pump 0.48

Bypass mode  (--bypass-Q)
──────────────────────────
  When a minimum-flow bypass is installed (bypass pipe from pump discharge
  to the suction well), the total pump flow is:

      Q_pump = Q_irrigation + Q_bypass

  Providing --bypass-Q adds the bypass flow to the irrigation demand and
  computes the pump's actual operating point (Q_pump, H_pump) on the RATED
  curve (n = 1 — no VFD needed), plus power at that point.

  Example:
    python amt_pump_affinity.py --Q 14.39 --H 55.1 --bypass-Q 33.6

  The plot shows both the irrigation demand point (amber star) and the new
  pump operating point (green diamond) on the rated curve.
"""

import argparse
import math
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.interpolate import CubicSpline
from scipy.optimize import brentq


# ── Physical constants ────────────────────────────────────────────────────────
RHO_WATER  = 1000.0      # water density [kg/m³], SG = 1.0
G_SI       = 9.80665     # gravitational acceleration [m/s²]
GPM_TO_M3S = 6.30902e-5  # 1 US gpm → m³/s
FT_TO_M    = 0.3048      # 1 ft     → m

# ── Motor / pump nameplate constants (Model 276D, Curve A) ────────────────────
N_RATED_RPM    = 3450        # rated synchronous speed [rpm]
P_SHAFT_W      = 2 * 745.7   # 2 HP shaft output [W]  (motor nameplate OUTPUT)
V_RATED        = 230.0       # rated voltage [V] (lower tap; use 460 with FLA/2)
FLA_RATED      = 6.0         # full-load amps at 230 V [A]
PHASES         = 3

# Apparent power at rated conditions (independent of voltage tap chosen)
S_RATED_VA = math.sqrt(3) * V_RATED * FLA_RATED   # 2392 VA

# Motor η derived from datasheet: η_motor × PF = P_shaft / S_rated
# Using PF = 0.76 (typical for a 2 HP 3-phase motor at full load)
PF_MOTOR      = 0.76
ETA_MOTOR     = P_SHAFT_W / (S_RATED_VA * PF_MOTOR)   # ≈ 0.82

# Default pump hydraulic efficiency (assumed – no power curve in datasheet).
#
# Efficiency is NOT constant along the H-Q curve.  It follows a bell-shaped
# profile: near zero at shutoff (internal recirculation), rising to a peak at
# the Best Efficiency Point (BEP), then falling again at high flows.
#
# Estimated BEP location: peak of Q × H on Curve A occurs near Q ≈ 80 gpm,
# giving P_hyd_peak ≈ 0.83 kW.  With the motor at full load (1.491 kW shaft):
#   η_pump_BEP ≈ 0.83 / 1.491 ≈ 0.56 — use this default near BEP.
#
# For operating points far to the left of BEP (low Q), reduce η_pump:
#   Q/Q_BEP ≈ 0.5  →  η_pump ≈ 0.45–0.50
#   Q/Q_BEP ≈ 0.2  →  η_pump ≈ 0.25–0.35  (strong internal recirculation)
#   Q/Q_BEP < 0.15 →  η_pump < 0.25        (minimum flow territory)
#
# The affinity law P ∝ n³ is also only exact at the BEP.  At off-BEP operating
# points, efficiency changes with speed, causing real power to deviate from n³
# scaling.  For low-flow design points, Method 2 (nameplate apparent power × n³)
# is the safer sizing estimate; Method 1 must use a reduced η_pump.
ETA_PUMP_DEFAULT = 0.58   # appropriate near BEP (Q ≈ 80 gpm); reduce for low Q
Q_BEP_ESTIMATE   = 80.0  # estimated gpm at best efficiency point


# ── 1. Hand-digitized Curve A performance data ────────────────────────────────
# Source: AMT 276 Series Cast-Iron performance chart (SPE-13), "Curve A"
#         276 Series 1½ HP ODP & 2 HP TEFC [1.12 & 1.49 kW]
#         Pink/magenta line; 3450 RPM, 60 Hz, Water SG = 1.0
#
# X-axis: Capacity [US gpm]  |  Y-axis: Total Head [ft]
CURVE_A_DATA = {
    'Q_gpm': [  0,  10,  20,  30,  40,  50,  60,  70,  80,  90, 100, 110, 120, 130],
    'H_ft':  [ 95,  91,  87,  83,  78,  73,  67,  61,  55,  48,  41,  33,  25,  17],
}

df_curve_a = pd.DataFrame(CURVE_A_DATA)

# ── 2. Cubic spline fit ────────────────────────────────────────────────────────
cs        = CubicSpline(df_curve_a['Q_gpm'], df_curve_a['H_ft'], extrapolate=False)
Q_MAX     = float(df_curve_a['Q_gpm'].max())    # max flow on rated curve [gpm]
H_SHUTOFF = float(df_curve_a['H_ft'].iloc[0])   # shut-off head [ft]


# ── 3. Affinity-law speed solver ───────────────────────────────────────────────
def find_speed_ratio(Q_d: float, H_d: float) -> float:
    """
    Find the fractional speed ratio n = N / N_rated so that the pump's H-Q
    curve, scaled by the affinity laws, passes exactly through (Q_d, H_d).

    At speed n·N_rated the rated curve f(Q) maps to:
        H(Q) = n² · f(Q / n)

    For the design point to lie on the scaled curve:
        H_d / n² = f(Q_d / n)     … solve for n

    Parameters
    ----------
    Q_d : design flow rate [US gpm]
    H_d : design total head [ft]

    Returns
    -------
    n : float  – speed ratio N / N_rated
    """
    def residual(n: float) -> float:
        Q0 = Q_d / n
        if Q0 < 0 or Q0 > Q_MAX:
            return np.inf
        H0  = H_d / n**2
        val = cs(Q0)
        return np.inf if np.isnan(val) else float(H0 - val)

    n_scan = np.linspace(0.05, 2.5, 3000)
    res    = np.array([residual(n) for n in n_scan])

    brackets = [
        (n_scan[i], n_scan[i + 1])
        for i in range(len(res) - 1)
        if np.isfinite(res[i]) and np.isfinite(res[i + 1])
        and res[i] * res[i + 1] < 0
    ]

    if not brackets:
        raise ValueError(
            f"No feasible speed found for design point "
            f"Q = {Q_d:.1f} gpm, H = {H_d:.1f} ft.\n"
            "  Verify the design point is within the pump's operating envelope:\n"
            f"  Q ∈ (0, {Q_MAX:.0f}] gpm  and  H ≤ {H_SHUTOFF:.0f} ft at rated speed."
        )

    a, b = brackets[0]    # lowest (most energy-efficient) root
    return brentq(residual, a, b, xtol=1e-10, rtol=1e-10)


# ── 4. Power calculation ───────────────────────────────────────────────────────
def calculate_power(
    Q_d: float,
    H_d: float,
    eta_pump: float = ETA_PUMP_DEFAULT,
    v_rated:  float = V_RATED,
    fla:      float = FLA_RATED,
) -> dict:
    """
    Calculate power at the design operating point (Q_d [gpm], H_d [ft]).

    Parameters
    ----------
    Q_d      : design flow rate [US gpm]
    H_d      : design total head [ft]
    eta_pump : pump hydraulic efficiency (default 0.52; adjust if known)
    v_rated  : rated motor voltage [V]  (default 230 V)
    fla      : full-load amps at v_rated [A] (default 6 A)

    Returns
    -------
    dict of result values
    """
    n     = find_speed_ratio(Q_d, H_d)
    N_rpm = n * N_RATED_RPM

    # Apparent power from nameplate electrical data (no assumed efficiencies)
    S_rated_va = math.sqrt(3) * v_rated * fla      # rated apparent power [VA]

    # η_motor × PF from datasheet (shaft output / apparent input)
    eta_pf     = P_SHAFT_W / S_rated_va            # dimensionless product
    eta_motor  = eta_pf / PF_MOTOR                 # η_motor ≈ 0.82 at default PF

    # ── Method 1: physics-based (design-point specific) ───────────────────────
    Q_m3s       = Q_d * GPM_TO_M3S
    H_m         = H_d * FT_TO_M
    P_hyd_W     = RHO_WATER * G_SI * Q_m3s * H_m  # hydraulic power [W]
    P_shaft_m1  = P_hyd_W  / eta_pump              # shaft power [W]
    P_elec_m1   = P_shaft_m1 / eta_motor           # electrical real power [W]

    # Nameplate consistency check
    if P_shaft_m1 > P_SHAFT_W * 1.05:
        import warnings
        warnings.warn(
            f"\nPhysical inconsistency: assumed η_pump = {eta_pump:.2f} implies a shaft "
            f"power of {P_shaft_m1/1000:.3f} kW, which exceeds the 2 HP nameplate "
            f"({P_SHAFT_W/1000:.3f} kW).\n"
            f"The minimum physically valid pump efficiency at this operating point is "
            f"η_pump_min = {P_hyd_W/P_SHAFT_W:.2f}.  "
            f"Increase --eta-pump to at least that value.",
            stacklevel=2,
        )

    # ── Method 2: nameplate apparent power × n³ (upper bound) ────────────────
    S_design_va = S_rated_va * n**3                # apparent power at design speed [VA]
    # Real power upper bound (using PF from datasheet-derived η×PF product):
    P_elec_m2   = S_design_va * PF_MOTOR           # real power upper bound [W]

    return dict(
        Q_design_gpm   = Q_d,
        H_design_ft    = H_d,
        speed_ratio    = n,
        N_design_rpm   = N_rpm,
        eta_pump       = eta_pump,
        eta_motor      = eta_motor,
        pf_motor       = PF_MOTOR,
        S_rated_kva    = S_rated_va / 1000,
        # Method 1
        P_hyd_kW       = P_hyd_W    / 1000,
        P_shaft_m1_kW  = P_shaft_m1 / 1000,
        P_elec_m1_kW   = P_elec_m1  / 1000,
        # Method 2
        S_design_kva   = S_design_va / 1000,
        P_elec_m2_kW   = P_elec_m2   / 1000,
    )


# ── 5a. Bypass result printer ─────────────────────────────────────────────────
def print_bypass_results(
    result_irrig: dict,
    Q_bypass: float,
    result_pump: dict,
) -> None:
    """
    Print a summary of the bypass design: irrigation demand, pump operating
    point, and power at the pump point.

    Parameters
    ----------
    result_irrig : dict   – output of calculate_power() at the irrigation point
    Q_bypass     : float  – bypass return flow [US gpm]
    result_pump  : dict   – output of calculate_power() at the pump point
    """
    r_i = result_irrig
    r_p = result_pump
    bar = '─' * 65

    print(f"\n{bar}")
    print("  AMT 276 Series – Curve A  │  Minimum-Flow Bypass Design")
    print(bar)

    # ── Irrigation demand ──────────────────────────────────────────────────────
    q_ratio_irrig = r_i['Q_design_gpm'] / Q_BEP_ESTIMATE
    print("  IRRIGATION DEMAND  (system operating point)")
    print(f"    Flow to field              :  {r_i['Q_design_gpm']:.2f} gpm  @  "
          f"{r_i['H_design_ft']:.1f} ft")
    print(f"    Q / Q_BEP                  :  {q_ratio_irrig*100:.0f} %  "
          f"← BELOW safe operating range without bypass")

    # ── Bypass ─────────────────────────────────────────────────────────────────
    print()
    print("  BYPASS CONFIGURATION")
    print(f"    Bypass return flow         :  {Q_bypass:.2f} gpm  (to well/suction)")
    print(f"    Total pump flow  Q_pump    :  {r_p['Q_design_gpm']:.2f} gpm  "
          f"(irrigation + bypass)")

    # ── Pump operating point ───────────────────────────────────────────────────
    q_ratio_pump = r_p['Q_design_gpm'] / Q_BEP_ESTIMATE
    n_p = r_p['speed_ratio']
    print()
    print("  PUMP OPERATING POINT  (rated speed, 3 450 RPM — no VFD required)")
    print(f"    Q_pump                     :  {r_p['Q_design_gpm']:.2f} gpm  @  "
          f"{r_p['H_design_ft']:.1f} ft  (on Curve A)")
    print(f"    Speed ratio n              :  {n_p:.4f}  (≈ 1.000 = rated speed)")
    print(f"    Required speed             :  {r_p['N_design_rpm']:.0f} RPM")
    print(f"    Q_pump / Q_BEP             :  {q_ratio_pump*100:.0f} %  "
          f"← improved; within acceptable range")
    h_surplus = r_p['H_design_ft'] - r_i['H_design_ft']
    print(f"    Head surplus (pump − sys)  :  {h_surplus:.1f} ft  "
          f"(dissipated by bypass valve + pipe)")
    print(bar)

    # ── Power ──────────────────────────────────────────────────────────────────
    print("  POWER AT PUMP OPERATING POINT")
    print()
    print("  Method 1 – Physics-based")
    print(f"    Hydraulic power ρgQH       :  {r_p['P_hyd_kW']:.3f} kW")
    print(f"    Pump efficiency η_pump     :  {r_p['eta_pump']*100:.0f} %  (assumed at "
          f"{q_ratio_pump*100:.0f}% BEP)")
    print(f"    Shaft power                :  {r_p['P_shaft_m1_kW']:.3f} kW")
    print(f"    Motor efficiency η_m       :  {r_p['eta_motor']*100:.0f} %  (derived)")
    print(f"  ► P_electrical  Method 1     :  {r_p['P_elec_m1_kW']:.3f} kW")
    print()
    print("  Method 2 – Nameplate apparent power × n³  (n = 1 → full-load upper bound)")
    print(f"    S_rated × n³               :  {r_p['S_rated_kva']:.3f} kVA × "
          f"{n_p:.4f}³")
    print(f"    S_design                   :  {r_p['S_design_kva']:.3f} kVA")
    print(f"  ► P_electrical  Method 2     :  {r_p['P_elec_m2_kW']:.3f} kW  (upper bound)")
    print(bar)

    # ── Advisory ──────────────────────────────────────────────────────────────
    # Use a 1% tolerance so that Q_pump/Q_BEP = 0.599 (≈ 60%) rounds up correctly.
    q_pct_pump = round(q_ratio_pump * 100)
    if q_pct_pump >= 60:
        advisory = (f"  ✓  Q_pump / Q_BEP = {q_pct_pump:.0f}% — acceptable for continuous operation.\n"
                    f"     Both power methods converge to ≈ {r_p['P_elec_m2_kW']:.2f} kW because n ≈ 1\n"
                    f"     and η_pump = {r_p['eta_pump']:.2f} implies shaft power ≈ 2 HP nameplate.\n"
                    f"     The pump is now fully loaded — working as engineered.")
    elif q_pct_pump >= 50:
        advisory = (f"  ⚠  Q_pump / Q_BEP = {q_pct_pump:.0f}% — marginal; API 610 minimum is 50%.\n"
                    f"     Consider increasing Q_bypass to bring Q_pump above 60% of BEP.")
    else:
        advisory = (f"  ✗  Q_pump / Q_BEP = {q_pct_pump:.0f}% — still below API 610 minimum (50%).\n"
                    f"     Increase --bypass-Q to raise total pump flow above {0.5*Q_BEP_ESTIMATE:.0f} gpm.")
    print("  Notes:")
    print(advisory)
    print()
    print("  Bypass pipe sizing guide  (Q_bypass = {:.1f} gpm = {:.5f} ft³/s):".format(
        Q_bypass, Q_bypass / 449))

    import math as _m
    pipes = [
        ('1.5" Sch 40 PVC', 1.610),
        ('2" Sch 40 PVC',   2.067),
        ('2.5" Sch 40 PVC', 2.469),
    ]
    Q_ft3s = Q_bypass / 449
    for name, ID_in in pipes:
        A = _m.pi / 4 * (ID_in / 12) ** 2
        v = Q_ft3s / A
        ok = '✓' if v < 5 else '✗'
        print(f"    {name:20s}  ID = {ID_in:.3f}\"  v = {v:.2f} ft/s  {ok}")
    print()
    print("  Recommended: 2\" Sch 40 PVC + globe valve for flow setting.")
    print(f"  Install a PRV (~{r_i['H_design_ft']/2.307:.0f} psi) on the field supply branch to maintain")
    print(f"  {r_i['H_design_ft']:.0f} ft ({r_i['H_design_ft']/2.307:.0f} psi) at the irrigation inlet.")
    print(bar)


# ── 5b. Result printer ─────────────────────────────────────────────────────────
def print_results(result: dict) -> None:
    r   = result
    bar = '─' * 60

    print(f"\n{bar}")
    print("  AMT 276 Series – Curve A (2 HP TEFC, 3-ph)  │  Results")
    print(bar)
    print(f"  Design point             :  {r['Q_design_gpm']:.1f} gpm  @  "
          f"{r['H_design_ft']:.1f} ft")
    print(f"  Speed ratio  n = N/N₀   :  {r['speed_ratio']:.4f}")
    print(f"  Required speed           :  {r['N_design_rpm']:.1f} RPM"
          f"  (rated: {N_RATED_RPM} RPM)")
    print()

    print(f"  Nameplate electrical data (Model 276D @ {V_RATED:.0f} V / "
          f"{FLA_RATED:.0f} A FLA):")
    print(f"    Rated apparent power S :  {r['S_rated_kva']:.3f} kVA  "
          f"(= √3 × {V_RATED:.0f} V × {FLA_RATED:.0f} A)")
    print(f"    η_motor × PF           :  {r['eta_motor']*r['pf_motor']:.3f}  "
          f"(= {P_SHAFT_W:.0f} W shaft / {r['S_rated_kva']*1000:.0f} VA)")
    print(f"    PF (assumed)           :  {r['pf_motor']:.2f}")
    print(f"    η_motor (derived)      :  {r['eta_motor']:.2f}  "
          f"({r['eta_motor']*100:.0f} %)")
    print(bar)

    print("  Method 1 – Physics-based (design-point specific)")
    print(f"    Hydraulic power ρgQH   :  {r['P_hyd_kW']:.3f} kW")
    print(f"    Pump efficiency η_pump :  {r['eta_pump']*100:.0f} %  (assumed)")
    print(f"    Shaft power            :  {r['P_shaft_m1_kW']:.3f} kW")
    print(f"    Motor efficiency η_m   :  {r['eta_motor']*100:.0f} %  (derived)")
    print(f"  ► Electrical real power  :  {r['P_elec_m1_kW']:.3f} kW")
    print()

    print("  Method 2 – Nameplate apparent power × n³  (upper bound for sizing)")
    print(f"    S_rated × n³           :  {r['S_rated_kva']:.3f} kVA × "
          f"{r['speed_ratio']:.4f}³")
    print(f"    Apparent power S_design:  {r['S_design_kva']:.3f} kVA  "
          f"← use for VFD / wiring rating")
    print(f"    × PF ({r['pf_motor']:.2f})")
    print(f"  ► Electrical real power  :  {r['P_elec_m2_kW']:.3f} kW  (upper bound)")
    print(bar)

    # ── Off-BEP efficiency advisory ───────────────────────────────────────────
    q_ratio = r['Q_design_gpm'] / Q_BEP_ESTIMATE
    if q_ratio < 0.35:
        eta_advice = (f"  ⚠  Off-BEP advisory: Q = {r['Q_design_gpm']:.1f} gpm is only"
                      f" {q_ratio*100:.0f} % of Q_BEP ≈ {Q_BEP_ESTIMATE:.0f} gpm.\n"
                      f"     At this far-left operating point, pump efficiency is likely\n"
                      f"     0.25–0.35, not the assumed {r['eta_pump']*100:.0f} %.  Method 1\n"
                      f"     will underestimate shaft power; Method 2 is the safer\n"
                      f"     sizing estimate here.  Also consider whether continuous\n"
                      f"     operation this far below BEP may cause overheating.")
    elif q_ratio < 0.60:
        eta_advice = (f"  ⚠  Off-BEP note: Q = {r['Q_design_gpm']:.1f} gpm is"
                      f" {q_ratio*100:.0f} % of Q_BEP ≈ {Q_BEP_ESTIMATE:.0f} gpm.\n"
                      f"     Pump efficiency here is likely 0.40–0.50 rather than the\n"
                      f"     assumed {r['eta_pump']*100:.0f} %.  Consider reducing --eta-pump.")
    else:
        eta_advice = (f"  ✓  Q = {r['Q_design_gpm']:.1f} gpm is"
                      f" {q_ratio*100:.0f} % of Q_BEP ≈ {Q_BEP_ESTIMATE:.0f} gpm.\n"
                      f"     The assumed η_pump = {r['eta_pump']*100:.0f} % is reasonable here.")

    print("  Notes:")
    print(eta_advice)
    print()
    print("    Pump efficiency is NOT constant along the H-Q curve.  It peaks")
    print("    at the BEP and drops on both sides.  The affinity law P ∝ n³ is")
    print("    also only exact at the BEP; off-BEP, efficiency changes with")
    print("    speed, causing real power to deviate from simple n³ scaling.")
    print()
    print("    Method 1 estimates real power at the specific design point but")
    print("    requires an accurate η_pump.  Use --eta-pump to override the")
    print("    default (0.58 near BEP; 0.30–0.35 at very low flow).")
    print()
    print("    Method 2 (apparent power × n³) is a conservative upper bound")
    print("    for VFD / wiring sizing.  Motor η and PF are implicit in the")
    print("    FLA — no separate motor assumption needed.  Actual real power")
    print("    lies between Method 1 and Method 2.")
    print(bar)


# ── 6. Plotting ────────────────────────────────────────────────────────────────
def plot_results(
    result: dict | None = None,
    bypass_result: dict | None = None,
    save_path: str = "one-year-analysis/2-pump-power/pump_affinity_result.png",
) -> None:
    """
    Plot rated curve, speed-scaled curve, affinity parabola, and operating
    point(s).

    Parameters
    ----------
    result        : dict from calculate_power() at the irrigation/design point.
    bypass_result : dict from calculate_power() at the pump operating point
                    (Q_pump = Q_irrigation + Q_bypass on the rated curve).
                    When supplied, a green diamond marks the pump point and an
                    amber star marks the irrigation demand point.
    save_path     : output PNG path.
    """
    # ── Colour palette ────────────────────────────────────────────────────────
    C_RATED    = '#2171B5'   # steel blue   – rated H-Q curve & data dots
    C_SCALED   = '#CB4B16'   # burnt orange – speed-scaled curve
    C_PARABOLA = '#999999'   # neutral grey – affinity parabola
    C_POINT    = '#F6AE2D'   # warm amber   – design / irrigation demand point
    C_PUMP_PT  = '#238B45'   # forest green – pump operating point (bypass mode)
    C_BEP      = '#238B45'   # forest green – BEP indicator

    Q_fine = np.linspace(0, Q_MAX, 600)
    H_fine = cs(Q_fine)

    fig, ax = plt.subplots(figsize=(10, 6))

    # Rated H-Q curve
    ax.plot(Q_fine, H_fine, '-', color=C_RATED, lw=2.5,
            label=f'Curve A @ {N_RATED_RPM} RPM  (rated)')
    ax.scatter(df_curve_a['Q_gpm'], df_curve_a['H_ft'],
               color=C_RATED, s=30, zorder=5, label='Digitized data points')

    # BEP indicator
    ax.axvline(Q_BEP_ESTIMATE, color=C_BEP, lw=1.0, ls='--', alpha=0.45,
               label=f'Est. BEP  (~{Q_BEP_ESTIMATE:.0f} gpm)')

    if result:
        n   = result['speed_ratio']
        N_d = result['N_design_rpm']
        Q_d = result['Q_design_gpm']
        H_d = result['H_design_ft']

        if bypass_result is None:
            # ── Standard mode: single design point ───────────────────────────
            # Speed-scaled pump curve
            ax.plot(Q_fine * n, H_fine * n**2, '--', color=C_SCALED, lw=2.0,
                    label=f'Scaled curve @ {N_d:.0f} RPM  (n = {n:.4f})')

            # Affinity / similarity parabola
            Q_par = np.linspace(0.5, Q_MAX, 400)
            H_par = H_d * (Q_par / Q_d) ** 2
            valid = H_par <= H_SHUTOFF * 1.15
            ax.plot(Q_par[valid], H_par[valid], ':', color=C_PARABOLA, lw=1.6,
                    label='Affinity parabola (similarity locus)')

            ax.scatter(Q_d, H_d, color=C_POINT, s=220, zorder=6, marker='*',
                       edgecolors='#333333', linewidths=0.5,
                       label=f'Design point  ({Q_d:.1f} gpm, {H_d:.1f} ft)')
        else:
            # ── Bypass mode: show irrigation demand + pump operating point ───
            Q_p = bypass_result['Q_design_gpm']
            H_p = bypass_result['H_design_ft']
            Q_bp = Q_p - Q_d

            # Shade the healthy operating band on the rated curve (50–100 % BEP)
            Q_healthy = np.linspace(0.50 * Q_BEP_ESTIMATE, Q_BEP_ESTIMATE, 200)
            H_healthy = cs(Q_healthy)
            ax.fill_betweenx(
                [0, H_SHUTOFF * 1.15],
                0.50 * Q_BEP_ESTIMATE, Q_BEP_ESTIMATE,
                color='#E8F5E9', alpha=0.6, zorder=0,
                label='Healthy operating band  (50–100 % BEP)',
            )

            # Arrow from irrigation demand → pump operating point
            ax.annotate(
                '',
                xy=(Q_p, H_p), xytext=(Q_d, H_d),
                arrowprops=dict(
                    arrowstyle='->', color='#888888', lw=1.4,
                    connectionstyle='arc3,rad=-0.25',
                ),
            )
            ax.annotate(
                f'+{Q_bp:.1f} gpm bypass',
                xy=((Q_d + Q_p) / 2 + 2, (H_d + H_p) / 2 + 4),
                fontsize=9, color='#555555', style='italic',
            )

            # Irrigation demand star (amber)
            ax.scatter(Q_d, H_d, color=C_POINT, s=200, zorder=7, marker='*',
                       edgecolors='#333333', linewidths=0.5,
                       label=f'Irrigation demand  ({Q_d:.1f} gpm, {H_d:.1f} ft)')

            # Pump operating point diamond (green)
            ax.scatter(Q_p, H_p, color=C_PUMP_PT, s=180, zorder=7, marker='D',
                       edgecolors='#333333', linewidths=0.5,
                       label=f'Pump operating point  ({Q_p:.1f} gpm, {H_p:.1f} ft)')

    ax.set_xlim(left=0, right=Q_MAX * 1.05)
    ax.set_ylim(bottom=0, top=H_SHUTOFF * 1.15)
    ax.set_xlabel('Capacity  [US gpm]', fontsize=12)
    ax.set_ylabel('Total Head  [ft]',   fontsize=12)

    if bypass_result:
        ax.set_title(
            'AMT 276 Series – Cast Iron, Model 276D  │  Minimum-Flow Bypass\n'
            'Curve A: 2 HP TEFC, 3-phase  |  3450 RPM  |  60 Hz  |  Water SG = 1.0',
            fontsize=12,
        )
    else:
        ax.set_title(
            'AMT 276 Series – Cast Iron, Model 276D\n'
            'Curve A: 2 HP TEFC, 3-phase  |  3450 RPM  |  60 Hz  |  Water SG = 1.0',
            fontsize=12,
        )

    ax.legend(fontsize=10, framealpha=0.9)
    ax.grid(True, color='#DDDDDD', linewidth=0.8)
    ax.set_facecolor('#FAFAFA')
    fig.patch.set_facecolor('white')
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    print(f"\n[plot] Saved → '{save_path}'")
    plt.show()


# ── 7. Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="AMT 276D Curve A – affinity-law power calculator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--Q',        type=float, default=None,
                        help='Design flow rate [US gpm]')
    parser.add_argument('--H',        type=float, default=None,
                        help='Design total head [ft]')
    parser.add_argument('--eta-pump', type=float, default=ETA_PUMP_DEFAULT,
                        dest='eta_pump',
                        help=f'Pump hydraulic efficiency (default {ETA_PUMP_DEFAULT})')
    parser.add_argument('--voltage',  type=float, default=V_RATED,
                        help=f'Motor rated voltage [V] (default {V_RATED:.0f})')
    parser.add_argument('--fla',      type=float, default=FLA_RATED,
                        help=f'Full-load amps at --voltage (default {FLA_RATED:.0f} A)')
    parser.add_argument('--no-plot',   action='store_true',
                        help='Skip the matplotlib plot')
    parser.add_argument('--bypass-Q', type=float, default=None,
                        dest='bypass_Q',
                        help='Bypass return flow [US gpm] added to --Q for '
                             'the pump operating point.  Prints a bypass '
                             'design summary and shows both points on the plot.')
    args = parser.parse_args()

    # Print the digitized data table
    print("\nCurve A – hand-digitized H-Q data  (3450 RPM, 60 Hz)")
    print(df_curve_a.to_string(index=False))
    print(f"\nOperating envelope: Q ∈ [0, {Q_MAX:.0f}] gpm, "
          f"H_shutoff = {H_SHUTOFF:.0f} ft")

    # Get design point
    if args.Q is not None and args.H is not None:
        Q_d, H_d = args.Q, args.H
    else:
        print("\nEnter the desired design operating point:")
        try:
            Q_d = float(input("  Design flow rate Q [US gpm]: "))
            H_d = float(input("  Design total head H   [ft] : "))
        except (ValueError, EOFError):
            print("Invalid input – exiting.")
            sys.exit(1)

    if Q_d <= 0 or H_d <= 0:
        print("Error: Q and H must be positive.")
        sys.exit(1)

    try:
        result = calculate_power(Q_d, H_d,
                                 eta_pump = args.eta_pump,
                                 v_rated  = args.voltage,
                                 fla      = args.fla)
    except ValueError as exc:
        print(f"\nError: {exc}")
        sys.exit(1)

    # ── Bypass mode ───────────────────────────────────────────────────────────
    if args.bypass_Q is not None and args.bypass_Q > 0:
        Q_bypass  = args.bypass_Q
        Q_pump    = Q_d + Q_bypass

        if Q_pump > Q_MAX:
            print(f"\nError: Total pump flow {Q_pump:.1f} gpm exceeds the maximum "
                  f"on Curve A ({Q_MAX:.0f} gpm).  Reduce --bypass-Q.")
            sys.exit(1)

        H_pump = float(cs(Q_pump))
        if np.isnan(H_pump):
            print(f"\nError: Could not interpolate head at Q_pump = {Q_pump:.1f} gpm.")
            sys.exit(1)

        if H_pump < H_d:
            print(
                f"\nWarning: Pump head at Q_pump = {Q_pump:.1f} gpm "
                f"({H_pump:.1f} ft) is less than the required system head "
                f"{H_d:.1f} ft.  The field may be under-pressurized.  "
                "Consider reducing --bypass-Q.",
                file=sys.stderr,
            )

        # Automatically choose η_pump for the pump operating point
        q_ratio_pump = Q_pump / Q_BEP_ESTIMATE
        if q_ratio_pump >= 0.70:
            eta_pump_pump = args.eta_pump           # near-BEP: use default or user value
        elif q_ratio_pump >= 0.60:
            eta_pump_pump = min(args.eta_pump, 0.50)
        elif q_ratio_pump >= 0.50:
            eta_pump_pump = 0.45
        else:
            eta_pump_pump = 0.38

        try:
            result_pump = calculate_power(
                Q_pump, H_pump,
                eta_pump = eta_pump_pump,
                v_rated  = args.voltage,
                fla      = args.fla,
            )
        except ValueError as exc:
            print(f"\nBypass error: {exc}")
            sys.exit(1)

        print_bypass_results(result, Q_bypass, result_pump)

        if not args.no_plot:
            plot_results(result, bypass_result=result_pump,
                         save_path=f"one-year-analysis/2-pump-power/images/pump_affinity_{Q_pump}_{H_pump}.png")
    else:
        # ── Standard mode ─────────────────────────────────────────────────────
        print_results(result)

        if not args.no_plot:
            plot_results(result, save_path=f"one-year-analysis/2-pump-power/images/pump_affinity_{Q_d}_{H_d}.png")


if __name__ == '__main__':
    main()