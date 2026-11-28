"""Debt payoff math (pure, Decimal only - never floats).

- simulate(): pay every minimum, then put the rest of a fixed monthly budget on one target debt at a time.
  When a debt is paid off, its minimum rolls into the next target (that's what makes the plan accelerate).
- Orders: avalanche (highest APR first - least interest) and snowball (smallest balance first - quick wins).
- minimum_only(): what happens if you only ever pay today's minimums.
- micro_compare(): the honest micropayment math - how much of the saving comes from paying MORE versus
  paying MORE OFTEN (credit cards charge interest on the average daily balance).

Assumptions (shown to the user): interest compounds monthly at APR/12; you keep paying at least today's
minimum even as the bank's required minimum falls; no new charges.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal(0)
MAX_MONTHS = 600
ORDERS = ("avalanche", "snowball")


def _c(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class DebtIn:
    key: int | str
    name: str
    balance: Decimal
    apr: Decimal          # percent, e.g. Decimal("24.99")
    minimum: Decimal


@dataclass
class Plan:
    order: str
    months: int | None              # None = never paid off at this budget
    total_interest: Decimal
    payoff_month: dict = field(default_factory=dict)   # key -> month number (1-based)
    sequence: list = field(default_factory=list)       # keys in the order they get paid off

    def debt_free(self, start: dt.date) -> dt.date | None:
        return None if self.months is None else add_months(start, self.months)


def add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31,
            30, 31][month - 1]
    return dt.date(year, month, min(d.day, last))


def ordered(debts: list[DebtIn], order: str) -> list[DebtIn]:
    if order == "avalanche":
        return sorted(debts, key=lambda d: (-d.apr, d.balance, str(d.key)))
    if order == "snowball":
        return sorted(debts, key=lambda d: (d.balance, -d.apr, str(d.key)))
    raise ValueError(f"order must be one of {ORDERS}")


def simulate(debts: list[DebtIn], extra_monthly: Decimal, order: str) -> Plan:
    debts = [d for d in debts if d.balance > 0]
    queue = ordered(debts, order)
    bal = {d.key: d.balance for d in debts}
    budget = sum((d.minimum for d in debts), ZERO) + max(ZERO, extra_monthly)
    plan = Plan(order, None, ZERO)
    for month in range(1, MAX_MONTHS + 1):
        for d in debts:
            if bal[d.key] > 0:
                i = _c(bal[d.key] * d.apr / Decimal(1200))
                bal[d.key] += i
                plan.total_interest += i
        left = budget
        for d in debts:  # minimums first, always
            if bal[d.key] > 0:
                pay = min(d.minimum, bal[d.key], left)
                bal[d.key] -= pay
                left -= pay
        for d in queue:  # everything else to the current target, then the next
            if left <= 0:
                break
            if bal[d.key] > 0:
                pay = min(bal[d.key], left)
                bal[d.key] -= pay
                left -= pay
        for d in queue:
            if bal[d.key] <= 0 and d.key not in plan.payoff_month:
                plan.payoff_month[d.key] = month
                plan.sequence.append(d.key)
        if all(v <= 0 for v in bal.values()):
            plan.months = month
            break
    plan.total_interest = _c(plan.total_interest)
    return plan


def minimum_only(debts: list[DebtIn]) -> Plan:
    """Each debt gets exactly its own minimum - no rollover, no extra."""
    worst, interest, payoff = 0, ZERO, {}
    for d in debts:
        p = simulate([DebtIn(d.key, d.name, d.balance, d.apr, d.minimum)], ZERO, "avalanche")
        interest += p.total_interest
        if p.months is None:
            return Plan("minimum", None, _c(interest), payoff)
        payoff[d.key] = p.months
        worst = max(worst, p.months)
    return Plan("minimum", worst if debts else 0, _c(interest), payoff)


@dataclass(frozen=True)
class MicroResult:
    minimum_only: tuple[int | None, Decimal]   # (months, interest)
    monthly_lump: tuple[int | None, Decimal]   # same extra money, paid once a month when the bill comes
    micro: tuple[int | None, Decimal]          # same extra money, paid every N days
    extra_per_month: Decimal

    @property
    def saved_by_paying_more(self) -> Decimal:
        return self.minimum_only[1] - self.monthly_lump[1]

    @property
    def saved_by_paying_often(self) -> Decimal:
        return self.monthly_lump[1] - self.micro[1]


def _daily(balance: Decimal, apr: Decimal, minimum: Decimal, extra: Decimal, every: int, mode: str) -> tuple[int | None, Decimal]:
    """30-day billing cycles, interest on the daily balance, posted at cycle end; minimum paid at cycle end.
    mode: 'none' | 'lump' (extra * 30/every at cycle end) | 'micro' (extra every `every` days)."""
    bal, accrued, total = balance, Decimal(0), ZERO
    daily_rate = apr / Decimal(36500)
    lump = _c(extra * Decimal(30) / Decimal(every))
    for day in range(1, MAX_MONTHS * 30 + 1):
        if mode == "micro" and day % every == 0 and bal > 0:
            bal -= min(extra, bal)
        accrued += bal * daily_rate
        if day % 30 == 0:
            i = _c(accrued)
            accrued = Decimal(0)
            bal += i
            total += i
            pay = minimum + (lump if mode == "lump" else ZERO)
            bal -= min(pay, bal)
            if bal <= 0:
                return day // 30, _c(total)
    return None, _c(total)


def micro_compare(balance: Decimal, apr: Decimal, minimum: Decimal, amount: Decimal, every_days: int) -> MicroResult:
    if every_days < 1:
        raise ValueError("every_days must be at least 1")
    return MicroResult(
        minimum_only=_daily(balance, apr, minimum, ZERO, every_days, "none"),
        monthly_lump=_daily(balance, apr, minimum, amount, every_days, "lump"),
        micro=_daily(balance, apr, minimum, amount, every_days, "micro"),
        extra_per_month=_c(amount * Decimal(30) / Decimal(every_days)),
    )


def micro_days(start: dt.date, every_days: int, lo: dt.date, hi: dt.date) -> list[dt.date]:
    """Micro-payment days between lo and hi (inclusive), counting from the plan's start date."""
    out, d = [], start
    while d <= hi:
        if d >= lo:
            out.append(d)
        d += dt.timedelta(days=every_days)
    return out
