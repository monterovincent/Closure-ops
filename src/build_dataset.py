"""Stage 2: build the one-row-per-well table that the whole project runs on.

WHAT THIS FILE DOES
Joins three downloaded files into a single table of Alberta's inactive wells, each
row carrying the company that owns it, its risk class, how deep it is, and roughly
where it sits on the map.

WHERE IT FITS
This is the first step in the chain. Nothing else reads `raw/`:

    raw/*.xlsx + raw/*.CSV  ->  THIS FILE  ->  data/inactive_wells_joined.csv
                                                      |
                                      src/plan.py and src/batching.py read that CSV

The output is committed to git (about 11 MB) so the app can run without the 390 MB
of raw downloads. That is why this script is run rarely, by hand.

WHY THE JOIN IS NEEDED AT ALL
The AER's inactive-well list says *which* wells are inactive but carries no depth
and no usable coordinates. The two Petrinex files have the depth and the legal land
location. Only together do they describe a well well enough to plan a trip to it.

HOW TO RUN IT
From the project root, with the virtual environment active. Argument order matters:
inactive list, infrastructure, licence, output.

    python -m src.build_dataset raw/Inactive_Well_Licence_List.xlsx \
        raw/Well_Infrastructure-AB.CSV raw/Well_Licence-AB.CSV \
        data/inactive_wells_joined.csv

COORDINATES ARE APPROXIMATE AND MUST ALWAYS BE DESCRIBED THAT WAY
`lat` and `lon` are *calculated by this project* from the legal land description,
not surveyed well-head positions and not AER data. A rough check against five
Alberta city locations showed errors of roughly 3 to 11 km, and those reference
points were themselves eyeballed, so the true error is not precisely known. Good
enough to decide which wells are within 15 km of each other; not good enough to
drive a truck to. See `dls_to_latlon` for the method.

DATA ATTRIBUTION (required wherever this data is shown)
Data: Alberta Energy Regulator and Petrinex public data. Unofficial project, not
endorsed by the AER.
"""
import sys

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------
# Constants. Every number used by the coordinate maths is named here so none of it
# appears as a bare digit inside a formula.
# --------------------------------------------------------------------------------

# SOURCE: 1 mile = 1.609344 km exactly (international definition).
# The +0.6% is an ASSUMPTION: Alberta's survey grid inserts road allowances between
# sections, so the ground distance covered by N "miles" of grid is slightly more
# than N miles. 0.6% was chosen to approximate that. It is not a surveyed figure.
KM_PER_MILE = 1.609344 * 1.006

# SOURCE: the Alberta township system is measured west from numbered meridians.
# The 4th meridian is the Alberta/Saskatchewan border at 110 degrees west; the 5th
# and 6th follow at 4-degree intervals.
MER_LON = {4: -110.0, 5: -114.0, 6: -118.0}

# SOURCE: the Dominion Land Survey grid starts at the 49th parallel (the Canada/US
# border) and counts townships northward from there.
BASE_LAT_DEG = 49.0

# A township is a 6x6 block of sections; a section is a 4x4 block of legal
# subdivisions (LSDs). SOURCE: Dominion Land Survey layout.
SECTIONS_PER_SIDE = 6
LSDS_PER_SIDE = 4

# A section is one mile square, so each LSD is a quarter of a mile across.
MILES_PER_LSD = 1.0 / LSDS_PER_SIDE

# ASSUMPTION (standard spherical approximation): kilometres per degree of latitude,
# and per degree of longitude at the equator. Longitude degrees shrink as you move
# north, which is why the longitude formula divides by cos(latitude).
KM_PER_DEG_LAT = 111.2
KM_PER_DEG_LON_AT_EQUATOR = 111.32

# A depth recorded as exactly 0 means "not reported", not "a well zero metres deep".
# WHY: 3,081 rows in the infrastructure file carry 0. Left as a number it would look
# like the shallowest, cheapest possible well and would be costed at the bottom tier.
DEPTH_NOT_REPORTED = 0

# Safety threshold for the join self-check in `check_join_integrity`.
# ASSUMPTION: the two independent sources of a well's location agreed on 99.99% of
# rows when measured on the 2026-10-25 snapshot, so anything below 99% means the
# join itself has broken rather than the data being slightly untidy.
MIN_LOCATION_AGREEMENT = 0.99

# The columns kept in the output, and the order they appear in. Renamed to
# snake_case below so downstream code never has to quote a column with spaces.
OUTPUT_COLUMNS = [
    "licence", "Licensee Name", "BA Code", "AER Field Centre", "Licence Status",
    "AER Deemed Risk Class", "Directive 13 Compliance Status", "Surface Location",
    "WellStatusFluid", "HorizontalDrill", "OrphanWellFlg", "depth_m", "lat", "lon",
]


def to_snake_case(column_name: str) -> str:
    """Turn a source column name into a lowercase, underscore-separated name.

    column_name: a column heading as it appears in the source file, e.g.
                 "AER Deemed Risk Class".
    Returns: the same name lowercased with spaces replaced by underscores, e.g.
             "aer_deemed_risk_class".

    WHY IT EXISTS: the three source files use three different naming styles
    ("Licensee Name", "WellStatusFluid", "depth_m"). Normalising once here means
    every other file in the project can use one predictable style.
    """
    return column_name.lower().replace(" ", "_")


def numeric_column(frame: pd.DataFrame, column_name: str) -> pd.Series:
    """Read a text column as numbers, turning anything unreadable into missing.

    frame:       the joined dataframe.
    column_name: which column to convert.
    Returns: a float Series; blanks, stray text and unmatched join rows become NaN.

    WHY IT EXISTS: every source column is deliberately read as text (see `build`
    for the leading-zero problem), so each one that is really a number has to be
    converted before use. `errors="coerce"` is the important part: a single odd
    value turns into missing instead of crashing the whole build.
    """
    return pd.to_numeric(frame[column_name], errors="coerce")


def dls_to_latlon(lsd, sec, twp, rge, mer):
    """Convert an Alberta legal land description into approximate latitude/longitude.

    lsd: legal subdivision, 1 to 16   (position inside a section)
    sec: section, 1 to 36             (position inside a township)
    twp: township, counts north from the 49th parallel
    rge: range, counts west from the meridian
    mer: meridian number, 4, 5 or 6
    All five are numeric arrays (one entry per well), not single values.

    Returns: (lat, lon) arrays in decimal degrees. Longitude is negative (west).

    WHY IT EXISTS: neither Petrinex file contains latitude or longitude, but the
    batching model cannot group wells by distance without positions. This function
    is the bridge from "07-21-057-21W4" to a point on a map. It is the single
    biggest source of approximation in the project.

    HOW THE ALBERTA GRID WORKS
    Land is divided into townships, each a 6x6 block of one-mile sections. Sections
    are numbered in a snake (boustrophedon) pattern: section 1 sits at the
    south-east corner, numbering runs west to 6 at the south-west corner, then
    jumps up a row to 7 directly above 6 and runs back east, and so on to 36 at the
    north-west. Each section is itself a 4x4 block of LSDs numbered the same
    snaking way, LSD 1 at the south-east.

    Because the numbering reverses on every other row, the column of a section
    cannot be worked out with one division; the direction depends on whether the
    row is even or odd. That is what the `np.where` calls below handle.

    THE FORMULA IN PLAIN WORDS
    Work out how many miles north of the 49th parallel the well is, and how many
    miles west of its meridian, then convert those distances to degrees.
      miles north = 6 miles per township + 1 mile per section row
                    + a quarter mile per LSD row, aimed at the middle of the LSD
      miles west  = 6 miles per range + 1 mile per section column
                    + a quarter mile per LSD column, aimed at the middle
      latitude  = 49 + (miles north converted to km) / km per degree of latitude
      longitude = meridian - (miles west in km) / (km per degree of longitude here)

    WORKED EXAMPLE: 07-21-057-21W4 (LSD 7, section 21, township 57, range 21, W4)
      section 21: (21-1)//6 = row 3, (21-1)%6 = position 2. Row 3 is odd, so the
                  column counted from the west is the position itself, 2.
      LSD 7:      (7-1)//4 = row 1, (7-1)%4 = position 2. Row 1 is odd, so the
                  column from the west is 2.
      miles north = (57-1)*6 + 3 + (1+0.5)*0.25 = 336 + 3 + 0.375 = 339.375
      miles west  = (21-1)*6 + (5-2) + (3-2+0.5)*0.25 = 120 + 3 + 0.375 = 123.375
      latitude    = 49 + 339.375*1.619/111.2            = about 53.94 N
      longitude   = -110 - 123.375*1.619/(111.32*cos(53.94)) = about -113.05
    which lands just east of Edmonton, as expected for township 57 range 21 W4.
    """
    # STEP 1: find which row and column of the township the section sits in.
    # Subtract 1 first so the arithmetic works from zero: section 1 becomes 0.
    section_index = sec - 1
    section_row = section_index // SECTIONS_PER_SIDE      # 0 at the south edge
    section_pos = section_index % SECTIONS_PER_SIDE       # 0..5 along that row

    # WHY the np.where: the snake numbering runs east-to-west on even rows and
    # west-to-east on odd rows. On an even row, position 0 is the *east* edge, so
    # its column counted from the west is 5. On an odd row the position already
    # counts from the west. Without this, half of Alberta mirrors east-west.
    section_col = np.where(section_row % 2 == 0,
                           (SECTIONS_PER_SIDE - 1) - section_pos,
                           section_pos)                   # 0 = westmost column

    # STEP 2: the same snake logic again, one level down, for the LSD inside the
    # section. Same reasoning, a 4x4 grid instead of 6x6.
    lsd_index = lsd - 1
    lsd_row = lsd_index // LSDS_PER_SIDE
    lsd_pos = lsd_index % LSDS_PER_SIDE
    lsd_col = np.where(lsd_row % 2 == 0,
                       (LSDS_PER_SIDE - 1) - lsd_pos,
                       lsd_pos)

    # STEP 3: add up the distance from the grid's origin in miles.
    # WHY the "+ 0.5" on the LSD term: rows and columns are counted from their
    # edge, but we want the *centre* of the LSD, which is half an LSD further on.
    # Without it every well would sit on its south-west corner.
    north_miles = ((twp - 1) * SECTIONS_PER_SIDE
                   + section_row
                   + (lsd_row + 0.5) * MILES_PER_LSD)

    # WHY the subtractions here: ranges are counted westward, but the column
    # numbers computed above count eastward from the west edge. Flipping them
    # ((5 - col) and (3 - col)) converts "how far from the west edge" into
    # "how far west from the meridian".
    west_miles = ((rge - 1) * SECTIONS_PER_SIDE
                  + ((SECTIONS_PER_SIDE - 1) - section_col)
                  + ((LSDS_PER_SIDE - 1) - lsd_col + 0.5) * MILES_PER_LSD)

    # STEP 4: miles to degrees. Latitude is the easy direction - degrees of
    # latitude are very nearly the same length everywhere.
    lat = BASE_LAT_DEG + north_miles * KM_PER_MILE / KM_PER_DEG_LAT

    # STEP 5: longitude, which needs the latitude first.
    # WHY divide by cos(latitude): lines of longitude converge toward the pole, so
    # a degree of longitude is narrower the further north you go. At Alberta's
    # latitudes it is only about 60% as wide as at the equator. Skipping this term
    # would push wells hundreds of kilometres west.
    meridian_lon = pd.Series(mer).map(MER_LON).to_numpy()
    lon = meridian_lon - (west_miles * KM_PER_MILE
                          / (KM_PER_DEG_LON_AT_EQUATOR * np.cos(np.radians(lat))))
    return lat, lon


def check_meridians(meridian_values: pd.Series) -> None:
    """Stop the build if any well needs a meridian the code cannot convert.

    meridian_values: the joined `LicenceMeridian` column, as numbers.
    Returns: nothing. Raises ValueError if an unmapped value is present.

    WHY IT EXISTS: `MER_LON` only knows meridians 4, 5 and 6. The Petrinex licence
    file also contains the value 0, and a well whose licence did not match the join
    at all has no meridian either. Both cases would flow through
    `pd.Series(mer).map(MER_LON)` as NaN and produce a silently blank coordinate -
    a well quietly dropped off the map with no error anywhere.

    On the 2026-10-25 snapshot the inactive wells split exactly 4: 44,870,
    5: 23,315, 6: 8,758, which sums to all 76,943 rows, so nothing is unmapped
    today. This check exists so that a future data refresh fails loudly instead of
    producing a table that looks fine and is quietly wrong.
    """
    present = set(meridian_values.dropna().unique())
    unmapped = present - set(MER_LON)
    missing_count = int(meridian_values.isna().sum())

    # WHY the int() conversion: these values arrive as numpy floats, which print as
    # "np.float64(0.0)" and make the error message hard to read. Plain ints are
    # what a person needs to see here.
    unmapped_readable = sorted(int(value) for value in unmapped)

    if unmapped or missing_count:
        raise ValueError(
            f"Cannot place every well on the map. "
            f"Unmapped meridian values: {unmapped_readable or 'none'}. "
            f"Wells with no meridian at all (join missed, or blank): {missing_count}. "
            f"Known meridians are {sorted(MER_LON)}. Add the missing meridian's "
            f"longitude to MER_LON, or investigate why the licence join failed."
        )


def check_join_integrity(frame: pd.DataFrame) -> dict:
    """Confirm the licence join lined up by comparing two independent locations.

    frame: the joined dataframe, after both merges, still using source column names.
    Returns: a small report dict with the number of rows compared and the share
             that agreed. Raises ValueError if agreement falls below
             MIN_LOCATION_AGREEMENT.

    WHY IT EXISTS: this guards against the worst failure mode in the project, which
    is also the quietest. Licence numbers carry leading zeros ("0000007"). If any
    future change reads them as numbers, "0000007" becomes 7, nothing matches on
    the join, and the output is 76,943 rows of real well names with every depth and
    coordinate blank - no exception, no warning, just a table that is entirely
    wrong. A row count check would not notice, because a left join keeps all the
    rows either way.

    HOW IT CHECKS: the AER workbook has its own `Surface Location` column holding
    the same legal land description in text form ("7-21-57-21W4"), which came from
    a different file than the Petrinex columns used for the coordinates. Parsing it
    gives a second, independent opinion on where each well is. If the join worked
    the two agree almost perfectly; if the join broke they agree on nothing.

    Measured on the 2026-10-25 snapshot: all five fields agreed on 76,938 of 76,943
    rows (99.99%). The handful of disagreements are genuine source discrepancies -
    4 on legal subdivision, 2 on section, none on township, range or meridian - so
    the threshold allows a small amount of real-world untidiness.
    """
    # STEP 1: pull the five grid numbers out of the workbook's text location.
    # The format is LSD-section-township-range + W + meridian, e.g. "7-21-57-21W4".
    # WHY \d{1,3} and not a fixed width: the workbook does not zero-pad these the
    # way the UWI column does, so "7-21-57-21W4" and "13-32-057-21W4" both occur.
    pattern = r"^(\d{1,2})-(\d{1,2})-(\d{1,3})-(\d{1,2})W(\d)$"
    parsed = frame["Surface Location"].str.strip().str.extract(pattern)
    parsed.columns = ["lsd", "sec", "twp", "rge", "mer"]

    # STEP 2: compare each grid number against the Petrinex column the coordinates
    # were actually built from. Compared as numbers, not text, so that "057" and
    # "57" count as the same township rather than a false mismatch.
    field_pairs = [
        ("lsd", "LicenceLegalSubdivision"),
        ("sec", "LicenceSection"),
        ("twp", "LicenceTownship"),
        ("rge", "LicenceRange"),
        ("mer", "LicenceMeridian"),
    ]
    agrees_everywhere = pd.Series(True, index=frame.index)
    per_field = {}
    for parsed_name, source_name in field_pairs:
        from_workbook = pd.to_numeric(parsed[parsed_name], errors="coerce")
        from_petrinex = numeric_column(frame, source_name)
        matches = from_workbook == from_petrinex
        per_field[source_name] = int((~matches).sum())
        agrees_everywhere &= matches

    agreement = float(agrees_everywhere.mean())

    # STEP 3: fail loudly if the two sources have stopped agreeing.
    if agreement < MIN_LOCATION_AGREEMENT:
        raise ValueError(
            f"The licence join looks broken. The AER workbook's Surface Location "
            f"agrees with the Petrinex grid columns on only {agreement:.2%} of rows "
            f"(expected at least {MIN_LOCATION_AGREEMENT:.0%}). "
            f"Disagreements per field: {per_field}. "
            f"First thing to check: are licence numbers still being read as text? "
            f"If they are read as numbers the leading zeros vanish and nothing joins."
        )

    return {"rows_compared": len(frame),
            "agreement": agreement,
            "disagreements_per_field": per_field}


def build(inactive_xlsx: str, infra_csv: str, licence_csv: str) -> pd.DataFrame:
    """Join the three source files into the finished one-row-per-well table.

    inactive_xlsx: path to the AER Inactive Well Licence List workbook (.xlsx).
    infra_csv:     path to the Petrinex Well Infrastructure extract (depth, fluid).
    licence_csv:   path to the Petrinex Well Licence extract (legal land location).
    Returns: a dataframe with the columns listed in OUTPUT_COLUMNS, renamed to
             snake_case, one row per inactive well.

    WHY IT EXISTS: this is the only place in the project that touches `raw/`. Every
    data quirk is absorbed here so that the model and the app can treat
    `data/inactive_wells_joined.csv` as clean input and never think about source
    formats again.
    """
    # STEP 1: read the AER inactive-well list.
    # QUIRK: the workbook has two note rows above the real header - row 0 is
    # "Report Created on", row 1 is "Petrinex Data Set (Volumetric Activity)".
    # header=2 tells pandas the third row holds the column names. Found by opening
    # the file with header=None and printing the first few rows. Without this the
    # columns come through as "Unnamed: 0", "Unnamed: 1", ...
    #
    # QUIRK: licence numbers carry leading zeros ("0000007"), and 76,721 of the
    # 76,943 wells have them. dtype=str keeps them as text. If pandas infers a
    # number instead, "0000007" becomes 7, the join silently matches nothing, and
    # every coordinate comes out blank. `check_join_integrity` below exists to
    # catch exactly that.
    inactive = pd.read_excel(inactive_xlsx, sheet_name=0, header=2,
                             dtype={"Well Licence Number": str})
    inactive = inactive.rename(columns={"Well Licence Number": "licence"})

    # STEP 2: read the legal land location for every licence in Alberta.
    # Only six columns are needed out of a 103 MB file, so usecols keeps the read
    # fast and the memory small. dtype=str again for the leading-zero reason.
    #
    # QUIRK: this file repeats licence numbers - 529,233 rows for 526,897 licences,
    # with one licence appearing three times. Keep the first row per licence.
    # Checked before trusting it: among the inactive wells, 811 licences appear
    # more than once and *none* of the duplicates disagree about the location, so
    # which row is kept makes no difference here.
    licence_location = pd.read_csv(licence_csv, dtype=str, usecols=[
        "LicenceNumber", "LicenceLegalSubdivision", "LicenceSection",
        "LicenceTownship", "LicenceRange", "LicenceMeridian",
    ]).drop_duplicates("LicenceNumber")

    # STEP 3: read the physical details of each well.
    #
    # QUIRK: this file has several rows per licence - 680,722 rows for 486,382
    # licences - because a licence can cover linked facilities and commingled
    # wellbores. Keep the first row per licence.
    #
    # WHY "first" and not "deepest": unlike the location file, these duplicates DO
    # disagree. Among the inactive wells, 24,642 licences have more than one row
    # and 3,830 of those report different depths (median spread 278 m, worst
    # 5,004 m). For 654 wells the disagreement crosses a cost-tier boundary, which
    # understates the total modelled cost by about $11.1M, or 0.29% of the $3.78B
    # all-wells total. The bias only runs one way - taking the first row can
    # understate the cost, never overstate it. Accepted deliberately: 0.29% is far
    # smaller than the guessed mobilization cost, which swings the headline result
    # by well over 100%. Revisit if the cost model is ever put on a real footing.
    well_infrastructure = pd.read_csv(infra_csv, dtype=str, usecols=[
        "LicenceNumber", "FinalTotalDepth", "HorizontalDrill", "WellStatusFluid",
        "Field", "FieldName", "OrphanWellFlg",
    ]).drop_duplicates("LicenceNumber")

    # STEP 4: attach both Petrinex files to the inactive list by licence number.
    # WHY a left join: the inactive list defines the population we care about. A
    # left join keeps all 76,943 inactive wells even when a Petrinex row is
    # missing, rather than silently shrinking the problem. The join column arrives
    # twice under two names, so the redundant "LicenceNumber" is dropped each time.
    joined = (inactive
              .merge(licence_location, left_on="licence", right_on="LicenceNumber",
                     how="left")
              .drop(columns="LicenceNumber")
              .merge(well_infrastructure, left_on="licence", right_on="LicenceNumber",
                     how="left")
              .drop(columns="LicenceNumber"))

    # STEP 5: prove the join actually lined up before building anything on top of
    # it. Both checks raise rather than warn - a wrong map is worse than no map.
    check_meridians(numeric_column(joined, "LicenceMeridian"))
    check_join_integrity(joined)

    # STEP 6: turn the legal land description into map coordinates.
    joined["lat"], joined["lon"] = dls_to_latlon(
        numeric_column(joined, "LicenceLegalSubdivision").to_numpy(),
        numeric_column(joined, "LicenceSection").to_numpy(),
        numeric_column(joined, "LicenceTownship").to_numpy(),
        numeric_column(joined, "LicenceRange").to_numpy(),
        numeric_column(joined, "LicenceMeridian").to_numpy(),
    )

    # STEP 7: clean up depth.
    # WHY replace 0 with missing: see DEPTH_NOT_REPORTED. A 0 left in place would
    # be costed as the cheapest, shallowest tier. Missing is honest, and
    # src/plan.py substitutes the median depth when it prices the well.
    joined["depth_m"] = numeric_column(joined, "FinalTotalDepth").replace(
        DEPTH_NOT_REPORTED, np.nan)

    # STEP 8: keep only the output columns and give them one consistent naming
    # style, so no downstream file has to deal with spaces or mixed capitals.
    output = joined[OUTPUT_COLUMNS]
    return output.rename(columns=to_snake_case)


if __name__ == "__main__":
    # Read the four paths from the command line in the documented order and write
    # the table. Kept deliberately thin: all the logic lives in `build` so the
    # tests can call it directly without touching the filesystem twice.
    inactive_path, infra_path, licence_path, output_path = sys.argv[1:5]
    table = build(inactive_path, infra_path, licence_path)
    table.to_csv(output_path, index=False)

    # Print the Stage 2 acceptance checks so a run is self-verifying: a glance at
    # this output is enough to tell whether the rebuild is sound.
    missing_coordinates = int(table[["lat", "lon"]].isna().any(axis=1).sum())
    print(f"wrote {output_path}: {table.shape[0]:,} rows x {table.shape[1]} columns")
    print(f"  rows missing lat/lon : {missing_coordinates:,}  (expected 0)")
    print(f"  rows missing depth_m : {table['depth_m'].isna().sum():,}  (expected ~122)")
    print(f"  licence still text   : {table['licence'].iloc[0]!r}  (expected '0000007')")
    print(f"  lat range            : {table['lat'].min():.3f} to {table['lat'].max():.3f}"
          f"  (expected 49 to 60)")
    print(f"  lon range            : {table['lon'].min():.3f} to {table['lon'].max():.3f}"
          f"  (expected -120 to -110)")
