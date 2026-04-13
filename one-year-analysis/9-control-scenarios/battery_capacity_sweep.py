"""
battery_capacity_sweep.py
=========================
Answers the question: "If we impose the continuous-run pump constraint, how
does the required battery capacity change for cassava vs. tomato?"

Sweeps battery nameplate capacity from 0 kWh to MAX_KWH and counts
unfulfilled irrigation days (days where the pump cannot achieve its full
target pump-hours) under continuous-run (Tier 1) control.  Runs for both
crops across all available seasons (2018–2024 planting years).

Control assumption — continuous-run (Tier 1)
--------------------------------------------
Once the pump starts for the day and energy fails in a subsequent hour,
the session ends for the day — no restart.  This models a farmer who
switches the pump on at PUMP_START_HOUR and leaves.

This is the worst-case battery sizing scenario: the battery must bridge
every cloud-induced dip within the scheduled block or the day is partially
unfulfilled.

The resulting curve should be monotonically non-increasing: more battery
capacity means at least as much energy is available at every point in the
day, so unfulfilled days can only decrease or stay the same as battery size
increases.

Compare with start_time_sensitivity.py, which asks a related question:
how does reliability change as a function of when the farmer arrives?

Usage
-----
    python battery_capacity_sweep.py
    python battery_capacity_sweep.py --max-kwh 12 --step 0.5
    python battery_capacity_sweep.py --no-degradation
    python battery_capacity_sweep.py --solar-following   # disable continuous-run

Outputs
-------
    9-control-scenarios/results/battery-sweep/battery_sweep_results.csv
    9-control-scenarios/images/battery-sweep/battery_sweep.png
"""

import argparse
import importlib.util
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

# =============================================================================
# PATH SETUP
# =============================================================================

_HERE    = os.path.dirname(os.path.abspath(__file__))
_ROOT    = os.path.normpath(os.path.join(_HERE, '..'))
# integrated_analysis.py lives in the 5-integrated-analysis library folder
_IA_PATH = os.path.join(_ROOT, '5-integrated-analysis', 'integrated_analysis.py')


def _load_ia():
    """Load a fresh integrated_analysis module instance (avoids state bleed)."""
    spec = importlib.util.spec_from_file_location('integrated_analysis', _IA_PATH)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# USER PARAMETERS
# =============================================================================

YEARS         = [2018, 2019, 2020, 2021, 2022, 2023, 2024]
CROPS         = ['cassava', 'tomato']
BATTERY_SWEEP = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0]  # nameplate kWh

# Fixed system parameters — consistent with run_simulation.py CONFIG
N_PANELS              = 15
PUMP_START_HOUR       = 8      # 8 AM default; consistent with run_simulation.py

# Battery degradation factor: LiFePO₄ ~5-year seasonal use
# (manufacturers specify 80% @ 2,000 cycles; ~85% at ~1,350 cycles)
BATTERY_DEGRADATION   = 0.85   # nameplate × factor = effective capacity

BATTERY_MIN_SOC_PCT   = 0.10
BATTERY_MAX_SOC_PCT   = 0.85   # 85% max is healthier for LiFePO₄ longevity
BATTERY_CHARGE_EFF    = 0.95
BATTERY_DISCHARGE_EFF = 0.95
INVERTER_EFF          = 0.96
PUMP_POWER_KW         = 1.263
# Solar must exceed this threshold before battery is allowed to discharge.
# Value matches run_simulation.py CONFIG (0.10 kW).
MIN_SOLAR_DISCHARGE   = 0.10   # kW AC

SOLAR_DIR   = os.path.join(_ROOT, '1-solar-power', 'gen-power')
SCHED_DIR   = os.path.join(_ROOT, '4-irrigation', 'results')
RESULTS_DIR = os.path.join(_HERE, 'results', 'battery-sweep')
IMAGES_DIR  = os.path.join(_HERE, 'images',  'battery-sweep')

# An irrigation day is "met" if pump hours achieved >= target_hrs - this tolerance
UNFULFILLED_TOL_HRS = 0.5

# =============================================================================
# MATPLOTLIB STYLE
# =============================================================================

plt.rcParams.update({
    'figure.dpi'        : 200,
    'savefig.dpi'       : 200,
    'savefig.bbox'      : 'tight',
    'savefig.pad_inches': 0.20,
    'font.family'       : 'sans-serif',
    'font.size'         : 9,
    'axes.spines.top'   : False,
    'axes.spines.right' : False,
    'axes.grid'         : True,
    'grid.alpha'        : 0.30,
    'axes.titlesize'    : 10,
    'axes.titleweight'  : 'semibold',
    'axes.titlepad'     : 10,
    'axes.labelsize'    : 9,
    'legend.fontsize'   : 8,
    'xtick.labelsize'   : 8,
    'ytick.labelsize'   : 8,
})

CROP_COLORS = {'cassava': '#47965A', 'tomato': '#C0392B'}

# =============================================================================
# HELPERS
# =============================================================================

def _load_solar_both_years(ia_mod, planting_year: int) -> dict:
    """
    Load solar power for both calendar years spanning the growing season.
    A Sep→May season for planting year Y uses data from Y and Y+1.
    """
    solar = {}
    for cal_yr in [planting_year, planting_year + 1]:
        try:
            solar.update(ia_mod.load_solar_power(SOLAR_DIR, cal_yr))
        except FileNotFoundError:
            pass
    return solar


def _patch_fixed_globals(ia_mod, continuous_run: bool) -> None:
    """Apply fixed system parameters to an integrated_analysis module instance."""
    ia_mod.INVERTER_EFF            = INVERTER_EFF
    ia_mod.PUMP_POWER_KW           = PUMP_POWER_KW
    ia_mod.MIN_SOLAR_FOR_DISCHARGE = MIN_SOLAR_DISCHARGE
    ia_mod.BATTERY_CHARGE_EFF      = BATTERY_CHARGE_EFF
    ia_mod.BATTERY_DISCHARGE_EFF   = BATTERY_DISCHARGE_EFF
    ia_mod.N_PANELS                = N_PANELS
    ia_mod.MAX_DAILY_HRS           = 8.0
    ia_mod.CONTINUOUS_RUN          = continuous_run   # control mode


def _patch_battery_globals(ia_mod, eff_kwh: float) -> None:
    """Patch battery-related globals for a specific effective capacity."""
    ia_mod.BATTERY_CAPACITY_KWH    = eff_kwh
    ia_mod.BATTERY_MIN_SOC_PCT     = BATTERY_MIN_SOC_PCT
    ia_mod.BATTERY_MAX_SOC_PCT     = BATTERY_MAX_SOC_PCT
    ia_mod.BATTERY_INITIAL_SOC_PCT = BATTERY_MAX_SOC_PCT   # start fully charged
    ia_mod.PUMP_START_HOUR         = PUMP_START_HOUR


def _count_day_results(records: list) -> tuple[int, int]:
    """
    Aggregate hourly simulation records to daily level and count:
        - total irrigation days
        - unfulfilled irrigation days (pump_hrs < target_hrs - tolerance)

    Returns
    -------
    (total_irr_days, unfulfilled_days)
    """
    daily: dict = {}
    for r in records:
        d = r['date']
        if d not in daily:
            daily[d] = {
                'is_irr_day': r['is_irr_day'],
                'target_hrs': r['target_hrs'],
                'pump_hrs'  : 0.0,
            }
        if r['pump_on']:
            daily[d]['pump_hrs'] += 1.0

    total = 0
    unf   = 0
    for v in daily.values():
        if v['is_irr_day']:
            total += 1
            if v['pump_hrs'] < v['target_hrs'] - UNFULFILLED_TOL_HRS:
                unf += 1
    return total, unf


# =============================================================================
# MAIN SWEEP
# =============================================================================

def run_sweep(battery_sweep: list, apply_degradation: bool = True,
              continuous_run: bool = True) -> pd.DataFrame:
    """
    Execute the battery capacity sweep for all crops × years × capacities.

    Parameters
    ----------
    battery_sweep      : list of nameplate capacities [kWh] to test
    apply_degradation  : if True, multiply nameplate by BATTERY_DEGRADATION
                         to get effective capacity (default True)
    continuous_run     : if True (default), enable continuous-run mode in the
                         simulation — once pump is interrupted it does not
                         restart that day.  Set False for solar-following mode.

    Returns
    -------
    DataFrame with one row per (crop, year, battery_nameplate_kwh)
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(IMAGES_DIR,  exist_ok=True)

    deg_factor   = BATTERY_DEGRADATION if apply_degradation else 1.0
    total_sims   = len(CROPS) * len(YEARS) * len(battery_sweep)
    n_done       = 0
    rows         = []
    mode_label   = 'continuous-run' if continuous_run else 'solar-following'

    print(f'\nBattery Capacity Sweep  [{mode_label}]')
    print(f'  Crops       : {CROPS}')
    print(f'  Years       : {YEARS}')
    print(f'  Capacities  : {battery_sweep} kWh (nameplate)')
    print(f'  Degradation : {"×" + str(deg_factor) if apply_degradation else "none (nameplate = effective)"}')
    print(f'  Start hour  : {PUMP_START_HOUR}:00')
    print(f'  Total sims  : {total_sims}\n')

    for crop in CROPS:
        print(f'{"="*60}')
        print(f'Crop: {crop}')
        print(f'{"="*60}')

        for year in YEARS:
            # Load a fresh module instance per (crop × year) to prevent
            # global state from leaking between battery sweep iterations.
            ia = _load_ia()
            _patch_fixed_globals(ia, continuous_run)

            solar = _load_solar_both_years(ia, year)
            if not solar:
                print(f'  SKIP {crop}/{year}: no solar power data found')
                n_done += len(battery_sweep)
                continue

            try:
                daily_sched = ia.load_daily_targets(SCHED_DIR, crop, year)
            except FileNotFoundError as e:
                print(f'  SKIP {crop}/{year}: {e}')
                n_done += len(battery_sweep)
                continue
            except KeyError as e:
                print(f'  SKIP {crop}/{year}: missing column {e} — re-run irrigation_schedule.py')
                n_done += len(battery_sweep)
                continue

            for nameplate_kwh in battery_sweep:
                eff_kwh = nameplate_kwh * deg_factor
                _patch_battery_globals(ia, eff_kwh)

                records = ia.simulate_season(solar, daily_sched, eff_kwh, N_PANELS)
                total_irr, unf = _count_day_results(records)
                pct = 100.0 * (total_irr - unf) / total_irr if total_irr else 100.0

                rows.append({
                    'crop'                  : crop,
                    'year'                  : year,
                    'control_mode'          : mode_label,
                    'battery_nameplate_kwh' : nameplate_kwh,
                    'battery_eff_kwh'       : round(eff_kwh, 3),
                    'irrigation_days'       : total_irr,
                    'unfulfilled_days'      : unf,
                    'fulfilled_days'        : total_irr - unf,
                    'fulfillment_pct'       : round(pct, 1),
                })

                n_done += 1
                print(f'  [{n_done:3d}/{total_sims}] {crop} {year} '
                      f'{nameplate_kwh:5.1f} kWh nameplate '
                      f'({eff_kwh:.2f} kWh eff) → '
                      f'{unf}/{total_irr} unf ({100 - pct:.1f}%)')

    df = pd.DataFrame(rows)
    mode_sfx = 'cr' if continuous_run else 'sf'
    csv_path = os.path.join(RESULTS_DIR, f'battery_sweep_results_{mode_sfx}.csv')
    df.to_csv(csv_path, index=False)
    print(f'\nResults CSV → {csv_path}')

    plot_sweep(df, apply_degradation, continuous_run)
    return df


# =============================================================================
# PLOTTING
# =============================================================================

def plot_sweep(df: pd.DataFrame, apply_degradation: bool = True,
               continuous_run: bool = True) -> None:
    """
    Two-panel figure: unfulfilled irrigation days per season vs battery
    nameplate capacity.  Per-year lines are shown faintly with a bold
    7-year mean overlaid.  Shaded band spans the min/max across years.
    """
    crops = [c for c in CROPS if c in df['crop'].unique()]
    fig, axes = plt.subplots(1, len(crops), figsize=(5.8 * len(crops), 4.8))
    if len(crops) == 1:
        axes = [axes]

    for ax, crop in zip(axes, crops):
        sub   = df[df['crop'] == crop].copy()
        color = CROP_COLORS.get(crop, '#3A7FC1')
        years = sorted(sub['year'].unique())

        # Per-year thin background lines
        for yr in years:
            ys = sub[sub['year'] == yr].sort_values('battery_nameplate_kwh')
            ax.plot(ys['battery_nameplate_kwh'], ys['unfulfilled_days'],
                    color=color, alpha=0.20, linewidth=0.9, zorder=2)

        # Mean ± min/max band
        mean_df = (sub.groupby('battery_nameplate_kwh', as_index=False)
                   .agg(mean_unf=('unfulfilled_days', 'mean'),
                        min_unf =('unfulfilled_days', 'min'),
                        max_unf =('unfulfilled_days', 'max')))
        mean_df = mean_df.sort_values('battery_nameplate_kwh')

        ax.fill_between(mean_df['battery_nameplate_kwh'],
                        mean_df['min_unf'], mean_df['max_unf'],
                        color=color, alpha=0.12, zorder=1, label='Year range')
        ax.plot(mean_df['battery_nameplate_kwh'], mean_df['mean_unf'],
                color=color, linewidth=2.2, zorder=3, label='7-year mean')

        # Baseline marker at 2 kWh nameplate
        if 2.0 in mean_df['battery_nameplate_kwh'].values:
            ax.axvline(2.0, color='#888888', linestyle='--',
                       linewidth=0.9, alpha=0.7, zorder=4)
            y_top = mean_df['max_unf'].max()
            ax.text(2.08, y_top * 0.97 if y_top > 0 else 1,
                    '2 kWh\nbaseline', fontsize=7, color='#666666', va='top')

        # Year labels at right end of each faint line
        for yr in years:
            ys = sub[sub['year'] == yr].sort_values('battery_nameplate_kwh')
            if not ys.empty:
                last = ys.iloc[-1]
                ax.text(last['battery_nameplate_kwh'] + 0.08,
                        last['unfulfilled_days'],
                        str(yr), fontsize=6, color=color, alpha=0.55, va='center')

        ax.set_title(f'{crop.capitalize()}')
        ax.set_xlabel('Battery nameplate capacity [kWh]')
        ax.set_ylabel('Unfulfilled irrigation days per season')
        ax.xaxis.set_major_locator(ticker.MultipleLocator(1.0))
        ax.xaxis.set_minor_locator(ticker.MultipleLocator(0.5))
        ax.set_xlim(left=-0.1)
        ax.set_ylim(bottom=0)
        ax.legend(loc='upper right')


    mode_label = 'continuous-run (Tier 1)' if continuous_run else 'solar-following'
    deg_note   = (f', ×{BATTERY_DEGRADATION} degradation'
                  if apply_degradation else ', no degradation')
    plt.suptitle(
        f'Battery sizing — unfulfilled irrigation days vs capacity  [{mode_label}]\n'
        f'15 panels × 405 W · 1.263 kW pump · {PUMP_START_HOUR}:00 start{deg_note}',
        fontsize=9, y=1.01,
    )

    fig.tight_layout()
    mode_sfx = 'cr' if continuous_run else 'sf'
    out_path = os.path.join(IMAGES_DIR, f'battery_sweep_{mode_sfx}.png')
    fig.savefig(out_path)
    plt.close(fig)
    print(f'Plot         → {out_path}')


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Battery capacity sweep for irrigation reliability (control scenarios)')
    parser.add_argument('--max-kwh', type=float, default=10.0,
                        help='Upper bound of nameplate capacity sweep [kWh]')
    parser.add_argument('--step', type=float, default=None,
                        help='If provided, generate a linear sweep 0→max-kwh '
                             'at this step size instead of the default list')
    parser.add_argument('--no-degradation', action='store_true',
                        help='Use nameplate capacity directly (skip degradation factor)')
    parser.add_argument('--solar-following', action='store_true',
                        help='Use solar-following mode instead of continuous-run '
                             '(pump can skip cloudy hours and resume later)')
    args = parser.parse_args()

    sweep = BATTERY_SWEEP
    if args.step is not None:
        sweep = list(np.round(np.arange(0, args.max_kwh + args.step / 2, args.step), 3))
    elif args.max_kwh < max(BATTERY_SWEEP):
        sweep = [x for x in BATTERY_SWEEP if x <= args.max_kwh]

    run_sweep(
        battery_sweep=sweep,
        apply_degradation=not args.no_degradation,
        continuous_run=not args.solar_following,
    )
