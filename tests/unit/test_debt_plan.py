"""DEBT-*: payoff math (Decimal), the honest micropayment comparison, the playbook and its reminders."""
import datetime as dt
from decimal import Decimal as D

import pytest

from lifeos.engines import debt as E
from lifeos.errors import ValidationError
from lifeos.models import Task
from lifeos.services import debt_plan as DP
from lifeos.services import money as M

T = dt.date(2026, 12, 10)
DEBTS = [E.DebtIn(1, "Card A", D(5000), D("24.99"), D(150)), E.DebtIn(2, "Card B", D(1200), D("19.99"), D(40)),
         E.DebtIn(3, "Auto", D(9000), D("7.5"), D(250))]


# ---------------------------------------------------------------- engine
def test_debt01_avalanche_saves_interest_snowball_wins_early_both_beat_minimums():
    av, sb, base = E.simulate(DEBTS, D(120), "avalanche"), E.simulate(DEBTS, D(120), "snowball"), E.minimum_only(DEBTS)
    assert av.sequence == [1, 2, 3] and sb.sequence == [2, 1, 3]
    assert av.total_interest < sb.total_interest < base.total_interest
    assert sb.payoff_month[2] < av.payoff_month[2]  # snowball's first win comes sooner
    assert av.months < base.months
    assert all(isinstance(x.total_interest, D) for x in (av, sb, base))


def test_debt02_never_paid_off_when_payment_is_below_interest():
    p = E.simulate([E.DebtIn(1, "Card", D(10000), D(30), D(200))], D(0), "avalanche")  # 250/mo interest
    assert p.months is None and p.debt_free(T) is None
    assert E.minimum_only([E.DebtIn(1, "Card", D(10000), D(30), D(200))]).months is None


def test_debt03_micropayments_honest_math():
    r = E.micro_compare(D(5000), D("24.99"), D(150), D(8), 2)
    assert r.extra_per_month == D("120.00")
    assert r.saved_by_paying_more > D(2000)          # the extra money does the heavy lifting
    assert D(0) < r.saved_by_paying_often < D(100)  # paying more often helps, a little
    assert r.micro[0] <= r.monthly_lump[0] < r.minimum_only[0]


def test_debt04_add_months_handles_month_ends():
    assert E.add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert E.add_months(dt.date(2026, 11, 30), 3) == dt.date(2027, 2, 28)
    assert E.micro_days(dt.date(2026, 12, 1), 2, dt.date(2026, 12, 4), dt.date(2026, 12, 8)) == \
        [dt.date(2026, 12, 5), dt.date(2026, 12, 7)]


# ---------------------------------------------------------------- service
def _card(s, name="Card A", owed=5000, apr="24.99", minimum=150, due=15, autopay=False):
    f = M.create_fund(s, name=name, space="owed", target=owed, counterparty=f"{name} issuer", today=T)
    DP.set_terms(s, f.id, apr=apr, min_payment=minimum, due_day=due, autopay=autopay)
    return f


def test_debt05_plan_uses_real_balances_and_keeps_unknowns_out(s):
    a = _card(s)
    b = M.create_fund(s, name="Owe Mom", space="owed", target=300, today=T)  # no terms yet
    pv = DP.plan(s)
    assert pv.target.id == a.id and pv.incomplete == [b] and pv.chosen.months is not None
    DP.set_terms(s, b.id, apr=0, min_payment=50, due_day=1)
    DP.configure(s, strategy="snowball", extra=100)
    pv = DP.plan(s)
    assert pv.target.id == b.id and pv.budget == D(300) and not pv.incomplete
    with pytest.raises(ValidationError):
        DP.set_terms(s, a.id, apr=150)
    with pytest.raises(ValidationError):
        DP.configure(s, strategy="vibes")


def test_debt06_micro_payments_go_to_the_target_and_count_in_the_plan(s):
    a = _card(s)
    with pytest.raises(ValidationError):
        DP.micro_pay(s, T)
    DP.configure(s, micro_amount="8", micro_every=2, today=T)
    assert DP.plan(s).extra == D("120.00")
    assert DP.micro_due(s, T) and not DP.micro_due(s, T + dt.timedelta(days=1))
    DP.micro_pay(s, T)
    assert not DP.micro_due(s, T)  # done for today
    assert M.paid(a) == D(8) and a.entries[-1].note == DP.MICRO_NOTE
    assert DP.month_progress(s, T)[0] == D(8)


def test_debt07_payday_rule_flows_into_got_paid_split(s):
    a = _card(s)
    M.create_fund(s, name="Emergency fund", space="reserves", target=1000, today=T)
    DP.configure(s, payday_pct=25)
    splits, left = M.suggest_split(s, 800, T)
    row = next(x for x in splits if x.fund.id == a.id)
    assert row.suggested == D("200.00") and "payday rule" in row.why


def test_debt08_due_reminders_missed_minimums_and_autopay(s):
    a = _card(s, due=12)
    a.created_at = dt.datetime(2026, 10, 1)
    due = {d.fund.id: d for d in DP.due_status(s, T)}
    assert due[a.id].days == 2 and due[a.id].missed  # nothing paid in the Oct 12 - Nov 12 cycle
    M.add_entry(s, a.id, "payment", 150, day=dt.date(2026, 11, 10), today=T)
    M.add_entry(s, a.id, "payment", 150, day=dt.date(2026, 12, 9), today=T)
    assert not DP.due_status(s, T)  # paid this cycle, last cycle covered
    DP.set_terms(s, a.id, autopay=True)
    assert not DP.due_status(s, dt.date(2027, 1, 10))


def test_debt09_statement_sync_logs_interest_and_plays_track_it(s):
    a = _card(s)
    assert DP.sync_statement(s, a.id, "5104.20", T) == D("104.20")
    assert M.remaining(a) == D("5104.20") and not DP.new_charges(a, T)  # interest isn't 'new spending'
    M.add_entry(s, a.id, "charge", 60, note="Dinner", today=T)
    p = {x.key: x for x in DP.plays(s, T)}
    assert p["stop"].status == "warn" and p["statement"].status == "done" and p["numbers"].status == "done"
    assert p["minimums"].status == "todo" and p["ask"].status == "todo"


def test_debt10_ask_for_lower_apr_creates_a_task_and_debt_free_goal(s):
    a = _card(s)
    task = DP.ask_for_lower_apr(s, a.id, T)
    assert isinstance(task, Task) and task.pillar_code == "FIN" and "lower APR" in task.title
    assert {x.key: x for x in DP.plays(s, T)}["ask"].status == "done"
    g, _ = DP.make_goal(s, T)
    assert g.pillar_code == "FIN" and g.title == "Debt-free" and g.target_date == DP.plan(s).chosen.debt_free(T)


# ---------------------------------------------------------------- status, settling, credit report
def test_debt11_charged_off_and_collections_leave_the_monthly_plan(s):
    rent = M.create_fund(s, name="Last rent payment", space="owed", target=3200, counterparty="Former landlord", today=T)
    DP.set_terms(s, rent.id, apr=0, min_payment=200, due_day=1)
    DP.set_profile(s, rent.id, debt_status="payment_plan")
    biz = _card(s, "Business card", owed=4000, apr="29.99", minimum=120)
    DP.set_profile(s, biz.id, owner="business")
    amex = M.create_fund(s, name="Amex (charged off)", space="owed", target=2500, today=T)
    DP.set_profile(s, amex.id, debt_status="charged_off", holder="American Express", first_delinquency=dt.date(2024, 3, 1))
    pv = DP.plan(s)
    assert {f.id for f in pv.complete} == {rent.id, biz.id} and amex not in pv.incomplete
    assert [f.id for f in DP.debts(s, "settle")] == [amex.id]
    assert DP.report_age_off(amex) == dt.date(2031, 8, 28)  # 2024-03-01 + 180 days + 7 years
    p = {x.key: x for x in DP.plays(s, T)}
    assert p["settle"].status == "todo" and "walk-away" in p["settle"].detail
    with pytest.raises(ValidationError):
        DP.set_profile(s, amex.id, debt_status="vanished")


def test_debt12_settle_in_writing_forgives_the_rest_and_flags_taxes(s):
    col = M.create_fund(s, name="Chase (collections)", space="owed", target=3000, today=T)
    DP.set_profile(s, col.id, debt_status="collections", holder="Collector LLC", validated=True,
                   first_delinquency=dt.date(2024, 6, 1))
    with pytest.raises(ValidationError):
        DP.set_settlement(s, col.id, opening=1500, target=1200, walkaway=1400)  # opening above target
    with pytest.raises(ValidationError):
        DP.set_settlement(s, col.id, walkaway=5000)
    DP.set_settlement(s, col.id, opening=900, target=1200, walkaway=1500)
    assert {x.key: x for x in DP.plays(s, T)}["settle"].status == "done"
    with pytest.raises(ValidationError):
        DP.record_settlement(s, col.id, 1200, T, in_writing=False)
    forgiven, task = DP.record_settlement(s, col.id, 1200, T, in_writing=True, who="Collector LLC")
    assert forgiven == D(1800) and col.status == "done" and M.remaining(col) == 0 and M.paid(col) == D(1200)
    assert task is not None and "1099-C" in task.title and task.due_date == dt.date(2027, 2, 15)


def test_debt13_credit_report_check_finds_what_to_fix_or_dispute(s):
    apple = M.create_fund(s, name="Apple Card", space="owed", target=1800, today=T)
    DP.set_profile(s, apple.id, debt_status="charged_off", first_delinquency=dt.date(2024, 1, 10))
    school = M.create_fund(s, name="Master's (school)", space="owed", target=6000, today=T)
    DP.set_terms(s, school.id, apr=0, min_payment=250, due_day=1)
    with pytest.raises(ValidationError):
        DP.add_report_item(s, bureau="Equifax", creditor="Apple", status="charged_off", pulled_on=T, last4="12345678")
    DP.add_report_item(s, bureau="Experian", creditor="GS Bank / Apple Card", status="charged_off", pulled_on=T,
                       last4="4321", balance="1950", first_delinquency=dt.date(2023, 12, 10), fund_id=apple.id)
    DP.add_report_item(s, bureau="Experian", creditor="Old Gym LLC", status="collections", pulled_on=T,
                       balance="240", first_delinquency=dt.date(2018, 1, 5))
    kinds = sorted(f.kind for f in DP.reconcile(s, T))
    assert kinds == ["balance", "date", "missing_from_report", "not_tracked"]
    old = DP.add_report_item(s, bureau="TransUnion", creditor="Ancient Card", status="charged_off", pulled_on=T,
                             first_delinquency=dt.date(2018, 1, 5))
    gone = M.create_fund(s, name="Ancient Card", space="owed", target=100, today=T)
    DP.link_report_item(s, old.id, gone.id)
    assert any(f.kind == "aged" for f in DP.reconcile(s, T))
    p = {x.key: x for x in DP.plays(s, T)}
    assert p["report"].status == "warn"
