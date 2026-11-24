"""Acquisition pipeline: candidates, inspection, the seven purchase gates, ranking, fleet (spec section 10)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.engines import acquisition as A
from lifeos.errors import NotFound, ReasonRequired, ValidationError
from lifeos.models import AcquisitionCandidate, CandidatePrice, Fund, PlanEvent
from lifeos.services import money as M

MONEY_FIELDS = ("asking_price", "est_repairs", "transport_cost", "prep_cost", "offer", "agreed_price", "market_value")
CHOICE_FIELDS = {"hull_condition": A.CONDITIONS, "mechanical_condition": A.CONDITIONS,
                 "title_status": A.TITLE_STATUSES, "trailer_included": A.YES_NO, "rental_suitable": A.YES_NO}
TEXT_FIELDS = ("title", "source", "url", "manufacturer", "model", "location", "seller_statements", "rental_note",
               "notes")


def _log(s: Session, c: AcquisitionCandidate, event: str, frm=None, to=None, reason=None) -> None:
    s.add(PlanEvent(entity_type="candidate", entity_code=f"C{c.id}", event_type=event,
                    from_value=None if frm is None else str(frm)[:200], to_value=None if to is None else str(to)[:200],
                    reason=reason))


def get(s: Session, cid: int) -> AcquisitionCandidate:
    c = s.get(AcquisitionCandidate, cid)
    if c is None:
        raise NotFound(f"Candidate {cid} not found.")
    return c


def _money_or_none(v):
    return None if v in (None, "") else M.money(v)


def _apply(c: AcquisitionCandidate, values: dict) -> list[str]:
    changed = []
    for k, v in values.items():
        if k in MONEY_FIELDS:
            v = _money_or_none(v)
        elif k in CHOICE_FIELDS:
            if v not in CHOICE_FIELDS[k]:
                raise ValidationError(f"{k.replace('_', ' ')} must be one of {CHOICE_FIELDS[k]}.")
        elif k in ("year", "engine_hours"):
            v = None if v in (None, "", 0) else int(v)
            if v is not None and v < 0:
                raise ValidationError(f"{k.replace('_', ' ')} can't be negative.")
            if k == "year" and v is not None and not 1980 <= v <= dt.date.today().year + 1:
                raise ValidationError("That year doesn't look right.")
        elif k in TEXT_FIELDS:
            v = (v or "").strip()
        elif k in ("title_verified", "cash_ready"):
            v = bool(v)
        else:
            raise ValidationError(f"Unknown field '{k}'.")
        if getattr(c, k) != v:
            setattr(c, k, v)
            changed.append(k)
    return changed


def add_candidate(s: Session, *, today: dt.date, title: str, **values) -> AcquisitionCandidate:
    if not title or not title.strip():
        raise ValidationError("Give the listing a name, e.g. '2019 Yamaha VX Cruiser - Newburgh'.")
    c = AcquisitionCandidate(title=title.strip(), first_seen=today, last_verified=today)
    _apply(c, values)
    s.add(c)
    s.flush()
    if c.asking_price is not None:
        s.add(CandidatePrice(candidate_id=c.id, day=today, price=c.asking_price, note="First seen"))
    _log(s, c, "added", to=c.title)
    s.flush()
    return c


def update_candidate(s: Session, cid: int, *, today: dt.date, **values) -> list[str]:
    c = get(s, cid)
    old_price = c.asking_price
    changed = _apply(c, values)
    if "asking_price" in changed and c.asking_price is not None:
        s.add(CandidatePrice(candidate_id=c.id, day=today, price=c.asking_price,
                             note=f"Was ${old_price:,.0f}" if old_price is not None else "Price added"))
        s.expire(c, ["price_history"])
    if changed:
        c.last_verified = today
        _log(s, c, "updated", to=", ".join(changed))
    s.flush()
    return changed


def set_inspection(s: Session, cid: int, item: str, result: str, note: str = "", cost=None) -> None:
    """Record one protocol item. A fix-cost estimate on failed items feeds repairs and the offer math."""
    keys = {k for k, _, _, _ in A.PROTOCOL}
    if item not in keys:
        raise ValidationError(f"Unknown inspection item '{item}'.")
    if result not in A.RESULTS:
        raise ValidationError("Result is pass, fail, dealbreaker or na.")
    if result == "na" and item in A.CRITICAL:
        raise ValidationError("A critical item can't be skipped as N/A.")
    c = get(s, cid)
    data = dict(c.inspection or {})
    if result == "":
        data.pop(item, None)
    else:
        entry = {"result": result, "note": (note or "").strip()}
        if result == "fail" and cost not in (None, ""):
            entry["cost"] = str(M.money(cost))
        data[item] = entry
    c.inspection = data
    costs = [Decimal(v["cost"]) for v in data.values() if v.get("result") == "fail" and v.get("cost")]
    if costs:
        c.est_repairs = sum(costs, Decimal(0))
    elif not any(v.get("result") == "fail" for v in data.values()) and data and c.est_repairs is None \
            and A.inspection_state(c) == "passed":  # clean pass
        c.est_repairs = Decimal(0)  # clean inspection: no known repairs
    if c.stage in ("found", "contacted") and data:
        c.stage = "inspecting"
    _log(s, c, "inspection", to=f"{item}={result}" + (f" ${cost}" if cost not in (None, "") else ""), reason=note or None)
    s.flush()


def set_funding(s: Session, cid: int, fund_id: int | None) -> None:
    c = get(s, cid)
    if fund_id is not None:
        f = M.get_fund(s, fund_id)
        if f.kind != "save":
            raise ValidationError("Pick a savings fund.")
    c.funding_fund_id = fund_id
    s.flush()
    s.refresh(c)


def set_stage(s: Session, cid: int, stage: str, reason: str = "") -> None:
    c = get(s, cid)
    if stage not in ("found", "contacted", "inspecting", "negotiating", "passed"):
        raise ValidationError("Use approve / mark bought for those stages.")
    if stage == "passed" and not reason.strip():
        raise ReasonRequired("Why are you passing on it? (it helps spot patterns in bad listings)")
    _log(s, c, "stage", frm=c.stage, to=stage, reason=reason or None)
    c.stage = stage
    s.flush()


# ---------------------------------------------------------------- settings (local database only)
def settings(s: Session) -> dict:
    get_ = lambda k: M.get_setting(s, k)  # noqa: E731
    return {"max_price": Decimal(get_("acq_max_price")) if get_("acq_max_price") else None,
            "fleet_target": int(get_("acq_fleet_target")) if get_("acq_fleet_target") else None,
            "high_hours": int(get_("acq_high_hours")) if get_("acq_high_hours") else 250}


def save_settings(s: Session, *, max_price=None, fleet_target=None, high_hours=None) -> None:
    if max_price not in (None, ""):
        M.set_setting(s, "acq_max_price", str(M.money(max_price)))
    if fleet_target not in (None, ""):
        if int(fleet_target) < 0:
            raise ValidationError("Fleet target can't be negative.")
        M.set_setting(s, "acq_fleet_target", str(int(fleet_target)))
    if high_hours not in (None, ""):
        M.set_setting(s, "acq_high_hours", str(int(high_hours)))


# ---------------------------------------------------------------- evaluation
@dataclass
class Evaluation:
    candidate: AcquisitionCandidate
    cost: A.Cost
    flags: list
    gates: list
    next_step: str
    facts_known: int
    facts_total: int
    facts_missing: list

    @property
    def gates_passed(self) -> int:
        return sum(g.passed for g in self.gates)

    @property
    def ready_for_approval(self) -> bool:
        return all(g.passed for g in self.gates if g.key != "approval")


def evaluate(s: Session, c: AcquisitionCandidate, today: dt.date, peers: list | None = None) -> Evaluation:
    cfg = settings(s)
    peers = peers if peers is not None else list(s.scalars(select(AcquisitionCandidate)))
    fund = c.funding_fund
    gl = A.gates(c, max_price=cfg["max_price"], fund_balance=M.balance(fund) if fund else None,
                 fund_protected=(fund.space in M.PROTECTED_SPACES) if fund else None,
                 approved=c.stage in ("approved", "owned"))
    known, total, missing = A.known_facts(c)
    return Evaluation(c, A.ready_to_rent_cost(c), A.risk_flags(c, today, peers=peers, high_hours=cfg["high_hours"]),
                      gl, A.next_step(c, gl), known, total, missing)


def ranked(s: Session, today: dt.date) -> list[Evaluation]:
    peers = list(s.scalars(select(AcquisitionCandidate)))
    evs = [evaluate(s, c, today, peers) for c in peers if c.stage in A.ACTIVE_STAGES]
    return sorted(evs, key=lambda e: A.rank_key(e.candidate, e.gates, e.flags))


# ---------------------------------------------------------------- decision
def approve(s: Session, cid: int, today: dt.date, reason: str = "", override_reason: str = "") -> None:
    c = get(s, cid)
    ev = evaluate(s, c, today)
    failing = [g for g in ev.gates if not g.passed and g.key != "approval"]
    if failing and not override_reason.strip():
        raise ValidationError("Not ready: " + "; ".join(f"{g.name} ({g.detail})" for g in failing) +
                              ". Fix these, or record an override reason.")
    _log(s, c, "approved", frm=c.stage, to="approved",
         reason=(f"OVERRIDE ({', '.join(g.key for g in failing)}): {override_reason.strip()}" if failing
                 else reason.strip() or None))
    c.stage = "approved"
    s.flush()


def mark_bought(s: Session, cid: int, price, today: dt.date, note: str = "") -> None:
    c = get(s, cid)
    if c.stage != "approved":
        raise ValidationError("Approve it first - that's the final gate.")
    paid = M.money(price)
    c.agreed_price, c.stage = paid, "owned"
    if c.funding_fund:
        cost = A.ready_to_rent_cost(c).total
        take = min(cost, M.balance(c.funding_fund))
        if take > 0:
            M.add_entry(s, c.funding_fund.id, "withdraw", take, note=f"Bought: {c.title}. {note}".strip(), today=today)
    _log(s, c, "bought", to=f"${paid:,.0f}", reason=note or None)
    s.flush()


def add_owned(s: Session, *, title: str, today: dt.date, **values) -> AcquisitionCandidate:
    """Record a unit you already own so the fleet count is right."""
    c = add_candidate(s, today=today, title=title, **values)
    c.stage = "owned"
    _log(s, c, "owned", reason="Existing fleet unit")
    s.flush()
    return c


def fleet(s: Session) -> list[AcquisitionCandidate]:
    return list(s.scalars(select(AcquisitionCandidate).where(AcquisitionCandidate.stage == "owned")
                          .order_by(AcquisitionCandidate.id)))


# ---------------------------------------------------------------- negotiation
def comparable_prices(s: Session, c: AcquisitionCandidate) -> list[Decimal]:
    """Prices of other listings of the same model that have no red flags (a scam or bad title never sets 'market')."""
    out = []
    for x in s.scalars(select(AcquisitionCandidate)):
        if x.id == c.id or not (x.model and c.model) or x.model.lower() != c.model.lower() or x.stage == "passed":
            continue
        if not A.working_price(x):
            continue
        if any(f.level == "risk" for f in A.risk_flags(x, x.first_seen)):
            continue
        out.append(A.working_price(x))
    return out


def comps_median(s: Session, c: AcquisitionCandidate) -> Decimal | None:
    import statistics
    prices = comparable_prices(s, c)
    return Decimal(str(statistics.median(prices))) if len(prices) >= 2 else None


def offer_plan(s: Session, c: AcquisitionCandidate, today: dt.date):
    from lifeos.engines.negotiation import plan_offers
    cfg = settings(s)
    fund = c.funding_fund
    bal = M.balance(fund) if fund and fund.space not in M.PROTECTED_SPACES else None
    plan = plan_offers(c, today=today, comps_median=comps_median(s, c), ceiling=cfg["max_price"],
                       fund_balance=bal, high_hours=cfg["high_hours"])
    n = len(comparable_prices(s, c))
    if "median of similar listings" in plan.reference_basis:
        plan.reference_basis = plan.reference_basis.replace("the median of similar listings",
                                                            f"the median of {n} similar clean listings")
    return plan


RESPONSES = ("accepted", "countered", "declined", "no_reply")


def record_offer(s: Session, cid: int, offer, response: str, counter=None, note: str = "") -> None:
    c = get(s, cid)
    if response not in RESPONSES:
        raise ValidationError(f"Response is one of {RESPONSES}.")
    amt = M.money(offer)
    ctr = M.money(counter) if counter not in (None, "") else None
    if response == "countered" and ctr is None:
        raise ValidationError("What did they counter with?")
    c.offer = amt
    if response == "accepted":
        c.agreed_price = amt
    if c.stage in ("found", "contacted", "inspecting"):
        c.stage = "negotiating"
    _log(s, c, "negotiation", frm=f"offered ${amt:,.0f}", to=response + (f" ${ctr:,.0f}" if ctr else ""),
         reason=note.strip() or None)
    s.flush()


def negotiation_log(s: Session, c: AcquisitionCandidate) -> list[PlanEvent]:
    return list(s.scalars(select(PlanEvent).where(PlanEvent.entity_type == "candidate",
                                                  PlanEvent.entity_code == f"C{c.id}",
                                                  PlanEvent.event_type == "negotiation").order_by(PlanEvent.id)))
