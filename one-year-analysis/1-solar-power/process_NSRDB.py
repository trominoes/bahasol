from solar_analysis import analyze_directory

results = analyze_directory(
    input_dir='one-year-analysis/1-solar-power/NSRDB-raw',
    output_dir='one-year-analysis/1-solar-power/gen-power',
    pattern='4469509_24.96_-78.05_*.csv',
)
print(f"Processed {len(results)} year(s).")