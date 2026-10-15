# 🥭 FreshRoute

**Decision support for perishable produce distribution (Wild Bugs Hackathon, PS 03).**

Fruit and vegetable distributors lose money when produce spoils, goes unsold, or is sent to the wrong market. FreshRoute looks at what is in stock, how long it will stay fresh, what each city wants and pays, and what trucks can carry. Then it tells you:

1. **What to dispatch first** (most perishable batches go first)
2. **Where to send it** (the most profitable use of limited truck space)
3. **When to discount** (clearance prices for stock at risk of going unsold)

Every recommendation comes with a plain-language reason, and every plan is compared against a simple baseline.

---

## Quick start

You need Python 3.10 or newer. These commands are for Windows; on Mac/Linux use `source .venv/bin/activate`.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python src/generate_data.py
streamlit run app.py
```

The app opens in your browser. Use the sidebar to pick a strategy and try what-if scenarios.

### Run the analysis in the terminal (optional)

```
python src/planner.py      # dispatch plan, discounts, feasibility checks
python src/metrics.py      # all strategies compared
python src/scenarios.py    # robustness study
```

---

## Project layout

```
freshroute/
├── app.py                  Streamlit dashboard
├── requirements.txt
├── README.md
├── data/                   Synthetic CSVs (created by generate_data.py)
│   ├── inventory.csv       Batches: commodity, quantity, shelf life, cost
│   ├── destinations.csv    Demand and selling price per city and commodity
│   └── routes.csv          Travel time, transport cost, handling, capacity
└── src/
    ├── generate_data.py    Builds the synthetic dataset (seeded, repeatable)
    ├── planner.py          The planning strategies + feasibility checks
    ├── metrics.py          Cost model and KPIs
    └── scenarios.py        Stress tests: delays, demand and price shocks
```

---

## The strategies

| Strategy | How it decides |
|---|---|
| **Baseline** | What a distributor does without software: batches in arrival order (FIFO), each sent to the *nearest* market with open demand and capacity. Ignores urgency and margin. |
| **Rule-based** | Shortest shelf life first, then the best profit per crate. |
| **Optimized** | A mathematical optimizer (integer program) that finds the most profitable plan that respects every limit. |
| **Optimized + discounts** | Same, but it may also offer clearance discounts on at-risk batches. |

Every strategy only ships a load if **travel time + handling time fits inside the batch's remaining shelf life**.

### Hard limits every plan must respect

- You can't ship more than a batch's stock.
- You can't ship more than a city wants (at each price level).
- You can't exceed a route's truck capacity.
- Cargo must arrive before it expires.

The app's **Assumptions & checks** tab verifies all four for the plan on screen.

### Dispatch order and priority

Batches are ranked by remaining shelf life, so the most perishable ship first. Each gets a priority label based on how much spare shelf life it has on arrival:

| Label | Spare shelf life |
|---|---|
| Critical | 6 hours or less |
| High | up to 12 hours |
| Medium | up to 24 hours |
| Low | more than 24 hours |

---

## Cost model (the same for every strategy)

| Item | Definition |
|---|---|
| Revenue | crates sold × selling price (after any discount) |
| **Net revenue** | revenue − transport − handling − spoilage write-off |
| **Net profit** | net revenue − purchase cost of the crates sold |
| Spoilage write-off | purchase cost of stock that expires unsold |

One quantity unit is one crate. Prices and costs are per crate.

---

## Assumptions (all can be changed at the top of `src/planner.py`)

| Assumption | Value | Meaning |
|---|---|---|
| Dispatch cycle | 24 h | Stock not sent now ages by 24 h. If it can then no longer reach any market, it counts as spoiled. Otherwise it is "carried forward". |
| Discount tiers | 10%, 20%, 30% | Clearance levels offered on at-risk batches. |
| Discount eligibility | shelf life ≤ 48 h | Only these batches may be discounted. |
| Demand response | +1.5% demand per +1% discount | Extra buyers appear when prices drop. Discounts target those buyers, so normal sales are not cannibalised. |
| Safety buffer | 0 h | Extra shelf-life margin required on arrival (used in the robustness study). |

**The discount assumption matters.** The profit gain from discounts depends on how many extra buyers a discount attracts. The app shows *Optimized* with and without discounts so you can see the effect of this assumption.

---

## What's in the app

| Tab | What you see |
|---|---|
| **Overview** | Net profit, net revenue, demand met and spoilage for every strategy, compared to the baseline |
| **Dispatch plan** | The dispatch queue, every shipment, a "why" explanation for any batch, and how full each route is |
| **Discounts** | Which batches to discount, by how much, and why |
| **Inventory** | What is left after dispatch and whether it is at risk or can wait |
| **Robustness** | Every strategy tested against delays, shorter shelf life, demand swings and price drops |
| **Assumptions & checks** | Feasibility checks, cost model, assumptions and the input data |

### What-if scenarios

The sidebar sliders change demand, travel delay, shelf life and prices. Every strategy is re-planned on the changed data. This shows how the system adapts when conditions change.

### Robustness study

Each strategy is tested in two ways:

- **Fixed plan:** the plan is made on normal data, then reality changes (for example, a 6-hour delay). How much do you lose?
- **Replanned:** the planner runs again with the new information. How well does it adapt?

A "6 h buffer" variant only ships loads that would arrive at least 6 hours before expiry. It trades a little profit for protection against long delays.

---

## Reproducibility

- The dataset is generated with a fixed random seed (`42`), so the data and results are identical on every machine.
- Delete the `data/` folder and run `python src/generate_data.py` to rebuild it.
- The app notices when the CSV files change and recalculates automatically.

---

## Known limitations

- **Synthetic data.** Real distributors would plug in their own inventory and price feeds.
- **Demand met vs profit.** The optimizer chases profit, so it can serve slightly less of total demand than the simple baseline, which ships everything including low-margin loads. The app shows both numbers.
- **Carry-forward stock** is assumed to stay sellable for one more cycle (24 h by default). Shorter real shelf lives would hurt it.
- **One depot, one dispatch cycle.** There is no multi-day planning and no cold storage yet.

## Ideas for next steps

- Forecast demand from sales history and seasonality
- Add cold storage to slow shelf-life decay for carried-forward stock
- Live replanning when a route is disrupted or a new order arrives
- Uncertainty ranges on shelf life and demand
- Expose the planner as an API (for example with Flask) so other systems can use it

---

*Built for the Wild Bugs Hackathon, PS 03: FreshRoute.*