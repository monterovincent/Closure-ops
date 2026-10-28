"""Stage 3: group nearby wells into crew trips, and price the grouping.

WHAT THIS FILE DOES
Takes the table of inactive wells and answers two questions:
  1. Which wells could sensibly be closed in the same trip? (clustering)
  2. What does that grouping save, compared with visiting each well separately?

THE IDEA IN ONE PARAGRAPH
Plugging a well needs a crew and a rig hauled out to it. That haul is expensive and
it is paid per trip, not per well. If ten wells sit within a few kilometres of each
other, one trip can cover all ten: the long haul is paid once, and the rig makes
nine short local moves instead of nine more long hauls. The saving from doing that
is what this file measures, and it is called Batching Efficiency (BE).

WHERE IT FITS
    data/inactive_wells_joined.csv
        -> src/plan.py: load_wells()  adds work_cost and risk_weight
        -> THIS FILE                  adds a batch label and prices each batch
        -> src/plan.py: plan()        spends a budget on the best batches

INPUT COLUMNS
Required: `lat`, `lon`, `work_cost`. Optional: `risk_weight` (defaults to 1.0 for
every well) and a grouping column, by default `licensee_name`, because wells are
only batched within a single company.

THE HONEST WARNING
The mobilization costs in `Params` are **not published by the AER**. They are
placeholders chosen to land the result in a plausible range, not sourced figures.
Every saving number this file produces is therefore "under these assumptions" and
must be labelled that way wherever it is shown. The AER has said area-based closure
can save up to 40%; this model's default lands near 19%, which is the right order of
magnitude but is not evidence of anything.

HOW TO RUN THE SELF-CHECK DEMO
From the project root:
    python -m src.batching
That runs the model on synthetic wells, not the real data, purely to show the shape
of the output.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

# SOURCE: mean Earth radius, 6371.0088 km (IUGG mean radius R1). Used to turn
# degrees of latitude/longitude into kilometres.
R_EARTH_KM = 6371.0088

# The label given to a well before it has been put in a batch. Every well must have
# lost this label by the time clustering finishes - see the check in `cluster_wells`.
UNASSIGNED = -1


@dataclass
class Params:
    """The five numbers that drive the model. All five are assumptions.

    WHY THIS EXISTS AS A DATACLASS: the app's sliders, the tests and the command
    line all need to change these values. Keeping them in one object means there is
    exactly one definition of each, and changing a default changes it everywhere.

    NONE OF THESE ARE SOURCED FIGURES. Mobilization pricing is commercially
    sensitive and the AER does not publish it. Treat every output as conditional on
    these guesses - `m_out` in particular, which the Stage 4 sensitivity test shows
    is the number the whole result hinges on.
    """

    # ASSUMPTION: how far apart two wells can be and still share a trip.
    # No regulatory basis was found for any particular distance. 15 km is roughly
    # "same local road network, under half an hour between sites".
    radius_km: float = 15.0

    # ASSUMPTION: the most wells one campaign can realistically cover.
    # WHY A CAP EXISTS AT ALL: without one, the clustering produces batches far too
    # large to be a single real campaign. Measured on the real data: uncapped and
    # grouped by company, the biggest batch is 967 wells; uncapped and ignoring
    # company altogether it reaches 1,471. No crew closes 1,471 wells in one
    # mobilization, and pricing it as one trip would invent an enormous saving out
    # of nothing. 25 is a judgement about a plausible season of work.
    max_batch: int = 25

    # ASSUMPTION: the long-haul mobilization cost, paid once per trip. This is the
    # single most important number in the project and it is a guess.
    m_out: float = 12_000.0

    # ASSUMPTION: the fixed cost of each short move between wells inside one batch.
    m_in_base: float = 1_500.0

    # ASSUMPTION: the cost per kilometre of those short local moves.
    m_in_per_km: float = 150.0


def to_xy_km(lat, lon, ref_lat: float | None = None) -> np.ndarray:
    """Turn latitude/longitude into flat x/y kilometre coordinates.

    lat, lon: arrays of decimal degrees (longitude negative for west).
    ref_lat:  the latitude at which the east-west scale is set, in degrees. If not
              given, the mean of `lat` is used, which is the right choice whenever
              the points passed in are a local group such as one company's wells.
    Returns: an (n, 2) array of [x, y] in kilometres, x increasing eastward and
             y increasing northward.

    WHY IT EXISTS: distances on a sphere need trigonometry, which is slow and
    awkward to feed to a spatial index. Flattening the map first means every later
    distance is ordinary Pythagoras, which is what `cKDTree` and `local_route_km`
    both want.

    WHY THERE IS A SINGLE REFERENCE LATITUDE - AND THE BUG THIS AVOIDS
    Lines of longitude crowd together as you move north, so a degree of longitude
    is worth fewer kilometres in northern Alberta than at the equator. The east-west
    scale therefore has to be multiplied by cos(latitude).

    The tempting version is `x = R * radians(lon) * cos(lat)`, using each point's
    *own* latitude. That is wrong, and badly so. It makes x depend on latitude, so
    moving north changes a point's x coordinate even when its longitude has not
    changed at all - the projection shears. Measured in central Alberta before this
    was fixed: two wells exactly 15 km apart north-south came out 27.74 km apart,
    an 85% overstatement, while east-west pairs were exact.

    The consequence was not cosmetic. The 15 km radius test rejected wells that
    were genuinely within range but stacked north-south, so batches came out
    smaller and more numerous than the geography justified, and the model
    understated what batching is worth (7,355 batches instead of 7,053).

    The fix is what equirectangular projection actually means: pick ONE latitude,
    scale all longitudes by its cosine, and let x depend only on longitude. Every
    well in a run is projected against the same reference, so the whole province
    sits in one consistent coordinate system and two wells can always be compared.

    WHAT THIS STILL GETS WRONG: the east-west scale is exactly right only at the
    reference latitude, which for the real data is 53.92 N. Alberta runs from 49 to
    60 degrees north, so wells far from that reference have their east-west
    distances misjudged. Measured against true great-circle distance on a 15 km
    east-west pair:

        at latitude 49 (south)  model says 14.32 km, truth is 15.95 km
        at latitude 53.92 (ref) model says 14.32 km, truth is 14.32 km  (exact)
        at latitude 60 (north)  model says 14.32 km, truth is 12.16 km

    In practice that means the nominal 15 km radius behaves like about 16.7 km for
    the southernmost wells and about 12.7 km for the northernmost. This is a real
    limitation and belongs in the README rather than hidden here. It is an order of
    magnitude smaller than the 85% error it replaced, and it sits well inside the
    3 to 11 km of error already baked into the coordinates themselves, which are
    derived from the legal land description rather than surveyed (see
    src/build_dataset.py). Removing it entirely would mean clustering on 3D
    spherical coordinates, which is noted as a possible later upgrade.
    """
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)

    # WHY a default rather than a required argument: most callers want "project
    # this set of wells sensibly" and the mean latitude of the set is the reference
    # that minimises the stretch described above. `cluster_wells` overrides it so
    # that every company in one run shares a single reference and the batches stay
    # comparable with each other.
    if ref_lat is None:
        ref_lat = float(np.nanmean(lat))

    x = R_EARTH_KM * np.radians(lon) * np.cos(np.radians(ref_lat))
    y = R_EARTH_KM * np.radians(lat)
    return np.column_stack([x, y])


def _cluster_xy(xy: np.ndarray, radius_km: float, cap: int) -> np.ndarray:
    """Group one company's wells into batches. The core clustering routine.

    xy:        (n, 2) array of flat kilometre coordinates, one row per well.
    radius_km: how far from the seed well a well may be and still join its batch.
    cap:       the most wells allowed in one batch.
    Returns: an array of n integer batch labels, numbered from 0 within this call.

    WHY IT EXISTS: this is the decision the whole project rests on - which wells
    travel together. Everything downstream just prices the answer.

    THE METHOD, STEP BY STEP
    Walk through the wells from west to east. Take the first well that is not yet
    in a batch and call it the seed. Find every unassigned well within `radius_km`
    of it, take the `cap` nearest, and make them all one batch. Repeat until no
    well is left over.

    WHY A SPATIAL INDEX (cKDTree) AND NOT A DISTANCE TABLE: the obvious approach is
    to compute the distance between every pair of wells and read off the close
    ones. For all of Alberta that table would be 76,943 x 76,943 = 5.9 billion
    distances, which at 8 bytes each is 47.4 GB of memory - impossible on a normal
    machine. A KD-tree answers "what is within 15 km of this point" without ever
    building the full table, in well under a second.

    WHY WEST TO EAST: the sweep needs *some* fixed order, otherwise the same input
    could produce different batches on different runs and no result would be
    reproducible. Sorting by x (then y to break ties) is arbitrary but stable.

    WHAT THIS IS NOT: it is a fast, greedy rule, not an optimal grouping. A seed
    claims its neighbours before any later well gets a say, so a well can end up in
    a slightly worse batch than some rearrangement would give it. Finding a
    genuinely optimal grouping is a hard combinatorial problem and would buy very
    little here, given that the cost inputs are guesses in the first place.
    """
    well_count = len(xy)
    tree = cKDTree(xy)
    already_in_a_batch = np.zeros(well_count, bool)
    labels = np.full(well_count, UNASSIGNED)
    next_batch_number = 0

    # STEP 1: fix the order of the sweep. np.lexsort takes its keys least
    # significant first, so passing (y, x) sorts by x first and uses y only to
    # break ties between wells at the same longitude.
    sweep_order = np.lexsort((xy[:, 1], xy[:, 0]))

    for seed in sweep_order:
        # STEP 2: skip wells an earlier seed already claimed.
        if already_in_a_batch[seed]:
            continue

        # STEP 3: find everything within reach of this seed that is still free.
        # query_ball_point returns neighbours in no useful order and includes the
        # seed itself (distance zero), which is what we want - the seed must end up
        # in its own batch.
        nearby = np.array([candidate
                           for candidate in tree.query_ball_point(xy[seed], radius_km)
                           if not already_in_a_batch[candidate]])

        # STEP 4: when more wells are in reach than the cap allows, keep the
        # closest ones. WHY nearest-first: the wells nearest the seed are the
        # cheapest to add to the trip, and keeping a tight batch makes the local
        # driving estimate in `local_route_km` smaller and more believable.
        distance_to_seed = np.linalg.norm(xy[nearby] - xy[seed], axis=1)
        chosen = nearby[np.argsort(distance_to_seed)][:cap]

        # STEP 5: commit the batch and move on.
        labels[chosen] = next_batch_number
        already_in_a_batch[chosen] = True
        next_batch_number += 1

    return labels


def cluster_wells(df: pd.DataFrame, p: Params,
                  by: str | None = "licensee_name") -> pd.Series:
    """Assign every well a batch number, clustering each company separately.

    df: the wells, needing `lat` and `lon` columns (and `by`, if grouping is used).
    p:  the Params holding `radius_km` and `max_batch`.
    by: the column to cluster within, or None to ignore ownership entirely.
    Returns: a Series of batch numbers, one per row, aligned to df's index. Batch
             numbers are unique across the whole table, not just within a company.

    WHY CLUSTER PER COMPANY: a closure campaign is run by the licensee, who is
    legally responsible for its own wells. Two wells 500 m apart owned by different
    companies are not going to share a crew, a rig or a budget, so putting them in
    one batch would invent a saving nobody can collect. There are 811 distinct
    licensees in the data.

    It is also much faster. Splitting the problem into 811 small clustering runs
    keeps every spatial index tiny, which is why the whole thing finishes in well
    under a second.
    """
    # STEP 1: project the whole table once, against one shared reference latitude.
    # WHY one shared reference and not one per company: a company's wells can be
    # spread across the province, so its own mean latitude is not a good local
    # reference for all of them - using it made the effective radius vary from
    # company to company, and wells up to 16.9 km from their seed were being
    # accepted into a 15 km batch. One reference for the whole run keeps the radius
    # test consistent for everybody.
    reference_latitude = float(np.nanmean(df["lat"].to_numpy(dtype=float)))
    xy = to_xy_km(df["lat"], df["lon"], ref_lat=reference_latitude)
    labels = np.full(len(df), UNASSIGNED)

    # STEP 2: work out the row positions belonging to each company.
    # reset_index(drop=True) matters: `groupby(...).indices` gives *positions*, and
    # those positions must line up with the rows of `xy`, which was built from df in
    # order. Without the reset, a df with a non-default index would scramble them.
    if not by or by not in df:
        groups = [np.arange(len(df))]
    else:
        groups = list(df.reset_index(drop=True).groupby(by).indices.values())

    # STEP 3: cluster each company, then shift its labels so they do not collide
    # with any other company's. _cluster_xy always numbers from 0, so without the
    # running offset every company would have a batch 0 and they would merge.
    #
    next_free_label = 0
    for row_positions in groups:
        company_labels = _cluster_xy(xy[row_positions], p.radius_km, p.max_batch)
        labels[row_positions] = company_labels + next_free_label
        next_free_label += company_labels.max() + 1

    # STEP 4: make sure nothing fell through the cracks.
    # WHY THIS CHECK: pandas `groupby` silently drops rows whose grouping key is
    # missing. If a well ever had a blank `licensee_name` it would never be passed
    # to the clustering, would keep its UNASSIGNED label, and would then be treated
    # downstream as a member of one enormous province-wide batch -1 - inventing a
    # huge fake saving with no error anywhere. There are no blank company names in
    # the current data (checked: 0 of 76,943), so this never fires today. It is
    # here so that if one ever appears, the run stops instead of lying.
    unlabelled = int((labels == UNASSIGNED).sum())
    if unlabelled:
        raise ValueError(
            f"{unlabelled:,} wells were never put in a batch. This usually means "
            f"their '{by}' value is blank, because pandas groupby drops rows with a "
            f"missing grouping key. Fill in or exclude those wells before planning."
        )

    return pd.Series(labels, index=df.index, name="batch")


def local_route_km(xy: np.ndarray) -> float:
    """Estimate the driving distance needed to visit every well in one batch.

    xy: (n, 2) flat kilometre coordinates for the wells in a single batch.
    Returns: total path length in kilometres. Zero for a batch of one well, since
             there is no local move to make.

    WHY IT EXISTS: the batched cost charges for local rig moves by the kilometre,
    so the model needs some notion of how far the rig travels once it is on site.

    THE METHOD: start at the first well and repeatedly drive to the nearest well
    not yet visited. This is the classic "nearest neighbour" heuristic for the
    travelling salesman problem.

    HOW ROUGH IS IT: rough, in three ways, all of which are acceptable because the
    cost per kilometre it feeds is itself a guess.
      1. Nearest-neighbour routing is not the shortest possible route; it typically
         comes out around 25% longer than optimal.
      2. It measures straight lines, not roads, so real driving is further.
      3. The result depends on which well it starts from, and it starts from
         whichever happens to be first in the batch rather than anything meaningful.
    A full distance matrix is built here, but a batch holds at most `max_batch`
    wells (25 by default), so that is a 25 x 25 table - trivial, unlike the
    province-wide table described in `_cluster_xy`.
    """
    well_count = len(xy)
    if well_count < 2:
        return 0.0

    # STEP 1: distance from every well in the batch to every other.
    distances = np.linalg.norm(xy[:, None] - xy[None, :], axis=2)

    # STEP 2: walk the nearest-neighbour path, adding up the legs.
    visited = {0}
    current = 0
    total_km = 0.0
    while len(visited) < well_count:
        unvisited = (j for j in range(well_count) if j not in visited)
        nearest = min(unvisited, key=lambda j: distances[current, j])
        total_km += distances[current, nearest]
        visited.add(nearest)
        current = nearest
    return float(total_km)


def batching_efficiency(df: pd.DataFrame, p: Params | None = None,
                        by: str | None = "licensee_name"):
    """Cluster the wells, price every batch both ways, and report the saving.

    df: wells with `lat`, `lon`, `work_cost`, and optionally `risk_weight`.
    p:  the Params; defaults are used if none are given.
    by: the column to cluster within (see `cluster_wells`).
    Returns a 3-tuple:
      table   - one row per batch, with its size, location, route length and both costs
      summary - the totals, including the headline BE figure
      wells   - the input wells with a `batch` column added, so a caller can see
                which well went where

    WHY IT EXISTS: this turns the clustering into money, which is the only form the
    rest of the project can use. `src/plan.py` buys batches out of this table.

    THE TWO COSTS, IN PLAIN WORDS
    Every well has a `work_cost` - the physical job of plugging it - and that is
    paid either way. What changes is the travel.

      Batched (one trip for the whole group):
          pay the long haul once
        + pay a fixed charge for each local move between wells (one fewer move
          than there are wells, because the first well needs no local move)
        + pay per kilometre of local driving
        + pay the work cost of every well
      Unbatched (a separate trip to each well):
          pay the work cost of every well
        + pay the long haul once per well

      Batching Efficiency:  BE = 1 - total batched cost / total unbatched cost
    A BE of 0.19 means the batched plan costs 19% less than visiting wells one at a
    time, under these assumptions.

    WORKED EXAMPLE - 3 wells, $30,000 of work each, 10 km of local driving,
    using the default costs ($12,000 long haul, $1,500 per local move, $150/km):
        work total      = 3 x 30,000                = $90,000
        unbatched       = 90,000 + 3 x 12,000       = $126,000
        batched         = 12,000                      (one long haul)
                        + 2 x 1,500                   (two local moves)
                        + 10 x 150                    (10 km of driving)
                        + 90,000                      (the work itself)
                        = $106,500
        BE              = 1 - 106,500 / 126,000     = 0.1548, or 15.5%
    This example is checked by a test in Stage 6.
    """
    p = p or Params()

    # WHY reset_index and copy: `groupby(...).indices` below returns positions, so
    # the index must be a plain 0..n-1 range for them to line up. The copy keeps
    # this function from adding a `batch` column to the caller's dataframe.
    df = df.reset_index(drop=True).copy()

    # STEP 1: decide which wells travel together.
    df["batch"] = cluster_wells(df, p, by).to_numpy()

    # STEP 2: risk weighting is optional. WHY default to 1.0: without it, callers
    # that only care about cost (like the demo at the bottom of this file) would
    # have to invent a risk column. Weighting every well equally is the neutral
    # choice - it makes `risk_score` fall back to simply counting wells.
    if "risk_weight" not in df:
        df["risk_weight"] = 1.0

    # WHY the same shared reference latitude as `cluster_wells` uses: the route
    # length has to be measured in the same coordinate system the batches were
    # formed in, or a batch could be priced for driving further than the radius
    # test said was possible.
    reference_latitude = float(np.nanmean(df["lat"].to_numpy(dtype=float)))
    xy = to_xy_km(df["lat"], df["lon"], ref_lat=reference_latitude)

    # STEP 3: price each batch.
    batch_rows = []
    for batch_number, row_positions in df.groupby("batch").indices.items():
        wells_in_batch = df.iloc[row_positions]
        well_count = len(wells_in_batch)
        route_km = local_route_km(xy[row_positions])
        work_cost = float(wells_in_batch["work_cost"].sum())

        # The two formulas from the docstring above. The "- 1" on the local moves
        # is the point worth remembering: arriving at the first well is the long
        # haul, so only the moves *between* wells are charged locally.
        batched_cost = (p.m_out
                        + (well_count - 1) * p.m_in_base
                        + p.m_in_per_km * route_km
                        + work_cost)
        unbatched_cost = work_cost + p.m_out * well_count

        batch_rows.append({
            "batch": batch_number,
            "group": wells_in_batch[by].iloc[0] if by and by in wells_in_batch else "",
            "wells": well_count,
            # WHY the mean: the map draws one dot per batch, and the average
            # position of its wells is a fair place to put it.
            "lat": wells_in_batch["lat"].mean(),
            "lon": wells_in_batch["lon"].mean(),
            "route_km": round(route_km, 1),
            "work_cost": work_cost,
            "batched_cost": batched_cost,
            "unbatched_cost": unbatched_cost,
            # Total risk closed by doing this batch. src/plan.py treats this as the
            # "value" of the batch when deciding what to buy with the budget.
            "risk_score": float(wells_in_batch["risk_weight"].sum()),
        })

    table = pd.DataFrame(batch_rows)

    # STEP 4: add it all up into the headline numbers.
    total_unbatched = table["unbatched_cost"].sum()
    total_batched = table["batched_cost"].sum()
    summary = {
        "wells": len(df),
        "batches": len(table),
        "unbatched_cost": round(total_unbatched),
        "batched_cost": round(total_batched),
        "saving": round(total_unbatched - total_batched),
        "BE": round(float(1 - total_batched / total_unbatched), 4),
    }
    return table, summary, df


if __name__ == "__main__":
    # A self-check on invented wells, not the real data. WHY IT EXISTS: it gives a
    # way to see the shape of the output and confirm the file runs, without needing
    # the 11 MB dataset or waiting for the full province.
    #
    # The wells are placed as three tight clusters in central Alberta plus some
    # scattered strays, so the result should show a few large batches and several
    # batches of one. The seed is fixed so the numbers repeat exactly.
    rng = np.random.default_rng(7)
    cluster_centres = [(52.5, -113.8), (53.1, -114.9), (52.0, -112.9)]

    points = []
    for centre_lat, centre_lon in cluster_centres:
        points += [(centre_lat + rng.normal(0, 0.06), centre_lon + rng.normal(0, 0.09))
                   for _ in range(25)]
    points += [(rng.uniform(51.5, 54), rng.uniform(-116, -112)) for _ in range(15)]

    demo = pd.DataFrame(points, columns=["lat", "lon"])
    demo["work_cost"] = rng.choice([13_300, 30_665, 45_966, 59_798], size=len(demo))

    # by=None because these invented wells have no owner to group them by.
    _, demo_summary, _ = batching_efficiency(demo, by=None)
    print(demo_summary)
