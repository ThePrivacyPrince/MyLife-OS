"""Acquisition rules - pure functions. Facts only: anything unknown stays unknown and is reported as missing.

A listing is never 'mechanically sound' because the seller says it runs (spec 10.3).
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)
CONDITIONS = ("unknown", "good", "fair", "poor")
TITLE_STATUSES = ("unknown", "clean", "lien", "missing", "salvage")
YES_NO = ("unknown", "yes", "no")
STAGES = ("found", "contacted", "inspecting", "negotiating", "approved", "owned", "passed")
ACTIVE_STAGES = ("found", "contacted", "inspecting", "negotiating", "approved")

# A general used-PWC pre-purchase protocol, in the order you'd do it. (key, section, text, critical)
# Existing keys are kept so earlier inspections stay valid. Critical items must pass for the inspection gate.
PROTOCOL: list[tuple[str, str, str, bool]] = [
    ("hin_match", "📄 Paperwork", "Hull ID (HIN) on the hull matches the title and registration", True),
    ("title_id", "📄 Paperwork", "Name on the title matches the seller's photo ID", True),
    ("history", "📄 Paperwork", "Service records / maintenance history seen", False),
    ("hull", "🛥️ On the trailer", "Hull: no cracks, soft spots or major repairs (check underneath and around the pump)", True),
    ("pump", "🛥️ On the trailer", "Pump, impeller and wear ring: no heavy wear, chips or gap", True),
    ("intake", "🛥️ On the trailer", "Intake grate and ride plate straight and secure", False),
    ("cosmetic", "🛥️ On the trailer", "Seat, hood latch, storage and hull finish (cosmetic)", False),
    ("leaks", "⚙️ Engine bay", "No fuel or oil leaks, no strong fuel smell", True),
    ("corrosion", "⚙️ Engine bay", "No heavy corrosion; hoses and clamps in good shape", False),
    ("electrics", "⚙️ Engine bay", "Battery, gauges and charging work", False),
    ("compression", "🔑 Engine test", "Compression test: every cylinder in spec and close to each other", True),
    ("cold_start", "🔑 Engine test", "Starts easily from cold, idles smoothly, no warning lights", False),
    ("water_test", "🌊 Water test", "Reaches full throttle and top speed, no overheating or limp mode", True),
    ("cooling", "🌊 Water test", "Steady tell-tale (pee) stream while running", True),
    ("controls", "🌊 Water test", "Steering, throttle and brake/reverse work smoothly", False),
    ("bilge", "🌊 Water test", "Engine bay: no excess water after riding", True),
    ("trailer", "🚚 Trailer", "Trailer lights, bearings, tires, winch/straps and paperwork (if included)", False),
]
INSPECTION_ITEMS: list[tuple[str, str, bool]] = [(k, txt, crit) for k, _, txt, crit in PROTOCOL]
CRITICAL = {k for k, _, _, crit in PROTOCOL if crit}
RESULTS = ("", "pass", "fail", "dealbreaker", "na")
SCAM_PHRASES = ("deposit", "shipping", "ship it", "western union", "gift card", "zelle only before", "lost title",
                "no title", "bill of sale only", "out of the country", "military deployment")
SOFT_CLAIMS = ("runs great", "runs good", "ran when parked", "just needs", "easy fix", "needs battery")


@dataclass(frozen=True)
class Cost:
    price: Decimal | None
    total: Decimal          # sum of known parts
    complete: bool
    missing: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"${self.total:,.0f}" + ("" if self.complete else "+ (at least)")


def working_price(c) -> Decimal | None:
    return c.agreed_price or c.offer or c.asking_price


def ready_to_rent_cost(c) -> Cost:
    parts = {"price": working_price(c), "repairs": c.est_repairs, "transport": c.transport_cost,
             "prep": c.prep_cost}
    missing = tuple(k for k, v in parts.items() if v is None)
    total = sum((v for v in parts.values() if v is not None), ZERO)
    return Cost(parts["price"], total, not missing, missing)


KEY_FACTS = ("year", "asking_price", "engine_hours", "hull_condition", "mechanical_condition", "title_status",
             "trailer_included")


def known_facts(c) -> tuple[int, int, list[str]]:
    missing = []
    for f in KEY_FACTS:
        v = getattr(c, f)
        if v is None or v == "unknown":
            missing.append(f.replace("_", " "))
    if not inspected(c):
        missing.append("inspection")
    total = len(KEY_FACTS) + 1
    return total - len(missing), total, missing


def inspection_state(c) -> str:
    """not_started | in_progress | failed | passed | passed_with_fixes.

    A critical item that fails but has a fix-cost estimate is a known, priced repair: the ski can still pass
    ('passed_with_fixes') and the cost flows into repairs and the offer. A deal-breaker, or a critical failure
    with no estimate yet, means 'failed'."""
    res = c.inspection or {}
    if not res:
        return "not_started"
    if any(v.get("result") == "dealbreaker" for v in res.values()):
        return "failed"
    if any(v.get("result") == "fail" and not v.get("cost") for k, v in res.items() if k in CRITICAL):
        return "failed"
    if all(res.get(k, {}).get("result") in ("pass", "fail") for k in CRITICAL):
        return "passed_with_fixes" if any(v.get("result") == "fail" for v in res.values()) else "passed"
    return "in_progress"


def inspected(c) -> bool:
    return inspection_state(c) in ("passed", "passed_with_fixes")


@dataclass(frozen=True)
class Flag:
    level: str  # risk | caution | leverage
    text: str


def risk_flags(c, today: dt.date, *, peers: list = (), high_hours: int = 250) -> list[Flag]:
    flags: list[Flag] = []
    said = (c.seller_statements or "").lower()
    if c.mechanical_condition in ("good", "fair") and not inspected(c):
        flags.append(Flag("caution", "Mechanical condition is the seller's word only - not verified by inspection."))
    if any(p in said for p in SOFT_CLAIMS) and not inspected(c):
        flags.append(Flag("caution", "Seller uses soft claims ('runs great', 'just needs…') - verify with a compression "
                                     "and water test."))
    if any(p in said for p in SCAM_PHRASES):
        flags.append(Flag("risk", "Possible scam pattern in the seller's words (deposit / shipping / no title). "
                                  "Never pay before seeing the ski and the title in person."))
    if c.title_status in ("missing", "salvage", "lien"):
        flags.append(Flag("risk", f"Title is {c.title_status}. Don't buy until it's resolved and verified."))
    elif c.title_status == "unknown":
        flags.append(Flag("caution", "Title status unknown - ask for a photo of the title and the HIN."))
    if c.engine_hours is None:
        flags.append(Flag("caution", "Engine hours unknown."))
    elif c.engine_hours > high_hours:
        flags.append(Flag("caution", f"High hours ({c.engine_hours}) - budget for engine wear in a rental fleet."))
    if c.hull_condition == "poor":
        flags.append(Flag("risk", "Hull in poor condition."))
    if inspection_state(c) == "failed":
        flags.append(Flag("risk", "Inspection failed: a deal-breaker, or a critical problem with no fix estimate yet."))
    price = working_price(c)
    same = [working_price(p) for p in peers if p.id != c.id and p.model and p.model.lower() == (c.model or "").lower()
            and working_price(p)]
    if price and len(same) >= 2:
        med = Decimal(str(statistics.median(same)))
        if price < med * Decimal("0.6"):
            flags.append(Flag("risk", f"Price is far below similar listings (median ${med:,.0f}) - check for hidden "
                                      "damage, salvage or scam."))
    age = (today - c.first_seen).days
    if age >= 30 and c.stage in ACTIVE_STAGES:
        flags.append(Flag("leverage", f"Listed {age} days - the seller may take a lower offer."))
    hist = list(getattr(c, "price_history", []) or [])
    if len(hist) >= 2 and hist[-1].price < hist[0].price:
        flags.append(Flag("leverage", f"Price already dropped ${hist[0].price - hist[-1].price:,.0f} - room to negotiate."))
    if c.trailer_included == "no":
        flags.append(Flag("caution", "No trailer - add a trailer or transport cost."))
    return flags


@dataclass(frozen=True)
class Gate:
    key: str
    name: str
    passed: bool
    detail: str


def gates(c, *, max_price: Decimal | None, fund_balance: Decimal | None, fund_protected: bool | None,
          approved: bool) -> list[Gate]:
    cost = ready_to_rent_cost(c)
    price = working_price(c)
    g = []
    if max_price is None:
        g.append(Gate("budget", "Budget eligibility", False, "Set your maximum purchase price first."))
    elif price is None:
        g.append(Gate("budget", "Budget eligibility", False, "Price unknown."))
    else:
        g.append(Gate("budget", "Budget eligibility", price <= max_price,
                      f"${price:,.0f} vs your ${max_price:,.0f} ceiling"))
    g.append(Gate("title", "Ownership & title verified", c.title_status == "clean" and bool(c.title_verified),
                  "Clean title seen in person, matching the seller's ID and the hull's HIN"
                  if c.title_status == "clean" and c.title_verified else
                  f"Title: {c.title_status}{'' if c.title_verified else ' · not verified in person'}"))
    st = inspection_state(c)
    g.append(Gate("inspection", "Mechanical inspection", st in ("passed", "passed_with_fixes"),
                  f"Inspection {st.replace('_', ' ')}"))
    g.append(Gate("suitability", "Suits rentals", c.rental_suitable == "yes",
                  c.rental_note or {"unknown": "Not assessed (seats, reliability, parts availability)",
                                    "no": "Assessed: not suitable"}.get(c.rental_suitable, "")))
    g.append(Gate("total_cost", "Total cost known", cost.complete,
                  f"Ready to rent: {cost.label}" + ("" if cost.complete else f" · missing {', '.join(cost.missing)}")))
    if fund_balance is None:
        g.append(Gate("reserves", "Reserves protected", False, "Pick the fund this comes from (not a protected reserve)."))
    elif fund_protected:
        g.append(Gate("reserves", "Reserves protected", False, "That fund is a protected reserve - use a purchase fund."))
    else:
        ok = cost.complete and fund_balance >= cost.total
        g.append(Gate("reserves", "Reserves protected", ok,
                      f"Fund has ${fund_balance:,.0f} vs ${cost.total:,.0f} needed" if cost.complete
                      else "Total cost still unknown"))
    g.append(Gate("approval", "Your final approval", approved, "Approved" if approved else "Not yet approved"))
    return g


def next_step(c, gate_list: list[Gate]) -> str:
    if c.stage == "found":
        return "Message the seller: ask for HIN, a photo of the title, engine hours and service records."
    for gt in gate_list:
        if gt.passed:
            continue
        return {
            "budget": "Set a price that fits your ceiling - prepare an offer with a walk-away point.",
            "title": "Verify the title in person: name matches the seller's ID, HIN matches the hull.",
            "inspection": "Do the inspection: compression test on every cylinder and a water test.",
            "suitability": "Decide if it suits rentals (3-seat, reliable model, parts easy to get).",
            "total_cost": "Estimate repairs, transport and prep so the full cost is known.",
            "reserves": "Choose the purchase fund this comes from and check it covers the full cost.",
            "approval": "Everything checks out - make the final call.",
        }[gt.key]
    return "Ready - complete the purchase and record it."


def rank_key(c, gate_list: list[Gate], flags: list[Flag]):
    risks = sum(1 for f in flags if f.level == "risk")
    cautions = sum(1 for f in flags if f.level == "caution")
    cost = ready_to_rent_cost(c)
    # Red-flag risks sink a listing first (a cheap scam must never outrank a clean ski), then gates passed.
    return (risks, -sum(g.passed for g in gate_list), cautions, not cost.complete, cost.total, c.id)
