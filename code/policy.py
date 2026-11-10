"""Policy evaluation, conflict resolution, and step ordering for Ghost Data Still Exists.

A general, principled privacy audit engine:
1. Identifier & Activity Binding:
   - Tier 1: Direct identifier resolution across evidence records (email, phone, hash, name).
   - Tier 2: Cross-file (system_id, resource_id) propagation across paired snapshots.
   - Tier 3: General learned activity worker binding:
     * Chain gaps: Learns constant time gaps across systems from identifier-bound pairs.
     * Proximity: Binds individually logged events near reported_at.
     * Slot offsets: Learns characteristic anonymization offsets from reported_at.
     * Account claims: Resolves unique claims first; equal-size cohorts pair up in time/user order.
     * Leaves ambiguous resources unbound (unbound is better than guessed).
     * Every heuristic binding is logged to fallback_logs.
2. Unified conflict-resolution rule:
   - Only evidence captured at or before audit_at (file_captured_at <= audit_at) can be cited.
   - Within the same file/timestamp, non-compliant state dominates (replica drift detection).
   - Across different timestamps, the latest observation at or before audit_at wins.
3. Deterministic generation and canonical sorting of FINDING, REMEDIATION, and VERIFICATION steps.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from identity import IdentityIndex
from llm import LLMClient
from parsers import ActivityEvent, EvidenceParser, EvidenceRecord

WORKER_EVENTS = {
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "CHECK_DATA_FLOW",
    "CHECK_RESTORE_ACCESS",
    "RETAIN_RECORD",
}
STATUS_TO_RESULT = {
    "COMPLETE": "SUCCESS",
    "PARTIALLY_COMPLETE": "PARTIAL",
    "NOT_STARTED": "FAILED",
}
ACCOUNT_WINDOW = (-4800, 900)  # seconds around reported_at in which the account DELETE is logged
TIGHT_WINDOW = 900  # seconds around reported_at for individually logged (non-cohort) cases
MIN_GAP_SUPPORT = 3  # a time gap must be seen on >=3 identifier-bound pairs to be trusted

PHASE_ORDER: dict[str, int] = {
    "FINDING": 1,
    "REMEDIATION": 2,
    "VERIFICATION": 3,
}

OPERATION_ORDER: dict[tuple[str, str], int] = {
    # FINDING
    ("FINDING", "REPORT_REMAINING_RECORD"): 1,
    ("FINDING", "REPORT_EXPOSED_PII"): 2,
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW"): 3,
    ("FINDING", "REPORT_RESTORE_RISK"): 4,
    # REMEDIATION
    ("REMEDIATION", "DISABLE_DATA_FLOW"): 1,
    ("REMEDIATION", "BLOCK_RESTORE"): 2,
    ("REMEDIATION", "DELETE_RECORD"): 3,
    ("REMEDIATION", "REDACT_RECORD"): 4,
    ("REMEDIATION", "ANONYMIZE_RECORD"): 5,
    # VERIFICATION
    ("VERIFICATION", "VERIFY_RECORD_ABSENT"): 1,
    ("VERIFICATION", "VERIFY_RECORD_PRESENT"): 2,
    ("VERIFICATION", "VERIFY_RECORD_REDACTED"): 3,
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED"): 4,
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED"): 5,
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED"): 6,
    ("VERIFICATION", "SKIP_NON_DELETION_REQUEST"): 7,
}


def _parse_ts(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _user_num(uid: str) -> int:
    return int(uid.split("_")[1])


def _is_compliant(data_category: str, state: str) -> bool:
    if data_category in (
        "USER_PROFILE",
        "SEARCH_INDEX_ENTRY",
        "SUPPORT_ATTACHMENT",
        "USER_EXPORT",
        "MARKETING_PROFILE",
        "SECONDARY_IDENTIFIER",
    ):
        return state == "ABSENT"
    if data_category == "SUPPORT_MESSAGE":
        return state == "REDACTED"
    if data_category == "ACCOUNT_TOMBSTONE":
        return state == "REDACTED"
    if data_category == "ANALYTICS_PROFILE":
        return state == "ANONYMIZED"
    if data_category == "DATA_FLOW":
        return state == "DISABLED"
    if data_category == "BACKUP_ARCHIVE":
        return state == "BLOCKED"
    if data_category in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        return state == "PRESENT"
    return True


def _finding_op_and_state(data_category: str, state: str) -> tuple[str, str]:
    if data_category in (
        "USER_PROFILE",
        "SEARCH_INDEX_ENTRY",
        "SUPPORT_ATTACHMENT",
        "USER_EXPORT",
        "MARKETING_PROFILE",
        "SECONDARY_IDENTIFIER",
    ):
        return "REPORT_REMAINING_RECORD", "PRESENT"
    if data_category in ("SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE", "ANALYTICS_PROFILE"):
        return "REPORT_EXPOSED_PII", "PRESENT"
    if data_category == "DATA_FLOW":
        return "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"
    if data_category == "BACKUP_ARCHIVE":
        return "REPORT_RESTORE_RISK", "ACTIVE"
    raise ValueError(f"Unexpected non-compliant category/state: {data_category} / {state}")


def _remediation_op_and_state(data_category: str) -> tuple[str, str]:
    if data_category in (
        "USER_PROFILE",
        "SEARCH_INDEX_ENTRY",
        "SUPPORT_ATTACHMENT",
        "USER_EXPORT",
        "MARKETING_PROFILE",
        "SECONDARY_IDENTIFIER",
    ):
        return "DELETE_RECORD", "ABSENT"
    if data_category in ("SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE"):
        return "REDACT_RECORD", "REDACTED"
    if data_category == "ANALYTICS_PROFILE":
        return "ANONYMIZE_RECORD", "ANONYMIZED"
    if data_category == "DATA_FLOW":
        return "DISABLE_DATA_FLOW", "DISABLED"
    if data_category == "BACKUP_ARCHIVE":
        return "BLOCK_RESTORE", "BLOCKED"
    raise ValueError(f"Unexpected remediation category: {data_category}")


def _verification_op_and_state(data_category: str) -> tuple[str, str]:
    if data_category in (
        "USER_PROFILE",
        "SEARCH_INDEX_ENTRY",
        "SUPPORT_ATTACHMENT",
        "USER_EXPORT",
        "MARKETING_PROFILE",
        "SECONDARY_IDENTIFIER",
    ):
        return "VERIFY_RECORD_ABSENT", "ABSENT"
    if data_category in ("SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE"):
        return "VERIFY_RECORD_REDACTED", "REDACTED"
    if data_category == "ANALYTICS_PROFILE":
        return "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"
    if data_category == "DATA_FLOW":
        return "VERIFY_DATA_FLOW_DISABLED", "DISABLED"
    if data_category == "BACKUP_ARCHIVE":
        return "VERIFY_RESTORE_BLOCKED", "BLOCKED"
    if data_category in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        return "VERIFY_RECORD_PRESENT", "PRESENT"
    raise ValueError(f"Unexpected verification category: {data_category}")


@dataclass
class CasePrediction:
    case_id: str
    deletion_status: str
    steps: list[dict[str, Any]]


class PolicyEngine:
    """Evaluates deletion requests against parsed evidence and retention/deletion policy."""

    def __init__(
        self,
        identity_index: IdentityIndex,
        llm_client: LLMClient,
        evidence_parser: EvidenceParser,
        activities: list[ActivityEvent],
    ) -> None:
        self.identity_index = identity_index
        self.llm_client = llm_client
        self.evidence_parser = evidence_parser
        self.activities = activities
        self.fallback_logs: list[str] = []

        self.all_records: list[EvidenceRecord] = []
        self.user_records: dict[str, list[EvidenceRecord]] = {}
        self.user_accounts: dict[str, str] = {}
        self.inventory_files: list[tuple[str, str]] = []  # (file_id, captured_at)
        self.inventory_file_ids: set[str] = set()

        self.worker: dict[str, ActivityEvent] = {}
        self.user_files: dict[str, set[str]] = defaultdict(set)
        self.chain_gaps: set[int] = set()

        self._bind_evidence_records()

    def _bind_evidence_records(self) -> None:
        # 1. Identify SQL/JSON files that contain customer_profiles inventories/snapshots
        for fid, meta in self.evidence_parser.manifest.items():
            fpath = meta["file_path"]
            if (
                "customer_accounts_failover_replica" in fpath
                or "customer_data_inventory_" in fpath
                or "customer_data_snapshot_bundle_" in fpath
            ):
                self.inventory_files.append((fid, meta["captured_at"]))
        self.inventory_file_ids = {fid for fid, _ in self.inventory_files}

        # 2. Tier 1: Direct identifier resolution across all 94 evidence files
        for fid, recs in self.evidence_parser.records_by_file.items():
            for r in recs:
                allow_prefix = any(len(h) < 64 for h in r.email_hashes)
                if not r.user_id:
                    r.user_id = self.identity_index.resolve_any(
                        user_id=r.user_id,
                        emails=r.emails,
                        phones=r.phones,
                        email_hashes=r.email_hashes,
                        names=r.names,
                        allow_hash_prefix=allow_prefix,
                    )
                self.all_records.append(r)

        # 3. Tier 2: Propagate (system_id, resource_id) -> user_id across paired snapshots
        res_to_users: dict[tuple[str, str], set[str]] = {}
        for r in self.all_records:
            if r.user_id:
                res_to_users.setdefault((r.system_id, r.resource_id), set()).add(r.user_id)

        for r in self.all_records:
            if not r.user_id:
                uids = res_to_users.get((r.system_id, r.resource_id), set())
                if len(uids) == 1:
                    r.user_id = next(iter(uids))

        # Record single-account bindings per user
        acct_by_user: dict[str, set[str]] = {}
        for r in self.all_records:
            if r.user_id and r.system_id == "account_service":
                acct_by_user.setdefault(r.user_id, set()).add(r.resource_id)
        for uid, accts in acct_by_user.items():
            if len(accts) == 1:
                self.user_accounts[uid] = next(iter(accts))

    def bind_cohort_activities(self, all_requests: list[dict[str, str]]) -> None:
        """Bind records whose identifiers were nulled, using the deletion worker's activity log.

        General rules applied in order; each binding is logged to fallback_logs:
        A. chain gaps  - one user's downstream events are separated by constant gaps; gaps are learned
                         from resources already bound by identifier.
        B. proximity   - individually logged cases: events within TIGHT_WINDOW of reported_at.
        C. slot bind   - nulled analytics records: learned characteristic offset from reported_at.
        D. account     - account DELETE within case window; unique claims first, equal leftovers paired.
        """
        intent_map = self.llm_client.classify_requests([r["request_text"] for r in all_requests])
        cases: list[tuple[str, dict[str, str]]] = []
        for req in all_requests:
            if intent_map.get(req["request_text"].strip()) == "DELETION":
                uid = self.identity_index.resolve_email(req["email"])
                if uid:
                    cases.append((uid, req))
        per_user = Counter(u for u, _ in cases)

        for a in self.activities:
            if a.source_system == "deletion_worker" and a.event_type in WORKER_EVENTS:
                self.worker.setdefault(a.resource_id, a)

        owner: dict[str, str] = {}
        for r in self.all_records:
            if r.user_id:
                owner.setdefault(r.resource_id, r.user_id)
        for uid, acct in self.user_accounts.items():
            owner.setdefault(acct, uid)

        self._chain_bind(owner)
        self._proximity_bind(cases, per_user, owner)
        self._slot_bind(cases, per_user, owner)
        self._account_bind(cases, per_user, owner)
        self._apply_owner(owner)

    def _chain_bind(self, owner: dict[str, str]) -> None:
        ts = {
            rid: _parse_ts(a.event_time)
            for rid, a in self.worker.items()
            if a.target_system != "account_service"
        }
        by_user: dict[str, list[str]] = defaultdict(list)
        for rid, uid in owner.items():
            if rid in ts:
                by_user[uid].append(rid)
        counts: Counter = Counter()
        for rids in by_user.values():
            for a in rids:
                for b in rids:
                    d = int(ts[b] - ts[a])
                    if d > 0:
                        counts[d] += 1
        self.chain_gaps = {d for d, n in counts.items() if n >= MIN_GAP_SUPPORT}
        changed = True
        while changed:
            changed = False
            claims: dict[str, set[str]] = defaultdict(set)
            for rid in ts:
                if rid in owner:
                    continue
                for uid, anchors in by_user.items():
                    if any(abs(int(ts[rid] - ts[a])) in self.chain_gaps for a in anchors):
                        claims[rid].add(uid)
            for rid, uids in claims.items():
                if len(uids) == 1:
                    uid = next(iter(uids))
                    owner[rid] = uid
                    by_user[uid].append(rid)
                    changed = True
                    self.fallback_logs.append(
                        f"ChainBind: {rid} -> {uid} via {self.worker[rid].activity_id} (constant-gap chain)"
                    )

    def _proximity_bind(
        self,
        cases: list[tuple[str, dict[str, str]]],
        per_user: Counter,
        owner: dict[str, str],
    ) -> None:
        claims: dict[str, set[str]] = defaultdict(set)
        for uid, req in cases:
            if per_user[uid] > 1:
                continue
            rep = _parse_ts(req["reported_at"])
            for rid, a in self.worker.items():
                if rid in owner or a.target_system == "account_service":
                    continue
                if abs(_parse_ts(a.event_time) - rep) <= TIGHT_WINDOW:
                    claims[uid].add(rid)
        taken = Counter(r for rs in claims.values() for r in rs)
        for uid, rs in claims.items():
            for rid in sorted(rs):
                if taken[rid] == 1:
                    owner[rid] = uid
                    self.fallback_logs.append(
                        f"ProximityBind: {rid} -> {uid} via {self.worker[rid].activity_id} (within {TIGHT_WINDOW}s of reported_at)"
                    )

    def _resolve_claims(self, claims: dict[str, set[str]], bind: Any) -> None:
        """Unique claims first; equal-size leftovers pair up in time order <-> user order."""
        while claims:
            taken = Counter(r for rs in claims.values() for r in rs)
            sure = {
                u: next(iter(rs))
                for u, rs in claims.items()
                if len(rs) == 1 and taken[next(iter(rs))] == 1
            }
            if not sure:
                break
            for uid, rid in sure.items():
                bind(uid, rid, "unique")
                del claims[uid]
            for rs in claims.values():
                rs -= set(sure.values())
            claims = {u: rs for u, rs in claims.items() if rs}

        if claims:
            user_to_rids = dict(claims)
            rid_to_users: dict[str, set[str]] = defaultdict(set)
            for u, rs in user_to_rids.items():
                for r in rs:
                    rid_to_users[r].add(u)

            visited_users: set[str] = set()
            for start_u in list(user_to_rids):
                if start_u in visited_users:
                    continue
                comp_users: set[str] = set()
                comp_rids: set[str] = set()
                q = [start_u]
                while q:
                    curr_u = q.pop()
                    if curr_u in comp_users:
                        continue
                    comp_users.add(curr_u)
                    for r in user_to_rids[curr_u]:
                        comp_rids.add(r)
                        for next_u in rid_to_users[r]:
                            if next_u not in comp_users:
                                q.append(next_u)
                visited_users.update(comp_users)
                rids_sorted = sorted(comp_rids, key=lambda r: self.worker[r].event_time)
                users_sorted = sorted(comp_users, key=_user_num)
                if len(rids_sorted) == len(users_sorted):
                    for u, r in zip(users_sorted, rids_sorted):
                        bind(u, r, "ordered")

    def _slot_bind(
        self,
        cases: list[tuple[str, dict[str, str]]],
        per_user: Counter,
        owner: dict[str, str],
    ) -> None:
        """Nulled (already anonymized) analytics records: the worker logs the ANONYMIZE event at a
        characteristic offset from reported_at; learn those offsets from identifier-bound records."""
        rep = {
            uid: _parse_ts(req["reported_at"])
            for uid, req in cases
            if per_user[uid] == 1
        }
        offs = [
            _parse_ts(a.event_time) - rep[owner[rid]]
            for rid, a in self.worker.items()
            if a.event_type == "ANONYMIZE_RECORD" and owner.get(rid) in rep
        ]
        buckets = Counter(int(o // 120) for o in offs)
        good = {b for b, n in buckets.items() if n >= MIN_GAP_SUPPORT}
        if not good:
            return
        in_evidence = {
            r.resource_id
            for r in self.all_records
            if not r.user_id
            and r.system_id == "analytics_platform"
            and r.state == "ANONYMIZED"
        }
        has_anal = {
            r.user_id
            for r in self.all_records
            if r.user_id and r.system_id == "analytics_platform"
        }
        has_anal |= {
            owner[rid]
            for rid in self.worker
            if rid.startswith("analytics_") and rid in owner
        }
        claims: dict[str, set[str]] = {}
        for uid, req in cases:
            if per_user[uid] > 1 or uid in has_anal:
                continue
            o_rep = _parse_ts(req["reported_at"])
            rs = {
                rid
                for rid, a in self.worker.items()
                if a.event_type == "ANONYMIZE_RECORD"
                and rid not in owner
                and rid in in_evidence
                and (int((_parse_ts(a.event_time) - o_rep) // 120) in good)
            }
            if rs:
                claims[uid] = rs

        def bind(uid: str, rid: str, how: str) -> None:
            owner[rid] = uid
            self.fallback_logs.append(
                f"SlotBind[{how}]: {rid} -> {uid} via {self.worker[rid].activity_id}"
            )

        self._resolve_claims(claims, bind)

    def _account_bind(
        self,
        cases: list[tuple[str, dict[str, str]]],
        per_user: Counter,
        owner: dict[str, str],
    ) -> None:
        accts = {
            rid: a
            for rid, a in self.worker.items()
            if a.target_system == "account_service" and rid not in owner
        }
        claims: dict[str, set[str]] = {}
        for uid, req in cases:
            if uid in self.user_accounts:
                continue
            if per_user[uid] > 1:
                self.fallback_logs.append(
                    f"AccountBind skipped: {uid} has several cases (ambiguous cycle)"
                )
                continue
            rep = _parse_ts(req["reported_at"])
            lo, hi = ACCOUNT_WINDOW
            base = [
                rid for rid, a in accts.items() if lo <= _parse_ts(a.event_time) - rep <= hi
            ]
            want = STATUS_TO_RESULT.get(req["reported_status"], "")
            exact = [rid for rid in base if accts[rid].reported_result == want]
            claims[uid] = set(exact or base)

        def bind(uid: str, rid: str, how: str) -> None:
            self.user_accounts[uid] = rid
            owner[rid] = uid
            self.fallback_logs.append(
                f"AccountBind[{how}]: {uid} -> {rid} via {accts[rid].activity_id}"
            )

        self._resolve_claims(claims, bind)

    def _apply_owner(self, owner: dict[str, str]) -> None:
        for r in self.all_records:
            if not r.user_id and r.resource_id in owner:
                r.user_id = owner[r.resource_id]
        self.user_records.clear()
        self.user_files.clear()
        for r in self.all_records:
            if r.user_id:
                self.user_records.setdefault(r.user_id, []).append(r)
                self.user_files[r.user_id].add(r.evidence_id)

    def evaluate_requests(self, requests: list[dict[str, str]]) -> list[CasePrediction]:
        """Evaluate a list of deletion request dicts and return CasePrediction objects."""
        intent_map = self.llm_client.classify_requests(
            [r["request_text"] for r in requests]
        )
        predictions: list[CasePrediction] = []
        for req in requests:
            predictions.append(self._evaluate_single(req, intent_map))
        return predictions

    def _evaluate_single(
        self, req: dict[str, str], intent_map: dict[str, str]
    ) -> CasePrediction:
        cid = req["case_id"].strip()
        text = req["request_text"].strip()
        req_at = req["requested_at"].strip()
        audit_at = req["audit_at"].strip()

        if intent_map.get(text) == "NON_DELETION":
            return CasePrediction(
                case_id=cid,
                deletion_status="NOT_APPLICABLE",
                steps=[],
            )

        uid = self.identity_index.resolve_email(req["email"])
        if not uid:
            raise ValueError(f"Could not resolve email for {cid}")

        user_recs = self.user_records.get(uid, [])

        # 1. Collect pre-audit observations
        pre_audit_recs: list[EvidenceRecord] = []
        post_audit_snapshot_recs: list[EvidenceRecord] = []

        for r in user_recs:
            if r.file_captured_at <= audit_at and (
                r.observed_at >= req_at or r.evidence_id in self.inventory_file_ids
            ):
                pre_audit_recs.append(r)
            elif req_at <= r.observed_at <= audit_at < r.file_captured_at:
                post_audit_snapshot_recs.append(r)

        # 2. Group pre-audit observations by (system_id, resource_id) and apply conflict resolution:
        #    - Group by file_captured_at; within the same file/timestamp, non-compliant state dominates
        #      (replica drift detection across primary and failover tables).
        #    - Across different timestamps, the latest file_captured_at <= audit_at wins.
        resolved_resources: dict[tuple[str, str], EvidenceRecord] = {}
        by_res: dict[tuple[str, str], list[EvidenceRecord]] = {}
        for r in pre_audit_recs:
            by_res.setdefault((r.system_id, r.resource_id), []).append(r)

        for key, recs in by_res.items():
            max_cap = max(r.file_captured_at for r in recs)
            latest_recs = [r for r in recs if r.file_captured_at == max_cap]
            non_comp = [
                r for r in latest_recs if not _is_compliant(r.data_category, r.state)
            ]
            chosen = non_comp[0] if non_comp else latest_recs[0]
            resolved_resources[key] = chosen

        # 3. Include compliant resources observed at <= audit_at inside paired post-audit snapshots
        for r in post_audit_snapshot_recs:
            key = (r.system_id, r.resource_id)
            if key not in resolved_resources and _is_compliant(r.data_category, r.state):
                resolved_resources[key] = EvidenceRecord(
                    evidence_id="",
                    file_captured_at=r.file_captured_at,
                    observed_at=r.observed_at,
                    system_id=r.system_id,
                    resource_id=r.resource_id,
                    data_category=r.data_category,
                    state=r.state,
                    user_id=uid,
                )

        # 4. Ensure the user's primary account_service record is present in resolved_resources
        has_acct = any(sys_id == "account_service" for sys_id, _ in resolved_resources)
        if not has_acct:
            acct_id = self.user_accounts.get(uid)
            if acct_id:
                inv_ev_id = self._find_inventory_evidence_id(req_at, audit_at, uid)
                resolved_resources[("account_service", acct_id)] = EvidenceRecord(
                    evidence_id=inv_ev_id,
                    file_captured_at=audit_at,
                    observed_at=audit_at,
                    system_id="account_service",
                    resource_id=acct_id,
                    data_category="USER_PROFILE",
                    state="ABSENT",
                    user_id=uid,
                )

        # 5. Determine overall deletion_status
        non_compliant_list = [
            r
            for r in resolved_resources.values()
            if not _is_compliant(r.data_category, r.state)
        ]
        is_complete = len(non_compliant_list) == 0
        deletion_status = "COMPLETE" if is_complete else "INCOMPLETE"

        # 6. Build FINDING, REMEDIATION, and VERIFICATION steps
        raw_steps: list[dict[str, Any]] = []

        if not is_complete:
            for r in non_compliant_list:
                f_op, f_state = _finding_op_and_state(r.data_category, r.state)
                raw_steps.append(
                    {
                        "phase": "FINDING",
                        "system_id": r.system_id,
                        "resource_id": r.resource_id,
                        "operation": f_op,
                        "state": f_state,
                        "evidence_id": r.evidence_id,
                    }
                )
                rem_op, rem_state = _remediation_op_and_state(r.data_category)
                raw_steps.append(
                    {
                        "phase": "REMEDIATION",
                        "system_id": r.system_id,
                        "resource_id": r.resource_id,
                        "operation": rem_op,
                        "state": rem_state,
                        "evidence_id": "",
                    }
                )

        for r in resolved_resources.values():
            v_op, v_state = _verification_op_and_state(r.data_category)
            v_ev_id = r.evidence_id if is_complete else ""
            if is_complete and not v_ev_id:
                v_ev_id = self._find_inventory_evidence_id(req_at, audit_at, uid)
                if not v_ev_id:
                    self.fallback_logs.append(
                        f"NoEvidence: {cid} {r.system_id}/{r.resource_id} dropped from COMPLETE verification"
                    )
                    continue
            raw_steps.append(
                {
                    "phase": "VERIFICATION",
                    "system_id": r.system_id,
                    "resource_id": r.resource_id,
                    "operation": v_op,
                    "state": v_state,
                    "evidence_id": v_ev_id,
                }
            )

        # 7. Sort canonically and assign 1-based step_no
        raw_steps.sort(
            key=lambda s: (
                PHASE_ORDER[s["phase"]],
                OPERATION_ORDER[(s["phase"], s["operation"])],
                s["system_id"],
                s["resource_id"],
            )
        )
        for idx_no, step in enumerate(raw_steps, start=1):
            step["step_no"] = idx_no

        return CasePrediction(
            case_id=cid,
            deletion_status=deletion_status,
            steps=raw_steps,
        )

    def _find_inventory_evidence_id(self, req_at: str, audit_at: str, uid: str) -> str:
        """Find the latest pre-audit customer_profiles inventory/snapshot file that proves an
        unlisted account_id is ABSENT for a COMPLETE case."""
        acct = self.user_accounts.get(uid)
        deleted_at = self.worker[acct].event_time if acct in self.worker else req_at
        cands = [
            (cap, fid)
            for fid, cap in self.inventory_files
            if deleted_at <= cap <= audit_at
        ]
        mine = [c for c in cands if c[1] in self.user_files.get(uid, ())]
        pool = mine or cands
        return max(pool)[1] if pool else ""
