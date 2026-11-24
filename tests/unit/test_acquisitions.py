"""ACQ-* : acquisition pipeline - unknowns stay unknown, risk flags, inspection, 7 gates, ranking, fleet."""
import datetime as dt
from decimal import Decimal

import pytest

from lifeos.engines import acquisition as A
from lifeos.errors import ReasonRequired, ValidationError
from lifeos.services import acquisitions as Q
from lifeos.services import money as M

T = dt.date(2026, 12, 1)
D = Decimal


def _vx(s, **kw):
    base = dict(title="2019 Yamaha VX Cruiser", model="VX Cruiser", manufacturer="Yamaha", year=2019,
                asking_price=4800)
    base.update(kw)
    return Q.add_candidate(s, today=T, **base)


def _pass_inspection(s, c):
    for key in A.CRITICAL:
        Q.set_inspection(s, c.id, key, "pass")


def test_acq01_unknowns_stay_unknown_and_cost_is_a_floor(s):
    c = _vx(s)
    assert (c.engine_hours, c.title_status, c.hull_condition) == (None, "unknown", "unknown")
    cost = A.ready_to_rent_cost(c)
    assert (cost.total, cost.complete, cost.missing) == (D(4800), False, ("repairs", "transport", "prep"))
    assert cost.label == "$4,800+ (at least)"
    known, total, missing = A.known_facts(c)
    assert (known, total) == (2, 8) and "engine hours" in missing and "inspection" in missing


def test_acq02_seller_saying_it_runs_is_not_evidence(s):
    c = _vx(s, mechanical_condition="good", seller_statements="Runs great, just needs a battery")
    texts = " ".join(f.text for f in A.risk_flags(c, T))
    assert "seller's word only" in texts and "soft claims" in texts
    _pass_inspection(s, c)
    after = " ".join(f.text for f in A.risk_flags(c, T))
    assert "seller's word only" not in after and "soft claims" not in after


def test_acq03_scam_title_and_underpriced_flags(s):
    a = _vx(s, asking_price=4600)
    b = _vx(s, asking_price=5000)
    cheap = _vx(s, asking_price=1900, seller_statements="Deployed overseas, small deposit and I ship it",
                title_status="missing")
    flags = A.risk_flags(cheap, T, peers=[a, b, cheap])
    risks = [f.text for f in flags if f.level == "risk"]
    assert any("scam" in r for r in risks) and any("Title is missing" in r for r in risks)
    assert any("far below similar listings" in r for r in risks)


def test_acq04_price_changes_and_age_are_leverage(s):
    c = _vx(s)
    Q.update_candidate(s, c.id, today=T + dt.timedelta(days=10), asking_price=4400)
    assert [p.price for p in c.price_history] == [D(4800), D(4400)]
    flags = A.risk_flags(c, T + dt.timedelta(days=35))
    assert {f.level for f in flags} >= {"leverage"}
    assert any("dropped $400" in f.text for f in flags)


def test_acq05_inspection_critical_items_and_validation(s):
    c = _vx(s)
    with pytest.raises(ValidationError):
        Q.set_inspection(s, c.id, "compression", "na")
    Q.set_inspection(s, c.id, "trailer", "na")
    assert A.inspection_state(c) == "in_progress" and c.stage == "inspecting"
    Q.set_inspection(s, c.id, "compression", "fail", "Cyl 3 at 95 psi vs 140")
    assert A.inspection_state(c) == "failed"
    _pass_inspection(s, c)
    assert A.inspection_state(c) == "passed"


def test_acq06_seven_gates_and_reserves_are_protected(s):
    Q.save_settings(s, max_price=5000, fleet_target=6)
    c = _vx(s, asking_price=4500, est_repairs=200, transport_cost=150, prep_cost=150, title_status="clean",
            rental_suitable="yes")
    ev = Q.evaluate(s, c, T)
    assert [g.key for g in ev.gates] == ["budget", "title", "inspection", "suitability", "total_cost", "reserves",
                                         "approval"]
    assert {g.key for g in ev.gates if g.passed} == {"budget", "suitability", "total_cost"}
    Q.update_candidate(s, c.id, today=T, title_verified=True)
    _pass_inspection(s, c)
    cushion = M.create_fund(s, name="Opening cushion", space="reserves", target=8000, today=T)
    M.add_entry(s, cushion.id, "add", 8000, today=T)
    Q.set_funding(s, c.id, cushion.id)
    g = {x.key: x for x in Q.evaluate(s, c, T).gates}
    assert not g["reserves"].passed and "protected reserve" in g["reserves"].detail
    skis = M.create_fund(s, name="Ski purchases", space="business", target=20000, today=T)
    M.add_entry(s, skis.id, "add", 4000, today=T)
    Q.set_funding(s, c.id, skis.id)
    assert not {x.key: x for x in Q.evaluate(s, c, T).gates}["reserves"].passed, "fund too small"
    M.add_entry(s, skis.id, "add", 1000, today=T)
    ev = Q.evaluate(s, c, T)
    assert ev.ready_for_approval and ev.gates_passed == 6


def test_acq07_approval_needs_all_gates_or_a_recorded_override_then_buy(s):
    Q.save_settings(s, max_price=5000)
    c = _vx(s)
    with pytest.raises(ValidationError, match="Not ready"):
        Q.approve(s, c.id, T)
    with pytest.raises(ValidationError):
        Q.mark_bought(s, c.id, 4500, T)
    Q.approve(s, c.id, T, override_reason="Mechanic friend inspected it; title photo verified")
    Q.mark_bought(s, c.id, 4500, T)
    assert c.stage == "owned" and c.agreed_price == D(4500) and Q.fleet(s) == [c]


def test_acq08_buying_draws_down_the_purchase_fund(s):
    Q.save_settings(s, max_price=5000)
    c = _vx(s, asking_price=4500, est_repairs=0, transport_cost=100, prep_cost=100)
    skis = M.create_fund(s, name="Ski purchases", space="business", target=20000, today=T)
    M.add_entry(s, skis.id, "add", 6000, today=T)
    Q.set_funding(s, c.id, skis.id)
    Q.approve(s, c.id, T, override_reason="test")
    Q.mark_bought(s, c.id, 4500, T)
    assert M.balance(skis) == D(1300)


def test_acq09_ranking_and_next_step(s):
    Q.save_settings(s, max_price=5000)
    good = _vx(s, asking_price=4500, title_status="clean", rental_suitable="yes", est_repairs=0, transport_cost=0,
               prep_cost=100)
    risky = _vx(s, asking_price=5600, title_status="missing")
    Q.set_stage(s, good.id, "contacted")
    order = [e.candidate.id for e in Q.ranked(s, T)]
    assert order == [good.id, risky.id]
    assert "Message the seller" in Q.evaluate(s, risky, T).next_step
    assert "title" in Q.evaluate(s, good, T).next_step.lower()


def test_acq10_passing_needs_a_reason_and_existing_fleet(s):
    c = _vx(s)
    with pytest.raises(ReasonRequired):
        Q.set_stage(s, c.id, "passed")
    Q.set_stage(s, c.id, "passed", "Seller wouldn't show the title")
    Q.add_owned(s, title="2016 Yamaha VX - unit 1", today=T)
    Q.add_owned(s, title="2017 Yamaha VX - unit 2", today=T)
    assert len(Q.fleet(s)) == 2 and Q.ranked(s, T) == []


@pytest.mark.parametrize("kw", [dict(title=" "), dict(year=1900), dict(engine_hours=-3), dict(asking_price="abc"),
                                dict(hull_condition="great")])
def test_acq11_validation(s, kw):
    base = dict(title="VX")
    base.update(kw)
    with pytest.raises(ValidationError):
        Q.add_candidate(s, today=T, **base)



def test_acq12_cheap_scam_never_outranks_a_clean_listing(s):
    Q.save_settings(s, max_price=5000)
    clean = _vx(s, asking_price=5100, title_status="clean", engine_hours=180)  # $100 over the ceiling
    scam = _vx(s, asking_price=1900, title_status="missing", seller_statements="send a deposit, I'll ship it")
    order = [e.candidate.id for e in Q.ranked(s, T)]
    assert order.index(clean.id) < order.index(scam.id)
