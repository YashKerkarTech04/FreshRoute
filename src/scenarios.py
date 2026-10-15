import numpy as np
import pandas as pd

try:  # works from app.py (package import) and from python src/scenarios.py
    from src.planner import (
        AT_RISK_STATUS, PLANNERS, arrival_hours, build_leftovers,
        create_plan_optimized, load_data, tier_caps,
    )
except ModuleNotFoundError:
    from planner import (
        AT_RISK_STATUS, PLANNERS, arrival_hours, build_leftovers,
        create_plan_optimized, load_data, tier_caps,
    )

SCENARIOS = {
    "Nominal": {},
    "Demand -20%": {"demand_mult": 0.8},
    "Demand +20%": {"demand_mult": 1.2},
    "Delay +1 h": {"delay_hours": 1},
    "Delay +2 h": {"delay_hours": 2},
    "Delay +6 h": {"delay_hours": 6},
    "Shelf life -20%": {"shelf_mult": 0.8},
    "Prices -10%": {"price_mult": 0.9},
    "Combined stress": {
        "demand_mult": 0.85, "delay_hours": 1,
        "shelf_mult": 0.85, "price_mult": 0.95,
    },
}


def apply_scenario(inventory, destinations, routes, demand_mult=1.0,
                   delay_hours=0.0, shelf_mult=1.0, price_mult=1.0):
    """Return perturbed copies of the three datasets."""
    inv = inventory.copy()
    dest = destinations.copy()
    rts = routes.copy()

    inv["shelf_life_hours"] = np.floor(
        inv["shelf_life_hours"] * shelf_mult
    ).astype(int)
    dest["demand"] = np.floor(dest["demand"] * demand_mult).astype(int)
    dest["selling_price"] = dest["selling_price"] * price_mult
    rts["travel_hours"] = rts["travel_hours"] + delay_hours
    return inv, dest, rts


def realized_outcome(plan, inventory, destinations, routes):
    """Evaluate a plan's shipments in a (possibly perturbed) world.

    Crates arriving after expiry, crates beyond realised demand, and depot
    stock that can no longer be sold are written off at purchase cost.
    Transport and handling are paid on every crate shipped.
    """
    route_lookup = routes.set_index("destination").to_dict("index")
    shelf = inventory.set_index("batch_id")["shelf_life_hours"].to_dict()
    cell_data = destinations.set_index(["destination", "commodity"])
    total_stock = int(inventory["quantity"].sum())
    total_demand = int(destinations["demand"].sum())

    sold_by_cell = {}
    remaining = {}
    revenue = transport = handling = product_cost = writeoff = 0.0
    in_transit = unsold_dest = 0

    if not plan.empty:
        ordered = plan.sort_values(
            ["destination", "commodity", "discount_pct", "dispatch_order"]
        )
        for r in ordered.itertuples(index=False):
            q = int(r.quantity)
            cell = (r.destination, r.commodity)
            transport += q * r.transport_cost_per_crate
            handling += q * r.handling_cost_per_crate

            if arrival_hours(route_lookup[r.destination]) > shelf[r.batch_id]:
                in_transit += q
                writeoff += q * r.unit_cost
                continue

            key = (cell, int(r.discount_pct))
            if key not in remaining:
                caps = tier_caps(cell_data.loc[cell, "demand"])
                remaining[key] = caps.get(int(r.discount_pct), 0)

            sell = min(q, remaining[key])
            remaining[key] -= sell
            unsold = q - sell
            unsold_dest += unsold
            writeoff += unsold * r.unit_cost

            price = cell_data.loc[cell, "selling_price"]
            revenue += sell * price * (1 - r.discount_pct / 100)
            product_cost += sell * r.unit_cost
            sold_by_cell[cell] = sold_by_cell.get(cell, 0) + sell

    # Stock still at the depot that can no longer be sold.
    left = build_leftovers(inventory, plan, routes)
    depot = left[left["status"] == AT_RISK_STATUS]
    unit_cost = inventory.set_index("batch_id")["unit_cost"]
    depot_crates = int(depot["unallocated_quantity"].sum())
    writeoff += float(
        (depot["unallocated_quantity"]
         * depot["batch_id"].map(unit_cost)).sum()
    )

    fulfilled = sum(
        min(sold, int(cell_data.loc[cell, "demand"]))
        for cell, sold in sold_by_cell.items()
    )
    spoiled = in_transit + unsold_dest + depot_crates
    net_revenue = revenue - transport - handling - writeoff

    return {
        "net_revenue": round(net_revenue, 2),
        "net_profit": round(net_revenue - product_cost, 2),
        "spoiled_crates": int(spoiled),
        "spoilage_rate_pct": round(spoiled / total_stock * 100, 1)
        if total_stock else 0.0,
        "demand_met_pct": round(fulfilled / total_demand * 100, 1)
        if total_demand else 0.0,
        "lost_in_transit": int(in_transit),
        "unsold_at_destination": int(unsold_dest),
        "expired_at_depot": depot_crates,
    }


def default_planners():
    """Planners compared in the robustness study."""
    planners = dict(PLANNERS)
    planners["Optimized + discounts + 6 h buffer"] = (
        lambda i, d, r: create_plan_optimized(i, d, r, buffer_hours=6)
    )
    return planners


def run_robustness(inventory, destinations, routes, planners=None,
                   scenarios=None):
    """Long table: scenario x planner x (fixed plan | replanned)."""
    planners = planners or default_planners()
    scenarios = scenarios or SCENARIOS

    nominal_plans = {
        name: fn(inventory, destinations, routes)[0]
        for name, fn in planners.items()
    }

    rows = []
    for scenario, params in scenarios.items():
        s_inv, s_dest, s_routes = apply_scenario(
            inventory, destinations, routes, **params
        )
        for name, fn in planners.items():
            fixed = realized_outcome(
                nominal_plans[name], s_inv, s_dest, s_routes
            )
            replan, _ = fn(s_inv, s_dest, s_routes)
            adapted = realized_outcome(replan, s_inv, s_dest, s_routes)

            rows.append({"scenario": scenario, "planner": name,
                         "mode": "Fixed plan", **fixed})
            rows.append({"scenario": scenario, "planner": name,
                         "mode": "Replanned", **adapted})

    return pd.DataFrame(rows)


def robustness_summary(results):
    """Average and worst case across scenarios, per planner and mode."""
    summary = results.groupby(["mode", "planner"], sort=False).agg(
        avg_net_profit=("net_profit", "mean"),
        worst_net_profit=("net_profit", "min"),
        avg_spoiled_crates=("spoiled_crates", "mean"),
        worst_spoiled_crates=("spoiled_crates", "max"),
        avg_demand_met_pct=("demand_met_pct", "mean"),
    ).round(1)
    return summary.reset_index()


if __name__ == "__main__":
    inventory, destinations, routes = load_data()
    results = run_robustness(inventory, destinations, routes)

    print("\n=== ROBUSTNESS SUMMARY ===")
    print(robustness_summary(results).to_string(index=False))

    print("\n=== FIXED PLAN: NET PROFIT BY SCENARIO ===")
    fixed = results[results["mode"] == "Fixed plan"]
    print(fixed.pivot(index="scenario", columns="planner",
                      values="net_profit").to_string())

    print("\n=== FIXED PLAN: CRATES SPOILED BY SCENARIO ===")
    print(fixed.pivot(index="scenario", columns="planner",
                      values="spoiled_crates").to_string())