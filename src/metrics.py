import pandas as pd

AT_RISK_STATUS = "At risk - will spoil"
CARRY_STATUS = "Carry forward"


def _shipped(plan, keys):
    """Crates shipped, grouped by the given columns."""
    if plan.empty:
        return pd.DataFrame(columns=keys + ["shipped"])
    return (
        plan.groupby(keys, as_index=False)["quantity"].sum()
        .rename(columns={"quantity": "shipped"})
    )


def route_utilization(plan, routes):
    """Share of each route's capacity that the plan uses."""
    out = routes[["destination", "capacity"]].merge(
        _shipped(plan, ["destination"]), on="destination", how="left"
    )
    out["shipped"] = out["shipped"].fillna(0).astype(int)
    out["utilization_pct"] = (
        out["shipped"] / out["capacity"] * 100
    ).round(1)
    return out


def demand_fulfilment(plan, destinations):
    """Demand met per destination and commodity.

    fulfilled counts crates up to normal demand. Crates sold beyond it
    (extra buyers unlocked by a discount) are reported as extra_via_discount.
    """
    keys = ["destination", "commodity"]
    out = destinations[keys + ["demand"]].merge(
        _shipped(plan, keys), on=keys, how="left"
    )
    out["shipped"] = out["shipped"].fillna(0).astype(int)
    out["fulfilled"] = out[["shipped", "demand"]].min(axis=1)
    out["extra_via_discount"] = (out["shipped"] - out["demand"]).clip(lower=0)
    out["unmet"] = out["demand"] - out["fulfilled"]
    out["fulfilment_pct"] = (
        out["fulfilled"] / out["demand"] * 100
    ).round(1)
    return out


def commodity_summary(plan, inventory):
    """Stock, crates shipped and profit per commodity."""
    stock = inventory.groupby("commodity", as_index=False)["quantity"].sum()
    stock = stock.rename(columns={"quantity": "stock"})

    if plan.empty:
        stock["shipped"] = 0
        stock["profit"] = 0.0
        return stock

    grouped = plan.groupby("commodity", as_index=False).agg(
        shipped=("quantity", "sum"),
        profit=("estimated_profit", "sum"),
    )
    out = stock.merge(grouped, on="commodity", how="left").fillna(0)
    out["shipped"] = out["shipped"].astype(int)
    out["profit"] = out["profit"].round(2)
    return out


def plan_metrics(plan, leftovers, inventory, destinations):
    """Headline KPIs for one plan under the shared cost model."""
    total_stock = int(inventory["quantity"].sum())
    total_demand = int(destinations["demand"].sum())

    cells = demand_fulfilment(plan, destinations)
    fulfilled = int(cells["fulfilled"].sum())

    by_dest = cells.groupby("destination")[["fulfilled", "demand"]].sum()
    dest_pct = by_dest["fulfilled"] / by_dest["demand"] * 100
    spread = float(dest_pct.max() - dest_pct.min())

    if plan.empty:
        shipped = discounted = rescued = 0
        revenue = list_revenue = transport = handling = product_cost = 0.0
        freshness = 0.0
    else:
        qty = plan["quantity"]
        shipped = int(qty.sum())
        revenue = float((qty * plan["selling_price"]).sum())
        list_revenue = float((qty * plan["list_price"]).sum())
        transport = float((qty * plan["transport_cost_per_crate"]).sum())
        handling = float((qty * plan["handling_cost_per_crate"]).sum())
        product_cost = float((qty * plan["unit_cost"]).sum())
        discounted = int(qty[plan["discount_pct"] > 0].sum())
        rescued = int(qty[plan["perishable"]].sum())

        # Average share of shelf life still left when crates arrive.
        remaining = (
            (plan["shelf_life_hours"] - plan["arrival_hours"])
            / plan["shelf_life_hours"]
        )
        freshness = float((remaining * qty).sum() / qty.sum() * 100)

    # Stock that expires unsold, and what it cost to buy.
    at_risk = leftovers[leftovers["status"] == AT_RISK_STATUS]
    unit_cost = inventory.set_index("batch_id")["unit_cost"]
    spoiled = int(at_risk["unallocated_quantity"].sum())
    spoilage_loss = float(
        (at_risk["unallocated_quantity"]
         * at_risk["batch_id"].map(unit_cost)).sum()
    )
    carried = int(
        leftovers.loc[
            leftovers["status"] == CARRY_STATUS, "unallocated_quantity"
        ].sum()
    )

    net_revenue = revenue - transport - handling - spoilage_loss
    net_profit = net_revenue - product_cost

    return {
        "net_revenue": round(net_revenue, 2),
        "net_profit": round(net_profit, 2),
        "revenue": round(revenue, 2),
        "discount_cost": round(list_revenue - revenue, 2),
        "transport_cost": round(transport, 2),
        "handling_cost": round(handling, 2),
        "spoilage_loss": round(spoilage_loss, 2),
        "product_cost": round(product_cost, 2),
        "crates_shipped": shipped,
        "crates_spoiling": spoiled,
        "spoilage_rate_pct": round(spoiled / total_stock * 100, 1)
        if total_stock else 0.0,
        "crates_carried_forward": carried,
        "demand_met_pct": round(fulfilled / total_demand * 100, 1)
        if total_demand else 0.0,
        "unmet_demand_crates": total_demand - fulfilled,
        "fulfilment_spread_pct": round(spread, 1),
        "discounted_crates": discounted,
        "perishable_crates_rescued": rescued,
        "avg_freshness_at_arrival_pct": round(freshness, 1),
        "stock_used_pct": round(shipped / total_stock * 100, 1)
        if total_stock else 0.0,
    }


METRIC_LABELS = {
    "net_revenue": "Net revenue (₹)",
    "net_profit": "Net profit after product cost (₹)",
    "revenue": "Revenue after discounts (₹)",
    "discount_cost": "Discount given (₹)",
    "transport_cost": "Transport cost (₹)",
    "handling_cost": "Handling cost (₹)",
    "spoilage_loss": "Spoilage write-off (₹)",
    "product_cost": "Product cost of crates sold (₹)",
    "crates_shipped": "Crates shipped",
    "crates_spoiling": "Crates expiring unsold",
    "spoilage_rate_pct": "Spoilage rate (% of stock)",
    "crates_carried_forward": "Crates carried forward",
    "demand_met_pct": "Demand met (%)",
    "unmet_demand_crates": "Unmet demand (crates)",
    "fulfilment_spread_pct": "Fulfilment spread across cities (pts)",
    "discounted_crates": "Crates sold at a discount",
    "perishable_crates_rescued": "Perishable crates rescued",
    "avg_freshness_at_arrival_pct": "Avg shelf life left on arrival (%)",
    "stock_used_pct": "Stock used (%)",
}


def compare_metrics(results):
    """Full table: one row per metric, one column per planner.

    results: {planner name: plan_metrics() dict}
    """
    rows = []
    for key, label in METRIC_LABELS.items():
        row = {"Metric": label}
        for name, metrics in results.items():
            row[name] = metrics[key]
        rows.append(row)
    return pd.DataFrame(rows)


def headline_table(results):
    """The three judged outcomes, one row per planner."""
    rows = []
    for name, m in results.items():
        rows.append({
            "Planner": name,
            "Crates expiring unsold": m["crates_spoiling"],
            "Spoilage rate (%)": m["spoilage_rate_pct"],
            "Demand met (%)": m["demand_met_pct"],
            "Net revenue (₹)": m["net_revenue"],
            "Net profit (₹)": m["net_profit"],
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    # Run as: python src/metrics.py
    from planner import PLANNERS, load_data

    inventory, destinations, routes = load_data()

    results, plans = {}, {}
    for name, fn in PLANNERS.items():
        plan, left = fn(inventory, destinations, routes)
        plans[name] = plan
        results[name] = plan_metrics(plan, left, inventory, destinations)

    print("\n=== HEADLINE COMPARISON ===")
    print(headline_table(results).to_string(index=False))

    print("\n=== FULL METRICS ===")
    print(compare_metrics(results).to_string(index=False))

    best = "Optimized + discounts"
    print(f"\n=== ROUTE UTILIZATION ({best}) ===")
    print(route_utilization(plans[best], routes).to_string(index=False))

    print(f"\n=== COMMODITY SUMMARY ({best}) ===")
    print(commodity_summary(plans[best], inventory).to_string(index=False))