# ClosureOps

**Which of Alberta's 77,000 inactive oil and gas wells should be closed first, and
which ones should be closed together?**

Set a yearly budget and ClosureOps picks the wells, groups the ones close enough to
share a crew trip, and shows how many more wells that grouping buys compared with
visiting each well on its own.

![The ClosureOps app](docs/screenshot.png)

---

## The problem

Alberta has roughly **77,000 inactive wells** and about **7,300 orphan wells** — ones
whose owner no longer exists to clean them up. In July 2026 the Orphan Well
Association put the cleanup estimate at **$1.66 billion**. Companies are legally
required to plug and reclaim their inactive wells, and industry has an annual closure
spending requirement, but the money arrives one budget at a time.

Plugging a well means hauling a crew and a rig to the site. **That haul is paid per
trip, not per well.** So if ten wells sit within a few kilometres of each other, one
trip can cover all ten: the expensive long haul happens once, and the rig makes nine
short local moves instead of nine more long hauls. The AER has said area-based
closure can save up to 40%.

ClosureOps answers one question: *given this budget, which wells should we close, and
in what groups?*

It does **not** address who should pay, the provincial levy, or any of the ongoing
funding dispute. It only plans how to spend a budget that already exists.

## Who it is for

- **Company closure planners** deciding a year's programme
- **Area-based closure coordinators** looking for wells that could share a campaign
- **Orphan well planners** prioritising a fixed pot of money
- **Analysts and landowner groups** wanting to understand how closure money goes further

> **This is a screening and demonstration tool, not a basis for real spending
> decisions.** The cost inputs are placeholders, not quoted prices. Use it to
> understand the *shape* of the problem, not to commit budget.

---

## Data sources

| File | Where it comes from | What it provides |
|---|---|---|
| `Inactive_Well_Licence_List.xlsx` | [AER](https://static.aer.ca/prd/data/codes/Inactive_Well_Licence_List.xlsx) | The 76,943 inactive wells: licence, company, risk class, compliance status. **No coordinates.** |
| `Well_Licence-AB.CSV` | [Petrinex](https://www.petrinex.ca/) — Infrastructure Data Download, "Well Licence" | Legal land location per licence |
| `Well_Infrastructure-AB.CSV` | Petrinex, same page, "Well Infrastructure" | Total depth, drill type, fluid, orphan flag |

The raw files total about 390 MB and are **not committed**. The processed table
(`data/inactive_wells_joined.csv`, about 11 MB) is, so the app runs without them.

Neither Petrinex file contains latitude or longitude, which is why this project
calculates coordinates from the legal land description. The AER's own all-Alberta
licence list does carry real coordinates, but its download was not working during
this build — swapping it in is listed under *What I would add next*.

**Data: Alberta Energy Regulator and Petrinex public data. Unofficial project, not
endorsed by the AER.** The AER's [copyright terms](https://www.aer.ca/node/2584)
permit reproduction for non-commercial educational use provided the AER is credited
and the work is not presented as official. The `lat`/`lon` columns are **calculated by
this project** and are not AER data.

---

## How the model works

### 1. Locating the wells

Alberta land is surveyed on a grid, and a well's location is recorded as something
like `07-21-057-21W4` — legal subdivision 7, section 21, township 57, range 21, west
of the 4th meridian. Townships are 6×6 blocks of one-mile sections; sections are 4×4
blocks of legal subdivisions. Both are numbered in a **snake pattern** that reverses
direction every row, which is the fiddly part.

```
miles north = 6 per township + 1 per section row + ¼ per LSD row (aimed at the centre)
miles west  = 6 per range    + 1 per section col + ¼ per LSD col
latitude    = 49 + (miles north × km per mile) / 111.2
longitude   = meridian − (miles west × km per mile) / (111.32 × cos(latitude))
```

Worked example: `07-21-057-21W4` → **53.9411 N, 113.0484 W**, just east of Edmonton.

### 2. Grouping wells into trips

Wells are grouped **only within a single company**, because a licensee is responsible
for its own wells — two neighbouring wells owned by rivals will never share a crew.
There are 811 licensees in the data.

Within a company, the model sweeps west to east. Each unclaimed well starts a trip and
takes its nearest unclaimed neighbours within **15 km**, up to **25 wells**. A spatial
index does the distance lookups; a full pairwise distance table for Alberta would be
76,943² × 8 bytes = **47.4 GB**.

### 3. Pricing a trip

```
Batched    = one mobilization
           + a fixed charge per local move between wells (one fewer than the wells)
           + a per-kilometre driving charge for the route
           + the plugging work for every well

One by one = the plugging work for every well
           + a full mobilization for each well

Batching Efficiency (BE) = 1 − batched cost / one-by-one cost
```

Worked example — 3 wells, $30,000 of work each, 10 km of local driving:

```
One by one = 3 × (30,000 + 12,000)                    = $126,000
Batched    = 12,000 + 2×1,500 + 10×150 + 90,000       = $106,500
BE         = 1 − 106,500/126,000                      =     15.5%
```

### 4. Spending the budget

Each trip is scored on **risk closed per dollar** and bought best-first until the
budget will not stretch to the next one. Risk weights are High 3, Medium 2, Low and
Not Reported 1.

This is a greedy approximation to the knapsack problem, which is NP-hard to solve
exactly. It is not guaranteed optimal — on a $10,000 budget it will take a $6,000 item
worth 7 points over two $5,000 items worth 5 each, closing 7 instead of 10. The gap is
usually small, and chasing an exact optimum built on guessed costs would be precision
without accuracy.

---

## Results under the default assumptions

**These are model outputs under guessed costs, not findings.**

| Budget | Wells closed, batched | Wells closed, one at a time | Extra |
|---|---|---|---|
| $50M | **3,109** | 1,976 | +1,133 (+57%) |
| $750M | **29,857** | 21,782 | +8,075 (+37%) |

Across all 76,943 wells the model groups them into **7,053 trips** with an overall
batching efficiency of **18.9%** — in the range the AER describes for area-based
closure, though that is a coincidence of the inputs rather than a validation of them.

### Sensitivity: the result rests on one guessed number

At a $50M budget, varying the mobilization cost:

| Mobilization per trip | Batched | One at a time | Extra wells | Overall BE |
|---|---|---|---|---|
| $5,000 | 3,190 | 2,732 | **+16.8%** | 7.0% |
| **$12,000** (default) | 3,109 | 1,976 | **+57.3%** | 18.9% |
| $25,000 | 2,991 | 1,300 | **+130.1%** | 34.0% |

Tripling a figure nobody publishes moves the headline benefit from +17% to +130%.

The *direction* is robust — grouping nearby work always helps when travel is
expensive — but the **size** of the benefit is only as trustworthy as that one number.

Set mobilization to **zero** and the benefit inverts entirely: at a $750M budget the
batched plan closes **1,769 fewer** wells than doing them one at a time (30,743
against 32,512). That is the model being honest rather than broken — with no long haul
to share, grouping saves nothing while local moves between wells still cost money. The
entire benefit of this project lives inside a number nobody publishes.

---

## Assumptions and limitations

**Read this before quoting any number from this project.**

### The costs are not real

- **Mobilization cost is not published by the AER.** The $12,000 default is a
  placeholder. Every saving figure depends on it more than on anything else.
- **Well costs are depth tiers**, standing in for the AER Directive 011 cost tables:
  ≤600 m $13,300, ≤1,200 m $30,665, ≤2,000 m $45,966, deeper $59,798. The real tables
  price by region, depth, downhole scenario and add-on events.
- **Risk weights (3/2/1) are invented.** Nothing official says a High-risk well is
  worth exactly three Low-risk ones.
- The **$750M a year** industry closure quota for 2025–2027 is a sourced figure, but it
  covers more than the abandonment work modelled here.

### The geography is approximate

- **Coordinates are calculated, not surveyed.** They come from the legal land
  description and are off by roughly **3 to 11 km**. Fine for deciding which wells are
  within 15 km of each other; not fine for navigating to a well head.
- **The flat-map projection stretches east-west away from its reference latitude**
  (53.92 N). A 15 km east-west pair measures 14.32 km everywhere, while the truth is
  15.95 km at latitude 49 and 12.16 km at latitude 60. In practice the nominal 15 km
  radius behaves like about **16.7 km in the far south and 12.7 km in the far north**.
- **Driving distance is a straight-line nearest-neighbour path.** Real routes follow
  roads and are longer; nearest-neighbour routing is itself typically about 25% longer
  than the best possible route.

### The modelling choices are judgement calls

- **15 km radius** and **25 wells per batch** have no regulatory basis. Without the cap
  the clustering produces trips of nearly 1,000 wells, which no crew could perform.
- **The budget picker is greedy**, not optimal (see above).
- **Where a licence has several infrastructure records, the first is used.** For 3,830
  wells those records disagree on depth, and for 654 the disagreement crosses a cost
  tier — understating total modelled cost by about $11.1M, or **0.29%**. One-directional
  (it can only understate), and far smaller than the mobilization uncertainty.
- **122 wells have no recorded depth** and are priced at the median, 1,121 m.

### What this tool does not do

It does not decide who pays, model regulatory timelines, account for site-specific
complications, or replace an engineering estimate.

---

## Running it yourself

```bash
# 1. Create the environment and install the pinned dependencies
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate         # macOS / Linux
python -m pip install -r requirements.txt

# 2. Run the app
streamlit run app.py

# 3. Run the tests
python -m pytest

# 4. A budget plan from the command line
python -m src.plan data/inactive_wells_joined.csv 50000000
```

Use `python -m pytest` rather than bare `pytest` — the `python -m` puts the project
root on the import path, which is what makes `from src.batching import ...` resolve.

### Rebuilding the dataset from source

Only needed if you want to refresh the data. Download the three files from the table
above into `raw/`, then:

```bash
python -m src.build_dataset raw/Inactive_Well_Licence_List.xlsx \
    raw/Well_Infrastructure-AB.CSV raw/Well_Licence-AB.CSV \
    data/inactive_wells_joined.csv
```

The build prints its own acceptance checks and **stops with an error** if the licence
join looks broken or a well needs a meridian it cannot convert.

### Project layout

```
data/inactive_wells_joined.csv   one row per inactive well (committed, ~11 MB)
src/build_dataset.py             joins the three raw files, derives coordinates
src/batching.py                  groups wells into trips, prices them
src/plan.py                      spends a budget, compares both strategies
app.py                           the Streamlit app
tests/test_core.py               21 automated checks
docs/CODE_TOUR.md                a guided walk through every function
```

`docs/CODE_TOUR.md` explains what each function does and why, including the bugs found
during the build and what they changed.

---

## What I would add next

1. **Real Directive 011 costs** — replace the four depth tiers with the regional base
   abandonment tables plus add-ons (groundwater protection $46,288, surface casing vent
   flow $157,437, gas migration $148,373) and reclamation by region.
2. **Surveyed coordinates** — if the AER's all-Alberta licence list can be downloaded,
   replace the calculated positions and report how far the approximations were off.
   That would also remove the projection distortion described above.
3. **Calibrate the mobilization cost** against a real contractor quote or an OWA
   report. This single number matters more than every other improvement combined.
4. **Clustering on 3D spherical coordinates**, which would eliminate the east-west
   distortion entirely rather than documenting it.
5. **Road-network routing** instead of straight lines, for a defensible local driving
   estimate.
6. **A plain-English summary** of each plan, generated from the computed numbers only.

---

*Data: Alberta Energy Regulator and Petrinex public data. Unofficial project, not
endorsed by the AER. Latitude and longitude are calculated by this project from legal
land descriptions and are not AER data. A screening and demonstration tool — not a
basis for real spending decisions.*
