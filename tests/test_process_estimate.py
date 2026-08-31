from __future__ import annotations

import unittest
from datetime import datetime

from lib.process_estimate import (
    blocked_paper_ids,
    current_step,
    estimate_remaining,
    fit_call_time_models,
    historical_phase_seconds,
    historical_step_seconds,
    latest_run,
)


class ProcessEstimateTests(unittest.TestCase):
    def test_latest_run_ignores_earlier_runs_in_the_same_log(self) -> None:
        log = """========== Review pipeline v4.2.2 2026-08-31 10:00:00 ==========
=== STEP 8/11: old ===
EVID4 P0001: 2 chunks
========== Review process v4.3.0 2026-08-31 12:00:00 ==========
=== STEP 6/11: current ===
MEMORY P0042: 4 text chunks
"""
        started, run = latest_run(log)
        self.assertEqual(started, datetime(2026, 8, 31, 12, 0, 0))
        self.assertNotIn("old", run)
        self.assertEqual(current_step(run), (6, 1))

    def test_historical_timings_sum_chunked_paper_work(self) -> None:
        log = """=== STEP 6/11: memory ===
MEMORY P0001: 2 text chunks
  CHUNK-DONE  P0001 chunk_0001 10.0s
  CHUNK-DONE  P0001 chunk_0002 20.0s
  MERGE-DONE  P0001 merge_l01_0001 15.0s
MEMORY P0002: 1 text chunk
  DIRECT-DONE P0002 35.0s
=== STEP 7/11: inventory ===
INV4    P0001: 2 chunks
  DIRECT-DONE P0001 80.0s
"""
        timing = historical_phase_seconds([log])
        self.assertEqual(timing["memory"], 40.0)
        self.assertEqual(timing["inventory"], 80.0)
        self.assertEqual(timing["evidence"], 105.0)

    def test_estimate_uses_only_work_remaining_after_current_step(self) -> None:
        current = """========== Review process v4.3.0 2026-08-31 12:00:00 ==========
=== STEP 7/11: inventory ===
INV4    P0001: 2 chunks
"""
        historical = """=== STEP 6/11: memory ===
MEMORY P0001: one
  DIRECT-DONE P0001 50.0s
=== STEP 7/11: inventory ===
INV4 P0001: one
  DIRECT-DONE P0001 100.0s
=== STEP 8/11: evidence ===
EVID4 P0001: one
  DIRECT-DONE P0001 120.0s
"""
        result = estimate_remaining(
            log_text=current,
            historical_logs=[historical],
            active_papers=10,
            missing_memory=9,
            missing_inventory=2,
            missing_evidence=3,
            pending_chunks={"inventory": 4, "evidence": 6},
            call_models={
                "inventory": {"seconds_per_chunk": 10.0, "sigma_per_chunk": 2.0, "sample_count": 50},
                "evidence": {"seconds_per_chunk": 20.0, "sigma_per_chunk": 4.0, "sample_count": 60},
            },
            now=datetime(2026, 8, 31, 12, 10, 0),
        )
        self.assertIsNotNone(result)
        assert result is not None
        # Step 7 must not count missing Step-6 memory work.
        self.assertEqual(result["estimate_seconds"], 370)
        self.assertEqual(result["uncertainty_seconds"], 120)
        self.assertEqual(result["remaining_low_seconds"], 250)
        self.assertEqual(result["step"], 7)

    def test_call_model_learns_retry_adjusted_chunk_time(self) -> None:
        model = fit_call_time_models(
            [
                ("extract_inventory_v4", "success", 10.0),
                ("extract_inventory_v4", "success", 20.0),
                ("extract_inventory_v4", "truncated", 30.0),
                ("summary_memory_v4", "success", 999.0),
            ]
        )["inventory"]
        self.assertEqual(model["sample_count"], 2)
        self.assertEqual(model["seconds_per_chunk"], 30.0)
        self.assertEqual(model["retry_factor"], 1.5)

    def test_step_timings_and_ocr_blocks_are_learned_from_logs(self) -> None:
        log = """========== Review process v4.3.0 2026-08-31 12:00:00 ==========
PROCESS-STEP-DONE step=2 seconds=40.0
PROCESS-STEP-DONE step=2 seconds=60.0
ERROR   P0078: OCR_REQUIRED: image-only PDF
ERROR   P0079: OCR_REQUIRED: image-only PDF
"""
        self.assertEqual(historical_step_seconds([log])[2], 50.0)
        self.assertEqual(blocked_paper_ids(log), {"P0078", "P0079"})


if __name__ == "__main__":
    unittest.main()
