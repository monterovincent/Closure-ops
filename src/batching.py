"""Batching efficiency model for well closure planning.

Mobilization costs are NOT published by the AER; the defaults below are
placeholders. Calibrate them so a typical batch lands in the 10-30% savings
range (AER says up to 40% for area-based closure).

Input columns: lat, lon, work_cost. Optional: a group column (default
"licensee_name", wells are only batched within one company) and risk_weight.

Imported as `from src.batching import Params`. The self-check demo at the bottom
runs from the project root with:
    python -m src.batching
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

R_EARTH_KM = 6371.0088


@dataclass
class Params:
    radius_km: float = 15.0        # modeling assumption, no regulatory basis found
    max_batch: int = 25            # assumed: wells one campaign can realistically cover
    m_out: float = 12_000.0        # assumed: one long-haul crew/rig mobilization per batch
    m_in_base: float = 1_500.0     # assumed: fixed cost of each local rig move
    m_in_per_km: float = 150.0     # assumed: local move cost per km


def to_xy_km(lat, lon) -> np.ndarray:
    """Flat km coordinates; fine for the short distances used here."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    x = R_EARTH_KM * np.radians(lon) * np.cos(np.radians(lat))
    y = R_EARTH_KM * np.radians(lat)
    return np.column_stack([x, y])


def _cluster_xy(xy: np.ndarray, radius_km: float, cap: int) -> np.ndarray:
    """Sweep west to east; each seed takes its nearest free wells within radius, up to cap."""
    n = len(xy)
    tree = cKDTree(xy)
    assigned = np.zeros(n, bool)
    labels = np.full(n, -1)
    k = 0
    for i in np.lexsort((xy[:, 1], xy[:, 0])):
        if assigned[i]:
            continue
        idx = np.array([j for j in tree.query_ball_point(xy[i], radius_km) if not assigned[j]])
        idx = idx[np.argsort(np.linalg.norm(xy[idx] - xy[i], axis=1))][:cap]
        labels[idx] = k
        assigned[idx] = True
        k += 1
    return labels


def cluster_wells(df: pd.DataFrame, p: Params, by: str | None = "licensee_name") -> pd.Series:
    xy = to_xy_km(df["lat"], df["lon"])
    labels = np.full(len(df), -1)
    offset = 0
    groups = [np.arange(len(df))] if not by or by not in df else \
        list(df.reset_index(drop=True).groupby(by).indices.values())
    for idx in groups:
        lab = _cluster_xy(xy[idx], p.radius_km, p.max_batch)
        labels[idx] = lab + offset
        offset += lab.max() + 1
    return pd.Series(labels, index=df.index, name="batch")


def local_route_km(xy: np.ndarray) -> float:
    """Nearest-neighbour path length through a batch (cheap proxy for the local moves)."""
    n = len(xy)
    if n < 2:
        return 0.0
    d = np.linalg.norm(xy[:, None] - xy[None, :], axis=2)
    visited, cur, total = {0}, 0, 0.0
    while len(visited) < n:
        nxt = min((j for j in range(n) if j not in visited), key=lambda j: d[cur, j])
        total += d[cur, nxt]
        visited.add(nxt)
        cur = nxt
    return float(total)


def batching_efficiency(df: pd.DataFrame, p: Params | None = None, by: str | None = "licensee_name"):
    """Return (per-batch table, summary, wells with batch labels).

    BE = 1 - cost_batched / cost_unbatched, where unbatched pays m_out for every well.
    """
    p = p or Params()
    df = df.reset_index(drop=True).copy()
    df["batch"] = cluster_wells(df, p, by).to_numpy()
    if "risk_weight" not in df:
        df["risk_weight"] = 1.0
    xy = to_xy_km(df["lat"], df["lon"])

    rows = []
    for b, idx in df.groupby("batch").indices.items():
        sub = df.iloc[idx]
        route = local_route_km(xy[idx])
        work = float(sub["work_cost"].sum())
        rows.append({
            "batch": b,
            "group": sub[by].iloc[0] if by and by in sub else "",
            "wells": len(sub),
            "lat": sub["lat"].mean(), "lon": sub["lon"].mean(),
            "route_km": round(route, 1),
            "work_cost": work,
            "batched_cost": p.m_out + (len(sub) - 1) * p.m_in_base + p.m_in_per_km * route + work,
            "unbatched_cost": work + p.m_out * len(sub),
            "risk_score": float(sub["risk_weight"].sum()),
        })
    table = pd.DataFrame(rows)
    unb, bat = table["unbatched_cost"].sum(), table["batched_cost"].sum()
    summary = {
        "wells": len(df), "batches": len(table),
        "unbatched_cost": round(unb), "batched_cost": round(bat),
        "saving": round(unb - bat), "BE": round(float(1 - bat / unb), 4),
    }
    return table, summary, df


if __name__ == "__main__":
    # Synthetic demo only: three dense areas plus scattered wells, central Alberta.
    rng = np.random.default_rng(7)
    centers = [(52.5, -113.8), (53.1, -114.9), (52.0, -112.9)]
    pts = []
    for lat0, lon0 in centers:
        pts += [(lat0 + rng.normal(0, 0.06), lon0 + rng.normal(0, 0.09)) for _ in range(25)]
    pts += [(rng.uniform(51.5, 54), rng.uniform(-116, -112)) for _ in range(15)]
    demo = pd.DataFrame(pts, columns=["lat", "lon"])
    demo["work_cost"] = rng.choice([13_300, 30_665, 45_966, 59_798], size=len(demo))
    _, s, _ = batching_efficiency(demo, by=None)
    print(s)
