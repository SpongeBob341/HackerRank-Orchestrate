"""Ground-truth sample scorer and custom evaluation suite for Ghost Data Still Exists.

Scores predictions on the 32 ground-truth cases in `dataset/sample_deletion_requests.csv`
against `dataset/sample_output.csv` across four metrics:
1. Case Status Accuracy (`deletion_status`)
2. Step-Set Precision, Recall, and F1 (matching `(phase, system_id, resource_id, operation, state, evidence_id)`)
3. Step Field Accuracy across aligned `(phase, system_id, resource_id)` tuples
4. Full Sequence Exact-Match Accuracy (both raw and excluding the two unlogged sample
   artifacts `audit_record_6645` in `case_18` and `audit_record_5569` in `case_20`)

Also executes 3 concrete custom evaluation test cases (`custom_case_01` .. `custom_case_03`)
covering replica drift, historical identity linkage, and negated non-deletion triage.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
from typing import Any

CODE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = CODE_DIR.parent
DATASET_DIR = ROOT_DIR / "dataset"
CACHE_DIR = CODE_DIR / "cache"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from identity import IdentityIndex
from llm import LLMClient
from parsers import EvidenceParser, EvidenceRecord, load_system_activity
from policy import PolicyEngine


def _step_tuple(s: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        s["phase"],
        s["system_id"],
        s["resource_id"],
        s["operation"],
        s["state"],
        s["evidence_id"],
    )


def score_samples(
    engine: PolicyEngine, sample_reqs: list[dict[str, str]], sample_outs: dict[str, dict[str, str]]
) -> dict[str, Any]:
    preds = engine.evaluate_requests(sample_reqs)

    status_correct = 0
    raw_exact_cases = 0
    adj_exact_cases = 0

    raw_tp = 0
    raw_fp = 0
    raw_fn = 0

    adj_tp = 0
    adj_fp = 0
    adj_fn = 0

    field_matches = 0
    field_total = 0

    discrepancies: list[dict[str, Any]] = []

    ignored_artifacts = {
        ("case_18", "audit_record_6645"),
        ("case_20", "audit_record_5569"),
    }

    for pred in preds:
        cid = pred.case_id
        gold_row = sample_outs[cid]
        gold_status = gold_row["deletion_status"].strip()
        gold_steps: list[dict[str, Any]] = json.loads(gold_row["steps_json"])

        if pred.deletion_status == gold_status:
            status_correct += 1

        pred_set = {_step_tuple(s) for s in pred.steps}
        gold_set = {_step_tuple(s) for s in gold_steps}

        # Adjusted gold steps (excluding the 2 unlogged sample artifacts in case_18 and case_20)
        adj_gold_steps = [
            s
            for s in gold_steps
            if (cid, s["resource_id"]) not in ignored_artifacts
        ]
        for idx_no, s in enumerate(adj_gold_steps, start=1):
            s = dict(s)
            s["step_no"] = idx_no
        adj_gold_set = {_step_tuple(s) for s in adj_gold_steps}

        raw_tp += len(pred_set & gold_set)
        raw_fp += len(pred_set - gold_set)
        raw_fn += len(gold_set - pred_set)

        adj_tp += len(pred_set & adj_gold_set)
        adj_fp += len(pred_set - adj_gold_set)
        adj_fn += len(adj_gold_set - pred_set)

        # Field accuracy across aligned (phase, system_id, resource_id)
        pred_by_key = {(s["phase"], s["system_id"], s["resource_id"]): s for s in pred.steps}
        for gs in adj_gold_steps:
            k = (gs["phase"], gs["system_id"], gs["resource_id"])
            if k in pred_by_key:
                ps = pred_by_key[k]
                for f in ("operation", "state", "evidence_id"):
                    field_total += 1
                    if ps[f] == gs[f]:
                        field_matches += 1

        if pred.deletion_status == gold_status and pred_set == gold_set:
            # Also check exact step_no ordering
            if [s["resource_id"] for s in pred.steps] == [s["resource_id"] for s in gold_steps]:
                raw_exact_cases += 1

        if pred.deletion_status == gold_status and pred_set == adj_gold_set:
            if [s["resource_id"] for s in pred.steps] == [
                s["resource_id"] for s in adj_gold_steps
            ]:
                adj_exact_cases += 1
            else:
                discrepancies.append(
                    {
                        "case_id": cid,
                        "type": "ORDER_MISMATCH",
                        "pred_order": [
                            (s["step_no"], s["phase"], s["system_id"], s["resource_id"])
                            for s in pred.steps
                        ],
                        "gold_order": [
                            (s["step_no"], s["phase"], s["system_id"], s["resource_id"])
                            for s in adj_gold_steps
                        ],
                    }
                )
        else:
            discrepancies.append(
                {
                    "case_id": cid,
                    "pred_status": pred.deletion_status,
                    "gold_status": gold_status,
                    "missing_steps": sorted(adj_gold_set - pred_set),
                    "extra_steps": sorted(pred_set - adj_gold_set),
                }
            )

    raw_prec = raw_tp / (raw_tp + raw_fp) if (raw_tp + raw_fp) else 0.0
    raw_rec = raw_tp / (raw_tp + raw_fn) if (raw_tp + raw_fn) else 0.0
    raw_f1 = (2 * raw_prec * raw_rec / (raw_prec + raw_rec)) if (raw_prec + raw_rec) else 0.0

    adj_prec = adj_tp / (adj_tp + adj_fp) if (adj_tp + adj_fp) else 0.0
    adj_rec = adj_tp / (adj_tp + adj_fn) if (adj_tp + adj_fn) else 0.0
    adj_f1 = (2 * adj_prec * adj_rec / (adj_prec + adj_rec)) if (adj_prec + adj_rec) else 0.0

    return {
        "total_cases": len(sample_reqs),
        "status_correct": status_correct,
        "status_accuracy": status_correct / len(sample_reqs),
        "raw_exact_cases": raw_exact_cases,
        "adj_exact_cases": adj_exact_cases,
        "raw_tp": raw_tp,
        "raw_fp": raw_fp,
        "raw_fn": raw_fn,
        "raw_precision": raw_prec,
        "raw_recall": raw_rec,
        "raw_f1": raw_f1,
        "adj_tp": adj_tp,
        "adj_fp": adj_fp,
        "adj_fn": adj_fn,
        "adj_precision": adj_prec,
        "adj_recall": adj_rec,
        "adj_f1": adj_f1,
        "field_matches": field_matches,
        "field_total": field_total,
        "field_accuracy": field_matches / field_total if field_total else 0.0,
        "discrepancies": discrepancies,
    }


def run_custom_cases(engine: PolicyEngine) -> list[dict[str, Any]]:
    """Execute the 3 custom evaluation cases defined in knowledge.md §9.2."""
    results: list[dict[str, Any]] = []

    # Custom Case 01: Multi-timestamp conflict & failover replica drift
    # Inject a synthetic user_custom_01 in identity_index and engine
    uid1 = "user_06"  # Uses user_06's search_001 replica drift scenario in evidence_57 + pre-audit compliant account
    custom_req_01 = {
        "case_id": "custom_case_01",
        "email": "jonas.berg@outlook.com",
        "requested_at": "2026-08-29T06:51:00Z",
        "reported_status": "PARTIALLY_COMPLETE",
        "reported_at": "2026-08-30T17:12:00Z",
        "audit_at": "2026-09-10T09:15:00Z",
        "request_text": "My account page is gone, but my old profile still appears when I search for it. Can you remove that search result too?",
    }
    pred_01 = engine.evaluate_requests([custom_req_01])[0]
    pass_01 = (
        pred_01.deletion_status == "INCOMPLETE"
        and len(pred_01.steps) == 4
        and pred_01.steps[0]["operation"] == "REPORT_REMAINING_RECORD"
        and pred_01.steps[0]["resource_id"] == "search_001"
        and pred_01.steps[0]["evidence_id"] == "evidence_57"
    )
    results.append(
        {
            "case_id": "custom_case_01",
            "title": "Replica Drift & Conflict Resolution",
            "passed": pass_01,
            "deletion_status": pred_01.deletion_status,
            "step_count": len(pred_01.steps),
        }
    )

    # Custom Case 02: Historical email + SHA-256 email_hash linkage with active data_flow
    custom_req_02 = {
        "case_id": "custom_case_02",
        "email": "mori.ren@gmail.com",
        "requested_at": "2026-09-06T11:00:00Z",
        "reported_status": "COMPLETE",
        "reported_at": "2026-09-09T11:00:00Z",
        "audit_at": "2026-09-26T11:00:00Z",
        "request_text": "I am asking you to remove my account and the personal information linked to it. Please review any identifiers connected to this account as part of the request. Please let me know once the request has been reviewed.",
    }
    pred_02 = engine.evaluate_requests([custom_req_02])[0]
    rem_ops_02 = [s["operation"] for s in pred_02.steps if s["phase"] == "REMEDIATION"]
    pass_02 = (
        pred_02.deletion_status == "INCOMPLETE"
        and rem_ops_02[0] == "DISABLE_DATA_FLOW"
        and "DELETE_RECORD" in rem_ops_02
        and "REDACT_RECORD" in rem_ops_02
        and "ANONYMIZE_RECORD" in rem_ops_02
    )
    results.append(
        {
            "case_id": "custom_case_02",
            "title": "Historical Identifier Equivalence & Remediation Ordering",
            "passed": pass_02,
            "deletion_status": pred_02.deletion_status,
            "step_count": len(pred_02.steps),
        }
    )

    # Custom Case 03: Negated Non-Deletion Request ("Do not close the account")
    custom_req_03 = {
        "case_id": "custom_case_03",
        "email": "elena.rostova@example.com",
        "requested_at": "2026-09-01T10:00:00Z",
        "reported_status": "NOT_STARTED",
        "reported_at": "2026-09-02T10:00:00Z",
        "audit_at": "2026-09-15T10:00:00Z",
        "request_text": "My surname is misspelled on my profile. Please update it without deleting my account or removing any of my records. Do not close the account.",
    }
    pred_03 = engine.evaluate_requests([custom_req_03])[0]
    pass_03 = (
        pred_03.deletion_status == "NOT_APPLICABLE"
        and len(pred_03.steps) == 0
    )
    results.append(
        {
            "case_id": "custom_case_03",
            "title": "Negated Non-Deletion Request Triage",
            "passed": pass_03,
            "deletion_status": pred_03.deletion_status,
            "step_count": len(pred_03.steps),
        }
    )

    return results


def main() -> None:
    idx = IdentityIndex(DATASET_DIR)
    llm = LLMClient(CACHE_DIR)
    activities = load_system_activity(DATASET_DIR)
    ep = EvidenceParser(DATASET_DIR, llm, activities)
    engine = PolicyEngine(idx, llm, ep, activities)

    with open(DATASET_DIR / "sample_deletion_requests.csv", mode="r", encoding="utf-8") as f:
        sample_reqs = list(csv.DictReader(f))
    with open(DATASET_DIR / "deletion_requests.csv", mode="r", encoding="utf-8") as f:
        eval_reqs = list(csv.DictReader(f))
    with open(DATASET_DIR / "sample_output.csv", mode="r", encoding="utf-8") as f:
        sample_outs = {r["case_id"].strip(): r for r in csv.DictReader(f)}

    engine.bind_cohort_activities(sample_reqs + eval_reqs)

    metrics = score_samples(engine, sample_reqs, sample_outs)
    custom_results = run_custom_cases(engine)

    print("=== SAMPLE BENCHMARK EVALUATION (32 CASES) ===")
    print(
        f"Case Status Accuracy:       {metrics['status_correct']}/{metrics['total_cases']} "
        f"({metrics['status_accuracy']*100:.2f}%)"
    )
    print(
        f"Raw Step-Set F1:            {metrics['raw_f1']*100:.2f}% "
        f"(TP={metrics['raw_tp']}, FP={metrics['raw_fp']}, FN={metrics['raw_fn']})"
    )
    print(
        f"Adjusted Step-Set F1:       {metrics['adj_f1']*100:.2f}% "
        f"(TP={metrics['adj_tp']}, FP={metrics['adj_fp']}, FN={metrics['adj_fn']})"
    )
    print(
        f"Field-Level Accuracy:       {metrics['field_matches']}/{metrics['field_total']} "
        f"({metrics['field_accuracy']*100:.2f}%)"
    )
    print(
        f"Raw Exact Sequence Match:   {metrics['raw_exact_cases']}/{metrics['total_cases']} "
        f"({metrics['raw_exact_cases']/metrics['total_cases']*100:.2f}%)"
    )
    print(
        f"Adj Exact Sequence Match:   {metrics['adj_exact_cases']}/{metrics['total_cases']} "
        f"({metrics['adj_exact_cases']/metrics['total_cases']*100:.2f}%)"
    )
    print(f"Tier 3 Fallback Log Count:  {len(engine.fallback_logs)}")

    if metrics["discrepancies"]:
        print("\nDiscrepancies:")
        for d in metrics["discrepancies"]:
            print(" ", d)

    print("\n=== CUSTOM EVALUATION SUITE (3 CASES) ===")
    for cr in custom_results:
        status_str = "PASS" if cr["passed"] else "FAIL"
        print(
            f"[{status_str}] {cr['case_id']} ({cr['title']}): "
            f"status={cr['deletion_status']}, steps={cr['step_count']}"
        )


if __name__ == "__main__":
    main()
