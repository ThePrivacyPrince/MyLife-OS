"""NEG-* : offer planning from inspection findings - walk-away, fair and great offers, all explained."""
import datetime as dt
from decimal import Decimal

import pytest

from lifeos.engines import acquisition as A
from lifeos.engines.negotiation import plan_offers, round_down, talk_track
from lifeos.errors import ValidationError
from lifeos.services import acquisitions as Q
from lifeos.services import daily_check as DC
from lifeos.services import money as M

D = Decimal
DEC = dt.date(2026, 12, 1)   # off-season
JUL = dt.date(2027, 7, 1)    # peak season


def _vx(s, today=DEC, **kw):
    base = dict(title="2019 Yamaha VX Cruiser", model="VX Cruiser", year=2019, asking_price=4800,
                title_status="clean", engine_hours=120)
    base.update(kw)
    return Q.add_candidate(s, today=today, **base)


def _clean(s, c):
    for k in A.CRITICAL:
        Q.set_inspection(s, c.id, k, "pass")


def test_neg01_rounding():
    assert round_down(D("4437.9")) == D(4425) and round_down(D(4400)) == D(4400)


def test_neg02_clean_ski_in_peak_season_uses_the_base_margin(s):
    Q.save_settings(s, max_price=5000)
    c = _vx(s, today=JUL)
    _clean(s, c)
    p = Q.offer_plan(s, c, JUL)
    assert p.ready and p.signals == [] and p.margin == D("0.08")
    assert (p.reference, p.value_to_you, p.walk_away) == (D(4800), D(4800), D(4800))
    assert (p.fair, p.great) == (D(4600), D(4400))  # 4800*0.96=4608 -> 4600 ; 4800*0.92=4416 -> 4400


def test_neg03_findings_are_deducted_and_leverage_widens_the_margin(s):
    Q.save_settings(s, max_price=5000)
    c = _vx(s, today=DEC - dt.timedelta(days=40), cash_ready=True)
    Q.update_candidate(s, c.id, today=DEC - dt.timedelta(days=5), asking_price=4600)
    _clean(s, c)
    Q.set_inspection(s, c.id, "pump", "fail", "Wear ring gap, chipped impeller", cost=180)
    Q.set_inspection(s, c.id, "cosmetic", "fail", "Torn seat", cost=120)
    assert c.est_repairs == D(300), "fix costs roll up into repairs"
    p = Q.offer_plan(s, c, DEC)
    assert p.reference == D(4600) and p.value_to_you == D(4300)
    labels = [x for x, _ in p.signals]
    assert any("Listed 40 days" in x for x in labels) and any("dropped $200" in x for x in labels)
    assert any("Off-season" in x for x in labels) and any("cash" in x for x in labels)
    assert any("Minor issues" in x for x in labels)
    assert p.margin == D("0.20"), "8% + 3+2+3+2+2 = 20% (the cap)"
    assert (p.walk_away, p.fair, p.great) == (D(4300), D(3850), D(3425))
    assert p.great < p.fair < p.walk_away


def test_neg04_comps_and_ceiling_and_fund_cap_the_walk_away(s):
    Q.save_settings(s, max_price=4500)
    _vx(s, asking_price=4200)
    _vx(s, asking_price=4400)
    c = _vx(s, asking_price=5200, transport_cost=100, prep_cost=150)
    _clean(s, c)
    p = Q.offer_plan(s, c, JUL)
    assert p.reference == D("4300") and "median of 2 similar clean listings" in p.reference_basis
    assert p.walk_away == D(4300)
    fund = M.create_fund(s, name="Ski purchases", space="business", target=20000, today=JUL)
    M.add_entry(s, fund.id, "add", 4000, today=JUL)
    Q.set_funding(s, c.id, fund.id)
    p = Q.offer_plan(s, c, JUL)
    assert p.walk_away == D(3750), "fund 4000 - transport 100 - prep 150"
    cushion = M.create_fund(s, name="Cushion", space="reserves", target=8000, today=JUL)
    M.add_entry(s, cushion.id, "add", 8000, today=JUL)
    Q.set_funding(s, c.id, cushion.id)
    assert Q.offer_plan(s, c, JUL).walk_away == D(4300), "protected reserves are never counted as spendable"


def test_neg05_dealbreakers_mean_no_prices(s):
    c = _vx(s)
    Q.set_inspection(s, c.id, "compression", "dealbreaker", "Cylinder 2 at 60 psi")
    p = Q.offer_plan(s, c, DEC)
    assert p.dealbreakers and p.walk_away is None and "Don't negotiate" in talk_track(c, p)
    scam = _vx(s, title_status="missing", seller_statements="pay a deposit and I'll ship")
    assert len(Q.offer_plan(s, scam, DEC).dealbreakers) == 2


def test_neg06_unknowns_block_the_math_instead_of_guessing(s):
    c = _vx(s, asking_price=None)
    assert "Asking price is unknown." in Q.offer_plan(s, c, DEC).incomplete
    d = _vx(s)
    Q.set_inspection(s, d.id, "leaks", "fail", "Oil weeping at the head")
    p = Q.offer_plan(s, d, DEC)
    assert p.incomplete and "fix cost" in p.incomplete[0] and p.walk_away is None


def test_neg07_talk_track_uses_findings_and_hides_the_walk_away(s):
    Q.save_settings(s, max_price=5000)
    c = _vx(s, cash_ready=True)
    _clean(s, c)
    Q.set_inspection(s, c.id, "pump", "fail", "Wear ring", cost=180)
    p = Q.offer_plan(s, c, DEC)
    text = talk_track(c, p)
    assert "wear ring (about $180)" in text and f"${p.great:,.0f}" in text and f"${p.fair:,.0f}" in text
    assert "don't say it" in text and "cash" in text


def test_neg08_offer_log_and_send_to_tomorrows_ask(s):
    c = _vx(s)
    with pytest.raises(ValidationError):
        Q.record_offer(s, c.id, 4200, "countered")
    Q.record_offer(s, c.id, 4200, "countered", counter=4550, note="Said a dealer offered 4500")
    Q.record_offer(s, c.id, 4400, "accepted")
    assert (c.offer, c.agreed_price, c.stage) == (D(4400), D(4400), "negotiating")
    assert [e.to_value for e in Q.negotiation_log(s, c)] == ["countered $4,550", "accepted"]
    DC.save_check(s, for_date=DEC + dt.timedelta(days=1), today=DEC, protect_note="Sleep after overnight")
    chk = DC.set_ask(s, for_date=DEC + dt.timedelta(days=1), today=DEC, who="Seller - VX Kingston", area="price",
                     what="$4,200 (fallback $4,350)", script="I can do $4,200 cash today.")
    assert chk.protect_note == "Sleep after overnight", "sending an ask never wipes the rest of the plan"
    assert chk.ask_area == "price" and not chk.no_ask


def test_neg09_older_inspections_still_count_and_na_rules(s):
    c = _vx(s)
    with pytest.raises(ValidationError):
        Q.set_inspection(s, c.id, "title_id", "na")
    with pytest.raises(ValidationError):
        Q.set_inspection(s, c.id, "nope", "pass")
    Q.set_inspection(s, c.id, "trailer", "na")
    _clean(s, c)
    assert A.inspection_state(c) == "passed" and c.est_repairs == D(0)



def test_neg10_priced_critical_fix_still_passes_and_preliminary_is_labelled(s):
    Q.save_settings(s, max_price=5000)
    c = _vx(s)
    pre = Q.offer_plan(s, c, DEC)
    assert pre.ready and pre.preliminary and "subject" not in talk_track(c, pre).lower() and \
        "Before the inspection" in talk_track(c, pre)
    _clean(s, c)
    Q.set_inspection(s, c.id, "pump", "fail", "Wear ring", cost=180)
    assert A.inspection_state(c) == "passed_with_fixes" and A.inspected(c)
    assert not any(f.level == "risk" for f in A.risk_flags(c, DEC))
    assert {g.key: g.passed for g in Q.evaluate(s, c, DEC).gates}["inspection"]
    assert not Q.offer_plan(s, c, DEC).preliminary
    Q.set_inspection(s, c.id, "pump", "fail", "Wear ring")  # estimate removed -> not priced -> failed
    assert A.inspection_state(c) == "failed"



def test_neg11_red_flag_listings_never_set_the_market(s):
    _vx(s, asking_price=1900, title_status="missing", seller_statements="send a deposit and I'll ship it")
    _vx(s, asking_price=5100)
    c = _vx(s, asking_price=4700)
    assert Q.comparable_prices(s, c) == [D(5100)] and Q.comps_median(s, c) is None
    _vx(s, asking_price=4500)
    assert Q.comps_median(s, c) == D(4800)
    assert "2 similar clean listings" not in Q.offer_plan(s, c, DEC).reference_basis  # 4800 > asking -> asking used
