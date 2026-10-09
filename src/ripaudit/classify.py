"""Runtime classification (pure functions, no I/O).

Threshold combination
---------------------
For an expected runtime E (seconds) and measured runtime A:

* shortfall = E - A
* review threshold  = min(short_review_seconds, short_review_percent% of E)
* high threshold    = min(short_high_seconds,  short_high_percent%  of E)
* longer threshold  = min(long_review_seconds, long_review_percent% of E)

Taking the *smaller* of the absolute and percentage values means a fixed
two-minute tolerance cannot hide a large proportional loss in short content.
When the expected runtime comes from TMDB (whole minutes), no threshold is
allowed below ``TMDB_GRANULARITY_SECONDS`` to avoid flagging rounding noise.

"No runtime issue detected" means only that the measured duration is within
tolerance of the reference. It never means the rip is verified complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TMDB_GRANULARITY_SECONDS = 60.0

OUTCOMES = {
    "pending": "Pending",
    "no_runtime_issue": "No runtime issue detected",
    "possible_incomplete": "Possible incomplete rip",
    "edition_review": "Edition review",
    "unverified": "Unverified",
    "probe_error": "Probe error",
    "approved_exception": "Approved exception",
    "missing": "File missing",
}
EXCEPTION_OUTCOMES = ("possible_incomplete", "edition_review", "unverified", "probe_error")


@dataclass
class Inputs:
    file_state: str = "present"
    ready: bool = True
    probe_status: str | None = None          # None => not probed for the current signature
    actual_seconds: float | None = None
    parts_expected: int = 1
    parts_probed: int = 1
    identity_state: str = "pending"          # pending | matched | manual | unmatched | ambiguous
    identity_grace_expired: bool = False
    expected_seconds: float | None = None
    expected_source: str | None = None       # tmdb | user_override
    plex_edition: str | None = None
    approved: bool = False
    approval_reason: str | None = None


@dataclass
class Verdict:
    outcome: str
    severity: str = "none"
    probe_state: str = "not_probed"
    identity_state: str = "pending"
    runtime_state: str = "unknown"
    health_state: str = "not_integrated"
    difference_seconds: float | None = None
    reasons: list[str] = field(default_factory=list)


def _fmt(seconds: float) -> str:
    sign = "-" if seconds < 0 else "+"
    s = abs(int(round(seconds)))
    return f"{sign}{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def thresholds_for(expected: float, t: dict, source: str | None) -> tuple[float, float, float]:
    review = min(float(t["short_review_seconds"]), expected * float(t["short_review_percent"]) / 100.0)
    high = min(float(t["short_high_seconds"]), expected * float(t["short_high_percent"]) / 100.0)
    longer = min(float(t["long_review_seconds"]), expected * float(t["long_review_percent"]) / 100.0)
    if source == "tmdb":
        review = max(review, TMDB_GRANULARITY_SECONDS)
        high = max(high, TMDB_GRANULARITY_SECONDS)
        longer = max(longer, TMDB_GRANULARITY_SECONDS)
    high = max(high, review)
    return review, high, longer


def classify(inp: Inputs, thresholds: dict) -> Verdict:
    v = Verdict(outcome="pending", identity_state=inp.identity_state,
                probe_state=inp.probe_status or "not_probed")
    if inp.file_state != "present":
        v.outcome = "missing"
        v.reasons.append("File is no longer present at this path.")
        return v
    if not inp.ready:
        v.reasons.append("Waiting for the file to be stable before inspection.")
        return v
    if inp.probe_status is None or inp.probe_status == "changed":
        v.reasons.append("Waiting for FFprobe inspection of the current file version.")
        return v
    if inp.probe_status in ("failed", "timeout", "invalid_duration"):
        v.outcome, v.severity = "probe_error", "review"
        v.reasons.append({"failed": "FFprobe could not read the file.",
                          "timeout": "FFprobe timed out.",
                          "invalid_duration": "FFprobe found no usable duration."}[inp.probe_status])
        return _apply_approval(v, inp)

    if inp.parts_probed < inp.parts_expected:
        v.reasons.append(f"Multipart movie: {inp.parts_probed} of {inp.parts_expected} parts inspected.")
        return v

    if inp.expected_source != "user_override":
        if inp.identity_state == "pending":
            if inp.identity_grace_expired:
                v.outcome, v.severity = "unverified", "review"
                v.reasons.append("Plex has not identified this file within the waiting period; still retrying.")
                return _apply_approval(v, inp)
            v.reasons.append("Waiting for Plex to index and identify this file.")
            return v
        if inp.identity_state in ("unmatched", "ambiguous"):
            v.outcome, v.severity = "unverified", "review"
            v.reasons.append("No trustworthy movie match; select a match or enter a source runtime."
                             if inp.identity_state == "ambiguous"
                             else "No external movie identifier is available for this file.")
            return _apply_approval(v, inp)

    if not inp.expected_seconds or inp.actual_seconds is None:
        v.outcome, v.severity = "unverified", "review"
        v.reasons.append("No independent expected runtime is available.")
        return _apply_approval(v, inp)

    expected, actual = float(inp.expected_seconds), float(inp.actual_seconds)
    diff = actual - expected
    v.difference_seconds = diff
    review, high, longer = thresholds_for(expected, thresholds, inp.expected_source)
    source_label = "your source runtime" if inp.expected_source == "user_override" else "TMDB runtime"
    detail = f"Measured {_fmt(actual)[1:]} vs {source_label} {_fmt(expected)[1:]} ({_fmt(diff)})."
    shortfall = -diff

    if shortfall > high:
        v.outcome, v.severity, v.runtime_state = "possible_incomplete", "high", "short"
        v.reasons.append(f"{detail} Shorter than the high-priority threshold of {high:.0f}s.")
    elif shortfall > review:
        v.outcome, v.severity, v.runtime_state = "possible_incomplete", "review", "short"
        v.reasons.append(f"{detail} Shorter than the review threshold of {review:.0f}s.")
    elif diff > longer:
        v.outcome, v.severity, v.runtime_state = "edition_review", "review", "long"
        v.reasons.append(f"{detail} Longer than expected by more than {longer:.0f}s; "
                         "check the edition or the movie match.")
    else:
        v.outcome, v.runtime_state = "no_runtime_issue", "within_tolerance"
        v.reasons.append(f"{detail} Within tolerance. This does not prove the rip is complete.")

    if inp.plex_edition and inp.expected_source == "tmdb" and v.runtime_state != "within_tolerance":
        v.reasons.append(f"Plex edition is '{inp.plex_edition}'; TMDB lists one general runtime that may "
                         "describe a different cut.")
        if v.outcome == "possible_incomplete" and v.severity == "review":
            v.outcome = "edition_review"
    if inp.probe_status == "ok_with_warnings":
        v.reasons.append("FFprobe reported warnings; review diagnostics.")
    return _apply_approval(v, inp)


def _apply_approval(v: Verdict, inp: Inputs) -> Verdict:
    if inp.approved and v.outcome in EXCEPTION_OUTCOMES:
        v.reasons.insert(0, f"Approved exception (underlying: {OUTCOMES[v.outcome]}): {inp.approval_reason}")
        v.outcome, v.severity = "approved_exception", "none"
    return v
