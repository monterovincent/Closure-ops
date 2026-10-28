"""Budget picker: given a budget, choose the batches that close the most risk-weighted wells.

Run from the project root:
    python -m src.plan data/inactive_wells_joined.csv 50000000
Compares batched planning against closing wells one by one with the same budget.
"""
import sys

import numpy as np
import pandas as pd

from src.batching import Params, batching_efficiency

RISK_WEIGHT = {"High": 3.0, "Medium": 2.0, "Low": 1.0, "Not Reported": 1.0}

# PLACEHOLDER cost tiers by depth (m). Replace with the Directive 011 tables
# (region, depth, downhole scenario, add-on flags) for a real estimate.
DEPTH_TIERS = [(600, 13_300), (1_200, 30_665), (2_000, 45_966), (float("inf"), 59_798)]


def depth_cost(d: float) -> float:
    d = 1_121 if pd.isna(d) else d          # fall back to the median depth
    return next(c for lim, c in DEPTH_TIERS if d <= lim)


def load_wells(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"licence": str})
    df["work_cost"] = df["depth_m"].map(depth_cost)
    df["risk_weight"] = df["aer_deemed_risk_class"].map(RISK_WEIGHT).fillna(1.0)
    return df


def greedy_fill(items: pd.DataFrame, cost_col: str, value_col: str, budget: float) -> pd.DataFrame:
    """Best value per dollar first; skip items that no longer fit and keep going."""
    items = items.assign(ratio=items[value_col] / items[cost_col]).sort_values("ratio", ascending=False)
    spent, keep = 0.0, []
    for i, c in zip(items.index, items[cost_col]):
        if spent + c <= budget:
            keep.append(i)
            spent += c
    return items.loc[keep]


def plan(df: pd.DataFrame, budget: float, p: Params | None = None):
    p = p or Params()
    table, summary, wells = batching_efficiency(df, p)

    chosen = greedy_fill(table, "batched_cost", "risk_score", budget)
    singles = df.assign(cost=df["work_cost"] + p.m_out)
    solo = greedy_fill(singles, "cost", "risk_weight", budget)

    result = {
        "budget": budget,
        "batched": {"wells": int(chosen["wells"].sum()), "risk_closed": chosen["risk_score"].sum(),
                    "spend": round(chosen["batched_cost"].sum()), "batches": len(chosen)},
        "one_by_one": {"wells": len(solo), "risk_closed": solo["risk_weight"].sum(),
                       "spend": round(solo["cost"].sum())},
        "all_wells": summary,
    }
    return result, chosen, wells


if __name__ == "__main__":
    wells = load_wells(sys.argv[1])
    res, chosen, _ = plan(wells, float(sys.argv[2]))
    b, s = res["batched"], res["one_by_one"]
    print(f"Budget ${res['budget']:,.0f}")
    print(f"Batched:    {b['wells']:,} wells, risk {b['risk_closed']:,.0f}, spend ${b['spend']:,}, {b['batches']} batches")
    print(f"One by one: {s['wells']:,} wells, risk {s['risk_closed']:,.0f}, spend ${s['spend']:,}")
    print(f"Extra wells from batching: {b['wells'] - s['wells']:,}")
    print("All-wells BE:", res["all_wells"])
