"""
solar_analysis.py
=================
Hour-by-hour DC power output analysis for a rooftop PV array.
Input:  NSRDB hourly irradiance CSV (satellite data).
Output: CSV with per-hour irradiance, cell temperature, and power.

Usage
-----
    python solar_analysis.py <input_csv> <output_csv>

Example
-------
    python solar_analysis.py 4469509_24.96_-78.05_2018.csv output.csv

References
----------
[1] Duffie, J.A. & Beckman, W.A. (2013). Solar Engineering of Thermal
    Processes, 4th ed. Wiley.
    — Solar geometry equations (declination, hour angle, zenith, azimuth,
      angle of incidence, transposition model).

[2] Liu, B.Y.H. & Jordan, R.C. (1963). "The long-term average performance
    of flat-plate solar energy collectors." Solar Energy 7(2), 53–74.
    — Isotropic sky transposition model for tilted surfaces.

[3] King, D.L., Boyson, W.E., & Kratochvil, J.A. (2004). Photovoltaic
    Array Performance Model. Sandia National Laboratories, SAND2004-3535.
    — NOCT cell temperature model; linear power correction.

[4] Spencer, J.W. (1971). "Fourier series representation of the position
    of the sun." Search 2(5), 172.
    — Equation of time approximation.

[5] JA Solar product datasheet / RES Supply listing.
    JAM54S31-405/MR — Module efficiency, rated power, temperature
    coefficient of Pmax.
    URL: https://ressupply.com/solar-panels/ja-solar-jam54s31-405mr-solar-panel
"""

import csv
import math
import argparse
from datetime import datetime

# =============================================================================
# INSTALLATION PARAMETERS  (edit here to reconfigure the system)
# =============================================================================

# --- Panel: JA Solar JAM54S31-405/MR  [Ref. 5] ---
PANEL_RATED_POWER_W  = 405.0    # Rated power at STC [W]
PANEL_EFFICIENCY     = 0.207    # Module efficiency at STC (20.7%) [—]
PANEL_LENGTH_M       = 1.722    # Panel length [m]  (67.8 in)
PANEL_WIDTH_M        = 1.134    # Panel width  [m]  (44.6 in)
PANEL_AREA_M2        = PANEL_LENGTH_M * PANEL_WIDTH_M   # ≈ 1.953 m²

TEMP_COEFF_PMAX      = -0.0035  # Temperature coefficient of Pmax [1/°C]
                                 # Spec: −0.35 %/°C  [Ref. 5]
T_STC                = 25.0     # Cell temperature at STC [°C]
G_STC                = 1000.0   # Irradiance at STC [W/m²]

NOCT                 = 45.0     # Nominal Operating Cell Temperature [°C]
                                 # Not stated in this datasheet; 45 °C is the
                                 # IEC 61215 standard default for open-rack
                                 # monocrystalline PERC modules.  [Ref. 3]

# --- Array ---
N_PANELS             = 15       # Number of panels in the array
ARRAY_RATED_POWER_W  = N_PANELS * PANEL_RATED_POWER_W   # 6 075 W

# --- Mounting geometry ---
TILT_DEG             = 24.0     # Panel tilt from horizontal [°]
AZIMUTH_DEG          = 180.0    # Panel azimuth, meteorological convention
                                 # (0 = N, 90 = E, 180 = S, 270 = W)
                                 # 180° = due south

# --- Site  (read from NSRDB header, repeated here for transparency) ---
LATITUDE_DEG         = 24.96    # [°N]
LONGITUDE_DEG        = -78.05   # [°E, negative = West]

# --- Ground reflectance ---
ALBEDO               = 0.20     # Ground surface albedo [—]
                                 # NSRDB file does not include an albedo column.
                                 # 0.20 is the standard value for mixed
                                 # ground cover (Duffie & Beckman [1] §2.15).

# --- System losses ---
PERFORMANCE_RATIO    = 0.85     # Accounts for wiring resistance, module
                                 # mismatch, soiling, and DC-side losses [—].
                                 # Typical range: 0.80–0.90  (IEA PVPS Task 2).

# =============================================================================
# SOLAR GEOMETRY  [Ref. 1, Ch. 1]
# =============================================================================

def day_of_year(year: int, month: int, day: int) -> int:
    """Return the day of year n (1 – 365/366)."""
    return datetime(year, month, day).timetuple().tm_yday


def solar_declination_deg(n: int) -> float:
    """
    Solar declination angle δ [degrees].

    Cooper (1969) approximation, cited in Duffie & Beckman [1] eq. 1.6.2:

        δ = 23.45 · sin( 360/365 · (284 + n) )

    Parameters
    ----------
    n : Day of year (1 – 365).
    """
    return 23.45 * math.sin(math.radians(360.0 / 365.0 * (284 + n)))


def equation_of_time_min(n: int) -> float:
    """
    Equation of time E [minutes].

    Spencer (1971) Fourier series [4], cited in Duffie & Beckman [1] eq. 1.5.3:

        B = 360/365 · (n − 1)   [degrees]
        E = 229.18 · (0.000075
                      + 0.001868·cos B  − 0.032077·sin B
                      − 0.014615·cos 2B − 0.04089·sin 2B)

    Parameters
    ----------
    n : Day of year (1 – 365).
    """
    B = math.radians(360.0 / 365.0 * (n - 1))
    return 229.18 * (
        0.000075
        + 0.001868 * math.cos(B)    - 0.032077 * math.sin(B)
        - 0.014615 * math.cos(2*B)  - 0.04089  * math.sin(2*B)
    )


def local_solar_time_h(local_std_hour: int, minute: int,
                        tz_offset: int, longitude_deg: float, n: int) -> float:
    """
    Local Solar Time (LST) in decimal hours.

    Step 1 — Convert local standard time to UTC:
        UTC = LST_clock − tz_offset          [hours]

    Step 2 — Apply longitude correction and equation of time [Ref. 1 §1.5]:
        LST_solar = UTC + longitude/15 + EoT/60

    Parameters
    ----------
    local_std_hour : Clock hour in the local standard time zone (0 – 23).
    minute         : Minute (0 – 59).
    tz_offset      : Time zone offset from UTC (e.g. −5 for UTC−5).
    longitude_deg  : Site longitude [°E].  Negative = West.
    n              : Day of year.
    """
    utc_decimal = (local_std_hour + minute / 60.0) - tz_offset
    eot = equation_of_time_min(n)
    return utc_decimal + longitude_deg / 15.0 + eot / 60.0


def hour_angle_deg(solar_time_h: float) -> float:
    """
    Hour angle ω [degrees].

    ω = 15 · (LST_solar − 12)               [Ref. 1 §1.5]

    Negative before solar noon, positive after.
    """
    return 15.0 * (solar_time_h - 12.0)


def solar_zenith_deg(lat_deg: float, decl_deg: float, omega_deg: float) -> float:
    """
    Solar zenith angle θ_z [degrees].

    cos θ_z = sin φ · sin δ + cos φ · cos δ · cos ω   [Ref. 1 eq. 1.6.5]

    Parameters
    ----------
    lat_deg  : Site latitude φ [°N].
    decl_deg : Solar declination δ [°].
    omega_deg: Hour angle ω [°].
    """
    phi   = math.radians(lat_deg)
    delta = math.radians(decl_deg)
    omega = math.radians(omega_deg)
    cos_tz = (math.sin(phi) * math.sin(delta)
              + math.cos(phi) * math.cos(delta) * math.cos(omega))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_tz))))


def solar_azimuth_met_deg(lat_deg: float, decl_deg: float,
                           omega_deg: float, zenith_deg: float) -> float:
    """
    Solar azimuth angle γ_s [degrees], meteorological convention (0 = N, 90 = E).

    Duffie & Beckman [1] eq. 1.6.6 gives the azimuth in the south-zero
    convention (positive West):

        cos γ_DB = (sin δ · cos φ − cos δ · cos ω · sin φ) / sin θ_z

    The sign of ω determines the hemisphere (morning/afternoon).
    Conversion to meteorological convention: γ_met = γ_DB + 180°.

    Returns 0.0 when the sun is at or below the horizon (θ_z ≥ 90°).
    """
    if zenith_deg >= 90.0:
        return 0.0

    phi   = math.radians(lat_deg)
    delta = math.radians(decl_deg)
    omega = math.radians(omega_deg)

    cos_g = ((math.sin(delta) * math.cos(phi)
              - math.cos(delta) * math.cos(omega) * math.sin(phi))
             / math.sin(math.radians(zenith_deg)))
    cos_g = max(-1.0, min(1.0, cos_g))

    gamma_db = math.degrees(math.acos(cos_g))
    if omega_deg < 0:           # Morning: east of south → negative in D&B
        gamma_db = -gamma_db

    return (gamma_db + 180.0) % 360.0


def angle_of_incidence_deg(zenith_deg: float, solar_az_met_deg: float,
                            tilt_deg: float, panel_az_met_deg: float) -> float:
    """
    Angle of incidence (AOI) of the direct beam on a tilted surface [degrees].

    Duffie & Beckman [1] eq. 1.6.3 (rewritten in meteorological azimuths):

        cos θ = cos θ_z · cos β
              + sin θ_z · sin β · cos(γ_s − γ_panel)

    where β = panel tilt and both azimuths follow the same convention.

    Parameters
    ----------
    zenith_deg       : Solar zenith angle θ_z [°].
    solar_az_met_deg : Solar azimuth γ_s, met. convention [°].
    tilt_deg         : Panel tilt β from horizontal [°].
    panel_az_met_deg : Panel azimuth γ_panel, met. convention [°].
    """
    tz  = math.radians(zenith_deg)
    b   = math.radians(tilt_deg)
    daz = math.radians(solar_az_met_deg - panel_az_met_deg)

    cos_aoi = (math.cos(tz) * math.cos(b)
               + math.sin(tz) * math.sin(b) * math.cos(daz))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_aoi))))


# =============================================================================
# IRRADIANCE TRANSPOSITION — Isotropic Sky Model  [Ref. 1, 2]
# =============================================================================

def in_plane_irradiance(dni: float, dhi: float, ghi: float,
                         aoi_deg: float, tilt_deg: float,
                         albedo: float = ALBEDO) -> float:
    """
    Total in-plane irradiance G_t on the tilted panel surface [W/m²].

    Liu & Jordan (1963) isotropic sky model [2], cited in Duffie &
    Beckman [1] eq. 2.15.1:

        G_t = G_b,T  +  G_d,T  +  G_r,T

    Beam component — direct beam projected onto the tilted plane:
        G_b,T = DNI · max(cos AOI, 0)

    Diffuse component — isotropic (uniform) sky dome:
        G_d,T = DHI · (1 + cos β) / 2

        The view factor (1 + cos β)/2 is the fraction of the sky hemisphere
        seen by the tilted surface  [Ref. 1 eq. 2.15.2].

    Ground-reflected component:
        G_r,T = GHI · ρ · (1 − cos β) / 2

        The view factor (1 − cos β)/2 is the fraction of the ground seen by
        the surface; ρ is ground albedo  [Ref. 1 eq. 2.15.3].

    Parameters
    ----------
    dni     : Direct Normal Irradiance [W/m²].
    dhi     : Diffuse Horizontal Irradiance [W/m²].
    ghi     : Global Horizontal Irradiance [W/m²].
    aoi_deg : Angle of incidence on the tilted surface [°].
    tilt_deg: Panel tilt β from horizontal [°].
    albedo  : Ground surface reflectance ρ [—].
    """
    b = math.radians(tilt_deg)

    beam      = dni * max(math.cos(math.radians(aoi_deg)), 0.0)
    diffuse   = dhi  * (1.0 + math.cos(b)) / 2.0
    reflected = ghi  * albedo * (1.0 - math.cos(b)) / 2.0

    return max(beam + diffuse + reflected, 0.0)


# =============================================================================
# CELL TEMPERATURE — NOCT Model  [Ref. 3]
# =============================================================================

def cell_temperature_c(t_amb: float, g_tilted: float,
                        noct: float = NOCT) -> float:
    """
    Photovoltaic cell temperature T_cell [°C].

    NOCT model from King et al. (2004) [3], also cited in Duffie &
    Beckman [1] §23.3:

        T_cell = T_amb + (NOCT − 20) / 800 · G_t

    NOCT is defined at: G = 800 W/m², T_amb = 20 °C, wind = 1 m/s,
    open-rack mounting, no electrical load.

    Parameters
    ----------
    t_amb    : Ambient air temperature [°C].
    g_tilted : In-plane irradiance G_t [W/m²].
    noct     : Nominal Operating Cell Temperature [°C].
    """
    return t_amb + (noct - 20.0) / 800.0 * g_tilted


# =============================================================================
# POWER OUTPUT
# =============================================================================

def array_power_w(g_tilted: float, t_cell: float) -> float:
    """
    Array DC power output P [W].

    Linear irradiance-and-temperature model [Ref. 1 §23.2, Ref. 3]:

        P = N · P_STC · (G_t / G_STC)
              · [1 + γ_Pmax · (T_cell − T_STC)]
              · PR

    Terms
    -----
    N · P_STC              : Array nameplate capacity at STC.
    G_t / G_STC            : Irradiance ratio — scales power linearly with
                             available sunlight.
    1 + γ_Pmax·(ΔT)       : Temperature derating — crystalline silicon loses
                             roughly 0.35 % of output per °C above 25 °C.
    PR                     : Performance ratio — lumps wiring, mismatch,
                             soiling, and other DC-side losses.

    Parameters
    ----------
    g_tilted : In-plane irradiance [W/m²].
    t_cell   : Cell temperature [°C].
    """
    temp_factor = 1.0 + TEMP_COEFF_PMAX * (t_cell - T_STC)
    power = (N_PANELS
             * PANEL_RATED_POWER_W
             * (g_tilted / G_STC)
             * temp_factor
             * PERFORMANCE_RATIO)
    return max(power, 0.0)


# =============================================================================
# MAIN ANALYSIS
# =============================================================================

def analyze_solar_power(input_csv_path: str, output_csv_path: str) -> list:
    """
    Read an NSRDB hourly CSV, compute in-plane irradiance and array power
    for every hour, and write a results CSV.

    NSRDB file structure assumed
    ----------------------------
    Row 1 : Site metadata (source, lat, lon, time zone, …)
    Row 2 : Units row
    Row 3 : Column header names
    Rows 4+: Hourly data

    Output CSV columns
    ------------------
    datetime_local   ISO 8601 timestamp in the file's local standard time.
    DNI_W_m2         Direct Normal Irradiance [W/m²].
    DHI_W_m2         Diffuse Horizontal Irradiance [W/m²].
    GHI_W_m2         Global Horizontal Irradiance [W/m²].
    solar_zenith_nsrdb_deg  Zenith angle as reported by NSRDB [°].
    solar_zenith_calc_deg   Zenith angle re-computed here (validation) [°].
    solar_azimuth_deg       Computed solar azimuth, met. convention [°].
    AOI_deg          Angle of incidence on the tilted panel [°].
    G_tilted_W_m2    In-plane (transposed) irradiance [W/m²].
    T_amb_C          Ambient air temperature [°C].
    T_cell_C         Estimated cell temperature (NOCT model) [°C].
    P_dc_W           Array DC power output [W].
    P_dc_kW          Array DC power output [kW].
    cloud_type       NSRDB cloud classification (0 = clear, …).
    fill_flag        NSRDB data-quality flag (0 = good).
    """
    # ------------------------------------------------------------------
    # 1. Parse the NSRDB header to extract time zone
    # ------------------------------------------------------------------
    with open(input_csv_path, newline='') as f:
        reader = csv.reader(f)
        meta_keys   = next(reader)   # Row 1: metadata field names
        meta_values = next(reader)   # Row 2: metadata values
        col_headers = next(reader)   # Row 3: data column names
        data_rows   = list(reader)   # Rows 4+

    # NSRDB row-1 / row-2 layout:
    #   Source, Location ID, City, State, Country,
    #   Latitude, Longitude, Time Zone, Elevation, Local Time Zone, …
    meta = {k.strip(): v.strip() for k, v in zip(meta_keys, meta_values)}
    tz_offset = int(float(meta['Time Zone']))   # e.g. −5 for UTC−5

    col = {name.strip(): idx for idx, name in enumerate(col_headers)}

    # ------------------------------------------------------------------
    # 2. Process each hourly row
    # ------------------------------------------------------------------
    results = []

    for row in data_rows:
        if not any(row):
            continue

        year   = int(row[col['Year']])
        month  = int(row[col['Month']])
        day    = int(row[col['Day']])
        hour   = int(row[col['Hour']])
        minute = int(row[col['Minute']])

        dni   = float(row[col['DNI']])
        dhi   = float(row[col['DHI']])
        ghi   = float(row[col['GHI']])
        t_amb = float(row[col['Temperature']])
        ws    = float(row[col['Wind Speed']])
        zenith_nsrdb = float(row[col['Solar Zenith Angle']])
        cloud_type   = row[col['Cloud Type']].strip()
        fill_flag    = row[col['Fill Flag']].strip()

        # --- Solar geometry ---
        n       = day_of_year(year, month, day)
        decl    = solar_declination_deg(n)
        lst     = local_solar_time_h(hour, minute, tz_offset, LONGITUDE_DEG, n)
        omega   = hour_angle_deg(lst)

        zenith_calc = solar_zenith_deg(LATITUDE_DEG, decl, omega)
        azimuth     = solar_azimuth_met_deg(LATITUDE_DEG, decl, omega, zenith_calc)

        # Use NSRDB zenith for AOI (authoritative source for this dataset)
        aoi = angle_of_incidence_deg(zenith_nsrdb, azimuth, TILT_DEG, AZIMUTH_DEG)

        # --- Transposition ---
        g_tilted = in_plane_irradiance(dni, dhi, ghi, aoi, TILT_DEG, ALBEDO)

        # --- Cell temperature ---
        t_cell = cell_temperature_c(t_amb, g_tilted)

        # --- Power ---
        p_dc_w  = array_power_w(g_tilted, t_cell)
        p_dc_kw = p_dc_w / 1000.0

        results.append({
            'datetime_local'        : datetime(year, month, day, hour, minute).isoformat(),
            'DNI_W_m2'              : round(dni, 2),
            'DHI_W_m2'              : round(dhi, 2),
            'GHI_W_m2'              : round(ghi, 2),
            'solar_zenith_nsrdb_deg': round(zenith_nsrdb, 3),
            'solar_zenith_calc_deg' : round(zenith_calc, 3),
            'solar_azimuth_deg'     : round(azimuth, 3),
            'AOI_deg'               : round(aoi, 3),
            'G_tilted_W_m2'         : round(g_tilted, 2),
            'T_amb_C'               : round(t_amb, 2),
            'T_cell_C'              : round(t_cell, 2),
            'P_dc_W'                : round(p_dc_w, 2),
            'P_dc_kW'               : round(p_dc_kw, 4),
            'cloud_type'            : cloud_type,
            'fill_flag'             : fill_flag,
        })

    # ------------------------------------------------------------------
    # 3. Write output CSV
    # ------------------------------------------------------------------
    fieldnames = list(results[0].keys())
    with open(output_csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # ------------------------------------------------------------------
    # 4. Print summary
    # ------------------------------------------------------------------
    total_kwh   = sum(r['P_dc_kW'] for r in results)   # 1 row = 1 hour
    peak_kw     = max(r['P_dc_kW'] for r in results)
    daytime_hrs = sum(1 for r in results if r['G_tilted_W_m2'] > 0)

    print("=" * 52)
    print("  Solar Power Analysis — Summary")
    print("=" * 52)
    print(f"  Panel model        : JA Solar JAM54S31-405/MR")
    print(f"  Array size         : {N_PANELS} × {PANEL_RATED_POWER_W:.0f} W"
          f"  =  {ARRAY_RATED_POWER_W/1000:.3f} kWp")
    print(f"  Tilt / Azimuth     : {TILT_DEG}° / {AZIMUTH_DEG}° (due south)")
    print(f"  Hours processed    : {len(results)}")
    print(f"  Daytime hours      : {daytime_hrs}")
    print(f"  Annual DC energy   : {total_kwh:,.1f} kWh")
    print(f"  Peak DC power      : {peak_kw:.3f} kW")
    print(f"  Specific yield     : {total_kwh / (ARRAY_RATED_POWER_W/1000):,.1f} kWh/kWp")
    print(f"  Output written to  : {output_csv_path}")
    print("=" * 52)

    return results


# =============================================================================
# BATCH PROCESSING
# =============================================================================

import fnmatch
import os

def analyze_directory(input_dir: str = 'NSRDB-raw',
                      output_dir: str = 'gen-power',
                      pattern: str = '*.csv') -> dict:
    """
    Run ``analyze_solar_power`` on every NSRDB CSV in *input_dir* whose
    filename matches *pattern*, writing one result CSV per input file into
    *output_dir*.

    The output filename mirrors the input filename, e.g.:
        NSRDB-raw/4469509_24.96_-78.05_2018.csv
            →  gen-power/4469509_24.96_-78.05_2018_power.csv

    Parameters
    ----------
    input_dir  : Directory containing raw NSRDB CSV files.
                 Defaults to ``'NSRDB-raw'`` (relative to the current working
                 directory, or supply an absolute path).
    output_dir : Directory where power-output CSVs will be written.
                 Created automatically if it does not exist.
                 Defaults to ``'gen-power'``.
    pattern    : Glob pattern used to select files inside *input_dir*.
                 Defaults to ``'*.csv'``, which matches every CSV.
                 Pass e.g. ``'4469509_24.96_-78.05_*.csv'`` to restrict to
                 a specific location/station prefix.

    Returns
    -------
    dict
        Mapping of ``{ input_path: results_list }`` for every file processed
        successfully.  Files that raised an exception are omitted and a
        warning is printed instead.

    Example — import and call from another script
    ----------------------------------------------
    ::

        from solar_analysis import analyze_directory

        summaries = analyze_directory(
            input_dir='NSRDB-raw',
            output_dir='gen-power',
            pattern='4469509_24.96_-78.05_*.csv',
        )
        print(f"Processed {len(summaries)} files.")
    """
    input_dir  = os.path.abspath(input_dir)
    output_dir = os.path.abspath(output_dir)

    if not os.path.isdir(input_dir):
        raise FileNotFoundError(
            f"Input directory not found: {input_dir!r}")

    os.makedirs(output_dir, exist_ok=True)

    # Collect matching files, sorted for deterministic ordering
    all_files = sorted(os.listdir(input_dir))
    matched   = [f for f in all_files if fnmatch.fnmatch(f, pattern)]

    if not matched:
        print(f"No files matching {pattern!r} found in {input_dir!r}.")
        return {}

    print(f"Found {len(matched)} file(s) to process in {input_dir!r}.")
    print(f"Output directory: {output_dir!r}\n")

    all_results = {}

    for filename in matched:
        input_path  = os.path.join(input_dir, filename)
        stem        = os.path.splitext(filename)[0]
        output_path = os.path.join(output_dir, f"{stem}_power.csv")

        print(f"Processing: {filename}")
        try:
            results = analyze_solar_power(input_path, output_path)
            all_results[input_path] = results
        except Exception as exc:
            print(f"  WARNING: skipped {filename!r} — {exc}\n")

    print(f"\nBatch complete. {len(all_results)}/{len(matched)} file(s) succeeded.")
    return all_results


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=(
            'Hour-by-hour solar power output from NSRDB CSV(s).\n\n'
            'Single-file mode:  supply input_csv and output_csv.\n'
            'Batch mode:        supply --input-dir and --output-dir '
            '(and optionally --pattern).'),
        formatter_class=argparse.RawDescriptionHelpFormatter)

    # --- Single-file arguments (both optional so batch mode works alone) ---
    parser.add_argument(
        'input_csv', nargs='?', default=None,
        help='Path to a single NSRDB input CSV file.')
    parser.add_argument(
        'output_csv', nargs='?', default=None,
        help='Path where the single output CSV will be written.')

    # --- Batch-mode arguments ---
    parser.add_argument(
        '--input-dir', default='NSRDB-raw',
        help="Directory of NSRDB CSVs to process (default: 'NSRDB-raw').")
    parser.add_argument(
        '--output-dir', default='gen-power',
        help="Directory for output CSVs (default: 'gen-power').")
    parser.add_argument(
        '--pattern', default='*.csv',
        help="Glob pattern to select files in --input-dir "
             "(default: '*.csv').  Example: '4469509_24.96_-78.05_*.csv'.")

    args = parser.parse_args()

    if args.input_csv and args.output_csv:
        # Single-file mode
        analyze_solar_power(args.input_csv, args.output_csv)
    else:
        # Batch mode
        analyze_directory(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            pattern=args.pattern,
        )