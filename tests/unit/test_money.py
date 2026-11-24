"""MNY-* : funds (save / debt / spend) in separate spaces, cool-off, frugality tools, paycheck split."""
import datetime as dt
from decimal import Decimal

import pytest

from lifeos.errors import ReasonRequired, ValidationError
from lifeos.services import money as M

D = Decimal
TODAY = dt.date(2026, 11, 24)


def test_mny01_spaces_keep_debts_apart(s):
    with pytest.raises(ValidationError, match="Money I owe"):
        M.create_fund(s, name="Credit card", space="family", kind="debt", target=900, today=TODAY)
    with pytest.raises(ValidationError):
        M.create_fund(s, name="Phone", space="owed", kind="save", target=100, today=TODAY)
    son = M.create_fund(s, name="My son", space="family", today=TODAY)
    card = M.create_fund(s, name="Credit card", space="owed", target=900, today=TODAY)
    assert (son.kind, card.kind) == ("spend", "debt")
    assert M.funds_in(s, "family") == [son] and M.funds_in(s, "owed") == [card]


@pytest.mark.parametrize("kw", [dict(target=None), dict(target="-5"), dict(target="lots"),
                                dict(deadline=TODAY - dt.timedelta(days=1)), dict(name=" ")])
def test_mny02_validation(s, kw):
    base = dict(name="Meta glasses", space="wants", target=379, today=TODAY)
    base.update(kw)
    with pytest.raises(ValidationError):
        M.create_fund(s, **base)


def test_mny03_savings_exact_decimals_and_withdraw_needs_reason(s):
    f = M.create_fund(s, name="Christmas", space="holiday", target="300", today=TODAY)
    for v in ("0.10", "0.20"):
        M.add_entry(s, f.id, "add", v, today=TODAY)
    assert M.balance(f) == D("0.30")
    M.add_entry(s, f.id, "add", "99.70", today=TODAY)
    with pytest.raises(ReasonRequired):
        M.add_entry(s, f.id, "withdraw", 20, today=TODAY)
    with pytest.raises(ValidationError):
        M.add_entry(s, f.id, "withdraw", 500, note="x", today=TODAY)
    M.add_entry(s, f.id, "withdraw", 20, note="Gas money, short week", today=TODAY)
    assert (M.balance(f), M.remaining(f)) == (D("80.00"), D("220.00"))
    with pytest.raises(ValidationError):
        M.add_entry(s, f.id, "add", 5, day=TODAY + dt.timedelta(days=1), today=TODAY)
    with pytest.raises(ValidationError):
        M.add_entry(s, f.id, "spend", 5, today=TODAY)


def test_mny04_debt_paid_down_with_new_charges_and_auto_closes(s):
    f = M.create_fund(s, name="Parents", space="owed", target=5000, counterparty="Mom & Dad",
                      deadline=TODAY + dt.timedelta(days=180), today=TODAY)
    M.add_entry(s, f.id, "payment", 500, today=TODAY)
    with pytest.raises(ReasonRequired):
        M.add_entry(s, f.id, "charge", 100, today=TODAY)
    M.add_entry(s, f.id, "charge", 100, note="Borrowed for car repair", today=TODAY)
    assert (M.paid(f), M.owed_total(f), M.remaining(f)) == (D(500), D(5100), D(4600))
    p = M.progress(f, TODAY)
    assert p.goal == D(5100) and round(p.fraction, 3) == round(500 / 5100, 3)
    assert p.weekly_needed == (D(4600) / (D(180) / D(7))).quantize(D("0.01"))
    M.add_entry(s, f.id, "payment", 4600, today=TODAY)
    assert f.status == "done"


def test_mny05_spending_tracker_by_month_against_budget(s):
    f = M.create_fund(s, name="My son", space="family", kind="spend", target=300, today=TODAY)
    M.add_entry(s, f.id, "spend", "45.50", note="Shoes", day=dt.date(2026, 11, 2), today=TODAY)
    M.add_entry(s, f.id, "spend", 280, note="Winter coat", day=TODAY, today=TODAY)
    M.add_entry(s, f.id, "spend", 30, note="Old month", day=dt.date(2026, 10, 30), today=TODAY)
    p = M.progress(f, TODAY)
    assert (p.done, p.pace, p.fraction) == (D("325.50"), "over_budget", 1.0)
    none = M.create_fund(s, name="Gifts for him", space="family", kind="spend", today=TODAY)
    assert M.progress(none, TODAY).pace == "no_budget"


def test_mny06_wants_cooloff_14_days(s):
    f = M.create_fund(s, name="Meta glasses", space="wants", target=300, today=TODAY)
    M.add_entry(s, f.id, "add", 100, day=TODAY - dt.timedelta(days=20), today=TODAY)
    with pytest.raises(ValidationError, match="isn't fully saved"):
        M.mark_bought(s, f.id, 299, today=TODAY)
    M.add_entry(s, f.id, "add", 200, day=TODAY - dt.timedelta(days=10), today=TODAY)
    assert M.full_since(f) == TODAY - dt.timedelta(days=10)
    assert M.cooloff_ends(f) == TODAY + dt.timedelta(days=4)
    with pytest.raises(ValidationError, match="Cool-off ends"):
        M.mark_bought(s, f.id, 279, today=TODAY)
    M.mark_bought(s, f.id, 279, today=TODAY + dt.timedelta(days=4), note="Found it on sale")
    assert (f.status, f.paid_price, M.balance(f)) == ("done", D(279), D(21))


def test_mny07_cooloff_resets_if_you_dip_below_and_override_is_recorded(s):
    f = M.create_fund(s, name="Speaker", space="wants", target=100, today=TODAY)
    M.add_entry(s, f.id, "add", 100, day=TODAY - dt.timedelta(days=20), today=TODAY)
    M.add_entry(s, f.id, "withdraw", 10, day=TODAY - dt.timedelta(days=15), note="Lunch", today=TODAY)
    M.add_entry(s, f.id, "add", 10, day=TODAY - dt.timedelta(days=5), today=TODAY)
    assert M.full_since(f) == TODAY - dt.timedelta(days=5)
    M.mark_bought(s, f.id, 100, today=TODAY, override_reason="Birthday gift for my son, needed this week")
    assert f.status == "done"


def test_mny08_before_you_buy_hours_and_kept_total(s):
    assert M.hours_of_work(s, 379) is None  # no rate set yet - never invented
    M.set_hourly_rate(s, "21.50")
    assert M.hours_of_work(s, "379") == D("17.6")
    M.impulse_check(s, item="Sneakers", price=180, decision="skip", today=TODAY)
    fund = M.impulse_check(s, item="Meta glasses", price=379, decision="wishlist", today=TODAY)
    assert fund.space == "wants" and fund.target == D(379)
    M.drop_fund(s, fund.id, "Don't need them - phone does the job", today=TODAY)
    assert M.kept_total(s) == D(559)


def test_mny09_paycheck_split_obligations_first_wants_last(s):
    debt = M.create_fund(s, name="Ex - Nov payment", space="owed", target=500, today=TODAY,
                         deadline=TODAY + dt.timedelta(days=6))
    xmas = M.create_fund(s, name="Christmas", space="holiday", target=300, today=TODAY,
                         deadline=TODAY + dt.timedelta(days=28))
    want = M.create_fund(s, name="Meta glasses", space="wants", target=379, today=TODAY,
                         deadline=TODAY + dt.timedelta(days=60))
    M.create_fund(s, name="My son", space="family", kind="spend", target=200, today=TODAY)
    splits, left = M.suggest_split(s, 600, TODAY)
    by = {x.fund.name: x.suggested for x in splits}
    assert by["Ex - Nov payment"] == D(500), "due within a week -> the whole remainder"
    assert by["Christmas"] == D(75)
    assert by["Meta glasses"] == D(25) and left == D(0), "wants get only what's left"
    assert "My son" not in by, "spending trackers aren't funded by the splitter"
    n = M.apply_split(s, {debt.id: "500", xmas.id: "75", want.id: ""}, TODAY)
    assert n == 2 and debt.status == "done" and M.balance(xmas) == D(75)


def test_mny10_cash_check_and_behind_protected(s):
    res = M.create_fund(s, name="Opening cushion", space="reserves", target=8000, today=TODAY,
                        deadline=TODAY + dt.timedelta(days=60))
    res.created_at = dt.datetime(2026, 10, 1)
    M.add_entry(s, res.id, "add", 200, today=TODAY)
    assert res in M.behind_protected(s, TODAY)
    M.set_cash_on_hand(s, 150, TODAY)
    cash, day, aside = M.cash_check(s)
    assert (cash, aside) == (D(150), D(200)), "set aside more than you have -> the page warns"


def test_mny11_week_money(s):
    f = M.create_fund(s, name="Christmas", space="holiday", target=300, today=TODAY)
    M.add_entry(s, f.id, "add", 40, today=TODAY)
    M.impulse_check(s, item="Hoodie", price=65, decision="skip", today=TODAY)
    w = M.week_money(s, TODAY - dt.timedelta(days=1), TODAY)
    assert (w["set_aside"], w["kept"]) == (D(40), D(65))
