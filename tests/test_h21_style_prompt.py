"""H21 (flow_001719): stricter irony rule in the default style prompt."""

from newsbot.config import DEFAULT_STYLE_PROMPT


def test_sensitive_topics_rule_and_coroute_ban():
    # Irony allowed only on light topics.
    assert "только если тема лёгкая" in DEFAULT_STYLE_PROMPT
    # Sensitive topics: no irony or jokes at all, calm neutral tone, end on a fact.
    assert "детях" in DEFAULT_STYLE_PROMPT
    assert "болезнях" in DEFAULT_STYLE_PROMPT
    assert "психике" in DEFAULT_STYLE_PROMPT
    assert "иронии и шуток нет вовсе" in DEFAULT_STYLE_PROMPT
    assert "заканчивай фактом, а не панчлайном" in DEFAULT_STYLE_PROMPT
    # "короче" joins the banned stock phrases.
    assert "«короче»" in DEFAULT_STYLE_PROMPT
