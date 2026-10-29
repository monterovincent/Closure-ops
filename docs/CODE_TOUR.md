# ClosureOps code tour

A guided map of the code, kept up to date at the end of every stage. For each file
it lists every function with one or two plain sentences: what it does and why it
exists. Read this before reviewing a diff.

**Stage reached: 4 (all three model files fully commented).** `src/build_dataset.py`,
`src/batching.py` and `src/plan.py` are all documented to the Stage 0.1 standard.
What remains is the app (Stage 5), then tests and the README (Stage 6).

> **Stage 3 changed the numbers.** A projection bug was found and fixed (details
> under `src/batching.py`). The all-wells result moved from 7,355 batches / BE
> 18.84% to **7,053 batches / BE 18.93%**, and the Stage 4 budget figures moved with
> it. The guide's Stage 4 targets were produced with the old, incorrect projection
> and are superseded by the re-baselined numbers recorded below.

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

### The workbook already carries the location (use it as a check)

The inactive workbook has a `Surface Location` column, and a `UWI` that embeds the
same thing: licence `0000007` shows `7-21-57-21W4` and `00/07-21-057-21W4/0`. It
parses for all 76,943 rows and agrees with the Petrinex licence file we actually use
for coordinates on 99.99% of them:

| Field | Disagreements |
|---|---|
| township, range, meridian | 0 |
| section | 2 |
| legal subdivision | 4 |
| **all five agree** | **76,938 of 76,943** |

Two reasons this matters. It is independent evidence the licence-number join is
sound — the failure that would otherwise be silent. And because `surface_location`
is a committed output column, the coordinates can be re-derived from it and checked
without touching `raw/`, which makes a much stronger test than checking leading
zeros alone.

It also means the 103 MB Petrinex licence file is redundant *for coordinates*: the
table could be rebuilt from the 7.9 MB workbook plus the infrastructure file (still
needed for depth). Left as a possible Stage 8 simplification, not done.

### Choosing a depth when a licence has several rows

The guide says keep the first infrastructure row per licence. "First" means
arbitrary file order, so it is worth knowing what that costs:

- 24,642 inactive wells have more than one infrastructure row
- 3,830 of those disagree on depth (median spread 278 m, worst 5,004 m)
- the disagreement crosses a cost-tier boundary for 654 wells
- keeping the first row understates total cost by **$11,098,225, or 0.29%** of the
  $3.78B all-wells unbatched total

The bias only ever runs one way: the first row can understate the cost, never
overstate it. **Decision (Vincent, Stage 1 review): keep the guide's "first row"
rule and document the bias.** At 0.29% it changes no conclusion, and the guessed
mobilization cost already moves the result by far more (±130% across the sensitivity
range). Keeping it also means the rebuilt CSV stays identical to the committed one,
so the Stage 2 "only replace it if it matches" check still works.

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

## `src/build_dataset.py` — Stage 2 (fully commented)

Builds the one-row-per-well table from the three raw files. The only file in the
project that reads `raw/`; everything downstream treats the CSV as clean input.

| Function | What and why |
|---|---|
| `to_snake_case(column_name)` | Lowercases a source column heading and swaps spaces for underscores. Exists because the three sources use three naming styles ("Licensee Name", "WellStatusFluid", "depth_m"); normalising once here means no downstream file quotes a column with a space in it. |
| `numeric_column(frame, column_name)` | Reads a text column as numbers, turning anything unreadable into missing. Needed because every column is deliberately read as text to protect the leading zeros, so each real number has to be converted back. `errors="coerce"` means one odd value becomes missing instead of killing the build. |
| `dls_to_latlon(lsd, sec, twp, rge, mer)` | Converts an Alberta legal land description into approximate latitude and longitude. Exists because neither Petrinex file carries coordinates and the batching model cannot group wells without positions. The single biggest source of approximation in the project — see the accuracy note below. |
| `check_meridians(meridian_values)` | Raises if any well needs a meridian `MER_LON` cannot convert, or has none at all. Exists because an unmapped meridian would otherwise flow through as a blank coordinate with no error — a well silently dropped off the map. |
| `check_join_integrity(frame)` | Raises if the workbook's own `Surface Location` stops agreeing with the Petrinex grid columns. Guards the project's quietest failure: if licence numbers are ever read as numbers, the leading zeros vanish, nothing joins, and the output is 76,943 rows of real company names with every coordinate blank — and a left join keeps the row count identical, so nothing else would notice. |
| `build(inactive_xlsx, infra_csv, licence_csv)` | Reads the three files, applies every documented quirk fix, joins on licence number, runs both guards, adds coordinates and a cleaned `depth_m`, and returns the 14 output columns in snake_case. |

Constants, all named and sourced in the file: `KM_PER_MILE` (exact mile-to-km, times
an assumed +0.6% for road allowances), `MER_LON` (longitudes of the 4th, 5th and 6th
meridians), `BASE_LAT_DEG` (49, where the survey grid starts), `SECTIONS_PER_SIDE`
and `LSDS_PER_SIDE` (the 6x6 and 4x4 grids), `MILES_PER_LSD`, `KM_PER_DEG_LAT` and
`KM_PER_DEG_LON_AT_EQUATOR`, `DEPTH_NOT_REPORTED` (the 0-means-missing rule),
`MIN_LOCATION_AGREEMENT` (the guard threshold) and `OUTPUT_COLUMNS`.

**Coordinate accuracy.** `lat`/`lon` are calculated by this project from the legal
land description — never describe them as AER data. A rough check against five
Alberta city locations showed errors of about 3 to 11 km, and those reference points
were themselves eyeballed, so the true error is not precisely known. Adequate for
deciding which wells fall within 15 km of each other; not adequate for navigation.
The documented worked example (`07-21-057-21W4` → 53.9411 N, -113.0484) was verified
against the code, and the snaking section numbering was spot-checked at the township
corners: sections 1 and 6 share a latitude with 6 further west, section 7 sits
directly above 6, and section 36 is the north-*east* corner, as the real survey has it.

**Reproducibility note.** Rebuilding the table with the commented code reproduces the
committed CSV exactly: same shape (76,943 x 14), same column names and order, and
identical values in all 14 columns. The rebuilt file is not byte-identical, for two
harmless reasons — pandas on Windows writes CRLF line endings while the committed
file has LF (76,944 endings = 76,944 bytes), and one float prints one digit shorter
(1 byte), which compares equal numerically. The committed file was therefore left in
place rather than replaced, to avoid an 11 MB diff that changes no data.

## `src/batching.py` — Stage 3 (fully commented)

Groups wells into crew trips and prices them against the no-batching case. This is
where the project's central claim — that closing wells together is cheaper — turns
into a number.

| Function | What and why |
|---|---|
| `Params` (dataclass) | The five modelling knobs: `radius_km`, `max_batch`, `m_out`, `m_in_base`, `m_in_per_km`. **All five are assumptions, none are sourced.** Kept in one object so the app's sliders, the tests and the command line all change the same definitions. |
| `to_xy_km(lat, lon, ref_lat)` | Projects latitude/longitude onto flat kilometre coordinates so every later distance is ordinary Pythagoras, which is what the spatial index wants. The `ref_lat` argument is the fix described below. |
| `_cluster_xy(xy, radius_km, cap)` | The clustering itself. Sweeps wells west to east; each unclaimed well becomes a seed and takes the nearest free wells within the radius, up to the cap. Greedy and deliberately not optimal — a seed claims its neighbours before later wells get a say. |
| `cluster_wells(df, p, by)` | Runs the clustering once per company (811 of them), because a licensee is legally responsible for its own wells and two neighbouring wells owned by rivals will never share a crew. Also raises if any well ends up unlabelled. |
| `local_route_km(xy)` | Estimates driving inside one batch with a nearest-neighbour path. Rough in three known ways: the route is ~25% longer than optimal, it measures straight lines not roads, and it depends on which well it starts from. |
| `batching_efficiency(df, p, by)` | Prices every batch both ways and returns the per-batch table, the summary, and the wells with their batch labels. Produces the headline Batching Efficiency figure. |

**Why there is a cap on batch size.** Without `max_batch`, the clustering produces
groups far too large to be one real campaign. Measured on the real data: uncapped and
grouped by company the biggest batch is **967 wells**; uncapped and ignoring company
it reaches **1,471**. (The guide's "over 1,000 wells" refers to that second,
pre-per-company version.) Pricing 1,471 wells as a single trip would invent an
enormous saving out of nothing.

**Why a spatial index rather than a distance table.** An all-Alberta pairwise table
would be 76,943² = 5.92 billion distances, which at 8 bytes each is **47.4 GB** —
impossible on a normal machine. A KD-tree answers "what is within 15 km of this
point" in under a second without ever building it.

### The projection bug found and fixed in Stage 3

The original formula was `x = R * radians(lon) * cos(lat)`, using **each point's own
latitude**. That makes `x` depend on latitude, so a well's x coordinate shifts when
it moves north even at identical longitude — the projection shears. Measured in
central Alberta:

| separation | true | as measured (before) | as measured (after) |
|---|---|---|---|
| 15 km north–south | 15.00 km | **27.74 km** (+85%) | 15.00 km |
| 15 km east–west | 14.97 km | 14.97 km | 14.97 km |

The effect was not cosmetic. Wells genuinely within range but stacked north–south
were rejected by the radius test, so batches came out smaller and more numerous than
the geography justified — the model **understated** what batching is worth.

The fix is what equirectangular projection actually means: pick one reference
latitude (the mean of all wells, 53.92 N), scale every longitude by its cosine, and
let `x` depend only on longitude. Verified against great-circle distance on 38,631
real within-batch pairs: median error 0.005%, worst 0.29%.

**The limitation that remains**, and it is a real one: the east–west scale is exact
only at the reference latitude. Measured on a 15 km east–west pair, the model says
14.32 km at every latitude, while the truth is 15.95 km at latitude 49 and 12.16 km
at latitude 60. So the nominal 15 km radius behaves like roughly **16.7 km for the
southernmost wells and 12.7 km for the northernmost**. This belongs in the README,
not buried in code. Eliminating it entirely would mean clustering on 3D spherical
coordinates — a candidate Stage 8 upgrade.

An earlier attempt used a *per-company* reference latitude, which is worse: a company
whose wells span the province has a mean latitude that is a poor local reference for
most of them, and 150 batches ended up accepting wells up to 16.9 km from their seed.
One shared reference for the whole run keeps the radius test consistent for everyone.

## `src/plan.py` — Stage 4 (fully commented)

Spends a budget two ways and reports both, which is the comparison the whole project
exists to make.

| Function | What and why |
|---|---|
| `depth_cost(depth_m)` | Prices the physical job of plugging one well from its depth. Exists because the budget cannot be spent without a price per well, and depth is the only cost driver the data offers. Tier boundaries are inclusive: exactly 600 m costs $13,300, 600.1 m costs $30,665. |
| `load_wells(path)` | Reads the processed CSV (licence as text, to protect the leading zeros) and adds `work_cost` and `risk_weight`. One loader so the app, the tests and the command line all read the data identically. |
| `greedy_fill(items, cost_col, value_col, budget)` | The actual spending decision: rank by risk-per-dollar, take whatever still fits, keep going past items that do not. A greedy approximation to the knapsack problem — explicitly *not* optimal, with a worked counter-example in the docstring. |
| `plan(df, budget, p)` | Runs the batching, fills the budget with whole batches, fills the same budget one well at a time, and returns both. The function the app calls. |

Constants, all labelled in the file: `RISK_WEIGHT` (an invented High 3 / Medium 2 /
Low 1 scale), `DEPTH_TIERS` (a placeholder standing in for the AER Directive 011
cost tables), and `MEDIAN_DEPTH_M` = 1,121.

**Where `MEDIAN_DEPTH_M` comes from**, since it would otherwise look invented: it is
the median `depth_m` of the *joined* table — inactive wells specifically. It is not
the median of the raw infrastructure file, which is 1,015 m, because that file covers
every well in Alberta rather than only the inactive ones. It is used for the 122
wells whose depth was never reported, and at 1,121 m it lands in the second tier, so
an unknown well is costed at $30,665.

**Why `greedy_fill` is not optimal, concretely.** On a $10,000 budget with batch D
worth 7 risk points at $6,000, and batches E and F each worth 5 at $5,000: greedy
takes D first because its ratio is best, then neither E nor F fits in the remaining
$4,000, closing 7 points. Taking E and F would have closed 10 for exactly $10,000.
Verified against the real function. Accepted because the cost inputs are guesses —
an exactly optimal plan built on an invented mobilization cost would be precision
without accuracy, and far too slow to move a slider against.

## `app.py` — Stage 5

Not written yet.

## `tests/test_core.py` — Stage 6

Not written yet.

---

## Open notes for later stages

- ~~`src/plan.py` imports `numpy` but does not use it~~ — **removed in Stage 4.**
- ~~**Stage 2:** guard the meridian lookup~~ — **done**, `check_meridians`. Verified
  to raise on meridian 0 and on a missing meridian, and to pass on 4/5/6.
- ~~**Stage 2:** assert the coordinates against `surface_location`~~ — **done**,
  `check_join_integrity`. Verified to pass on matching locations and to raise on a
  simulated broken join (0% agreement).
- ~~**Stage 2:** comment the depth choice~~ — **done**, in `build` STEP 3.
- **Stage 6:** reuse `check_join_integrity` as a test — it makes a stronger check
  than leading zeros alone and needs only the committed CSV, not `raw/`.
- **Housekeeping:** `core.autocrlf = true`, so git rewrites line endings in the
  11 MB CSV on every checkout. A one-line `.gitattributes` (`data/*.csv -text`)
  would stop git touching the data file at all. Not added — it is not in the
  guide's layout, so it is Vincent's call.
- **Stage 4:** the `1_121` median-depth fallback in `depth_cost` comes from the
  *joined* table's median depth, not from the raw infrastructure file (whose median
  is 1,015 m, since it covers all Alberta wells rather than only inactive ones).
  Say so in the comment, so the number does not look invented.
- ~~**Stage 4:** the batched well count comes out one well above the guide~~ —
  superseded. The Stage 3 projection fix re-baselined these numbers entirely; see
  "Current results" below.
- **Stage 5 — performance.** The guide assumes a full run takes about 4 seconds. It
  takes about **40 seconds**. Profiled: clustering 0.58s, projection 0.00s, groupby
  0.01s, `local_route_km` 0.97s, and **`df.iloc[positions]` per batch 32.1s** across
  7,053 slices. The pricing loop rebuilds a small dataframe for every batch. Fixing
  it means replacing the per-batch `df.iloc` with vectorised `groupby` aggregations.
  Not done in Stage 3, which was a commenting stage — but the app will feel broken
  without it.
- **Stage 6:** test the projection against great-circle distance, not just the
  worked example. The bug fixed in Stage 3 would have been caught immediately by one.

---

## Current results (defaults, after the Stage 3 projection fix)

Everything below is **under the assumed costs**, which are not sourced. The
one-by-one figures do not involve clustering, which is why they did not move.

| | before the fix | after the fix | guide's target |
|---|---|---|---|
| all-wells batches | 7,355 | **7,053** | about 7,355 |
| all-wells BE | 18.84% | **18.93%** | about 18.8% |
| $50M — batched wells | 3,114 | **3,109** | about 3,113 |
| $50M — one by one | 1,976 | 1,976 | about 1,976 |
| $750M — batched wells | 29,804 | **29,857** | about 29,803 |
| $750M — one by one | 21,782 | 21,782 | about 21,782 |

Spend stays under budget in both cases ($49,985,099 and $749,979,760). Budget `0`
closes nothing and does not crash; so do budgets of $1 and $13,300, because one well
costs its work plus a full mobilization ($13,300 + $12,000 = $25,300) and nothing
fits below that.

### Sensitivity: the result rests on a guessed number

Measured at a $50M budget on the corrected model. This is the most important table
in the project and it belongs in the README too.

| `m_out` | batched | one by one | extra wells | extra % | all-wells BE |
|---|---|---|---|---|---|
| $5,000 | 3,190 | 2,732 | 458 | **+16.8%** | 7.00% |
| $12,000 (default) | 3,109 | 1,976 | 1,133 | **+57.3%** | 18.93% |
| $25,000 | 2,991 | 1,300 | 1,691 | **+130.1%** | 33.97% |

Tripling a figure nobody published moves the headline benefit from +17% to +130%.
The guide predicted +17% / +58% / +130% from the old projection, so this conclusion
survived the Stage 3 fix essentially unchanged.

**Why a dearer haul helps batching so much:** the one-by-one plan pays that cost once
per *well*, the batched plan once per *trip*. Raising it therefore punishes the
one-by-one plan far harder — its well count collapses from 2,732 to 1,300 — while the
batched count barely moves (3,190 to 2,991).
- The function notes above are one-liners. Stages 2 to 4 expand each file to the
  commenting standard in section 0.1 of the build guide, and this tour gets the
  "why" for each formula alongside it.
