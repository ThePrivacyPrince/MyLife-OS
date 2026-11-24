"""Offer planning - pure, transparent arithmetic. Every number shows where it came from.

    reference      = asking price, or what similar units sell for if that's lower (never above asking)
    value to you   = reference - fix costs from the inspection
    walk-away      = the lowest of: value to you, your price ceiling, what your purchase fund can cover
    fair offer     = walk-away minus half the negotiating margin
    great offer    = walk-away minus the full negotiating margin (your opening number)

The negotiating margin starts at 8% and grows with each leverage signal, capped at 20%.
Deal-breakers (bad title, scam signs, items you marked 'walk away') produce no prices at all.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from lifeos.engines import acquisition as A

BASE_MARGIN = Decimal("0.08")
MAX_MARGIN = Decimal("0.20")
OFF_SEASON_MONTHS = (10, 11, 12, 1, 2, 3)
STEP = Decimal(25)


def round_down(x: Decimal, step: Decimal = STEP) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


@dataclass
class OfferPlan:
    dealbreakers: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    reference: Decimal | None = None
    reference_basis: str = ""
    deductions: list[tuple[str, Decimal]] = field(default_factory=list)
    value_to_you: Decimal | None = None
    caps: list[tuple[str, Decimal]] = field(default_factory=list)
    signals: list[tuple[str, Decimal]] = field(default_factory=list)
    margin: Decimal = BASE_MARGIN
    walk_away: Decimal | None = None
    fair: Decimal | None = None
    great: Decimal | None = None
    notes: list[str] = field(default_factory=list)
    preliminary: bool = False  # worked out before the inspection is done - never commit on these

    @property
    def ready(self) -> bool:
        return not self.dealbreakers and not self.incomplete and self.walk_away is not None


def findings(c) -> list[dict]:
    out = []
    res = c.inspection or {}
    for key, section, text, crit in A.PROTOCOL:
        r = res.get(key, {})
        if r.get("result") in ("fail", "dealbreaker"):
            cost = r.get("cost")
            out.append({"key": key, "section": section, "text": text, "critical": crit, "result": r["result"],
                        "cost": Decimal(cost) if cost not in (None, "") else None, "note": r.get("note", "")})
    return out


def plan_offers(c, *, today: dt.date, comps_median: Decimal | None, ceiling: Decimal | None,
                fund_balance: Decimal | None, high_hours: int = 250) -> OfferPlan:
    p = OfferPlan(preliminary=not A.inspected(c))
    said = (c.seller_statements or "").lower()
    # Deal-breakers: don't negotiate at all.
    if c.title_status in ("missing", "salvage", "lien"):
        p.dealbreakers.append(f"Title is {c.title_status}.")
    if any(x in said for x in A.SCAM_PHRASES):
        p.dealbreakers.append("Scam-pattern wording from the seller (deposit / shipping / no title).")
    if c.hull_condition == "poor":
        p.dealbreakers.append("Hull in poor condition.")
    found = findings(c)
    for f in found:
        if f["result"] == "dealbreaker":
            p.dealbreakers.append(f"You marked '{f['text']}' as a walk-away issue.")
    if p.dealbreakers:
        return p

    if c.asking_price is None:
        p.incomplete.append("Asking price is unknown.")
        return p
    market = c.market_value or comps_median
    if market is not None and market < c.asking_price:
        p.reference = market
        p.reference_basis = ("your market research" if c.market_value else "the median of similar listings") + \
            f" (${market:,.0f}) - lower than the ${c.asking_price:,.0f} asking"
    else:
        p.reference = c.asking_price
        p.reference_basis = "the asking price" + (f" (similar units go for about ${market:,.0f})" if market else
                                                  " - add your market research or more listings of this model")
    for f in found:
        if f["cost"] is None:
            p.incomplete.append(f"Estimate the fix cost for: {f['text']} (or mark it 'walk away').")
        else:
            p.deductions.append((f"{f['text']}" + (f" - {f['note']}" if f["note"] else ""), f["cost"]))
    if p.incomplete:
        return p
    p.value_to_you = p.reference - sum((d for _, d in p.deductions), Decimal(0))

    caps = [("What it's worth to you", p.value_to_you)]
    if ceiling is not None:
        caps.append(("Your price ceiling", ceiling))
    else:
        p.notes.append("No price ceiling set - add one in Deal settings.")
    if fund_balance is not None:
        other = sum((x for x in (c.transport_cost, c.prep_cost) if x is not None), Decimal(0))
        repairs = sum((d for _, d in p.deductions), Decimal(0))
        caps.append(("What your purchase fund can cover after repairs, transport and prep",
                     fund_balance - repairs - other))
        if c.transport_cost is None or c.prep_cost is None:
            p.notes.append("Transport or prep cost unknown - the fund limit assumes $0 for them.")
    p.caps = caps
    walk = min(v for _, v in caps)
    if walk <= 0:
        p.notes.append("Nothing left to spend once repairs and costs are covered - walk away or fund it first.")
        p.walk_away = Decimal(0)
        return p

    # Leverage signals widen the margin between your opening offer and your walk-away.
    age = (today - c.first_seen).days
    if age >= 30:
        p.signals.append((f"Listed {age} days", Decimal("0.03")))
    hist = list(getattr(c, "price_history", []) or [])
    if len(hist) >= 2 and hist[-1].price < hist[0].price:
        p.signals.append((f"Price already dropped ${hist[0].price - hist[-1].price:,.0f}", Decimal("0.02")))
    if today.month in OFF_SEASON_MONTHS:
        p.signals.append(("Off-season - fewer buyers in the cold months", Decimal("0.03")))
    if c.cash_ready:
        p.signals.append(("You can pay cash and pick up quickly", Decimal("0.02")))
    if any(not f["critical"] for f in found):
        p.signals.append(("Minor issues found in the inspection", Decimal("0.02")))
    if c.engine_hours is not None and c.engine_hours > high_hours:
        p.signals.append((f"High hours ({c.engine_hours})", Decimal("0.02")))
    p.margin = min(MAX_MARGIN, BASE_MARGIN + sum((x for _, x in p.signals), Decimal(0)))

    p.walk_away = round_down(walk)
    p.fair = round_down(p.walk_away * (1 - p.margin / 2))
    p.great = round_down(p.walk_away * (1 - p.margin))
    if c.asking_price <= p.great:
        p.notes.append("The asking price is already at or below your great offer - verify everything and buy.")
    elif p.walk_away < c.asking_price * Decimal("0.7"):
        p.notes.append("Your walk-away is far below the asking price - expect a 'no' and be ready to leave.")
    return p


def talk_track(c, p: OfferPlan) -> str:
    if p.dealbreakers:
        return "Don't negotiate. Thank them and walk away: " + " ".join(p.dealbreakers)
    if not p.ready or p.great is None:
        return "Finish the inspection and costs first - the offer depends on them."
    if p.preliminary:
        lines = ["(Before the inspection - keep it non-committal.)",
                 "Hi - I'm interested in the ski. Before I make an offer I'd like to see it in person, check the title "
                 "and HIN, and do a compression and water test. When could I come look?",
                 f"If they ask for a number: \"Depending on the inspection, I'm thinking around ${p.great:,.0f}.\"",
                 f"(For you only: preliminary walk-away ${p.walk_away:,.0f} - it will change once you've inspected it.)"]
        return "\n\n".join(lines)
    lines = ["Thanks for letting me look it over - I'm interested" +
             (" and I can pay cash and pick it up right away." if c.cash_ready else ".")]
    fixes = [(t, d) for t, d in p.deductions if d > 0]
    if fixes:
        def say(label: str) -> str:  # prefer your own inspection note - it sounds natural out loud
            item, _, note = label.partition(" - ")
            return (note or item.split(":")[0]).strip().rstrip(".").lower()
        items = "; ".join(f"{say(t)} (about ${d:,.0f})" for t, d in fixes)
        lines.append(f"A few things came up in the inspection: {items}. "
                     f"That's about ${sum(d for _, d in fixes):,.0f} to put right.")
    lev = [s for s, _ in p.signals if not s.startswith("You can pay")]
    if lev:
        lines.append("It's also " + ", and ".join(x[0].lower() + x[1:] for x in lev[:2]) + ".")
    lines.append(f"With that in mind, I can do ${p.great:,.0f}.")
    lines.append(f"If they push back: \"The most I can stretch to is ${p.fair:,.0f}.\"")
    lines.append(f"(For you only - don't say it: above ${p.walk_away:,.0f}, thank them and leave. Another ski will come.)")
    return "\n\n".join(lines)
