"""Money buckets: saving up, paying down, and tracking spending - kept in separate spaces.

All arithmetic is Decimal (never floats). LIFE OS tracks money; it never moves it. Every entry is logged,
and taking money back out of a fund needs a reason.

Frugality tools: a 14-day cool-off on wants, "Before you buy" (price in hours of work), a running total of
money kept by not buying, and a paycheck splitter that funds obligations before wants.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings, utcnow
from lifeos.engines.cascade import parse_amount
from lifeos.errors import NotFound, ReasonRequired, ValidationError
from lifeos.models import Fund, FundEntry, ImpulseCheck, PlanEvent, UserSetting

SPACES = {
    "family": "👦 Family",
    "holiday": "🎄 Holidays & gifts",
    "wants": "🎁 Wants",
    "reserves": "🛡️ Reserves",
    "business": "💼 Business",
    "owed": "💳 Money I owe",
}
KINDS = ("save", "debt", "spend")
DEFAULT_KIND = {"family": "spend", "holiday": "save", "wants": "save", "reserves": "save", "business": "save",
                "owed": "debt"}
# Funding order for the paycheck splitter: obligations first, wants last.
FUNDING_ORDER = ("owed", "reserves", "family", "holiday", "business", "wants")
PROTECTED_SPACES = ("owed", "reserves")
ZERO = Decimal(0)
CENT = Decimal("0.01")


def money(value) -> Decimal:
    d = parse_amount(value)
    if d < 0:
        raise ValidationError("Use a positive amount - the action says which way the money moves.")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(d: Decimal | None) -> str:
    if d is None:
        return "-"
    return f"${d:,.2f}".replace(".00", "")


def _log(s: Session, f: Fund, event: str, frm=None, to=None, reason=None) -> None:
    s.add(PlanEvent(entity_type="fund", entity_code=f"F{f.id}", event_type=event,
                    from_value=None if frm is None else str(frm)[:200], to_value=None if to is None else str(to)[:200],
                    reason=reason))


def get_fund(s: Session, fund_id: int) -> Fund:
    f = s.get(Fund, fund_id)
    if f is None:
        raise NotFound(f"Fund {fund_id} not found.")
    return f


# ---------------------------------------------------------------- funds
def create_fund(s: Session, *, name: str, space: str, kind: str | None = None, target=None,
                deadline: dt.date | None = None, counterparty: str = "", kept_where: str = "",
                notes: str = "", today: dt.date | None = None) -> Fund:
    if not name or not name.strip():
        raise ValidationError("Give it a name.")
    if space not in SPACES:
        raise ValidationError(f"Unknown space '{space}'.")
    kind = kind or DEFAULT_KIND[space]
    if kind not in KINDS:
        raise ValidationError(f"Kind is one of {KINDS}.")
    if space == "owed" and kind != "debt":
        raise ValidationError("'Money I owe' holds debts only.")
    if kind == "debt" and space != "owed":
        raise ValidationError("Debts live in 'Money I owe', so they never sit next to your other funds.")
    tgt = money(target) if target not in (None, "") else None
    if kind in ("save", "debt") and not tgt:
        raise ValidationError("How much? Saving needs a target; a debt needs the amount owed.")
    today = today or get_settings().today()
    if deadline and deadline < today:
        raise ValidationError("Deadline is in the past.")
    f = Fund(name=name.strip(), space=space, kind=kind, target=tgt, deadline=deadline,
             counterparty=counterparty.strip(), kept_where=kept_where.strip(), notes=notes)
    s.add(f)
    s.flush()
    _log(s, f, "created", to=f"{kind} {fmt(tgt)}")
    return f


def update_fund(s: Session, fund_id: int, *, name: str | None = None, target=..., deadline=...,
                kept_where: str | None = None, counterparty: str | None = None, reason: str = "") -> Fund:
    f = get_fund(s, fund_id)
    if name is not None and name.strip() and name.strip() != f.name:
        _log(s, f, "renamed", frm=f.name, to=name.strip())
        f.name = name.strip()
    if target is not ...:
        new = money(target) if target not in (None, "") else None
        if new != f.target:
            if f.kind in ("save", "debt") and not new:
                raise ValidationError("A target is required for this fund.")
            _log(s, f, "target", frm=fmt(f.target), to=fmt(new), reason=reason or None)
            f.target = new
    if deadline is not ... and deadline != f.deadline:
        if f.deadline is not None and not reason.strip():
            raise ReasonRequired("Moving a deadline needs a reason.")
        _log(s, f, "deadline", frm=f.deadline, to=deadline, reason=reason or None)
        f.deadline = deadline
    if kept_where is not None:
        f.kept_where = kept_where.strip()
    if counterparty is not None:
        f.counterparty = counterparty.strip()
    s.flush()
    return f


# ---------------------------------------------------------------- entries
ENTRY_KINDS = {"save": ("add", "withdraw", "purchase"), "debt": ("payment", "charge"), "spend": ("spend",)}


def add_entry(s: Session, fund_id: int, kind: str, amount, *, day: dt.date | None = None, note: str = "",
              today: dt.date | None = None) -> FundEntry:
    f = get_fund(s, fund_id)
    if f.status != "active":
        raise ValidationError("That fund is closed.")
    if kind not in ENTRY_KINDS[f.kind]:
        raise ValidationError(f"'{kind}' doesn't apply to a {f.kind} fund.")
    amt = money(amount)
    if amt == 0:
        raise ValidationError("Amount must be more than zero.")
    today = today or get_settings().today()
    day = day or today
    if day > today:
        raise ValidationError("Record money on the day it happened, not in the future.")
    if kind == "withdraw":
        if not note.strip():
            raise ReasonRequired("Taking money out of a fund needs a reason.")
        if amt > balance(f):
            raise ValidationError(f"Only {fmt(balance(f))} is in this fund.")
    if kind == "charge" and not note.strip():
        raise ReasonRequired("What was the new charge for?")
    e = FundEntry(fund_id=f.id, day=day, kind=kind, amount=amt, note=note.strip())
    s.add(e)
    s.flush()
    s.expire(f, ["entries"])
    if f.kind == "debt" and remaining(f) <= 0:
        f.status, f.closed_at = "done", utcnow()
        _log(s, f, "paid_off", to=fmt(paid(f)))
    return e


# ---------------------------------------------------------------- reading a fund
def _sum(f: Fund, *kinds: str, until: dt.date | None = None) -> Decimal:
    return sum((e.amount for e in f.entries if e.kind in kinds and (until is None or e.day <= until)), ZERO)


def balance(f: Fund, until: dt.date | None = None) -> Decimal:
    """Savings: money currently set aside."""
    return _sum(f, "add", until=until) - _sum(f, "withdraw", "purchase", until=until)


def paid(f: Fund) -> Decimal:
    return _sum(f, "payment")


def owed_total(f: Fund) -> Decimal:
    return (f.target or ZERO) + _sum(f, "charge")


def remaining(f: Fund) -> Decimal:
    """Debts: still owed. Savings: still to save."""
    if f.kind == "debt":
        return max(ZERO, owed_total(f) - paid(f))
    if f.kind == "save":
        return max(ZERO, (f.target or ZERO) - balance(f))
    return ZERO


def spent_between(f: Fund, lo: dt.date, hi: dt.date) -> Decimal:
    return sum((e.amount for e in f.entries if e.kind == "spend" and lo <= e.day <= hi), ZERO)


def month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    lo = day.replace(day=1)
    nxt = (lo + dt.timedelta(days=32)).replace(day=1)
    return lo, nxt - dt.timedelta(days=1)


@dataclass(frozen=True)
class Progress:
    done: Decimal      # saved / paid / spent this month
    goal: Decimal | None
    fraction: float
    weekly_needed: Decimal | None   # to hit the deadline
    pace: str          # on_track | behind | done | no_deadline | over_budget | under_budget | no_budget
    eta: dt.date | None  # at the recent pace


def progress(f: Fund, today: dt.date) -> Progress:
    if f.kind == "spend":
        lo, hi = month_bounds(today)
        spent = spent_between(f, lo, hi)
        if not f.target:
            return Progress(spent, None, 0.0, None, "no_budget", None)
        frac = float(min(spent / f.target, Decimal(1)))
        return Progress(spent, f.target, frac, None, "over_budget" if spent > f.target else "under_budget", None)
    done = paid(f) if f.kind == "debt" else balance(f)
    goal = owed_total(f) if f.kind == "debt" else (f.target or ZERO)
    frac = float(min(done / goal, Decimal(1))) if goal else 0.0
    left = max(ZERO, goal - done)
    if left == 0:
        return Progress(done, goal, 1.0, ZERO, "done", None)
    weekly = None
    pace = "no_deadline"
    if f.deadline:
        weeks = max(Decimal(1), Decimal((f.deadline - today).days) / Decimal(7))
        weekly = (left / weeks).quantize(CENT, rounding=ROUND_HALF_UP)
        start = f.created_at.date() if f.created_at else today
        span = max(1, (f.deadline - start).days)
        expected = goal * Decimal(min(max((today - start).days, 0), span)) / Decimal(span)
        pace = "behind" if done < expected.quantize(CENT) and (today - start).days >= 7 else "on_track"
    # ETA from the last 28 days of progress
    since = today - dt.timedelta(days=28)
    kinds = ("payment",) if f.kind == "debt" else ("add",)
    recent = sum((e.amount for e in f.entries if e.kind in kinds and e.day > since), ZERO)
    if f.kind == "save":
        recent -= sum((e.amount for e in f.entries if e.kind == "withdraw" and e.day > since), ZERO)
    eta = None
    if recent > 0:
        per_day = recent / Decimal(28)
        eta = today + dt.timedelta(days=int((left / per_day).to_integral_value(rounding="ROUND_CEILING")))
    return Progress(done, goal, frac, weekly, pace, eta)


def full_since(f: Fund) -> dt.date | None:
    """The day a savings fund first reached its target (and has stayed there)."""
    if f.kind != "save" or not f.target:
        return None
    running, reached = ZERO, None
    for e in sorted(f.entries, key=lambda e: (e.day, e.id)):
        running += e.amount if e.kind == "add" else -e.amount
        if running >= f.target and reached is None:
            reached = e.day
        elif running < f.target:
            reached = None
    return reached


def cooloff_ends(f: Fund) -> dt.date | None:
    """Wants unlock 'Bought it' after the cool-off, counted from the day the fund filled up."""
    if f.space != "wants":
        return None
    full = full_since(f)
    return None if full is None else full + dt.timedelta(days=get_settings().money.cooloff_days)


def mark_bought(s: Session, fund_id: int, price, *, today: dt.date, note: str = "", override_reason: str = "") -> Fund:
    f = get_fund(s, fund_id)
    if f.kind != "save":
        raise ValidationError("Only savings funds are 'bought'.")
    unlock = cooloff_ends(f)
    if f.space == "wants":
        if unlock is None:
            if not override_reason.strip():
                raise ValidationError("This want isn't fully saved yet. Keep filling it, or record a reason to buy now.")
        elif today < unlock and not override_reason.strip():
            raise ValidationError(f"Cool-off ends {unlock:%b} {unlock.day}. Still want it then? Buy it then.")
    paid_price = money(price)
    take = min(paid_price, balance(f))
    if take > 0:
        s.add(FundEntry(fund_id=f.id, day=today, kind="purchase", amount=take, note=note.strip()))
    f.paid_price, f.status, f.closed_at = paid_price, "done", utcnow()
    _log(s, f, "bought", to=fmt(paid_price),
         reason=(f"Before cool-off: {override_reason.strip()}" if override_reason.strip() else note or None))
    s.flush()
    s.expire(f, ["entries"])
    return f


def drop_fund(s: Session, fund_id: int, reason: str, *, today: dt.date) -> Fund:
    """Decided not to buy / no longer needed. For wants, what you'd saved counts as kept."""
    f = get_fund(s, fund_id)
    if not reason.strip():
        raise ReasonRequired("Why are you dropping it?")
    if f.kind == "debt" and remaining(f) > 0:
        raise ValidationError("A debt is closed by paying it off (or recording a settlement as a payment).")
    if f.space == "wants" and f.target:
        s.add(ImpulseCheck(day=today, item=f.name, price=f.target, decision="skip",
                           note=f"Dropped from wish list: {reason.strip()}"))
    f.status, f.closed_at = "dropped", utcnow()
    _log(s, f, "dropped", reason=reason.strip())
    s.flush()
    return f


def funds_in(s: Session, space: str, include_closed: bool = False) -> list[Fund]:
    rows = s.scalars(select(Fund).where(Fund.space == space).order_by(Fund.deadline.is_(None), Fund.deadline, Fund.id))
    return [f for f in rows if include_closed or f.status == "active"]


# ---------------------------------------------------------------- frugality
def get_setting(s: Session, key: str) -> str | None:
    row = s.get(UserSetting, key)
    return row.value if row else None


def set_setting(s: Session, key: str, value: str) -> None:
    row = s.get(UserSetting, key) or UserSetting(key=key, value="")
    row.value = value
    s.add(row)
    s.flush()


def set_hourly_rate(s: Session, rate) -> None:
    set_setting(s, "hourly_take_home", str(money(rate)))


def hourly_rate(s: Session) -> Decimal | None:
    v = get_setting(s, "hourly_take_home")
    return Decimal(v) if v else None


def hours_of_work(s: Session, price) -> Decimal | None:
    rate = hourly_rate(s)
    if not rate:
        return None
    return (money(price) / rate).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def impulse_check(s: Session, *, item: str, price, decision: str, today: dt.date, note: str = "") -> ImpulseCheck | Fund:
    """skip -> counted as kept · wishlist -> becomes a Wants fund (the cool-off starts once it's saved) · buy -> logged."""
    if not item or not item.strip():
        raise ValidationError("What is it?")
    amt = money(price)
    if decision not in ("skip", "wishlist", "buy"):
        raise ValidationError("Decision is skip, wishlist or buy.")
    row = ImpulseCheck(day=today, item=item.strip(), price=amt, decision=decision, note=note.strip())
    s.add(row)
    s.flush()
    if decision == "wishlist":
        return create_fund(s, name=item.strip(), space="wants", target=amt, today=today, notes="From 'Before you buy'")
    return row


def kept_total(s: Session, lo: dt.date | None = None, hi: dt.date | None = None) -> Decimal:
    rows = s.scalars(select(ImpulseCheck).where(ImpulseCheck.decision == "skip"))
    return sum((r.price for r in rows if (lo is None or r.day >= lo) and (hi is None or r.day <= hi)), ZERO)


def set_cash_on_hand(s: Session, amount, day: dt.date) -> None:
    set_setting(s, "cash_on_hand", f"{money(amount)}|{day.isoformat()}")


def cash_check(s: Session) -> tuple[Decimal | None, dt.date | None, Decimal]:
    """(cash on hand you reported, when, total set aside in savings funds)."""
    v = get_setting(s, "cash_on_hand")
    set_aside = sum((balance(f) for f in s.scalars(select(Fund).where(Fund.kind == "save", Fund.status == "active"))),
                    ZERO)
    if not v:
        return None, None, set_aside
    amt, day = v.split("|")
    return Decimal(amt), dt.date.fromisoformat(day), set_aside


def behind_protected(s: Session, today: dt.date) -> list[Fund]:
    """Debts and reserves behind their deadline pace - shown next to wants as a gentle 'fill these first?'."""
    out = []
    for space in PROTECTED_SPACES:
        for f in funds_in(s, space):
            if progress(f, today).pace == "behind":
                out.append(f)
    return out


@dataclass(frozen=True)
class Split:
    fund: Fund
    suggested: Decimal
    why: str


def suggest_split(s: Session, amount, today: dt.date) -> tuple[list[Split], Decimal]:
    """Suggest how to divide money that just came in: obligations first, wants last.
    Each fund with a deadline is offered its weekly pace (or what's left, if less); others get 0 to fill by hand."""
    left = money(amount)
    out: list[Split] = []
    from lifeos.services import debt_plan as DP  # payday rule: a set % of every payday to the target debt
    rule = DP.payday_amount(s, amount)
    target = DP.plan(s).target if rule else None
    for space in FUNDING_ORDER:
        for f in funds_in(s, space):
            if f.kind == "spend":
                continue
            p = progress(f, today)
            if p.pace == "done":
                continue
            need = remaining(f)
            if p.weekly_needed is not None:
                want = min(need, p.weekly_needed)
                why = f"{fmt(p.weekly_needed)}/week hits {f.deadline:%b} {f.deadline.day}"
            else:
                want, why = ZERO, "no deadline - your call"
            if target is not None and f.id == target.id and rule > want:
                want, why = min(need, rule), f"payday rule: {DP.setting(s, 'payday_pct')}% to your target debt"
            give = min(want, left)
            left -= give
            out.append(Split(f, give, why if give == want else f"{why} (only {fmt(give)} left to give)"))
    return out, left


def apply_split(s: Session, allocations: dict[int, Decimal | str], today: dt.date, note: str = "Paycheck split") -> int:
    n = 0
    for fid, amt in allocations.items():
        a = money(amt) if amt not in (None, "") else ZERO
        if a > 0:
            f = get_fund(s, fid)
            add_entry(s, fid, "payment" if f.kind == "debt" else "add", a, day=today, note=note, today=today)
            n += 1
    if n == 0:
        raise ValidationError("Nothing to record - every amount is zero.")
    return n


def week_money(s: Session, lo: dt.date, hi: dt.date) -> dict:
    rows = s.scalars(select(FundEntry).where(FundEntry.day >= lo, FundEntry.day <= hi))
    out = {"set_aside": ZERO, "paid_down": ZERO, "spent_tracked": ZERO}
    for e in rows:
        if e.kind == "add":
            out["set_aside"] += e.amount
        elif e.kind == "payment":
            out["paid_down"] += e.amount
        elif e.kind == "spend":
            out["spent_tracked"] += e.amount
    out["kept"] = kept_total(s, lo, hi)
    return out
