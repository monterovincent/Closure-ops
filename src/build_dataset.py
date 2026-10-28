"""Join the AER inactive well list to Petrinex well files and add approximate coordinates.

Run from the project root (argument order: inactive list, infrastructure, licence, output):
    python -m src.build_dataset raw/Inactive_Well_Licence_List.xlsx
        raw/Well_Infrastructure-AB.CSV raw/Well_Licence-AB.CSV data/inactive_wells_joined.csv
Coordinates are APPROXIMATE: derived from the legal land description (LSD-Sec-Twp-Rge-Mer),
not surveyed lat/long. Fine for 15 km clustering, not for mapping exact well heads.
"""
import sys
import numpy as np
import pandas as pd

KM_PER_MILE = 1.609344 * 1.006   # +0.6% for road allowances
MER_LON = {4: -110.0, 5: -114.0, 6: -118.0}


def dls_to_latlon(lsd, sec, twp, rge, mer):
    """Approximate centre of the LSD. Inputs are numeric arrays."""
    # section grid snakes: 1 at SE, 6 at SW, 7 above 6, ...
    s0 = sec - 1
    s_row, s_pos = s0 // 6, s0 % 6
    s_col = np.where(s_row % 2 == 0, 5 - s_pos, s_pos)        # 0 = west
    # LSD grid snakes the same way, 4x4
    l0 = lsd - 1
    l_row, l_pos = l0 // 4, l0 % 4
    l_col = np.where(l_row % 2 == 0, 3 - l_pos, l_pos)
    north_mi = (twp - 1) * 6 + s_row + (l_row + 0.5) * 0.25
    west_mi = (rge - 1) * 6 + (5 - s_col) + (3 - l_col + 0.5) * 0.25
    lat = 49.0 + north_mi * KM_PER_MILE / 111.2
    lon0 = pd.Series(mer).map(MER_LON).to_numpy()
    lon = lon0 - west_mi * KM_PER_MILE / (111.32 * np.cos(np.radians(lat)))
    return lat, lon


def build(inactive_xlsx, infra_csv, licence_csv):
    inact = pd.read_excel(inactive_xlsx, sheet_name=0, header=2,
                          dtype={"Well Licence Number": str})
    inact = inact.rename(columns={"Well Licence Number": "licence"})

    wl = pd.read_csv(licence_csv, dtype=str, usecols=[
        "LicenceNumber", "LicenceLegalSubdivision", "LicenceSection",
        "LicenceTownship", "LicenceRange", "LicenceMeridian"]).drop_duplicates("LicenceNumber")
    wi = pd.read_csv(infra_csv, dtype=str, usecols=[
        "LicenceNumber", "FinalTotalDepth", "HorizontalDrill", "WellStatusFluid",
        "Field", "FieldName", "OrphanWellFlg"]).drop_duplicates("LicenceNumber")

    df = inact.merge(wl, left_on="licence", right_on="LicenceNumber", how="left") \
              .drop(columns="LicenceNumber") \
              .merge(wi, left_on="licence", right_on="LicenceNumber", how="left") \
              .drop(columns="LicenceNumber")

    num = lambda c: pd.to_numeric(df[c], errors="coerce")
    lat, lon = dls_to_latlon(num("LicenceLegalSubdivision").to_numpy(),
                             num("LicenceSection").to_numpy(),
                             num("LicenceTownship").to_numpy(),
                             num("LicenceRange").to_numpy(),
                             num("LicenceMeridian").to_numpy())
    df["lat"], df["lon"] = lat, lon
    df["depth_m"] = num("FinalTotalDepth").replace(0, np.nan)   # 0 = not reported
    keep = ["licence", "Licensee Name", "BA Code", "AER Field Centre", "Licence Status",
            "AER Deemed Risk Class", "Directive 13 Compliance Status", "Surface Location",
            "WellStatusFluid", "HorizontalDrill", "OrphanWellFlg", "depth_m", "lat", "lon"]
    return df[keep].rename(columns=str.lower).rename(columns=lambda c: c.replace(" ", "_"))


if __name__ == "__main__":
    out = build(*sys.argv[1:4])
    out.to_csv(sys.argv[4], index=False)
    print(out.shape, "missing coords:", out[["lat", "lon"]].isna().any(axis=1).sum(),
          "missing depth:", out["depth_m"].isna().sum())
