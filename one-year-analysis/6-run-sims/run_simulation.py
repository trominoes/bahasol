"""
run_simulation.py
=================
Master script for the BahaSol one-year analysis pipeline.

Runs any combination of the four analysis modules (solar → pump/battery →
irrigation → integrated) with fully configurable system parameters.  Edit
the CONFIG block below — or pass flags on the command line — to explore
different system configurations and find the minimum viable product.

Pipeline stages
---------------
  STAGE 1 — Solar power        (1-solar-power/solar_analysis.py)
    Reads NSRDB CSVs, computes hourly DC power for the given array.

  STAGE 2 — Pump / battery     (3-operating-hours-available/battery_pump_analysis.py)
    Simulates the hybrid inverter + battery + pump system at hourly resolution
    and reports total pump run-hours, energy balance, and failure modes.
    (Greedy mode: pump runs whenever solar/battery allows, up to MAX_HRS_PER_DAY.)

  STAGE 3 — Irrigation         (4-irrigation/irrigation_schedule.py)
    Computes daily ETc via FAO-56 Penman-Monteith, derives a weekly irrigation
    schedule, and flags high-demand / dry-spell weeks.
    Requires ET data CSVs from 4-irrigation/fetch_et_data.py (run once separately).

  STAGE 4 — Integrated         (5-integrated-analysis/integrated_analysis.py)
    Checks whether the solar/battery system can supply the pump for the
    specific irrigation schedule derived in Stage 3.  Flags energy shortfalls.

Usage
-----
    python run_simulation.py                        # run all stages, default config
    python run_simulation.py --stages 1 2           # only solar + pump/battery
    python run_simulation.py --stages 3 4           # only irrigation + integrated
    python run_simulation.py --panels 20 --battery-kwh 5 --crop tomato
    python run_simulation.py --year 2021
    python run_simulation.py --years 2019 2020 2021

Parameter sweeps (MVP search):
    python run_simulation.py --sweep-panels 10 12 15 18 20
    python run_simulation.py --sweep-battery 0 1 2 3 5
    python run_simulation.py --sweep-panels 12 15 18 --sweep-battery 1 2 3

All outputs are written to  output/<tag>/  inside this folder.
"""

import argparse
import importlib.util
import os
import sys
import csv
import math
from datetime import date
from itertools import product as iterproduct

# ===========================================================================
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                          CONFIGURATION BLOCK                            ║
# ║  Edit these values to reconfigure the system without touching flags.    ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# ===========================================================================

CONFIG = {

    # ── Analysis scope ──────────────────────────────────────────────────────
    'years'           : [2018, 2019, 2020, 2021, 2022, 2023, 2024],
    'crop'            : 'cassava',      # crop name (see 4-irrigation for list)
    'planting_month'  : 9,              # September
    'planting_day'    : 1,
    'harvest_month'   : 5,              # May
    'harvest_day'     : 31,

    # ── Solar array ──────────────────────────────────────────────────────────
    'n_panels'        : 15,             # number of panels
    'panel_watt'      : 405.0,          # rated power per panel at STC [W]
    'panel_eff'       : 0.207,          # module efficiency (20.7%)
    'panel_length_m'  : 1.722,          # panel length [m]
    'panel_width_m'   : 1.134,          # panel width  [m]
    'temp_coeff_pmax' : -0.0035,        # Pmax temperature coefficient [1/°C]
    'noct'            : 45.0,           # Nominal Operating Cell Temperature [°C]
    'tilt_deg'        : 24.0,           # panel tilt from horizontal [°]
    'azimuth_deg'     : 180.0,          # panel azimuth (180 = due south)
    'performance_ratio': 0.85,          # PR: wiring / mismatch / soiling losses

    # ── Battery ───────────────────────────────────────────────────────────────
    'battery_kwh'     : 2.0,            # nameplate capacity [kWh]  (0 = no battery)
    'battery_min_soc' : 0.10,           # minimum allowed SoC [fraction]
    'battery_max_soc' : 1.00,           # maximum SoC [fraction]
    'battery_charge_eff'   : 0.95,      # charge efficiency (AC → stored kWh)
    'battery_discharge_eff': 0.95,      # discharge efficiency (stored → AC)

    # ── Inverter ──────────────────────────────────────────────────────────────
    'inverter_eff'    : 0.96,           # DC → AC conversion efficiency

    # ── Pump ─────────────────────────────────────────────────────────────────
    'pump_power_kw'   : 1.263,          # AC power draw [kW]  (all-or-nothing load)
    'pump_flow_gpm'   : 14.39,          # irrigation flow rate [US gal/min]
    'n_drip_lines'    : 13,             # number of drip lines
    'drip_efficiency' : 0.90,           # drip irrigation application efficiency

    # ── Greedy pump schedule (Stage 2) ────────────────────────────────────────
    'greedy_schedule_days': 'all',      # 'all' or list of ISO weekday ints e.g. [1,3,5]
    'greedy_max_hrs_day'  : 6,          # max pump run-hours per scheduled day (Stage 2)
    'pump_start_hour'     : 0,          # earliest hour pump may start (0 = no limit)
    'min_solar_discharge' : 0.10,       # kW AC floor before battery discharges

    # ── Farm ─────────────────────────────────────────────────────────────────
    'farm_acres'      : 0.78,           # farm area [acres]
    'latitude_deg'    : 24.96,          # [°N]
    'longitude_deg'   : -78.05,         # [°E, negative = West]
    'elevation_m'     : 9.0,            # [m]
    'albedo'          : 0.20,           # ground reflectance

    # ── Effective rainfall ────────────────────────────────────────────────────
    'eff_rain_factor' : 0.80,           # fraction of rainfall entering root zone

}

# ===========================================================================
# PATH SETUP
# ===========================================================================

_HERE       = os.path.dirname(os.path.abspath(__file__))
_ROOT       = os.path.abspath(os.path.join(_HERE, '..'))
_NSRDB_DIR  = os.path.join(_ROOT, '1-solar-power', 'NSRDB-raw')
_POWER_DIR  = os.path.join(_ROOT, '1-solar-power', 'gen-power')
_ET_DIR     = os.path.join(_ROOT, '4-irrigation', 'et-data')
_IRR_DIR    = os.path.join(_ROOT, '4-irrigation', 'results')
_SYS_DIR    = os.path.join(_ROOT, '3-operating-hours-available', 'battery-pump')
_OUT_ROOT   = os.path.join(_HERE, 'output')


def _out(tag: str, *parts) -> str:
    path = os.path.join(_OUT_ROOT, tag, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


# ===========================================================================
# MODULE LOADER — import sibling scripts without installing them
# ===========================================================================

def _load_module(name: str, fpath: str):
    """Dynamically load a Python file as a module."""
    spec = importlib.util.spec_from_file_location(name, fpath)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# STAGE 1 — SOLAR POWER
# ===========================================================================

def run_stage1(cfg: dict, years: list, tag: str) -> dict:
    """
    Compute hourly solar power for each year using solar_analysis.py.
    Writes gen-power CSVs to a tagged output subdirectory.
    Returns {year: results_list}.
    """
    print(f'\n{"─"*60}')
    print(f'  STAGE 1 — Solar Power  ({cfg["n_panels"]} panels × '
          f'{cfg["panel_watt"]:.0f}W, tilt {cfg["tilt_deg"]}°, '
          f'azimuth {cfg["azimuth_deg"]}°)')
    print(f'{"─"*60}')

    solar_mod = _load_module('solar_analysis',
                             os.path.join(_ROOT, '1-solar-power', 'solar_analysis.py'))

    # Patch module-level constants with config values
    solar_mod.N_PANELS            = cfg['n_panels']
    solar_mod.PANEL_RATED_POWER_W = cfg['panel_watt']
    solar_mod.PANEL_EFFICIENCY    = cfg['panel_eff']
    solar_mod.PANEL_LENGTH_M      = cfg['panel_length_m']
    solar_mod.PANEL_WIDTH_M       = cfg['panel_width_m']
    solar_mod.PANEL_AREA_M2       = cfg['panel_length_m'] * cfg['panel_width_m']
    solar_mod.ARRAY_RATED_POWER_W = cfg['n_panels'] * cfg['panel_watt']
    solar_mod.TEMP_COEFF_PMAX     = cfg['temp_coeff_pmax']
    solar_mod.NOCT                = cfg['noct']
    solar_mod.TILT_DEG            = cfg['tilt_deg']
    solar_mod.AZIMUTH_DEG         = cfg['azimuth_deg']
    solar_mod.LATITUDE_DEG        = cfg['latitude_deg']
    solar_mod.LONGITUDE_DEG       = cfg['longitude_deg']
    solar_mod.ALBEDO              = cfg['albedo']
    solar_mod.PERFORMANCE_RATIO   = cfg['performance_ratio']

    out_dir = _out(tag, 'gen-power')
    all_results = {}
    for yr in years:
        pattern = os.path.join(_NSRDB_DIR, f'*_{yr}.csv')
        from glob import glob
        files = glob(pattern)
        if not files:
            print(f'  WARNING: No NSRDB file for {yr} — skipping Stage 1 for this year.')
            continue
        stem   = os.path.splitext(os.path.basename(files[0]))[0]
        out_csv = os.path.join(out_dir, f'{stem}_power.csv')
        print(f'  Processing {yr} …')
        results = solar_mod.analyze_solar_power(files[0], out_csv)
        all_results[yr] = results
        annual_kwh = sum(r['P_dc_kW'] for r in results)
        peak_kw    = max(r['P_dc_kW'] for r in results)
        print(f'    Annual DC energy: {annual_kwh:,.0f} kWh  |  '
              f'Peak: {peak_kw:.3f} kW')
    return all_results


# ===========================================================================
# STAGE 2 — PUMP / BATTERY (GREEDY)
# ===========================================================================

def run_stage2(cfg: dict, years: list, tag: str, power_dir: str = None) -> dict:
    """
    Run the greedy battery+pump simulation for each year.
    Returns {year: DataFrame of simulation results}.
    """
    print(f'\n{"─"*60}')
    print(f'  STAGE 2 — Pump/Battery Simulation  '
          f'(battery {cfg["battery_kwh"]:.1f}kWh, '
          f'pump {cfg["pump_power_kw"]:.3f}kW)')
    print(f'{"─"*60}')

    batt_mod = _load_module(
        'battery_pump_analysis',
        os.path.join(_ROOT, '3-operating-hours-available', 'battery_pump_analysis.py'))

    # Patch constants
    batt_mod.BATTERY_CAPACITY_KWH    = cfg['battery_kwh']
    batt_mod.BATTERY_MIN_SOC_PCT     = cfg['battery_min_soc']
    batt_mod.BATTERY_MAX_SOC_PCT     = cfg['battery_max_soc']
    batt_mod.BATTERY_INITIAL_SOC_PCT = 1.00
    batt_mod.BATTERY_CHARGE_EFF      = cfg['battery_charge_eff']
    batt_mod.BATTERY_DISCHARGE_EFF   = cfg['battery_discharge_eff']
    batt_mod.INVERTER_EFF            = cfg['inverter_eff']
    batt_mod.PUMP_POWER_KW           = cfg['pump_power_kw']
    batt_mod.MIN_SOLAR_FOR_DISCHARGE_KW = cfg['min_solar_discharge']
    batt_mod.SCHEDULE_DAYS           = cfg['greedy_schedule_days']
    batt_mod.MAX_HOURS_PER_DAY       = cfg['greedy_max_hrs_day']
    batt_mod.PUMP_START_HOUR         = cfg['pump_start_hour']

    src_dir = power_dir or _POWER_DIR
    out_dir = _out(tag, 'battery-pump')
    img_dir = _out(tag, 'images-stage2')

    all_results = {}
    import pandas as pd
    for yr in years:
        from glob import glob
        files = glob(os.path.join(src_dir, f'*_{yr}_power.csv'))
        if not files:
            print(f'  WARNING: No power CSV for {yr} — skipping Stage 2.')
            continue
        stem    = os.path.splitext(os.path.basename(files[0]))[0].replace('_power', '')
        out_csv = os.path.join(out_dir, f'{stem}_system.csv')
        batt_mod.INPUT_CSV  = files[0]
        batt_mod.OUTPUT_CSV = out_csv
        batt_mod.IMAGES_DIR = os.path.join(img_dir, str(yr))
        batt_mod.YEAR       = yr
        print(f'  Simulating {yr} …')
        # battery_pump_analysis.py exposes run(input_csv, output_csv, images_dir)
        img_yr = os.path.join(img_dir, str(yr))
        os.makedirs(img_yr, exist_ok=True)
        df = batt_mod.run(files[0], out_csv, img_yr)
        all_results[yr] = df
        try:
            pump_hrs = int(df['pump_on'].sum())
        except Exception:
            pump_hrs = 0
        print(f'    Total pump-hours: {pump_hrs:.0f}  |  Output: {out_csv}')
    return all_results


# ===========================================================================
# STAGE 3 — IRRIGATION SCHEDULING
# ===========================================================================

def run_stage3(cfg: dict, years: list, tag: str) -> dict:
    """
    Compute FAO-56 ETc and weekly irrigation schedules.
    Returns {year: (daily_rows, weekly_rows)}.
    """
    print(f'\n{"─"*60}')
    print(f'  STAGE 3 — Irrigation Scheduling  '
          f'(crop: {cfg["crop"]}, season: Sep→May)')
    print(f'{"─"*60}')

    irr_mod = _load_module(
        'irrigation_schedule',
        os.path.join(_ROOT, '4-irrigation', 'irrigation_schedule.py'))

    # Patch farm / pump constants
    irr_mod.FARM_AREA_ACRES   = cfg['farm_acres']
    irr_mod.FARM_AREA_M2      = cfg['farm_acres'] * 4046.8564
    irr_mod.PUMP_FLOW_GPM     = cfg['pump_flow_gpm']
    irr_mod.N_DRIP_LINES      = cfg['n_drip_lines']
    irr_mod.DRIP_EFFICIENCY   = cfg['drip_efficiency']
    # Recompute derived quantities
    _L_PER_GAL = 3.785411784
    irr_mod.PUMP_FLOW_L_HR      = cfg['pump_flow_gpm'] * _L_PER_GAL * 60
    irr_mod.PUMP_FLOW_M3_HR     = irr_mod.PUMP_FLOW_L_HR / 1000
    irr_mod.GROSS_APP_RATE_MM_HR = irr_mod.PUMP_FLOW_M3_HR * 1000 / irr_mod.FARM_AREA_M2
    irr_mod.NET_APP_RATE_MM_HR  = irr_mod.GROSS_APP_RATE_MM_HR * cfg['drip_efficiency']
    irr_mod.EFF_RAIN_FACTOR     = cfg['eff_rain_factor']

    crop_name = cfg['crop']
    if crop_name not in irr_mod.CROP_COEFFICIENTS:
        print(f'  ERROR: Crop "{crop_name}" not found. '
              f'Available: {list(irr_mod.CROP_COEFFICIENTS.keys())}')
        return {}

    crop = irr_mod.CROP_COEFFICIENTS[crop_name]
    out_dir = _out(tag, 'irrigation')

    # Load all ET data
    all_rows = irr_mod.load_data(_ET_DIR, years)
    if not all_rows:
        print(f'  ERROR: No ET data found in {_ET_DIR}. '
              'Run 4-irrigation/fetch_et_data.py first.')
        return {}

    data_by_date = {r['date']: r for r in all_rows}
    all_results  = {}

    for py in years:
        plant_date, harv_date = irr_mod.season_bounds(
            py,
            cfg['planting_month'], cfg['planting_day'],
            cfg['harvest_month'],  cfg['harvest_day'])
        season_data = {d: r for d, r in data_by_date.items()
                       if plant_date.isoformat() <= d <= harv_date.isoformat()}
        if len(season_data) < 30:
            print(f'  Season {py}: only {len(season_data)} days — skipping.')
            continue

        print(f'  Season {py}/{py+1} …')
        result = irr_mod.analyze_season(season_data, crop, plant_date, harv_date)
        if not result:
            continue
        daily_rows, weekly_rows = result
        irr_mod.print_report(py, crop, daily_rows, weekly_rows, crop_name)

        # Save CSVs
        slug = crop_name.replace('_', '-')
        irr_mod.save_csv(weekly_rows,
                         os.path.join(out_dir, f'weekly_{slug}_{py}.csv'),
                         'weekly schedule')
        irr_mod.save_csv(daily_rows,
                         os.path.join(out_dir, f'daily_{slug}_{py}.csv'),
                         'daily ETc')
        all_results[py] = (daily_rows, weekly_rows)

    return all_results


# ===========================================================================
# STAGE 4 — INTEGRATED ANALYSIS
# ===========================================================================

def run_stage4(cfg: dict, years: list, tag: str,
               power_dir: str = None, sched_dir: str = None) -> dict:
    """
    Check energy availability against the irrigation schedule.
    Returns {year: (daily_energy, weekly_energy)}.
    """
    print(f'\n{"─"*60}')
    print(f'  STAGE 4 — Integrated Analysis  '
          f'({cfg["n_panels"]} panels, {cfg["battery_kwh"]:.1f}kWh battery)')
    print(f'{"─"*60}')

    int_mod = _load_module(
        'integrated_analysis',
        os.path.join(_ROOT, '5-integrated-analysis', 'integrated_analysis.py'))

    int_mod.BATTERY_CAPACITY_KWH    = cfg['battery_kwh']
    int_mod.BATTERY_MIN_SOC_PCT     = cfg['battery_min_soc']
    int_mod.BATTERY_MAX_SOC_PCT     = cfg['battery_max_soc']
    int_mod.BATTERY_INITIAL_SOC_PCT = 1.00
    int_mod.BATTERY_CHARGE_EFF      = cfg['battery_charge_eff']
    int_mod.BATTERY_DISCHARGE_EFF   = cfg['battery_discharge_eff']
    int_mod.INVERTER_EFF            = cfg['inverter_eff']
    int_mod.PUMP_POWER_KW           = cfg['pump_power_kw']
    int_mod.MIN_SOLAR_FOR_DISCHARGE = cfg['min_solar_discharge']
    int_mod.N_PANELS                = cfg['n_panels']
    int_mod.PANEL_RATED_POWER_W     = cfg['panel_watt']

    src_power_dir  = power_dir or _POWER_DIR
    src_sched_dir  = sched_dir or _IRR_DIR
    out_dir        = _out(tag, 'integrated')
    img_dir        = _out(tag, 'images-stage4')

    slug = cfg['crop'].replace('_', '-')
    all_results = {}

    for yr in years:
        # Load solar power (two calendar years span the growing season)
        solar_power = {}
        for cal_yr in [yr, yr + 1]:
            try:
                solar_power.update(
                    int_mod.load_solar_power(src_power_dir, cal_yr))
            except FileNotFoundError:
                pass
        if not solar_power:
            print(f'  WARNING: No solar power data for {yr} — skipping Stage 4.')
            continue

        try:
            weekly_sched = int_mod.load_weekly_schedule(
                src_sched_dir, cfg['crop'], yr)
        except FileNotFoundError:
            print(f'  WARNING: No weekly schedule for {yr}/{cfg["crop"]} — skipping.')
            continue

        daily_sched = int_mod.build_daily_schedule(weekly_sched)
        print(f'  Season {yr}/{yr+1} …')

        hourly    = int_mod.simulate_season(
            solar_power, daily_sched, cfg['battery_kwh'], cfg['n_panels'])
        daily_res = int_mod.aggregate_daily(hourly, daily_sched)
        weekly_res= int_mod.aggregate_weekly(daily_res, weekly_sched)

        int_mod.print_report(weekly_res, daily_res, yr, cfg['crop'],
                             cfg['battery_kwh'], cfg['n_panels'])

        param_tag = f"{slug}_{yr}_p{cfg['n_panels']}_b{cfg['battery_kwh']:.0f}kWh"
        int_mod.save_csv(daily_res,
                         os.path.join(out_dir, f'daily_energy_{param_tag}.csv'),
                         'daily energy')
        int_mod.save_csv(weekly_res,
                         os.path.join(out_dir, f'weekly_energy_{param_tag}.csv'),
                         'weekly energy')
        int_mod.make_plots(hourly, daily_res, weekly_res,
                           yr, cfg['crop'], cfg['battery_kwh'],
                           os.path.join(img_dir, str(yr)))
        all_results[yr] = (daily_res, weekly_res)

    return all_results


# ===========================================================================
# PARAMETER SWEEP
# ===========================================================================

def run_sweep(cfg: dict, years: list,
              sweep_panels: list, sweep_battery: list):
    """
    Run Stage 4 for every (n_panels, battery_kwh) combination and produce a
    summary reliability matrix.
    """
    print(f'\n{"═"*60}')
    print(f'  PARAMETER SWEEP')
    print(f'  Panels: {sweep_panels}')
    print(f'  Battery: {sweep_battery} kWh')
    print(f'  Years: {years}')
    print(f'{"═"*60}')

    results_matrix = {}

    for n_panels, batt_kwh in iterproduct(sweep_panels, sweep_battery):
        cfg_sw = {**cfg, 'n_panels': n_panels, 'battery_kwh': batt_kwh}
        tag    = f"sweep_p{n_panels}_b{batt_kwh:.0f}kWh"
        print(f'\n  → {n_panels} panels, {batt_kwh:.1f}kWh battery …')

        int_mod = _load_module(
            'integrated_analysis',
            os.path.join(_ROOT, '5-integrated-analysis', 'integrated_analysis.py'))
        int_mod.BATTERY_CAPACITY_KWH = batt_kwh
        int_mod.N_PANELS             = n_panels
        int_mod.PANEL_RATED_POWER_W  = cfg_sw['panel_watt']
        int_mod.INVERTER_EFF         = cfg_sw['inverter_eff']
        int_mod.PUMP_POWER_KW        = cfg_sw['pump_power_kw']
        int_mod.BATTERY_MIN_SOC_PCT  = cfg_sw['battery_min_soc']
        int_mod.BATTERY_MAX_SOC_PCT  = cfg_sw['battery_max_soc']
        int_mod.BATTERY_CHARGE_EFF   = cfg_sw['battery_charge_eff']
        int_mod.BATTERY_DISCHARGE_EFF= cfg_sw['battery_discharge_eff']
        int_mod.MIN_SOLAR_FOR_DISCHARGE = cfg_sw['min_solar_discharge']
        int_mod.BATTERY_INITIAL_SOC_PCT = 1.00

        slug = cfg_sw['crop'].replace('_', '-')
        year_reliabilities = []

        for yr in years:
            solar_power = {}
            for cal_yr in [yr, yr + 1]:
                try:
                    solar_power.update(int_mod.load_solar_power(_POWER_DIR, cal_yr))
                except FileNotFoundError:
                    pass
            if not solar_power:
                continue
            try:
                weekly_sched = int_mod.load_weekly_schedule(_IRR_DIR, cfg_sw['crop'], yr)
            except FileNotFoundError:
                continue

            daily_sched = int_mod.build_daily_schedule(weekly_sched)
            hourly      = int_mod.simulate_season(
                solar_power, daily_sched, batt_kwh, n_panels)
            daily_res   = int_mod.aggregate_daily(hourly, daily_sched)
            weekly_res  = int_mod.aggregate_weekly(daily_res, weekly_sched)

            irr_days = [d for d in daily_res if d['is_irr_day']]
            if irr_days:
                n_met = sum(1 for d in irr_days if d['met'])
                rel   = n_met / len(irr_days) * 100
                year_reliabilities.append(rel)
                print(f'    {yr}: {rel:.1f}% reliability')

        if year_reliabilities:
            avg_rel = sum(year_reliabilities) / len(year_reliabilities)
            results_matrix[(n_panels, batt_kwh)] = avg_rel
        else:
            results_matrix[(n_panels, batt_kwh)] = None

    # Print summary matrix
    print(f'\n  RELIABILITY MATRIX  (% irrigation days fully met)')
    print(f'  {"":>12}  ' +
          '  '.join(f'Batt {b:.0f}kWh' for b in sweep_battery))
    for n_p in sweep_panels:
        row_vals = []
        for b_k in sweep_battery:
            v = results_matrix.get((n_p, b_k))
            row_vals.append(f'{v:>7.1f}%' if v is not None else '    N/A')
        print(f'  {n_p:>2} panels:     ' + '  '.join(row_vals))

    # Save sweep CSV
    sweep_csv = os.path.join(_OUT_ROOT, 'sweep', 'reliability_matrix.csv')
    os.makedirs(os.path.dirname(sweep_csv), exist_ok=True)
    with open(sweep_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        header = ['n_panels'] + [f'batt_{b:.0f}kWh' for b in sweep_battery]
        writer.writerow(header)
        for n_p in sweep_panels:
            row = [n_p] + [results_matrix.get((n_p, b)) for b in sweep_battery]
            writer.writerow(row)
    print(f'\n  Sweep results saved: {sweep_csv}')
    return results_matrix


# ===========================================================================
# SUMMARY REPORT
# ===========================================================================

def print_summary(cfg: dict, years: list, stage_results: dict):
    """Print a compact cross-year summary table."""
    print(f'\n{"═"*72}')
    print(f'  CROSS-YEAR SUMMARY  |  '
          f'{cfg["n_panels"]}×{cfg["panel_watt"]:.0f}W = '
          f'{cfg["n_panels"]*cfg["panel_watt"]/1000:.2f}kWp  |  '
          f'{cfg["battery_kwh"]:.1f}kWh  |  '
          f'Crop: {cfg["crop"]}')
    print(f'{"═"*72}')
    s4 = stage_results.get(4, {})
    if s4:
        print(f'  {"Year":>6}  {"Irr days":>9}  {"Target hrs":>11}  '
              f'{"Got hrs":>8}  {"Reliability":>12}')
        print(f'  {"-"*6}  {"-"*9}  {"-"*11}  {"-"*8}  {"-"*12}')
        for yr, (daily_res, weekly_res) in sorted(s4.items()):
            irr_days = [d for d in daily_res if d['is_irr_day']]
            if not irr_days:
                continue
            n_tot   = len(irr_days)
            n_met   = sum(1 for d in irr_days if d['met'])
            tgt_h   = sum(d['target_hrs'] for d in irr_days)
            got_h   = sum(d['pump_hrs']   for d in irr_days)
            rel     = n_met / n_tot * 100
            print(f'  {yr:>6}  {n_tot:>9}  {tgt_h:>11.0f}  '
                  f'{got_h:>8.0f}  {rel:>11.1f}%')
    print(f'{"═"*72}')


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description='BahaSol master simulation — run any combination of '
                    'pipeline stages with configurable system parameters.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Examples:\n'
            '  python run_simulation.py                         '
            '# all stages, default config\n'
            '  python run_simulation.py --stages 3 4            '
            '# irrigation + integrated only\n'
            '  python run_simulation.py --year 2021\n'
            '  python run_simulation.py --panels 18 --battery-kwh 3\n'
            '  python run_simulation.py --crop tomato --year 2020\n'
            '  python run_simulation.py --sweep-panels 10 12 15 18 '
            '--sweep-battery 1 2 3\n'
        ),
    )
    # Scope
    parser.add_argument('--year', type=int, default=None,
                        help='Single planting year (overrides CONFIG years).')
    parser.add_argument('--years', type=int, nargs='+', default=None,
                        help='List of planting years.')
    parser.add_argument('--stages', type=int, nargs='+', default=[1, 2, 3, 4],
                        choices=[1, 2, 3, 4],
                        help='Which stages to run (default: 1 2 3 4).')

    # Key parameters (override CONFIG)
    parser.add_argument('--panels', type=int, default=None,
                        help=f'Number of solar panels (CONFIG: {CONFIG["n_panels"]}).')
    parser.add_argument('--battery-kwh', type=float, default=None,
                        help=f'Battery capacity kWh (CONFIG: {CONFIG["battery_kwh"]}).')
    parser.add_argument('--pump-power-kw', type=float, default=None,
                        help=f'Pump AC power kW (CONFIG: {CONFIG["pump_power_kw"]}).')
    parser.add_argument('--pump-flow-gpm', type=float, default=None,
                        help=f'Pump flow rate GPM (CONFIG: {CONFIG["pump_flow_gpm"]}).')
    parser.add_argument('--tilt', type=float, default=None,
                        help=f'Panel tilt degrees (CONFIG: {CONFIG["tilt_deg"]}).')
    parser.add_argument('--azimuth', type=float, default=None,
                        help=f'Panel azimuth degrees (CONFIG: {CONFIG["azimuth_deg"]}).')
    parser.add_argument('--crop', default=None,
                        help=f'Crop name (CONFIG: {CONFIG["crop"]}).')
    parser.add_argument('--performance-ratio', type=float, default=None,
                        help=f'Solar PR 0–1 (CONFIG: {CONFIG["performance_ratio"]}).')
    parser.add_argument('--drip-eff', type=float, default=None,
                        help=f'Drip efficiency 0–1 (CONFIG: {CONFIG["drip_efficiency"]}).')

    # Sweep mode
    parser.add_argument('--sweep-panels', type=int, nargs='+', default=None,
                        help='Sweep over panel counts. Enables parameter sweep mode.')
    parser.add_argument('--sweep-battery', type=float, nargs='+', default=None,
                        help='Sweep over battery capacities [kWh].')

    args = parser.parse_args()

    # ── Build effective config ──
    cfg = dict(CONFIG)
    if args.panels       is not None: cfg['n_panels']        = args.panels
    if args.battery_kwh  is not None: cfg['battery_kwh']     = args.battery_kwh
    if args.pump_power_kw is not None: cfg['pump_power_kw']  = args.pump_power_kw
    if args.pump_flow_gpm is not None: cfg['pump_flow_gpm']  = args.pump_flow_gpm
    if args.tilt         is not None: cfg['tilt_deg']        = args.tilt
    if args.azimuth      is not None: cfg['azimuth_deg']     = args.azimuth
    if args.crop         is not None: cfg['crop']            = args.crop
    if args.performance_ratio is not None: cfg['performance_ratio'] = args.performance_ratio
    if args.drip_eff     is not None: cfg['drip_efficiency'] = args.drip_eff

    if args.year:
        years = [args.year]
    elif args.years:
        years = sorted(args.years)
    else:
        years = cfg['years']

    stages = sorted(set(args.stages))

    # ── Run tag ──
    tag = (f"p{cfg['n_panels']}_b{cfg['battery_kwh']:.0f}kWh"
           f"_{cfg['crop']}_yr{years[0]}" +
           (f"-{years[-1]}" if len(years) > 1 else ''))

    print(f'\n{"═"*72}')
    print(f'  BahaSol Simulation Run')
    print(f'  Tag     : {tag}')
    print(f'  Stages  : {stages}')
    print(f'  Years   : {years}')
    print(f'  Panels  : {cfg["n_panels"]} × {cfg["panel_watt"]:.0f}W '
          f'= {cfg["n_panels"]*cfg["panel_watt"]/1000:.2f}kWp')
    print(f'  Battery : {cfg["battery_kwh"]:.1f}kWh')
    print(f'  Pump    : {cfg["pump_power_kw"]:.3f}kW / {cfg["pump_flow_gpm"]:.2f}GPM')
    print(f'  Crop    : {cfg["crop"]}  |  '
          f'Tilt: {cfg["tilt_deg"]}°  |  PR: {cfg["performance_ratio"]:.2f}')
    print(f'{"═"*72}')

    # ── Parameter sweep mode ──
    if args.sweep_panels or args.sweep_battery:
        sp = args.sweep_panels or [cfg['n_panels']]
        sb = args.sweep_battery or [cfg['battery_kwh']]
        run_sweep(cfg, years, sp, sb)
        return

    # ── Sequential stage execution ──
    stage_results: dict = {}
    power_dir_for_stage2 = None

    if 1 in stages:
        s1 = run_stage1(cfg, years, tag)
        stage_results[1] = s1
        # Use stage-1 output for stage 2 if both run together
        power_dir_for_stage2 = _out(tag, 'gen-power')

    if 2 in stages:
        pdir = power_dir_for_stage2 or _POWER_DIR
        s2 = run_stage2(cfg, years, tag, power_dir=pdir)
        stage_results[2] = s2

    if 3 in stages:
        s3 = run_stage3(cfg, years, tag)
        stage_results[3] = s3

    if 4 in stages:
        pdir   = power_dir_for_stage2 or _POWER_DIR
        sdir   = _out(tag, 'irrigation') if 3 in stages else _IRR_DIR
        s4     = run_stage4(cfg, years, tag,
                            power_dir=pdir, sched_dir=sdir)
        stage_results[4] = s4

    # ── Cross-year summary ──
    if 4 in stages and stage_results.get(4):
        print_summary(cfg, years, stage_results)

    print(f'\n  All outputs written to: {os.path.join(_OUT_ROOT, tag)}')


if __name__ == '__main__':
    main()
