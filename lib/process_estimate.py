from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Iterable


RUN_START_RE = re.compile(
    r"^=+\s*Review (?:process|pipeline).*?"
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*=+",
    re.MULTILINE | re.IGNORECASE,
)
STEP_RE = re.compile(r"^=== STEP (\d+)/11:", re.MULTILINE)
PAPER_RE = re.compile(r"\bP\d{4,}\b")
DONE_SECONDS_RE = re.compile(
    r"\b(?:DIRECT|CHUNK|MERGE)-DONE\b.*?\b(\d+(?:\.\d+)?)s\b",
    re.IGNORECASE,
)
PHASE_PAPER_RE = re.compile(r"^(?:MEMORY|INV4|EVID4)\s+(P\d{4,})\b")

PHASE_FOR_STEP = {6: "memory", 7: "inventory", 8: "evidence"}
FALLBACK_PHASE_SECONDS = {
    "memory": 75.0,
    "inventory": 105.0,
    "evidence": 105.0,
}


def latest_run(log_text: str) -> tuple[datetime | None, str]:
    """Return the timestamp and text for the last run in a daily log."""
    matches = list(RUN_START_RE.finditer(log_text))
    if not matches:
        return None, ""
    match = matches[-1]
    try:
        started_at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        started_at = None
    return started_at, log_text[match.start() :]


def current_step(run_text: str) -> tuple[int | None, int]:
    """Return the current Step and paper count observed within that Step."""
    matches = list(STEP_RE.finditer(run_text))
    if not matches:
        return None, 0
    match = matches[-1]
    return int(match.group(1)), len(set(PAPER_RE.findall(run_text[match.end() :])))


def historical_phase_seconds(log_texts: Iterable[str]) -> dict[str, float]:
    """Learn typical uncached per-paper LLM time for Steps 6, 7, and 8."""
    samples: dict[str, list[float]] = {name: [] for name in FALLBACK_PHASE_SECONDS}
    for text in log_texts:
        phase: str | None = None
        paper: str | None = None
        seconds = 0.0

        def finish_paper() -> None:
            nonlocal seconds
            if phase and paper and seconds > 0:
                samples[phase].append(seconds)
            seconds = 0.0

        for line in text.splitlines():
            step_match = STEP_RE.match(line)
            if step_match:
                finish_paper()
                phase = PHASE_FOR_STEP.get(int(step_match.group(1)))
                paper = None
                continue
            if not phase:
                continue
            paper_match = PHASE_PAPER_RE.match(line)
            if paper_match:
                if paper_match.group(1) != paper:
                    finish_paper()
                    paper = paper_match.group(1)
                continue
            done_match = DONE_SECONDS_RE.search(line)
            if paper and done_match:
                seconds += float(done_match.group(1))
        finish_paper()

    result: dict[str, float] = {}
    for phase, fallback in FALLBACK_PHASE_SECONDS.items():
        values = samples[phase]
        # Trim pathological provider stalls while retaining genuinely long papers.
        usable = [value for value in values if 1.0 <= value <= 7200.0]
        result[phase] = float(statistics.median(usable)) if usable else fallback
    return result


def estimate_remaining(
    *,
    log_text: str,
    historical_logs: Iterable[str] = (),
    typical_seconds: dict[str, float] | None = None,
    active_papers: int,
    missing_memory: int,
    missing_inventory: int,
    missing_evidence: int,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Estimate a running process from its current Step and unfinished artifacts.

    The result is deliberately a range: provider throttling, unusually long PDFs,
    and local model retries cannot be predicted exactly.
    """
    started_at, run_text = latest_run(log_text)
    if started_at is None:
        return None
    step, observed_papers = current_step(run_text)
    if step is None:
        return None

    typical = dict(typical_seconds) if typical_seconds is not None else historical_phase_seconds(historical_logs)
    remaining = 0.0
    if step <= 6:
        remaining += missing_memory * typical["memory"]
    if step <= 7:
        remaining += missing_inventory * typical["inventory"]
    if step <= 8:
        remaining += missing_evidence * typical["evidence"]

    # Parsing, validation, database updates, and graph construction are usually
    # short relative to uncached local-model work, but still need a floor.
    overhead_by_step = {
        2: 360.0,
        3: 330.0,
        4: 300.0,
        5: 270.0,
        6: 240.0,
        7: 210.0,
        8: 180.0,
        9: 150.0,
        10: 110.0,
        11: 75.0,
    }
    remaining += overhead_by_step.get(step, 60.0)

    current_time = now or datetime.now()
    elapsed = max(0.0, (current_time - started_at).total_seconds())
    provider_warning = bool(
        re.search(r"\b(?:HTTP\s*(?:429|500|502|503|504)|Retry-After|timeout)\b", run_text[-12000:], re.IGNORECASE)
    )

    # A range is more honest than a precise-looking point estimate. The upper
    # bound widens when the current log already shows provider or timeout issues.
    lower = max(30.0, remaining * 0.70)
    upper = max(lower + 30.0, remaining * (2.0 if provider_warning else 1.45))
    return {
        "step": step,
        "observed_papers": min(observed_papers, max(active_papers, 0)),
        "active_papers": max(active_papers, 0),
        "elapsed_seconds": round(elapsed),
        "remaining_low_seconds": round(lower),
        "remaining_high_seconds": round(upper),
        "provider_warning": provider_warning,
        "backlog": {
            "memory": max(missing_memory, 0),
            "inventory": max(missing_inventory, 0),
            "evidence": max(missing_evidence, 0),
        },
        "typical_seconds": {key: round(value, 1) for key, value in typical.items()},
    }
