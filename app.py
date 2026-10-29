"""Stage 5: the ClosureOps web app.

WHAT THIS FILE DOES
Puts the model behind a handful of sliders so somebody who has never seen the code
can ask "what does this budget buy?" and get an answer in a second.

WHERE IT FITS - this is the top of the stack and the only file a user ever sees.

    data/inactive_wells_joined.csv
        -> src/plan.py: load_wells()      price and risk-weight every well
        -> src/batching.py                group wells into trips, price each group
        -> src/plan.py: greedy_fill()     spend the budget on the best groups
        -> THIS FILE                      sliders, metrics, map, downloadable table

HOW TO RUN IT
From the project root, with the virtual environment active:
    streamlit run app.py

HOW THE CACHING WORKS, AND WHY IT IS SPLIT THE WAY IT IS
The expensive part is clustering and pricing every well in Alberta. That depends on
only three of the five controls - the radius, the batch cap and the mobilization
cost. It does NOT depend on the budget or the company filter.

So the work is split in two:
  1. `cluster_and_price` does the heavy lifting and is cached on those three values.
  2. Everything after it - filtering to one company, then spending the budget - is
     cheap arithmetic that runs fresh on every interaction.

The practical effect is that dragging the budget slider, which is what a reader will
actually do, never re-runs the model at all.

EVERY NUMBER ON SCREEN RESTS ON ASSUMPTIONS
The mobilization cost is a guess, the well costs are placeholder depth tiers, and
the coordinates are calculated from legal land descriptions rather than surveyed.
The banner and the footer say so, and nothing here should be read as a finding.
"""
import pandas as pd
import pydeck as pdk
import streamlit as st

from src.batching import Params, batching_efficiency
from src.plan import greedy_fill, load_wells

# --------------------------------------------------------------------------------
# Constants for the controls. Defaults match src/batching.py, so opening the app
# with nothing touched reproduces the numbers in docs/CODE_TOUR.md.
# --------------------------------------------------------------------------------

DATA_PATH = "data/inactive_wells_joined.csv"

# Budget range, expressed in MILLIONS of dollars.
# WHY millions rather than raw dollars: Streamlit's slider format string is
# printf-style and cannot insert thousands separators, so a raw budget renders as
# the unreadable "$50000000". Sliding in millions gives "$50M" instead, and the
# value is multiplied back up to dollars immediately after.
# The lower end is small enough to show a near-empty plan without breaking; the
# upper end is the industry-wide closure spend quota of $750M a year for 2025-2027.
# SOURCE for the upper figure: AER closure quota - note it covers more than the
# abandonment work modelled here.
BUDGET_MIN_M, BUDGET_MAX_M, BUDGET_DEFAULT_M, BUDGET_STEP_M = 5, 750, 50, 5
DOLLARS_PER_MILLION = 1_000_000

# ASSUMPTION, all of these: see Params in src/batching.py. None are published.
MOBILIZATION_MIN, MOBILIZATION_MAX, MOBILIZATION_DEFAULT, MOBILIZATION_STEP = 0, 40_000, 12_000, 500
RADIUS_MIN, RADIUS_MAX, RADIUS_DEFAULT = 5, 30, 15
BATCH_CAP_MIN, BATCH_CAP_MAX, BATCH_CAP_DEFAULT = 5, 50, 25

ALL_COMPANIES = "All companies"

# Map styling: metres of dot radius per SQUARE ROOT of one well.
#
# WHY THE SQUARE ROOT, which looks like a strange thing to do: a circle's area grows
# with the square of its radius. If the radius were set straight from the well count
# (radius = wells x scale), then a 25-well trip would be drawn with 25 x 25 = 625
# times the area of a 1-well trip, not 25 times. People judge circles by area, so
# that encoding exaggerates big batches enormously. Taking the square root first
# means area ends up proportional to the well count, which is what a reader
# actually perceives. This is the standard rule for sizing circles by quantity.
#
# The scale factor itself is cosmetic, tuned so a 1-well trip is about 2.4 km across
# and a 25-well trip about 12 km - visible at provincial zoom without swamping the
# map once several hundred trips are selected.
DOT_RADIUS_PER_SQRT_WELL = 2_400


@st.cache_data(show_spinner=False)
def load_data(path: str) -> pd.DataFrame:
    """Read the well table once per session and keep it in memory.

    path: location of the processed CSV.
    Returns: every inactive well, priced and risk-weighted.

    WHY CACHED: the file is about 11 MB and roughly 77,000 rows. Re-reading it on
    every slider move would dominate the runtime for no reason - the data never
    changes while the app is open.
    """
    return load_wells(path)


@st.cache_data(show_spinner=False)
def cluster_and_price(path: str, radius_km: float, max_batch: int,
                      mobilization_cost: float):
    """Group every well into trips and price each group. The expensive step.

    path:              location of the processed CSV.
    radius_km:         how far apart two wells can be and still share a trip.
    max_batch:         the most wells allowed in one trip.
    mobilization_cost: the guessed long-haul cost per trip, in dollars.
    Returns: (batch table, all-wells summary, wells with their batch labels).

    WHY CACHED ON EXACTLY THESE THREE: they are the only inputs that change the
    answer. The budget decides what gets *bought* out of this table, and the company
    filter decides what gets *shown*, but neither changes how wells are grouped or
    what a group costs. Keying the cache on just these three means the budget slider
    and the company dropdown never trigger a recalculation.

    WHY THE WHOLE PROVINCE IS CLUSTERED EVEN WHEN ONE COMPANY IS SELECTED: the
    projection in `to_xy_km` takes its reference latitude from the wells handed to
    it. Clustering a single company first would give that company a different
    reference latitude, and therefore slightly different batches, than it has under
    "All companies". Clustering everything once and filtering afterwards keeps the
    two views consistent - a company's batches look the same either way.
    """
    wells = load_data(path)
    params = Params(radius_km=float(radius_km),
                    max_batch=int(max_batch),
                    m_out=float(mobilization_cost))
    return batching_efficiency(wells, params)


def build_plan(batch_table: pd.DataFrame, wells: pd.DataFrame,
               budget: float, mobilization_cost: float, company: str):
    """Spend the budget two ways on whatever the filter has left in scope.

    batch_table:       every batch, priced, from `cluster_and_price`.
    wells:             every well with its batch label.
    budget:            money available, in dollars.
    mobilization_cost: the per-trip haul cost, needed to price single wells.
    company:           a licensee name, or ALL_COMPANIES for no filter.
    Returns: (chosen batches, chosen single wells, wells in scope).

    WHY IT EXISTS: this is the cheap half of the work, deliberately kept out of the
    cache so that moving the budget slider feels instant.

    The filter is applied BEFORE the budget is spent, which is the honest reading of
    the question "what can this company do with this money" - the budget belongs to
    whoever is selected, and should not be spent on anyone else's wells.
    """
    if company != ALL_COMPANIES:
        batch_table = batch_table[batch_table["group"] == company]
        wells = wells[wells["licensee_name"] == company]

    # The batched plan: buy whole trips, best risk-per-dollar first. A batch is
    # all-or-nothing because you cannot send a crew to half a trip.
    chosen_batches = greedy_fill(batch_table, "batched_cost", "risk_score", budget)

    # The comparison plan: same budget, same wells, but every well pays its own
    # full mobilization. This matches `unbatched_cost` in src/batching.py exactly,
    # which is what makes the two figures comparable.
    singles = wells.assign(cost=wells["work_cost"] + mobilization_cost)
    chosen_singles = greedy_fill(singles, "cost", "risk_weight", budget)

    return chosen_batches, chosen_singles, wells


# --------------------------------------------------------------------------------
# Page layout
# --------------------------------------------------------------------------------

st.set_page_config(page_title="ClosureOps", page_icon="🛢️", layout="wide")

st.title("ClosureOps")
st.markdown(
    "**Alberta has about 77,000 inactive oil and gas wells.** Closing them costs "
    "money that arrives one budget at a time. This tool picks which wells to close "
    "first, and shows what grouping nearby wells into shared crew trips is worth."
)

# The honesty banner, always visible. Required by the project's own rules: a reader
# must not be able to see a saving figure without seeing what it rests on.
st.warning(
    "**Read this before the numbers.** The mobilization cost is a **placeholder** - "
    "the AER does not publish it, so the headline saving moves enormously with it "
    "(see *How this works*). Well costs are **stand-in depth tiers**, not the "
    "Directive 011 tables. Map positions are **calculated** from legal land "
    "descriptions, not surveyed, and are off by roughly 3 to 11 km. Every figure "
    "below is \"under these assumptions\", not a finding.",
    icon="⚠️",
)

# --- Sidebar controls ---------------------------------------------------------
# Each slider writes straight into the model. The two cheap ones (budget, company)
# are deliberately separated from the three expensive ones in the caching above.
st.sidebar.header("Controls")

budget_millions = st.sidebar.slider(
    "Yearly budget", min_value=BUDGET_MIN_M, max_value=BUDGET_MAX_M,
    value=BUDGET_DEFAULT_M, step=BUDGET_STEP_M, format="$%dM",
    help="Money available for closure work this year. Moving this is instant - it "
         "only changes what gets bought, not how wells are grouped.",
)
# Back to dollars straight away, so every calculation below works in real money.
budget = budget_millions * DOLLARS_PER_MILLION

mobilization_cost = st.sidebar.slider(
    "Mobilization cost per trip", min_value=MOBILIZATION_MIN, max_value=MOBILIZATION_MAX,
    value=MOBILIZATION_DEFAULT, step=MOBILIZATION_STEP, format="$%d",
    help="The guessed cost of hauling a crew and rig out to a site. NOT a published "
         "figure. This is the number the whole result hinges on.",
)

radius_km = st.sidebar.slider(
    "Batch radius (km)", min_value=RADIUS_MIN, max_value=RADIUS_MAX,
    value=RADIUS_DEFAULT,
    help="How far apart two wells can be and still share a trip. A modelling "
         "assumption with no regulatory basis.",
)

max_batch = st.sidebar.slider(
    "Max wells per batch", min_value=BATCH_CAP_MIN, max_value=BATCH_CAP_MAX,
    value=BATCH_CAP_DEFAULT,
    help="The most wells one campaign can realistically cover. Without a cap the "
         "model produces batches of nearly 1,000 wells, which no crew could do.",
)

# The company list comes from the data rather than being hard-coded, so it stays
# correct when the dataset is rebuilt.
all_wells = load_data(DATA_PATH)
company_names = sorted(all_wells["licensee_name"].dropna().unique().tolist())
company = st.sidebar.selectbox(
    "Company", [ALL_COMPANIES] + company_names,
    help="Restrict the plan to one licensee. The budget is then spent only on that "
         "company's wells.",
)

# --- Run the model ------------------------------------------------------------
# The spinner covers the cached call. On a cache hit it never appears.
with st.spinner("Grouping wells and pricing trips..."):
    batch_table, all_wells_summary, wells_with_batches = cluster_and_price(
        DATA_PATH, radius_km, max_batch, mobilization_cost)

chosen_batches, chosen_singles, wells_in_scope = build_plan(
    batch_table, wells_with_batches, budget, mobilization_cost, company)

# --- The four headline metrics ------------------------------------------------
# How each one is computed:
#   wells_batched  - every well inside the batches the budget could afford. Summed
#                    from the batch table, because one row is a whole trip.
#   wells_one_by_one - how many individual wells the same budget buys when each one
#                    pays its own full mobilization. One row is one well, so count.
#   extra_wells    - the difference. This is the headline claim of the project.
#   plan_saving    - 1 - (what the chosen batches cost) / (what those same wells
#                    would cost visited separately). Measured on the chosen plan,
#                    not on all of Alberta, so it answers "what did batching save
#                    me on the work I can actually afford?"
wells_batched = int(chosen_batches["wells"].sum()) if len(chosen_batches) else 0
wells_one_by_one = len(chosen_singles)
extra_wells = wells_batched - wells_one_by_one

batched_spend = float(chosen_batches["batched_cost"].sum()) if len(chosen_batches) else 0.0
same_wells_unbatched = float(chosen_batches["unbatched_cost"].sum()) if len(chosen_batches) else 0.0
# WHY the guard: with no batches chosen there is nothing to divide by.
plan_saving = (1 - batched_spend / same_wells_unbatched) if same_wells_unbatched > 0 else 0.0

left, middle_left, middle_right, right = st.columns(4)
left.metric("Wells closed, batched", f"{wells_batched:,}")
middle_left.metric("Wells closed, one at a time", f"{wells_one_by_one:,}")
middle_right.metric(
    "Extra wells from batching", f"{extra_wells:,}",
    delta=f"{100 * extra_wells / wells_one_by_one:+.0f}%" if wells_one_by_one else None,
)
right.metric("Saving on this plan", f"{plan_saving:.1%}")

# WHY the backslashes before every dollar sign in markdown: Streamlit renders
# captions as markdown, and markdown treats a $...$ pair as inline LaTeX maths. An
# unescaped "$49,985,099 of a $50,000,000" silently loses BOTH dollar signs and
# renders the text between them as a formula. Escaping keeps them literal.
st.caption(
    f"Spending \\${batched_spend:,.0f} of a \\${budget:,.0f} budget across "
    f"{len(chosen_batches):,} trips. Across all {all_wells_summary['wells']:,} "
    f"inactive wells the model's overall batching efficiency is "
    f"{all_wells_summary['BE']:.1%}."
)

# --- Map ----------------------------------------------------------------------
st.subheader("Where the work is")
st.caption(
    "One dot per selected trip, placed at the average position of its wells and "
    "sized by how many it covers. Positions are calculated from legal land "
    "descriptions, so treat them as approximate - they are not survey coordinates."
)

if len(chosen_batches) == 0:
    # Budget too small to buy even one trip. Say so rather than drawing an empty map.
    st.info("This budget does not stretch to a single trip. Try a larger budget.")
else:
    map_data = chosen_batches.assign(
        # Pre-format the tooltip fields: pydeck tooltips cannot format numbers.
        wells_label=chosen_batches["wells"].map("{:,}".format),
        cost_label=chosen_batches["batched_cost"].map("${:,.0f}".format),
        saving_label=(chosen_batches["unbatched_cost"]
                      - chosen_batches["batched_cost"]).map("${:,.0f}".format),
        # ** 0.5 is the square root - see DOT_RADIUS_PER_SQRT_WELL for why.
        dot_radius=(chosen_batches["wells"] ** 0.5) * DOT_RADIUS_PER_SQRT_WELL,
    )
    st.pydeck_chart(pdk.Deck(
        map_style=None,
        initial_view_state=pdk.ViewState(
            latitude=float(map_data["lat"].mean()),
            longitude=float(map_data["lon"].mean()),
            zoom=4.5,
        ),
        layers=[pdk.Layer(
            "ScatterplotLayer",
            data=map_data,
            get_position="[lon, lat]",
            get_radius="dot_radius",
            # WHY these pixel limits: at a large budget several hundred trips are
            # drawn at once in the same few dense parts of Alberta. Capping the
            # radius stops the biggest dots swallowing their neighbours, and the
            # floor keeps a single-well trip from vanishing entirely when zoomed out.
            radius_min_pixels=2,
            radius_max_pixels=22,
            # WHY the low alpha and the outline: overlapping trips are the normal
            # case, not the exception. A translucent fill lets dense areas read as
            # darker rather than as one solid shape, and the outline keeps
            # individual trips distinguishable where they overlap.
            get_fill_color=[200, 70, 40, 105],
            stroked=True,
            get_line_color=[140, 35, 15, 170],
            line_width_min_pixels=1,
            pickable=True,
        )],
        tooltip={"html": "<b>{group}</b><br/>{wells_label} wells<br/>"
                         "Cost {cost_label}<br/>Saves {saving_label}"},
    ))

# --- The plan table -----------------------------------------------------------
st.subheader("The plan")

if len(chosen_batches) == 0:
    st.info("Nothing selected at this budget.")
else:
    # Already sorted best risk-per-dollar first, because that is the order
    # greedy_fill bought them in.
    plan_table = pd.DataFrame({
        "Company": chosen_batches["group"],
        "Wells": chosen_batches["wells"],
        "Work cost": chosen_batches["work_cost"].round(0),
        "Trip cost": chosen_batches["batched_cost"].round(0),
        "Saving vs one at a time": (chosen_batches["unbatched_cost"]
                                    - chosen_batches["batched_cost"]).round(0),
        "Risk closed": chosen_batches["risk_score"].round(1),
        "Risk per $1M": (chosen_batches["ratio"] * 1_000_000).round(2),
    })
    st.dataframe(plan_table, width="stretch", hide_index=True)

    st.download_button(
        "Download this plan (CSV)",
        data=plan_table.to_csv(index=False).encode("utf-8"),
        file_name=f"closureops_plan_{int(budget)}.csv",
        mime="text/csv",
        help="The selected trips, in the order the model would do them.",
    )

# --- Explanation --------------------------------------------------------------
with st.expander("How this works"):
    st.markdown(
        f"""
**The idea.** Plugging a well needs a crew and a rig hauled to the site. That haul
is paid per *trip*, not per well. If several wells sit close together, one trip
covers them all: the long haul is paid once and the rig makes short local moves
instead of driving back and forth.

**How wells are grouped.** Wells are grouped only within a single company, because a
licensee is responsible for its own wells and two neighbouring wells owned by rivals
will never share a crew. Within a company, the model sweeps west to east; each
unclaimed well starts a trip and takes its nearest free neighbours within
{radius_km} km, up to {max_batch} wells.

**What a trip costs.**

- *Batched:* one mobilization + a fixed charge per local move between wells
  + a per-kilometre driving charge + the plugging work for every well.
- *One at a time:* the plugging work for every well + a full mobilization each.

**Which trips get bought.** Each trip is scored on risk closed per dollar, and they
are bought best-first until the budget will not stretch to the next one. This is a
fast approximation, not a guaranteed-optimal plan.

**The caveat that matters most.** The mobilization cost is a guess. At a \\$50M
budget the extra wells batching buys goes from **+17%** at \\$5,000 per trip, to
**+57%** at \\$12,000, to **+130%** at \\$25,000. The *direction* is robust -
grouping always helps - but the size of the benefit is only as good as that one
number.

**If you drag mobilization to \\$0** the saving goes *negative* (about -11%). That is
correct, not a bug: with no long haul to share, batching only adds the cost of local
moves between wells. The saving exists because the haul is expensive.

**Risk weighting.** High-risk wells count 3, Medium 2, Low and Not Reported 1. That
scale is invented for this tool; nothing official says a High well is worth exactly
three Low ones.
"""
    )

# --- Footer -------------------------------------------------------------------
# Attribution is mandatory: the AER permits non-commercial reproduction provided the
# source is named and the work is not presented as official or endorsed.
st.divider()
st.caption(
    "Data: Alberta Energy Regulator and Petrinex public data. Unofficial project, "
    "not endorsed by the AER. Latitude and longitude are calculated by this project "
    "from legal land descriptions and are not AER data. A screening and "
    "demonstration tool - not a basis for real spending decisions."
)
