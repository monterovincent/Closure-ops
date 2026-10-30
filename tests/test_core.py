"""Stage 6: automated checks for the ClosureOps model.

WHAT THESE TESTS ARE FOR
Every test here exists because something could go wrong silently. A wrong number
that still looks like a number is the failure mode this project is most exposed to:
the data joins cleanly whether or not the join worked, the model produces a saving
whether or not the geometry is right, and the app displays whatever it is handed.
These tests pin down the things that have no other alarm.

Two of them are regression tests for bugs that were actually found during the build,
and are marked as such. The rest guard invariants that must hold for the result to
mean anything.

HOW TO RUN THEM
From the project root, with the virtual environment active:
    python -m pytest

Use `python -m pytest`, not bare `pytest`. The leading `python -m` puts the project
root on the import path, which is what lets `from src.batching import ...` resolve.

WHAT THESE TESTS DELIBERATELY DO NOT NEED
None of them read anything from `raw/`. That folder is roughly 390 MB and is not
committed, so a test that needed it could never run on a fresh clone or in the
cloud. The checks that need real data use `data/inactive_wells_joined.csv`, which is
committed.
"""
import numpy as np
import pandas as pd
import pytest

from src.batching import (Params, R_EARTH_KM, UNASSIGNED, batching_efficiency,
                          cluster_wells, to_xy_km)
from src.build_dataset import MER_LON, check_meridians, dls_to_latlon
from src.plan import depth_cost, greedy_fill, load_wells, plan

# The processed table, committed to the repo so these tests run anywhere.
DATA_PATH = "data/inactive_wells_joined.csv"

# How far a derived coordinate may sit from a known town before we call it wrong.
# DELIBERATELY LOOSE. Two different approximations stack up here: the coordinates
# come from a legal land description rather than a survey, and the "true" position
# of a town is its centre, which is not the same point as the middle of the township
# it sits in. Measured errors run from 0.7 km to 7.4 km, so 12 km catches a gross
# error - a mirrored grid, a wrong meridian, a sign flip - without pretending to
# measure accuracy the method does not have.
TOWN_TOLERANCE_KM = 12.0

# Section 16, LSD 16 is the section/LSD pair closest to the centre of a township.
# WHY IT MATTERS: a township is about 9.7 km across, so testing against its
# south-east corner (section 1, LSD 1) would add up to 7 km of offset that has
# nothing to do with whether the conversion is right.
TOWNSHIP_CENTRE_SECTION = 16
TOWNSHIP_CENTRE_LSD = 16


def great_circle_km(lat1, lon1, lat2, lon2):
    """True distance over the Earth's surface, in kilometres.

    WHY IT EXISTS: the model flattens the map to make distances cheap to compute.
    That is an approximation, and the only way to test an approximation is against
    something exact. This is the haversine formula, and it is deliberately written
    out here rather than imported from the model, so that a bug in the model's own
    geometry cannot hide by also being present in the test.
    """
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = np.radians(lon2 - lon1)
    a = np.sin(d_phi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(d_lambda / 2) ** 2
    return 2 * R_EARTH_KM * np.arcsin(np.sqrt(a))


def make_test_wells(seed: int = 7) -> pd.DataFrame:
    """Build a small, fixed set of invented wells for the clustering tests.

    Returns: a dataframe with lat, lon, work_cost, risk_weight and licensee_name.

    WHY INVENTED RATHER THAN REAL: these tests check structural rules - every well
    in one batch, nobody over the cap - which hold for any input. A small fixed set
    makes them fast and makes a failure easy to read. The seed is fixed so a failure
    is reproducible rather than appearing one run in ten.

    The wells are placed as three tight clusters plus scattered strays, across two
    companies, so that the batching has something real to do and the per-company
    rule gets exercised.
    """
    rng = np.random.default_rng(seed)
    centres = [(52.5, -113.8), (53.1, -114.9), (52.0, -112.9)]

    points = []
    for centre_lat, centre_lon in centres:
        points += [(centre_lat + rng.normal(0, 0.05), centre_lon + rng.normal(0, 0.08))
                   for _ in range(20)]
    points += [(rng.uniform(51.5, 54.0), rng.uniform(-116.0, -112.0)) for _ in range(10)]

    wells = pd.DataFrame(points, columns=["lat", "lon"])
    wells["work_cost"] = rng.choice([13_300.0, 30_665.0, 45_966.0, 59_798.0], size=len(wells))
    wells["risk_weight"] = rng.choice([1.0, 2.0, 3.0], size=len(wells))
    wells["licensee_name"] = ["Company A" if i % 2 == 0 else "Company B"
                              for i in range(len(wells))]
    return wells


# --------------------------------------------------------------------------------
# 1. Turning a legal land description into coordinates
# --------------------------------------------------------------------------------

def test_dls_to_latlon_reproduces_the_documented_example():
    """PROVES: the worked example written in the code's own docstring is true.

    `dls_to_latlon` documents 07-21-057-21W4 as landing at about 53.94 N, 113.05 W.
    A docstring that drifts away from the code is worse than no docstring, because
    a reader trusts it. This pins the two together.
    """
    lat, lon = dls_to_latlon(np.array([7]), np.array([21]), np.array([57]),
                             np.array([21]), np.array([4]))
    assert lat[0] == pytest.approx(53.9411, abs=0.001)
    assert lon[0] == pytest.approx(-113.0484, abs=0.001)


def test_dls_to_latlon_lands_near_known_towns():
    """PROVES: the conversion puts wells in roughly the right place on Earth.

    Five Alberta towns, each with the township and range it sits in and its real
    centre coordinates. Every one must land within TOWN_TOLERANCE_KM.

    WHAT THIS CATCHES: gross errors, which is what matters here. A mirrored section
    grid, a meridian mapped to the wrong longitude, a missing cos(latitude) term or
    a sign flip would all throw a town hundreds of kilometres out. It does NOT prove
    the method is accurate to a few kilometres - see TOWN_TOLERANCE_KM for why that
    claim is not available.
    """
    towns = [
        # name,            township, range, meridian, true latitude, true longitude
        ("Edmonton",             52, 24, 4, 53.5461, -113.4938),
        ("Calgary",              24,  1, 5, 51.0447, -114.0719),
        ("Red Deer",             38, 27, 4, 52.2681, -113.8112),
        ("Lethbridge",            8, 21, 4, 49.6956, -112.8451),
        ("Grande Prairie",       71,  6, 6, 55.1707, -118.7947),
    ]
    for name, township, rge, meridian, true_lat, true_lon in towns:
        lat, lon = dls_to_latlon(np.array([TOWNSHIP_CENTRE_LSD]),
                                 np.array([TOWNSHIP_CENTRE_SECTION]),
                                 np.array([township]), np.array([rge]),
                                 np.array([meridian]))
        error_km = great_circle_km(lat[0], lon[0], true_lat, true_lon)
        assert error_km < TOWN_TOLERANCE_KM, (
            f"{name} came out {error_km:.1f} km from where it should be")


def test_section_numbering_follows_the_snake_pattern():
    """PROVES: the boustrophedon ("snake") section numbering is handled correctly.

    Sections in a township are numbered in alternating directions: 1 at the
    south-east corner, running west to 6 at the south-west, then up a row to 7
    directly above 6 and back east, and so on to 36 at the north-east.

    WHAT THIS CATCHES: dropping the direction flip would mirror half of Alberta
    east-to-west. Every well would still get a plausible-looking coordinate inside
    the province, and no other test here would necessarily notice.
    """
    def position(section):
        lat, lon = dls_to_latlon(np.array([1]), np.array([section]), np.array([57]),
                                 np.array([21]), np.array([4]))
        return lat[0], lon[0]

    lat_1, lon_1 = position(1)      # south-east corner
    lat_6, lon_6 = position(6)      # south-west corner, same row
    lat_7, lon_7 = position(7)      # directly above section 6
    lat_36, lon_36 = position(36)   # north-east corner

    # Sections 1 and 6 share a row, so the same latitude, with 6 further west.
    assert lat_1 == pytest.approx(lat_6, abs=1e-9)
    assert lon_6 < lon_1

    # Section 7 sits directly above 6: further north, near the same longitude.
    assert lat_7 > lat_6
    assert lon_7 == pytest.approx(lon_6, abs=0.01)

    # Section 36 is the north-east corner, so north of 1 and east of 6.
    assert lat_36 > lat_1
    assert lon_36 > lon_6


# --------------------------------------------------------------------------------
# 2. The flat-map projection
# --------------------------------------------------------------------------------

def test_projection_agrees_with_true_distance_in_every_direction():
    """REGRESSION TEST for the projection bug found in Stage 3.

    PROVES: distances measured on the flattened map match real distances, whichever
    way the two points are separated.

    THE BUG THIS GUARDS: `to_xy_km` originally computed x using each point's own
    latitude, which made x change when a point moved north even at an unchanged
    longitude. Two wells 15 km apart north-to-south measured 27.74 km - an 85%
    overstatement - while east-west pairs were exact. The effect was that genuinely
    nearby wells stacked north-south were rejected by the radius test, so the model
    quietly understated the very thing it exists to measure.

    Nothing else caught it. The totals still looked reasonable, every well still got
    a batch, and no exception was raised. Checking north-south separately from
    east-west is what makes the bug visible, so both directions are tested here.
    """
    base_lat, base_lon = 52.0, -113.0
    reference_latitude = base_lat

    cases = [
        ("north-south", 52.1349, -113.0),
        ("east-west",   52.0,    -112.7813),
        ("diagonal",    52.0670, -112.8900),
    ]
    for direction, other_lat, other_lon in cases:
        xy = to_xy_km([base_lat, other_lat], [base_lon, other_lon],
                      ref_lat=reference_latitude)
        measured_km = float(np.linalg.norm(xy[0] - xy[1]))
        true_km = great_circle_km(base_lat, base_lon, other_lat, other_lon)
        relative_error = abs(measured_km - true_km) / true_km
        assert relative_error < 0.01, (
            f"{direction}: projection says {measured_km:.2f} km, "
            f"truth is {true_km:.2f} km")


# --------------------------------------------------------------------------------
# 3. Grouping wells into batches
# --------------------------------------------------------------------------------

def test_every_well_lands_in_exactly_one_batch():
    """PROVES: no well is lost and none is counted twice.

    WHY IT MATTERS: the plan's headline is a count of wells closed. If clustering
    dropped wells the number would be quietly too low; if it double-counted them it
    would be too high. Either way nothing would raise an error, and the totals would
    still look plausible.
    """
    wells = make_test_wells()
    labels = cluster_wells(wells, Params())

    assert len(labels) == len(wells)
    assert (labels != UNASSIGNED).all(), "some well never got a batch"

    # The sizes of all batches must add back up to the number of wells, which is
    # only true if each well belongs to exactly one.
    assert labels.value_counts().sum() == len(wells)


def test_no_batch_is_larger_than_the_cap():
    """PROVES: `max_batch` is actually enforced, at several settings.

    WHY IT MATTERS: the cap is what keeps a batch to something a real crew could do.
    Without it the clustering produces groups of nearly a thousand wells, and
    pricing those as a single trip would invent an enormous saving out of nothing.
    """
    wells = make_test_wells()
    for cap in (3, 5, 25):
        labels = cluster_wells(wells, Params(max_batch=cap))
        largest = labels.value_counts().max()
        assert largest <= cap, f"cap {cap} produced a batch of {largest}"


def test_batch_members_are_within_the_radius_of_their_seed():
    """PROVES: `radius_km` is respected - no batch secretly spans further.

    HOW THE SEED IS FOUND: the clustering sweeps west to east and starts each batch
    from the first well it meets that has no batch yet. So the seed is whichever
    member comes first in that same west-to-east order, which is what is recomputed
    here. The projection has to use the same shared reference latitude the
    clustering used, or the distances would not be the ones it actually tested.

    WHY IT MATTERS: the radius is the promise the model makes about what a single
    trip means. If members could sit further out, the local driving estimate - and
    therefore the cost - would be wrong.
    """
    wells = make_test_wells()
    params = Params(radius_km=15.0)
    labels = cluster_wells(wells, params)

    reference_latitude = float(np.nanmean(wells["lat"].to_numpy(dtype=float)))
    xy = to_xy_km(wells["lat"], wells["lon"], ref_lat=reference_latitude)

    for batch_number, positions in wells.assign(batch=labels.to_numpy()).groupby("batch").indices.items():
        batch_xy = xy[positions]
        sweep_order = np.lexsort((batch_xy[:, 1], batch_xy[:, 0]))
        seed_xy = batch_xy[sweep_order[0]]
        distances = np.linalg.norm(batch_xy - seed_xy, axis=1)
        assert distances.max() <= params.radius_km + 1e-9, (
            f"batch {batch_number} has a well {distances.max():.3f} km from its seed")


def test_clustering_refuses_to_run_with_a_missing_company():
    """PROVES: the guard in `cluster_wells` fires instead of losing wells silently.

    THE TRAP: pandas `groupby` drops rows whose grouping key is missing. A well with
    a blank `licensee_name` would never be handed to the clustering at all, would
    keep its "unassigned" label, and would then be treated downstream as a member of
    one enormous province-wide batch - inventing a large fake saving with no error
    anywhere.

    There are no blank company names in the current data, so this guard never fires
    in practice. That is exactly why it needs a test: untriggered guards rot.
    """
    wells = make_test_wells()
    wells.loc[0, "licensee_name"] = None

    with pytest.raises(ValueError, match="never put in a batch"):
        cluster_wells(wells, Params())


# --------------------------------------------------------------------------------
# 4. What a batch costs
# --------------------------------------------------------------------------------

def test_worked_example_gives_the_documented_efficiency():
    """PROVES: the worked example in `batching_efficiency`'s docstring is true.

    Three wells, $30,000 of plugging work each, 10 km of local driving, at the
    default costs:
        one at a time = 3 x (30,000 + 12,000)                       = $126,000
        batched       = 12,000 + 2 x 1,500 + 10 x 150 + 90,000      = $106,500
        efficiency    = 1 - 106,500 / 126,000                       = 15.48%

    The three wells are placed in a straight north-south line exactly 5 km apart, so
    the nearest-neighbour route through them is exactly 10 km.
    """
    km_per_degree_latitude = R_EARTH_KM * np.pi / 180
    five_km_step = 5.0 / km_per_degree_latitude

    wells = pd.DataFrame({
        "lat": [52.0, 52.0 + five_km_step, 52.0 + 2 * five_km_step],
        "lon": [-113.0, -113.0, -113.0],
        "work_cost": [30_000.0, 30_000.0, 30_000.0],
    })
    table, summary, _ = batching_efficiency(wells, Params(), by=None)

    assert len(table) == 1, "three wells 5 km apart should form one batch"
    assert table.iloc[0]["route_km"] == pytest.approx(10.0, abs=0.05)
    assert table.iloc[0]["unbatched_cost"] == pytest.approx(126_000.0, abs=1.0)
    assert table.iloc[0]["batched_cost"] == pytest.approx(106_500.0, abs=1.0)
    assert summary["BE"] == pytest.approx(0.1548, abs=0.0005)


def test_a_batch_of_one_well_saves_exactly_nothing():
    """PROVES: the two cost formulas agree at the boundary where they must.

    A batch containing a single well IS a single well visited on its own: one long
    haul, no local moves, the same plugging work. So the batched and unbatched costs
    have to come out identical, and the saving must be exactly zero - not nearly
    zero.

    WHAT THIS CATCHES: an off-by-one in the "(wells - 1) x local move" term. If that
    were written as "wells x local move", a lone well would be charged for a move it
    never makes, and every batch in the project would be overpriced by $1,500.
    """
    wells = pd.DataFrame({"lat": [52.0], "lon": [-113.0], "work_cost": [13_300.0]})
    table, _, _ = batching_efficiency(wells, Params(), by=None)

    row = table.iloc[0]
    assert row["wells"] == 1
    assert row["route_km"] == 0.0, "a single well has nowhere to drive"
    assert row["batched_cost"] == row["unbatched_cost"]


# --------------------------------------------------------------------------------
# 5. Spending the budget
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("budget", [0, 1, 25_000, 1_000_000, 10_000_000])
def test_spending_never_exceeds_the_budget(budget):
    """PROVES: the picker cannot overspend, at budgets from nothing to plenty.

    WHY IT MATTERS: this is the one promise a budgeting tool absolutely must keep. A
    plan that quietly costs more than the money available is worse than no plan.
    Several budgets are tried because the interesting failures are at the edges -
    nothing at all, and just under the price of a single item.
    """
    items = pd.DataFrame({
        "cost": [25_300.0, 41_094.0, 13_300.0, 59_798.0, 120_000.0],
        "value": [2.0, 4.0, 1.0, 3.0, 9.0],
    })
    chosen = greedy_fill(items, "cost", "value", budget)
    assert chosen["cost"].sum() <= budget


def test_a_zero_budget_closes_nothing_and_does_not_crash():
    """PROVES: the empty case is handled, end to end, on the real data.

    WHY IT MATTERS: zero is where code divides by zero, indexes into nothing, or
    calls `.sum()` on an empty column and gets a surprise. The app exposes a budget
    slider, so this path is reachable by a user, and it must produce an empty plan
    rather than a stack trace.
    """
    wells = load_wells(DATA_PATH)
    result, chosen, _ = plan(wells, 0.0)

    assert result["batched"]["wells"] == 0
    assert result["batched"]["spend"] == 0
    assert result["one_by_one"]["wells"] == 0
    assert len(chosen) == 0


def test_greedy_fill_is_not_optimal_and_this_is_known():
    """DOCUMENTS a known limitation, rather than guarding against a bug.

    `greedy_fill` takes the best value-per-dollar item first. That is an
    approximation to the knapsack problem, and it can be beaten.

    Here, on a $10,000 budget:
        D - 7 risk points for $6,000  -> best ratio, taken first
        E - 5 risk points for $5,000
        F - 5 risk points for $5,000
    After taking D, neither E nor F fits in the remaining $4,000, so greedy closes
    7 points. Taking E and F instead would have closed 10 for exactly $10,000.

    WHY TEST SOMETHING WE KNOW IS SUBOPTIMAL: so that the limitation stays visible
    and measured. If someone later swaps in a better algorithm, this test fails and
    tells them the behaviour changed deliberately rather than by accident.
    """
    items = pd.DataFrame({"name": ["D", "E", "F"],
                          "cost": [6_000.0, 5_000.0, 5_000.0],
                          "value": [7.0, 5.0, 5.0]})
    chosen = greedy_fill(items, "cost", "value", 10_000.0)

    assert chosen["name"].tolist() == ["D"]
    assert chosen["value"].sum() == 7.0

    # The better plan that greedy misses, confirmed to fit inside the same budget.
    better = items[items["name"].isin(["E", "F"])]
    assert better["cost"].sum() <= 10_000.0
    assert better["value"].sum() == 10.0


def test_depth_cost_tier_boundaries_are_inclusive():
    """PROVES: the depth tiers switch exactly where the comments say they do.

    The tiers are checked in order and the first one a depth fits inside wins, so
    the boundaries are inclusive: 600 m is in the first tier, 600.1 m is in the
    second. Also proves a missing depth falls back to the median rather than being
    treated as zero, which would price an unknown well at the cheapest tier.
    """
    assert depth_cost(600.0) == 13_300
    assert depth_cost(600.1) == 30_665
    assert depth_cost(1_200.0) == 30_665
    assert depth_cost(1_200.1) == 45_966
    assert depth_cost(2_000.0) == 45_966
    assert depth_cost(2_000.1) == 59_798

    # A missing depth uses the median of 1,121 m, which sits in the second tier.
    assert depth_cost(float("nan")) == 30_665


# --------------------------------------------------------------------------------
# 6. The processed data itself
# --------------------------------------------------------------------------------

def test_licence_numbers_keep_their_leading_zeros():
    """PROVES: licence numbers survive as text, with their leading zeros intact.

    WHY THIS IS THE QUIETEST FAILURE IN THE PROJECT: licence numbers look like
    numbers. Read as numbers, "0000007" becomes 7, and every join against the
    Petrinex files matches nothing. Because those joins are left joins, the row
    count stays at 76,943 either way - so a row-count check would not notice, and
    the output would be a full table of real company names with every depth and
    coordinate blank.

    AND THEY ARE NOT ALL NUMBERS ANYWAY: 227 licences contain letters, either as a
    suffix ("0050700D") or a prefix ("X0000281"). Those do not merely lose their
    zeros when read as numbers - they become missing outright. This was found by
    this very test failing, which is the sort of thing tests are for.

    The dtype is checked with `is_string_dtype` rather than `== object` because
    pandas 3 returns a dedicated StringDtype where pandas 2 returned object. Both
    are text; pinning the check to one spelling would make this test fail on a
    library upgrade while the code was still correct.
    """
    wells = load_wells(DATA_PATH)
    licences = wells["licence"]

    assert pd.api.types.is_string_dtype(licences), "licence stopped being text"
    assert isinstance(licences.iloc[0], str)

    # The leading zeros themselves.
    assert licences.str.startswith("0").sum() > 70_000
    assert licences.iloc[0] == "0000007"

    # And the licences that are not numeric at all, which only text can carry.
    has_a_letter = licences.str.contains(r"[A-Za-z]", regex=True)
    assert has_a_letter.sum() > 200, "the non-numeric licences have disappeared"


def test_stored_coordinates_match_the_wells_own_surface_location():
    """PROVES: the coordinates really were built from each well's own location.

    HOW: the AER workbook carries its own text description of where each well is, in
    the `surface_location` column ("7-21-57-21W4"). That came from a different file
    than the Petrinex grid columns the coordinates were computed from. Re-deriving a
    coordinate from `surface_location` and comparing gives an independent second
    opinion on every one of the 76,943 rows.

    WHAT THIS CATCHES that the leading-zero test does not: a join that lines up but
    lines up WRONG - rows shifted, a merge on the wrong key, a sort that reordered
    one side. Those would leave every coordinate populated and in Alberta, and only
    this comparison would show that they belong to the wrong wells.

    The threshold is 99.9% rather than 100% because 5 rows genuinely disagree
    between the two sources - 4 on legal subdivision, 2 on section - and those are
    source discrepancies, not join errors. Measured agreement is 99.9935%.
    """
    wells = pd.read_csv(DATA_PATH, dtype={"licence": str})

    parsed = wells["surface_location"].str.strip().str.extract(
        r"^(\d{1,2})-(\d{1,2})-(\d{1,3})-(\d{1,2})W(\d)$")
    parsed.columns = ["lsd", "sec", "twp", "rge", "mer"]
    assert parsed.notna().all(axis=1).all(), "some surface_location could not be read"

    as_number = {name: pd.to_numeric(parsed[name], errors="coerce").to_numpy()
                 for name in parsed.columns}
    lat, lon = dls_to_latlon(as_number["lsd"], as_number["sec"], as_number["twp"],
                             as_number["rge"], as_number["mer"])

    matches = ((np.abs(lat - wells["lat"].to_numpy()) < 1e-9)
               & (np.abs(lon - wells["lon"].to_numpy()) < 1e-9))
    assert matches.mean() >= 0.999, (
        f"only {matches.mean():.4%} of coordinates match the well's own "
        f"surface_location - the licence join may be wrong")


def test_check_meridians_rejects_a_meridian_it_cannot_convert():
    """PROVES: the guard in `check_meridians` refuses to guess.

    `MER_LON` knows meridians 4, 5 and 6. The Petrinex licence file also contains
    the value 0, and a well whose licence did not match the join has no meridian at
    all. Both would otherwise flow through as a blank coordinate - a well silently
    dropped off the map with no error raised.

    No inactive well currently needs an unmapped meridian, so this guard never fires
    on today's data. It exists so that a future data refresh fails loudly instead of
    producing a table that looks fine and is quietly wrong.
    """
    check_meridians(pd.Series([4.0, 5.0, 6.0]))        # the known ones: no complaint

    with pytest.raises(ValueError, match="Unmapped meridian"):
        check_meridians(pd.Series([4.0, 0.0]))

    with pytest.raises(ValueError, match="no meridian"):
        check_meridians(pd.Series([4.0, np.nan]))

    # And the mapping itself should hold only the three meridians Alberta uses.
    assert set(MER_LON) == {4, 5, 6}
