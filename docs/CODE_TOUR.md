# ClosureOps code tour

A guided map of the code, kept up to date at the end of every stage. For each file
it lists every function with one or two plain sentences: what it does and why it
exists. Read this before reviewing a diff.

**Stage reached: 0 (setup and restructure).** The model files were moved into `src/`
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
- The function notes above are one-liners. Stages 2 to 4 expand each file to the
  commenting standard in section 0.1 of the build guide, and this tour gets the
  "why" for each formula alongside it.
