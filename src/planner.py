
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def load_data():
    """Load the three synthetic datasets."""
    inventory = pd.read_csv(DATA_DIR / "inventory.csv")
    destinations = pd.read_csv(DATA_DIR / "destinations.csv")
    routes = pd.read_csv(DATA_DIR / "routes.csv")

    return inventory, destinations, routes


def create_plan(inventory, destinations, routes):
    """Allocate produce while respecting demand, expiry and route capacity."""

    # Track the remaining demand for each destination and commodity.
    remaining_demand = {
        (row.destination, row.commodity): int(row.demand)
        for row in destinations.itertuples(index=False)
    }

    # Track total capacity remaining on each destination's route.
    remaining_capacity = {
        row.destination: int(row.capacity)
        for row in routes.itertuples(index=False)
    }

    route_lookup = routes.set_index("destination").to_dict("index")
    allocations = []

    # Process batches with the shortest shelf life first.
    inventory = inventory.sort_values(
        "shelf_life_hours", ascending=True
    )

    for batch in inventory.itertuples(index=False):
        batch_remaining = int(batch.quantity)

        # Consider destinations with demand for this commodity.
        candidates = destinations[
            (destinations["commodity"] == batch.commodity)
            & (destinations["demand"] > 0)
        ].copy()

        # Prefer destinations with higher selling prices.
        candidates = candidates.sort_values(
            "selling_price", ascending=False
        )

        for destination in candidates.itertuples(index=False):
            if batch_remaining <= 0:
                break

            destination_name = destination.destination
            route = route_lookup[destination_name]

            travel_hours = float(route["travel_hours"])
            handling_hours = float(route["handling_hours"])
            arrival_hours = travel_hours + handling_hours

            # Reject deliveries that would arrive after expiry.
            if arrival_hours > batch.shelf_life_hours:
                continue

            key = (destination_name, batch.commodity)
            demand_left = remaining_demand.get(key, 0)
            capacity_left = remaining_capacity[destination_name]

            if demand_left <= 0 or capacity_left <= 0:
                continue

            # Ship only what the batch, demand and route can accommodate.
            quantity = min(
                batch_remaining,
                demand_left,
                capacity_left,
            )

            if quantity <= 0:
                continue

            unit_transport_cost = float(
                route["transport_cost_per_crate"]
            )
            unit_cost = float(batch.unit_cost)
            selling_price = float(destination.selling_price)

            unit_margin = (
                selling_price - unit_cost - unit_transport_cost
            )

            if unit_margin <= 0:
                continue

            allocations.append({
                "batch_id": batch.batch_id,
                "commodity": batch.commodity,
                "destination": destination_name,
                "quantity": quantity,
                "shelf_life_hours": int(batch.shelf_life_hours),
                "arrival_hours": arrival_hours,
                "selling_price": selling_price,
                "unit_cost": unit_cost,
                "transport_cost_per_crate": unit_transport_cost,
                "estimated_unit_margin": unit_margin,
                "estimated_profit": quantity * unit_margin,
                "reason": (
                    f"Arrives in {arrival_hours:g} hours before expiry; "
                    f"positive estimated margin of ₹{unit_margin:.2f} "
                    "per crate."
                ),
            })

            # Update the remaining resources.
            batch_remaining -= quantity
            remaining_demand[key] -= quantity
            remaining_capacity[destination_name] -= quantity

    plan = pd.DataFrame(allocations)

    # Report how much of each original batch was not allocated.
    allocated_by_batch = (
        plan.groupby("batch_id")["quantity"].sum().to_dict()
        if not plan.empty else {}
    )

    leftovers = inventory[["batch_id", "commodity", "quantity",
                           "shelf_life_hours"]].copy()

    leftovers["allocated_quantity"] = leftovers["batch_id"].map(
        allocated_by_batch
    ).fillna(0).astype(int)

    leftovers["unallocated_quantity"] = (
        leftovers["quantity"] - leftovers["allocated_quantity"]
    )

    leftovers["status"] = leftovers["shelf_life_hours"].apply(
        lambda hours: (
            "At risk within 24 hours"
            if hours <= 24 else
            "Shelf life exceeds 24 hours"
        )
    )

    return plan, leftovers


if __name__ == "__main__":
    inventory, destinations, routes = load_data()

    plan, leftovers = create_plan(
        inventory, destinations, routes
    )

    print("\n=== FRESHROUTE ALLOCATION PLAN ===")

    if plan.empty:
        print("No feasible profitable allocations found.")
    else:
        print(plan[
            ["batch_id", "commodity", "destination",
             "quantity", "arrival_hours", "estimated_profit"]
        ].to_string(index=False))

    print("\n=== UNALLOCATED INVENTORY ===")
    print(leftovers.to_string(index=False))

    print("\n=== SUMMARY ===")
    print(f"Total inventory: {inventory['quantity'].sum()} crates")
    print(
        f"Total allocated: "
        f"{plan['quantity'].sum() if not plan.empty else 0} crates"
    )
    print(
        f"Total unallocated: "
        f"{leftovers['unallocated_quantity'].sum()} crates"
    )
