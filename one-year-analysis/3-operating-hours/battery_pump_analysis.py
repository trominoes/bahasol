"""
battery_pump_analysis.py
========================
Simulates a hybrid solar + battery + pump system at hourly resolution over
one year.  Takes the CSV produced by solar_analysis.py as its only input.

System architecture
-------------------
  Solar panels (DC)
       │
       ▼
  Hybrid inverter (DC → AC, η_inv)
       │
       ├──► AC bus ──► Pump load  (PUMP_POWER_KW AC)
       │
       └──► Battery charger (η_charge) ──► 48 V Li battery   [optional]
                                               │
                                           ────┘ (discharge via inverter, η_discharge)

Set BATTERY_CAPACITY_KWH = 0 to model a solar-only system with no battery.

Losses modelled here (beyond solar_analysis.py PR = 0.85)
----------------------------------------------------------
  η_inv        DC solar → AC power conversion (hybrid inverter)
  η_charge     AC surplus → energy stored in battery
  η_discharge  Battery energy → AC power at output

VSCode usage
------------
1. Set YEAR and file paths in USER PARAMETERS below.
2. Press F5.

Terminal usage
--------------
    python battery_pump_analysis.py [input_csv] [output_csv] [--images-dir DIR]

Dependencies
------------
    pip install pandas matplotlib numpy
"""

# =============================================================================
# USER PARAMETERS  — edit here when running from VSCode / F5
# =============================================================================

YEAR = 2024
INPUT_CSV  = f'one-year-analysis/1-solar-power/gen-power/4469509_24.96_-78.05_{YEAR}_power.csv'
OUTPUT_CSV = f'one-year-analysis/3-operating-hours/battery-pump/4469509_24.96_-78.05_{YEAR}_system.csv'
IMAGES_DIR = f'one-year-analysis/3-operating-hours/images/{YEAR}'

# ---------------------------------------------------------------------------
# Battery: 48 V Lithium (LiFePO₄)
# Set BATTERY_CAPACITY_KWH = 0 for a solar-only system (no battery).
# ---------------------------------------------------------------------------
BATTERY_CAPACITY_KWH     = 2.0  # Nameplate capacity [kWh].  Set to 0 for no battery.
BATTERY_MIN_SOC_PCT      = 0.10   # Minimum allowed SoC (protects cycle life) [fraction]
BATTERY_MAX_SOC_PCT      = 1.00   # Maximum SoC [fraction]
BATTERY_INITIAL_SOC_PCT  = 1.00   # SoC at the very start of the year [fraction]
BATTERY_CHARGE_EFF       = 0.95   # Charge efficiency (AC → stored kWh) [fraction]
BATTERY_DISCHARGE_EFF    = 0.95   # Discharge efficiency (stored kWh → AC) [fraction]
# Usable capacity = BATTERY_CAPACITY_KWH × (MAX − MIN) SOC
# Defaults: 15 × 0.90 = 13.5 kWh usable.

# ---------------------------------------------------------------------------
# Hybrid inverter
# ---------------------------------------------------------------------------
INVERTER_EFF = 0.96   # DC solar → AC conversion efficiency [fraction]
# solar_analysis.py already applies PR = 0.85 for wiring / mismatch / soiling.
# INVERTER_EFF is the additional AC-stage loss not covered by PR.

# ---------------------------------------------------------------------------
# Pump
# ---------------------------------------------------------------------------
PUMP_POWER_KW = 1.263
# Pump is an all-or-nothing load: it must receive PUMP_POWER_KW or it is off.

# Solar must exceed this threshold before the battery is allowed to discharge.
# Prevents overnight battery drain and ensures daytime recharge opportunity.
MIN_SOLAR_FOR_DISCHARGE_KW = 0.10   # [kW AC]

# ---------------------------------------------------------------------------
# Pump schedule
# ---------------------------------------------------------------------------
# SCHEDULE_DAYS: 'all' — every calendar day
#                list of ISO weekday integers (Monday=1 … Sunday=7)
# Examples:
#   'all'            → every day (default)
#   [1, 3, 5]        → Monday / Wednesday / Friday
#   [1, 2, 3, 4, 5]  → weekdays only
SCHEDULE_DAYS    = 'all'
MAX_HOURS_PER_DAY = 6   # Hard ceiling on pump run-hours per scheduled day

# Earliest hour of day the pump may start (local standard time, 0–23).
# 0 = no restriction (pump starts as soon as solar/battery conditions are met).
# e.g. 10 = pump will not activate before 10:00 regardless of solar availability.
PUMP_START_HOUR = 0

# =============================================================================
# IMPORTS
# =============================================================================

import argparse
import os

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd

MONTH_COLORS = [
    '#7EB8D4', '#A8CDE0', '#95C78F', '#6DB56A',
    '#C5E0A5', '#F9DC8C', '#F5A74E', '#EF8236',
    '#D4956A', '#C49BC4', '#9B79BA', '#5D8FBD',
]
MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun',
              'Jul','Aug','Sep','Oct','Nov','Dec']

plt.rcParams.update({
    'figure.dpi'        : 200,
    'savefig.dpi'       : 200,
    'font.family'       : 'sans-serif',
    'axes.spines.top'   : False,
    'axes.spines.right' : False,
    'axes.grid'         : True,
    'grid.alpha'        : 0.35,
    'axes.titlesize'    : 11,
    'axes.titleweight'  : 'normal',
    'axes.titlepad'     : 12,
    'axes.labelpad'     : 8,
})

# =============================================================================
# HELPERS
# =============================================================================

def _batt_label() -> str:
    """Short battery descriptor for titles and filenames."""
    if BATTERY_CAPACITY_KWH == 0:
        return 'no battery'
    return f'{BATTERY_CAPACITY_KWH:.0f} kWh battery'

def _batt_fname() -> str:
    """Battery descriptor safe for use in a filename."""
    if BATTERY_CAPACITY_KWH == 0:
        return 'noBatt'
    return f'{int(BATTERY_CAPACITY_KWH)}kWh'

def _title(base: str, year: int) -> str:
    """Consistent plot title: base — battery, pump — year."""
    return f'{base} — {_batt_label()}, {PUMP_POWER_KW:.3f} kW pump — {year}'

def _fname(prefix: str, year: int) -> str:
    """Consistent filename: prefix_battDesc.png"""
    return f'{prefix}_{_batt_fname()}.png'

def _save(fig, images_dir: str, filename: str):
    os.makedirs(images_dir, exist_ok=True)
    path = os.path.join(images_dir, filename)
    fig.savefig(path, bbox_inches='tight', pad_inches=0.35, dpi=200)
    plt.close(fig)
    print(f'  Saved → {path}')

# =============================================================================
# 1. DATA LOADING
# =============================================================================

def load_solar_csv(path: str) -> pd.DataFrame:
    """Load a solar_analysis.py output CSV and add convenience columns."""
    df = pd.read_csv(path, parse_dates=['datetime_local'])
    df = df.sort_values('datetime_local').reset_index(drop=True)
    df['date']        = df['datetime_local'].dt.date
    df['hour']        = df['datetime_local'].dt.hour
    df['month']       = df['datetime_local'].dt.month
    df['day_of_year'] = df['datetime_local'].dt.day_of_year
    df['iso_weekday'] = df['datetime_local'].dt.isocalendar().day.astype(int)
    return df

# =============================================================================
# 2. HOURLY SIMULATION
# =============================================================================

def _charge_battery(p_available_ac: float, soc: float,
                    max_soc: float, eta: float) -> tuple:
    """
    Attempt to charge the battery from available AC surplus.

    Returns
    -------
    (energy_into_battery_kwh, p_solar_consumed_ac_kw, p_curtailed_ac_kw)
    """
    if p_available_ac <= 0:
        return 0.0, 0.0, 0.0
    space_kwh        = max_soc - soc
    energy_in        = min(p_available_ac * eta, space_kwh)
    p_solar_consumed = energy_in / eta if energy_in > 0 else 0.0
    p_curtailed      = max(p_available_ac - p_solar_consumed, 0.0)
    return energy_in, p_solar_consumed, p_curtailed


def simulate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Run the hourly battery + pump simulation.

    Power management rules
    ----------------------
    1. Convert DC solar → AC via inverter efficiency.

    2. If the pump is scheduled today, the daily run-hour cap has not been
       reached, and the current hour is on or after PUMP_START_HOUR:
         a. Solar ≥ pump demand → pump runs on solar; surplus charges battery.
         b. Solar < pump demand AND daytime AND battery above min SoC
            → battery supplements solar to meet pump demand.
         c. Otherwise → pump off; solar charges battery.

    3. Pump not scheduled, daily cap reached, or before PUMP_START_HOUR
       → pump off; solar charges battery.

    4. Battery discharge blocked when P_dc < MIN_SOLAR_FOR_DISCHARGE_KW
       (prevents overnight drain and ensures next-morning recharge).

    5. No-battery mode (BATTERY_CAPACITY_KWH = 0): all battery steps
       are skipped; pump only runs when solar alone meets demand.

    6. Excess solar that cannot be stored or used is curtailed.

    Failure reasons logged per hour
    --------------------------------
    'ok'             — pump ran successfully.
    'night'          — no solar, battery discharge blocked.
    'low_solar'      — solar below pump demand, no usable battery.
    'battery_empty'  — battery at minimum SoC, solar insufficient.
    'before_start'   — hour is before PUMP_START_HOUR.
    'cap_reached'    — daily run-hour cap already met.
    'not_scheduled'  — not a scheduled operating day.
    'no_power'       — solar-only mode, solar insufficient.
    ''               — pump was not attempted (not scheduled / cap).
    """
    no_battery = (BATTERY_CAPACITY_KWH == 0)
    min_soc    = 0.0 if no_battery else BATTERY_CAPACITY_KWH * BATTERY_MIN_SOC_PCT
    max_soc    = 0.0 if no_battery else BATTERY_CAPACITY_KWH * BATTERY_MAX_SOC_PCT
    soc        = 0.0 if no_battery else BATTERY_CAPACITY_KWH * BATTERY_INITIAL_SOC_PCT

    current_date     = None
    daily_pump_hours = 0
    records          = []

    for _, row in df.iterrows():
        dt   = row['datetime_local']
        date = row['date']

        if date != current_date:
            current_date     = date
            daily_pump_hours = 0

        p_dc       = float(row['P_dc_kW'])
        p_solar_ac = p_dc * INVERTER_EFF
        is_daytime = p_dc >= MIN_SOLAR_FOR_DISCHARGE_KW

        iso_wd = int(row['iso_weekday'])
        scheduled_day = (SCHEDULE_DAYS == 'all') or (iso_wd in SCHEDULE_DAYS)

        hour = int(row['hour'])
        after_start = (hour >= PUMP_START_HOUR)

        pump_can_run = scheduled_day and (daily_pump_hours < MAX_HOURS_PER_DAY) and after_start

        p_solar_to_pump   = 0.0
        p_battery_to_pump = 0.0
        p_solar_to_battery = 0.0
        p_curtailed       = 0.0
        pump_on           = False
        soc_before        = soc
        failure_reason    = ''

        if not scheduled_day:
            failure_reason = 'not_scheduled'
        elif daily_pump_hours >= MAX_HOURS_PER_DAY:
            failure_reason = 'cap_reached'
        elif not after_start:
            failure_reason = 'before_start'

        if pump_can_run:
            if no_battery:
                # ── No-battery mode ────────────────────────────────────────
                if p_solar_ac >= PUMP_POWER_KW:
                    pump_on           = True
                    p_solar_to_pump   = PUMP_POWER_KW
                    p_curtailed       = p_solar_ac - PUMP_POWER_KW
                    failure_reason    = 'ok'
                else:
                    failure_reason = 'no_power'

            elif p_solar_ac >= PUMP_POWER_KW:
                # ── Case A: solar alone covers pump ────────────────────────
                pump_on         = True
                p_solar_to_pump = PUMP_POWER_KW
                p_excess        = p_solar_ac - PUMP_POWER_KW
                energy_in, _, curtailed = _charge_battery(
                    p_excess, soc, max_soc, BATTERY_CHARGE_EFF)
                p_solar_to_battery = energy_in
                p_curtailed        = curtailed
                soc               += energy_in
                failure_reason     = 'ok'

            elif is_daytime and (soc > min_soc):
                # ── Case B: battery supplements solar ──────────────────────
                p_deficit     = PUMP_POWER_KW - p_solar_ac
                energy_needed = p_deficit / BATTERY_DISCHARGE_EFF

                if (soc - min_soc) >= energy_needed:
                    pump_on           = True
                    p_solar_to_pump   = p_solar_ac
                    p_battery_to_pump = p_deficit
                    soc              -= energy_needed
                    failure_reason    = 'ok'
                else:
                    failure_reason = 'battery_empty'
                    energy_in, _, curtailed = _charge_battery(
                        p_solar_ac, soc, max_soc, BATTERY_CHARGE_EFF)
                    p_solar_to_battery = energy_in
                    p_curtailed        = curtailed
                    soc               += energy_in

            elif not is_daytime:
                failure_reason = 'night'
                # No battery discharge at night; charge from any residual solar
                energy_in, _, curtailed = _charge_battery(
                    p_solar_ac, soc, max_soc, BATTERY_CHARGE_EFF)
                p_solar_to_battery = energy_in
                p_curtailed        = curtailed
                soc               += energy_in

            else:
                failure_reason = 'low_solar'
                energy_in, _, curtailed = _charge_battery(
                    p_solar_ac, soc, max_soc, BATTERY_CHARGE_EFF)
                p_solar_to_battery = energy_in
                p_curtailed        = curtailed
                soc               += energy_in

        else:
            # Pump not attempted — charge battery from solar
            if not no_battery:
                energy_in, _, curtailed = _charge_battery(
                    p_solar_ac, soc, max_soc, BATTERY_CHARGE_EFF)
                p_solar_to_battery = energy_in
                p_curtailed        = curtailed
                soc               += energy_in
            else:
                p_curtailed = p_solar_ac

        soc = max(min_soc, min(max_soc, soc))
        if pump_on:
            daily_pump_hours += 1

        records.append({
            'datetime_local'        : dt,
            'month'                 : row['month'],
            'day_of_year'           : row['day_of_year'],
            'hour'                  : hour,
            'P_dc_kW'               : round(p_dc, 3),
            'P_solar_ac_kW'         : round(p_solar_ac, 3),
            'GHI_W_m2'              : round(float(row['GHI_W_m2']), 1),
            'is_daytime'            : is_daytime,
            'pump_scheduled_day'    : scheduled_day,
            'pump_on'               : pump_on,
            'pump_hours_today'      : daily_pump_hours,
            'failure_reason'        : failure_reason,
            'P_solar_to_pump_kW'    : round(p_solar_to_pump, 3),
            'P_battery_to_pump_kW'  : round(p_battery_to_pump, 3),
            'P_total_to_pump_kW'    : round(p_solar_to_pump + p_battery_to_pump, 3),
            'P_solar_to_battery_kW' : round(p_solar_to_battery, 3),
            'P_curtailed_kW'        : round(p_curtailed, 3),
            'battery_delta_kWh'     : round(soc - soc_before, 3),
            'battery_soc_kWh'       : round(soc, 3),
            'battery_soc_pct'       : round(100.0 * soc / BATTERY_CAPACITY_KWH, 1)
                                      if BATTERY_CAPACITY_KWH > 0 else 0.0,
        })

    return pd.DataFrame(records)

# =============================================================================
# 3. SUMMARY STATISTICS
# =============================================================================

def compute_summary(sim: pd.DataFrame, year: int) -> pd.DataFrame:
    """
    Print an annual summary and return a DataFrame of scheduled days that
    did not reach the target pump run-hours.
    """
    pump_on = sim[sim['pump_on']]
    total_hrs        = sim['pump_on'].sum()
    from_solar_kwh   = pump_on['P_solar_to_pump_kW'].sum()
    from_battery_kwh = pump_on['P_battery_to_pump_kW'].sum()
    total_pump_kwh   = from_solar_kwh + from_battery_kwh
    solar_frac       = 100 * from_solar_kwh / total_pump_kwh if total_pump_kwh else 0

    daily = sim.groupby('day_of_year').agg(
        pump_hrs    =('pump_on',              'sum'),
        scheduled   =('pump_scheduled_day',   'first'),
        date        =('datetime_local',        'first'),
        month       =('month',                'first'),
    ).reset_index()

    sched     = daily[daily['scheduled']]
    short     = sched[sched['pump_hrs'] < MAX_HOURS_PER_DAY].copy()
    short['date'] = pd.to_datetime(short['date']).dt.date

    curtailed_kwh  = sim['P_curtailed_kW'].sum()
    solar_total_dc = sim['P_dc_kW'].sum()

    sep = '=' * 62
    print(f'\n{sep}')
    print(f'  Battery + Pump System — Annual Summary {year}')
    print(sep)
    print(f'  Battery               : {_batt_label()}')
    print(f'  Pump power demand     : {PUMP_POWER_KW:.3f} kW AC')
    print(f'  Schedule              : {"every day" if SCHEDULE_DAYS == "all" else str(SCHEDULE_DAYS)}'
          f'  |  max {MAX_HOURS_PER_DAY} h/day'
          f'  |  start ≥ {PUMP_START_HOUR:02d}:00')

    sched_count = int(sched['pump_hrs'].count())
    print(f'\n  PUMP OPERATION')
    print(f'    Scheduled days      : {sched_count} / 365')
    print(f'    Total run-hours     : {total_hrs} h')
    print(f'    Avg on scheduled day: {total_hrs/sched_count:.1f} h/day')
    print(f'    Total pump energy   : {total_pump_kwh:.1f} kWh')
    print(f'    From solar (direct) : {from_solar_kwh:.1f} kWh  ({solar_frac:.1f} %)')
    print(f'    From battery        : {from_battery_kwh:.1f} kWh  ({100-solar_frac:.1f} %)')

    print(f'\n  DAYS BELOW TARGET ({MAX_HOURS_PER_DAY} h/day) — scheduled days only')
    print(f'    Count               : {len(short)} / {sched_count} scheduled days')
    if not short.empty:
        for m in range(1, 13):
            m_short = short[short['month'] == m]
            if not m_short.empty:
                dates_str = ', '.join(str(d) for d in m_short['date'])
                hrs_str   = ', '.join(f'{h}h' for h in m_short['pump_hrs'])
                print(f'    {MONTH_ABBR[m-1]:>3}: {dates_str}')
                print(f'         hrs: {hrs_str}')
    else:
        print('    (none — target reached every scheduled day)')

    if BATTERY_CAPACITY_KWH > 0:
        print(f'\n  BATTERY')
        print(f'    Start SoC           : {sim["battery_soc_pct"].iloc[0]:.1f} %')
        print(f'    End SoC             : {sim["battery_soc_pct"].iloc[-1]:.1f} %')
        print(f'    Min SoC reached     : {sim["battery_soc_pct"].min():.1f} %')
        print(f'    Avg SoC             : {sim["battery_soc_pct"].mean():.1f} %')

    print(f'\n  SOLAR')
    print(f'    Total DC generated  : {solar_total_dc:.1f} kWh')
    print(f'    Curtailed (unused)  : {curtailed_kwh:.1f} kWh'
          f'  ({100*curtailed_kwh/solar_total_dc:.1f} %)')
    print(f'{sep}\n')

    return short   # DataFrame of under-performing scheduled days

# =============================================================================
# 4. PLOTS
# =============================================================================

# ---------------------------------------------------------------------------
# P1. Battery SoC over the year
# ---------------------------------------------------------------------------

def plot_battery_soc(sim: pd.DataFrame, year: int, images_dir: str):
    """Full-year battery SoC with 7-day rolling average."""
    if BATTERY_CAPACITY_KWH == 0:
        print('  P1 skipped — no battery.')
        return

    ts   = pd.to_datetime(sim['datetime_local'])
    soc  = sim['battery_soc_pct']
    roll = soc.rolling(7 * 24, center=True).mean()

    fig, ax = plt.subplots(figsize=(14, 4.5))
    ax.axhspan(0, BATTERY_MIN_SOC_PCT * 100, color='#F1948A', alpha=0.12,
               label='Below minimum SoC')
    ax.axhline(BATTERY_MIN_SOC_PCT * 100, lw=1.0, ls='--', color='#C0392B', alpha=0.7)
    ax.axhline(BATTERY_MAX_SOC_PCT * 100, lw=1.0, ls='--', color='#2980B9', alpha=0.7)

    ax.fill_between(ts, soc, alpha=0.18, color='#aaaaaa')
    ax.plot(ts, soc,  lw=0.5, color='#aaaaaa', label='Hourly SoC')
    ax.plot(ts, roll, lw=2.0, color='#1F5C99', label='7-day rolling avg')

    ax.set_title(_title('Battery State of Charge', year))
    ax.set_ylabel('SoC (%)')
    ax.set_ylim(0, 105)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.set_xlim(ts.iloc[0], ts.iloc[-1])
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout(pad=2.2)
    _save(fig, images_dir, _fname('P1_battery_soc', year))


# ---------------------------------------------------------------------------
# P2. Daily pump run-hours (365 bars), underperforming days highlighted
# ---------------------------------------------------------------------------

def plot_daily_pump_hours(sim: pd.DataFrame, short_days: pd.DataFrame,
                          year: int, images_dir: str):
    """
    Bar chart of pump run-hours per day.  Scheduled days that did not reach
    the target are outlined in red so they stand out immediately.
    """
    daily = (sim.groupby('day_of_year')
               .agg(pump_hrs=('pump_on', 'sum'),
                    month=('month', 'first'),
                    scheduled=('pump_scheduled_day', 'first'))
               .reset_index())

    colours = [MONTH_COLORS[m-1] for m in daily['month']]
    avg     = daily.loc[daily['scheduled'], 'pump_hrs'].mean()

    short_doys = set(short_days['day_of_year']) if 'day_of_year' in short_days.columns \
                 else set()

    fig, ax = plt.subplots(figsize=(15, 4.5))
    bars = ax.bar(daily['day_of_year'], daily['pump_hrs'],
                  color=colours, width=1.0, linewidth=0)

    # Red outline on under-performing scheduled days
    for bar, doy in zip(bars, daily['day_of_year']):
        if doy in short_doys:
            bar.set_edgecolor('#C0392B')
            bar.set_linewidth(1.3)

    month_starts = daily.groupby('month')['day_of_year'].first().values
    ax.set_xticks(month_starts)
    ax.set_xticklabels(MONTH_ABBR, fontsize=9)

    ax.axhline(MAX_HOURS_PER_DAY, lw=1.2, ls=':', color='#333333', alpha=0.6)
    ax.axhline(avg, lw=1.4, ls='--', color='#C0392B')

    patches = [mpatches.Patch(color=MONTH_COLORS[m-1], label=MONTH_ABBR[m-1])
               for m in range(1, 13)]
    short_patch = mpatches.Patch(facecolor='none', edgecolor='#C0392B',
                                 linewidth=1.3, label=f'Below target ({len(short_doys)} days)')
    ax.legend(handles=patches + [
        plt.Line2D([0],[0], ls='--', color='#C0392B',
                   label=f'Scheduled avg {avg:.1f} h/day'),
        plt.Line2D([0],[0], ls=':', color='#333333',
                   label=f'Cap {MAX_HOURS_PER_DAY} h'),
        short_patch,
    ], ncol=8, fontsize=7, loc='upper right')

    ax.set_ylabel('Pump run-hours')
    ax.set_ylim(0, MAX_HOURS_PER_DAY + 1.5)
    ax.set_xlim(0.5, 365.5)
    ax.set_title(_title('Daily Pump Run-Hours', year))
    fig.tight_layout(pad=2.2)
    _save(fig, images_dir, _fname('P2_daily_pump_hours', year))


# ---------------------------------------------------------------------------
# P3. Monthly pump hours + energy source breakdown
# ---------------------------------------------------------------------------

def plot_monthly_pump_breakdown(sim: pd.DataFrame, year: int, images_dir: str):
    """Stacked bars: monthly pump hours and energy split by power source."""
    pump_on = sim[sim['pump_on']].copy()
    pump_on['source'] = np.where(
        pump_on['P_battery_to_pump_kW'] > 0, 'Solar + Battery', 'Solar only')

    monthly_hrs = (pump_on.groupby(['month', 'source'])
                          .size()
                          .unstack(fill_value=0)
                          .reindex(columns=['Solar only', 'Solar + Battery'],
                                   fill_value=0))
    monthly_kwh = pump_on.groupby('month').agg(
        solar_kwh=('P_solar_to_pump_kW', 'sum'),
        batt_kwh =('P_battery_to_pump_kW', 'sum'),
    )

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.0))
    x = np.arange(1, 13)

    for ax, (col_a, col_b, ylabel, subtitle) in zip(axes, [
        (monthly_hrs.get('Solar only', pd.Series(0, index=x)),
         monthly_hrs.get('Solar + Battery', pd.Series(0, index=x)),
         'Pump run-hours', 'Monthly Pump Run-Hours by Power Source'),
        (monthly_kwh['solar_kwh'],
         monthly_kwh['batt_kwh'],
         'Energy delivered to pump (kWh)', 'Monthly Pump Energy by Source'),
    ]):
        ax.bar(x, col_a, color='#F9DC8C', edgecolor='white', lw=0.6,
               label='Solar direct' if 'kWh' in ylabel else 'Solar only')
        ax.bar(x, col_b, bottom=col_a.values if hasattr(col_a, 'values') else col_a,
               color='#5D8FBD', edgecolor='white', lw=0.6, label='Battery')
        ax.set_xticks(x); ax.set_xticklabels(MONTH_ABBR)
        ax.set_ylabel(ylabel)
        ax.set_title(subtitle)
        ax.legend(fontsize=8)

    fig.suptitle(_title('Monthly Pump Operation Summary', year), y=1.02)
    fig.tight_layout(pad=2.2)
    _save(fig, images_dir, _fname('P3_monthly_pump_breakdown', year))


# ---------------------------------------------------------------------------
# P4. Average hourly power balance (stacked area, hour of day)
# ---------------------------------------------------------------------------

def plot_hourly_power_balance(sim: pd.DataFrame, year: int, images_dir: str):
    """
    Stacked-area chart of the average disposition of solar AC power for
    each hour of the day across the full year.

    Note on apparent power levels
    ------------------------------
    All values shown are averages across all 365 days.  Since the pump
    only runs ~{MAX_HOURS_PER_DAY} hours each day and solar is only
    available for ~12 hours, the average power at any given hour is much
    lower than the peak.  For example, if the pump runs 6 h/day at
    1.263 kW, its 24-hour average contribution is only 0.32 kW — well
    below the pump threshold.  The stacked area reflects how those
    kilowatts are distributed across the day on average, not the
    instantaneous value during actual operation.
    """
    hourly = sim.groupby('hour').agg(
        solar_to_pump   =('P_solar_to_pump_kW',    'mean'),
        batt_to_pump    =('P_battery_to_pump_kW',  'mean'),
        solar_to_battery=('P_solar_to_battery_kW', 'mean'),
        curtailed       =('P_curtailed_kW',         'mean'),
    ).reset_index()

    hrs = hourly['hour'].values
    s2p = hourly['solar_to_pump'].values
    b2p = hourly['batt_to_pump'].values
    s2b = hourly['solar_to_battery'].values
    cur = hourly['curtailed'].values

    fig, ax = plt.subplots(figsize=(11, 5.0))
    ax.stackplot(hrs, s2p, b2p, s2b, cur,
                 labels=['Solar → pump (direct)',
                         'Battery → pump',
                         'Solar → battery (charging)',
                         'Curtailed / unused'],
                 colors=['#F9DC8C', '#5D8FBD', '#95C78F', '#aaaaaa'],
                 alpha=0.85)

    ax.set_xlabel('Hour of day (local standard time)')
    ax.set_ylabel('Average power (kW)  [across all 365 days]')
    ax.set_title(_title('Average Hourly Power Balance', year))
    ax.set_xlim(0, 23)
    ax.legend(fontsize=8, loc='upper left')
    fig.tight_layout(pad=2.2)
    _save(fig, images_dir, _fname('P4_hourly_power_balance', year))


# ---------------------------------------------------------------------------
# P5. Pump status heatmap (hour × day of year)
# ---------------------------------------------------------------------------

def plot_pump_heatmap(sim: pd.DataFrame, year: int, images_dir: str):
    """Heatmap: off (cream) / solar only (yellow) / solar + battery (blue)."""
    s = sim.copy()
    s['pump_code'] = 0
    s.loc[s['pump_on'] & (s['P_battery_to_pump_kW'] == 0), 'pump_code'] = 1
    s.loc[s['pump_on'] & (s['P_battery_to_pump_kW'] >  0), 'pump_code'] = 2

    pivot = s.pivot_table(index='hour', columns='day_of_year',
                          values='pump_code', aggfunc='max').fillna(0)

    cmap = mcolors.ListedColormap(['#F5F5F0', '#F9DC8C', '#5D8FBD'])
    norm = mcolors.BoundaryNorm([0, 0.5, 1.5, 2.5], cmap.N)

    fig, ax = plt.subplots(figsize=(15, 5.5))
    im = ax.imshow(pivot.values, aspect='auto', origin='lower',
                   cmap=cmap, norm=norm, extent=[1, 365, -0.5, 23.5])

    cbar = fig.colorbar(im, ax=ax, ticks=[0.25, 1.0, 2.0],
                        pad=0.015, fraction=0.025)
    cbar.ax.set_yticklabels(['Off', 'Solar only', 'Solar + Battery'], fontsize=8)

    month_days = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 365]
    month_mids = [(month_days[i]+month_days[i+1])/2 for i in range(12)]
    for d in month_days[1:-1]:
        ax.axvline(d, color='#AAAAAA', lw=0.5, alpha=0.7)

    ax.set_xticks(month_mids); ax.set_xticklabels(MONTH_ABBR, fontsize=9)
    ax.set_yticks(range(0, 24, 2))
    ax.set_ylabel('Hour of day (local standard time)')
    ax.set_title(_title('Pump Status — Hour of Day vs. Day of Year', year))
    fig.tight_layout(pad=2.5)
    _save(fig, images_dir, _fname('P5_pump_status_heatmap', year))


# ---------------------------------------------------------------------------
# P6. Battery charge / discharge heatmap (hour × day of year)
# ---------------------------------------------------------------------------

def plot_charge_discharge_heatmap(sim: pd.DataFrame, year: int, images_dir: str):
    """
    Heatmap of net battery energy flow (kWh/h) by hour of day and day of year.
    Green = charging, red = discharging, white = idle or no battery.
    """
    if BATTERY_CAPACITY_KWH == 0:
        print('  P6 skipped — no battery.')
        return

    pivot = sim.pivot_table(index='hour', columns='day_of_year',
                            values='battery_delta_kWh', aggfunc='mean').fillna(0)

    # Symmetric limits so zero = white
    lim = max(abs(pivot.values.min()), abs(pivot.values.max()), 0.1)

    fig, ax = plt.subplots(figsize=(15, 5.5))
    im = ax.imshow(pivot.values, aspect='auto', origin='lower',
                   cmap='RdYlGn', vmin=-lim, vmax=lim,
                   extent=[1, 365, -0.5, 23.5])

    cbar = fig.colorbar(im, ax=ax, pad=0.015, fraction=0.025)
    cbar.set_label('Net battery Δ (kWh/h)\n(+) charging   (−) discharging',
                   fontsize=8, labelpad=10)

    month_days = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 365]
    month_mids = [(month_days[i]+month_days[i+1])/2 for i in range(12)]
    for d in month_days[1:-1]:
        ax.axvline(d, color='#AAAAAA', lw=0.5, alpha=0.7)

    ax.set_xticks(month_mids); ax.set_xticklabels(MONTH_ABBR, fontsize=9)
    ax.set_yticks(range(0, 24, 2))
    ax.set_ylabel('Hour of day (local standard time)')
    ax.set_title(_title('Battery Charge / Discharge — Hour of Day vs. Day of Year', year))
    fig.tight_layout(pad=2.5)
    _save(fig, images_dir, _fname('P6_charge_discharge_heatmap', year))


# ---------------------------------------------------------------------------
# P7. Battery SoC heatmap (hour × month)
# ---------------------------------------------------------------------------

def plot_soc_heatmap(sim: pd.DataFrame, year: int, images_dir: str):
    """Average battery SoC (%) by hour of day and month."""
    if BATTERY_CAPACITY_KWH == 0:
        print('  P7 skipped — no battery.')
        return

    pivot = sim.pivot_table(index='hour', columns='month',
                            values='battery_soc_pct', aggfunc='mean')

    fig, ax = plt.subplots(figsize=(12, 5.5))
    im = ax.imshow(pivot.values, aspect='auto', origin='lower',
                   cmap='RdYlGn',
                   vmin=BATTERY_MIN_SOC_PCT * 100,
                   vmax=BATTERY_MAX_SOC_PCT * 100,
                   extent=[0.5, 12.5, -0.5, 23.5])

    cbar = fig.colorbar(im, ax=ax, pad=0.015, fraction=0.025)
    cbar.set_label('Average SoC (%)', fontsize=9, labelpad=10)

    ax.set_xticks(range(1, 13)); ax.set_xticklabels(MONTH_ABBR, fontsize=9)
    ax.set_yticks(range(0, 24, 2))
    ax.set_ylabel('Hour of day (local standard time)')
    ax.set_title(_title('Average Battery SoC (%) by Hour and Month', year))
    fig.tight_layout(pad=2.5)
    _save(fig, images_dir, _fname('P7_soc_heatmap', year))


# =============================================================================
# 5. MAIN
# =============================================================================

def run(input_csv: str, output_csv: str, images_dir: str):
    """Load → simulate → write CSV → summary → plots."""
    print(f'\nLoading solar data from: {input_csv}')
    df   = load_solar_csv(input_csv)
    year = int(df['datetime_local'].dt.year.iloc[0])
    print(f'  {len(df):,} hourly rows  |  year: {year}')
    print(f'  Battery: {_batt_label()}  |  Pump start hour: {PUMP_START_HOUR:02d}:00')

    print('Running hourly simulation …')
    sim = simulate(df)

    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
    sim.drop(columns=['month', 'day_of_year', 'hour']).to_csv(output_csv, index=False)
    print(f'  Results written to: {output_csv}')

    short_days = compute_summary(sim, year)

    print('Generating plots …')
    plot_battery_soc(sim, year, images_dir)
    plot_daily_pump_hours(sim, short_days, year, images_dir)
    plot_monthly_pump_breakdown(sim, year, images_dir)
    plot_hourly_power_balance(sim, year, images_dir)
    plot_pump_heatmap(sim, year, images_dir)
    plot_charge_discharge_heatmap(sim, year, images_dir)
    plot_soc_heatmap(sim, year, images_dir)

    n = len([f for f in os.listdir(images_dir) if f.endswith('.png')])
    print(f'\nAll done.  {n} plots saved to: {os.path.abspath(images_dir)}\n')
    return sim


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Hourly battery + pump simulation from a solar power CSV.')
    parser.add_argument('input_csv',  nargs='?', default=None)
    parser.add_argument('output_csv', nargs='?', default=None)
    parser.add_argument('--images-dir', default=None)
    args = parser.parse_args()

    run(
        input_csv  = args.input_csv  or INPUT_CSV,
        output_csv = args.output_csv or OUTPUT_CSV,
        images_dir = args.images_dir or IMAGES_DIR,
    )