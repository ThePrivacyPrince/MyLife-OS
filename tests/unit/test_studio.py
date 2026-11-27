"""STU-*: Studio - brands, content from idea to posted, disclosure, follow-ups."""
import datetime as dt

import pytest

from lifeos.errors import ValidationError
from lifeos.services import studio as ST

T = dt.date(2026, 11, 27)


def test_stu01_brands_status_and_follow_ups(s):
    b = ST.add_brand(s, "Valentino Beauty", T, contact="PR rep (via gifting package)", status="gifted")
    with pytest.raises(ValidationError):
        ST.add_brand(s, "valentino beauty", T)  # duplicate, any case
    with pytest.raises(ValidationError):
        ST.add_brand(s, "X", T, status="besties")
    ST.update_brand(s, b.id, status="talking", follow_up=T + dt.timedelta(days=2))
    assert not ST.due(s, T).follow_ups and ST.due(s, T + dt.timedelta(days=2)).follow_ups == [b]
    ST.update_brand(s, b.id, status="paused")
    assert not ST.due(s, T + dt.timedelta(days=5)).follow_ups  # paused brands don't nag
    assert [e.event_type for e in ST.history(s, "brand", b.id)] == ["created", "status", "follow_up", "status"]


def test_stu02_content_moves_idea_to_posted_with_disclosure_and_auto_follow_up(s):
    b = ST.add_brand(s, "Valentino Beauty", T, status="gifted")
    c = ST.add_content(s, "Vendetta - 15s commercial", T, brand_id=b.id, shoot_date=T + dt.timedelta(days=1),
                       post_date=T + dt.timedelta(days=4), platform="Instagram", plan="Dark set, gold reflections")
    assert c.status == "planned"
    with pytest.raises(ValidationError):
        ST.add_content(s, "bad", T, shoot_date=T, post_date=T - dt.timedelta(days=1))
    day = T + dt.timedelta(days=1)
    assert ST.due(s, day).shoots == [c]
    for _ in range(2):
        ST.advance(s, c.id, day)  # planned -> shot -> edited
    assert c.status == "edited" and not ST.due(s, day).shoots
    post_day = T + dt.timedelta(days=4)
    assert ST.due(s, post_day).posts == [c]
    with pytest.raises(ValidationError):
        ST.advance(s, c.id, post_day)  # brand content needs disclosure
    ST.advance(s, c.id, post_day, link="instagram.com/p/xyz", disclosed=True)
    assert c.status == "posted" and c.posted_on == post_day and c.link == "instagram.com/p/xyz"
    assert b.follow_up == post_day + dt.timedelta(days=ST.FOLLOW_UP_AFTER_POST)  # every post opens the next conversation
    with pytest.raises(ValidationError):
        ST.advance(s, c.id, post_day)
    assert ST.scoreboard(s, post_day) == {"posted_this_month": 1, "in_production": 0, "brands_active": 1, "paid_brands": 0}


def test_stu03_own_content_posts_without_brand_and_keeps_existing_follow_up(s):
    own = ST.add_content(s, "Jet Life sunset ride", T)
    assert own.status == "idea"
    for _ in range(4):
        ST.advance(s, own.id, T)
    assert own.status == "posted"
    b = ST.add_brand(s, "YSL Beauty", T, status="gifted", follow_up=T + dt.timedelta(days=10))
    c = ST.add_content(s, "MYSLF review", T, brand_id=b.id)
    ST.update_content(s, c.id, disclosed=True, post_date=T + dt.timedelta(days=1))
    for _ in range(4):
        ST.advance(s, c.id, T)
    assert b.follow_up == T + dt.timedelta(days=10)  # an existing follow-up is not overwritten
