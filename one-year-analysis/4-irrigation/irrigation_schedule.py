"""
irrigation_schedule.py
======================
Reads daily ET0 data produced by fetch_et_data.py and computes crop water
requirements via the FAO-56 crop coefficient (Kc) approach.  For each
growing season found in the data it outputs:

  • A per-season summary with key water-balance statistics
  • A weekly irrigation schedule (days/week × hours/day — simple enough for
    a farmer to operate)
  • Flagged warnings for high-demand weeks, irrigation deficits, and
    extended dry spells
  • Two CSVs per season:  a weekly schedule file and a daily ETc file

Usage
-----
    python irrigation_schedule.py                          # cassava, all years
    python irrigation_schedule.py --crop tomato
    python irrigation_schedule.py --crop cassava --years 2020 2021
    python irrigation_schedule.py --list-crops

Crop name is passed as a plain string matching the keys in CROP_COEFFICIENTS.
Use --list-crops to see all available options.

References
----------
[FAO56]  Allen, R.G., Pereira, L.S., Raes, D., & Smith, M. (1998).
         FAO Irrigation and Drainage Paper No. 56 — Crop Evapotranspiration.
         FAO, Rome.
         Table 11: growth stage lengths.
         Table 12: single crop coefficients Kc.

[Smith]  Smith, M. (1992). CROPWAT — a computer program for irrigation
         planning and management. FAO Irrigation and Drainage Paper 46.
         (Effective rainfall methodology §3.4.)
"""

import argparse
import csv
import math
import os
import sys
from datetime import date, timedelta
from glob import glob

# ===========================================================================
# FARM AND PUMP PARAMETERS
# ===========================================================================

FARM_AREA_ACRES     = 0.78          # [acres]
FARM_AREA_M2        = FARM_AREA_ACRES * 4046.8564     # ≈ 3,157 m²

PUMP_FLOW_GPM       = 14.39         # [US gal/min] — flow delivered to drip field
N_DRIP_LINES        = 13            # number of drip lines
DRIP_EFFICIENCY     = 0.90          # fraction of applied water reaching root zone

# Derived pump flow quantities
_L_PER_GAL          = 3.785411784
PUMP_FLOW_L_HR      = PUMP_FLOW_GPM * _L_PER_GAL * 60.0   # [L/hr]  ≈ 3,268 L/hr
PUMP_FLOW_M3_HR     = PUMP_FLOW_L_HR / 1000.0              # [m³/hr] ≈ 3.268 m³/hr

# Gross application rate [mm/hr] — total water spread over the field per hour
GROSS_APP_RATE_MM_HR = PUMP_FLOW_M3_HR * 1000.0 / FARM_AREA_M2

# Net (effective) application rate [mm/hr] — water entering root zone per hour
NET_APP_RATE_MM_HR  = GROSS_APP_RATE_MM_HR * DRIP_EFFICIENCY

# Maximum pump run-hours per irrigation day — conservative estimate based on
# the solar system simulation in module 3-operating-hours (at 2 kWh battery
# capacity, the pump reliably runs during daytime hours).  This is used only
# to flag weeks that exceed capacity; the system model has authoritative data.
MAX_PUMP_HRS_PER_DAY = 8.0

# Effective rainfall coefficient — fraction of daily rainfall that enters the
# root zone and offsets irrigation.  FAO-56 §3.4 recommends site-specific
# data; 0.80 is a conservative planning value for well-drained soils.
EFF_RAIN_FACTOR = 0.80

# Maximum consecutive dry days before a drought warning is issued
DRY_SPELL_THRESHOLD_DAYS = 14
DRY_PRECIP_MM_DAY        = 2.0   # days with less than this count as "dry"

# ===========================================================================
# GROWING SEASON DEFAULTS
# ===========================================================================

PLANTING_MONTH  = 9    # September
PLANTING_DAY    = 1
HARVEST_MONTH   = 5    # May
HARVEST_DAY     = 31

# ===========================================================================
# CROP COEFFICIENT DATABASE  (FAO-56 Tables 11 & 12)
# ===========================================================================
# Each entry defines:
#   Kc_ini, Kc_mid, Kc_end  Single (non-stressed) crop coefficients.
#   L_ini, L_dev, L_mid, L_late  Stage lengths [days].
#
# The Kc values apply to sub-humid conditions (RHmin ≈ 45%, u2 ≈ 2 m/s).
# For Andros Island the climate is humid; if you wish to apply the FAO-56
# humidity/wind correction for Kc_mid and Kc_end, see FAO-56 eq. 70.

CROP_COEFFICIENTS: dict = {
    'cassava': {
        'description': 'Cassava – first year  (FAO-56 Table 12)',
        'notes'      : 'Kc values for first-year planting; matures in 9–18 months.',
        'Kc_ini' : 0.30,  'Kc_mid' : 0.80,  'Kc_end' : 0.30,
        'L_ini'  :   60,  'L_dev'  :   90,  'L_mid'  : 110,  'L_late': 60,
    },
    'tomato': {
        'description': 'Tomato  (FAO-56 Table 12)',
        'notes'      : 'Indeterminate variety; adjust L_mid for trellised crops.',
        'Kc_ini' : 0.60,  'Kc_mid' : 1.15,  'Kc_end' : 0.80,
        'L_ini'  :   30,  'L_dev'  :   40,  'L_mid'  :  40,  'L_late': 25,
    },
    'sweet_potato': {
        'description': 'Sweet Potato  (FAO-56 Table 12)',
        'notes'      : '',
        'Kc_ini' : 0.50,  'Kc_mid' : 1.15,  'Kc_end' : 0.65,
        'L_ini'  :   20,  'L_dev'  :   30,  'L_mid'  :  60,  'L_late': 40,
    },
    'pepper': {
        'description': 'Pepper / Capsicum  (FAO-56 Table 12)',
        'notes'      : '',
        'Kc_ini' : 0.60,  'Kc_mid' : 1.05,  'Kc_end' : 0.90,
        'L_ini'  :   30,  'L_dev'  :   40,  'L_mid'  :  40,  'L_late': 20,
    },
    'maize': {
        'description': 'Maize / Corn – grain  (FAO-56 Table 12)',
        'notes'      : '',
        'Kc_ini' : 0.30,  'Kc_mid' : 1.20,  'Kc_end' : 0.60,
        'L_ini'  :   20,  'L_dev'  :   35,  'L_mid'  :  40,  'L_late': 30,
    },
    'beans': {
        'description': 'Green / Dry Beans  (FAO-56 Table 12)',
        'notes'      : '',
        'Kc_ini' : 0.40,  'Kc_mid' : 1.15,  'Kc_end' : 0.35,
        'L_ini'  :   20,  'L_dev'  :   30,  'L_mid'  :  40,  'L_late': 20,
    },
    'watermelon': {
        'description': 'Watermelon  (FAO-56 Table 12)',
        'notes'      : '',
        'Kc_ini' : 0.40,  'Kc_mid' : 1.00,  'Kc_end' : 0.75,
        'L_ini'  :   20,  'L_dev'  :   30,  'L_mid'  :  30,  'L_late': 30,
    },
    'squash': {
        'description': 'Squash / Pumpkin  (FAO-56 Table 12)',
        'notes'      : '',
        'Kc_ini' : 0.50,  'Kc_mid' : 1.00,  'Kc_end' : 0.80,
        'L_ini'  :   25,  'L_dev'  :   35,  'L_mid'  :  25,  'L_late': 15,
    },
    'okra': {
        'description': 'Okra  (FAO-56 Table 12 / supplemental sources)',
        'notes'      : 'Stage lengths are indicative; limited FAO-56 data available.',
        'Kc_ini' : 0.45,  'Kc_mid' : 1.05,  'Kc_end' : 0.60,
        'L_ini'  :   20,  'L_dev'  :   35,  'L_mid'  :  45,  'L_late': 20,
    },
    'sorghum': {
        'description': 'Sorghum – grain  (FAO-56 Table 12)',
        'notes'      : '',
        'Kc_ini' : 0.30,  'Kc_mid' : 1.00,  'Kc_end' : 0.55,
        'L_ini'  :   20,  'L_dev'  :   35,  'L_mid'  :  40,  'L_late': 30,
    },
    'grass': {
        'description': 'Reference grass (ET0 baseline — Kc ≡ 1.0 throughout)',
        'notes'      : 'Use to validate ET0 values; ETc == ET0 every day.',
        'Kc_ini' : 1.00,  'Kc_mid' : 1.00,  'Kc_end' : 1.00,
        'L_ini'  :   30,  'L_dev'  :   30,  'L_mid'  : 150,  'L_late': 30,
    },
}

# ===========================================================================
# DEFAULT PATHS
# ===========================================================================

_HERE           = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR   = os.path.join(_HERE, 'et-data')
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, 'results')


# ===========================================================================
# CROP COEFFICIENT CURVE
# ===========================================================================

def kc_for_day(day_in_season: int, crop: dict):
    """
    Return the Kc value for a given day within the growing season (1-based).

    Uses the FAO-56 §6.1 piecewise-linear Kc curve:
      • Initial  (days 1 … L_ini):                     Kc = Kc_ini
      • Development (L_ini+1 … L_ini+L_dev):           linear Kc_ini → Kc_mid
      • Mid-season  (L_ini+L_dev+1 … +L_mid):          Kc = Kc_mid
      • Late-season (L_ini+L_dev+L_mid+1 … L_total):   linear Kc_mid → Kc_end

    Returns None if day_in_season is beyond L_total.
    """
    L_ini  = crop['L_ini']
    L_dev  = crop['L_dev']
    L_mid  = crop['L_mid']
    L_late = crop['L_late']
    L_total = L_ini + L_dev + L_mid + L_late

    Kc_ini = crop['Kc_ini']
    Kc_mid = crop['Kc_mid']
    Kc_end = crop['Kc_end']

    if day_in_season < 1 or day_in_season > L_total:
        return None

    if day_in_season <= L_ini:
        return Kc_ini
    elif day_in_season <= L_ini + L_dev:
        frac = (day_in_season - L_ini) / L_dev
        return Kc_ini + frac * (Kc_mid - Kc_ini)
    elif day_in_season <= L_ini + L_dev + L_mid:
        return Kc_mid
    else:
        frac = (day_in_season - L_ini - L_dev - L_mid) / L_late
        return Kc_mid + frac * (Kc_end - Kc_mid)


# ===========================================================================
# DATA LOADING
# ===========================================================================

def read_et_csv(csv_path: str) -> list:
    """Load one et_data_YYYY.csv and return a list of row dicts."""
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            rows.append({
                'date'         : row['date'],
                'ET0_mm_day'   : float(row['ET0_mm_day']),
                'precip_mm_day': float(row['precip_mm_day']),
                'Tmean_C'      : float(row['Tmean_C']),
                'Tmax_C'       : float(row['Tmax_C']),
            })
    return rows


def load_data(data_dir: str, years: list = None) -> list:
    """
    Load all et_data_YYYY.csv files from data_dir.
    If years is provided, restrict to those years.
    Returns a flat list of row dicts sorted by date.
    """
    files = sorted(glob(os.path.join(data_dir, 'et_data_*.csv')))
    if not files:
        return []

    all_rows = []
    for fpath in files:
        yr = int(os.path.basename(fpath).replace('et_data_', '').replace('.csv', ''))
        if years and yr not in years:
            continue
        rows = read_et_csv(fpath)
        print(f"  Loaded {len(rows)} days from {os.path.basename(fpath)}")
        all_rows.extend(rows)

    return sorted(all_rows, key=lambda r: r['date'])


# ===========================================================================
# IRRIGATION SCHEDULING HELPERS
# ===========================================================================

def schedule_for_weekly_need(net_mm: float):
    """
    Given the net weekly irrigation requirement [mm], return a simple
    farm-operable schedule as (days_per_week, hours_per_day).

    Algorithm:
      1. Compute total hours needed = net_mm / NET_APP_RATE_MM_HR
      2. Choose the minimum number of irrigation days from {2, 3, 4, 5, 6, 7}
         such that hours_per_day ≤ MAX_PUMP_HRS_PER_DAY.
      3. Round hours_per_day UP to the nearest 0.5 h.

    Returns (0, 0.0) when net_mm ≤ 0 (rainfall is sufficient).
    """
    if net_mm <= 0.0:
        return 0, 0.0

    total_hrs = net_mm / NET_APP_RATE_MM_HR

    for n_days in range(2, 8):          # 2 … 7 days/week
        hrs = total_hrs / n_days
        if hrs <= MAX_PUMP_HRS_PER_DAY:
            hrs_rounded = math.ceil(hrs * 2.0) / 2.0   # round up to ½ h
            return n_days, hrs_rounded

    # If 7 days at MAX is still not enough, cap and let the caller flag it
    return 7, MAX_PUMP_HRS_PER_DAY


def capacity_mm(n_days: int, hrs_per_day: float) -> float:
    """Maximum irrigation [mm] deliverable with this schedule."""
    return n_days * hrs_per_day * NET_APP_RATE_MM_HR


def suggested_day_names(n_days: int) -> str:
    """Human-readable irrigation day suggestion."""
    schedules = {
        2: 'Mon, Thu',
        3: 'Mon, Wed, Fri',
        4: 'Mon, Tue, Thu, Fri',
        5: 'Mon, Tue, Wed, Thu, Fri',
        6: 'Mon–Sat',
        7: 'every day',
    }
    return schedules.get(n_days, f'{n_days} days/week')


# ===========================================================================
# GROWING SEASON BUILDER
# ===========================================================================

def season_bounds(planting_year: int,
                  planting_month: int, planting_day: int,
                  harvest_month: int, harvest_day: int):
    """
    Return (plant_date, harvest_date) for a season starting in planting_year.
    If harvest_month < planting_month, harvest falls in the following year.
    """
    plant = date(planting_year, planting_month, planting_day)
    harv_year = planting_year + 1 if harvest_month < planting_month else planting_year
    harv = date(harv_year, harvest_month, harvest_day)
    return plant, harv


# ===========================================================================
# MAIN SEASONAL ANALYSIS
# ===========================================================================

def analyze_season(data: dict, crop: dict,
                   plant_date: date, harv_date: date):
    """
    Walk through the growing season day-by-day, compute Kc and ETc,
    and return (daily_rows, weekly_rows) lists.

    Parameters
    ----------
    data       : {date_str: row_dict} from the ET data CSVs
    crop       : entry from CROP_COEFFICIENTS
    plant_date : first day of growing season
    harv_date  : last day of growing season
    """
    L_total = (crop['L_ini'] + crop['L_dev']
               + crop['L_mid'] + crop['L_late'])

    daily_rows = []
    cur = plant_date
    day_num = 1

    while cur <= harv_date and day_num <= L_total:
        d_str = cur.isoformat()
        if d_str in data:
            row = data[d_str]
            ET0    = row['ET0_mm_day']
            precip = row['precip_mm_day']
            Kc     = kc_for_day(day_num, crop)
            if Kc is None:
                Kc = crop['Kc_end']
            ETc        = Kc * ET0
            eff_precip = EFF_RAIN_FACTOR * precip
            net_irr    = max(0.0, ETc - eff_precip)

            daily_rows.append({
                'date'          : d_str,
                'day_in_season' : day_num,
                'growth_stage'  : _stage_name(day_num, crop),
                'Kc'            : round(Kc, 3),
                'ET0_mm'        : round(ET0, 2),
                'ETc_mm'        : round(ETc, 2),
                'precip_mm'     : round(precip, 2),
                'eff_precip_mm' : round(eff_precip, 2),
                'net_irr_mm'    : round(net_irr, 2),
            })

        cur    += timedelta(days=1)
        day_num += 1

    if not daily_rows:
        return [], []

    # --- Aggregate to weeks ---
    weekly_rows = []
    for i in range(0, len(daily_rows), 7):
        chunk = daily_rows[i: i + 7]
        w_start = chunk[0]['date']
        w_end   = chunk[-1]['date']
        n       = len(chunk)

        ETc_wk    = sum(d['ETc_mm']        for d in chunk)
        ET0_wk    = sum(d['ET0_mm']        for d in chunk)
        precip_wk = sum(d['precip_mm']     for d in chunk)
        eff_p_wk  = sum(d['eff_precip_mm'] for d in chunk)
        net_wk    = sum(d['net_irr_mm']    for d in chunk)
        Kc_mean   = sum(d['Kc']            for d in chunk) / n
        stage     = chunk[n // 2]['growth_stage']   # midpoint of the week

        n_days, hrs_day = schedule_for_weekly_need(net_wk)
        cap = capacity_mm(n_days, hrs_day)
        deficit = max(0.0, net_wk - cap)

        weekly_rows.append({
            'week_start'    : w_start,
            'week_end'      : w_end,
            'n_days_data'   : n,
            'growth_stage'  : stage,
            'Kc_mean'       : round(Kc_mean, 3),
            'ET0_mm'        : round(ET0_wk,  1),
            'ETc_mm'        : round(ETc_wk,  1),
            'precip_mm'     : round(precip_wk, 1),
            'eff_precip_mm' : round(eff_p_wk,  1),
            'net_irr_mm'    : round(net_wk,    1),
            'irr_days_week' : n_days,
            'hrs_per_day'   : hrs_day,
            'capacity_mm'   : round(cap,     1),
            'deficit_mm'    : round(deficit, 1),
        })

    return daily_rows, weekly_rows


def _stage_name(day: int, crop: dict) -> str:
    """Return a short growth stage label for a given day in the season."""
    L_ini  = crop['L_ini']
    L_dev  = crop['L_dev']
    L_mid  = crop['L_mid']
    L_late = crop['L_late']
    if day <= L_ini:
        return 'Initial'
    elif day <= L_ini + L_dev:
        return 'Development'
    elif day <= L_ini + L_dev + L_mid:
        return 'Mid-season'
    else:
        return 'Late'


# ===========================================================================
# REPORTING
# ===========================================================================

def print_report(plant_yr: int, crop: dict, daily_rows: list,
                 weekly_rows: list, crop_name: str):
    """Print a human-readable irrigation schedule and warnings."""

    # ── Summary statistics ─────────────────────────────────────────────────
    n = len(daily_rows)
    harv_yr = int(weekly_rows[-1]['week_end'][:4]) if weekly_rows else plant_yr + 1
    total_ETc  = sum(d['ETc_mm']        for d in daily_rows)
    total_rain = sum(d['precip_mm']     for d in daily_rows)
    total_effp = sum(d['eff_precip_mm'] for d in daily_rows)
    total_net  = sum(d['net_irr_mm']    for d in daily_rows)
    avg_ET0    = sum(d['ET0_mm']        for d in daily_rows) / n
    avg_ETc    = total_ETc / n
    L_total    = crop['L_ini'] + crop['L_dev'] + crop['L_mid'] + crop['L_late']
    total_pump_hrs = total_net / NET_APP_RATE_MM_HR

    SEP = '=' * 72
    print(f"\n{SEP}")
    print(f"  IRRIGATION SCHEDULE  —  Season {plant_yr}/{harv_yr}")
    print(f"  Crop: {crop['description']}")
    print(SEP)
    print(f"  Farm area         : {FARM_AREA_ACRES} acres  ({FARM_AREA_M2:.0f} m²)")
    print(f"  Pump flow         : {PUMP_FLOW_GPM} GPM  "
          f"({PUMP_FLOW_M3_HR:.3f} m³/hr,  {PUMP_FLOW_L_HR:.0f} L/hr)")
    print(f"  Drip lines        : {N_DRIP_LINES}")
    print(f"  Drip efficiency   : {DRIP_EFFICIENCY * 100:.0f} %")
    print(f"  Gross app. rate   : {GROSS_APP_RATE_MM_HR:.3f} mm/hr")
    print(f"  Net app. rate     : {NET_APP_RATE_MM_HR:.3f} mm/hr  "
          f"(at {DRIP_EFFICIENCY * 100:.0f} % efficiency)")
    print()
    print(f"  Planting          : {daily_rows[0]['date']}")
    print(f"  Last data day     : {daily_rows[-1]['date']}")
    print(f"  Days with data    : {n}  (crop total: {L_total} days)")
    print()
    print(f"  Mean ET0          : {avg_ET0:.2f} mm/day")
    print(f"  Mean ETc (Kc×ET0) : {avg_ETc:.2f} mm/day")
    print(f"  Season ETc        : {total_ETc:.0f} mm")
    print(f"  Season rainfall   : {total_rain:.0f} mm  "
          f"(effective: {total_effp:.0f} mm  @ {EFF_RAIN_FACTOR * 100:.0f} % factor)")
    print(f"  Net irrigation    : {total_net:.0f} mm  for the season")
    print(f"  Pump-hours needed : {total_pump_hrs:.0f} hrs  for the season")
    print()

    # ── Recommended base schedule (median non-zero week) ──────────────────
    non_zero = sorted(w['net_irr_mm'] for w in weekly_rows if w['net_irr_mm'] > 0)
    if non_zero:
        median_net = non_zero[len(non_zero) // 2]
        base_days, base_hrs = schedule_for_weekly_need(median_net)
        print(f"  ── Recommended base schedule (median demand: "
              f"{median_net:.0f} mm/week) ──")
        if base_days == 0:
            print(f"     Rainfall typically sufficient; irrigate only during dry spells.")
        else:
            print(f"     {base_days} irrigation day(s) per week  ×  {base_hrs:.1f} hrs/day")
            print(f"     Suggested days: {suggested_day_names(base_days)}")
            print(f"     Effective supply: {capacity_mm(base_days, base_hrs):.0f} mm/week")
    print()

    # ── Weekly table ──────────────────────────────────────────────────────
    W  = 24   # column widths
    header = (f"  {'Wk':>3}  {'Period':<22}  {'Stage':<12}  "
              f"{'ETc':>5}  {'Rain':>5}  {'Net':>5}  "
              f"{'Schedule':<20}  Notes")
    print(header)
    print('  ' + '-' * (len(header) - 2))

    warnings = []     # collect (date_str, message) tuples
    for idx, w in enumerate(weekly_rows, start=1):
        sch_str = (f"{w['irr_days_week']}d × {w['hrs_per_day']:.1f}h"
                   if w['irr_days_week'] > 0 else "no irrigation")

        flags = []
        if w['deficit_mm'] > 0.5:
            flags.append(f"⚠ DEFICIT {w['deficit_mm']:.0f}mm")
            warnings.append((w['week_start'],
                             f"Irrigation deficit: need {w['net_irr_mm']:.0f}mm, "
                             f"capacity {w['capacity_mm']:.0f}mm  "
                             f"(gap = {w['deficit_mm']:.0f}mm)"))
        if w['ETc_mm'] / max(w['n_days_data'], 1) > 7.0:
            flags.append("☀ HIGH ET")
            warnings.append((w['week_start'],
                             f"High ETc: {w['ETc_mm']:.0f}mm this week "
                             f"({w['ETc_mm']/w['n_days_data']:.1f}mm/day avg)"))
        if w['net_irr_mm'] <= 0.5 and w['precip_mm'] > 25:
            flags.append("🌧 RAIN COVERS")

        note_str = ', '.join(flags)
        period   = f"{w['week_start'][5:]} – {w['week_end'][5:]}"   # MM-DD
        print(f"  {idx:>3}  {period:<22}  {w['growth_stage']:<12}  "
              f"{w['ETc_mm']:>5.1f}  {w['precip_mm']:>5.1f}  "
              f"{w['net_irr_mm']:>5.1f}  {sch_str:<20}  {note_str}")

    # ── Warnings block ────────────────────────────────────────────────────
    if warnings:
        print(f"\n  ⚠  WARNINGS ({len(warnings)} events)")
        print('  ' + '-' * 65)
        seen: set = set()
        for d_str, msg in warnings:
            key = msg[:50]
            if key not in seen:
                print(f"    {d_str}  {msg}")
                seen.add(key)

    # ── Dry spell alerts ──────────────────────────────────────────────────
    print(f"\n  DRY SPELL ALERTS  (>{DRY_SPELL_THRESHOLD_DAYS} consecutive "
          f"days with <{DRY_PRECIP_MM_DAY:.0f}mm/day rain):")
    dry_start = None
    dry_len   = 0
    found_dry = False
    for d in daily_rows:
        if d['precip_mm'] < DRY_PRECIP_MM_DAY:
            if dry_start is None:
                dry_start = d['date']
            dry_len += 1
        else:
            if dry_len >= DRY_SPELL_THRESHOLD_DAYS:
                print(f"    {dry_start}  through  {d['date']}: "
                      f"{dry_len} consecutive dry days")
                found_dry = True
            dry_start, dry_len = None, 0
    # Catch a dry spell running to the end of the season
    if dry_len >= DRY_SPELL_THRESHOLD_DAYS:
        print(f"    {dry_start}  through season end: {dry_len} consecutive dry days")
        found_dry = True
    if not found_dry:
        print(f"    None detected.")

    print(f"\n{SEP}")


# ===========================================================================
# CSV OUTPUT
# ===========================================================================

def save_csv(rows: list, path: str, label: str):
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {label}: {path}")


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            'FAO-56 crop water requirement and irrigation scheduling.\n'
            'Reads daily ET data CSVs produced by fetch_et_data.py,\n'
            'applies crop coefficients, and outputs weekly schedules.'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Examples:\n'
            '  python irrigation_schedule.py                     '
            '# cassava, all seasons\n'
            '  python irrigation_schedule.py --crop tomato\n'
            '  python irrigation_schedule.py --crop cassava --years 2020 2021\n'
            '  python irrigation_schedule.py --list-crops\n'
        ),
    )
    parser.add_argument(
        '--data-dir', default=DEFAULT_DATA_DIR,
        help=f'Directory containing et_data_YYYY.csv files '
             f'(default: {DEFAULT_DATA_DIR}).')
    parser.add_argument(
        '--output-dir', default=DEFAULT_OUTPUT_DIR,
        help=f'Directory for output CSVs (default: {DEFAULT_OUTPUT_DIR}).')
    parser.add_argument(
        '--crop', default='cassava',
        choices=list(CROP_COEFFICIENTS.keys()),
        metavar='CROP',
        help='Crop name (default: cassava).  Use --list-crops to see options.')
    parser.add_argument(
        '--years', type=int, nargs='+', metavar='YYYY',
        default=None,
        help='Planting years to analyse (default: all years in data directory).')
    parser.add_argument(
        '--planting-month', type=int, default=PLANTING_MONTH, metavar='M',
        help=f'Planting month (default: {PLANTING_MONTH} = September).')
    parser.add_argument(
        '--planting-day', type=int, default=PLANTING_DAY, metavar='D',
        help=f'Planting day of month (default: {PLANTING_DAY}).')
    parser.add_argument(
        '--harvest-month', type=int, default=HARVEST_MONTH, metavar='M',
        help=f'Harvest month (default: {HARVEST_MONTH} = May).')
    parser.add_argument(
        '--harvest-day', type=int, default=HARVEST_DAY, metavar='D',
        help=f'Harvest day of month (default: {HARVEST_DAY}).')
    parser.add_argument(
        '--list-crops', action='store_true',
        help='Print available crop names and exit.')
    args = parser.parse_args()

    # ── List crops ────────────────────────────────────────────────────────
    if args.list_crops:
        print(f"{'Crop name':<16}  {'Total days':>10}  Description")
        print('-' * 70)
        for name, c in CROP_COEFFICIENTS.items():
            L = c['L_ini'] + c['L_dev'] + c['L_mid'] + c['L_late']
            note = f"  [{c['notes']}]" if c['notes'] else ''
            print(f"  {name:<14}  {L:>10}  {c['description']}{note}")
        return

    crop = CROP_COEFFICIENTS[args.crop]

    # ── Load data ─────────────────────────────────────────────────────────
    print(f"\nLoading ET data from: {os.path.abspath(args.data_dir)}")
    all_rows = load_data(args.data_dir, args.years)
    if not all_rows:
        print(f"\nERROR: No ET data files found in: {args.data_dir}")
        print("Run fetch_et_data.py first.")
        sys.exit(1)

    # Build a fast date-lookup dict
    data_by_date: dict = {r['date']: r for r in all_rows}

    # Determine planting years from available data
    all_data_years = sorted({int(r['date'][:4]) for r in all_rows})
    planting_years = args.years if args.years else all_data_years

    os.makedirs(args.output_dir, exist_ok=True)
    crop_slug = args.crop.replace('_', '-')

    # ── Process each growing season ───────────────────────────────────────
    for py in planting_years:
        plant_date, harv_date = season_bounds(
            py,
            args.planting_month, args.planting_day,
            args.harvest_month,  args.harvest_day,
        )

        # Filter data to this growing season
        season_data = {
            d: r for d, r in data_by_date.items()
            if plant_date.isoformat() <= d <= harv_date.isoformat()
        }
        if len(season_data) < 30:
            print(f"\n  Season {py}/{py+1}: only {len(season_data)} days of data — skipping.")
            continue

        print(f"\n  Analysing season {py}/{py+1}  "
              f"({plant_date} → {harv_date}) ...")

        daily_rows, weekly_rows = analyze_season(
            season_data, crop, plant_date, harv_date)

        if not daily_rows:
            print(f"  No daily rows produced for season {py} — skipping.")
            continue

        print_report(py, crop, daily_rows, weekly_rows, args.crop)

        # Save CSVs
        save_csv(weekly_rows,
                 os.path.join(args.output_dir, f"weekly_{crop_slug}_{py}.csv"),
                 'weekly schedule')
        save_csv(daily_rows,
                 os.path.join(args.output_dir, f"daily_{crop_slug}_{py}.csv"),
                 'daily ETc')

    print(f"\nResults written to: {os.path.abspath(args.output_dir)}")


if __name__ == '__main__':
    main()
