"""Main entry point for Ghost Data Still Exists.

Reads `dataset/deletion_requests.csv` (518 cases) and all supporting dataset tables and
evidence files, evaluates each request against privacy retention/deletion policies, and
writes `output.csv` in the repository root in exact numeric `case_id` order (`case_33` .. `case_550`).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys

CODE_DIR = Path(__file__).resolve().parent
ROOT_DIR = CODE_DIR.parent
DATASET_DIR = ROOT_DIR / "dataset"
CACHE_DIR = CODE_DIR / "cache"
OUTPUT_CSV = ROOT_DIR / "output.csv"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from identity import IdentityIndex
from llm import LLMClient
from parsers import EvidenceParser, load_system_activity
from policy import PolicyEngine

CANONICAL_STEP_KEYS = (
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
)


def main() -> None:
    identity_index = IdentityIndex(DATASET_DIR)
    llm_client = LLMClient(CACHE_DIR)
    activities = load_system_activity(DATASET_DIR)
    evidence_parser = EvidenceParser(DATASET_DIR, llm_client, activities)
    policy_engine = PolicyEngine(
        identity_index=identity_index,
        llm_client=llm_client,
        evidence_parser=evidence_parser,
        activities=activities,
    )

    with open(
        DATASET_DIR / "sample_deletion_requests.csv",
        mode="r",
        encoding="utf-8",
        newline="",
    ) as f:
        sample_requests = list(csv.DictReader(f))

    with open(
        DATASET_DIR / "deletion_requests.csv",
        mode="r",
        encoding="utf-8",
        newline="",
    ) as f:
        eval_requests = list(csv.DictReader(f))

    # Bind cohort activities across the full request set
    policy_engine.bind_cohort_activities(sample_requests + eval_requests)

    # Evaluate the 518 evaluation requests preserving numeric case_id order
    eval_requests_sorted = sorted(
        eval_requests, key=lambda r: int(r["case_id"].strip().split("_")[1])
    )
    predictions = policy_engine.evaluate_requests(eval_requests_sorted)

    with open(OUTPUT_CSV, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for pred in predictions:
            ordered_steps = [
                {k: step[k] for k in CANONICAL_STEP_KEYS} for step in pred.steps
            ]
            steps_json = json.dumps(ordered_steps, ensure_ascii=False)
            writer.writerow([pred.case_id, pred.deletion_status, steps_json])

    status_counts: dict[str, int] = {}
    total_steps = 0
    for pred in predictions:
        status_counts[pred.deletion_status] = (
            status_counts.get(pred.deletion_status, 0) + 1
        )
        total_steps += len(pred.steps)

    print(f"Wrote {len(predictions)} predictions to {OUTPUT_CSV}")
    print(f"Status breakdown: {status_counts}")
    print(f"Total steps emitted: {total_steps}")
    print(f"Tier 3 fallback invocations logged: {len(policy_engine.fallback_logs)}")

    # Strict post-generation schema verification
    with open(OUTPUT_CSV, mode="r", encoding="utf-8") as f:
        written_rows = list(csv.DictReader(f))
    assert len(written_rows) == 518, f"Expected 518 rows, got {len(written_rows)}"
    case_ints = [int(r["case_id"].split("_")[1]) for r in written_rows]
    assert case_ints == sorted(case_ints), "Rows not in ascending numeric case_id order"
    for r in written_rows:
        cid = r["case_id"]
        st = r["deletion_status"]
        s_arr = json.loads(r["steps_json"])
        assert [x["step_no"] for x in s_arr] == list(range(1, len(s_arr) + 1)), f"{cid} non-consecutive step_no"
        if st == "NOT_APPLICABLE":
            assert s_arr == [], f"{cid} NOT_APPLICABLE must be empty array"
        elif st == "INCOMPLETE":
            assert all(x["evidence_id"] == "" for x in s_arr if x["phase"] != "FINDING"), f"{cid} INCOMPLETE non-empty remediation/verification evidence_id"
        elif st == "COMPLETE":
            assert all(x["phase"] == "VERIFICATION" and x["evidence_id"] for x in s_arr), f"{cid} COMPLETE must only have verified steps with evidence_id"
    print("Schema self-check: OK")


if __name__ == "__main__":
    main()
