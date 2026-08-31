from __future__ import annotations

import re
import math
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
STEP_DONE_RE = re.compile(r"^PROCESS-STEP-DONE\s+step=(\d+)\s+seconds=(\d+(?:\.\d+)?)", re.MULTILINE)
OCR_BLOCKED_RE = re.compile(r"^ERROR\s+(P\d{4,}):\s+OCR_REQUIRED\b", re.MULTILINE)

PHASE_FOR_STEP = {6: "memory", 7: "inventory", 8: "evidence"}
FALLBACK_PHASE_SECONDS = {
    "memory": 75.0,
    "inventory": 105.0,
    "evidence": 105.0,
}
FALLBACK_STEP_SECONDS = {2: 60.0, 3: 30.0, 4: 120.0, 5: 60.0, 9: 60.0, 10: 60.0, 11: 30.0}
LLM_STAGE_PHASE = {"extract_inventory_v4": "inventory", "extract_evidence_v4": "evidence"}


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


def blocked_paper_ids(log_text: str) -> set[str]:
    """Return image-only PDFs that cannot advance without OCR in the latest run."""
    _, run_text = latest_run(log_text)
    return set(OCR_BLOCKED_RE.findall(run_text))


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


def historical_step_seconds(log_texts: Iterable[str]) -> dict[int, float]:
    """Learn wall-clock Step durations from timestamped markers in newer logs."""
    samples: dict[int, list[float]] = {}
    for text in log_texts:
        for step_text, seconds_text in STEP_DONE_RE.findall(text):
            step = int(step_text)
            seconds = float(seconds_text)
            if 0.0 <= seconds <= 7 * 86400:
                samples.setdefault(step, []).append(seconds)
    result = dict(FALLBACK_STEP_SECONDS)
    for step, values in samples.items():
        result[step] = float(statistics.median(values))
    return result


def fit_call_time_models(rows: Iterable[tuple[str, str, float]]) -> dict[str, dict[str, float | int]]:
    """Fit a small empirical model from saved LLM call durations.

    Each successful Step-7/8 call produces one chunk. Dividing all attempt time
    (including truncated/error attempts) by successful chunks learns both local
    model speed and the observed retry rate without an external ML dependency.
    """
    grouped: dict[str, list[tuple[str, float]]] = {"inventory": [], "evidence": []}
    for stage, status, raw_seconds in rows:
        phase = LLM_STAGE_PHASE.get(stage)
        seconds = float(raw_seconds)
        if phase and 0.0 <= seconds <= 1800.0:
            grouped[phase].append((status, seconds))

    models: dict[str, dict[str, float | int]] = {}
    for phase, values in grouped.items():
        success_count = sum(status == "success" for status, _ in values)
        if not success_count:
            continue
        durations = [seconds for _, seconds in values]
        retry_factor = len(values) / success_count
        seconds_per_chunk = sum(durations) / success_count
        spread = statistics.pstdev(durations) if len(durations) > 1 else seconds_per_chunk * 0.25
        models[phase] = {
            "sample_count": success_count,
            "seconds_per_chunk": seconds_per_chunk,
            "sigma_per_chunk": max(1.0, spread * math.sqrt(retry_factor)),
            "retry_factor": retry_factor,
        }
    return models


def estimate_remaining(
    *,
    log_text: str,
    historical_logs: Iterable[str] = (),
    typical_seconds: dict[str, float] | None = None,
    active_papers: int,
    missing_memory: int,
    missing_inventory: int,
    missing_evidence: int,
    pending_chunks: dict[str, int] | None = None,
    call_models: dict[str, dict[str, float | int]] | None = None,
    learned_step_seconds: dict[int, float] | None = None,
    blocked_count: int = 0,
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
    variance = 0.0
    pending_chunks = pending_chunks or {}
    call_models = call_models or {}

    def add_phase(phase: str, paper_count: int) -> None:
        nonlocal remaining, variance
        model = call_models.get(phase)
        chunks = max(0, int(pending_chunks.get(phase, 0)))
        if model and chunks > 0:
            per_chunk = float(model["seconds_per_chunk"])
            sigma = float(model["sigma_per_chunk"])
            remaining += chunks * per_chunk
            variance += chunks * sigma * sigma
        else:
            per_paper = typical[phase]
            remaining += paper_count * per_paper
            variance += paper_count * (per_paper * 0.35) ** 2

    if step <= 6:
        add_phase("memory", max(missing_memory, 0))
    if step <= 7:
        add_phase("inventory", max(missing_inventory, 0))
    if step <= 8:
        add_phase("evidence", max(missing_evidence, 0))

    step_seconds = dict(FALLBACK_STEP_SECONDS)
    if learned_step_seconds:
        step_seconds.update(learned_step_seconds)
    # Steps 6-8 are predicted from actual unfinished papers/chunks above.
    for number in (2, 3, 4, 5, 9, 10, 11):
        if number >= step:
            duration = max(0.0, float(step_seconds[number]))
            remaining += duration
            variance += (duration * 0.30) ** 2
    remaining += 60.0  # project-specific graph/report finalization
    variance += 30.0**2

    current_time = now or datetime.now()
    elapsed = max(0.0, (current_time - started_at).total_seconds())
    provider_warning = bool(
        re.search(r"\b(?:HTTP\s*(?:429|500|502|503|504)|Retry-After|timeout)\b", run_text[-12000:], re.IGNORECASE)
    )

    # Statistical error shrinks as many papers/chunks are averaged. Retain a
    # 10% systematic floor for model-speed drift and widen only on provider errors.
    uncertainty = max(120.0, math.sqrt(variance), remaining * (0.30 if provider_warning else 0.10))
    lower = max(30.0, remaining - uncertainty)
    upper = max(lower + 30.0, remaining + uncertainty)
    training_samples = sum(int(model.get("sample_count", 0)) for model in call_models.values())
    return {
        "step": step,
        "observed_papers": min(observed_papers, max(active_papers, 0)),
        "active_papers": max(active_papers, 0),
        "elapsed_seconds": round(elapsed),
        "remaining_low_seconds": round(lower),
        "remaining_high_seconds": round(upper),
        "estimate_seconds": round(remaining),
        "uncertainty_seconds": round(uncertainty),
        "provider_warning": provider_warning,
        "blocked_papers": max(0, blocked_count),
        "backlog": {
            "memory": max(missing_memory, 0),
            "inventory": max(missing_inventory, 0),
            "evidence": max(missing_evidence, 0),
        },
        "typical_seconds": {key: round(value, 1) for key, value in typical.items()},
        "model": {
            "kind": "historical chunk-time model",
            "training_samples": training_samples,
            "pending_chunks": {key: max(0, int(value)) for key, value in pending_chunks.items()},
            "seconds_per_chunk": {
                key: round(float(value["seconds_per_chunk"]), 1) for key, value in call_models.items()
            },
        },
    }
