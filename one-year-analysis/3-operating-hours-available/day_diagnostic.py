"""
day_diagnostic.py
=================
Given a specific date, produces a detailed diagnostic of why the pump did
or did not reach its target run-hours on that day.

Requires two input CSV files produced by the pipeline:
  1.  solar_analysis.py output  → irradiance, temperatures, DC power
  2.  battery_pump_analysis.py output → hourly simulation (SoC, pump state, flows)

Outputs
-------
  • Printed hourly table and narrative summary in the terminal
  • Multi-panel diagnostic plot saved as a PNG

VSCode usage
------------
  1. Set DATE and the two CSV paths in USER PARAMETERS below.
  2. Press F5.

Terminal usage
--------------
    python day_diagnostic.py 2024-07-04 [--solar-csv PATH] [--system-csv PATH] [--images-dir DIR]

Dependencies
------------
    pip install pandas matplotlib numpy
"""

# =============================================================================
# USER PARAMETERS  — edit here when running from VSCode / F5
# =============================================================================

YEAR = 2024

DATE = f'{YEAR}-10-07'      # Target date (YYYY-MM-DD)

SOLAR_CSV  = (
    f'one-year-analysis/1-solar-power/gen-power/'
    f'4469509_24.96_-78.05_{YEAR}_power.csv'
)
SYSTEM_CSV = (
    f'one-year-analysis/3-operating-hours/battery-pump/'
    f'4469509_24.96_-78.05_{YEAR}_system.csv'
)
IMAGES_DIR = f'one-year-analysis/3-operating-hours/images/{YEAR}'

# ---------------------------------------------------------------------------
# Battery and pump reference values
# Must match the values used when battery_pump_analysis.py was run.
# These are only used to draw reference lines on the diagnostic plot.
# ---------------------------------------------------------------------------
BATTERY_CAPACITY_KWH    = 15.0    # Set to 0 if no-battery mode was used
BATTERY_MIN_SOC_PCT     = 0.10
BATTERY_MAX_SOC_PCT     = 1.00
PUMP_POWER_KW           = 1.263
MAX_HOURS_PER_DAY       = 6
PUMP_START_HOUR         = 0       # Earliest hour pump is allowed to start
MIN_SOLAR_FOR_DISCHARGE_KW = 0.10

# =============================================================================
# IMPORTS
# =============================================================================

import argparse
import os
import textwrap
from collections import Counter
from datetime import date as date_type

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# =============================================================================
# STYLE
# =============================================================================

plt.rcParams.update({
    'figure.dpi'        : 200,
    'savefig.dpi'       : 200,
    'font.family'       : 'sans-serif',
    'axes.spines.top'   : False,
    'axes.spines.right' : False,
    'axes.grid'         : True,
    'grid.alpha'        : 0.30,
    'axes.titlesize'    : 10,
    'axes.titleweight'  : 'normal',
    'axes.titlepad'     : 10,
    'axes.labelpad'     : 7,
})

# Colour palette shared across panels
COL = {
    'solar_ac'    : '#F5A74E',   # orange  — available solar AC power
    'solar_pump'  : '#6DB56A',   # green   — solar delivered to pump
    'batt_pump'   : '#5D8FBD',   # blue    — battery delivered to pump
    'charging'    : '#95C78F',   # pale green — solar → battery
    'curtailed'   : '#aaaaaa',   # grey    — curtailed / unused
    'pump_line'   : '#C0392B',   # red     — pump power demand line
    'soc_line'    : '#1F5C99',   # dark blue — SoC trajectory
    'soc_fill'    : '#A8CDE0',   # light blue
    'g_tilted'    : '#FF8800',   # deep orange
    'ghi'         : '#F9DC8C',   # straw yellow
    'dni'         : '#EF8236',   # amber
    'dhi'         : '#C5E0A5',   # pale green
    't_cell'      : '#C0392B',   # red
    't_amb'       : '#5D8FBD',   # blue
    'pump_shade'  : '#6DB56A',   # green (semi-transparent shade when pump on)
}

# =============================================================================
# FAILURE REASON — human-readable explanations
# =============================================================================

REASON_EXPLAIN = {
    'ok'           : 'Pump ran successfully.',
    'night'        : 'No solar power; battery discharge blocked at night '
                     '(prevents overnight drain and ensures next-day recharge).',
    'low_solar'    : 'Solar below pump threshold; battery also insufficient to '
                     'make up the deficit.',
    'battery_empty': 'Battery at minimum SoC; solar alone could not meet pump '
                     'demand, so the pump stayed off to protect battery life.',
    'before_start' : f'Hour is before the configured PUMP_START_HOUR ({PUMP_START_HOUR:02d}:00); '
                     'pump is not permitted to run yet.',
    'cap_reached'  : f'Daily run-hour cap of {MAX_HOURS_PER_DAY} h/day already reached.',
    'not_scheduled': 'This day is not in the pump operating schedule '
                     '(e.g., weekday-only schedule and today is a weekend).',
    'no_power'     : 'Solar-only mode (no battery); solar alone could not meet '
                     'pump demand.',
    ''             : 'Pump was not attempted (not scheduled or cap already met).',
}

# =============================================================================
# DATA LOADING
# =============================================================================

def load_day(solar_csv: str, system_csv: str, target_date: str) -> pd.DataFrame:
    """
    Load both CSVs, filter to *target_date*, and merge into one DataFrame.

    Returns a 24-row DataFrame (one row per hour, 00–23) with all columns
    from both sources.
    """
    target = pd.to_datetime(target_date).date()

    # ── solar CSV ────────────────────────────────────────────────────────────
    sol = pd.read_csv(solar_csv, parse_dates=['datetime_local'])
    sol['_date'] = sol['datetime_local'].dt.date
    sol_day = sol[sol['_date'] == target].copy()
    if sol_day.empty:
        raise ValueError(
            f"Date {target_date} not found in solar CSV: {solar_csv}")

    # ── system CSV ───────────────────────────────────────────────────────────
    sys = pd.read_csv(system_csv, parse_dates=['datetime_local'])
    sys['_date'] = sys['datetime_local'].dt.date
    sys_day = sys[sys['_date'] == target].copy()
    if sys_day.empty:
        raise ValueError(
            f"Date {target_date} not found in system CSV: {system_csv}")

    # ── merge on datetime ────────────────────────────────────────────────────
    # system CSV may have fewer columns (month/doy/hour dropped on write)
    merged = pd.merge(
        sys_day.drop(columns=['_date']),
        sol_day.drop(columns=['_date', 'GHI_W_m2',   # avoid duplicate column
                               'P_dc_kW']),           # keep system version
        on='datetime_local', how='left',
    )
    merged['hour'] = merged['datetime_local'].dt.hour
    merged = merged.sort_values('hour').reset_index(drop=True)

    return merged


# =============================================================================
# TERMINAL OUTPUT
# =============================================================================

def print_hourly_table(day: pd.DataFrame, target_date: str):
    """Print a compact hourly table for the day."""
    sep = '─' * 96
    print(f'\n{sep}')
    print(f'  Day Diagnostic  —  {target_date}')
    print(sep)

    hdr = (f"  {'Hr':>3}  {'Sol-AC':>7}  {'→Pump':>7}  "
           f"{'BattΔ':>7}  {'SoC%':>6}  {'Pump':>5}  {'G_tilt':>7}  "
           f"{'T_amb':>6}  {'T_cell':>6}  Reason")
    print(hdr)
    print(f"  {'':->3}  {'kW':>7}  {'kW':>7}  "
          f"{'kWh':>7}  {'%':>6}  {'':>5}  {'W/m²':>7}  "
          f"{'°C':>6}  {'°C':>6}")
    print(f'  {sep}')

    for _, r in day.iterrows():
        pump_str = '  ON ' if r['pump_on'] else '  -- '
        soc_str  = f"{r['battery_soc_pct']:5.1f}" if BATTERY_CAPACITY_KWH > 0 else '  n/a'
        reason   = r.get('failure_reason', '')

        # Only print reason for hours where the pump didn't run and there's info
        reason_short = '' if reason in ('', 'ok') else f'[{reason}]'

        print(
            f"  {int(r['hour']):>3}  "
            f"{r['P_solar_ac_kW']:>7.3f}  "
            f"{r['P_total_to_pump_kW']:>7.3f}  "
            f"{r['battery_delta_kWh']:>+7.3f}  "
            f"{soc_str}  "
            f"{pump_str}  "
            f"{r.get('G_tilted_W_m2', 0.0):>7.1f}  "
            f"{r.get('T_amb_C', 0.0):>6.1f}  "
            f"{r.get('T_cell_C', 0.0):>6.1f}  "
            f"{reason_short}"
        )

    print(sep)


def print_summary(day: pd.DataFrame, target_date: str):
    """
    Print a narrative summary: total pump hours, energy split, and detailed
    failure-reason analysis for missed hours.
    """
    total_pump_hrs = int(day['pump_on'].sum())
    shortfall      = MAX_HOURS_PER_DAY - total_pump_hrs

    solar_kwh  = day['P_solar_to_pump_kW'].sum()
    batt_kwh   = day['P_battery_to_pump_kW'].sum()
    total_kwh  = solar_kwh + batt_kwh
    solar_pct  = 100 * solar_kwh / total_kwh if total_kwh > 0 else 0.0

    peak_solar = day['P_solar_ac_kW'].max()
    peak_g     = day['G_tilted_W_m2'].max() if 'G_tilted_W_m2' in day.columns else float('nan')
    max_t_cell = day['T_cell_C'].max() if 'T_cell_C' in day.columns else float('nan')

    sep = '─' * 72
    print(f'\n{sep}')
    print(f'  SUMMARY FOR {target_date}')
    print(sep)

    scheduled = bool(day['pump_scheduled_day'].iloc[0])
    print(f'  Scheduled day        : {"Yes" if scheduled else "No — pump not scheduled today"}')
    print(f'  Pump hours achieved  : {total_pump_hrs} / {MAX_HOURS_PER_DAY} h')

    if shortfall == 0:
        print(f'  ✓ Target reached — no shortfall.')
    else:
        print(f'  ✗ Shortfall         : {shortfall} h below target')

    print(f'\n  ENERGY')
    print(f'  Peak solar AC power  : {peak_solar:.3f} kW')
    print(f'  Peak tilted irrad.   : {peak_g:.0f} W/m²')
    print(f'  Max cell temperature : {max_t_cell:.1f} °C')
    print(f'  Total energy → pump  : {total_kwh:.3f} kWh')
    print(f'    From solar         : {solar_kwh:.3f} kWh  ({solar_pct:.1f} %)')
    print(f'    From battery       : {batt_kwh:.3f} kWh  ({100-solar_pct:.1f} %)')

    if BATTERY_CAPACITY_KWH > 0:
        soc_vals = day['battery_soc_pct']
        print(f'\n  BATTERY SoC')
        print(f'  Start of day         : {soc_vals.iloc[0]:.1f} %')
        print(f'  End of day           : {soc_vals.iloc[-1]:.1f} %')
        print(f'  Minimum reached      : {soc_vals.min():.1f} %')

    # ── failure-reason breakdown ──────────────────────────────────────────
    # Only consider hours when the pump DIDN'T run and a reason is given
    non_pump = day[~day['pump_on'] & (day['failure_reason'] != '')]
    if not non_pump.empty:
        reason_counts = Counter(non_pump['failure_reason'])
        print(f'\n  WHY THE PUMP WAS OFF ({len(non_pump)} hours)')
        for reason, count in sorted(reason_counts.items(),
                                    key=lambda x: -x[1]):
            hrs = non_pump[non_pump['failure_reason'] == reason]['hour'].tolist()
            hrs_str = ', '.join(f'{h:02d}:00' for h in hrs)
            print(f'  [{reason}]  ×{count}  ({hrs_str})')
            explanation = REASON_EXPLAIN.get(reason, '')
            if explanation:
                wrapped = textwrap.fill(explanation, width=65,
                                        initial_indent='     ',
                                        subsequent_indent='     ')
                print(wrapped)

    print(sep)


# =============================================================================
# DIAGNOSTIC PLOT
# =============================================================================

def _shade_pump_hours(ax, day: pd.DataFrame):
    """Add a light green vertical band for each hour the pump ran."""
    for _, r in day[day['pump_on']].iterrows():
        ax.axvspan(r['hour'] - 0.5, r['hour'] + 0.5,
                   color=COL['pump_shade'], alpha=0.10, lw=0)


def _shade_night(ax, day: pd.DataFrame):
    """Shade hours where solar is essentially zero (night time)."""
    for _, r in day[~day['is_daytime']].iterrows():
        ax.axvspan(r['hour'] - 0.5, r['hour'] + 0.5,
                   color='#000000', alpha=0.04, lw=0)


def plot_diagnostic(day: pd.DataFrame, target_date: str, images_dir: str):
    """
    Four-panel diagnostic plot for a single day:
      1. Power balance  — solar available, flows to pump & battery, curtailed
      2. Battery SoC    — trajectory through the day  (skipped if no battery)
      3. Irradiance     — G_tilted, GHI, DNI, DHI
      4. Temperature    — T_amb and T_cell
    """
    no_battery = (BATTERY_CAPACITY_KWH == 0)
    n_panels   = 3 if no_battery else 4
    hrs        = day['hour'].values

    fig, axes = plt.subplots(
        n_panels, 1, figsize=(12, 3.4 * n_panels),
        sharex=True,
        gridspec_kw={'hspace': 0.55},
    )
    if n_panels == 1:
        axes = [axes]

    ax_pow = axes[0]
    ax_soc = None if no_battery else axes[1]
    ax_irr = axes[-2]
    ax_tmp = axes[-1]

    # ── Panel 1: Power balance ───────────────────────────────────────────────
    ax = ax_pow

    # Stacked area: solar → pump (green), battery → pump (blue)
    s2p = day['P_solar_to_pump_kW'].values
    b2p = day['P_battery_to_pump_kW'].values
    s2b = day['P_solar_to_battery_kW'].values
    cur = day['P_curtailed_kW'].values
    sol = day['P_solar_ac_kW'].values

    ax.stackplot(
        hrs, s2p, b2p, s2b, cur,
        labels=['Solar → pump (direct)',
                'Battery → pump',
                'Solar → battery (charging)',
                'Curtailed / unused'],
        colors=[COL['solar_pump'], COL['batt_pump'],
                COL['charging'],   COL['curtailed']],
        alpha=0.80,
        step='mid',
    )
    ax.plot(hrs, sol, lw=1.8, color=COL['solar_ac'],
            label='Solar AC available', zorder=5)
    ax.axhline(PUMP_POWER_KW, lw=1.2, ls='--', color=COL['pump_line'],
               label=f'Pump demand ({PUMP_POWER_KW:.3f} kW)', zorder=6)

    if PUMP_START_HOUR > 0:
        ax.axvline(PUMP_START_HOUR - 0.5, lw=1.0, ls=':', color='#555555',
                   label=f'Pump start hour ({PUMP_START_HOUR:02d}:00)')

    _shade_pump_hours(ax, day)
    _shade_night(ax, day)

    ax.set_ylabel('Power (kW)')
    ax.set_title(f'Power Balance — {target_date}', pad=10)
    ax.legend(ncol=3, fontsize=7, loc='upper left')
    ax.set_ylim(bottom=0)

    # Annotate pump-on hours as a horizontal rug at the top
    pump_hrs = day[day['pump_on']]['hour'].tolist()
    if pump_hrs:
        y_top = ax.get_ylim()[1]
        for h in pump_hrs:
            ax.plot(h, y_top * 0.97, marker='v', ms=5,
                    color=COL['pump_shade'], alpha=0.9, clip_on=False)

    # ── Panel 2: Battery SoC ────────────────────────────────────────────────
    if ax_soc is not None:
        ax = ax_soc
        soc = day['battery_soc_pct'].values

        ax.fill_between(hrs, soc, alpha=0.30, color=COL['soc_fill'], step='mid')
        ax.step(hrs, soc, lw=2.0, color=COL['soc_line'],
                where='mid', label='Battery SoC')

        ax.axhline(BATTERY_MIN_SOC_PCT * 100, lw=1.0, ls='--',
                   color='#C0392B', alpha=0.7,
                   label=f'Min SoC ({BATTERY_MIN_SOC_PCT*100:.0f} %)')
        ax.axhline(BATTERY_MAX_SOC_PCT * 100, lw=1.0, ls='--',
                   color='#2980B9', alpha=0.7,
                   label=f'Max SoC ({BATTERY_MAX_SOC_PCT*100:.0f} %)')
        ax.axhspan(0, BATTERY_MIN_SOC_PCT * 100,
                   color='#F1948A', alpha=0.10)

        _shade_pump_hours(ax, day)
        _shade_night(ax, day)

        ax.set_ylabel('Battery SoC (%)')
        ax.set_title(f'Battery State of Charge — {target_date}')
        ax.set_ylim(max(0, BATTERY_MIN_SOC_PCT * 100 - 10), 105)
        ax.legend(ncol=3, fontsize=7, loc='lower right')

    # ── Panel 3: Irradiance ─────────────────────────────────────────────────
    ax = ax_irr
    if 'G_tilted_W_m2' in day.columns:
        ax.plot(hrs, day['G_tilted_W_m2'],  lw=2.0, color=COL['g_tilted'],
                label='G_tilted (panel plane)')
    if 'GHI_W_m2' in day.columns:
        ax.plot(hrs, day['GHI_W_m2'],       lw=1.5, color=COL['ghi'],
                ls='--', label='GHI')
    if 'DNI_W_m2' in day.columns:
        ax.plot(hrs, day['DNI_W_m2'],       lw=1.2, color=COL['dni'],
                ls='-.', label='DNI')
    if 'DHI_W_m2' in day.columns:
        ax.plot(hrs, day['DHI_W_m2'],       lw=1.2, color=COL['dhi'],
                ls=':', label='DHI')

    ax.axhline(MIN_SOLAR_FOR_DISCHARGE_KW * 1000, lw=0.9, ls=':', color='#555555',
               alpha=0.7,
               label=f'Discharge threshold ({MIN_SOLAR_FOR_DISCHARGE_KW*1000:.0f} W/m²)')
    _shade_night(ax, day)
    ax.set_ylabel('Irradiance (W/m²)')
    ax.set_title(f'Solar Irradiance Components — {target_date}')
    ax.set_ylim(bottom=0)
    ax.legend(ncol=3, fontsize=7)

    # ── Panel 4: Temperature ─────────────────────────────────────────────────
    ax = ax_tmp
    if 'T_amb_C' in day.columns:
        ax.plot(hrs, day['T_amb_C'],  lw=2.0, color=COL['t_amb'],
                label='Ambient temp (T_amb)')
    if 'T_cell_C' in day.columns:
        ax.plot(hrs, day['T_cell_C'], lw=2.0, color=COL['t_cell'],
                label='Cell temp (T_cell)', ls='--')
    ax.fill_between(hrs,
                    day.get('T_amb_C',  pd.Series(0, index=day.index)).values,
                    day.get('T_cell_C', pd.Series(0, index=day.index)).values,
                    alpha=0.12, color=COL['t_cell'],
                    label='Derating zone')
    _shade_night(ax, day)
    ax.set_ylabel('Temperature (°C)')
    ax.set_title(f'Cell & Ambient Temperature — {target_date}')
    ax.legend(ncol=3, fontsize=7)

    # ── Shared x-axis formatting ─────────────────────────────────────────────
    for ax in axes:
        ax.set_xlim(-0.5, 23.5)
        ax.xaxis.set_major_locator(mticker.MultipleLocator(2))
        ax.xaxis.set_minor_locator(mticker.MultipleLocator(1))
        ax.set_xticks(range(0, 24, 2))
        ax.set_xticklabels([f'{h:02d}:00' for h in range(0, 24, 2)],
                            fontsize=8, rotation=30, ha='right')

    # Overall figure title
    pump_hrs_achieved = int(day['pump_on'].sum())
    batt_desc = 'no battery' if no_battery else f'{BATTERY_CAPACITY_KWH:.0f} kWh battery'
    fig.suptitle(
        f'Day Diagnostic  —  {target_date}  '
        f'({pump_hrs_achieved}/{MAX_HOURS_PER_DAY} pump hours)  '
        f'|  {batt_desc},  {PUMP_POWER_KW:.3f} kW pump',
        fontsize=11, y=1.01,
    )

    # ── Save ─────────────────────────────────────────────────────────────────
    os.makedirs(images_dir, exist_ok=True)
    date_slug = target_date.replace('-', '')
    batt_slug = 'noBatt' if no_battery else f'{int(BATTERY_CAPACITY_KWH)}kWh'
    fname     = f'diagnostic_{date_slug}_{batt_slug}.png'
    path      = os.path.join(images_dir, fname)
    fig.savefig(path, bbox_inches='tight', pad_inches=0.35, dpi=200)
    plt.close(fig)
    print(f'\n  Plot saved → {path}')


# =============================================================================
# MAIN
# =============================================================================

def run(target_date: str, solar_csv: str, system_csv: str, images_dir: str):
    """Load, filter, print table + summary, save diagnostic plot."""
    print(f'\nDay Diagnostic  —  {target_date}')
    print(f'  Solar CSV  : {solar_csv}')
    print(f'  System CSV : {system_csv}')

    day = load_day(solar_csv, system_csv, target_date)
    print(f'  Loaded {len(day)} hourly rows for {target_date}.')

    print_hourly_table(day, target_date)
    print_summary(day, target_date)
    plot_diagnostic(day, target_date, images_dir)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Per-day diagnostic for the solar + battery + pump pipeline.')
    parser.add_argument(
        'date', nargs='?', default=None,
        help='Date to diagnose in YYYY-MM-DD format (default: DATE constant above).')
    parser.add_argument('--solar-csv',  default=None)
    parser.add_argument('--system-csv', default=None)
    parser.add_argument('--images-dir', default=None)
    args = parser.parse_args()

    run(
        target_date = args.date       or DATE,
        solar_csv   = args.solar_csv  or SOLAR_CSV,
        system_csv  = args.system_csv or SYSTEM_CSV,
        images_dir  = args.images_dir or IMAGES_DIR,
    )