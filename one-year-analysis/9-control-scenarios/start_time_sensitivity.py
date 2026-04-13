"""
start_time_sensitivity.py
=========================
Answers the question: "How reliable is the system when the farmer starts
watering at 8 AM vs. 10 AM vs. noon?"

Sweeps the pump start hour from 06:00 to 14:00 and measures irrigation day
fulfillment rate (fraction of scheduled days where the pump achieves its
full target pump-hours) under continuous-run (Tier 1) control.  Runs for
both crops across all available seasons (2018–2024 planting years) using
the 2 kWh reference battery.

Control assumption — continuous-run (Tier 1)
--------------------------------------------
Once the pump starts for the day and energy fails in a subsequent hour,
the session ends for the day.  Earlier starts give more solar hours but
less battery pre-charge; later starts give a fuller battery but a shorter
window to accumulate target hours.  This trade-off is the subject of this
script.

Failure mode documented here:
    "The irrigation system does not fulfill needs if it is started too
    late in the day."

Compare with battery_capacity_sweep.py, which asks: given a fixed 8 AM
start, how much battery is needed to achieve full reliability?

Usage
-----
    python start_time_sensitivity.py
    python start_time_sensitivity.py --battery-kwh 2.0
    python start_time_sensitivity.py --battery-kwh 0 --no-degradation
    python start_time_sensitivity.py --solar-following

Outputs
-------
    9-control-scenarios/results/start-time/start_time_results_b<N>kWh.csv
    9-control-scenarios/images/start-time/start_time_sensitivity_b<N>kWh.png
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

YEARS            = [2018, 2019, 2020, 2021, 2022, 2023, 2024]
CROPS            = ['cassava', 'tomato']
START_HOUR_SWEEP = list(range(6, 15))   # 06:00 to 14:00 inclusive

# Battery baseline — 2 kWh nameplate (project reference configuration)
BATTERY_NAMEPLATE_KWH = 2.0

# Degradation factor — same as run_simulation.py CONFIG
BATTERY_DEGRADATION   = 0.85
BATTERY_MIN_SOC_PCT   = 0.10
BATTERY_MAX_SOC_PCT   = 0.85
BATTERY_CHARGE_EFF    = 0.95
BATTERY_DISCHARGE_EFF = 0.95

N_PANELS            = 15
INVERTER_EFF        = 0.96
PUMP_POWER_KW       = 1.263
MIN_SOLAR_DISCHARGE = 0.10   # kW AC — consistent with run_simulation.py CONFIG

SOLAR_DIR   = os.path.join(_ROOT, '1-solar-power', 'gen-power')
SCHED_DIR   = os.path.join(_ROOT, '4-irrigation', 'results')
RESULTS_DIR = os.path.join(_HERE, 'results', 'start-time')
IMAGES_DIR  = os.path.join(_HERE, 'images',  'start-time')

UNFULFILLED_TOL_HRS = 0.5   # day is "met" if pump_hrs >= target_hrs - this

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
    A Sep→May season for planting year Y requires solar data for Y and Y+1.
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


def _patch_battery_globals(ia_mod, eff_kwh: float, start_hour: int) -> None:
    """Patch battery and schedule globals for one simulation run."""
    ia_mod.BATTERY_CAPACITY_KWH    = eff_kwh
    ia_mod.BATTERY_MIN_SOC_PCT     = BATTERY_MIN_SOC_PCT
    ia_mod.BATTERY_MAX_SOC_PCT     = BATTERY_MAX_SOC_PCT
    ia_mod.BATTERY_INITIAL_SOC_PCT = BATTERY_MAX_SOC_PCT   # start fully charged
    ia_mod.PUMP_START_HOUR         = start_hour


def _count_day_results(records: list) -> tuple[int, int]:
    """
    Aggregate hourly records to daily level and count:
        - total irrigation days  (is_irr_day == True)
        - met irrigation days    (pump_hrs >= target_hrs - tolerance)

    Returns
    -------
    (total_irr_days, met_days)
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
    met   = 0
    for v in daily.values():
        if v['is_irr_day']:
            total += 1
            if v['pump_hrs'] >= v['target_hrs'] - UNFULFILLED_TOL_HRS:
                met += 1
    return total, met


# =============================================================================
# MAIN SWEEP
# =============================================================================

def run_sweep(battery_nameplate_kwh: float, crops: list,
              apply_degradation: bool = True,
              continuous_run: bool = True) -> pd.DataFrame:
    """
    Execute the start-hour sensitivity sweep for all crops × years × start hours.

    Parameters
    ----------
    battery_nameplate_kwh : nameplate battery capacity [kWh]
    crops                 : list of crop names to simulate
    apply_degradation     : if True, multiply nameplate by BATTERY_DEGRADATION
    continuous_run        : if True (default), enable continuous-run mode

    Returns
    -------
    DataFrame with one row per (crop, year, start_hour)
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(IMAGES_DIR,  exist_ok=True)

    deg_factor  = BATTERY_DEGRADATION if apply_degradation else 1.0
    eff_kwh     = battery_nameplate_kwh * deg_factor
    total_sims  = len(crops) * len(YEARS) * len(START_HOUR_SWEEP)
    n_done      = 0
    rows        = []
    mode_label  = 'continuous-run' if continuous_run else 'solar-following'

    print(f'\nStart-Time Sensitivity Analysis  [{mode_label}]')
    print(f'  Crops            : {crops}')
    print(f'  Years            : {YEARS}')
    print(f'  Start hours      : {START_HOUR_SWEEP[0]:02d}:00 – {START_HOUR_SWEEP[-1]:02d}:00')
    print(f'  Battery          : {battery_nameplate_kwh:.1f} kWh nameplate '
          f'→ {eff_kwh:.2f} kWh effective')
    print(f'  Total sims       : {total_sims}\n')

    for crop in crops:
        print(f'{"="*60}')
        print(f'Crop: {crop}')
        print(f'{"="*60}')

        for year in YEARS:
            # Fresh module per (crop × year) to prevent global state bleeding
            # between start-hour iterations within the same year.
            ia = _load_ia()
            _patch_fixed_globals(ia, continuous_run)

            solar = _load_solar_both_years(ia, year)
            if not solar:
                print(f'  SKIP {crop}/{year}: no solar power data found')
                n_done += len(START_HOUR_SWEEP)
                continue

            try:
                daily_sched = ia.load_daily_targets(SCHED_DIR, crop, year)
            except FileNotFoundError as e:
                print(f'  SKIP {crop}/{year}: {e}')
                n_done += len(START_HOUR_SWEEP)
                continue
            except KeyError as e:
                print(f'  SKIP {crop}/{year}: missing column {e} — '
                      're-run irrigation_schedule.py')
                n_done += len(START_HOUR_SWEEP)
                continue

            for start_hr in START_HOUR_SWEEP:
                _patch_battery_globals(ia, eff_kwh, start_hr)

                records = ia.simulate_season(solar, daily_sched, eff_kwh, N_PANELS)
                total, met = _count_day_results(records)
                pct        = 100.0 * met / total if total else 100.0

                rows.append({
                    'crop'             : crop,
                    'year'             : year,
                    'control_mode'     : mode_label,
                    'start_hour'       : start_hr,
                    'battery_nameplate': battery_nameplate_kwh,
                    'battery_eff_kwh'  : round(eff_kwh, 3),
                    'irrigation_days'  : total,
                    'met_days'         : met,
                    'unfulfilled_days' : total - met,
                    'fulfillment_pct'  : round(pct, 1),
                })

                n_done += 1
                print(f'  [{n_done:3d}/{total_sims}] {crop} {year} '
                      f'start {start_hr:02d}:00 → '
                      f'{met}/{total} met ({pct:.1f}%)')

    df = pd.DataFrame(rows)
    mode_sfx = 'cr' if continuous_run else 'sf'
    tag      = f'b{battery_nameplate_kwh:.0f}kWh_{mode_sfx}'
    csv_path = os.path.join(RESULTS_DIR, f'start_time_results_{tag}.csv')
    df.to_csv(csv_path, index=False)
    print(f'\nResults CSV → {csv_path}')

    plot_sensitivity(df, battery_nameplate_kwh, apply_degradation, continuous_run)
    return df


# =============================================================================
# PLOTTING
# =============================================================================

def plot_sensitivity(df: pd.DataFrame, battery_nameplate_kwh: float,
                     apply_degradation: bool = True,
                     continuous_run: bool = True) -> None:
    """
    One panel per crop: fulfillment rate (% of irrigation days fully met)
    vs pump start hour.  Per-year thin lines with bold 7-year mean overlaid
    and a min/max shaded band.
    """
    crops  = [c for c in CROPS if c in df['crop'].unique()]
    n_cols = len(crops)
    fig, axes = plt.subplots(1, n_cols, figsize=(5.8 * n_cols, 4.8))
    if n_cols == 1:
        axes = [axes]

    eff_kwh = battery_nameplate_kwh * (BATTERY_DEGRADATION if apply_degradation else 1.0)

    for ax, crop in zip(axes, crops):
        sub   = df[df['crop'] == crop].copy()
        color = CROP_COLORS.get(crop, '#3A7FC1')
        years = sorted(sub['year'].unique())

        # Per-year thin lines
        for yr in years:
            ys = sub[sub['year'] == yr].sort_values('start_hour')
            ax.plot(ys['start_hour'], ys['fulfillment_pct'],
                    color=color, alpha=0.20, linewidth=0.9, zorder=2)

        # Mean ± min/max shaded band
        mean_df = (sub.groupby('start_hour', as_index=False)
                   .agg(mean_pct=('fulfillment_pct', 'mean'),
                        min_pct =('fulfillment_pct', 'min'),
                        max_pct =('fulfillment_pct', 'max')))
        mean_df = mean_df.sort_values('start_hour')

        ax.fill_between(mean_df['start_hour'],
                        mean_df['min_pct'], mean_df['max_pct'],
                        color=color, alpha=0.12, zorder=1, label='Year range')
        ax.plot(mean_df['start_hour'], mean_df['mean_pct'],
                color=color, linewidth=2.2, zorder=3, label='7-year mean')

        # Reference line at 8 AM default
        ax.axvline(8, color='#888888', linestyle='--',
                   linewidth=0.9, alpha=0.7, zorder=4)
        ax.text(8.12, 3, '8 AM\ndefault',
                fontsize=7, color='#666666', va='bottom')

        # Reference line at 90% fulfillment threshold
        ax.axhline(90, color='#AAAAAA', linestyle=':', linewidth=0.8, alpha=0.8)
        ax.text(START_HOUR_SWEEP[0] + 0.05, 90.5, '90%',
                fontsize=7, color='#888888', va='bottom')

        # Year labels at right end of each faint line
        for yr in years:
            ys = sub[sub['year'] == yr].sort_values('start_hour')
            if not ys.empty:
                last = ys.iloc[-1]
                ax.text(last['start_hour'] + 0.08,
                        last['fulfillment_pct'],
                        str(yr), fontsize=6, color=color, alpha=0.55, va='center')

        ax.set_title(f'{crop.capitalize()}')
        ax.set_xlabel('Pump start hour (local time)')
        ax.set_ylabel('Irrigation days fully met [%]')
        ax.set_xlim(START_HOUR_SWEEP[0] - 0.3, START_HOUR_SWEEP[-1] + 0.6)
        ax.set_ylim(0, 105)
        ax.xaxis.set_major_locator(ticker.MultipleLocator(1))
        ax.set_xticks(START_HOUR_SWEEP)
        ax.set_xticklabels([f'{h:02d}:00' for h in START_HOUR_SWEEP],
                           rotation=35, ha='right')
        ax.legend(loc='lower left')

    mode_label = 'continuous-run (Tier 1)' if continuous_run else 'solar-following'
    deg_note   = (f', ×{BATTERY_DEGRADATION} degradation'
                  if apply_degradation else ', no degradation')
    plt.suptitle(
        f'Start-time sensitivity: fulfillment rate vs pump start hour  [{mode_label}]\n'
        f'{battery_nameplate_kwh:.0f} kWh nameplate ({eff_kwh:.2f} kWh eff{deg_note}) · '
        f'15 panels × 405 W · 1.263 kW pump',
        fontsize=9, y=1.01,
    )

    fig.tight_layout()
    mode_sfx = 'cr' if continuous_run else 'sf'
    tag      = f'b{battery_nameplate_kwh:.0f}kWh_{mode_sfx}'
    out_path = os.path.join(IMAGES_DIR, f'start_time_sensitivity_{tag}.png')
    fig.savefig(out_path)
    plt.close(fig)
    print(f'Plot         → {out_path}')


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Start-time sensitivity analysis for irrigation reliability (control scenarios)')
    parser.add_argument('--battery-kwh', type=float, default=BATTERY_NAMEPLATE_KWH,
                        help='Battery nameplate capacity to use [kWh] (default: 2.0)')
    parser.add_argument('--crops', nargs='+', default=CROPS,
                        help='Crop names to simulate (default: cassava tomato)')
    parser.add_argument('--no-degradation', action='store_true',
                        help='Use nameplate capacity directly (skip degradation factor)')
    parser.add_argument('--solar-following', action='store_true',
                        help='Use solar-following mode instead of continuous-run '
                             '(pump can skip cloudy hours and resume later)')
    args = parser.parse_args()

    run_sweep(
        battery_nameplate_kwh=args.battery_kwh,
        crops=args.crops,
        apply_degradation=not args.no_degradation,
        continuous_run=not args.solar_following,
    )
