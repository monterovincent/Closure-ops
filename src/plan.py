"""Stage 4: spend a budget, and show what grouping the work is worth.

WHAT THIS FILE DOES
Given a yearly closure budget, it answers the question the whole project exists for:
which wells should be closed first, and how many more can be closed if nearby wells
are grouped into shared crew trips instead of visited one at a time?

WHERE IT FITS - this is the last step before the app.

    data/inactive_wells_joined.csv
        -> load_wells()            adds a cost and a risk weight to every well
        -> src/batching.py         groups wells into trips and prices each group
        -> greedy_fill()           buys the best value-for-money groups until the
                                   budget runs out
        -> plan()                  does the same again, one well at a time, and
                                   returns both so they can be compared

THE COMPARISON IS THE POINT
Two plans are built from the same budget. The batched plan pays one long haul per
group of wells. The one-by-one plan pays a long haul for every single well. The gap
between them is the entire argument for area-based closure, and it is only as good
as the guessed haul cost - see the sensitivity table above `plan` below.

HOW TO RUN IT
From the project root, with the virtual environment active:
    python -m src.plan data/inactive_wells_joined.csv 50000000

WHAT TO BE HONEST ABOUT WHEREVER THESE NUMBERS APPEAR
  - The mobilization cost is a guess. It is not published by the AER.
  - The well costs are placeholder depth tiers, not the Directive 011 tables.
  - The risk weights are an invented 3/2/1 scale.
  - Coordinates are calculated from the legal land description, not surveyed.
Every output is therefore "under these assumptions", never a finding.
"""
import sys

import pandas as pd

from src.batching import Params, batching_efficiency

# --------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------

# ASSUMPTION: a simple priority scale, so that closing a high-risk well counts for
# more than closing a low-risk one when the budget is being spent.
# There is NO source behind these particular numbers. High is treated as worth three
# Low wells because the scale needs some shape, not because anything says it is.
# "Not Reported" is treated as Low rather than High, which is the optimistic
# reading - a well with no risk class recorded might be anything.
RISK_WEIGHT = {"High": 3.0, "Medium": 2.0, "Low": 1.0, "Not Reported": 1.0}

# PLACEHOLDER, not a sourced cost table. These stand in for the AER Directive 011
# abandonment cost tables, which price a well by region, depth, downhole scenario
# and a list of add-on events (groundwater protection, vent flow, gas migration).
# Replacing these four tiers with the real tables is listed as a Stage 8 upgrade.
# Read as: a well down to 600 m costs $13,300 to plug, and so on.
DEPTH_TIERS = [
    (600, 13_300),
    (1_200, 30_665),
    (2_000, 45_966),
    (float("inf"), 59_798),
]

# The depth used when a well's depth is unknown (122 wells, about 0.16% of the data).
# WHERE THIS NUMBER COMES FROM: it is the median `depth_m` of the joined table -
# that is, of inactive wells specifically. It is NOT the median of the raw
# infrastructure file, which is 1,015 m, because that file covers every well in
# Alberta rather than only the inactive ones. Using the median keeps an unknown well
# in the middle of the cost range instead of guessing it cheap or expensive.
# At 1,121 m this falls in the second tier, so an unknown well is costed at $30,665.
MEDIAN_DEPTH_M = 1_121


def depth_cost(depth_m: float) -> float:
    """Price the physical job of plugging one well, from its depth.

    depth_m: the well's total depth in metres, or NaN if it was never reported.
    Returns: the placeholder cost in dollars.

    WHY IT EXISTS: the budget cannot be spent without a price per well, and depth is
    the only cost driver available in the data. Deeper wells need more casing cut,
    more cement and more rig time, so depth is a reasonable first approximation -
    but only an approximation, since the real Directive 011 cost depends on region
    and on downhole complications this data does not describe.

    The tiers are checked in order and the first one the depth fits inside wins, so
    the boundaries are inclusive: a well of exactly 600 m costs $13,300, and one of
    600.1 m costs $30,665.
    """
    # WHY the substitution rather than returning 0 or skipping the well: a missing
    # depth is not a free well. Leaving it out of the plan would quietly shrink the
    # problem; pricing it at the median keeps it in the running at a fair cost.
    if pd.isna(depth_m):
        depth_m = MEDIAN_DEPTH_M

    return next(cost for tier_limit, cost in DEPTH_TIERS if depth_m <= tier_limit)


def load_wells(path: str) -> pd.DataFrame:
    """Read the processed well table and add the two columns the planner needs.

    path: path to data/inactive_wells_joined.csv, as built by src/build_dataset.py.
    Returns: the wells, plus `work_cost` (dollars) and `risk_weight` (the 3/2/1
             scale above).

    WHY IT EXISTS: the saved CSV describes wells, not decisions. The planner needs a
    price and a priority for each one, and keeping that in a single function means
    the app, the tests and the command line all load the data identically.
    """
    # QUIRK: `licence` must be read as text. Licence numbers carry leading zeros
    # ("0000007") and pandas would otherwise infer a number and silently drop them.
    # The same trap is explained at length in src/build_dataset.py.
    df = pd.read_csv(path, dtype={"licence": str})

    df["work_cost"] = df["depth_m"].map(depth_cost)

    # WHY fillna(1.0): a risk class outside the four known labels, or a blank one,
    # falls back to the lowest weight rather than crashing or being dropped. The
    # well still gets planned, it just does not get priority it has not earned.
    df["risk_weight"] = df["aer_deemed_risk_class"].map(RISK_WEIGHT).fillna(1.0)
    return df


def greedy_fill(items: pd.DataFrame, cost_col: str, value_col: str,
                budget: float) -> pd.DataFrame:
    """Buy the best value-for-money items until the budget will not stretch further.

    items:     the things that can be bought - either batches or single wells.
    cost_col:  name of the column holding each item's price in dollars.
    value_col: name of the column holding what the item is worth (risk closed).
    budget:    the money available, in dollars.
    Returns: the chosen rows, in the order they were bought (best value first), with
             an extra `ratio` column showing value per dollar.

    WHY IT EXISTS: this is the actual spending decision. Everything else in the
    project prepares the inputs for it.

    THE RULE IN PLAIN WORDS
    Work out how much risk each dollar buys. Sort everything from best to worst on
    that measure. Walk down the list and take anything that still fits in what is
    left of the budget, skipping what does not fit and carrying on to cheaper items.

    WORKED EXAMPLE with a $100,000 budget:
        batch A - closes 30 risk points for $60,000  -> 0.00050 per dollar
        batch B - closes 12 risk points for $20,000  -> 0.00060 per dollar
        batch C - closes 25 risk points for $50,000  -> 0.00050 per dollar
        Sorted: B (best), then A and C. Take B ($20,000 spent), take A ($80,000
        spent), skip C because $50,000 will not fit in the remaining $20,000.
        Result: 42 risk points closed for $80,000.

    THIS IS NOT GUARANTEED TO BE THE BEST ANSWER. It is the standard greedy
    approximation to the knapsack problem, which is NP-hard to solve exactly. Here
    is a small case where this rule genuinely loses, on a $10,000 budget:
        batch D - 7 risk points for $6,000  -> 0.00117 per dollar  (best ratio)
        batch E - 5 risk points for $5,000  -> 0.00100 per dollar
        batch F - 5 risk points for $5,000  -> 0.00100 per dollar
    Greedy takes D first because its ratio is best, and then neither E nor F fits in
    the $4,000 left, so it closes 7 risk points. Taking E and F instead would have
    closed 10 for exactly $10,000. The best-ratio item crowded out two that would
    have packed the budget perfectly.

    WHY THAT IS ACCEPTABLE HERE: the inputs are guesses. Chasing an exactly optimal
    plan built on an invented mobilization cost would be precision without accuracy,
    and it would make the app far too slow to move a slider. A fast, explainable
    rule that a planner can sanity-check by eye is worth more.

    WHY IT KEEPS GOING INSTEAD OF STOPPING at the first item that does not fit:
    stopping early would leave money unspent whenever one expensive batch sits near
    the top of the list. Continuing lets cheaper items mop up the remainder, which
    is both more realistic and closer to optimal.
    """
    # STEP 1: rank by value per dollar, best first. This is the whole heuristic.
    ranked = items.assign(ratio=items[value_col] / items[cost_col])
    ranked = ranked.sort_values("ratio", ascending=False)

    # STEP 2: walk the ranking and take whatever still fits.
    # WHY track `spent` rather than subtracting from `budget`: it keeps the original
    # budget available for the comparison and makes the running total easy to read.
    spent = 0.0
    chosen_rows = []
    for row_label, item_cost in zip(ranked.index, ranked[cost_col]):
        if spent + item_cost <= budget:
            chosen_rows.append(row_label)
            spent += item_cost

    # Returning .loc[chosen_rows] preserves best-value-first order, which is what the
    # app wants to display and what makes the plan readable.
    return ranked.loc[chosen_rows]


def plan(df: pd.DataFrame, budget: float, p: Params | None = None):
    """Build both plans from one budget and return them side by side.

    df:     wells from `load_wells`, with `work_cost` and `risk_weight`.
    budget: money available for the year, in dollars.
    p:      the batching Params; defaults are used if none are given.
    Returns a 3-tuple:
      result - a dict of the headline numbers for both plans, plus the all-wells
               summary from the batching model
      chosen - the batches actually bought, best value per dollar first
      wells  - every well with its batch label, so the app can map the selection

    WHY IT EXISTS: this is the function the app calls. It is the only place the two
    strategies are put on an equal footing - same wells, same costs, same budget,
    the only difference being whether travel is shared.

    HOW THE ONE-BY-ONE COMPARISON IS COSTED
    Each well pays its own work cost plus a full long-haul mobilization, which is
    exactly the `unbatched_cost` definition used in src/batching.py. Keeping the two
    definitions identical is what makes the comparison fair.

    SENSITIVITY - READ THIS BEFORE QUOTING ANY RESULT
    The answer depends mostly on `m_out`, the guessed mobilization cost. Measured on
    the real data at a $50M budget:

        m_out     batched   one-by-one   extra wells   extra %   all-wells BE
        $5,000      3,190        2,732           458    +16.8%          7.00%
        $12,000     3,109        1,976         1,133    +57.3%         18.93%
        $25,000     2,991        1,300         1,691   +130.1%         33.97%

    Tripling a number nobody published takes the headline benefit from +17% to
    +130%. That is the single most important caveat in the project and it belongs
    next to any figure shown to a reader.

    WHY RAISING THE HAUL COST HELPS BATCHING SO MUCH: the one-by-one plan pays that
    cost once per well, while the batched plan pays it once per trip. Making the
    haul dearer therefore punishes the one-by-one plan far harder - its well count
    collapses from 2,732 to 1,300 - while the batched count barely moves.
    """
    p = p or Params()

    # STEP 1: group the wells into trips and price every group both ways.
    batch_table, all_wells_summary, wells_with_batches = batching_efficiency(df, p)

    # STEP 2: spend the budget on whole batches, best risk-per-dollar first.
    # A batch is bought or not bought as a unit - you cannot send a crew to half a
    # trip - which is exactly the all-or-nothing choice greedy_fill assumes.
    chosen_batches = greedy_fill(batch_table, "batched_cost", "risk_score", budget)

    # STEP 3: spend the same budget again, this time one well at a time, so the two
    # strategies can be compared on equal terms.
    singles = df.assign(cost=df["work_cost"] + p.m_out)
    chosen_singles = greedy_fill(singles, "cost", "risk_weight", budget)

    result = {
        "budget": budget,
        "batched": {
            "wells": int(chosen_batches["wells"].sum()),
            "risk_closed": chosen_batches["risk_score"].sum(),
            "spend": round(chosen_batches["batched_cost"].sum()),
            "batches": len(chosen_batches),
        },
        "one_by_one": {
            # WHY len() here but a sum() above: each row of `chosen_singles` is one
            # well, whereas each row of `chosen_batches` is a group of them.
            "wells": len(chosen_singles),
            "risk_closed": chosen_singles["risk_weight"].sum(),
            "spend": round(chosen_singles["cost"].sum()),
        },
        "all_wells": all_wells_summary,
    }
    return result, chosen_batches, wells_with_batches


if __name__ == "__main__":
    # Thin command-line wrapper: read the CSV path and the budget, print both plans.
    # All the logic lives in the functions above so that the app and the tests can
    # call them directly.
    csv_path = sys.argv[1]
    budget = float(sys.argv[2])

    wells = load_wells(csv_path)
    result, chosen, _ = plan(wells, budget)
    batched, one_by_one = result["batched"], result["one_by_one"]

    print(f"Budget ${result['budget']:,.0f}")
    print(f"Batched:    {batched['wells']:,} wells, risk {batched['risk_closed']:,.0f}, "
          f"spend ${batched['spend']:,}, {batched['batches']} batches")
    print(f"One by one: {one_by_one['wells']:,} wells, risk {one_by_one['risk_closed']:,.0f}, "
          f"spend ${one_by_one['spend']:,}")
    print(f"Extra wells from batching: {batched['wells'] - one_by_one['wells']:,}")
    print("All-wells BE:", result["all_wells"])
