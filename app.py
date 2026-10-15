import sys
from pathlib import Path

import plotly.express as px
import streamlit as st

# Hide the Streamlit header, footer, and main menu
hide_st_style = """
            <style>
            #MainMenu {visibility: hidden;}
            footer {visibility: hidden;}
            header {visibility: hidden;}
            </style>
            """
st.markdown(hide_st_style, unsafe_allow_html=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.metrics import (  
    commodity_summary, headline_table, plan_metrics, route_utilization,
)
from src.planner import (  
    CYCLE_HOURS, DATA_DIR, DEMAND_ELASTICITY, DISCOUNT_ELIGIBLE_HOURS,
    DISCOUNT_TIERS, PLANNERS, discount_recommendations, load_data,
    validate_plan,
)
from src.scenarios import (  
    apply_scenario, robustness_summary, run_robustness,
)

st.set_page_config(page_title="FreshRoute", page_icon="🥭", layout="wide")

BASELINE = "Baseline"
BEST = "Optimized + discounts"
DATA_FILES = ("inventory.csv", "destinations.csv", "routes.csv")

def data_version():
    """Changes whenever a CSV is regenerated, so caches never go stale."""
    return tuple((DATA_DIR / f).stat().st_mtime for f in DATA_FILES)


@st.cache_data(show_spinner="Planning all strategies...")
def run_all(demand_pct, delay_h, shelf_pct, price_pct, version):
    inventory, destinations, routes = load_data()
    inventory, destinations, routes = apply_scenario(
        inventory, destinations, routes,
        demand_mult=1 + demand_pct / 100,
        delay_hours=delay_h,
        shelf_mult=1 + shelf_pct / 100,
        price_mult=1 + price_pct / 100,
    )

    results = {}
    for name, fn in PLANNERS.items():
        plan, left = fn(inventory, destinations, routes)
        results[name] = {
            "plan": plan,
            "left": left,
            "metrics": plan_metrics(plan, left, inventory, destinations),
            "checks": validate_plan(plan, inventory, destinations, routes),
        }
    return results, inventory, destinations, routes


@st.cache_data(show_spinner="Stress-testing every plan...")
def robustness(version):
    inventory, destinations, routes = load_data()
    return run_robustness(inventory, destinations, routes)


def rupees(value):
    return f"₹{value:,.0f}"

st.title("🥭 FreshRoute")
st.caption(
    "Decision support for perishable produce distribution: what to "
    "dispatch first, where to send it, and when to discount. "
    "All data is synthetic."
)

if not all((DATA_DIR / f).exists() for f in DATA_FILES):
    st.error("Data files not found. Run `python src/generate_data.py`, "
             "then refresh this page.")
    st.stop()

version = data_version()

st.sidebar.header("Plan settings")
selected = st.sidebar.selectbox(
    "Strategy to inspect", list(PLANNERS), index=list(PLANNERS).index(BEST)
)

st.sidebar.subheader("What-if scenario")
st.sidebar.caption("Every strategy is re-planned with the changed inputs.")
demand_pct = st.sidebar.slider("Demand change (%)", -50, 50, 0, 5)
delay_h = st.sidebar.slider("Extra travel delay (hours)", 0.0, 8.0, 0.0, 0.5)
shelf_pct = st.sidebar.slider("Shelf-life change (%)", -50, 20, 0, 5)
price_pct = st.sidebar.slider("Market price change (%)", -30, 30, 0, 5)

changes = [
    text for text, active in [
        (f"demand {demand_pct:+d}%", demand_pct != 0),
        (f"delay +{delay_h:g} h", delay_h != 0),
        (f"shelf life {shelf_pct:+d}%", shelf_pct != 0),
        (f"prices {price_pct:+d}%", price_pct != 0),
    ] if active
]
if changes:
    st.info("What-if scenario active: " + ", ".join(changes) + ".")

results, inventory, destinations, routes = run_all(
    demand_pct, delay_h, shelf_pct, price_pct, version
)
plan = results[selected]["plan"]
left = results[selected]["left"]
cur = results[selected]["metrics"]
base = results[BASELINE]["metrics"]
is_base = selected == BASELINE

tabs = st.tabs([
    "Overview", "Dispatch plan", "Discounts", "Inventory",
    "Robustness", "Assumptions & checks",
])

with tabs[0]:
    st.subheader(f"{selected} vs {BASELINE}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Net profit", rupees(cur["net_profit"]),
        delta=None if is_base
        else f"{cur['net_profit'] - base['net_profit']:+,.0f}",
    )
    c2.metric(
        "Net revenue", rupees(cur["net_revenue"]),
        delta=None if is_base
        else f"{cur['net_revenue'] - base['net_revenue']:+,.0f}",
    )
    c3.metric(
        "Demand met", f"{cur['demand_met_pct']:.1f}%",
        delta=None if is_base
        else f"{cur['demand_met_pct'] - base['demand_met_pct']:+.1f} pts",
    )
    c4.metric(
        "Crates expiring unsold", cur["crates_spoiling"],
        delta=None if is_base
        else f"{cur['crates_spoiling'] - base['crates_spoiling']:+d}",
        delta_color="inverse",
    )

    table = headline_table({n: r["metrics"] for n, r in results.items()})
    st.markdown("**All strategies side by side**")
    st.dataframe(table, hide_index=True)

    left_col, right_col = st.columns(2)
    money = table.melt(
        id_vars="Planner",
        value_vars=["Net revenue (₹)", "Net profit (₹)"],
        var_name="Measure", value_name="₹",
    )
    left_col.plotly_chart(
        px.bar(money, x="Planner", y="₹", color="Measure", barmode="group",
               title="Net revenue and net profit"),
    )
    right_col.plotly_chart(
        px.bar(table, x="Planner", y="Demand met (%)",
               title="Demand met (%)", range_y=[0, 100]),
    )

    st.caption(
        "Net revenue = revenue after discounts - transport - handling - "
        "spoilage write-off. Net profit also subtracts the purchase cost of "
        "the crates sold. See the Assumptions tab for details."
    )

# Dispatch plan
with tabs[1]:
    st.subheader(f"Dispatch plan: {selected}")

    if plan.empty:
        st.warning("No feasible shipments in this scenario.")
    else:
        queue = (
            plan.groupby(
                ["dispatch_order", "priority", "batch_id", "commodity",
                 "shelf_life_hours"], as_index=False,
            ).agg(
                crates=("quantity", "sum"),
                destinations=("destination",
                              lambda s: ", ".join(sorted(set(s)))),
            )
        ).rename(columns={
            "dispatch_order": "Dispatch #", "priority": "Priority",
            "batch_id": "Batch", "commodity": "Commodity",
            "shelf_life_hours": "Shelf life (h)", "crates": "Crates",
            "destinations": "Sent to",
        })
        st.markdown("**Dispatch queue** (ship in this order)")
        st.dataframe(queue, hide_index=True)

        st.markdown("**Shipments**")
        picked = st.multiselect(
            "Show destinations", sorted(plan["destination"].unique()),
            default=sorted(plan["destination"].unique()),
        )
        view = plan[plan["destination"].isin(picked)][[
            "dispatch_order", "priority", "batch_id", "commodity",
            "destination", "quantity", "discount_pct", "arrival_hours",
            "selling_price", "estimated_profit", "reason",
        ]].rename(columns={
            "dispatch_order": "Dispatch #", "priority": "Priority",
            "batch_id": "Batch", "commodity": "Commodity",
            "destination": "Destination", "quantity": "Crates",
            "discount_pct": "Discount %", "arrival_hours": "Arrives (h)",
            "selling_price": "Price (₹/crate)",
            "estimated_profit": "Profit (₹)", "reason": "Why",
        })
        view["Price (₹/crate)"] = view["Price (₹/crate)"].round(2)
        view["Profit (₹)"] = view["Profit (₹)"].round(2)
        st.dataframe(view, hide_index=True)

        st.markdown("**Why was this batch sent where it was?**")
        chosen = st.selectbox(
            "Pick a batch", queue["Batch"].tolist(), key="explain_batch"
        )
        for r in plan[plan["batch_id"] == chosen].itertuples():
            st.markdown(
                f"- **{r.quantity} crates to {r.destination}**"
                f" ({r.priority} priority): {r.reason}"
            )
        row = left[left["batch_id"] == chosen].iloc[0]
        if row["unallocated_quantity"] > 0:
            st.markdown(
                f"- **{row['unallocated_quantity']} crates not shipped:** "
                f"{row['status']}."
            )

        st.plotly_chart(
            px.bar(route_utilization(plan, routes), x="destination",
                   y="utilization_pct", text="utilization_pct",
                   title="Route capacity used (%)", range_y=[0, 105]),
        )

# Discounts
with tabs[2]:
    st.subheader("Discount recommendations")
    best_plan = results[BEST]["plan"]
    recs = discount_recommendations(best_plan)

    tier_text = ", ".join(f"{int(t * 100)}%" for t in DISCOUNT_TIERS)
    st.markdown(
        f"**Rule:** batches with at most **{DISCOUNT_ELIGIBLE_HOURS} h** of "
        f"shelf life are at risk of going unsold. For them, the planner may "
        f"offer clearance discounts of {tier_text}. Each +1% discount is "
        f"assumed to bring **+{DEMAND_ELASTICITY:g}%** extra demand from "
        f"price-sensitive buyers. A discount is recommended only when it "
        f"beats the alternatives for that crate."
    )

    if recs.empty:
        st.info("No discounts are recommended in this scenario.")
    else:
        d1, d2, d3 = st.columns(3)
        d1.metric("Crates on discount", int(recs["quantity"].sum()))
        d2.metric("Batches affected", recs["batch_id"].nunique())
        d3.metric("Discount given",
                  rupees(results[BEST]["metrics"]["discount_cost"]))

        shown = recs.rename(columns={
            "batch_id": "Batch", "commodity": "Commodity",
            "destination": "Destination", "discount_pct": "Discount %",
            "quantity": "Crates", "list_price": "List price (₹)",
            "selling_price": "Discounted price (₹)", "reason": "Why",
        })
        st.dataframe(shown, hide_index=True)

    st.caption("Discount recommendations come from the 'Optimized + "
               "discounts' strategy.")

# Inventory
with tabs[3]:
    st.subheader(f"Inventory after dispatch: {selected}")

    st.dataframe(
        left.rename(columns={
            "batch_id": "Batch", "commodity": "Commodity",
            "quantity": "Stock", "shelf_life_hours": "Shelf life (h)",
            "allocated_quantity": "Shipped",
            "unallocated_quantity": "Not shipped", "status": "Status",
        }),
        hide_index=True,
    )

    summary = commodity_summary(plan, inventory)
    chart_data = summary.melt(
        id_vars="commodity", value_vars=["stock", "shipped"],
        var_name="Crates", value_name="Count",
    )
    st.plotly_chart(
        px.bar(chart_data, x="commodity", y="Count", color="Crates",
               barmode="group", title="Stock vs crates shipped"),
    )

    st.caption(
        "'At risk - will spoil' stock cannot reach any market in the next "
        f"cycle ({CYCLE_HOURS} h). 'Carry forward' stock is still sellable "
        "next cycle."
    )

with tabs[4]:
    st.subheader("Robustness and sensitivity")
    st.markdown(
        "Every strategy is tested against demand swings, delays, shorter "
        "shelf life and price drops, in two ways:\n"
        "- **Fixed plan:** the plan was made on normal data, then reality "
        "changes.\n"
        "- **Replanned:** the planner reruns with the new information."
    )

    if st.button("Run robustness study"):
        st.session_state["show_robustness"] = True

    if st.session_state.get("show_robustness"):
        rob = robustness(version)

        st.markdown("**Summary across all scenarios**")
        st.dataframe(
            robustness_summary(rob).rename(columns={
                "mode": "Mode", "planner": "Strategy",
                "avg_net_profit": "Avg net profit (₹)",
                "worst_net_profit": "Worst net profit (₹)",
                "avg_spoiled_crates": "Avg crates lost",
                "worst_spoiled_crates": "Worst crates lost",
                "avg_demand_met_pct": "Avg demand met (%)",
            }),
            hide_index=True,
        )

        mode = st.radio("Show", ["Fixed plan", "Replanned"], horizontal=True)
        subset = rob[rob["mode"] == mode]
        st.plotly_chart(
            px.bar(subset, x="scenario", y="net_profit", color="planner",
                   barmode="group", title=f"Net profit by scenario ({mode})",
                   labels={"net_profit": "Net profit (₹)"}),
        )
        st.plotly_chart(
            px.bar(subset, x="scenario", y="spoiled_crates", color="planner",
                   barmode="group",
                   title=f"Crates lost by scenario ({mode})",
                   labels={"spoiled_crates": "Crates lost"}),
        )
        st.caption(
            "Crates lost = expired in transit + unsold at the destination + "
            "expired at the depot. The '6 h buffer' strategy only ships "
            "loads that arrive 6 h before expiry, which protects against "
            "long delays."
        )
    else:
        st.caption("This runs about 90 planning passes, so it starts only "
                   "when you click.")

# Assumptions and checks
with tabs[5]:
    st.subheader("Feasibility checks")
    st.dataframe(results[selected]["checks"], hide_index=True)

    st.subheader("Cost model (same for every strategy)")
    st.markdown(
        "- **Revenue** = crates x selling price after discount\n"
        "- **Net revenue** = revenue - transport - handling - spoilage "
        "write-off\n"
        "- **Net profit** = net revenue - purchase cost of crates sold\n"
        "- **Spoilage write-off** = purchase cost of stock that expires "
        "unsold"
    )

    st.subheader("Assumptions")
    st.markdown(
        f"- **Strategies.** *Baseline*: batches in arrival order (FIFO) to "
        f"the nearest market, ignoring urgency and margin. *Rule-based*: "
        f"shortest shelf life first, then best margin. *Optimized*: an "
        f"integer program that maximises net profit.\n"
        f"- **Delivery window.** A load is feasible only if travel plus "
        f"handling time fits within the batch's remaining shelf life.\n"
        f"- **Dispatch cycle.** {CYCLE_HOURS} h. Stock not dispatched "
        f"ages by one cycle, and is lost if it can no longer reach any "
        f"market.\n"
        f"- **Discounts.** Tiers {tier_text}, only for batches with at "
        f"most {DISCOUNT_ELIGIBLE_HOURS} h shelf life, elasticity "
        f"{DEMAND_ELASTICITY:g}. Discounts target extra buyers, so "
        f"list-price sales are not cannibalised.\n"
        f"- **Units.** One quantity unit is one crate; prices and costs "
        f"are per crate.\n"
        f"- **Data.** Synthetic and seeded, so every run is reproducible."
    )

    with st.expander("Input data used for this run"):
        st.markdown("**Inventory**")
        st.dataframe(inventory, hide_index=True)
        st.markdown("**Destination demand and prices**")
        st.dataframe(destinations, hide_index=True)
        st.markdown("**Routes**")
        st.dataframe(routes, hide_index=True)