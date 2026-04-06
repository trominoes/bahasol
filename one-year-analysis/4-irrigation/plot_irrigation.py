"""
plot_irrigation.py
==================
Generates a suite of publication-quality visualisations from the CSVs
produced by irrigation_schedule.py.  Call directly or import and call
``plot_season()`` from another script.

Outputs  (written to ``results/images/<crop>_<year>/``)
-------------------------------------------------------
  P1_water_balance.png      — daily ETc vs. rainfall + irrigation (stacked area)
  P2_kc_et_curves.png       — Kc / ET0 / ETc curves with growth-stage shading
  P3_weekly_schedule.png    — weekly irrigation need, capacity, and deficit bars
  P4_precip_heatmap.png     — daily precipitation calendar heatmap
  P5_monthly_balance.png    — monthly water-balance summary (stacked bars)
  P6_season_dashboard.png   — 4-panel season overview dashboard

Usage
-----
    python plot_irrigation.py                         # all seasons, cassava
    python plot_irrigation.py --crop tomato
    python plot_irrigation.py --years 2020 2021
"""

import argparse
import os
import csv
from glob import glob
from datetime import date, timedelta
import math

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as ticker
import matplotlib.dates as mdates
import matplotlib.colors as mcolors
import matplotlib.gridspec as gridspec
import numpy as np

# ===========================================================================
# STYLING
# ===========================================================================

# Colour palette — water / agriculture theme
C_ETC       = '#D4622A'   # ETc crop demand      — warm brick-orange
C_RAIN      = '#3A7FC1'   # effective rainfall   — clear blue
C_IRR       = '#47965A'   # net irrigation       — field green
C_DEFICIT   = '#B83232'   # irrigation deficit   — deep red
C_EXCESS    = '#A8CBE8'   # rain surplus         — light sky
C_STORM     = '#6A1F8A'   # extreme rain event   — purple
C_DRY       = '#EDB84A'   # dry spell backdrop   — dry-grass amber

STAGE_COLORS = {
    'Initial'    : '#C8E6C9',   # pale green
    'Development': '#66BB6A',   # medium green
    'Mid-season' : '#2E7D32',   # deep green
    'Late'       : '#8D6E63',   # earth brown
}
STAGE_ORDER = ['Initial', 'Development', 'Mid-season', 'Late']

MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun',
              'Jul','Aug','Sep','Oct','Nov','Dec']

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
    'grid.linewidth'   : 0.5,
    'axes.titlesize'   : 10,
    'axes.titleweight' : 'semibold',
    'axes.titlepad'    : 10,
    'axes.labelsize'   : 9,
    'axes.labelpad'    : 6,
    'legend.fontsize'  : 8,
    'legend.framealpha': 0.85,
    'xtick.labelsize'  : 8,
    'ytick.labelsize'  : 8,
    'lines.linewidth'  : 1.5,
})

_HERE          = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR   = os.path.join(_HERE, 'results')
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, 'results', 'images')

STORM_THRESHOLD_MM   = 50.0   # daily precip → flag as potential storm/hurricane
DRY_SPELL_DAYS       = 14     # consecutive dry days → annotate
DRY_PRECIP_MM        = 2.0    # threshold for "dry day"


# ===========================================================================
# DATA LOADING
# ===========================================================================

def _load_daily(path: str) -> list:
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append({
                'date'         : r['date'],
                'day_in_season': int(r['day_in_season']),
                'growth_stage' : r['growth_stage'],
                'Kc'           : float(r['Kc']),
                'ET0_mm'       : float(r['ET0_mm']),
                'ETc_mm'       : float(r['ETc_mm']),
                'precip_mm'    : float(r['precip_mm']),
                'eff_precip_mm': float(r['eff_precip_mm']),
                'net_irr_mm'   : float(r['net_irr_mm']),
            })
    return rows


def _load_weekly(path: str) -> list:
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append({
                'week_start'   : r['week_start'],
                'week_end'     : r['week_end'],
                'growth_stage' : r['growth_stage'],
                'Kc_mean'      : float(r['Kc_mean']),
                'ETc_mm'       : float(r['ETc_mm']),
                'precip_mm'    : float(r['precip_mm']),
                'eff_precip_mm': float(r['eff_precip_mm']),
                'net_irr_mm'   : float(r['net_irr_mm']),
                'irr_days_week': int(r['irr_days_week']),
                'hrs_per_day'  : float(r['hrs_per_day']),
                'capacity_mm'  : float(r['capacity_mm']),
                'deficit_mm'   : float(r['deficit_mm']),
            })
    return rows


def _dates(rows):
    """Return numpy array of datetime64 from 'date' field."""
    return np.array([np.datetime64(r['date']) for r in rows])


def _stage_spans(daily):
    """Return list of (start_date, end_date, stage_name) tuples."""
    spans = []
    cur_stage = daily[0]['growth_stage']
    start = daily[0]['date']
    for r in daily[1:]:
        if r['growth_stage'] != cur_stage:
            spans.append((start, r['date'], cur_stage))
            cur_stage = r['growth_stage']
            start = r['date']
    spans.append((start, daily[-1]['date'], cur_stage))
    return spans


def _shade_stages(ax, spans, alpha=0.08, ymin=0, ymax=1, transform=None):
    """Draw translucent growth-stage bands on ax (in data x-coordinates)."""
    for s_start, s_end, stage in spans:
        x0 = np.datetime64(s_start)
        x1 = np.datetime64(s_end)
        ax.axvspan(x0, x1,
                   color=STAGE_COLORS.get(stage, '#cccccc'),
                   alpha=alpha, linewidth=0, zorder=0)


# ===========================================================================
# PLOT 1 — Daily Water Balance
# ===========================================================================

def plot_water_balance(daily, title_suffix, out_path):
    """Stacked area: rain fills + irrigation need vs. ETc demand line."""
    dates  = _dates(daily)
    ETc    = np.array([r['ETc_mm']        for r in daily])
    rain   = np.array([r['eff_precip_mm'] for r in daily])
    irr    = np.array([r['net_irr_mm']    for r in daily])
    raw_p  = np.array([r['precip_mm']     for r in daily])

    fig, ax = plt.subplots(figsize=(14, 4.5))

    # ── stacked supply area ──
    ax.stackplot(dates,
                 np.minimum(rain, ETc),          # rain portion of supply
                 np.minimum(irr,  ETc - np.minimum(rain, ETc)),  # irrigation portion
                 colors=[C_RAIN, C_IRR], alpha=0.75,
                 labels=['Effective rainfall', 'Irrigation applied'])

    # ── ETc demand line ──
    ax.plot(dates, ETc, color=C_ETC, lw=1.8, label='ETc demand', zorder=4)

    # ── deficit shading (demand not met) ──
    supply = rain + irr
    deficit_mask = ETc > supply + 0.1
    ax.fill_between(dates, supply, ETc,
                    where=deficit_mask,
                    color=C_DEFICIT, alpha=0.4, label='Unmet demand', zorder=3)

    # ── storm / hurricane markers ──
    storm_dates = [d for d, p in zip(dates, raw_p) if p >= STORM_THRESHOLD_MM]
    for sd in storm_dates:
        ax.axvline(sd, color=C_STORM, lw=0.8, alpha=0.6, zorder=5)
    if storm_dates:
        ax.axvline(storm_dates[0], color=C_STORM, lw=0.8, alpha=0.6,
                   label=f'Rain ≥ {STORM_THRESHOLD_MM:.0f}mm (storm)')

    # ── growth stage shading ──
    _shade_stages(ax, _stage_spans(daily), alpha=0.07)

    # ── stage legend bar (thin strip at top) ──
    ax2 = ax.inset_axes([0, 1.0, 1, 0.04])
    ax2.set_xlim(0, len(daily))
    ax2.set_yticks([])
    ax2.set_xticks([])
    for sp in ['top', 'bottom', 'left', 'right']:
        ax2.spines[sp].set_visible(False)
    stage_start_idx = 0
    cur_stage = daily[0]['growth_stage']
    for i, r in enumerate(daily):
        if r['growth_stage'] != cur_stage or i == len(daily) - 1:
            end_idx = i if i < len(daily) - 1 else i + 1
            ax2.barh(0, end_idx - stage_start_idx, left=stage_start_idx,
                     color=STAGE_COLORS.get(cur_stage, '#ccc'), height=1,
                     linewidth=0)
            mid = (stage_start_idx + end_idx) / 2
            ax2.text(mid, 0, cur_stage[:3], ha='center', va='center',
                     fontsize=6.5, color='#333', fontweight='bold')
            cur_stage = r['growth_stage']
            stage_start_idx = i

    # ── formatting ──
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
    ax.set_ylabel('Water depth  [mm/day]')
    ax.set_xlim(dates[0], dates[-1])
    ax.set_ylim(bottom=0)
    ax.legend(loc='upper right', ncol=2, framealpha=0.9)
    ax.set_title(f'Daily Water Balance — {title_suffix}')

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f'  Saved: {os.path.basename(out_path)}')


# ===========================================================================
# PLOT 2 — Kc / ET0 / ETc Curves
# ===========================================================================

def plot_kc_et_curves(daily, title_suffix, out_path):
    """Kc piecewise curve + ET0 and ETc time series, growth stages shaded."""
    days   = np.array([r['day_in_season'] for r in daily])
    Kc     = np.array([r['Kc']            for r in daily])
    ET0    = np.array([r['ET0_mm']        for r in daily])
    ETc    = np.array([r['ETc_mm']        for r in daily])

    fig, ax1 = plt.subplots(figsize=(14, 4.5))
    ax2 = ax1.twinx()

    # ── growth stage backgrounds ──
    cur_stage = daily[0]['growth_stage']
    span_start = 0
    for i, r in enumerate(daily):
        if r['growth_stage'] != cur_stage or i == len(daily) - 1:
            end = i if i < len(daily) - 1 else i + 1
            ax1.axvspan(span_start, end,
                        color=STAGE_COLORS.get(cur_stage, '#eee'),
                        alpha=0.15, linewidth=0)
            mid = (span_start + end) / 2
            ax1.text(mid, ax1.get_ylim()[0] if ax1.get_ylim()[0] > 0 else 0.02,
                     cur_stage, ha='center', va='bottom',
                     fontsize=7, color='#555', style='italic', zorder=3)
            cur_stage = r['growth_stage']
            span_start = i

    # ── ET0 and ETc on left axis ──
    ax1.fill_between(days, 0, ET0, color='#B0BEC5', alpha=0.35, label='ET₀ range')
    ax1.plot(days, ET0, color='#90A4AE', lw=1.2, linestyle='--', label='ET₀  (reference)')
    ax1.plot(days, ETc, color=C_ETC, lw=2.0, label='ETc  (crop demand)')
    ax1.set_xlabel('Day in growing season')
    ax1.set_ylabel('Evapotranspiration  [mm/day]', color='#444')
    ax1.set_ylim(bottom=0)

    # ── Kc curve on right axis ──
    ax2.plot(days, Kc, color='#1B5E20', lw=2.2, linestyle='-.',
             label='Kc  (crop coeff.)', zorder=5)
    ax2.set_ylabel('Crop coefficient  Kc  [—]', color='#1B5E20')
    ax2.tick_params(axis='y', colors='#1B5E20')
    ax2.set_ylim(0, 1.6)

    # ── combined legend ──
    lines1, labs1 = ax1.get_legend_handles_labels()
    lines2, labs2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labs1 + labs2,
               loc='upper left', ncol=2, framealpha=0.9)

    ax1.set_xlim(1, days[-1])
    ax1.set_title(f'Kc Curve and Evapotranspiration — {title_suffix}')

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f'  Saved: {os.path.basename(out_path)}')


# ===========================================================================
# PLOT 3 — Weekly Schedule Chart
# ===========================================================================

def plot_weekly_schedule(weekly, title_suffix, out_path):
    """Grouped bars: ETc, effective rain, net irrigation, schedule capacity."""
    n = len(weekly)
    x = np.arange(n)
    bar_w = 0.55

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(max(10, n * 0.55), 7),
        gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.04},
        sharex=True)

    # ── top panel: water balance bars ──
    eff_rain = np.array([w['eff_precip_mm'] for w in weekly])
    net_irr  = np.array([w['net_irr_mm']    for w in weekly])
    ETc      = np.array([w['ETc_mm']        for w in weekly])
    cap      = np.array([w['capacity_mm']   for w in weekly])
    deficit  = np.array([w['deficit_mm']    for w in weekly])

    # Rain + irrigation stacked bar
    ax_top.bar(x, eff_rain, width=bar_w, color=C_RAIN, alpha=0.80,
               label='Effective rainfall', zorder=3)
    ax_top.bar(x, net_irr, width=bar_w, bottom=eff_rain, color=C_IRR,
               alpha=0.80, label='Irrigation scheduled', zorder=3)

    # Deficit overlay (red hatch)
    for i, (d, er, ni) in enumerate(zip(deficit, eff_rain, net_irr)):
        if d > 0.5:
            ax_top.bar(i, d, width=bar_w, bottom=er + ni,
                       color=C_DEFICIT, alpha=0.55, hatch='//', zorder=4,
                       label='_nolegend_')

    # ETc demand line
    ax_top.step(x - 0.5 * bar_w / n, ETc, where='post',
                color=C_ETC, lw=2.0, label='ETc demand', zorder=5)

    # Schedule capacity dots
    valid_cap = [(i, c) for i, (c, ni) in enumerate(zip(cap, net_irr)) if ni > 0.5]
    if valid_cap:
        xi, yi = zip(*valid_cap)
        ax_top.scatter(xi, yi, s=30, color='#2E7D32', marker='_',
                       linewidths=2, zorder=6, label='Schedule capacity')

    # Deficit asterisks
    for i, d in enumerate(deficit):
        if d > 0.5:
            ax_top.text(i, ETc[i] + 0.5, '✕', ha='center', va='bottom',
                        color=C_DEFICIT, fontsize=9, fontweight='bold', zorder=7)

    ax_top.set_ylabel('Water  [mm/week]')
    ax_top.set_ylim(bottom=0)
    ax_top.legend(ncol=3, loc='upper right', framealpha=0.9)
    ax_top.set_title(f'Weekly Irrigation Schedule — {title_suffix}')

    # ── bottom panel: pump hours per week ──
    hours = np.array([w['irr_days_week'] * w['hrs_per_day'] for w in weekly])
    stage_cols = np.array([STAGE_COLORS.get(w['growth_stage'], '#aaa')
                           for w in weekly])
    bars = ax_bot.bar(x, hours, width=bar_w, color=stage_cols, alpha=0.85,
                      edgecolor='white', linewidth=0.4)
    ax_bot.set_ylabel('Pump hrs/wk', fontsize=8)
    ax_bot.set_ylim(0, max(hours.max() * 1.25, 5))

    # ── x-axis: week labels (month only when it changes) ──
    labels = []
    for i, w in enumerate(weekly):
        m = w['week_start'][5:7]
        if i == 0 or weekly[i - 1]['week_start'][5:7] != m:
            labels.append(MONTH_ABBR[int(m) - 1])
        else:
            labels.append('')
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels(labels, fontsize=7.5)

    # ── stage legend for bottom panel ──
    stage_patches = [mpatches.Patch(color=v, label=k, alpha=0.85)
                     for k, v in STAGE_COLORS.items()
                     if any(w['growth_stage'] == k for w in weekly)]
    ax_bot.legend(handles=stage_patches, fontsize=7, ncol=4,
                  loc='upper right', framealpha=0.9)

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f'  Saved: {os.path.basename(out_path)}')


# ===========================================================================
# PLOT 4 — Precipitation Calendar Heatmap
# ===========================================================================

def plot_precip_heatmap(daily, title_suffix, out_path):
    """Month-by-month calendar heatmap of daily precipitation."""
    # Group by calendar month
    monthly: dict = {}
    for r in daily:
        ym = r['date'][:7]
        monthly.setdefault(ym, []).append(r)

    months = sorted(monthly.keys())
    n_months = len(months)
    cols = min(4, n_months)
    rows_fig = math.ceil(n_months / cols)

    fig, axes = plt.subplots(rows_fig, cols,
                             figsize=(cols * 3.8, rows_fig * 2.4))
    axes_flat = np.array(axes).flatten() if n_months > 1 else [axes]

    # Custom colormap: white → light blue → deep blue, purple for extreme
    cmap = mcolors.LinearSegmentedColormap.from_list(
        'precip',
        ['#FFFFFF', '#BDD7EE', '#3A7FC1', '#1A3A5C'],
        N=256)
    norm = mcolors.Normalize(vmin=0, vmax=60)

    for ax_idx, ym in enumerate(months):
        ax  = axes_flat[ax_idx]
        yr  = int(ym[:4])
        mon = int(ym[5:7])
        rows_data = monthly[ym]
        # Build 7-column (Mon–Sun) grid
        grid = np.full((6, 7), np.nan)
        labels = np.full((6, 7), '', dtype=object)
        for r in rows_data:
            d   = date.fromisoformat(r['date'])
            dow = (d.isoweekday() - 1) % 7   # 0=Mon
            wk  = (d.day - 1 + (date(yr, mon, 1).isoweekday() - 1)) // 7
            if wk < 6:
                grid[wk, dow] = r['precip_mm']
                labels[wk, dow] = str(d.day)

        im = ax.imshow(grid, cmap=cmap, norm=norm, aspect='auto',
                       interpolation='nearest')

        # Storm markers
        for r in rows_data:
            if r['precip_mm'] >= STORM_THRESHOLD_MM:
                d   = date.fromisoformat(r['date'])
                dow = (d.isoweekday() - 1) % 7
                wk  = (d.day - 1 + (date(yr, mon, 1).isoweekday() - 1)) // 7
                if wk < 6:
                    ax.add_patch(mpatches.Circle(
                        (dow, wk), 0.42, fill=False,
                        edgecolor=C_STORM, lw=1.5, zorder=5))

        # Cell text (day numbers + values)
        for wi in range(6):
            for di in range(7):
                if not np.isnan(grid[wi, di]):
                    val = grid[wi, di]
                    txt_col = 'white' if val > 25 else '#333'
                    ax.text(di, wi, f"{labels[wi,di]}\n{val:.0f}",
                            ha='center', va='center',
                            fontsize=5.5, color=txt_col, linespacing=1.3)

        ax.set_xlim(-0.5, 6.5)
        ax.set_ylim(5.5, -0.5)
        ax.set_xticks(range(7))
        ax.set_xticklabels(['M','T','W','T','F','S','S'], fontsize=7)
        ax.set_yticks([])
        ax.set_title(f"{MONTH_ABBR[mon-1]} {yr}", fontsize=9, pad=3)
        for sp in ['top','right','bottom','left']:
            ax.spines[sp].set_visible(False)

    # Hide unused axes
    for ax_idx in range(n_months, len(axes_flat)):
        axes_flat[ax_idx].set_visible(False)

    # Colour bar
    cb_ax = fig.add_axes([0.92, 0.15, 0.012, 0.70])
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cb = fig.colorbar(sm, cax=cb_ax)
    cb.set_label('Precipitation  [mm/day]', fontsize=8)
    cb.ax.tick_params(labelsize=7)

    # Dry spell annotation
    dry_start = None
    dry_len   = 0
    dry_spans = []
    for r in daily:
        if r['precip_mm'] < DRY_PRECIP_MM:
            if dry_start is None:
                dry_start = r['date']
            dry_len += 1
        else:
            if dry_len >= DRY_SPELL_DAYS:
                dry_spans.append((dry_start, r['date'], dry_len))
            dry_start, dry_len = None, 0
    if dry_len >= DRY_SPELL_DAYS:
        dry_spans.append((dry_start, daily[-1]['date'], dry_len))

    fig.suptitle(
        f'Daily Precipitation Calendar — {title_suffix}'
        + (f'\n▲ = storm ≥{STORM_THRESHOLD_MM:.0f}mm   '
           + '   '.join(f'Dry spell: {s}→{e} ({d}d)'
                        for s, e, d in dry_spans[:3])
           if dry_spans else ''),
        fontsize=9, y=1.01)

    fig.subplots_adjust(right=0.90, hspace=0.55, wspace=0.25)
    fig.savefig(out_path)
    plt.close(fig)
    print(f'  Saved: {os.path.basename(out_path)}')


# ===========================================================================
# PLOT 5 — Monthly Water Balance
# ===========================================================================

def plot_monthly_balance(daily, title_suffix, out_path):
    """Stacked bar chart: monthly ETc, effective rainfall, net irrigation."""
    # Aggregate by calendar month label (e.g. 'Sep 2018')
    monthly: dict = {}
    for r in daily:
        key = r['date'][:7]
        if key not in monthly:
            monthly[key] = {'ETc': 0, 'rain': 0, 'irr': 0, 'precip': 0}
        monthly[key]['ETc']   += r['ETc_mm']
        monthly[key]['rain']  += r['eff_precip_mm']
        monthly[key]['irr']   += r['net_irr_mm']
        monthly[key]['precip'] += r['precip_mm']

    months  = sorted(monthly.keys())
    n       = len(months)
    labels  = [f"{MONTH_ABBR[int(m[5:7])-1]}\n{m[:4]}" for m in months]
    ETc_v   = np.array([monthly[m]['ETc']    for m in months])
    rain_v  = np.array([monthly[m]['rain']   for m in months])
    irr_v   = np.array([monthly[m]['irr']    for m in months])
    precip_v= np.array([monthly[m]['precip'] for m in months])

    x = np.arange(n)
    w = 0.38

    fig, ax = plt.subplots(figsize=(max(8, n * 1.1), 5))

    # Left bar group: supply (rain + irrigation)
    b1 = ax.bar(x - w/2, rain_v, width=w, color=C_RAIN, alpha=0.82,
                label='Effective rainfall', zorder=3)
    b2 = ax.bar(x - w/2, irr_v, width=w, bottom=rain_v, color=C_IRR,
                alpha=0.82, label='Irrigation applied', zorder=3)

    # Right bar: ETc demand
    b3 = ax.bar(x + w/2, ETc_v, width=w, color=C_ETC, alpha=0.82,
                label='ETc demand', zorder=3)

    # Raw precipitation as thin line
    ax.plot(x, precip_v, 'o--', color='#1565C0', ms=4, lw=1.2,
            alpha=0.6, label='Total rainfall (raw)', zorder=5)

    # Deficit shading
    supply_v = rain_v + irr_v
    for i in range(n):
        if ETc_v[i] > supply_v[i] + 1:
            ax.annotate('⚠', (i + w/2, ETc_v[i] + 2),
                        ha='center', va='bottom',
                        color=C_DEFICIT, fontsize=10, zorder=6)

    # Value labels on top of ETc bars
    for i, v in enumerate(ETc_v):
        ax.text(i + w/2, v + 1.5, f'{v:.0f}', ha='center', va='bottom',
                fontsize=7, color='#555')

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel('Water depth  [mm/month]')
    ax.set_ylim(bottom=0)
    ax.legend(ncol=2, loc='upper left', framealpha=0.9)
    ax.set_title(f'Monthly Water Balance — {title_suffix}')

    # Totals annotation
    tot_ETc  = ETc_v.sum()
    tot_rain = rain_v.sum()
    tot_irr  = irr_v.sum()
    ax.text(0.98, 0.97,
            f'Season totals:  ETc = {tot_ETc:.0f}mm  |  '
            f'Eff. rain = {tot_rain:.0f}mm  |  '
            f'Irrigation = {tot_irr:.0f}mm',
            transform=ax.transAxes, ha='right', va='top',
            fontsize=8, color='#333',
            bbox=dict(boxstyle='round,pad=0.4', fc='white', alpha=0.85))

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f'  Saved: {os.path.basename(out_path)}')


# ===========================================================================
# PLOT 6 — Season Dashboard (4-panel)
# ===========================================================================

def plot_season_dashboard(daily, weekly, title_suffix, out_path):
    """4-panel dashboard: Kc strip, pie, weekly heat-map, and pump hrs."""
    fig = plt.figure(figsize=(16, 9))
    gs  = gridspec.GridSpec(2, 3, figure=fig,
                             hspace=0.45, wspace=0.38,
                             left=0.07, right=0.97,
                             top=0.90, bottom=0.09)

    ax_kc    = fig.add_subplot(gs[0, :2])   # top-left wide: Kc / ETc strip
    ax_pie   = fig.add_subplot(gs[0, 2])    # top-right: water-balance pie
    ax_heat  = fig.add_subplot(gs[1, :2])   # bottom-left wide: weekly heatmap
    ax_pump  = fig.add_subplot(gs[1, 2])    # bottom-right: pump hours bar

    # ── Panel A: Kc / ETc seasonal strip ──────────────────────────────────
    dates  = _dates(daily)
    Kc     = np.array([r['Kc']     for r in daily])
    ETc    = np.array([r['ETc_mm'] for r in daily])
    ET0    = np.array([r['ET0_mm'] for r in daily])
    rain   = np.array([r['eff_precip_mm'] for r in daily])
    irr    = np.array([r['net_irr_mm']    for r in daily])

    _shade_stages(ax_kc, _stage_spans(daily), alpha=0.12)
    ax_kc.fill_between(dates, 0, ET0, color='#B0BEC5', alpha=0.3)
    ax_kc.plot(dates, ET0, color='#90A4AE', lw=1.0, ls='--', label='ET₀')
    ax_kc.plot(dates, ETc, color=C_ETC, lw=1.8, label='ETc')

    ax_kc2 = ax_kc.twinx()
    ax_kc2.plot(dates, Kc, color='#1B5E20', lw=2.0, ls='-.', label='Kc')
    ax_kc2.set_ylim(0, 1.5)
    ax_kc2.set_ylabel('Kc', fontsize=8, color='#1B5E20')
    ax_kc2.tick_params(axis='y', colors='#1B5E20', labelsize=7)

    ax_kc.xaxis.set_major_locator(mdates.MonthLocator())
    ax_kc.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
    ax_kc.set_ylabel('mm/day', fontsize=8)
    ax_kc.tick_params(labelsize=7)
    ax_kc.set_title('A  —  Crop coefficient and evapotranspiration',
                     loc='left', fontsize=9)
    lines_a = [mpatches.Patch(color=C_ETC, label='ETc'),
               mpatches.Patch(color='#90A4AE', label='ET₀'),
               mpatches.Patch(color='#1B5E20', label='Kc')]
    ax_kc.legend(handles=lines_a, fontsize=7, loc='upper left', ncol=3)

    # ── Panel B: Water balance pie ─────────────────────────────────────────
    tot_ETc   = float(np.sum(ETc))
    tot_rain  = float(np.sum(rain))
    tot_irr   = float(np.sum(irr))
    covered   = min(tot_rain, tot_ETc)
    irr_c     = min(tot_irr, max(0, tot_ETc - covered))
    deficit_c = max(0, tot_ETc - covered - irr_c)
    excess_rain = max(0, tot_rain - tot_ETc)

    pie_vals   = [covered, irr_c]
    pie_labels = [f'Rain\n{covered:.0f}mm', f'Irrigation\n{irr_c:.0f}mm']
    pie_colors = [C_RAIN, C_IRR]
    if deficit_c > 0.5:
        pie_vals.append(deficit_c)
        pie_labels.append(f'Deficit\n{deficit_c:.0f}mm')
        pie_colors.append(C_DEFICIT)
    if excess_rain > 0.5:
        pie_vals.append(excess_rain)
        pie_labels.append(f'Rain surplus\n{excess_rain:.0f}mm')
        pie_colors.append(C_EXCESS)

    wedges, texts, autotexts = ax_pie.pie(
        pie_vals, labels=pie_labels, colors=pie_colors,
        autopct='%1.0f%%', startangle=90,
        wedgeprops={'linewidth': 0.5, 'edgecolor': 'white'},
        textprops={'fontsize': 7.5})
    for at in autotexts:
        at.set_fontsize(7)
    ax_pie.set_title(f'B  —  Season water balance\n(ETc = {tot_ETc:.0f} mm)',
                     loc='left', fontsize=9)

    # ── Panel C: Weekly net-irrigation heat strip ──────────────────────────
    n_weeks = len(weekly)
    net_arr = np.array([[w['net_irr_mm'] for w in weekly]])
    cmap_c  = mcolors.LinearSegmentedColormap.from_list(
        'irr', ['#EAF4EC', C_IRR, '#1B5E20'], N=128)
    norm_c  = mcolors.Normalize(vmin=0,
                                vmax=max(max(w['net_irr_mm'] for w in weekly), 5))
    im = ax_heat.imshow(net_arr, cmap=cmap_c, norm=norm_c,
                        aspect='auto', interpolation='nearest')

    # Deficit markers
    for i, w in enumerate(weekly):
        if w['deficit_mm'] > 0.5:
            ax_heat.text(i, 0, '✕', ha='center', va='center',
                         color='white', fontsize=8, fontweight='bold')

    # Month separators
    prev_m = None
    for i, w in enumerate(weekly):
        m = w['week_start'][5:7]
        if m != prev_m:
            if i > 0:
                ax_heat.axvline(i - 0.5, color='white', lw=1.5, alpha=0.6)
            ax_heat.text(i, -0.6, MONTH_ABBR[int(m)-1],
                         ha='left', va='top', fontsize=7, color='#555')
            prev_m = m

    ax_heat.set_yticks([])
    ax_heat.set_xticks([])
    ax_heat.set_title('C  —  Weekly net irrigation need  (✕ = deficit week)',
                      loc='left', fontsize=9)
    cb_c = fig.colorbar(im, ax=ax_heat, orientation='horizontal',
                        pad=0.28, fraction=0.04, aspect=40)
    cb_c.set_label('mm/week', fontsize=7.5)
    cb_c.ax.tick_params(labelsize=7)

    # ── Panel D: Monthly pump hours ────────────────────────────────────────
    monthly_hrs: dict = {}
    for w in weekly:
        mon = w['week_start'][:7]
        hrs = w['irr_days_week'] * w['hrs_per_day']
        monthly_hrs[mon] = monthly_hrs.get(mon, 0) + hrs

    mon_keys   = sorted(monthly_hrs.keys())
    mon_labels = [MONTH_ABBR[int(k[5:7])-1] for k in mon_keys]
    mon_hrs    = [monthly_hrs[k] for k in mon_keys]
    stage_of_m = {}
    for r in daily:
        stage_of_m[r['date'][:7]] = r['growth_stage']
    bar_colors = [STAGE_COLORS.get(stage_of_m.get(k, ''), '#aaa')
                  for k in mon_keys]

    ax_pump.bar(range(len(mon_keys)), mon_hrs, color=bar_colors,
                alpha=0.85, edgecolor='white', linewidth=0.4)
    for i, v in enumerate(mon_hrs):
        ax_pump.text(i, v + 0.3, f'{v:.0f}h', ha='center', va='bottom',
                     fontsize=7, color='#444')
    ax_pump.set_xticks(range(len(mon_keys)))
    ax_pump.set_xticklabels(mon_labels, fontsize=8)
    ax_pump.set_ylabel('Pump hrs / month', fontsize=8)
    ax_pump.set_ylim(0, max(mon_hrs) * 1.2 if mon_hrs else 10)
    ax_pump.set_title('D  —  Required pump-hours per month',
                      loc='left', fontsize=9)

    stage_patches = [mpatches.Patch(color=v, label=k, alpha=0.85)
                     for k, v in STAGE_COLORS.items()
                     if any(r['growth_stage'] == k for r in daily)]
    ax_pump.legend(handles=stage_patches, fontsize=7, ncol=2,
                   loc='upper right', framealpha=0.9)

    fig.suptitle(f'Irrigation Season Dashboard — {title_suffix}',
                 fontsize=12, fontweight='semibold', y=0.97)
    fig.savefig(out_path)
    plt.close(fig)
    print(f'  Saved: {os.path.basename(out_path)}')


# ===========================================================================
# MAIN ORCHESTRATOR
# ===========================================================================

def plot_season(daily: list, weekly: list, crop: str, plant_year: int,
                output_dir: str):
    """Generate all 6 plots for one growing season."""
    tag = f"{crop.replace('_','-')} {plant_year}/{plant_year+1}"
    os.makedirs(output_dir, exist_ok=True)

    def p(name):
        return os.path.join(output_dir, name)

    plot_water_balance  (daily, tag, p('P1_water_balance.png'))
    plot_kc_et_curves   (daily, tag, p('P2_kc_et_curves.png'))
    plot_weekly_schedule(weekly, tag, p('P3_weekly_schedule.png'))
    plot_precip_heatmap (daily, tag, p('P4_precip_heatmap.png'))
    plot_monthly_balance(daily, tag, p('P5_monthly_balance.png'))
    plot_season_dashboard(daily, weekly, tag, p('P6_season_dashboard.png'))
    print(f'  → All plots written to {output_dir}')


def main():
    parser = argparse.ArgumentParser(
        description='Generate irrigation visualisations from schedule CSVs.')
    parser.add_argument('--data-dir', default=DEFAULT_DATA_DIR,
                        help='Directory containing weekly/daily CSV files.')
    parser.add_argument('--output-dir', default=DEFAULT_OUTPUT_DIR,
                        help='Root directory for image outputs.')
    parser.add_argument('--crop', default='cassava',
                        help='Crop slug (default: cassava).')
    parser.add_argument('--years', type=int, nargs='+', default=None,
                        help='Planting years to plot (default: all found).')
    args = parser.parse_args()

    slug  = args.crop.replace('_', '-')
    files = sorted(glob(os.path.join(args.data_dir, f'weekly_{slug}_*.csv')))
    if not files:
        print(f'No weekly CSV files found for crop "{args.crop}" in {args.data_dir}')
        return

    for wf in files:
        yr = int(os.path.basename(wf).replace(f'weekly_{slug}_','').replace('.csv',''))
        if args.years and yr not in args.years:
            continue
        df = os.path.join(args.data_dir, f'daily_{slug}_{yr}.csv')
        if not os.path.exists(df):
            print(f'  Missing daily CSV for {yr} — skipping.')
            continue

        print(f'\nPlotting season {yr}/{yr+1} ({args.crop}) …')
        daily  = _load_daily(df)
        weekly = _load_weekly(wf)
        out    = os.path.join(args.output_dir, f'{slug}_{yr}')
        plot_season(daily, weekly, args.crop, yr, out)


if __name__ == '__main__':
    main()
