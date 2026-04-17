"""
Theoretical Solar Panel Power Prediction — minute-by-minute + NSRDB
===================================================================

Predicts theoretical DC power output for a fixed-tilt solar panel using
two independent irradiance sources, piped through the same
POA → NOCT → linear-with-temperature power model:

  (A) pvlib Ineichen clear-sky model   — minute-by-minute, idealized
      atmosphere.
  (B) NSRDB PSM4 satellite data        — 5-minute actual-atmosphere,
      read from pre-downloaded CSVs in ./NSRDB-raw/.  If multiple years
      are present, values are averaged across years at each 5-minute
      timestamp to produce a climatological estimate for the same
      calendar day.

Methodology matches solar_analysis.py:
  • Liu-Jordan **isotropic** sky transposition for POA.
  • **NOCT** cell temperature model:  T_cell = T_amb + (NOCT-20)/800 · G_t
  • Linear DC power with **Pmax temperature coefficient**:
        P = P_STC · (G_t / G_STC) · (1 + γ_Pmax · (T_cell − T_STC))
  • No wiring, soiling, mismatch, or inverter losses (no PR factor), per
    the user's request to keep only fundamental physics.

Panel        : JA Solar JAM54S31-405/MR (405 Wp, half-cell mono PERC)
Location     : 37.4271° N, -122.1837° W  (Palo Alto, CA)
Date         : 2026-04-16
Time window  : 16:26 – 16:45 local PDT (= 15:26 – 15:45 LST)
Orientation  : 57° tilt from horizontal, azimuth 180° (due south)

Run
---
    pip install pvlib pandas matplotlib
    python theoretical_solar_power.py

Outputs
-------
    theoretical_power.csv       — clear-sky per-minute table
    theoretical_power.png       — clear-sky plot
    nsrdb_by_year.csv           — NSRDB results, per-year per-sample
    nsrdb_avg.csv               — NSRDB results averaged across years
    power_comparison.png        — clear-sky vs NSRDB-per-year vs NSRDB-mean
"""

from __future__ import annotations

import os
import glob
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import pvlib
from pvlib.location import Location


# ─────────────────────────────────────────────────────────────────────
# 1. Site and panel configuration
# ─────────────────────────────────────────────────────────────────────
LATITUDE    = 37.4271           # degrees N
LONGITUDE   = -122.1837         # degrees E (negative = W)
ALTITUDE_M  = 42                # from freemaptools elevation finder
TIMEZONE    = "America/Los_Angeles"

PANEL_TILT    = 57.0            # degrees from horizontal
PANEL_AZIMUTH = 180.0           # 180° = due south (pvlib/met convention)

# JA Solar JAM54S31-405/MR — nameplate specs at STC
P_MAX_STC_W     = 405.0                  # Wp
MODULE_LENGTH_M = 1.722                  # m
MODULE_WIDTH_M  = 1.134                  # m
MODULE_AREA_M2  = MODULE_LENGTH_M * MODULE_WIDTH_M    # ≈ 1.953 m²
MODULE_EFF      = 0.207                  # 20.7% at STC

# Temperature model parameters (matches solar_analysis.py)
TEMP_COEFF_PMAX = -0.0035                # −0.35 %/°C (JA Solar datasheet)
T_STC           = 25.0                   # °C at STC
G_STC           = 1000.0                 # W/m² at STC
NOCT            = 45.0                   # °C — IEC 61215 default for open-rack
                                         # mono PERC; not stated in datasheet

ALBEDO          = 0.20                   # standard mixed-ground albedo

# Time window (minute-by-minute for clear-sky branch)
DATE_LOCAL   = "2026-04-16"
START_LOCAL  = "16:26"
END_LOCAL    = "16:45"
FREQ         = "1min"

# ── NSRDB configuration ──────────────────────────────────────────────
# Directory of pre-downloaded NSRDB PSM4 CSVs (SAM format).  One file per
# year is assumed; all files matching *.csv are loaded and averaged.
NSRDB_DIR = os.environ.get(
    "NSRDB_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "NSRDB-raw"),
)


# ─────────────────────────────────────────────────────────────────────
# 2. Reusable POA + temperature-corrected power pipeline
#    (isotropic sky model + NOCT cell temp + γ·ΔT linear power)
# ─────────────────────────────────────────────────────────────────────
def compute_poa_and_power(times, ghi, dni, dhi, temp_air,
                          solar_zenith=None, solar_azimuth=None,
                          apply_temp_correction=True):
    """
    Given irradiance components (GHI, DNI, DHI) and ambient air
    temperature aligned to `times`, compute:

        poa_global  — plane-of-array irradiance on the tilted panel
                      (Liu-Jordan isotropic sky model, Duffie & Beckman §2.15)
        t_cell      — cell temperature from NOCT model
        p_nameplate — P_STC · (POA / 1000)               (no temp correction)
        p_temp      — p_nameplate · (1 + γ · (T_cell − 25))

    If solar positions aren't supplied, they're computed from the site.
    """
    if solar_zenith is None or solar_azimuth is None:
        site_ = Location(LATITUDE, LONGITUDE, TIMEZONE, ALTITUDE_M)
        sp = site_.get_solarposition(times)
        solar_zenith  = sp["apparent_zenith"]
        solar_azimuth = sp["azimuth"]

    # Isotropic transposition (same formula as solar_analysis.py's
    # in_plane_irradiance).  pvlib's 'isotropic' model:
    #   POA_beam     = DNI · cos(AOI)
    #   POA_diffuse  = DHI · (1 + cos β) / 2
    #   POA_ground   = GHI · ρ · (1 − cos β) / 2
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt   =PANEL_TILT,
        surface_azimuth=PANEL_AZIMUTH,
        solar_zenith   =solar_zenith,
        solar_azimuth  =solar_azimuth,
        dni            =dni,
        ghi            =ghi,
        dhi            =dhi,
        albedo         =ALBEDO,
        model          ="isotropic",
    )
    poa_global = poa["poa_global"].fillna(0.0)

    # NOCT cell temperature:  T_cell = T_amb + (NOCT − 20) / 800 · G_t
    t_cell = temp_air + (NOCT - 20.0) / 800.0 * poa_global

    p_nameplate = P_MAX_STC_W * poa_global / G_STC                    # W, no temp
    temp_factor = 1.0 + TEMP_COEFF_PMAX * (t_cell - T_STC)
    p_temp      = p_nameplate * temp_factor                           # W, temp-corrected

    p_out = p_temp if apply_temp_correction else p_nameplate
    return poa_global, t_cell, p_nameplate, p_temp, p_out


# ─────────────────────────────────────────────────────────────────────
# 3. Clear-sky branch (minute-by-minute) — idealized atmosphere
# ─────────────────────────────────────────────────────────────────────
times = pd.date_range(
    start=f"{DATE_LOCAL} {START_LOCAL}",
    end  =f"{DATE_LOCAL} {END_LOCAL}",
    freq =FREQ,
    tz   =TIMEZONE,
)
site = Location(LATITUDE, LONGITUDE, TIMEZONE, ALTITUDE_M, name="Palo Alto")

solpos   = site.get_solarposition(times)
clearsky = site.get_clearsky(times, model="ineichen")

# Clear-sky model has no ambient T; assume 15 °C (typical April late afternoon
# in Palo Alto).  Users can override via CLEAR_SKY_T_AMB env var.
CS_T_AMB = float(os.environ.get("CLEAR_SKY_T_AMB", "15.0"))
t_air_cs = pd.Series(CS_T_AMB, index=times)

poa_cs, tcell_cs, p_cs_name, p_cs_temp, _ = compute_poa_and_power(
    times,
    ghi=clearsky["ghi"], dni=clearsky["dni"], dhi=clearsky["dhi"],
    temp_air=t_air_cs,
    solar_zenith=solpos["apparent_zenith"],
    solar_azimuth=solpos["azimuth"],
)

cs_results = pd.DataFrame({
    "time_local":       times.strftime("%H:%M"),
    "sun_elev_deg":     solpos["apparent_elevation"].round(2).values,
    "sun_az_deg":       solpos["azimuth"].round(2).values,
    "ghi_Wm2":          clearsky["ghi"].round(1).values,
    "dni_Wm2":          clearsky["dni"].round(1).values,
    "dhi_Wm2":          clearsky["dhi"].round(1).values,
    "poa_global_Wm2":   poa_cs.round(1).values,
    "T_amb_C":          t_air_cs.round(1).values,
    "T_cell_C":         tcell_cs.round(1).values,
    "P_nameplate_W":    p_cs_name.round(1).values,
    "P_temp_corr_W":    p_cs_temp.round(1).values,
})
print("\n=== Clear-sky (Ineichen) — minute-by-minute ===")
print(cs_results.to_string(index=False))
print(f"\nClear-sky theoretical energy (temp-corrected) "
      f"over {len(times)} min: "
      f"{p_cs_temp.sum()/60:.2f} Wh  "
      f"(mean P = {p_cs_temp.mean():.1f} W)")
cs_results.to_csv("theoretical_power.csv", index=False)

# Clear-sky plot
fig, ax1 = plt.subplots(figsize=(10, 5))
ax1.plot(times, p_cs_temp, "b-o", label="P (temp-corrected)")
ax1.plot(times, p_cs_name, "c:",  label="P (nameplate-only)", alpha=0.7)
ax1.set_xlabel("Local time")
ax1.set_ylabel("DC power (W)", color="b")
ax1.tick_params(axis="y", labelcolor="b")
ax1.legend(loc="upper left")
ax1.grid(True, alpha=0.3)
ax2 = ax1.twinx()
ax2.plot(times, poa_cs, "r--", label="POA irradiance")
ax2.set_ylabel("POA irradiance (W/m²)", color="r")
ax2.tick_params(axis="y", labelcolor="r")
plt.title(f"Clear-sky theoretical power — JAM54S31-405 — {DATE_LOCAL}\n"
          f"{PANEL_TILT:.0f}° tilt, due south, Ineichen clear-sky, NOCT temp model")
fig.autofmt_xdate()
plt.tight_layout()
plt.savefig("theoretical_power.png", dpi=150)
plt.close()


# ─────────────────────────────────────────────────────────────────────
# 4. NSRDB multi-year branch (5-min resolution, actual atmosphere)
# ─────────────────────────────────────────────────────────────────────
def load_nsrdb_window(csv_path: str) -> pd.DataFrame:
    """Read one NSRDB CSV and filter to April-16 test window (LST).

    The test window is 16:26–16:45 PDT = 15:26–15:45 LST (April is DST).
    NSRDB 5-min slots covering that window: 15:25, 15:30, 15:35, 15:40, 15:45.
    """
    data, meta = pvlib.iotools.read_nsrdb_psm4(csv_path, map_variables=True)
    # Index is tz-aware in Etc/GMT+8 (Pacific Standard Time)
    idx = data.index
    mask = (
        (idx.month == 4) & (idx.day == 16)
        & (idx.hour == 15) & (idx.minute >= 25) & (idx.minute <= 45)
    )
    window = data.loc[mask, [
        "ghi", "dni", "dhi", "temp_air", "wind_speed", "solar_zenith",
        "ghi_clear", "dni_clear", "dhi_clear",
    ]].copy()
    window["year"] = window.index.year
    # Convert index to local clock time (PDT for April)
    window.index = window.index.tz_convert(TIMEZONE)
    return window


def run_nsrdb_branch():
    if not os.path.isdir(NSRDB_DIR):
        print(f"\n=== NSRDB === skipped: directory not found: {NSRDB_DIR}")
        return None

    csv_paths = sorted(glob.glob(os.path.join(NSRDB_DIR, "*.csv")))
    if not csv_paths:
        print(f"\n=== NSRDB === skipped: no CSVs in {NSRDB_DIR}")
        return None

    print(f"\n=== NSRDB — {len(csv_paths)} file(s) from {NSRDB_DIR} ===")
    frames = []
    for path in csv_paths:
        try:
            frames.append(load_nsrdb_window(path))
            print(f"  loaded {os.path.basename(path)}")
        except Exception as exc:
            print(f"  WARNING: skipped {os.path.basename(path)} — {exc}")

    if not frames:
        return None

    combined = pd.concat(frames).sort_index()

    # Compute POA / T_cell / P for every row using pvlib-computed solar
    # position (so we can plug the 2026 panel orientation into the
    # historical atmosphere).
    times_combined = combined.index
    sp = Location(LATITUDE, LONGITUDE, TIMEZONE, ALTITUDE_M) \
            .get_solarposition(times_combined)

    poa, tcell, p_name, p_temp, _ = compute_poa_and_power(
        times_combined,
        ghi=combined["ghi"], dni=combined["dni"], dhi=combined["dhi"],
        temp_air=combined["temp_air"],
        solar_zenith=sp["apparent_zenith"],
        solar_azimuth=sp["azimuth"],
    )

    combined["sun_elev_deg"]    = sp["apparent_elevation"].round(2).values
    combined["sun_az_deg"]      = sp["azimuth"].round(2).values
    combined["poa_global_Wm2"]  = poa.round(1).values
    combined["T_cell_C"]        = tcell.round(2).values
    combined["P_nameplate_W"]   = p_name.round(1).values
    combined["P_temp_corr_W"]   = p_temp.round(1).values

    # ── Per-year, per-sample table ────────────────────────────────────
    combined["time_pdt"] = combined.index.strftime("%H:%M")
    by_year = combined.reset_index().rename(columns={"index": "timestamp_pdt"})[[
        "year", "time_pdt",
        "ghi", "dni", "dhi", "temp_air",
        "poa_global_Wm2", "T_cell_C", "P_nameplate_W", "P_temp_corr_W",
    ]].round(1)
    by_year.to_csv("nsrdb_by_year.csv", index=False)

    print("\n--- NSRDB per-year samples (April 16, 16:25–16:45 PDT) ---")
    print(by_year.to_string(index=False))

    # ── Averaged across years at each 5-min timestamp ────────────────
    avg = combined.groupby("time_pdt").agg({
        "ghi": "mean", "dni": "mean", "dhi": "mean",
        "temp_air": "mean",
        "poa_global_Wm2": "mean",
        "T_cell_C": "mean",
        "P_nameplate_W": "mean",
        "P_temp_corr_W": "mean",
        "year": "count",
    }).rename(columns={"year": "n_years"}).round(1).reset_index()
    avg.to_csv("nsrdb_avg.csv", index=False)

    print("\n--- NSRDB averaged across years ---")
    print(avg.to_string(index=False))

    # ── Summary stats ────────────────────────────────────────────────
    mean_p = float(avg["P_temp_corr_W"].mean())
    peak_p = float(avg["P_temp_corr_W"].max())
    # 5-min samples → energy = sum * (5/60) Wh
    energy = float(avg["P_temp_corr_W"].sum() * 5.0 / 60.0)
    print(f"\nNSRDB (multi-year average, temp-corrected): "
          f"mean P = {mean_p:.1f} W   peak {peak_p:.1f} W   "
          f"energy over window ≈ {energy:.2f} Wh")

    return combined, by_year, avg


nsrdb_result = run_nsrdb_branch()


# ─────────────────────────────────────────────────────────────────────
# 5. Combined plot: clear-sky vs NSRDB per-year + NSRDB mean
# ─────────────────────────────────────────────────────────────────────
if nsrdb_result is not None:
    combined, by_year, avg = nsrdb_result

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(times, p_cs_temp, "b-o", lw=2,
            label=f"Clear-sky Ineichen (pvlib, 1-min) — T_amb={CS_T_AMB:.0f}°C")

    # Per-year NSRDB traces
    years = sorted(combined["year"].unique())
    palette = plt.cm.viridis(np.linspace(0.1, 0.9, len(years)))
    for year, color in zip(years, palette):
        sub = combined[combined["year"] == year].sort_index()
        ax.plot(sub.index, sub["P_temp_corr_W"], "-s", color=color,
                alpha=0.7, label=f"NSRDB {year}")

    # NSRDB mean across years
    avg_times = pd.to_datetime(
        [f"{DATE_LOCAL} {t}" for t in avg["time_pdt"]]
    ).tz_localize(TIMEZONE)
    ax.plot(avg_times, avg["P_temp_corr_W"], "k-^", lw=2.5,
            label="NSRDB mean across years")

    ax.set_xlabel("Local time (PDT)")
    ax.set_ylabel("Theoretical DC power (W)")
    ax.set_title(f"Theoretical power — JAM54S31-405 — April 16, "
                 f"{PANEL_TILT:.0f}° tilt, due south\n"
                 f"NOCT temp model + γ·ΔT correction, no PR losses")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig("power_comparison.png", dpi=150)
    plt.close()

    print("\nSaved: theoretical_power.csv, theoretical_power.png, "
          "nsrdb_by_year.csv, nsrdb_avg.csv, power_comparison.png")
else:
    print("\nSaved: theoretical_power.csv, theoretical_power.png")


# ─────────────────────────────────────────────────────────────────────
# 6. Overall summary
# ─────────────────────────────────────────────────────────────────────
print("\n" + "=" * 62)
print("SUMMARY — theoretical DC power over 16:26–16:45 PDT, April 16")
print("=" * 62)
print(f"  Panel              : JA Solar JAM54S31-405/MR ({P_MAX_STC_W:.0f} Wp)")
print(f"  Tilt / Azimuth     : {PANEL_TILT:.0f}° / {PANEL_AZIMUTH:.0f}° (due south)")
print(f"  Temp model         : NOCT={NOCT}°C, γ_Pmax={TEMP_COEFF_PMAX*100:+.2f}%/°C")
print(f"  Losses             : none (no PR, no wiring, no soiling)")
print()
print(f"  Clear-sky Ineichen (per-min mean, T_amb={CS_T_AMB:.0f}°C):")
print(f"    P_nameplate_only  : {p_cs_name.mean():6.1f} W")
print(f"    P_temp_corrected  : {p_cs_temp.mean():6.1f} W  "
      f"(+{100*(p_cs_temp.mean()/p_cs_name.mean()-1):.1f}% from temp)")
if nsrdb_result is not None:
    _, _, avg = nsrdb_result
    print()
    print(f"  NSRDB {years[0]}–{years[-1]} multi-year average (5-min, measured atmos.):")
    print(f"    P_nameplate_only  : {avg['P_nameplate_W'].mean():6.1f} W")
    print(f"    P_temp_corrected  : {avg['P_temp_corr_W'].mean():6.1f} W")
    ratio = avg["P_temp_corr_W"].mean() / p_cs_temp.mean()
    print(f"    NSRDB / clear-sky : {ratio:.2f}x "
          f"({'higher' if ratio > 1 else 'lower'} than Ineichen)")
print("=" * 62)


# ─────────────────────────────────────────────────────────────────────
# 7. Helper to merge with your experimental data
# ─────────────────────────────────────────────────────────────────────
# If you have a spreadsheet with columns like:
#     time_local (HH:MM)  ,  P_measured_W
# you can join it like this:
#
#     measured = pd.read_excel("my_measurements.xlsx")
#     merged   = cs_results.merge(measured, on="time_local", how="left")
#     merged["residual_W"]   = merged["P_measured_W"] - merged["P_temp_corr_W"]
#     merged["pct_of_model"] = 100 * merged["P_measured_W"] / merged["P_temp_corr_W"]
#     merged.to_csv("comparison.csv", index=False)