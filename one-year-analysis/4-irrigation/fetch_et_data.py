"""
fetch_et_data.py
================
Fetches and processes climate data needed for FAO-56 Penman-Monteith
reference evapotranspiration (ET0) calculations, then writes one cleaned
daily-resolution CSV per year to the et-data/ subdirectory.

Data sources
------------
  NSRDB (local CSV, already downloaded)
    • Solar radiation: GHI, DNI, DHI  [W/m²]
    • Air temperature                  [°C]
    • Wind speed at 10 m               [m/s]

  Open-Meteo ERA5 Historical API  (no key required, free)
    URL: https://archive-api.open-meteo.com/v1/archive
    • Relative humidity at 2 m         [%]
    • Precipitation                    [mm/hr, summed to daily]

Usage
-----
    python fetch_et_data.py                          # process all years 2018–2024
    python fetch_et_data.py --year 2022
    python fetch_et_data.py --years 2018 2019 2020
    python fetch_et_data.py --nsrdb-dir PATH --output-dir PATH

Output CSV columns (one row per calendar day)
---------------------------------------------
    date, Tmax_C, Tmin_C, Tmean_C,
    RHmax_pct, RHmin_pct, RHmean_pct,
    u10_m_s, u2_m_s,
    Rs_MJ_m2_day, Ra_MJ_m2_day, Rso_MJ_m2_day,
    Rns_MJ_m2_day, Rnl_MJ_m2_day, Rn_MJ_m2_day,
    es_kPa, ea_kPa, VPD_kPa,
    delta_kPa_C, gamma_kPa_C,
    ET0_mm_day, precip_mm_day

References
----------
[FAO56]  Allen, R.G., Pereira, L.S., Raes, D., & Smith, M. (1998).
         FAO Irrigation and Drainage Paper No. 56 — Crop Evapotranspiration.
         FAO, Rome.  https://www.fao.org/4/x0490e/x0490e00.htm
         (All equation numbers below refer to this document.)

[NSRDB]  NREL National Solar Radiation Database (PSM v3).
         https://nsrdb.nrel.gov/

[OM]     Open-Meteo Historical Weather API (ERA5 reanalysis).
         https://open-meteo.com/en/docs/historical-weather-api
"""

import argparse
import csv
import json
import math
import os
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from glob import glob

# ===========================================================================
# SITE PARAMETERS
# ===========================================================================

LATITUDE_DEG  =  24.96    # °N  (from NSRDB header)
LONGITUDE_DEG = -78.05    # °E  (negative = West)
ELEVATION_M   =   9.0     # m   (from NSRDB metadata)

# Open-Meteo timezone — Bahamas use UTC-5 (EST) with no DST.
# "America/Nassau" is the correct IANA name.
TIMEZONE = "America/Nassau"

# Default years to process when no --year / --years flag is given
DEFAULT_YEARS = list(range(2018, 2025))   # 2018 through 2024 inclusive

# Solar constant  [MJ m⁻² min⁻¹]  (FAO-56 eq. 21)
GSC = 0.0820

# Stefan-Boltzmann constant  [MJ K⁻⁴ m⁻² day⁻¹]  (FAO-56 eq. 39)
SIGMA = 4.903e-9

# Reference crop albedo for net shortwave radiation (FAO-56 §3.6)
ALPHA = 0.23

# Paths relative to this script's location
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_NSRDB_DIR  = os.path.join(_HERE, '..', '1-solar-power', 'NSRDB-raw')
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, 'et-data')


# ===========================================================================
# FAO-56 THERMODYNAMICS AND RADIATION HELPERS
# ===========================================================================

def sat_vp(T_C: float) -> float:
    """
    Saturation vapour pressure e°(T) [kPa] at temperature T [°C].
    FAO-56 eq. 11.
    """
    return 0.6108 * math.exp(17.27 * T_C / (T_C + 237.3))


def actual_vp_from_rh(RH_pct: float, T_C: float) -> float:
    """
    Actual vapour pressure ea [kPa] from relative humidity and temperature.
    ea = (RH/100) × e°(T)    FAO-56 eq. 17.
    """
    return (RH_pct / 100.0) * sat_vp(T_C)


def slope_svp(T_C: float) -> float:
    """
    Slope of the saturation vapour pressure curve Δ [kPa °C⁻¹].
    FAO-56 eq. 13.
    """
    return 4098.0 * sat_vp(T_C) / (T_C + 237.3) ** 2


def psychro_const(elevation_m: float) -> float:
    """
    Psychrometric constant γ [kPa °C⁻¹].
    Atmospheric pressure from altitude: P = 101.3·((293 − 0.0065·z)/293)^5.26
    γ = 0.000665 × P    FAO-56 eqs. 7–8.
    """
    P = 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26
    return 0.000665 * P


def u2_from_uz(u_z: float, z: float = 10.0) -> float:
    """
    Convert wind speed at height z [m] to wind speed at 2 m [m/s].
    Logarithmic wind profile, FAO-56 eq. 47:
        u2 = uz × 4.87 / ln(67.8·z − 5.42)
    NSRDB provides wind speed at 10 m (z=10).
    """
    return u_z * 4.87 / math.log(67.8 * z - 5.42)


def extraterrestrial_radiation(doy: int, lat_deg: float) -> float:
    """
    Daily extraterrestrial radiation Ra [MJ m⁻² day⁻¹].
    FAO-56 eqs. 21–25.

    dr  = inverse relative Earth–Sun distance
    δ   = solar declination
    ωs  = sunset hour angle
    """
    lat_rad = math.radians(lat_deg)
    dr      = 1.0 + 0.033 * math.cos(2.0 * math.pi * doy / 365.0)
    decl    = 0.409 * math.sin(2.0 * math.pi * doy / 365.0 - 1.39)
    # Clamp argument to acos domain (avoids NaN at high latitudes near solstice)
    arg = -math.tan(lat_rad) * math.tan(decl)
    arg = max(-1.0, min(1.0, arg))
    ws  = math.acos(arg)
    # Factor (24 × 60 / π) converts Gsc [MJ m⁻² min⁻¹] to daily total [MJ m⁻² day⁻¹]
    Ra  = ((24.0 * 60.0 / math.pi) * GSC * dr
           * (ws * math.sin(lat_rad) * math.sin(decl)
              + math.cos(lat_rad) * math.cos(decl) * math.sin(ws)))
    return max(Ra, 0.0)


def clear_sky_radiation(Ra: float, elevation_m: float) -> float:
    """
    Clear-sky solar radiation Rso [MJ m⁻² day⁻¹].
    FAO-56 eq. 37:  Rso = (0.75 + 2×10⁻⁵·z) · Ra
    """
    return (0.75 + 2e-5 * elevation_m) * Ra


def net_radiation_components(Rs: float, Ra: float,
                              Tmax_C: float, Tmin_C: float,
                              ea_kPa: float, elevation_m: float):
    """
    Compute net radiation Rn [MJ m⁻² day⁻¹] and its components.

    Net shortwave (FAO-56 eq. 38):
        Rns = (1 − α) · Rs           α = 0.23 for reference grass

    Net outgoing longwave (FAO-56 eq. 39):
        Rnl = σ · mean(Tmax_K⁴, Tmin_K⁴) · (0.34 − 0.14·√ea)
                · (1.35·Rs/Rso − 0.35)

    Returns (Rn, Rns, Rnl).
    """
    Rns = (1.0 - ALPHA) * Rs

    Rso = clear_sky_radiation(Ra, elevation_m)
    Rs_ratio = min(Rs / Rso, 1.0) if Rso > 1e-6 else 0.0

    Tmax_K = Tmax_C + 273.16
    Tmin_K = Tmin_C + 273.16
    Rnl = (SIGMA
           * (Tmax_K ** 4 + Tmin_K ** 4) / 2.0
           * (0.34 - 0.14 * math.sqrt(max(ea_kPa, 0.0)))
           * (1.35 * Rs_ratio - 0.35))

    Rn = Rns - Rnl
    return Rn, Rns, Rnl


def pm_et0(Rn: float, G: float, T_mean: float,
           u2: float, es: float, ea: float,
           delta: float, gamma: float) -> float:
    """
    FAO-56 Penman-Monteith reference evapotranspiration ET0 [mm day⁻¹].
    FAO-56 eq. 6:

        ET0 = [0.408·Δ·(Rn−G) + γ·(900/(T+273))·u2·(es−ea)]
              / [Δ + γ·(1 + 0.34·u2)]

    Parameters
    ----------
    Rn    : Net radiation [MJ m⁻² day⁻¹]
    G     : Soil heat flux density [MJ m⁻² day⁻¹]  (≈ 0 for daily)
    T_mean: Mean daily air temperature [°C]
    u2    : Wind speed at 2 m [m/s]
    es    : Saturation vapour pressure [kPa]
    ea    : Actual vapour pressure [kPa]
    delta : Slope of SVP curve [kPa °C⁻¹]
    gamma : Psychrometric constant [kPa °C⁻¹]
    """
    numer = (0.408 * delta * (Rn - G)
             + gamma * (900.0 / (T_mean + 273.0)) * u2 * (es - ea))
    denom = delta + gamma * (1.0 + 0.34 * u2)
    return max(numer / denom, 0.0)


# ===========================================================================
# NSRDB READER — aggregate hourly CSV to daily values
# ===========================================================================

def read_nsrdb_daily(csv_path: str) -> dict:
    """
    Read one NSRDB hourly CSV and return a dict keyed by ISO date string
    (``'YYYY-MM-DD'``).  Each value is a sub-dict with:
        Tmax_C, Tmin_C, Tmean_C  [°C]
        u10_mean_m_s             [m/s]   daily mean wind speed at 10 m
        Rs_MJ_m2_day             [MJ/m²/day]  from GHI

    NSRDB file layout (see solar_analysis.py for full description):
        Row 1: metadata keys
        Row 2: metadata values
        Row 3: column headers
        Rows 4+: hourly data
    """
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        _meta_keys   = next(reader)   # Row 1 (unused here)
        _meta_values = next(reader)   # Row 2 (unused here)
        col_headers  = next(reader)   # Row 3
        data_rows    = list(reader)   # Rows 4+

    col = {name.strip(): idx for idx, name in enumerate(col_headers)}

    daily: dict = {}
    for row in data_rows:
        if not any(row):
            continue
        year  = int(row[col['Year']])
        month = int(row[col['Month']])
        day   = int(row[col['Day']])
        d_str = f"{year:04d}-{month:02d}-{day:02d}"

        T   = float(row[col['Temperature']])    # °C
        ws  = float(row[col['Wind Speed']])     # m/s at 10 m
        ghi = float(row[col['GHI']])            # W/m²

        if d_str not in daily:
            daily[d_str] = {'T': [], 'ws': [], 'ghi': []}
        daily[d_str]['T'].append(T)
        daily[d_str]['ws'].append(ws)
        daily[d_str]['ghi'].append(ghi)

    result: dict = {}
    for d_str, v in daily.items():
        T_list  = v['T']
        ws_list = v['ws']
        ghi_list = v['ghi']
        # Solar radiation: sum of hourly GHI [W/m²] × 3600 s → [J/m²] ÷ 1e6 → [MJ/m²]
        Rs_MJ = sum(ghi_list) * 3600.0 / 1e6
        result[d_str] = {
            'Tmax_C'      : max(T_list),
            'Tmin_C'      : min(T_list),
            'Tmean_C'     : sum(T_list) / len(T_list),
            'u10_mean_m_s': sum(ws_list) / len(ws_list),
            'Rs_MJ_m2_day': Rs_MJ,
        }
    return result


def find_nsrdb_file(nsrdb_dir: str, year: int):
    """Return path to the NSRDB CSV for the given year, or None if not found."""
    pattern = os.path.join(os.path.abspath(nsrdb_dir), f'*_{year}.csv')
    matches = glob(pattern)
    return matches[0] if matches else None


# ===========================================================================
# OPEN-METEO FETCHER — relative humidity and precipitation
# ===========================================================================

def fetch_openmeteo_daily(year: int, lat: float, lon: float,
                           timezone: str) -> dict:
    """
    Query the Open-Meteo ERA5 historical API for hourly relative humidity and
    precipitation, then aggregate to daily values.

    Returns a dict keyed by ISO date string with:
        RHmax_pct, RHmin_pct, RHmean_pct  [%]
        precip_mm_day                      [mm/day]  (sum of hourly values)

    Open-Meteo endpoint: https://archive-api.open-meteo.com/v1/archive
    No API key is required.  Free for non-commercial use.
    Reference: https://open-meteo.com/en/docs/historical-weather-api
    """
    start = f"{year}-01-01"
    end   = f"{year}-12-31"
    tz_enc = timezone.replace('/', '%2F')
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start}&end_date={end}"
        f"&hourly=relative_humidity_2m,precipitation"
        f"&timezone={tz_enc}"
        f"&wind_speed_unit=ms"
        f"&precipitation_unit=mm"
    )

    print(f"  Querying Open-Meteo (ERA5) for {year} ...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'fetch_et_data/1.0'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Open-Meteo API call failed for {year}: {exc}\n"
            "Check your internet connection and try again.") from exc

    times  = payload['hourly']['time']                     # "YYYY-MM-DDTHH:MM"
    rh_h   = payload['hourly']['relative_humidity_2m']    # [%], may contain None
    prec_h = payload['hourly']['precipitation']            # [mm/hr], may contain None

    daily: dict = {}
    for t_str, rh_val, p_val in zip(times, rh_h, prec_h):
        d_str = t_str[:10]   # "YYYY-MM-DD"
        if d_str not in daily:
            daily[d_str] = {'rh': [], 'precip_mm': 0.0}
        if rh_val is not None:
            daily[d_str]['rh'].append(float(rh_val))
        if p_val is not None:
            daily[d_str]['precip_mm'] += float(p_val)

    result: dict = {}
    for d_str, v in daily.items():
        rh_list = v['rh']
        if rh_list:
            result[d_str] = {
                'RHmax_pct'    : max(rh_list),
                'RHmin_pct'    : min(rh_list),
                'RHmean_pct'   : sum(rh_list) / len(rh_list),
                'precip_mm_day': round(v['precip_mm'], 2),
            }
    return result


# ===========================================================================
# MERGE AND COMPUTE ET0
# ===========================================================================

def compute_daily_et0(nsrdb_daily: dict, om_daily: dict) -> list:
    """
    Merge NSRDB and Open-Meteo daily data, compute FAO-56 ET0 for each day.
    Only dates present in both datasets are included.

    Returns a sorted list of row dicts (one per day).
    """
    gamma = psychro_const(ELEVATION_M)

    rows = []
    common_dates = sorted(set(nsrdb_daily.keys()) & set(om_daily.keys()))

    for d_str in common_dates:
        ns = nsrdb_daily[d_str]
        om = om_daily[d_str]

        Tmax  = ns['Tmax_C']
        Tmin  = ns['Tmin_C']
        Tmean = (Tmax + Tmin) / 2.0
        u10   = ns['u10_mean_m_s']
        u2    = u2_from_uz(u10, z=10.0)
        Rs    = ns['Rs_MJ_m2_day']

        RHmax = om['RHmax_pct']
        RHmin = om['RHmin_pct']

        # Saturation vapour pressure [kPa]
        # FAO-56 eq. 12: es = mean of e°(Tmax) and e°(Tmin)
        es_Tmax = sat_vp(Tmax)
        es_Tmin = sat_vp(Tmin)
        es      = (es_Tmax + es_Tmin) / 2.0

        # Actual vapour pressure [kPa]
        # FAO-56 eq. 19: ea = mean of [e°(Tmin)·RHmax/100, e°(Tmax)·RHmin/100]
        # This two-point method is recommended when only daily RH is available.
        ea = (actual_vp_from_rh(RHmax, Tmin) + actual_vp_from_rh(RHmin, Tmax)) / 2.0
        ea = max(ea, 0.001)   # guard against non-physical zero

        delta = slope_svp(Tmean)

        # Extraterrestrial and net radiation
        d_obj = date.fromisoformat(d_str)
        doy   = d_obj.timetuple().tm_yday
        Ra    = extraterrestrial_radiation(doy, LATITUDE_DEG)
        Rso   = clear_sky_radiation(Ra, ELEVATION_M)
        Rn, Rns, Rnl = net_radiation_components(Rs, Ra, Tmax, Tmin, ea, ELEVATION_M)

        # Soil heat flux ≈ 0 for daily time step  (FAO-56 §3.5)
        G = 0.0

        ET0 = pm_et0(Rn, G, Tmean, u2, es, ea, delta, gamma)

        rows.append({
            'date'            : d_str,
            'Tmax_C'          : round(Tmax,  2),
            'Tmin_C'          : round(Tmin,  2),
            'Tmean_C'         : round(Tmean, 2),
            'RHmax_pct'       : round(RHmax, 1),
            'RHmin_pct'       : round(RHmin, 1),
            'RHmean_pct'      : round(om['RHmean_pct'], 1),
            'u10_m_s'         : round(u10,   3),
            'u2_m_s'          : round(u2,    3),
            'Rs_MJ_m2_day'    : round(Rs,    3),
            'Ra_MJ_m2_day'    : round(Ra,    3),
            'Rso_MJ_m2_day'   : round(Rso,   3),
            'Rns_MJ_m2_day'   : round(Rns,   3),
            'Rnl_MJ_m2_day'   : round(Rnl,   3),
            'Rn_MJ_m2_day'    : round(Rn,    3),
            'es_kPa'          : round(es,     4),
            'ea_kPa'          : round(ea,     4),
            'VPD_kPa'         : round(es - ea, 4),
            'delta_kPa_C'     : round(delta,  4),
            'gamma_kPa_C'     : round(gamma,  4),
            'ET0_mm_day'      : round(ET0,    2),
            'precip_mm_day'   : round(om['precip_mm_day'], 2),
        })

    return rows


def write_csv(rows: list, output_path: str):
    """Write a list of dicts to CSV."""
    if not rows:
        print("  No data to write.")
        return
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {len(rows)} daily rows → {output_path}")


# ===========================================================================
# PER-YEAR PROCESSING
# ===========================================================================

def process_year(year: int, nsrdb_dir: str, output_dir: str):
    """Fetch, merge, compute ET0, and write CSV for one calendar year."""
    print(f"\n{'=' * 56}")
    print(f"  Year: {year}")
    print(f"{'=' * 56}")

    # 1. NSRDB
    nsrdb_path = find_nsrdb_file(nsrdb_dir, year)
    if nsrdb_path is None:
        raise FileNotFoundError(
            f"No NSRDB CSV found for {year} in: {os.path.abspath(nsrdb_dir)}\n"
            "Expected filename pattern: *_{year}.csv")
    print(f"  NSRDB file : {os.path.basename(nsrdb_path)}")
    nsrdb_daily = read_nsrdb_daily(nsrdb_path)
    print(f"  NSRDB days : {len(nsrdb_daily)}")

    # 2. Open-Meteo
    om_daily = fetch_openmeteo_daily(year, LATITUDE_DEG, LONGITUDE_DEG, TIMEZONE)
    print(f"  OM days    : {len(om_daily)}")

    # 3. Merge and compute ET0
    rows = compute_daily_et0(nsrdb_daily, om_daily)
    print(f"  Merged days: {len(rows)}")

    # 4. Write output
    out_path = os.path.join(output_dir, f"et_data_{year}.csv")
    write_csv(rows, out_path)

    # 5. Summary statistics
    if rows:
        et_vals    = [r['ET0_mm_day']    for r in rows]
        prec_vals  = [r['precip_mm_day'] for r in rows]
        T_vals     = [r['Tmean_C']       for r in rows]
        print()
        print(f"  Mean ET0       : {sum(et_vals)/len(et_vals):.2f} mm/day")
        print(f"  Max ET0        : {max(et_vals):.2f} mm/day")
        print(f"  Annual ET0     : {sum(et_vals):.0f} mm/year")
        print(f"  Annual rainfall: {sum(prec_vals):.0f} mm/year")
        print(f"  Mean temp      : {sum(T_vals)/len(T_vals):.1f} °C")

    return rows


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            'Compute FAO-56 Penman-Monteith ET0 for Andros Island, Bahamas.\n'
            'Reads NSRDB CSVs (solar, temp, wind) and fetches Open-Meteo ERA5\n'
            '(humidity, precipitation) to produce a cleaned daily ET data CSV\n'
            'for each requested year.'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Examples:\n'
            '  python fetch_et_data.py                    # all years 2018–2024\n'
            '  python fetch_et_data.py --year 2022\n'
            '  python fetch_et_data.py --years 2020 2021 2022\n'
        ),
    )
    parser.add_argument(
        '--year', type=int, metavar='YYYY',
        help='Single year to process.')
    parser.add_argument(
        '--years', type=int, nargs='+', metavar='YYYY',
        help='One or more years to process (space-separated).')
    parser.add_argument(
        '--nsrdb-dir', default=DEFAULT_NSRDB_DIR, metavar='DIR',
        help=f"Directory of NSRDB raw CSVs (default: {DEFAULT_NSRDB_DIR}).")
    parser.add_argument(
        '--output-dir', default=DEFAULT_OUTPUT_DIR, metavar='DIR',
        help=f"Output directory for et_data_YYYY.csv files (default: {DEFAULT_OUTPUT_DIR}).")
    args = parser.parse_args()

    if args.years:
        years = sorted(args.years)
    elif args.year:
        years = [args.year]
    else:
        years = DEFAULT_YEARS

    os.makedirs(args.output_dir, exist_ok=True)

    success, failed = 0, []
    for yr in years:
        try:
            process_year(yr, args.nsrdb_dir, args.output_dir)
            success += 1
        except Exception as exc:
            print(f"  ERROR for {yr}: {exc}")
            failed.append(yr)

    print(f"\n{'=' * 56}")
    print(f"  Batch complete: {success}/{len(years)} year(s) succeeded.")
    if failed:
        print(f"  Failed years: {failed}")
    print(f"  ET data directory: {os.path.abspath(args.output_dir)}")
    print(f"{'=' * 56}")


if __name__ == '__main__':
    main()
