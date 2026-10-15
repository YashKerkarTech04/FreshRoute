from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

CYCLE_HOURS = 24
SAFETY_BUFFER_HOURS = 0.0
DISCOUNT_TIERS = (0.10, 0.20, 0.30)
DEMAND_ELASTICITY = 1.5
DISCOUNT_ELIGIBLE_HOURS = 48

AT_RISK_STATUS = "At risk - will spoil"
CARRY_STATUS = "Carry forward"
DONE_STATUS = "Fully allocated"

def _read_csv(path):
    """Read a CSV that may have been re-saved by Excel or Notepad.

    Handles a UTF-8 BOM, UTF-16, semicolon or tab delimiters, and stray
    spaces around column names.
    """
    last_error = None
    for encoding in ("utf-8-sig", "utf-16"):
        for sep in (",", None):  # None = sniff the delimiter
            try:
                extra = {"sep": None, "engine": "python"} if sep is None else {}
                df = pd.read_csv(
                    path, encoding=encoding, skipinitialspace=True, **extra
                )
            except (UnicodeError, pd.errors.ParserError) as exc:
                last_error = exc
                continue
            df.columns = df.columns.astype(str).str.strip()
            if df.shape[1] > 1:
                return df
    raise ValueError(f"Could not read {path} as a CSV table ({last_error})")


def _require(df, filename, columns):
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"{filename} is missing columns {missing}. "
            f"Columns found: {list(df.columns)}"
        )


def load_data():
    """Load the three synthetic datasets."""
    inventory = _read_csv(DATA_DIR / "inventory.csv")
    destinations = _read_csv(DATA_DIR / "destinations.csv")
    routes = _read_csv(DATA_DIR / "routes.csv")

    _require(inventory, "inventory.csv", [
        "batch_id", "commodity", "quantity", "shelf_life_hours", "unit_cost",
    ])
    _require(destinations, "destinations.csv", [
        "destination", "commodity", "demand", "selling_price",
    ])
    _require(routes, "routes.csv", [
        "destination", "travel_hours", "transport_cost_per_crate",
        "handling_hours", "capacity",
    ])

    # Older datasets have no handling cost; treat it as zero.
    if "handling_cost_per_crate" not in routes.columns:
        routes["handling_cost_per_crate"] = 0.0

    return inventory, destinations, routes


def _buffer(buffer_hours):
    return SAFETY_BUFFER_HOURS if buffer_hours is None else float(buffer_hours)


def arrival_hours(route):
    """Travel plus handling time for one route (a dict-like row)."""
    return float(route["travel_hours"]) + float(route["handling_hours"])


def min_arrival_hours(routes):
    return float((routes["travel_hours"] + routes["handling_hours"]).min())


def expires_unsold(shelf_life_hours, routes):
    """True if stock left at the depot cannot reach any market next cycle."""
    return shelf_life_hours - CYCLE_HOURS < min_arrival_hours(routes)


def is_discount_eligible(shelf_life_hours):
    return shelf_life_hours <= DISCOUNT_ELIGIBLE_HOURS


def tier_caps(demand):
    """Demand available at each price level, keyed by discount percent.

    0 -> normal demand. Each discount tier unlocks an extra slice of buyers:
    ELASTICITY x demand x (this discount - previous discount).
    """
    caps = {0: int(demand)}
    previous = 0.0
    for discount in DISCOUNT_TIERS:
        extra = DEMAND_ELASTICITY * demand * (discount - previous)
        caps[int(round(discount * 100))] = int(np.floor(extra + 1e-9))
        previous = discount
    return caps


def _lookups(destinations, routes):
    route_lookup = routes.set_index("destination").to_dict("index")
    dest_lookup = {
        (r.destination, r.commodity): r
        for r in destinations.itertuples(index=False)
    }
    return route_lookup, dest_lookup


def _plan_row(batch_id, commodity, shelf_life, unit_cost, perishable,
              destination, quantity, list_price, discount, route, reason):
    price = float(list_price) * (1 - discount)
    transport = float(route["transport_cost_per_crate"])
    handling = float(route.get("handling_cost_per_crate", 0.0))
    arrival = arrival_hours(route)
    margin = price - float(unit_cost) - transport - handling

    return {
        "batch_id": batch_id,
        "commodity": commodity,
        "destination": destination,
        "quantity": int(quantity),
        "shelf_life_hours": int(shelf_life),
        "arrival_hours": arrival,
        "perishable": bool(perishable),
        "list_price": float(list_price),
        "discount_pct": int(round(discount * 100)),
        "selling_price": price,
        "unit_cost": float(unit_cost),
        "transport_cost_per_crate": transport,
        "handling_cost_per_crate": handling,
        "estimated_unit_margin": margin,
        "estimated_profit": int(quantity) * margin,
        "reason": reason,
    }


def priority_label(slack_hours):
    if slack_hours <= 6:
        return "Critical"
    if slack_hours <= 12:
        return "High"
    if slack_hours <= 24:
        return "Medium"
    return "Low"


def finalize_plan(plan, order="urgency"):
    """Add slack, priority and dispatch order, then sort the plan.

    order="urgency": least remaining shelf life ships first.
    order="fifo":    batch id order (what the baseline actually does).
    """
    if plan.empty:
        return plan

    plan = plan.copy()
    plan["slack_hours"] = plan["shelf_life_hours"] - plan["arrival_hours"]

    batch_slack = plan.groupby("batch_id")["slack_hours"].transform("min")
    plan["priority"] = batch_slack.apply(priority_label)

    sort_cols = (
        ["shelf_life_hours", "batch_id"] if order == "urgency"
        else ["batch_id"]
    )
    ranking = (
        plan[["batch_id", "shelf_life_hours"]].drop_duplicates()
        .sort_values(sort_cols).reset_index(drop=True)
    )
    ranking["dispatch_order"] = ranking.index + 1

    plan = plan.merge(ranking[["batch_id", "dispatch_order"]], on="batch_id")
    return plan.sort_values(
        ["dispatch_order", "arrival_hours", "discount_pct", "destination"]
    ).reset_index(drop=True)


def build_leftovers(inventory, plan, routes):
    """Unallocated stock per batch, with an honest status.

    "At risk - will spoil": cannot reach any market next cycle.
    "Carry forward":        unsold now, but still sellable next cycle.
    """
    allocated = (
        plan.groupby("batch_id")["quantity"].sum().to_dict()
        if not plan.empty else {}
    )

    left = inventory[
        ["batch_id", "commodity", "quantity", "shelf_life_hours"]
    ].copy()
    left["allocated_quantity"] = (
        left["batch_id"].map(allocated).fillna(0).astype(int)
    )
    left["unallocated_quantity"] = (
        left["quantity"] - left["allocated_quantity"]
    )

    min_arrival = min_arrival_hours(routes)

    def status(row):
        if row["unallocated_quantity"] == 0:
            return DONE_STATUS
        if row["shelf_life_hours"] - CYCLE_HOURS < min_arrival:
            return AT_RISK_STATUS
        return CARRY_STATUS

    left["status"] = left.apply(status, axis=1)
    return left.sort_values(
        ["shelf_life_hours", "batch_id"]
    ).reset_index(drop=True)

def create_plan_baseline(inventory, destinations, routes, buffer_hours=None):
    """Baseline: what a distributor does without decision support.

    Batches are handled in arrival order (FIFO). Each batch goes to the
    nearest market that still has demand and route capacity. It ignores
    urgency and margin. It will not ship a load that arrives expired.
    """
    buf = _buffer(buffer_hours)
    route_lookup, dest_lookup = _lookups(destinations, routes)

    remaining_demand = {k: int(v.demand) for k, v in dest_lookup.items()}
    remaining_capacity = {
        r.destination: int(r.capacity)
        for r in routes.itertuples(index=False)
    }
    nearest_first = sorted(
        route_lookup,
        key=lambda name: (float(route_lookup[name]["travel_hours"]), name),
    )

    allocations = []
    for batch in inventory.sort_values("batch_id").itertuples(index=False):
        remaining = int(batch.quantity)
        perishable = expires_unsold(batch.shelf_life_hours, routes)

        for name in nearest_first:
            if remaining <= 0:
                break

            route = route_lookup[name]
            if arrival_hours(route) + buf > batch.shelf_life_hours:
                continue

            key = (name, batch.commodity)
            if key not in dest_lookup:
                continue

            quantity = min(
                remaining, remaining_demand[key], remaining_capacity[name]
            )
            if quantity <= 0:
                continue

            allocations.append(_plan_row(
                batch.batch_id, batch.commodity, batch.shelf_life_hours,
                batch.unit_cost, perishable, name, quantity,
                dest_lookup[key].selling_price, 0.0, route,
                "Baseline rule: next batch in FIFO order, sent to the "
                "nearest market with open demand and capacity.",
            ))

            remaining -= quantity
            remaining_demand[key] -= quantity
            remaining_capacity[name] -= quantity

    plan = pd.DataFrame(allocations)
    plan = finalize_plan(plan, order="fifo")
    return plan, build_leftovers(inventory, plan, routes)

def create_plan_greedy(inventory, destinations, routes, buffer_hours=None):
    """Rule-based planner: shortest shelf life first, best margin first."""
    buf = _buffer(buffer_hours)
    route_lookup, dest_lookup = _lookups(destinations, routes)

    remaining_demand = {k: int(v.demand) for k, v in dest_lookup.items()}
    remaining_capacity = {
        r.destination: int(r.capacity)
        for r in routes.itertuples(index=False)
    }

    allocations = []
    ordered = inventory.sort_values(["shelf_life_hours", "batch_id"])

    for batch in ordered.itertuples(index=False):
        remaining = int(batch.quantity)
        perishable = expires_unsold(batch.shelf_life_hours, routes)

        feasible = []
        for (name, commodity), dest in dest_lookup.items():
            if commodity != batch.commodity:
                continue

            route = route_lookup[name]
            arrival = arrival_hours(route)
            if arrival + buf > batch.shelf_life_hours:
                continue

            price = float(dest.selling_price)
            margin = (
                price - float(batch.unit_cost)
                - float(route["transport_cost_per_crate"])
                - float(route.get("handling_cost_per_crate", 0.0))
            )
            if margin <= 0:
                continue

            feasible.append((margin, arrival, name, price, route))

        # Best margin first, then the faster delivery.
        feasible.sort(key=lambda c: (c[0], -c[1]), reverse=True)

        for margin, arrival, name, price, route in feasible:
            if remaining <= 0:
                break

            key = (name, batch.commodity)
            quantity = min(
                remaining, remaining_demand[key], remaining_capacity[name]
            )
            if quantity <= 0:
                continue

            allocations.append(_plan_row(
                batch.batch_id, batch.commodity, batch.shelf_life_hours,
                batch.unit_cost, perishable, name, quantity, price, 0.0,
                route,
                f"Arrives in {arrival:g} h, "
                f"{batch.shelf_life_hours - arrival:g} h before expiry; "
                f"contribution ₹{margin:.2f} per crate.",
            ))

            remaining -= quantity
            remaining_demand[key] -= quantity
            remaining_capacity[name] -= quantity

    plan = finalize_plan(pd.DataFrame(allocations), order="urgency")
    return plan, build_leftovers(inventory, plan, routes)

def create_plan_optimized(inventory, destinations, routes,
                          allow_discounts=True, buffer_hours=None):
    """Maximise net profit with an integer program.

    Decision: crates of each batch sent to each destination at each price
    level (list price, or a clearance discount for at-risk batches).
    Limits: batch stock, demand per price level, route capacity, shelf life.

    Stock that would expire unsold is valued at its avoided write-off, so
    shipping it below cost beats letting it spoil.
    """
    buf = _buffer(buffer_hours)
    route_lookup, dest_lookup = _lookups(destinations, routes)
    tiers = [0.0] + (list(DISCOUNT_TIERS) if allow_discounts else [])

    # 1. Enumerate every feasible shipment option.
    options = []
    for b in inventory.itertuples(index=False):
        perishable = expires_unsold(b.shelf_life_hours, routes)

        for cell, dest in dest_lookup.items():
            name, commodity = cell
            if commodity != b.commodity:
                continue

            route = route_lookup[name]
            if arrival_hours(route) + buf > b.shelf_life_hours:
                continue

            for k, discount in enumerate(tiers):
                if k > 0 and not is_discount_eligible(b.shelf_life_hours):
                    break

                price = float(dest.selling_price) * (1 - discount)
                margin = (
                    price - float(b.unit_cost)
                    - float(route["transport_cost_per_crate"])
                    - float(route.get("handling_cost_per_crate", 0.0))
                )
                # Expiring stock is lost anyway, so its cost is sunk.
                value = margin + (float(b.unit_cost) if perishable else 0.0)
                if value <= 1e-9:
                    continue

                options.append({
                    "batch": b, "cell": cell, "route": route,
                    "perishable": perishable, "tier": k,
                    "discount": discount, "list_price": dest.selling_price,
                    "margin": margin, "value": value,
                })

    if not options:
        empty = pd.DataFrame()
        return empty, build_leftovers(inventory, empty, routes)

    # 2. Constraint matrix.
    batch_ids = inventory["batch_id"].tolist()
    cells = list(dest_lookup)
    dest_names = routes["destination"].tolist()
    nb, nc, nt, nr = len(batch_ids), len(cells), len(tiers), len(dest_names)

    b_idx = {k: i for i, k in enumerate(batch_ids)}
    c_idx = {k: i for i, k in enumerate(cells)}
    d_idx = {k: i for i, k in enumerate(dest_names)}
    route_row0 = nb + nc * nt

    A = np.zeros((route_row0 + nr, len(options)))
    for j, o in enumerate(options):
        A[b_idx[o["batch"].batch_id], j] = 1
        A[nb + o["tier"] * nc + c_idx[o["cell"]], j] = 1
        A[route_row0 + d_idx[o["cell"][0]], j] = 1

    upper = np.zeros(route_row0 + nr)
    upper[:nb] = inventory["quantity"].to_numpy(dtype=float)
    for cell, i in c_idx.items():
        caps = tier_caps(dest_lookup[cell].demand)
        for k, discount in enumerate(tiers):
            upper[nb + k * nc + i] = caps[int(round(discount * 100))]
    upper[route_row0:] = routes["capacity"].to_numpy(dtype=float)

    # 3. Solve (milp minimises, so negate the objective).
    result = milp(
        c=-np.array([o["value"] for o in options]),
        constraints=LinearConstraint(A, -np.inf, upper),
        integrality=np.ones(len(options)),
        bounds=Bounds(0, np.inf),
    )
    if not result.success:
        raise RuntimeError(f"Optimizer failed: {result.message}")

    # 4. Convert the solution into plan rows with plain-language reasons.
    allocations = []
    for o, x in zip(options, np.round(result.x).astype(int)):
        if x <= 0:
            continue

        b, route = o["batch"], o["route"]
        arrival = arrival_hours(route)
        slack = b.shelf_life_hours - arrival
        pct = int(round(o["discount"] * 100))

        if pct > 0:
            reason = (
                f"Clearance: {pct}% off to move at-risk stock "
                f"({b.shelf_life_hours} h shelf life). Assumes extra buyers "
                f"respond at elasticity {DEMAND_ELASTICITY:g}; "
                f"contribution ₹{o['margin']:.2f} per crate."
            )
        elif o["margin"] < 0:
            reason = (
                f"Arrives in {arrival:g} h ({slack:g} h slack). Sold below "
                "cost: this stock would expire unsold, so recovering part "
                "of its cost beats a full write-off."
            )
        elif o["perishable"]:
            reason = (
                f"Arrives in {arrival:g} h ({slack:g} h slack). Cannot wait "
                f"for the next cycle; contribution "
                f"₹{o['margin']:.2f} per crate."
            )
        else:
            reason = (
                f"Arrives in {arrival:g} h ({slack:g} h slack). Best use of "
                f"scarce route capacity; contribution "
                f"₹{o['margin']:.2f} per crate."
            )

        allocations.append(_plan_row(
            b.batch_id, b.commodity, b.shelf_life_hours, b.unit_cost,
            o["perishable"], o["cell"][0], x, o["list_price"],
            o["discount"], route, reason,
        ))

    plan = finalize_plan(pd.DataFrame(allocations), order="urgency")
    return plan, build_leftovers(inventory, plan, routes)


def create_plan(inventory, destinations, routes, **kwargs):
    """Default planner used by the app: optimizer with discounts."""
    return create_plan_optimized(inventory, destinations, routes, **kwargs)


PLANNERS = {
    "Baseline": create_plan_baseline,
    "Rule-based": create_plan_greedy,
    "Optimized": lambda i, d, r, **kw: create_plan_optimized(
        i, d, r, allow_discounts=False, **kw
    ),
    "Optimized + discounts": create_plan_optimized,
}

def discount_recommendations(plan):
    """Clearance discounts recommended by a plan, one row per shipment."""
    columns = [
        "batch_id", "commodity", "destination", "discount_pct",
        "quantity", "list_price", "selling_price", "reason",
    ]
    if plan.empty or (plan["discount_pct"] > 0).sum() == 0:
        return pd.DataFrame(columns=columns)

    out = plan.loc[plan["discount_pct"] > 0, columns].copy()
    out["list_price"] = out["list_price"].round(2)
    out["selling_price"] = out["selling_price"].round(2)
    return out.reset_index(drop=True)


def validate_plan(plan, inventory, destinations, routes, buffer_hours=None):
    """Check every hard constraint. Each row of the result should PASS."""
    buf = _buffer(buffer_hours)
    checks = []

    def record(name, violations, detail):
        checks.append({
            "Check": name,
            "Violations": int(violations),
            "Status": "PASS" if violations == 0 else "FAIL",
            "Detail": detail,
        })

    if plan.empty:
        for name in ["Batch stock", "Destination demand",
                     "Route capacity", "Shelf life on arrival",
                     "Positive whole quantities"]:
            record(name, 0, "No shipments")
        return pd.DataFrame(checks)

    # Stock per batch.
    shipped = plan.groupby("batch_id")["quantity"].sum()
    stock = inventory.set_index("batch_id")["quantity"]
    over = (shipped - stock.reindex(shipped.index)) > 0
    record("Batch stock", over.sum(), "Shipped <= stock for every batch")

    # Demand per destination, commodity and price level.
    demand = destinations.set_index(["destination", "commodity"])["demand"]
    cell_tier = plan.groupby(
        ["destination", "commodity", "discount_pct"]
    )["quantity"].sum()
    bad = 0
    for (dest, commodity, pct), qty in cell_tier.items():
        cap = tier_caps(demand[(dest, commodity)]).get(int(pct), 0)
        bad += int(qty > cap)
    record("Destination demand", bad,
           "Shipped <= demand at each price level")

    # Route capacity.
    by_dest = plan.groupby("destination")["quantity"].sum()
    capacity = routes.set_index("destination")["capacity"]
    over = (by_dest - capacity.reindex(by_dest.index)) > 0
    record("Route capacity", over.sum(), "Shipped <= capacity per route")

    # Shelf life.
    late = (plan["arrival_hours"] + buf) > plan["shelf_life_hours"]
    record("Shelf life on arrival", late.sum(),
           f"Arrival + {buf:g} h buffer <= shelf life")

    # Sanity.
    bad_qty = (plan["quantity"] <= 0) | (plan["quantity"] % 1 != 0)
    record("Positive whole quantities", bad_qty.sum(),
           "Crates are whole numbers > 0")

    return pd.DataFrame(checks)


if __name__ == "__main__":
    inventory, destinations, routes = load_data()

    plan, leftovers = create_plan(inventory, destinations, routes)

    print("\n=== DISPATCH PLAN (Optimized + discounts) ===")
    print(plan[
        ["dispatch_order", "priority", "batch_id", "commodity",
         "destination", "quantity", "discount_pct", "arrival_hours",
         "estimated_profit"]
    ].to_string(index=False))

    print("\n=== DISCOUNT RECOMMENDATIONS ===")
    recs = discount_recommendations(plan)
    print(recs.drop(columns="reason").to_string(index=False)
          if not recs.empty else "No discounts recommended.")

    print("\n=== UNALLOCATED INVENTORY ===")
    print(leftovers.to_string(index=False))

    print("\n=== FEASIBILITY CHECKS ===")
    print(validate_plan(plan, inventory, destinations, routes)
          .to_string(index=False))