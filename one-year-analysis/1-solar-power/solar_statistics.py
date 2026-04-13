"""
solar_statistics.py
===================
Descriptive statistics and visualisations for a solar power generation CSV
produced by solar_analysis.py.

VSCode usage (quickest)
-----------------------
1. Open this file in VSCode.
2. Edit YEAR (and paths if needed) in the USER PARAMETERS section below.
3. Optionally adjust THRESHOLD_KW.
4. Press F5 (or Run ▷ in the top-right corner) to run the whole script.

Terminal usage
--------------
    python solar_statistics.py <input_csv> [--images-dir PATH] [--threshold KW]

    Example:
        python solar_statistics.py gen-power/4469509_24.96_-78.05_2018_power.csv \
            --images-dir images/2018 --threshold 1.0

Dependencies
------------
    pip install pandas matplotlib numpy

Plot inventory (in output order)
---------------------------------
    01_monthly_energy.png         — Total kWh per month (bar chart)
    02_monthly_boxplots.png       — Distribution of daily kWh by month
    03_daily_energy.png           — Daily kWh across the year + rolling avg
    04_power_heatmap.png          — Power by hour-of-day × day-of-year
    05_hourly_profile.png         — Hourly power curves (all days + mean±std)
    06_hours_above_threshold.png  — Hours/day above a power threshold
    07_irradiance_vs_power.png    — Irradiance–power relationship (scatter +
                                    monthly GHI correlation)
    08_temperature_effect.png     — Temperature derating (normalized output)
"""

# =============================================================================
# USER PARAMETERS  — edit these when running from VSCode / F5
# =============================================================================

YEAR         = 2018
INPUT_CSV    = f'one-year-analysis/1-solar-power/gen-power/4469509_24.96_-78.05_{YEAR}_power.csv'
IMAGES_DIR   = f'one-year-analysis/1-solar-power/images/{YEAR}'
THRESHOLD_KW = 1.0      # Power threshold for the hours-above plot [kW]

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

# ---------------------------------------------------------------------------
# Month colour palette — seasonal gradient:
#   blues for winter → greens for spring → warm yellows/oranges for summer
#   → mauves/purples for autumn
# ---------------------------------------------------------------------------
MONTH_COLORS = [
    '#7EB8D4',  # Jan  — steel blue        (deep winter)
    '#A8CDE0',  # Feb  — powder blue
    '#95C78F',  # Mar  — spring green
    '#6DB56A',  # Apr  — medium green
    '#C5E0A5',  # May  — yellow-green
    '#F9DC8C',  # Jun  — warm yellow
    '#F5A74E',  # Jul  — orange
    '#EF8236',  # Aug  — deep orange       (peak summer)
    '#D4956A',  # Sep  — terracotta
    '#C49BC4',  # Oct  — mauve
    '#9B79BA',  # Nov  — amethyst
    '#5D8FBD',  # Dec  — periwinkle blue
]

MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun',
               'Jul','Aug','Sep','Oct','Nov','Dec']

# Consistent rcParams for all figures
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

# Custom heatmap colormap: very pale cream → deep red
_HEAT_COLORS = ['#FFFEF5', '#FFF5CC', '#FFD966', '#FF8800', '#C92A00']
HEAT_CMAP    = mcolors.LinearSegmentedColormap.from_list('solar_heat', _HEAT_COLORS)

SAVE_DPI = 200

# =============================================================================
# 1. DATA LOADING
# =============================================================================

def load_data(csv_path: str) -> pd.DataFrame:
    """
    Load a solar_analysis.py output CSV into a tidy DataFrame.

    Derived columns added
    ---------------------
    date        : calendar date
    hour        : integer hour of day (0–23)
    month       : integer month (1–12)
    day_of_year : integer 1–365
    is_daytime  : True when G_tilted_W_m2 > 0
    year        : integer calendar year
    """
    df = pd.read_csv(csv_path, parse_dates=['datetime_local'])
    df = df.sort_values('datetime_local').reset_index(drop=True)

    df['date']        = df['datetime_local'].dt.date
    df['hour']        = df['datetime_local'].dt.hour
    df['month']       = df['datetime_local'].dt.month
    df['day_of_year'] = df['datetime_local'].dt.day_of_year
    df['year']        = df['datetime_local'].dt.year
    df['is_daytime']  = df['G_tilted_W_m2'] > 0

    return df

# =============================================================================
# 2. DESCRIPTIVE STATISTICS
# =============================================================================

def compute_statistics(df: pd.DataFrame, threshold_kw: float) -> dict:
    """
    Compute a comprehensive set of descriptive statistics and print a report.
    """
    peak_idx = df['P_dc_kW'].idxmax()
    peak_row = df.loc[peak_idx]
    day_df   = df[df['is_daytime']]

    daily_kwh   = df.groupby('date')['P_dc_kW'].sum()
    monthly_kwh = df.groupby('month')['P_dc_kW'].sum()

    above = df[df['P_dc_kW'] >= threshold_kw]
    hours_above_per_day = above.groupby('date').size().reindex(
        daily_kwh.index, fill_value=0)

    total_kwh       = daily_kwh.sum()
    rated_kw        = df['P_dc_kW'].max()
    capacity_factor = total_kwh / (rated_kw * len(df))

    stats = {
        'year'                    : int(df['year'].iloc[0]),

        # Peak
        'peak_power_kw'           : peak_row['P_dc_kW'],
        'peak_datetime'           : peak_row['datetime_local'],
        'peak_g_tilted'           : peak_row['G_tilted_W_m2'],
        'peak_t_cell_c'           : peak_row['T_cell_C'],
        'peak_t_amb_c'            : peak_row['T_amb_C'],

        # Averages
        'avg_all_hours_kw'        : df['P_dc_kW'].mean(),
        'avg_daytime_kw'          : day_df['P_dc_kW'].mean(),
        'avg_daytime_hours_day'   : df.groupby('date')['is_daytime'].sum().mean(),
        'avg_daily_kwh'           : daily_kwh.mean(),
        'median_daily_kwh'        : daily_kwh.median(),

        # Energy
        'total_annual_kwh'        : total_kwh,
        'best_day_kwh'            : daily_kwh.max(),
        'best_day_date'           : daily_kwh.idxmax(),
        'worst_day_kwh'           : daily_kwh.min(),
        'worst_day_date'          : daily_kwh.idxmin(),
        'monthly_kwh'             : monthly_kwh,
        'daily_kwh'               : daily_kwh,

        # Threshold
        'threshold_kw'            : threshold_kw,
        'avg_hours_above_thresh'  : hours_above_per_day.mean(),
        'total_hours_above_thresh': hours_above_per_day.sum(),
        'hours_above_per_day'     : hours_above_per_day,

        # Performance
        'capacity_factor_pct'     : capacity_factor * 100,
    }

    _print_statistics(stats)
    return stats


def _print_statistics(s: dict):
    sep = '=' * 58
    yr  = s['year']

    print(f'\n{sep}')
    print(f'  Solar Power Generation {yr} — Descriptive Statistics')
    print(sep)

    print('\n  PEAK POWER')
    print(f'    Peak output        : {s["peak_power_kw"]:.3f} kW')
    print(f'    When               : {s["peak_datetime"].strftime("%Y-%m-%d  %H:%M  (local std time)")}')
    print(f'    In-plane irrad.    : {s["peak_g_tilted"]:.1f} W/m²')
    print(f'    Ambient temp       : {s["peak_t_amb_c"]:.1f} °C')
    print(f'    Cell temperature   : {s["peak_t_cell_c"]:.1f} °C')

    print('\n  AVERAGES')
    print(f'    All-hours avg power: {s["avg_all_hours_kw"]:.3f} kW')
    print(f'    Daytime avg power  : {s["avg_daytime_kw"]:.3f} kW  (when G_tilted > 0)')
    print(f'    Avg daytime hours  : {s["avg_daytime_hours_day"]:.1f} h/day')
    print(f'    Avg daily energy   : {s["avg_daily_kwh"]:.2f} kWh/day')
    print(f'    Median daily energy: {s["median_daily_kwh"]:.2f} kWh/day')

    print('\n  ENERGY')
    print(f'    Total annual energy: {s["total_annual_kwh"]:,.1f} kWh')
    print(f'    Best day           : {s["best_day_kwh"]:.2f} kWh  ({s["best_day_date"]})')
    print(f'    Worst day          : {s["worst_day_kwh"]:.2f} kWh  ({s["worst_day_date"]})')

    print('\n  MONTHLY ENERGY (kWh)')
    for m, kwh in s['monthly_kwh'].items():
        bar = '█' * int(kwh / s['monthly_kwh'].max() * 24)
        print(f'    {MONTH_ABBR[m-1]:>3}  {kwh:>7.1f}  {bar}')

    thresh = s['threshold_kw']
    print(f'\n  HOURS ABOVE {thresh:.1f} kW THRESHOLD')
    print(f'    Avg per day        : {s["avg_hours_above_thresh"]:.1f} h/day')
    print(f'    Annual total       : {s["total_hours_above_thresh"]} h')

    print(f'\n  CAPACITY FACTOR    : {s["capacity_factor_pct"]:.1f} %')
    print(f'{sep}\n')

# =============================================================================
# 3. PLOTS
# =============================================================================

def _save(fig: plt.Figure, images_dir: str, filename: str):
    os.makedirs(images_dir, exist_ok=True)
    path = os.path.join(images_dir, filename)
    fig.savefig(path, bbox_inches='tight', pad_inches=0.35, dpi=SAVE_DPI)
    plt.close(fig)
    print(f'  Saved → {path}')


# ---------------------------------------------------------------------------
# 01. Monthly energy bar chart
# ---------------------------------------------------------------------------

def plot_monthly_energy(stats: dict, images_dir: str):
    """Total kWh per month with annotated values."""
    year    = stats['year']
    monthly = stats['monthly_kwh']

    fig, ax = plt.subplots(figsize=(10, 4.5))
    bars = ax.bar(range(1, 13), monthly.values,
                  color=MONTH_COLORS, edgecolor='white', linewidth=0.8)

    for bar, val in zip(bars, monthly.values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 6,
                f'{val:.0f}', ha='center', va='bottom', fontsize=8)

    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(MONTH_ABBR)
    ax.set_title(f'Monthly Energy Output — {year}')
    ax.set_ylabel('Energy (kWh)')
    ax.set_ylim(0, monthly.max() * 1.15)
    ax.axhline(monthly.mean(), lw=1.2, ls='--', color='#444444',
               label=f'Monthly avg  {monthly.mean():.0f} kWh')
    ax.legend(fontsize=8)
    fig.tight_layout(pad=2.2)

    _save(fig, images_dir, '01_monthly_energy.png')


# ---------------------------------------------------------------------------
# 02. Monthly box plots of daily energy
# ---------------------------------------------------------------------------

def plot_monthly_boxplots(df: pd.DataFrame, stats: dict, images_dir: str):
    """
    Box-and-whisker: distribution of daily kWh output for each month.
    Shows spread and outlier days within each month.
    """
    year  = stats['year']
    daily = df.groupby(['date', 'month'])['P_dc_kW'].sum().reset_index()
    daily.columns = ['date', 'month', 'kwh']
    groups = [daily[daily['month'] == m]['kwh'].values for m in range(1, 13)]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    bp = ax.boxplot(
        groups,
        patch_artist=True,
        notch=False,
        medianprops=dict(color='#222222', lw=2.0),
        whiskerprops=dict(lw=1.1),
        flierprops=dict(
            marker='o', ms=6,
            markerfacecolor='#555555',
            markeredgecolor='#333333',
            markeredgewidth=0.6,
            alpha=0.65,
        ),
    )
    for patch, colour in zip(bp['boxes'], MONTH_COLORS):
        patch.set_facecolor(colour)
        patch.set_alpha(0.88)

    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(MONTH_ABBR)
    ax.set_title(f'Distribution of Daily Energy by Month — {year}')
    ax.set_ylabel('Daily energy (kWh)')
    fig.tight_layout(pad=2.2)

    _save(fig, images_dir, '02_monthly_boxplots.png')


# ---------------------------------------------------------------------------
# 03. Daily energy time series
# ---------------------------------------------------------------------------

def plot_daily_energy(df: pd.DataFrame, stats: dict, images_dir: str):
    """
    Daily kWh across the year with a 14-day rolling average and annual
    mean line.  Daily bars shaded grey; rolling average in a muted blue.
    """
    year  = stats['year']
    daily = stats['daily_kwh'].reset_index()
    daily.columns = ['date', 'kwh']
    daily['date']   = pd.to_datetime(daily['date'])
    daily['roll14'] = daily['kwh'].rolling(14, center=True).mean()

    fig, ax = plt.subplots(figsize=(13, 4.5))

    ax.fill_between(daily['date'], daily['kwh'], alpha=0.30, color='#aaaaaa')
    ax.plot(daily['date'], daily['kwh'],   lw=0.6, color='#aaaaaa', label='Daily kWh')
    ax.plot(daily['date'], daily['roll14'], lw=2.2, color='#1F5C99',
            label='14-day rolling avg')
    ax.axhline(stats['avg_daily_kwh'], lw=1.2, ls='--', color='#C0392B',
               label=f'Annual avg  {stats["avg_daily_kwh"]:.2f} kWh/day')

    ax.set_title(f'Daily Energy Output — {year}')
    ax.set_ylabel('Energy (kWh)')
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.legend(fontsize=8)
    ax.set_xlim(daily['date'].iloc[0], daily['date'].iloc[-1])
    ax.set_ylim(bottom=0)
    fig.tight_layout(pad=2.2)

    _save(fig, images_dir, '03_daily_energy.png')


# ---------------------------------------------------------------------------
# 04. Power heatmap — hour of day × day of year
# ---------------------------------------------------------------------------

def plot_power_heatmap(df: pd.DataFrame, stats: dict, images_dir: str):
    """
    Colour-map of DC power by hour of day (y-axis) and day of year (x-axis).
    Reveals seasonal and diurnal patterns simultaneously.
    """
    year  = stats['year']
    pivot = df.pivot_table(index='hour', columns='day_of_year',
                           values='P_dc_kW', aggfunc='mean')

    fig, ax = plt.subplots(figsize=(15, 6))
    im = ax.imshow(pivot.values, aspect='auto', origin='lower',
                   cmap=HEAT_CMAP, vmin=0,
                   extent=[1, 365, -0.5, 23.5])

    cbar = fig.colorbar(im, ax=ax, pad=0.015, fraction=0.025)
    cbar.set_label('DC Power (kW)', fontsize=9, labelpad=10)

    month_days = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 365]
    month_mids = [(month_days[i] + month_days[i+1]) / 2 for i in range(12)]
    for d in month_days[1:-1]:
        ax.axvline(d, color='#AAAAAA', lw=0.5, alpha=0.7)

    ax.set_xticks(month_mids)
    ax.set_xticklabels(MONTH_ABBR, fontsize=9)
    ax.set_yticks(range(0, 24, 2))
    ax.set_ylabel('Hour of day (local standard time)')
    ax.set_title(f'Power Output — Hour of Day vs. Day of Year — {year}')
    fig.tight_layout(pad=2.5)

    _save(fig, images_dir, '04_power_heatmap.png')


# ---------------------------------------------------------------------------
# 05. Hourly power profile — individual days + mean ± std
# ---------------------------------------------------------------------------

def plot_hourly_profile(df: pd.DataFrame, stats: dict, images_dir: str):
    """
    Left panel : every day overlaid as a semi-transparent line in a single
                 neutral colour — overlapping lines naturally darken to reveal
                 the most common output values.
    Right panel: annual hourly mean ± 1 std-dev band (grey) with per-month
                 mean overlay (month colours).
    """
    year = stats['year']
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5), sharey=True)

    # --- Left: all days, single colour ---
    ax = axes[0]
    for d in df['date'].unique():
        day_df = df[df['date'] == d]
        ax.plot(day_df['hour'], day_df['P_dc_kW'],
                color='#4A7FB5', alpha=0.07, lw=1.1)

    ax.set_title('Every Day — Hourly Profile')
    ax.set_xlabel('Hour of day (local standard time)')
    ax.set_ylabel('Power (kW)')
    ax.set_xlim(0, 23)

    # --- Right: mean ± std + monthly means ---
    ax = axes[1]
    hs   = df.groupby('hour')['P_dc_kW'].agg(['mean', 'std'])
    hrs  = hs.index.values
    mean = hs['mean'].values
    std  = hs['std'].values

    ax.fill_between(hrs, np.maximum(mean - std, 0), mean + std,
                    alpha=0.22, color='#aaaaaa', label='±1 std dev')
    ax.plot(hrs, mean, lw=2.4, color='#1F5C99', label='Annual hourly mean')

    for m in range(1, 13):
        m_mean = df[df['month'] == m].groupby('hour')['P_dc_kW'].mean()
        ax.plot(m_mean.index, m_mean.values,
                color=MONTH_COLORS[m-1], lw=1.2, alpha=0.90)

    patches = [mpatches.Patch(color=MONTH_COLORS[m-1], label=MONTH_ABBR[m-1])
               for m in range(1, 13)]
    ax.legend(handles=patches + [
        mpatches.Patch(color='#aaaaaa', alpha=0.5, label='±1 std dev'),
        plt.Line2D([0],[0], color='#1F5C99', lw=2, label='Annual mean'),
    ], ncol=4, fontsize=7, loc='upper left')

    ax.set_title('Mean ± Std Dev by Hour of Day')
    ax.set_xlabel('Hour of day (local standard time)')
    ax.set_xlim(0, 23)

    fig.suptitle(f'Hourly Power Generation Profile — {year}', y=1.02)
    fig.tight_layout(pad=2.2)
    _save(fig, images_dir, '05_hourly_profile.png')


# ---------------------------------------------------------------------------
# 06. Hours per day above threshold
# ---------------------------------------------------------------------------

def plot_hours_above_threshold(df: pd.DataFrame, stats: dict, images_dir: str):
    """
    Bar chart: hours per day where output ≥ threshold_kw, one bar per
    calendar day, coloured by month.
    """
    year   = stats['year']
    thresh = stats['threshold_kw']
    series = stats['hours_above_per_day'].reset_index()
    series.columns = ['date', 'hours']
    series['date']  = pd.to_datetime(series['date'])
    series['month'] = series['date'].dt.month
    colours = [MONTH_COLORS[m-1] for m in series['month']]

    fig, ax = plt.subplots(figsize=(15, 4.5))
    ax.bar(range(len(series)), series['hours'], color=colours,
           width=1.0, linewidth=0)

    month_starts = series.groupby('month')['date'].apply(
        lambda g: g.index[0]).values
    ax.set_xticks(month_starts)
    ax.set_xticklabels(MONTH_ABBR, fontsize=9)

    ax.axhline(stats['avg_hours_above_thresh'], lw=1.5, ls='--', color='#333333')

    patches = [mpatches.Patch(color=MONTH_COLORS[m-1], label=MONTH_ABBR[m-1])
               for m in range(1, 13)]
    avg_line = plt.Line2D([0],[0], ls='--', color='#333333',
                          label=f'Avg {stats["avg_hours_above_thresh"]:.1f} h/day')
    ax.legend(handles=patches + [avg_line], ncol=7, fontsize=7, loc='upper right')

    ax.set_title(f'Hours per Day with Output ≥ {thresh:.1f} kW — {year}')
    ax.set_ylabel('Hours')
    ax.set_xlim(-0.5, len(series) - 0.5)
    ax.set_ylim(0, 13)
    fig.tight_layout(pad=2.2)

    _save(fig, images_dir, '06_hours_above_threshold.png')


# ---------------------------------------------------------------------------
# 07. Irradiance vs. power
# ---------------------------------------------------------------------------

def plot_irradiance_vs_power(df: pd.DataFrame, stats: dict, images_dir: str):
    """
    Two-panel figure showing how irradiance drives power output.

    Left panel — In-plane irradiance (G_tilted) vs. DC power (scatter).
        Each point is one daytime hour, coloured by cell temperature.
        The relationship is almost perfectly linear; the slight spread at
        any given irradiance level reflects temperature variation.

    Right panel — Monthly average daily GHI vs. monthly energy output.
        GHI (global horizontal irradiance) is the raw solar resource arriving
        from the sky before any panel-geometry effects.  Plotting it against
        monthly kWh shows that the seasonal energy pattern is almost entirely
        explained by the seasonal change in available sunlight.
    """
    year   = stats['year']
    day_df = df[df['is_daytime']].copy()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))

    # --- Left: G_tilted vs P_dc_kW, colour = T_cell ---
    ax = axes[0]
    sc = ax.scatter(
        day_df['G_tilted_W_m2'], day_df['P_dc_kW'],
        c=day_df['T_cell_C'],
        cmap='RdYlBu_r', s=5, alpha=0.35, linewidths=0,
        vmin=day_df['T_cell_C'].quantile(0.02),
        vmax=day_df['T_cell_C'].quantile(0.98),
    )
    cbar = fig.colorbar(sc, ax=ax, pad=0.02)
    cbar.set_label('Cell temperature (°C)', fontsize=9)

    ax.set_xlabel('In-plane irradiance, G_tilted (W/m²)')
    ax.set_ylabel('DC power output (kW)')
    ax.set_title('In-Plane Irradiance vs. Power Output\n'
                 '(colour = cell temperature)')

    # --- Right: monthly avg GHI vs monthly energy ---
    ax = axes[1]

    # Average daily GHI per month (W/m²)
    monthly_ghi = df.groupby('month')['GHI_W_m2'].mean()
    monthly_kwh = stats['monthly_kwh']

    ax2 = ax.twinx()
    ax2.spines['top'].set_visible(False)

    bars = ax.bar(range(1, 13), monthly_ghi.values,
                  color=MONTH_COLORS, alpha=0.75,
                  edgecolor='white', linewidth=0.8,
                  label='Avg hourly GHI (W/m²)')
    line, = ax2.plot(range(1, 13), monthly_kwh.values,
                     color='#1F5C99', lw=2.2, marker='o',
                     markersize=6, label='Monthly energy (kWh)')

    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(MONTH_ABBR)
    ax.set_ylabel('Avg hourly GHI (W/m²)')
    ax2.set_ylabel('Monthly energy (kWh)')
    ax.set_title('Monthly Solar Resource (GHI)\nvs. Energy Output')

    # Combined legend
    handles = [mpatches.Patch(color='#7EB8D4', alpha=0.75,
                              label='Avg hourly GHI (W/m²)'), line]
    ax.legend(handles=handles, fontsize=8, loc='upper left')

    fig.suptitle(f'Irradiance and Power Generation — {year}', y=1.02)
    fig.tight_layout(pad=2.2)
    _save(fig, images_dir, '07_irradiance_vs_power.png')


# ---------------------------------------------------------------------------
# 08. Temperature derating — normalized power vs. cell temperature
# ---------------------------------------------------------------------------

def plot_temperature_effect(df: pd.DataFrame, stats: dict, images_dir: str):
    """
    Isolates the temperature derating effect by normalizing power output
    against irradiance, then plotting the result against cell temperature.

    ── Why normalize? ───────────────────────────────────────────────────────
    In the raw data, irradiance drives BOTH power (up) AND cell temperature
    (up) at the same time, so a direct P vs. T_cell scatter appears
    positively correlated.  That apparent correlation reflects the irradiance
    effect, not the temperature effect.

    ── The normalization ────────────────────────────────────────────────────
    Divide power by irradiance to cancel the dominant driver:

        R = P_dc_kW / (G_tilted_W_m2 / 1000)   [kW per kW/m²]

    From the power equation used in solar_analysis.py:

        P = N · P_STC · (G_t / G_STC) · [1 + γ · (T_cell − T_STC)] · PR

    dividing both sides by (G_t / G_STC):

        R = N · P_STC · [1 + γ · (T_cell − T_STC)] · PR

    This is now a LINEAR function of T_cell only — all irradiance dependence
    is gone.  The slope is N · P_STC · γ · PR, which for this system works
    out to ≈ −0.018 kW/°C (i.e. the array loses about 18 W for every 1 °C
    rise in cell temperature).

    ── Reading the plots ─────────────────────────────────────────────────────
    Left : Raw scatter of R vs. T_cell coloured by month.  Each dot is one
           daytime hour.  The negative slope should now be visible.

    Right: The same data binned into 20 equal temperature bands.  The median
           R in each bin is plotted with its spread (±1 std dev), making the
           linear derating trend clearer.  The dashed theoretical line is
           computed directly from the −0.35 %/°C temperature coefficient of
           the JA Solar JAM54S31 panel.
    """
    year   = stats['year']
    # Exclude very low irradiance (< 50 W/m²) to avoid noisy ratios near zero
    day_df = df[df['is_daytime'] & (df['G_tilted_W_m2'] > 50)].copy()

    day_df['power_ratio'] = day_df['P_dc_kW'] / (day_df['G_tilted_W_m2'] / 1000.0)

    # Theoretical line: R(T) = R_stc · [1 + γ · (T − 25)]
    gamma  = -0.0035   # temp. coefficient [1/°C] — JA Solar JAM54S31 spec
    r_stc  = day_df['power_ratio'].quantile(0.97)   # 97th-pct ≈ near-STC conditions
    t_line = np.linspace(day_df['T_cell_C'].min(), day_df['T_cell_C'].max(), 120)
    r_line = r_stc * (1 + gamma * (t_line - 25))

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))

    # --- Left: raw scatter, coloured by month ---
    ax = axes[0]
    for m in range(1, 13):
        sub = day_df[day_df['month'] == m]
        ax.scatter(sub['T_cell_C'], sub['power_ratio'],
                   color=MONTH_COLORS[m-1], s=5, alpha=0.45, linewidths=0)
    ax.plot(t_line, r_line, color='#222222', lw=1.8, ls='--',
            label=f'Theoretical  γ = {gamma*100:.2f} %/°C')

    patches = [mpatches.Patch(color=MONTH_COLORS[m-1], label=MONTH_ABBR[m-1])
               for m in range(1, 13)]
    ax.legend(handles=patches + [
        plt.Line2D([0],[0], ls='--', color='#222222',
                   label=f'Theoretical  γ = {gamma*100:.2f} %/°C')
    ], ncol=3, fontsize=7)
    ax.set_xlabel('Cell temperature (°C)')
    ax.set_ylabel('Normalized output  R  (kW per kW/m²)')
    ax.set_title('Normalized Output vs. Cell Temperature\n(coloured by month)')

    # --- Right: binned medians ---
    ax = axes[1]
    day_df['t_bin'] = pd.cut(day_df['T_cell_C'], bins=20)
    binned = day_df.groupby('t_bin', observed=True)['power_ratio'].agg(
        ['median', 'std'])
    bin_centres = np.array([iv.mid for iv in binned.index])

    ax.fill_between(bin_centres,
                    binned['median'] - binned['std'],
                    binned['median'] + binned['std'],
                    alpha=0.22, color='#888888', label='±1 std dev')
    ax.scatter(bin_centres, binned['median'],
               color='#1F5C99', s=35, zorder=3, label='Median per temperature bin')
    ax.plot(t_line, r_line, color='#222222', lw=1.8, ls='--',
            label=f'Theoretical  γ = {gamma*100:.2f} %/°C')

    ax.set_xlabel('Cell temperature (°C)')
    ax.set_ylabel('Normalized output  R  (kW per kW/m²)')
    ax.set_title('Median Normalized Output by Temperature Bin\n'
                 '(negative slope confirms temperature derating)')
    ax.legend(fontsize=8)

    fig.suptitle(f'Temperature Derating Effect — {year}', y=1.02)
    fig.tight_layout(pad=2.2)
    _save(fig, images_dir, '08_temperature_effect.png')


# =============================================================================
# 4. MAIN
# =============================================================================

def run_analysis(input_csv: str, images_dir: str, threshold_kw: float):
    """Full pipeline: load → statistics → all plots."""
    print(f'\nLoading data from: {input_csv}')
    df = load_data(input_csv)
    print(f'  {len(df):,} hourly rows  |  year: {df["year"].unique().tolist()}')

    stats = compute_statistics(df, threshold_kw)

    print('Generating plots …')
    plot_monthly_energy(stats, images_dir)
    plot_monthly_boxplots(df, stats, images_dir)
    plot_daily_energy(df, stats, images_dir)
    plot_power_heatmap(df, stats, images_dir)
    plot_hourly_profile(df, stats, images_dir)
    plot_hours_above_threshold(df, stats, images_dir)
    plot_irradiance_vs_power(df, stats, images_dir)
    plot_temperature_effect(df, stats, images_dir)

    n = len([f for f in os.listdir(images_dir) if f.endswith('.png')])
    print(f'\nAll done.  {n} images saved to: {os.path.abspath(images_dir)}\n')
    return stats


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Descriptive statistics and plots for solar power CSVs.')
    parser.add_argument(
        'input_csv', nargs='?', default=None,
        help='Path to a solar_analysis.py output CSV.')
    parser.add_argument(
        '--images-dir', default=None,
        help=f'Folder for output images (default: {IMAGES_DIR!r}).')
    parser.add_argument(
        '--threshold', type=float, default=None,
        help=f'Power threshold for hours-above plot in kW (default: {THRESHOLD_KW}).')

    args = parser.parse_args()

    csv_path   = args.input_csv  if args.input_csv  is not None else INPUT_CSV
    images_dir = args.images_dir if args.images_dir is not None else IMAGES_DIR
    threshold  = args.threshold  if args.threshold  is not None else THRESHOLD_KW

    run_analysis(csv_path, images_dir, threshold)