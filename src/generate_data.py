
from pathlib import Path

import numpy as np
import pandas as pd


# Save CSV files in the project's data folder.
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Reproducible simulated data.
rng = np.random.default_rng(42)

# Inventory batches.
commodities = {
    "Tomato": {
        "unit_cost": 18,
        "shelf_life": (12, 48),
        "quantity": (40, 150),
    },
    "Mango": {
        "unit_cost": 45,
        "shelf_life": (24, 96),
        "quantity": (25, 100),
    },
    "Banana": {
        "unit_cost": 22,
        "shelf_life": (12, 60),
        "quantity": (30, 120),
    },
    "Spinach": {
        "unit_cost": 15,
        "shelf_life": (6, 24),
        "quantity": (20, 80),
    },
}

inventory_rows = []

for i in range(1, 21):
    commodity = list(commodities)[(i - 1) % len(commodities)]
    spec = commodities[commodity]

    inventory_rows.append({
        "batch_id": f"B{i:03}",
        "commodity": commodity,
        "quantity": int(rng.integers(
            spec["quantity"][0],
            spec["quantity"][1] + 1
        )),
        "shelf_life_hours": int(rng.integers(
            spec["shelf_life"][0],
            spec["shelf_life"][1] + 1
        )),
        "unit_cost": spec["unit_cost"],
    })

inventory = pd.DataFrame(inventory_rows)

# Destination demand and selling prices.
destinations = ["Mumbai", "Pune", "Nashik", "Thane"]
destination_rows = []

for destination in destinations:
    for commodity, spec in commodities.items():
        destination_rows.append({
            "destination": destination,
            "commodity": commodity,
            "demand": int(rng.integers(20, 130)),
            "selling_price": round(
                spec["unit_cost"] * rng.uniform(1.35, 2.1), 2
            ),
        })

destination_df = pd.DataFrame(destination_rows)

# Simulated travel and transport constraints.
routes = pd.DataFrame([
    {
        "destination": "Mumbai",
        "travel_hours": 3,
        "transport_cost_per_crate": 3.0,
        "handling_hours": 1,
        "capacity": 350,
    },
    {
        "destination": "Pune",
        "travel_hours": 5,
        "transport_cost_per_crate": 5.0,
        "handling_hours": 1,
        "capacity": 250,
    },
    {
        "destination": "Nashik",
        "travel_hours": 4,
        "transport_cost_per_crate": 4.0,
        "handling_hours": 1,
        "capacity": 200,
    },
    {
        "destination": "Thane",
        "travel_hours": 3,
        "transport_cost_per_crate": 2.5,
        "handling_hours": 1,
        "capacity": 200,
    },
])

inventory.to_csv(DATA_DIR / "inventory.csv", index=False)
destination_df.to_csv(DATA_DIR / "destinations.csv", index=False)
routes.to_csv(DATA_DIR / "routes.csv", index=False)

print("Simulated dataset generated successfully!")
print(f"Inventory batches: {len(inventory)}")
print(f"Destination demand rows: {len(destination_df)}")
print(f"Route rows: {len(routes)}")
print(f"Files saved in: {DATA_DIR}")
