"""
integrated_analysis.py
=======================
Assesses whether the solar + battery system can power the irrigation pump
according to the weekly schedule produced by 4-irrigation, over a full
growing season.

Unlike the greedy "run whenever you can" logic in 3-operating-hours-available,
this simulation enforces a specific irrigation calendar:

  • Irrigation only on the days designated by the weekly schedule.
  • On each irrigation day the pump must accumulate exactly ``hrs_per_day``
    pump-hours (or as many as the energy allows — shortfalls are flagged).
  • The battery continues to charge / discharge between irrigation days.
  • A hard ``MAX_DAILY_HRS`` cap prevents over-running the schedule.

Inputs
------
  Solar power CSV     : 1-solar-power/gen-power/*_power.csv
  Weekly schedule CSV : 4-irrigation/results/weekly_<crop>_<year>.csv

Outputs
-------
  Printed report   : per-week energy reliability summary and warning list
  CSV              : hourly simulation results for the growing season
  PNG images       : 4 diagnostic plots saved to images/

Usage
-----
    python integrated_analysis.py                        # cassava 2018, default paths
    python integrated_analysis.py --year 2022 --crop tomato
    python integrated_analysis.py --year 2020 --battery-kwh 5.0 --panels 18

References
----------
  System model logic from  3-operating-hours-available/battery_pump_analysis.py
  Irrigation schedule from 4-irrigation/irrigation_schedule.py
"""

import argparse
import csv
import math
import os
import sys
from datetime import date, timedelta
from glob import glob

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
import matplotlib.ticker as ticker
import numpy as np

# ===========================================================================
# DEFAULT SYSTEM PARAMETERS
# ===========================================================================

# ── Solar array ──
N_PANELS             = 15
PANEL_RATED_POWER_W  = 405.0
INVERTER_EFF         = 0.96   # DC → AC

# ── Battery ──
BATTERY_CAPACITY_KWH    = 5.0
BATTERY_MIN_SOC_PCT     = 0.10
BATTERY_MAX_SOC_PCT     = 0.85
BATTERY_INITIAL_SOC_PCT = 1.00
BATTERY_CHARGE_EFF      = 0.95
BATTERY_DISCHARGE_EFF   = 0.95

# ── Pump ──
PUMP_POWER_KW            = 1.263
MIN_SOLAR_FOR_DISCHARGE  = 0.10  # kW DC — battery may not discharge below this solar floor.
                                  # Matched to run_simulation.py CONFIG (min_solar_discharge = 0.10).
                                  # The old default of 1.0 kW effectively disabled the battery
                                  # during cloud events, making battery size irrelevant.

# ── Pump application rate (must match 4-irrigation/irrigation_schedule.py) ──
# These are used to convert SWD-based irrigation targets [mm] → pump hours.
# run_simulation.py patches these if farm area or pump specs change.
PUMP_FLOW_GPM        = 14.39
DRIP_EFFICIENCY      = 0.90
FARM_AREA_ACRES      = 0.78
FARM_AREA_M2         = FARM_AREA_ACRES * 4046.8564
_L_PER_GAL           = 3.785411784
PUMP_FLOW_M3_HR      = PUMP_FLOW_GPM * _L_PER_GAL * 60.0 / 1000.0
GROSS_APP_RATE_MM_HR = PUMP_FLOW_M3_HR * 1000.0 / FARM_AREA_M2
NET_APP_RATE_MM_HR   = GROSS_APP_RATE_MM_HR * DRIP_EFFICIENCY   # ≈ 0.932 mm/hr

# ── Irrigation schedule constraints ──
MAX_DAILY_HRS    = 8.0   # absolute cap on pump hours per irrigation day
PUMP_START_HOUR  = 8     # earliest local hour the pump may be started (24-hr)
                         # reflects that a manually operated system won't begin
                         # at first light; battery pre-charges while waiting

# ── Control mode ──
# CONTINUOUS_RUN = False  (default / solar-following):
#   The pump accumulates target hours across the day, skipping hours when
#   solar + battery cannot meet demand and resuming when conditions improve.
#   Models a smart controller (or an attentive operator) that cycles the
#   pump in sync with generation.
#
# CONTINUOUS_RUN = True  (continuous-run / Tier 1):
#   The farmer makes a single visit at PUMP_START_HOUR:
#     • If power is available at PUMP_START_HOUR → pump starts.  Any
#       subsequent energy failure halts it for the rest of the day (no restart).
#     • If power is NOT available at PUMP_START_HOUR → farmer leaves without
#       starting the pump; it stays off all day.
#   This means more battery cannot produce more unfulfilled days: a larger
#   battery is always at least as likely to start at PUMP_START_HOUR and to
#   sustain the pump longer once started.
#   This is the worst-case battery sizing scenario used by
#   9-control-scenarios/battery_capacity_sweep.py.
CONTINUOUS_RUN   = False  # set True via --continuous-run CLI flag

# ── Default paths (relative to this script) ──
_HERE           = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOLAR_DIR   = os.path.join(_HERE, '..', '1-solar-power', 'gen-power')
DEFAULT_SCHED_DIR   = os.path.join(_HERE, '..', '4-irrigation', 'results')
DEFAULT_OUTPUT_DIR  = os.path.join(_HERE, 'results')   # CSVs → results/<season_tag>/
DEFAULT_IMAGES_DIR  = os.path.join(_HERE, 'images')

# Day-of-week mapping (1=Mon … 7=Sun) for each schedule size
SCHEDULE_DOW = {
    0: [],
    1: [1],
    2: [1, 4],           # Mon, Thu
    3: [1, 3, 5],        # Mon, Wed, Fri
    4: [1, 2, 4, 5],     # Mon, Tue, Thu, Fri
    5: [1, 2, 3, 4, 5],  # weekdays
    6: [1, 2, 3, 4, 5, 6],
    7: [1, 2, 3, 4, 5, 6, 7],
}

# ===========================================================================
# MATPLOTLIB STYLE
# ===========================================================================

plt.rcParams.update({
    'figure.dpi'       : 200,
    'savefig.dpi'      : 200,
    'savefig.bbox'     : 'tight',
    'savefig.pad_inches': 0.15,
    'font.family'      : 'sans-serif',
    'font.size'        : 9,
    'axes.spines.top'  : False,
    'axes.spines.right': False,
    'axes.grid'        : True,
    'grid.alpha'       : 0.30,
    'axes.titlesize'   : 10,
    'axes.titleweight' : 'semibold',
    'axes.titlepad'    : 10,
    'axes.labelsize'   : 9,
    'legend.fontsize'  : 8,
    'xtick.labelsize'  : 8,
    'ytick.labelsize'  : 8,
})

C_SOLAR   = '#F5A623'
C_BATTERY = '#7B68EE'
C_PUMP    = '#47965A'
C_FAIL    = '#C0392B'
C_SOC     = '#3A7FC1'
C_NET_IRR = '#47965A'

MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun',
              'Jul','Aug','Sep','Oct','Nov','Dec']


# ===========================================================================
# DATA LOADERS
# ===========================================================================

def load_solar_power(solar_dir: str, year: int) -> dict:
    """
    Load the gen-power CSV for `year`.
    Returns {datetime_str: P_dc_kW} for all 8760 hourly rows.
    """
    pattern = os.path.join(os.path.abspath(solar_dir), f'*_{year}_power.csv')
    files   = glob(pattern)
    if not files:
        raise FileNotFoundError(
            f"No solar power CSV for {year} in: {os.path.abspath(solar_dir)}")
    rows = {}
    with open(files[0], newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows[r['datetime_local']] = float(r['P_dc_kW'])
    return rows


def load_weekly_schedule(sched_dir: str, crop: str, year: int) -> list:
    """
    Load the weekly schedule CSV from 4-irrigation.
    Returns list of week dicts sorted by week_start.
    """
    slug  = crop.replace('_', '-')
    path  = os.path.join(os.path.abspath(sched_dir), f'weekly_{slug}_{year}.csv')
    if not os.path.exists(path):
        raise FileNotFoundError(f"Weekly schedule not found: {path}")
    weeks = []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            weeks.append({
                'week_start'   : r['week_start'],
                'week_end'     : r['week_end'],
                'growth_stage' : r['growth_stage'],
                'net_irr_mm'   : float(r['net_irr_mm']),
                'irr_days_week': int(r['irr_days_week']),
                'hrs_per_day'  : float(r['hrs_per_day']),
                'ETc_mm'       : float(r['ETc_mm']),
                'precip_mm'    : float(r['precip_mm']),
            })
    return sorted(weeks, key=lambda w: w['week_start'])


# ===========================================================================
# SCHEDULE BUILDER
# ===========================================================================

def load_daily_targets(sched_dir: str, crop: str, year: int) -> dict:
    """
    Build a per-day irrigation schedule from the SWD-based daily CSV
    (``daily_<crop>_<year>.csv``) produced by irrigation_schedule.py.

    This replaces ``build_daily_schedule`` for the simulation.  Instead of
    dividing the weekly total evenly across irrigation days, each day carries
    its actual accumulated soil-water deficit as the pump target.  Rainfall
    surpluses on non-irrigation days are correctly propagated forward rather
    than discarded.

    Parameters
    ----------
    sched_dir : str
        Directory containing the daily CSV (same as the weekly CSV).
    crop : str
        Crop name (e.g. 'cassava').
    year : int
        Planting year (season starting in that year).

    Returns
    -------
    dict  {date_str: {'irrigate': bool, 'target_hrs': float}}

    Raises
    ------
    FileNotFoundError
        If the daily CSV does not exist (run irrigation_schedule.py first).
    KeyError
        If the daily CSV is missing the ``irr_target_mm`` column (old format;
        re-run irrigation_schedule.py to regenerate with SWD columns).
    """
    slug = crop.replace('_', '-')
    path = os.path.join(os.path.abspath(sched_dir), f'daily_{slug}_{year}.csv')
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Daily schedule CSV not found: {path}\n"
            f"Run: python 4-irrigation/irrigation_schedule.py --crop {crop}")

    daily: dict = {}
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if 'irr_target_mm' not in (reader.fieldnames or []):
            raise KeyError(
                f"'irr_target_mm' column missing from {path}.\n"
                f"Regenerate with: python 4-irrigation/irrigation_schedule.py")
        for r in reader:
            irr_mm   = float(r['irr_target_mm'] or 0.0)
            irrigate = irr_mm > 0.01    # tiny threshold against float noise
            # Pump hours = volume needed / net application rate; capped at daily max
            target_hrs = min(irr_mm / NET_APP_RATE_MM_HR, MAX_DAILY_HRS) if irrigate else 0.0
            daily[r['date']] = {
                'irrigate'  : irrigate,
                'target_hrs': target_hrs,
            }
    return daily


def build_daily_schedule(weekly: list) -> dict:
    """
    Legacy equal-distribution schedule builder (kept for reference).

    Expands the weekly schedule into a per-day dict using DOW assignment and
    divides the weekly total evenly across irrigation days.  Superseded by
    ``load_daily_targets`` which uses SWD-based per-event targets.

    Still used as fallback when the daily CSV is unavailable.
    """
    daily_sched = {}
    for w in weekly:
        n_days    = w['irr_days_week']
        hrs_day   = min(w['hrs_per_day'], MAX_DAILY_HRS)
        dow_list  = SCHEDULE_DOW.get(n_days, list(range(1, n_days + 1)))

        start = date.fromisoformat(w['week_start'])
        end   = date.fromisoformat(w['week_end'])
        cur   = start
        while cur <= end:
            iso_wd = cur.isoweekday()   # 1=Mon … 7=Sun
            irrigate = (n_days > 0) and (iso_wd in dow_list)
            daily_sched[cur.isoformat()] = {
                'irrigate'  : irrigate,
                'target_hrs': hrs_day if irrigate else 0.0,
            }
            cur += timedelta(days=1)
    return daily_sched


# ===========================================================================
# HOURLY SIMULATION
# ===========================================================================

def simulate_season(solar_power: dict, daily_sched: dict,
                    battery_kwh: float, n_panels: int) -> list:
    """
    Simulate the solar + battery + pump system hour-by-hour over the
    growing season, honouring the irrigation schedule.

    Parameters
    ----------
    solar_power  : {datetime_str: P_dc_kW}  — from 1-solar-power gen-power CSV
    daily_sched  : {date_str: {irrigate, target_hrs}}  — from build_daily_schedule
    battery_kwh  : Nameplate battery capacity [kWh] (may be overridden via CLI)
    n_panels     : Number of solar panels (scales solar output proportionally)

    Returns
    -------
    List of hourly record dicts (one per hour in the growing season).
    """
    # _SOLAR_REF_PANELS is the fixed panel count used when the solar gen-power
    # CSVs were generated (always 15 — see 1-solar-power/).  It must NOT be
    # confused with N_PANELS (the user-configurable default), which may be
    # monkey-patched by run_simulation.py during a sweep.  Using N_PANELS here
    # caused every sweep configuration to produce panel_scale = 1.0.
    _SOLAR_REF_PANELS = 15
    panel_scale = n_panels / _SOLAR_REF_PANELS  # relative to reference 15-panel CSVs

    no_battery = (battery_kwh <= 0.0)
    min_soc    = 0.0 if no_battery else battery_kwh * BATTERY_MIN_SOC_PCT
    max_soc    = 0.0 if no_battery else battery_kwh * BATTERY_MAX_SOC_PCT
    soc        = 0.0 if no_battery else battery_kwh * BATTERY_INITIAL_SOC_PCT

    current_date       = None
    daily_hrs_run      = 0.0
    target_hrs         = 0.0
    is_irr_day         = False
    pump_started_today = False   # True once the pump has run ≥ 1 hour today
    pump_halted_today  = False   # True once a continuous-run session was interrupted

    records = []

    # Build sorted list of (datetime_str, P_dc_kW) for season dates only
    season_dates = set(daily_sched.keys())

    # We need hourly rows for every date in the season
    for dt_str, P_dc_raw in sorted(solar_power.items()):
        d_str = dt_str[:10]
        if d_str not in season_dates:
            continue

        # ── New calendar day ──
        if d_str != current_date:
            current_date       = d_str
            daily_hrs_run      = 0.0
            sched              = daily_sched.get(d_str, {'irrigate': False, 'target_hrs': 0.0})
            is_irr_day         = sched['irrigate']
            target_hrs         = sched['target_hrs']
            pump_started_today = False
            pump_halted_today  = False

        hour       = int(dt_str[11:13])   # local hour 0–23 from "YYYY-MM-DDTHH:…"
        P_dc       = P_dc_raw * panel_scale
        P_solar_ac = P_dc * INVERTER_EFF
        is_daytime = P_dc >= MIN_SOLAR_FOR_DISCHARGE

        # ── Can the pump run this hour? ──
        need_more    = daily_hrs_run < target_hrs
        after_start  = hour >= PUMP_START_HOUR  # don't run before 8 AM local time
        pump_can_try = is_irr_day and need_more and after_start

        pump_on            = False
        P_solar_to_pump    = 0.0
        P_battery_to_pump  = 0.0
        P_solar_to_battery = 0.0
        P_curtailed        = 0.0
        failure_reason     = ''

        if not is_irr_day:
            failure_reason = 'not_scheduled'
        elif not need_more:
            failure_reason = 'target_reached'
        elif not after_start:
            failure_reason = 'before_start_hour'
        elif CONTINUOUS_RUN and pump_halted_today:
            # Continuous-run mode: session was interrupted earlier today; no restart
            failure_reason = 'continuous_run_halted'
            pump_can_try   = False

        if pump_can_try:
            if no_battery:
                if P_solar_ac >= PUMP_POWER_KW:
                    pump_on = True
                    P_solar_to_pump = PUMP_POWER_KW
                    P_curtailed = P_solar_ac - PUMP_POWER_KW
                    failure_reason = 'ok'
                else:
                    failure_reason = 'insufficient_solar'

            elif P_solar_ac >= PUMP_POWER_KW:
                # Case A: solar alone covers pump
                pump_on = True
                P_solar_to_pump = PUMP_POWER_KW
                surplus = P_solar_ac - PUMP_POWER_KW
                stored  = min(surplus * BATTERY_CHARGE_EFF, max_soc - soc)
                P_solar_to_battery = stored / BATTERY_CHARGE_EFF if stored > 0 else 0
                P_curtailed = surplus - P_solar_to_battery
                soc += stored
                failure_reason = 'ok'

            elif is_daytime and soc > min_soc:
                # Case B: battery supplements solar
                deficit      = PUMP_POWER_KW - P_solar_ac
                energy_need  = deficit / BATTERY_DISCHARGE_EFF
                available    = soc - min_soc
                if available >= energy_need:
                    pump_on = True
                    P_solar_to_pump   = P_solar_ac
                    P_battery_to_pump = deficit
                    soc -= energy_need
                    failure_reason = 'ok'
                else:
                    failure_reason = 'battery_empty'
                    stored = min(P_solar_ac * BATTERY_CHARGE_EFF, max_soc - soc)
                    P_solar_to_battery = stored / BATTERY_CHARGE_EFF if stored > 0 else 0
                    P_curtailed = P_solar_ac - P_solar_to_battery
                    soc += stored

            elif not is_daytime:
                failure_reason = 'night'
                # No discharge at night; charge from any residual solar is ~0
            else:
                failure_reason = 'low_solar_no_battery'
                stored = min(P_solar_ac * BATTERY_CHARGE_EFF, max_soc - soc)
                P_solar_to_battery = stored / BATTERY_CHARGE_EFF if stored > 0 else 0
                P_curtailed = P_solar_ac - P_solar_to_battery
                soc += stored

        else:
            # Not trying to pump — charge battery with any surplus
            if not no_battery and P_solar_ac > 0:
                stored = min(P_solar_ac * BATTERY_CHARGE_EFF, max_soc - soc)
                P_solar_to_battery = stored / BATTERY_CHARGE_EFF if stored > 0 else 0
                P_curtailed = P_solar_ac - P_solar_to_battery
                soc += stored

        if pump_on:
            daily_hrs_run      += 1.0
            pump_started_today  = True
        elif pump_can_try and CONTINUOUS_RUN:
            if pump_started_today:
                # Pump was running earlier today but energy failed this hour —
                # the continuous-run session is over; no restart.
                pump_halted_today = True
            elif hour == PUMP_START_HOUR:
                # Pump could not start at the scheduled start hour.
                # In continuous-run mode this models a farmer who arrives once,
                # finds insufficient power, and leaves without starting the pump.
                # No later starts are permitted for the rest of the day.
                pump_halted_today = True

        records.append({
            'datetime'         : dt_str,
            'date'             : d_str,
            'is_irr_day'       : is_irr_day,
            'target_hrs'       : round(target_hrs, 2),
            'pump_on'          : pump_on,
            'pump_hrs_today'   : round(daily_hrs_run, 1),
            'failure_reason'   : failure_reason,
            'P_dc_kW'          : round(P_dc, 4),
            'P_solar_ac_kW'    : round(P_solar_ac, 4),
            'P_solar_to_pump'  : round(P_solar_to_pump, 4),
            'P_batt_to_pump'   : round(P_battery_to_pump, 4),
            'P_solar_to_batt'  : round(P_solar_to_battery, 4),
            'P_curtailed'      : round(P_curtailed, 4),
            'battery_soc_kWh'  : round(soc, 4),
            'battery_soc_pct'  : round(soc / battery_kwh * 100 if battery_kwh > 0 else 0, 1),
        })

    return records


# ===========================================================================
# DAILY AND WEEKLY AGGREGATION
# ===========================================================================

def aggregate_daily(records: list, daily_sched: dict) -> list:
    """Collapse hourly records to daily summaries."""
    days: dict = {}
    for r in records:
        d = r['date']
        if d not in days:
            days[d] = {
                'date'        : d,
                'is_irr_day'  : r['is_irr_day'],
                'target_hrs'  : r['target_hrs'],
                'pump_hrs'    : 0.0,
                'energy_pump_kWh': 0.0,
                'energy_solar_kWh': 0.0,
                'failures'    : [],
                'soc_end'     : 0.0,
            }
        if r['pump_on']:
            days[d]['pump_hrs']         += 1.0
            days[d]['energy_pump_kWh']  += PUMP_POWER_KW
        days[d]['energy_solar_kWh'] += r['P_dc_kW'] * INVERTER_EFF
        if r['failure_reason'] not in ('ok', '', 'not_scheduled', 'target_reached'):
            days[d]['failures'].append(r['failure_reason'])
        days[d]['soc_end'] = r['battery_soc_pct']

    daily_list = []
    for d_str, v in sorted(days.items()):
        target  = v['target_hrs']
        actual  = v['pump_hrs']
        shortfall = max(0.0, target - actual)
        daily_list.append({
            'date'           : d_str,
            'is_irr_day'     : v['is_irr_day'],
            'target_hrs'     : target,
            'pump_hrs'       : actual,
            'shortfall_hrs'  : round(shortfall, 2),
            'met'            : shortfall < 0.5,
            'energy_pump_kWh': round(v['energy_pump_kWh'], 3),
            'energy_solar_kWh': round(v['energy_solar_kWh'], 3),
            'primary_failure': v['failures'][0] if v['failures'] else '',
            'soc_end_pct'    : v['soc_end'],
        })
    return daily_list


def aggregate_weekly(daily: list, weekly_sched: list) -> list:
    """Match daily results to the original weekly schedule rows."""
    # Build date → daily_result dict
    by_date = {d['date']: d for d in daily}
    results = []
    for w in weekly_sched:
        start = w['week_start']
        end   = w['week_end']
        # Collect irrigation days in this week
        irr_days = [d for d in daily
                    if start <= d['date'] <= end and d['is_irr_day']]
        total_target = sum(d['target_hrs']   for d in irr_days)
        total_actual = sum(d['pump_hrs']     for d in irr_days)
        total_short  = sum(d['shortfall_hrs'] for d in irr_days)
        n_met        = sum(1 for d in irr_days if d['met'])
        n_days       = len(irr_days)
        reliability  = (n_met / n_days * 100) if n_days > 0 else 100.0

        # Convert pump hours → mm irrigation delivered
        # (NET_APP_RATE_MM_HR is reproduced here to avoid importing 4-irrigation)
        _farm_m2 = 0.78 * 4046.856
        _pump_m3hr = 14.39 * 3.785411784 * 60 / 1000
        _net_mm_hr = _pump_m3hr * 1000 / _farm_m2 * 0.90   # 90% drip efficiency
        mm_delivered = total_actual * _net_mm_hr
        mm_needed    = w['net_irr_mm']
        mm_gap       = max(0.0, mm_needed - mm_delivered)

        results.append({
            'week_start'    : start,
            'week_end'      : end,
            'growth_stage'  : w['growth_stage'],
            'net_irr_needed': round(mm_needed, 1),
            'irr_days'      : n_days,
            'target_hrs'    : round(total_target, 1),
            'pump_hrs'      : round(total_actual, 1),
            'shortfall_hrs' : round(total_short, 2),
            'days_met'      : n_met,
            'reliability_pct': round(reliability, 1),
            'mm_delivered'  : round(mm_delivered, 1),
            'mm_gap'        : round(mm_gap, 1),
            'ETc_mm'        : w['ETc_mm'],
            'precip_mm'     : w['precip_mm'],
        })
    return results


# ===========================================================================
# REPORTING
# ===========================================================================

def print_report(weekly_res: list, daily_res: list,
                 year: int, crop: str, battery_kwh: float, n_panels: int):
    SEP = '=' * 72
    print(f'\n{SEP}')
    print(f'  INTEGRATED ANALYSIS — Season {year}/{year+1}  |  Crop: {crop}')
    print(f'  Solar: {n_panels} × {PANEL_RATED_POWER_W:.0f}W panels '
          f'= {n_panels * PANEL_RATED_POWER_W / 1000:.2f} kWp')
    print(f'  Battery: {battery_kwh:.1f} kWh  |  '
          f'Pump: {PUMP_POWER_KW:.3f} kW')
    print(SEP)

    # Season totals
    irr_days   = [d for d in daily_res if d['is_irr_day']]
    total_tgt  = sum(d['target_hrs']  for d in irr_days)
    total_act  = sum(d['pump_hrs']    for d in irr_days)
    total_sh   = sum(d['shortfall_hrs'] for d in irr_days)
    n_irr_days = len(irr_days)
    n_met      = sum(1 for d in irr_days if d['met'])
    season_rel = (n_met / n_irr_days * 100) if n_irr_days > 0 else 100.0

    print(f'  Irrigation days in season  : {n_irr_days}')
    print(f'  Target pump-hours (season) : {total_tgt:.0f} hrs')
    print(f'  Achieved pump-hours        : {total_act:.0f} hrs  '
          f'({total_act/total_tgt*100:.1f}% of target)')
    print(f'  Shortfall                  : {total_sh:.1f} hrs')
    print(f'  Days fully met             : {n_met}/{n_irr_days}  '
          f'({season_rel:.1f}% reliability)')
    print()

    # Weekly table
    print(f"  {'Wk':>3}  {'Period':>22}  {'Stage':<12}  "
          f"{'Need':>6}  {'Sched':>5}  {'Got':>5}  {'Gap':>5}  "
          f"{'Rel%':>5}  Notes")
    print('  ' + '-' * 75)
    warnings = []
    for idx, w in enumerate(weekly_res, 1):
        flags = []
        if w['shortfall_hrs'] > 0.5:
            flags.append(f"⚠ -{w['shortfall_hrs']:.1f}h")
            warnings.append((w['week_start'],
                             f"Energy shortfall: scheduled {w['target_hrs']:.1f}h, "
                             f"achieved {w['pump_hrs']:.1f}h "
                             f"({w['mm_gap']:.1f}mm unmet)"))
        if w['reliability_pct'] < 80:
            flags.append('LOW REL')
        note = ', '.join(flags)
        period = f"{w['week_start'][5:]} – {w['week_end'][5:]}"
        print(f"  {idx:>3}  {period:<22}  {w['growth_stage']:<12}  "
              f"{w['net_irr_needed']:>6.1f}  "
              f"{w['target_hrs']:>5.1f}  {w['pump_hrs']:>5.1f}  "
              f"{w['shortfall_hrs']:>5.1f}  "
              f"{w['reliability_pct']:>5.1f}  {note}")

    if warnings:
        print(f'\n  ⚠  ENERGY SHORTFALL WARNINGS ({len(warnings)})')
        print('  ' + '-' * 65)
        for d_str, msg in warnings:
            print(f'    {d_str}  {msg}')

    # Failure mode breakdown
    failures: dict = {}
    for r in daily_res:
        if r['primary_failure']:
            failures[r['primary_failure']] = failures.get(r['primary_failure'], 0) + 1
    if failures:
        print(f'\n  Failure mode breakdown (irrigation days only):')
        for mode, cnt in sorted(failures.items(), key=lambda x: -x[1]):
            print(f'    {mode:<25} {cnt:>4} hours')

    print(f'\n{SEP}')


# ===========================================================================
# PLOTS
# ===========================================================================

def make_plots(hourly: list, daily: list, weekly: list,
               year: int, crop: str, battery_kwh: float,
               images_dir: str):
    """Generate 4 diagnostic plots."""
    os.makedirs(images_dir, exist_ok=True)

    dates_all  = np.array([np.datetime64(r['date']) for r in daily])
    pump_hrs   = np.array([r['pump_hrs']         for r in daily])
    target_hrs = np.array([r['target_hrs']        for r in daily])
    shortfall  = np.array([r['shortfall_hrs']     for r in daily])
    soc_end    = np.array([r['soc_end_pct']       for r in daily])
    is_irr     = np.array([r['is_irr_day']        for r in daily], dtype=bool)
    tag        = f"{crop} {year}/{year+1}"

    # ── P1: Daily pump hours vs. target ────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.bar(dates_all[is_irr], target_hrs[is_irr],
           width=0.85, color='#B0BEC5', alpha=0.5, label='Target hrs')
    ax.bar(dates_all[is_irr], pump_hrs[is_irr],
           width=0.85, color=C_PUMP, alpha=0.85, label='Achieved hrs')
    # Shortfall in red
    fail_mask = is_irr & (shortfall > 0.1)
    if fail_mask.any():
        ax.bar(dates_all[fail_mask], shortfall[fail_mask],
               width=0.85, bottom=pump_hrs[fail_mask],
               color=C_FAIL, alpha=0.6, label='Shortfall')
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
    ax.set_ylabel('Pump hours / irrigation day')
    ax.set_title(f'Daily Pump Hours vs. Schedule Target — {tag}')
    ax.legend(ncol=3)
    fig.tight_layout()
    p = os.path.join(images_dir, 'P1_pump_hours.png')
    fig.savefig(p); plt.close(fig)
    print(f'  Saved: {os.path.basename(p)}')

    # ── P2: Battery SoC over season ─────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 3.5))
    ax.fill_between(dates_all, 0, soc_end, color=C_SOC, alpha=0.35)
    ax.plot(dates_all, soc_end, color=C_SOC, lw=1.4)
    ax.axhline(BATTERY_MIN_SOC_PCT * 100, color=C_FAIL, ls='--', lw=1.0,
               label=f'Min SoC ({BATTERY_MIN_SOC_PCT*100:.0f}%)')
    # Mark irrigation days
    for d in daily:
        if d['is_irr_day'] and d['pump_hrs'] > 0:
            ax.axvline(np.datetime64(d['date']), color=C_PUMP,
                       lw=0.3, alpha=0.25, zorder=1)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
    ax.set_ylabel('Battery SoC  [%]')
    ax.set_ylim(0, 105)
    ax.set_title(f'Battery State of Charge — {tag}')
    ax.legend()
    fig.tight_layout()
    p = os.path.join(images_dir, 'P2_battery_soc.png')
    fig.savefig(p); plt.close(fig)
    print(f'  Saved: {os.path.basename(p)}')

    # ── P3: Weekly reliability bar chart ───────────────────────────────────
    n_wk  = len(weekly)
    x_wk  = np.arange(n_wk)
    rel   = np.array([w['reliability_pct'] for w in weekly])
    tgt_h = np.array([w['target_hrs']      for w in weekly])
    got_h = np.array([w['pump_hrs']        for w in weekly])

    fig, (ax_rel, ax_hrs) = plt.subplots(2, 1, figsize=(max(10, n_wk * 0.55), 6),
                                          sharex=True, gridspec_kw={'hspace': 0.07})

    bar_col = np.where(rel >= 80, C_PUMP, C_FAIL)
    ax_rel.bar(x_wk, rel, color=bar_col, alpha=0.85, width=0.7)
    ax_rel.axhline(80, color=C_FAIL, ls='--', lw=1.0,
                   label='80% reliability threshold')
    ax_rel.set_ylabel('Schedule reliability  [%]')
    ax_rel.set_ylim(0, 110)
    ax_rel.set_title(f'Weekly Energy Reliability — {tag}')
    ax_rel.legend(fontsize=8)

    ax_hrs.bar(x_wk - 0.18, tgt_h, width=0.34,
               color='#B0BEC5', alpha=0.7, label='Target hrs/week')
    ax_hrs.bar(x_wk + 0.18, got_h, width=0.34,
               color=C_PUMP, alpha=0.85, label='Achieved hrs/week')
    ax_hrs.set_ylabel('Pump hrs / week')
    ax_hrs.legend(fontsize=8, ncol=2)

    month_labels = []
    prev_m = None
    for w in weekly:
        m = w['week_start'][5:7]
        if m != prev_m:
            month_labels.append(
                f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m)-1]}")
            prev_m = m
        else:
            month_labels.append('')
    ax_hrs.set_xticks(x_wk)
    ax_hrs.set_xticklabels(month_labels, fontsize=7.5)

    fig.tight_layout()
    p = os.path.join(images_dir, 'P3_weekly_reliability.png')
    fig.savefig(p); plt.close(fig)
    print(f'  Saved: {os.path.basename(p)}')

    # ── P4: Hourly power flow on a representative irrigation day ───────────
    # Find the median-demand irrigation day
    irr_day_records = [(d['date'], d['target_hrs'])
                       for d in daily if d['is_irr_day'] and d['target_hrs'] > 2]
    if irr_day_records:
        irr_day_records.sort(key=lambda x: x[1])
        rep_date = irr_day_records[len(irr_day_records) // 2][0]
        hr_data  = [r for r in hourly if r['date'] == rep_date]
        hours    = list(range(len(hr_data)))

        fig, (ax_pwr, ax_soc2) = plt.subplots(2, 1, figsize=(12, 5),
                                               gridspec_kw={'hspace': 0.08},
                                               sharex=True)

        solar_v  = np.array([r['P_solar_ac_kW']    for r in hr_data])
        pump_on_v= np.array([r['pump_on']           for r in hr_data], dtype=float)
        soc_v    = np.array([r['battery_soc_pct']   for r in hr_data])
        batt_v   = np.array([r['P_batt_to_pump']    for r in hr_data])

        ax_pwr.fill_between(hours, 0, solar_v, color=C_SOLAR, alpha=0.6,
                            label='Solar AC  [kW]')
        ax_pwr.fill_between(hours, 0, pump_on_v * PUMP_POWER_KW,
                            color=C_PUMP, alpha=0.55, label='Pump load  [kW]')
        ax_pwr.plot(hours, batt_v, color=C_BATTERY, lw=1.5, ls='--',
                    label='Battery → pump  [kW]')
        ax_pwr.axhline(PUMP_POWER_KW, color=C_PUMP, ls=':', lw=1.0,
                       alpha=0.6, label=f'Pump demand ({PUMP_POWER_KW}kW)')
        ax_pwr.set_ylabel('Power  [kW]')
        ax_pwr.set_title(f'Hourly Power Flow on Representative Irrigation Day  ({rep_date})')
        ax_pwr.legend(ncol=4, fontsize=7.5)

        ax_soc2.fill_between(hours, 0, soc_v, color=C_SOC, alpha=0.35)
        ax_soc2.plot(hours, soc_v, color=C_SOC, lw=1.4, label='Battery SoC  [%]')
        ax_soc2.axhline(BATTERY_MIN_SOC_PCT * 100, color=C_FAIL, ls='--',
                        lw=0.9, alpha=0.7)
        ax_soc2.set_ylabel('Battery SoC  [%]')
        ax_soc2.set_xlabel('Hour of day')
        ax_soc2.set_xlim(0, 23)
        ax_soc2.set_ylim(0, 105)
        ax_soc2.legend(fontsize=8)

        fig.tight_layout()
        p = os.path.join(images_dir, 'P4_hourly_power_flow.png')
        fig.savefig(p); plt.close(fig)
        print(f'  Saved: {os.path.basename(p)}')


# ===========================================================================
# CSV OUTPUT
# ===========================================================================

def save_csv(rows: list, path: str, label: str):
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f'  Saved {label}: {path}')


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            'Assess whether the solar+battery system can deliver the irrigation\n'
            'schedule from 4-irrigation over the growing season.\n'
            'Failures (energy shortfalls) are flagged per week and per day.'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Examples:\n'
            '  python integrated_analysis.py --year 2022\n'
            '  python integrated_analysis.py --year 2020 --crop tomato\n'
            '  python integrated_analysis.py --year 2021 --battery-kwh 5 --panels 18\n'
        ),
    )
    parser.add_argument('--year', type=int, default=2018,
                        help='Planting year of the growing season (default: 2018).')
    parser.add_argument('--crop', default='cassava',
                        help='Crop name matching the 4-irrigation results '
                             '(default: cassava).')
    parser.add_argument('--solar-dir', default=DEFAULT_SOLAR_DIR,
                        help='Directory with solar gen-power CSVs.')
    parser.add_argument('--sched-dir', default=DEFAULT_SCHED_DIR,
                        help='Directory with weekly/daily irrigation CSVs.')
    parser.add_argument('--output-dir', default=DEFAULT_OUTPUT_DIR,
                        help='Directory for output CSVs.')
    parser.add_argument('--images-dir', default=DEFAULT_IMAGES_DIR,
                        help='Directory for plot images.')
    parser.add_argument('--battery-kwh', type=float, default=BATTERY_CAPACITY_KWH,
                        help=f'Battery nameplate capacity [kWh] '
                             f'(default: {BATTERY_CAPACITY_KWH}).')
    parser.add_argument('--panels', type=int, default=N_PANELS,
                        help=f'Number of solar panels (default: {N_PANELS}).')
    parser.add_argument('--no-plots', action='store_true',
                        help='Skip plot generation.')
    parser.add_argument('--continuous-run', action='store_true',
                        help=(
                            'Enable continuous-run control mode.  Once the pump '
                            'starts for the day and then fails in a subsequent hour '
                            '(energy insufficient), it does NOT restart — modelling '
                            'a Tier 1 system where the farmer switches the pump on '
                            'and leaves.  Default: off (solar-following mode, pump '
                            'skips cloudy hours and resumes when conditions improve).'))
    args = parser.parse_args()

    # Apply continuous-run mode if requested
    global CONTINUOUS_RUN
    CONTINUOUS_RUN = args.continuous_run

    year    = args.year
    harv_yr = year + 1   # season spans Sep → May of next year

    mode_label = 'continuous-run (Tier 1)' if CONTINUOUS_RUN else 'solar-following (default)'
    print(f'\nLoading data for season {year}/{harv_yr} ({args.crop}) …')
    print(f'  Control mode: {mode_label}')

    # Load solar power for planting year AND harvest year (season spans 2 cal. years)
    solar_power = {}
    for yr in [year, harv_yr]:
        try:
            solar_power.update(load_solar_power(args.solar_dir, yr))
        except FileNotFoundError as e:
            print(f'  WARNING: {e}')

    if not solar_power:
        print('ERROR: No solar power data found. Run 1-solar-power/solar_analysis.py first.')
        sys.exit(1)

    # Load irrigation schedule
    weekly_sched = load_weekly_schedule(args.sched_dir, args.crop, year)
    print(f'  Weekly schedule: {len(weekly_sched)} weeks')

    # Build daily schedule from SWD-based daily CSV (preferred) or fall back
    # to the legacy equal-distribution method if the daily CSV is missing.
    try:
        daily_sched = load_daily_targets(args.sched_dir, args.crop, year)
        print(f'  Using SWD-based per-day targets from daily CSV.')
    except (FileNotFoundError, KeyError) as e:
        print(f'  WARNING: {e}')
        print(f'  Falling back to equal-distribution schedule.')
        daily_sched = build_daily_schedule(weekly_sched)
    total_irr_days = sum(1 for v in daily_sched.values() if v['irrigate'])
    print(f'  Irrigation days planned: {total_irr_days}')

    # Run simulation
    print(f'  Simulating with {args.panels} panels, '
          f'{args.battery_kwh:.1f} kWh battery …')
    hourly = simulate_season(solar_power, daily_sched,
                             args.battery_kwh, args.panels)

    if not hourly:
        print('ERROR: No hourly records produced. '
              'Check that solar data covers the growing season dates.')
        sys.exit(1)

    # Aggregate
    daily_res  = aggregate_daily(hourly, daily_sched)
    weekly_res = aggregate_weekly(daily_res, weekly_sched)

    # Report
    print_report(weekly_res, daily_res, year, args.crop,
                 args.battery_kwh, args.panels)

    # Save CSVs — each configuration gets its own subfolder so the results/
    # directory stays navigable even after many sweep runs.
    # Folder structure:  results/<crop>_<year>_p<panels>_b<battery>kWh/
    slug     = args.crop.replace('_', '-')
    mode_sfx = '_cr' if CONTINUOUS_RUN else ''
    tag      = f'{slug}_{year}_p{args.panels}_b{args.battery_kwh:.0f}kWh{mode_sfx}'
    out_dir  = os.path.join(args.output_dir, tag)
    os.makedirs(out_dir, exist_ok=True)
    save_csv(daily_res,
             os.path.join(out_dir, f'daily_energy_{tag}.csv'),
             'daily energy results')
    save_csv(weekly_res,
             os.path.join(out_dir, f'weekly_energy_{tag}.csv'),
             'weekly energy results')

    # Plots
    if not args.no_plots:
        sub_img = os.path.join(args.images_dir, tag)
        make_plots(hourly, daily_res, weekly_res,
                   year, args.crop, args.battery_kwh, sub_img)


if __name__ == '__main__':
    main()
