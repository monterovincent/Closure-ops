# ClosureOps code tour

A guided map of the code, kept up to date at the end of every stage. For each file
it lists every function with one or two plain sentences: what it does and why it
exists. Read this before reviewing a diff.

**Stage reached: 1 (data sources verified).** The model files were moved into `src/`
but not yet commented to the Stage 0.1 standard — that happens in Stages 2 to 4, so
the function notes below are brief placeholders for now.

---

## How the pieces connect

```
raw/                               (downloaded, never committed)
  Inactive_Well_Licence_List.xlsx    inactive wells: company, risk class, status
  Well_Licence-AB.CSV                legal land location per licence
  Well_Infrastructure-AB.CSV         depth, drill type, fluid
            |
            |  src/build_dataset.py   joins on licence number,
            |                         turns land description into approximate lat/lon
            v
data/inactive_wells_joined.csv     one row per inactive well (committed, ~11 MB)
            |
            |  src/plan.py: load_wells()   adds work_cost (depth tier) and risk_weight
            v
      wells table
            |
            |  src/batching.py        groups nearby wells of the same company into
            |                         batches = one crew trip each, and prices them
            v
      per-batch table (wells, route_km, work_cost, batched_cost, risk_score)
            |
            |  src/plan.py: greedy_fill()  buys the best risk-per-dollar batches
            |                              until the budget runs out
            v
   plan result: wells closed with batching  vs  wells closed one at a time
            |
            |  app.py (Stage 5)       sliders, metrics, map, downloadable table
            v
        the web app
```

The comparison at the bottom is the whole point of the project: the same budget,
spent two ways, to show what grouping the work is worth.

---

## The data sources (Stage 1)

Three files in `raw/`, downloaded by hand, never committed (~390 MB combined; the
infrastructure file alone is 281 MB, well over GitHub's 100 MB limit).

| File | Source | What it gives | Size |
|---|---|---|---|
| `Inactive_Well_Licence_List.xlsx` | AER, https://static.aer.ca/prd/data/codes/Inactive_Well_Licence_List.xlsx | The 76,943 inactive wells: licence, company, risk class, compliance status. **No coordinates.** | 7.9 MB |
| `Well_Licence-AB.CSV` | Petrinex, Infrastructure Data Download, "Well Licence" | Legal land location per licence (LSD, section, township, range, meridian) | 103 MB |
| `Well_Infrastructure-AB.CSV` | Petrinex, same page, "Well Infrastructure" | Final total depth, drill type, fluid, orphan flag | 281 MB |

Neither Petrinex file carries latitude or longitude — that is why `dls_to_latlon`
exists. The AER's own all-Alberta licence list does have real coordinates, but its
download was not working; swapping it in is an optional Stage 8 upgrade.

### Data quirks, each verified against the files

Measured on the 2026-10-25 snapshot. Stage 2 handles all five; the counts are here
so a reviewer can tell a handled quirk from a new bug after a data refresh.

| Quirk | Evidence | How it is handled |
|---|---|---|
| The workbook has two note rows above the header | Row 0 is "Report Created on", row 1 is "Petrinex Data Set", the real header is row 2 | `read_excel(..., header=2)` |
| Licence numbers have leading zeros | 76,721 of 76,943 begin with `0`, e.g. `0000007` | Read as text everywhere. As numbers, `0000007` becomes `7` and every join silently misses. |
| The licence file repeats licence numbers | 529,233 rows, 526,897 unique (one licence appears 3x) | `drop_duplicates("LicenceNumber")`, keep the first |
| The infrastructure file has many rows per licence (linked facilities, commingling) | 680,722 rows, 486,382 unique — 194,340 extra | `drop_duplicates("LicenceNumber")`, keep the first |
| A depth of `0` means "not reported" | 3,081 rows at exactly 0, plus 2,042 already blank | Replace `0` with missing, then fall back to the median depth when costing |

### A trap worth knowing about

The licence file contains meridian values `0, 4, 5, 6`, but `MER_LON` only maps 4, 5
and 6. On the current snapshot this is harmless: after the join, the inactive wells
split exactly `4: 44,870`, `5: 23,315`, `6: 8,758`, which sums to all 76,943 rows, so
no well needs a meridian the code cannot map. The risk is that a future refresh
*does* include one, and an unmapped meridian produces a silent `NaN` coordinate
rather than an error. Stage 2 should assert on this instead of trusting it.

### Attribution and terms (required in the README and the app footer)

> Data: Alberta Energy Regulator and Petrinex public data. Unofficial project, not
> endorsed by the AER.

The AER's copyright page (https://www.aer.ca/node/2584) permits reproduction for
personal and non-commercial educational use provided the AER is credited, the
material is accurate, and it is not presented as official or endorsed. This is a
portfolio and learning project, which fits that use. Commercial redistribution would
need written permission (media@aer.ca). The separate "AER Public Access Copyright and
Permission to Reproduce" document linked from the Petrinex page has **not** been read.

The `lat`/`lon` columns must always be described as **calculated by this project**
from the legal land description — they are not AER or Petrinex data.

---

## Run commands

All commands run **from the project root** (`closure-ops/`) with the virtual
environment active. Everything is imported as a package (`from src.batching import
Params`), so scripts are run with `python -m`, never `python src/plan.py` — the
latter breaks the imports.

| What | Command |
|---|---|
| Activate the environment (PowerShell) | `.venv\Scripts\Activate.ps1` |
| Budget plan | `python -m src.plan data/inactive_wells_joined.csv 50000000` |
| Rebuild the dataset | `python -m src.build_dataset raw/Inactive_Well_Licence_List.xlsx raw/Well_Infrastructure-AB.CSV raw/Well_Licence-AB.CSV data/inactive_wells_joined.csv` |
| Batching self-check demo | `python -m src.batching` |
| Tests (Stage 6) | `python -m pytest` |
| The app (Stage 5) | `streamlit run app.py` |

---

## `src/__init__.py`

Marks `src/` as a package. No logic. It exists so the scripts, the app and the
tests all import the model code the same way.

## `src/build_dataset.py` — Stage 2

Builds the one-row-per-well table from the three raw files.

| Function | What and why |
|---|---|
| `dls_to_latlon(lsd, sec, twp, rge, mer)` | Converts an Alberta legal land description (LSD, section, township, range, meridian) into approximate latitude and longitude. Exists because neither Petrinex file carries coordinates, and the batching model cannot group wells without positions. |
| `build(inactive_xlsx, infra_csv, licence_csv)` | Reads the three files, joins them on licence number, adds the coordinates and a cleaned `depth_m`, and returns the output columns in snake_case. |

Constants: `KM_PER_MILE` (mile-to-km with a 0.6% allowance for road allowances),
`MER_LON` (longitude of the 4th, 5th and 6th meridians).

## `src/batching.py` — Stage 3

Groups wells into crew trips and prices them against the no-batching case.

| Function | What and why |
|---|---|
| `Params` (dataclass) | The five modelling knobs: `radius_km`, `max_batch`, `m_out`, `m_in_base`, `m_in_per_km`. All assumptions, not sourced figures. Kept in one place so the app's sliders and the tests change the same numbers. |
| `to_xy_km(lat, lon)` | Projects latitude/longitude onto flat kilometre coordinates so distances are ordinary straight lines. Fine over the short distances used here. |
| `_cluster_xy(xy, radius_km, cap)` | The clustering itself: sweeps wells west to east and lets each seed well claim its nearest unassigned neighbours within the radius, up to the cap. |
| `cluster_wells(df, p, by)` | Runs the clustering once per company, because a company only closes its own wells. |
| `local_route_km(xy)` | Estimates the driving distance inside one batch with a nearest-neighbour path. A cheap stand-in for route planning. |
| `batching_efficiency(df, p, by)` | Prices every batch both ways and returns the per-batch table, a summary, and the wells with their batch labels. Produces the headline Batching Efficiency number. |

## `src/plan.py` — Stage 4

Spends a budget and reports the two totals.

| Function | What and why |
|---|---|
| `depth_cost(d)` | Maps a well's depth to a placeholder plug-and-abandon cost tier. Missing depth falls back to the median. |
| `load_wells(path)` | Reads the processed CSV (licence as text, to keep leading zeros) and adds `work_cost` and `risk_weight`. |
| `greedy_fill(items, cost_col, value_col, budget)` | Buys items in best-value-per-dollar order while they fit. A fast approximation of the knapsack problem, not guaranteed optimal. |
| `plan(df, budget, p)` | Runs the batching, fills the budget with batches, fills the same budget with single wells, and returns both so they can be compared. |

Constants: `RISK_WEIGHT` (High 3 / Medium 2 / Low 1), `DEPTH_TIERS` (placeholder
stand-in for the AER Directive 011 cost tables).

## `app.py` — Stage 5

Not written yet.

## `tests/test_core.py` — Stage 6

Not written yet.

---

## Open notes for later stages

- `src/plan.py` imports `numpy` but does not use it. Remove when Stage 4 rewrites
  that file with full comments.
- **Stage 2:** guard the meridian lookup so an unmapped meridian fails loudly
  instead of producing a silent `NaN` coordinate (see "A trap worth knowing about").
- **Stage 4:** the `1_121` median-depth fallback in `depth_cost` comes from the
  *joined* table's median depth, not from the raw infrastructure file (whose median
  is 1,015 m, since it covers all Alberta wells rather than only inactive ones).
  Say so in the comment, so the number does not look invented.
- **Stage 4:** the batched well count comes out one well above the guide's figure at
  both budgets (3,114 vs 3,113 at $50M; 29,804 vs 29,803 at $750M) while every other
  number matches exactly. Most likely a tie-break ordering difference in the
  `value / cost` sort under pandas 3.1.0, but this is unconfirmed — check it when
  rewriting `greedy_fill`.
- The function notes above are one-liners. Stages 2 to 4 expand each file to the
  commenting standard in section 0.1 of the build guide, and this tour gets the
  "why" for each formula alongside it.
