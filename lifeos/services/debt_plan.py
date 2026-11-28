"""Debt playbook: a payoff plan built on your 'Money I owe' debts, eight plays that keep you honest, and the
reminders that make it happen (due dates, micro-payment days, payday rule, statement checks).

LIFE OS tracks money; it never moves it. Every payment is a normal fund entry with its audit trail."""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from lifeos.engines import debt as E
from lifeos.errors import ValidationError
from lifeos.services import money as M
from lifeos.services import tasks as T

MICRO_NOTE = "☕ Micro-payment"
PAYDAY_NOTE = "💵 Payday rule"
STATEMENT_NOTE = "Interest & fees (statement)"
HIGH_APR = Decimal(20)
DEFAULTS = {"strategy": "avalanche", "extra": "0", "micro_amount": "", "micro_every": "2", "micro_start": "",
            "payday_pct": ""}


# ---------------------------------------------------------------- settings
def setting(s: Session, key: str) -> str:
    v = M.get_setting(s, f"debt.{key}")
    return DEFAULTS.get(key, "") if v is None else v


def configure(s: Session, *, strategy: str | None = None, extra=None, micro_amount=None, micro_every=None,
              payday_pct=None, today: dt.date | None = None) -> None:
    if strategy is not None:
        if strategy not in E.ORDERS:
            raise ValidationError("Strategy is avalanche or snowball.")
        M.set_setting(s, "debt.strategy", strategy)
        M.set_setting(s, "debt.strategy_chosen", "1")
    if extra not in (None, ""):
        M.set_setting(s, "debt.extra", str(M.money(extra)))
    if micro_amount is not None:
        if micro_amount == "":
            M.set_setting(s, "debt.micro_amount", "")
        else:
            amt = M.money(micro_amount)
            if amt <= 0:
                raise ValidationError("A micro-payment needs an amount above zero.")
            M.set_setting(s, "debt.micro_amount", str(amt))
            if not setting(s, "micro_start") and today:
                M.set_setting(s, "debt.micro_start", today.isoformat())
    if micro_every not in (None, ""):
        n = int(micro_every)
        if not 1 <= n <= 14:
            raise ValidationError("Micro-payments every 1 to 14 days.")
        M.set_setting(s, "debt.micro_every", str(n))
    if payday_pct is not None:
        if payday_pct == "":
            M.set_setting(s, "debt.payday_pct", "")
        else:
            pct = Decimal(str(payday_pct))
            if not 0 < pct <= 100:
                raise ValidationError("Payday rule is a percent between 1 and 100.")
            M.set_setting(s, "debt.payday_pct", str(pct))


def _dec(v: str) -> Decimal | None:
    return Decimal(v) if v not in (None, "") else None


# ---------------------------------------------------------------- debts and their terms
def debts(s: Session) -> list:
    return [f for f in M.funds_in(s, "owed") if f.kind == "debt" and f.status == "active"]


def set_terms(s: Session, fund_id: int, *, apr=None, min_payment=None, due_day=None, autopay: bool | None = None) -> None:
    f = M.get_fund(s, fund_id)
    if f.kind != "debt":
        raise ValidationError("Terms apply to debts only.")
    old = f"APR {f.apr} · min {f.min_payment} · due {f.due_day} · autopay {f.autopay}"
    if apr not in (None, ""):
        a = Decimal(str(apr))
        if not 0 <= a <= 100:
            raise ValidationError("APR is a percent between 0 and 100 (use 0 for interest-free, like family).")
        f.apr = a
    if min_payment not in (None, ""):
        f.min_payment = M.money(min_payment)
    if due_day not in (None, ""):
        d = int(due_day)
        if not 1 <= d <= 31:
            raise ValidationError("Due day is a day of the month, 1-31.")
        f.due_day = d
    if autopay is not None:
        f.autopay = bool(autopay)
    new = f"APR {f.apr} · min {f.min_payment} · due {f.due_day} · autopay {f.autopay}"
    if new != old:
        M._log(s, f, "debt_terms", old, new)
    s.flush()


def missing_terms(f) -> list[str]:
    out = []
    if f.apr is None:
        out.append("APR")
    if f.min_payment is None:
        out.append("minimum payment")
    if f.due_day is None:
        out.append("due day")
    return out


def sync_statement(s: Session, fund_id: int, statement_balance, today: dt.date) -> Decimal:
    """Match the debt to the real statement. A higher statement = interest & fees (logged as a charge);
    a lower one = a credit or correction (logged as a payment). Returns the difference."""
    f = M.get_fund(s, fund_id)
    real = M.money(statement_balance)
    diff = real - M.remaining(f)
    if diff > 0:
        M.add_entry(s, f.id, "charge", diff, day=today, note=STATEMENT_NOTE, today=today)
    elif diff < 0:
        M.add_entry(s, f.id, "payment", -diff, day=today, note="Statement credit / correction", today=today)
    M.set_setting(s, f"debt.synced.{f.id}", today.isoformat())
    M._log(s, f, "statement_sync", to=M.fmt(real))
    return diff


# ---------------------------------------------------------------- the plan
@dataclass
class PlanView:
    complete: list            # funds with every term filled in
    incomplete: list          # funds still missing APR / minimum
    strategy: str
    extra: Decimal
    plans: dict               # order -> E.Plan
    baseline: E.Plan          # minimums only
    target: object | None     # the fund to throw extra money at
    budget: Decimal           # minimums + extra, per month

    @property
    def chosen(self) -> E.Plan | None:
        return self.plans.get(self.strategy)

    def interest_saved(self) -> Decimal | None:
        c = self.chosen
        if not c or c.months is None or self.baseline.months is None:
            return None
        return self.baseline.total_interest - c.total_interest


def plan(s: Session) -> PlanView:
    fs = debts(s)
    complete = [f for f in fs if f.apr is not None and f.min_payment is not None and M.remaining(f) > 0]
    incomplete = [f for f in fs if f not in complete and M.remaining(f) > 0]
    ins = [E.DebtIn(f.id, f.name, M.remaining(f), f.apr, f.min_payment) for f in complete]
    strategy = setting(s, "strategy")
    extra = _dec(setting(s, "extra")) or M.ZERO
    micro = _dec(setting(s, "micro_amount"))
    if micro:
        extra += (micro * Decimal(30) / Decimal(int(setting(s, "micro_every")))).quantize(M.CENT)
    plans = {o: E.simulate(ins, extra, o) for o in E.ORDERS} if ins else {}
    target = None
    if ins:
        first = E.ordered(ins, strategy)[0].key
        target = next(f for f in complete if f.id == first)
    budget = sum((d.minimum for d in ins), M.ZERO) + extra
    return PlanView(complete, incomplete, strategy, extra, plans, E.minimum_only(ins), target, budget)


# ---------------------------------------------------------------- doing it
def micro_pay(s: Session, today: dt.date, fund_id: int | None = None) -> None:
    amt = _dec(setting(s, "micro_amount"))
    if not amt:
        raise ValidationError("Set a micro-payment amount first (Debt playbook → ☕ Micro-payments).")
    fid = fund_id or (plan(s).target.id if plan(s).target else None)
    if fid is None:
        raise ValidationError("No target debt yet - add APR and minimum to your debts.")
    M.add_entry(s, fid, "payment", amt, day=today, note=MICRO_NOTE, today=today)


def micro_due(s: Session, today: dt.date) -> bool:
    amt, start = setting(s, "micro_amount"), setting(s, "micro_start")
    if not amt or not start:
        return False
    days = E.micro_days(dt.date.fromisoformat(start), int(setting(s, "micro_every")), today, today)
    done_today = any(e.note == MICRO_NOTE and e.day == today for f in debts(s) for e in f.entries)
    return bool(days) and not done_today


def payday_amount(s: Session, amount) -> Decimal | None:
    pct = _dec(setting(s, "payday_pct"))
    return (M.money(amount) * pct / Decimal(100)).quantize(M.CENT) if pct else None


def _due_dates(due_day: int, today: dt.date) -> tuple[dt.date, dt.date]:
    """(previous due date, next due date on or after today)."""
    def on(y, m):
        return dt.date(y, m, min(due_day, calendar.monthrange(y, m)[1]))
    this = on(today.year, today.month)
    if this >= today:
        py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return on(py, pm), this
    ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return this, on(ny, nm)


@dataclass(frozen=True)
class Due:
    fund: object
    due: dt.date
    days: int
    paid_this_cycle: bool
    missed: bool          # the previous due date passed with no payment in that cycle


def due_status(s: Session, today: dt.date, within: int = 3) -> list[Due]:
    out = []
    for f in debts(s):
        if not f.due_day:
            continue
        prev, nxt = _due_dates(f.due_day, today)
        prev2, _ = _due_dates(f.due_day, prev - dt.timedelta(days=1))
        paid_now = any(e.kind == "payment" and prev < e.day <= today for e in f.entries)
        started = f.created_at.date() if f.created_at else today
        missed = (not f.autopay and started <= prev2 and
                  not any(e.kind == "payment" and prev2 < e.day <= prev for e in f.entries))
        days = (nxt - today).days
        if missed or (days <= within and not paid_now and not f.autopay):
            out.append(Due(f, nxt, days, paid_now, missed))
    return out


def new_charges(f, today: dt.date, days: int = 30) -> list:
    """Real new spending on a debt (statement interest and fees don't count)."""
    lo = today - dt.timedelta(days=days)
    return [e for e in f.entries if e.kind == "charge" and e.day > lo and e.note != STATEMENT_NOTE]


def month_progress(s: Session, today: dt.date) -> tuple[Decimal, Decimal]:
    """(paid toward debts this month, the plan's monthly budget)."""
    lo, hi = M.month_bounds(today)
    paid = sum((e.amount for f in M.funds_in(s, "owed", include_closed=True) for e in f.entries
                if e.kind == "payment" and lo <= e.day <= hi and not e.note.startswith("Statement")), M.ZERO)
    return paid, plan(s).budget


def ask_for_lower_apr(s: Session, fund_id: int, today: dt.date):
    f = M.get_fund(s, fund_id)
    task = T.create_task(s, title=f"Call {f.counterparty or f.name}: ask for a lower APR"[:200], pillar="FIN",
                         priority=2, due_date=today + dt.timedelta(days=7), scheduled_date=today,
                         description="Script: how long you've been a customer, on-time payments, a competing offer "
                                     "if you have one. Ask: 'What can you do on my APR?' Then log the result in the "
                                     "debt's terms. (NEG101: anchor, calibrated questions.)")
    M.set_setting(s, f"debt.asked.{f.id}", today.isoformat())
    M._log(s, f, "apr_call_planned", to=f"task #{task.id}")
    return task


def make_goal(s: Session, today: dt.date):
    from lifeos.services import cascade as C
    pv = plan(s)
    if not pv.chosen or pv.chosen.months is None:
        raise ValidationError("Fill in every debt's APR and minimum first, so there's a debt-free date to aim at.")
    target = pv.chosen.debt_free(today)
    g, warnings = C.create_goal(s, pillar="FIN", title="Debt-free", target_date=target, today=today,
                                description=f"{pv.strategy.title()} plan: minimums + extra each month, micro-payments "
                                            "and the payday rule. Plan lives on Money → Debt playbook.",
                                progress_evidence="Every 'Money I owe' debt at zero.")
    M.set_setting(s, "debt.goal_code", g.code)
    return g, warnings


# ---------------------------------------------------------------- the playbook
@dataclass(frozen=True)
class Play:
    key: str
    title: str
    status: str   # done | todo | warn
    detail: str


def plays(s: Session, today: dt.date) -> list[Play]:
    fs = debts(s)
    pv = plan(s)
    out: list[Play] = []
    miss = [f"{f.name} ({', '.join(missing_terms(f))})" for f in fs if missing_terms(f)]
    out.append(Play("numbers", "📋 Know every number", "todo" if miss or not fs else "done",
                    "Add your debts under 💳 Money I owe." if not fs else
                    ("Missing: " + "; ".join(miss)) if miss else "Balance, APR, minimum and due day for every debt."))
    late = [d.fund.name for d in due_status(s, today) if d.missed]
    manual = [f.name for f in fs if not f.autopay]
    out.append(Play("minimums", "🛡️ Never miss a minimum", "warn" if late else ("todo" if manual else "done"),
                    f"Missed last cycle: {', '.join(late)}. Pay it and call about the late fee." if late else
                    (f"Turn on autopay for the minimum: {', '.join(manual)}." if manual else
                     "Every minimum is on autopay - late fees and penalty APRs can't happen.")))
    chosen = M.get_setting(s, "debt.strategy_chosen") == "1"
    tgt = f" Target now: **{pv.target.name}**." if pv.target else ""
    out.append(Play("target", "🎯 One target at a time", "done" if chosen and pv.target else "todo",
                    f"{pv.strategy.title()}: {'highest APR first (least interest)' if pv.strategy == 'avalanche' else 'smallest balance first (quick wins)'}.{tgt}"
                    + ("" if chosen else " Pick avalanche or snowball below.")))
    micro = setting(s, "micro_amount")
    lo, hi = M.month_bounds(today)
    micro_n = sum(1 for f in M.funds_in(s, "owed", include_closed=True) for e in f.entries
                  if e.note == MICRO_NOTE and lo <= e.day <= hi)
    out.append(Play("micro", "☕ Micro-payments", "done" if micro else "todo",
                    f"{M.fmt(Decimal(micro))} every {setting(s, 'micro_every')} days to the target · {micro_n} this month."
                    if micro else "Set a coffee-size amount every few days. Small, automatic, adds up."))
    pct = setting(s, "payday_pct")
    out.append(Play("payday", "💵 Payday rule", "done" if pct else "todo",
                    f"{pct}% of every payday or windfall goes to the target - built into 'Got paid? Split it'."
                    if pct else "Decide now what % of every paycheck and windfall goes to debt, before the money arrives."))
    charged = [f.name for f in fs if new_charges(f, today)]
    out.append(Play("stop", "🧊 Stop adding to it", "warn" if charged else "done",
                    f"New charges in the last 30 days: {', '.join(charged)}. Wants go through the 14-day cool-off."
                    if charged else "No new charges in 30 days."))
    high = [f for f in fs if f.apr is not None and f.apr >= HIGH_APR]
    unasked = [f.name for f in high if not M.get_setting(s, f"debt.asked.{f.id}")]
    out.append(Play("ask", "📞 Ask for a lower rate", "todo" if unasked else "done",
                    f"APR {HIGH_APR}%+ and not asked yet: {', '.join(unasked)}. One call can cut the interest."
                    if unasked else ("Asked on every high-APR debt." if high else "No debts at 20%+ APR.")))
    stale = [f.name for f in fs if not M.get_setting(s, f"debt.synced.{f.id}") or
             (today - dt.date.fromisoformat(M.get_setting(s, f"debt.synced.{f.id}"))).days > 35]
    out.append(Play("statement", "🧾 Monthly statement check", "todo" if stale else "done",
                    f"Match to the real statement (adds interest & fees): {', '.join(stale)}." if stale else
                    "Every balance matches a statement from the last 35 days."))
    return out
