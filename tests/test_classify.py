from ripaudit.classify import Inputs, classify, thresholds_for

T = {"short_review_seconds": 120, "short_high_seconds": 300, "short_review_percent": 3.0,
     "short_high_percent": 10.0, "long_review_seconds": 120, "long_review_percent": 3.0}


def matched(actual, expected, source="tmdb", **kw):
    return Inputs(probe_status="ok", actual_seconds=actual, identity_state="matched", expected_seconds=expected,
                  expected_source=source, **kw)


def test_exact_match_is_not_labeled_complete():
    v = classify(matched(7200, 7200), T)
    assert v.outcome == "no_runtime_issue"
    assert "does not prove" in v.reasons[0]
    assert "complete" not in v.outcome


def test_shortened_runtime_is_high_priority():
    v = classify(matched(7200 - 1200, 7200), T)
    assert (v.outcome, v.severity, v.runtime_state) == ("possible_incomplete", "high", "short")
    assert v.difference_seconds == -1200
    assert "-0:20:00" in v.reasons[0]


def test_moderately_short_is_review():
    v = classify(matched(7200 - 200, 7200), T)
    assert (v.outcome, v.severity) == ("possible_incomplete", "review")


def test_longer_edition():
    v = classify(matched(7200 + 900, 7200), T)
    assert (v.outcome, v.runtime_state) == ("edition_review", "long")


def test_plex_edition_softens_moderate_shortfall():
    v = classify(matched(7200 - 200, 7200, plex_edition="Theatrical"), T)
    assert v.outcome == "edition_review"
    assert any("Theatrical" in r for r in v.reasons)


def test_plex_edition_does_not_hide_severe_shortfall():
    v = classify(matched(3600, 7200, plex_edition="Extended"), T)
    assert (v.outcome, v.severity) == ("possible_incomplete", "high")


def test_missing_expected_runtime_is_unverified():
    v = classify(matched(7200, None), T)
    assert v.outcome == "unverified"


def test_percentage_threshold_catches_short_content():
    # 20-minute short missing 100 s: under the 120 s absolute tolerance but 8% of the content.
    v = classify(matched(1100, 1200, source="user_override"), T)
    assert v.outcome == "possible_incomplete"


def test_tmdb_minute_granularity_floor():
    review, high, longer = thresholds_for(1200, T, "tmdb")
    assert review == 60 and longer == 60
    assert classify(matched(1200 - 50, 1200), T).outcome == "no_runtime_issue"


def test_probe_errors():
    for status in ("failed", "timeout", "invalid_duration"):
        assert classify(Inputs(probe_status=status), T).outcome == "probe_error"


def test_pending_states():
    assert classify(Inputs(ready=False), T).outcome == "pending"
    assert classify(Inputs(probe_status=None), T).outcome == "pending"
    assert classify(Inputs(probe_status="ok", actual_seconds=10, identity_state="pending"), T).outcome == "pending"
    v = classify(Inputs(probe_status="ok", actual_seconds=10, identity_state="pending", identity_grace_expired=True), T)
    assert v.outcome == "unverified"


def test_ambiguous_identity_is_unverified():
    v = classify(Inputs(probe_status="ok", actual_seconds=7200, identity_state="ambiguous"), T)
    assert v.outcome == "unverified"


def test_user_override_works_without_identity():
    v = classify(Inputs(probe_status="ok", actual_seconds=7000, identity_state="pending",
                        expected_seconds=7010, expected_source="user_override"), T)
    assert v.outcome == "no_runtime_issue"


def test_approval_applies_only_to_exceptions():
    v = classify(matched(3600, 7200, approved=True, approval_reason="Disc is the short cut"), T)
    assert v.outcome == "approved_exception"
    assert "Disc is the short cut" in v.reasons[0]
    assert classify(matched(7200, 7200, approved=True, approval_reason="x"), T).outcome == "no_runtime_issue"


def test_multipart_waits_for_all_parts():
    v = classify(Inputs(probe_status="ok", actual_seconds=3600, parts_expected=2, parts_probed=1,
                        identity_state="matched", expected_seconds=7200, expected_source="tmdb"), T)
    assert v.outcome == "pending"
