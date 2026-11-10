"""Unified evidence and activity parsers for Ghost Data Still Exists.

Parses all 94 evidence files referenced in `dataset/evidence_files.csv`:
- `.sql` database dumps executed in in-memory `sqlite3`
- `.json` snapshot bundles, service exports, manifests, and audit ledgers
- `.log` observability exports, service readback traces, and lookup logs
- `.txt` shift handovers
- `.png` and `.pdf` multimodal extractions via `LLMClient`
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from llm import LLMClient

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_ID_RE = re.compile(r"^user_\d+$")
HEX64_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
LINK_RE = re.compile(
    r"(?:linked|confirmed)\s+([a-z0-9_]+)\s+\(([^)]+)\)\s+"
    r"(?:to|belongs only to)\s+([^\s]+)\s+and\s+(user_\d+)"
)


@dataclass
class EvidenceRecord:
    """Normalized point-in-time observation of a resource in an evidence file."""

    evidence_id: str
    file_captured_at: str
    observed_at: str
    system_id: str
    resource_id: str
    data_category: str
    state: str  # PRESENT, ABSENT, REDACTED, ANONYMIZED, ACTIVE, DISABLED, BLOCKED
    user_id: str | None = None
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    source_table: str = ""


@dataclass
class ActivityEvent:
    """Row from dataset/system_activity.csv."""

    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


def _add_identifier(
    val: Any,
    emails: set[str],
    phones: set[str],
    names: set[str],
    hashes: set[str],
    user_id_box: list[str | None],
) -> None:
    if val is None:
        return
    s = str(val).strip()
    if not s or s.lower() in ("null", "none", "[redacted]", "no direct identifier"):
        return
    if USER_ID_RE.match(s):
        user_id_box[0] = s
        return
    m_hash = HEX64_RE.match(s)
    if m_hash:
        h = m_hash.group(1).lower()
        if not h.startswith("000000000000"):
            hashes.add(h)
        return
    if "@" in s and EMAIL_RE.fullmatch(s):
        emails.add(s.lower())
        return
    for em in EMAIL_RE.findall(s):
        emails.add(em.lower())
    if s.startswith("+") and sum(c.isdigit() for c in s) >= 7:
        phones.add(s)


def load_system_activity(dataset_dir: Path) -> list[ActivityEvent]:
    path = dataset_dir / "system_activity.csv"
    events: list[ActivityEvent] = []
    with open(path, mode="r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            events.append(
                ActivityEvent(
                    activity_id=row["activity_id"].strip(),
                    event_time=row["event_time"].strip(),
                    event_type=row["event_type"].strip(),
                    source_system=row["source_system"].strip(),
                    target_system=row["target_system"].strip(),
                    resource_id=row["resource_id"].strip(),
                    reported_result=row["reported_result"].strip(),
                    details=row["details"].strip(),
                )
            )
    return events


class EvidenceParser:
    """Loads and normalizes all 94 evidence files."""

    def __init__(
        self,
        dataset_dir: str | Path,
        llm_client: LLMClient,
        activities: list[ActivityEvent],
    ) -> None:
        self.dataset_dir = Path(dataset_dir)
        self.llm_client = llm_client
        self.activities = activities

        # Map resource_id -> set of deletion_worker event_types (e.g. DELETE_RECORD vs REDACT_RECORD)
        self.resource_policy_ops: dict[str, set[str]] = {}
        # Map (target_system, display_label) -> (canonical_resource_id, email, user_id)
        self.label_links: dict[tuple[str, str], tuple[str, str, str]] = {}

        for act in activities:
            if act.event_type in (
                "DELETE_RECORD",
                "REDACT_RECORD",
                "ANONYMIZE_RECORD",
                "DISABLE_DATA_FLOW",
                "BLOCK_RESTORE",
                "RETAIN_RECORD",
            ):
                self.resource_policy_ops.setdefault(act.resource_id, set()).add(
                    act.event_type
                )
            elif act.event_type == "LINK_RESOURCE_IDENTITY":
                m = LINK_RE.search(act.details)
                if m:
                    canon_id, label, email, uid = m.groups()
                    self.label_links[(act.target_system, label)] = (
                        canon_id,
                        email.lower(),
                        uid,
                    )
                    self.label_links[(act.target_system, canon_id)] = (
                        canon_id,
                        email.lower(),
                        uid,
                    )

        self.manifest: dict[str, dict[str, str]] = {}
        self.records_by_file: dict[str, list[EvidenceRecord]] = {}
        # Track snapshot bundles that have post-deletion customer_profiles sub-snapshots
        self.bundle_profile_snapshots: dict[str, list[tuple[str, set[str]]]] = {}
        self._load_all()

    def _support_category(self, resource_id: str, default: str = "SUPPORT_MESSAGE") -> str:
        ops = self.resource_policy_ops.get(resource_id, set())
        if "DELETE_RECORD" in ops:
            return "SUPPORT_ATTACHMENT"
        if "REDACT_RECORD" in ops:
            return "SUPPORT_MESSAGE"
        return default

    def _analytics_category(
        self, resource_id: str, default: str = "ANALYTICS_PROFILE"
    ) -> str:
        ops = self.resource_policy_ops.get(resource_id, set())
        if "DELETE_RECORD" in ops:
            return "SECONDARY_IDENTIFIER"
        return default

    def _load_all(self) -> None:
        manifest_path = self.dataset_dir / "evidence_files.csv"
        with open(manifest_path, mode="r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                fid = row["file_id"].strip()
                self.manifest[fid] = {
                    "file_id": fid,
                    "file_path": row["file_path"].strip(),
                    "origin": row["origin"].strip(),
                    "captured_at": row["captured_at"].strip(),
                }

        for fid, meta in self.manifest.items():
            fpath = self.dataset_dir / meta["file_path"]
            cap_at = meta["captured_at"]
            suffix = fpath.suffix.lower()
            if suffix == ".sql":
                recs = self._parse_sql(fid, fpath, cap_at)
            elif suffix == ".json":
                recs = self._parse_json(fid, fpath, cap_at)
            elif suffix == ".log":
                recs = self._parse_log(fid, fpath, cap_at)
            elif suffix == ".txt":
                recs = self._parse_txt(fid, fpath, cap_at)
            elif suffix in (".png", ".pdf"):
                recs = self._parse_multimodal(fid, fpath, cap_at)
            else:
                recs = []
            self.records_by_file[fid] = recs

    def _parse_multimodal(
        self, fid: str, fpath: Path, cap_at: str
    ) -> list[EvidenceRecord]:
        raw_list = self.llm_client.extract_multimodal_evidence(fid, fpath)
        out: list[EvidenceRecord] = []
        for item in raw_list:
            sys_id = item["system_id"]
            raw_rid = item["resource_id"]
            cat = item["data_category"]
            state = item["state"]

            # Ignore non-canonical order register rows (e.g. ORD-20264118 in evidence_27)
            if raw_rid.startswith("ORD-"):
                continue

            emails: set[str] = set()
            phones: set[str] = set()
            names: set[str] = set()
            hashes: set[str] = set()
            uid_box: list[str | None] = [item.get("user_id")]

            if item.get("email"):
                emails.add(str(item["email"]).strip().lower())
            if item.get("phone"):
                phones.add(str(item["phone"]).strip())
            if item.get("full_name"):
                names.add(" ".join(str(item["full_name"]).strip().lower().split()))
            if item.get("email_hash"):
                hashes.add(str(item["email_hash"]).strip().lower())

            canon_rid = raw_rid
            if (sys_id, raw_rid) in self.label_links:
                canon_rid, linked_email, linked_uid = self.label_links[(sys_id, raw_rid)]
                emails.add(linked_email)
                uid_box[0] = linked_uid

            out.append(
                EvidenceRecord(
                    evidence_id=fid,
                    file_captured_at=cap_at,
                    observed_at=cap_at,
                    system_id=sys_id,
                    resource_id=canon_rid,
                    data_category=cat,
                    state=state,
                    user_id=uid_box[0],
                    emails=emails,
                    phones=phones,
                    names=names,
                    email_hashes=hashes,
                    source_table="multimodal",
                )
            )
        return out

    def _parse_collection_record(
        self,
        fid: str,
        cap_at: str,
        obs_at: str,
        sys_id: str,
        collection: str,
        rec: dict[str, Any],
        source_table: str,
    ) -> EvidenceRecord | None:
        rid = rec.get("resource_id")
        if not rid:
            return None

        emails: set[str] = set()
        phones: set[str] = set()
        names: set[str] = set()
        hashes: set[str] = set()
        uid_box: list[str | None] = [None]

        for k in (
            "customer_reference",
            "subject_reference",
            "user_id",
            "email",
            "owner_email",
            "uploaded_by",
            "subject_selector",
            "phone",
            "email_hash",
        ):
            if k in rec:
                _add_identifier(rec[k], emails, phones, names, hashes, uid_box)

        for nk in ("full_name", "name"):
            if rec.get(nk):
                names.add(" ".join(str(rec[nk]).strip().lower().split()))

        if collection == "customer_profiles":
            status = str(rec.get("account_status", "ACTIVE")).upper()
            state = "ABSENT" if status == "DELETED" else "PRESENT"
            return EvidenceRecord(
                fid, cap_at, obs_at, "account_service", rid, "USER_PROFILE",
                state, uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "customer_search_documents":
            doc = rec.get("document") or {}
            _add_identifier(doc.get("customer_reference"), emails, phones, names, hashes, uid_box)
            _add_identifier(doc.get("email"), emails, phones, names, hashes, uid_box)
            if doc.get("display_name"):
                names.add(" ".join(str(doc["display_name"]).strip().lower().split()))
            return EvidenceRecord(
                fid, cap_at, obs_at, "search_service", rid, "SEARCH_INDEX_ENTRY",
                "PRESENT", uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "customer_analytics_reports":
            has_pii = any(
                rec.get(k) is not None
                for k in ("user_id", "email", "phone", "email_hash")
            )
            state = "PRESENT" if has_pii else "ANONYMIZED"
            cat = self._analytics_category(rid, "ANALYTICS_PROFILE")
            return EvidenceRecord(
                fid, cap_at, obs_at, "analytics_platform", rid, cat,
                state, uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "backup_catalog":
            allowed = bool(rec.get("restore_allowed"))
            state = "ACTIVE" if allowed else "BLOCKED"
            return EvidenceRecord(
                fid, cap_at, obs_at, "backup_service", rid, "BACKUP_ARCHIVE",
                state, uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "customer_export_jobs":
            avail = rec.get("download_available", True)
            state = "PRESENT" if avail else "ABSENT"
            return EvidenceRecord(
                fid, cap_at, obs_at, "export_service", rid, "USER_EXPORT",
                state, uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "deleted_customer_references":
            has_pii = any(
                rec.get(k) is not None
                for k in ("name", "email", "phone", "email_hash")
            )
            state = "PRESENT" if has_pii else "REDACTED"
            return EvidenceRecord(
                fid, cap_at, obs_at, "identity_service", rid, "ACCOUNT_TOMBSTONE",
                state, uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "connector_configurations":
            enabled = bool(rec.get("enabled"))
            state = "ACTIVE" if enabled else "DISABLED"
            return EvidenceRecord(
                fid, cap_at, obs_at, "integration_service", rid, "DATA_FLOW",
                state, uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "retained_order_records":
            return EvidenceRecord(
                fid, cap_at, obs_at, "legal_service", rid, "LEGAL_HOLD_RECORD",
                "PRESENT", uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "security_audit_events":
            return EvidenceRecord(
                fid, cap_at, obs_at, "security_service", rid, "SECURITY_AUDIT_LOG",
                "PRESENT", uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "support_conversations":
            msg = str(rec.get("message_text", ""))
            _add_identifier(msg, emails, phones, names, hashes, uid_box)
            state = "REDACTED" if msg.strip() == "[REDACTED]" else "PRESENT"
            return EvidenceRecord(
                fid, cap_at, obs_at, "support_platform", rid, "SUPPORT_MESSAGE",
                state, uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "ticket_attachments":
            return EvidenceRecord(
                fid, cap_at, obs_at, "support_platform", rid, "SUPPORT_ATTACHMENT",
                "PRESENT", uid_box[0], emails, phones, names, hashes, source_table,
            )

        if collection == "operational_record_history":
            return self._parse_lookup_result_record(
                fid,
                cap_at,
                rec.get("recorded_at", obs_at),
                rec["system"],
                rid,
                rec["record_type"],
                rec["result"],
                rec.get("lookup_value"),
                source_table,
            )

        return None

    def _parse_lookup_result_record(
        self,
        fid: str,
        cap_at: str,
        obs_at: str,
        sys_id: str,
        rid: str,
        record_type: str,
        result: str,
        subject_ref: Any,
        source_table: str,
    ) -> EvidenceRecord:
        emails: set[str] = set()
        phones: set[str] = set()
        names: set[str] = set()
        hashes: set[str] = set()
        uid_box: list[str | None] = [None]
        _add_identifier(subject_ref, emails, phones, names, hashes, uid_box)

        res_u = result.strip().upper()
        if res_u in ("NOT_FOUND", "DELETED", "ABSENT"):
            state = "ABSENT"
        elif res_u in ("FOUND", "PRESENT", "IDENTIFIABLE", "RETAINED"):
            state = "PRESENT"
        elif res_u == "REDACTED":
            state = "REDACTED"
        elif res_u == "ANONYMIZED":
            state = "ANONYMIZED"
        elif res_u == "ACTIVE":
            state = "ACTIVE"
        elif res_u == "DISABLED":
            state = "DISABLED"
        elif res_u == "BLOCKED":
            state = "BLOCKED"
        else:
            state = "PRESENT"

        cat = record_type.strip().upper()
        if sys_id == "support_platform":
            cat = self._support_category(rid, cat)
        elif sys_id == "analytics_platform":
            cat = self._analytics_category(rid, cat)

        return EvidenceRecord(
            fid, cap_at, obs_at, sys_id, rid, cat,
            state, uid_box[0], emails, phones, names, hashes, source_table,
        )

    def _parse_sql(
        self, fid: str, fpath: Path, cap_at: str
    ) -> list[EvidenceRecord]:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        with open(fpath, mode="r", encoding="utf-8") as f:
            con.executescript(f.read())

        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        out: list[EvidenceRecord] = []

        for tname in tables:
            if tname == "snapshot_scope":
                continue
            rows = con.execute(f'SELECT * FROM "{tname}"').fetchall()
            if not rows:
                continue
            cols = set(rows[0].keys())

            if "record_json" in cols:
                for r in rows:
                    rj = json.loads(r["record_json"])
                    ev = self._parse_collection_record(
                        fid,
                        cap_at,
                        r["updated_at"],
                        r["origin_system"],
                        r["collection_name"],
                        rj,
                        tname,
                    )
                    if ev:
                        out.append(ev)
                continue

            # Relational inventory & special SQL tables
            for r in rows:
                rid = r["resource_id"]
                obs_at = r["updated_at"] if "updated_at" in cols else (
                    r["indexed_at"] if "indexed_at" in cols else cap_at
                )
                emails: set[str] = set()
                phones: set[str] = set()
                names: set[str] = set()
                hashes: set[str] = set()
                uid_box: list[str | None] = [None]

                for k in (
                    "user_id",
                    "customer_reference",
                    "subject_reference",
                    "email",
                    "customer_email",
                    "requester_email",
                    "owner_email",
                    "phone",
                    "requester_phone",
                    "owner_phone",
                    "email_hash",
                ):
                    if k in cols:
                        _add_identifier(r[k], emails, phones, names, hashes, uid_box)

                for nk in ("full_name", "display_name", "requester_name"):
                    if nk in cols and r[nk]:
                        names.add(" ".join(str(r[nk]).strip().lower().split()))

                if tname in (
                    "customer_profiles",
                    "customer_profiles_primary",
                    "customer_profiles_failover_replica",
                ):
                    st = str(r["account_status"]).upper()
                    state = "ABSENT" if st == "DELETED" else "PRESENT"
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "account_service", rid,
                            "USER_PROFILE", state, uid_box[0], emails, phones,
                            names, hashes, tname,
                        )
                    )
                elif tname in ("customer_search_documents", "search_documents_primary"):
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "search_service", rid,
                            "SEARCH_INDEX_ENTRY", "PRESENT", uid_box[0], emails,
                            phones, names, hashes, tname,
                        )
                    )
                elif tname == "customer_analytics_profiles":
                    has_pii = any(
                        r[k] is not None
                        for k in ("customer_reference", "email", "phone", "email_hash")
                    )
                    state = "PRESENT" if has_pii else "ANONYMIZED"
                    cat = self._analytics_category(rid, "ANALYTICS_PROFILE")
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "analytics_platform", rid, cat,
                            state, uid_box[0], emails, phones, names, hashes, tname,
                        )
                    )
                elif tname == "support_records":
                    has_pii = any(
                        r[k] is not None
                        for k in ("requester_name", "requester_email", "requester_phone")
                    )
                    state = "PRESENT" if has_pii else "REDACTED"
                    cat = self._support_category(rid, "SUPPORT_MESSAGE")
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "support_platform", rid, cat,
                            state, uid_box[0], emails, phones, names, hashes, tname,
                        )
                    )
                elif tname == "customer_exports":
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "export_service", rid,
                            "USER_EXPORT", "PRESENT", uid_box[0], emails, phones,
                            names, hashes, tname,
                        )
                    )
                elif tname == "connector_subscriptions":
                    state = "ACTIVE" if int(r["enabled"]) == 1 else "DISABLED"
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "integration_service", rid,
                            "DATA_FLOW", state, uid_box[0], emails, phones,
                            names, hashes, tname,
                        )
                    )
                elif tname == "backup_restore_entries":
                    state = "ACTIVE" if int(r["restore_allowed"]) == 1 else "BLOCKED"
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "backup_service", rid,
                            "BACKUP_ARCHIVE", state, uid_box[0], emails, phones,
                            names, hashes, tname,
                        )
                    )
                elif tname == "security_audit_records":
                    out.append(
                        EvidenceRecord(
                            fid, cap_at, obs_at, "security_service", rid,
                            "SECURITY_AUDIT_LOG", "PRESENT", uid_box[0], emails,
                            phones, names, hashes, tname,
                        )
                    )

        con.close()
        return out

    def _parse_json(
        self, fid: str, fpath: Path, cap_at: str
    ) -> list[EvidenceRecord]:
        with open(fpath, mode="r", encoding="utf-8") as f:
            data = json.load(f)

        out: list[EvidenceRecord] = []

        if "snapshots" in data:
            prof_snaps: list[tuple[str, set[str]]] = []
            for idx, snap in enumerate(data["snapshots"]):
                sub_cap = snap.get("captured_at", cap_at)
                ds = snap["data_store"]
                coll = snap["collection"]
                recs = snap.get("records", [])
                if ds == "account_service" and coll == "customer_profiles":
                    prof_snaps.append(
                        (sub_cap, {r["resource_id"] for r in recs if "resource_id" in r})
                    )
                for r in recs:
                    ev = self._parse_collection_record(
                        fid, cap_at, sub_cap, ds, coll, r, f"snapshot_{idx}"
                    )
                    if ev:
                        out.append(ev)

            # If an account_id appeared in an earlier customer_profiles sub-snapshot
            # and is absent in a later customer_profiles sub-snapshot in the same bundle,
            # emit an ABSENT observation at the later sub-snapshot timestamp.
            if len(prof_snaps) >= 2:
                seen_accounts: dict[str, EvidenceRecord] = {}
                for ev in list(out):
                    if ev.system_id == "account_service":
                        seen_accounts[ev.resource_id] = ev
                for sub_cap, present_ids in prof_snaps:
                    for acc_id, base_ev in seen_accounts.items():
                        if acc_id not in present_ids and sub_cap > base_ev.observed_at:
                            out.append(
                                EvidenceRecord(
                                    evidence_id=fid,
                                    file_captured_at=cap_at,
                                    observed_at=sub_cap,
                                    system_id="account_service",
                                    resource_id=acc_id,
                                    data_category="USER_PROFILE",
                                    state="ABSENT",
                                    user_id=base_ev.user_id,
                                    emails=set(base_ev.emails),
                                    phones=set(base_ev.phones),
                                    names=set(base_ev.names),
                                    email_hashes=set(base_ev.email_hashes),
                                    source_table="snapshot_absent",
                                )
                            )
            return out

        if "service_exports" in data:
            se = data["service_exports"]
            for r in se.get("account_service", {}).get("profile_lookups", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                prof = r.get("profile") or {}
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                for k in ("customer_reference", "email", "phone", "email_hash"):
                    _add_identifier(prof.get(k), emails, phones, names, hashes, uid_box)
                if prof.get("full_name"):
                    names.add(" ".join(str(prof["full_name"]).strip().lower().split()))
                state = "ABSENT" if r.get("status_code") == 404 else "PRESENT"
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "account_service", rid,
                        "USER_PROFILE", state, uid_box[0], emails, phones, names, hashes,
                    )
                )

            for r in se.get("search_service", {}).get("index_queries", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                hits_obj = r.get("hits") or {}
                total = hits_obj.get("total", 0)
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                for doc in hits_obj.get("documents", []):
                    for k in ("customer_reference", "email", "phone", "email_hash"):
                        _add_identifier(doc.get(k), emails, phones, names, hashes, uid_box)
                    if doc.get("display_name"):
                        names.add(" ".join(str(doc["display_name"]).strip().lower().split()))
                state = "PRESENT" if total > 0 else "ABSENT"
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "search_service", rid,
                        "SEARCH_INDEX_ENTRY", state, uid_box[0], emails, phones, names, hashes,
                    )
                )

            for r in se.get("analytics_platform", {}).get("profiles", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                for k in ("customer_reference", "email", "phone", "email_hash"):
                    _add_identifier(r.get(k), emails, phones, names, hashes, uid_box)
                has_pii = any(
                    r.get(k) is not None
                    for k in ("customer_reference", "email", "phone", "email_hash")
                )
                state = "PRESENT" if has_pii else "ANONYMIZED"
                cat = self._analytics_category(rid, "ANALYTICS_PROFILE")
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "analytics_platform", rid, cat,
                        state, uid_box[0], emails, phones, names, hashes,
                    )
                )

            for r in se.get("support_platform", {}).get("tickets", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                for k in ("customer_reference", "requester_email", "requester_phone"):
                    _add_identifier(r.get(k), emails, phones, names, hashes, uid_box)
                if r.get("requester_name"):
                    names.add(" ".join(str(r["requester_name"]).strip().lower().split()))
                has_pii = any(
                    r.get(k) is not None
                    for k in ("requester_name", "requester_email", "requester_phone")
                )
                state = "PRESENT" if has_pii else "REDACTED"
                cat = self._support_category(rid, "SUPPORT_MESSAGE")
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "support_platform", rid, cat,
                        state, uid_box[0], emails, phones, names, hashes,
                    )
                )

            for r in se.get("export_service", {}).get("exports", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                for k in ("customer_reference", "owner_email", "owner_phone"):
                    _add_identifier(r.get(k), emails, phones, names, hashes, uid_box)
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "export_service", rid,
                        "USER_EXPORT", "PRESENT", uid_box[0], emails, phones, names, hashes,
                    )
                )

            for r in se.get("integration_service", {}).get("connectors", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                for k in ("customer_reference", "customer_email"):
                    _add_identifier(r.get(k), emails, phones, names, hashes, uid_box)
                state = "ACTIVE" if bool(r.get("enabled")) else "DISABLED"
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "integration_service", rid,
                        "DATA_FLOW", state, uid_box[0], emails, phones, names, hashes,
                    )
                )

            for r in se.get("backup_service", {}).get("restore_entries", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                for k in ("customer_reference", "customer_email"):
                    _add_identifier(r.get(k), emails, phones, names, hashes, uid_box)
                state = "ACTIVE" if bool(r.get("restore_allowed")) else "BLOCKED"
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "backup_service", rid,
                        "BACKUP_ARCHIVE", state, uid_box[0], emails, phones, names, hashes,
                    )
                )

            for r in se.get("security_service", {}).get("audit_records", []):
                rid = r["resource_id"]
                obs_at = r.get("observed_at", cap_at)
                emails, phones, names, hashes, uid_box = set(), set(), set(), set(), [None]
                _add_identifier(r.get("subject_reference"), emails, phones, names, hashes, uid_box)
                out.append(
                    EvidenceRecord(
                        fid, cap_at, obs_at, "security_service", rid,
                        "SECURITY_AUDIT_LOG", "PRESENT", uid_box[0], emails, phones, names, hashes,
                    )
                )
            return out

        if "records" in data and "origin" in data:
            # Flat lookup manifests (evidence_13, evidence_36)
            for r in data["records"]:
                out.append(
                    self._parse_lookup_result_record(
                        fid,
                        cap_at,
                        r.get("recorded_at", cap_at),
                        r["system"],
                        r["resource_id"],
                        r["record_type"],
                        r["result"],
                        r.get("subject_reference"),
                        "manifest",
                    )
                )
            return out

        if "collection" in data and "records" in data:
            # Single-collection JSON files (evidence_56, evidence_59, evidence_50)
            coll = data["collection"]
            sys_map = {
                "customer_analytics_reports": "analytics_platform",
                "customer_export_jobs": "export_service",
            }
            if coll in sys_map:
                for r in data["records"]:
                    ev = self._parse_collection_record(
                        fid, cap_at, data.get("captured_at", cap_at),
                        sys_map[coll], coll, r, "json_single",
                    )
                    if ev:
                        out.append(ev)
            return out

        return out

    def _parse_txt(
        self, fid: str, fpath: Path, cap_at: str
    ) -> list[EvidenceRecord]:
        out: list[EvidenceRecord] = []
        with open(fpath, mode="r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("- "):
                    continue
                parts = [p.strip() for p in line[2:].split("|")]
                if len(parts) < 6:
                    continue
                obs_at, sys_id, rid, rtype, result, ident = parts[:6]
                out.append(
                    self._parse_lookup_result_record(
                        fid, cap_at, obs_at, sys_id, rid, rtype, result, ident, "txt"
                    )
                )
        return out

    def _parse_log(
        self, fid: str, fpath: Path, cap_at: str
    ) -> list[EvidenceRecord]:
        out: list[EvidenceRecord] = []
        traces: dict[str, dict[str, Any]] = {}
        untraced: list[dict[str, Any]] = []

        with open(fpath, mode="r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}T[\d:]+Z)", line)
                ts = ts_match.group(1) if ts_match else cap_at

                line_u = re.sub(r'\bmessage="', " ", line).replace('\\"', '"')
                kvs: dict[str, Any] = {
                    k: (v1 if v1 != "" else v2)
                    for k, v1, v2 in re.findall(
                        r'([A-Za-z_]+)=(?:"([^"]*)"|([^\s"|}]+))', line_u
                    )
                }
                if "{" in line and "}" in line:
                    try:
                        j = json.loads(line[line.index("{") : line.rindex("}") + 1])
                        kvs.update(j)
                    except Exception:
                        pass

                m_br = re.match(
                    r"^\S+\s+\[\w+\]\s+\[([a-z_]+)\]\s+\[(tr-\d+)\]\s+([a-z_]+):",
                    line,
                )
                if m_br:
                    kvs["service"], kvs["trace_id"], kvs["event"] = m_br.groups()

                m_pi = re.match(
                    r"^\S+\s+\|\s+\w+\s+\|\s+([a-z_]+)\s+\|\s+([a-z0-9-]+)\s+\|\s+([a-z_]+)\s+\|",
                    line,
                )
                if m_pi:
                    kvs["service"], kvs["trace_id"], kvs["event"] = m_pi.groups()

                m_sy = re.match(
                    r"^\S+\s+<\d+>1\s+\S+\s+([a-z_]+)\s+(tr-\d+)\s+-\s+-", line
                )
                if m_sy:
                    kvs["service"], kvs["trace_id"] = m_sy.groups()

                if "function" in kvs and "service" not in kvs:
                    kvs["service"] = kvs["function"]
                if "requestId" in kvs and "trace_id" not in kvs:
                    if str(kvs["requestId"]).startswith("tr-"):
                        kvs["trace_id"] = kvs["requestId"]

                kvs["_ts"] = ts

                # Handle record_lookup single-line logs (evidence_08, evidence_41)
                if kvs.get("event") == "record_lookup" and "system" in kvs:
                    out.append(
                        self._parse_lookup_result_record(
                            fid,
                            cap_at,
                            ts,
                            str(kvs["system"]),
                            str(kvs["resource_id"]),
                            str(kvs["record_type"]),
                            str(kvs["result"]),
                            kvs.get("subject_reference"),
                            "log_lookup",
                        )
                    )
                    continue

                tid = kvs.get("trace_id")
                if tid:
                    agg = traces.setdefault(str(tid), {"_idents": []})
                    for ik in (
                        "lookup_key",
                        "match_key",
                        "scan_key",
                        "subject_key",
                        "correlation_key",
                        "subject_ref",
                        "subject_reference",
                        "customer_reference",
                        "email",
                        "customer_email",
                        "requester_email",
                        "owner_email",
                        "matched_value",
                        "phone",
                        "requester_phone",
                        "email_hash",
                    ):
                        if ik in kvs:
                            agg["_idents"].append(kvs[ik])
                    agg.update(kvs)
                elif "resource_id" in kvs:
                    kvs["_idents"] = [
                        kvs[ik]
                        for ik in ("lookup_key", "email", "matched_value")
                        if ik in kvs
                    ]
                    untraced.append(kvs)

        for item in list(traces.values()) + untraced:
            ev = self._build_log_record(fid, cap_at, item)
            if ev:
                out.append(ev)

        return out

    def _build_log_record(
        self, fid: str, cap_at: str, item: dict[str, Any]
    ) -> EvidenceRecord | None:
        rid = item.get("resource_id")
        if not rid:
            return None
        svc = str(item.get("service", ""))
        ev_name = str(item.get("event", ""))
        obs_at = str(item.get("_ts", cap_at))

        emails: set[str] = set()
        phones: set[str] = set()
        names: set[str] = set()
        hashes: set[str] = set()
        uid_box: list[str | None] = [None]

        for raw_id in item.get("_idents", []):
            _add_identifier(raw_id, emails, phones, names, hashes, uid_box)
        if item.get("requester_name"):
            _add_identifier(item["requester_name"], emails, phones, names, hashes, uid_box)

        if svc in ("account_service", "profile_service") or rid.startswith("account_"):
            rows = str(item.get("rows", ""))
            status = str(item.get("http_status", ""))
            state = "ABSENT" if (rows == "0" or status == "404") else "PRESENT"
            return EvidenceRecord(
                fid, cap_at, obs_at, "account_service", rid, "USER_PROFILE",
                state, uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc == "search_service" or rid.startswith("search_"):
            res = str(item.get("result", "")).lower()
            hits = str(item.get("total_hits", item.get("hits", "")))
            state = "ABSENT" if (res == "miss" or hits == "0") else "PRESENT"
            return EvidenceRecord(
                fid, cap_at, obs_at, "search_service", rid, "SEARCH_INDEX_ENTRY",
                state, uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc == "analytics_platform" or rid.startswith("analytics_"):
            if ev_name == "contact_lookup_completed":
                state = "ABSENT" if str(item.get("rows", "1")) == "0" else "PRESENT"
                return EvidenceRecord(
                    fid, cap_at, obs_at, "analytics_platform", rid,
                    "SECONDARY_IDENTIFIER", state, uid_box[0], emails, phones,
                    names, hashes, "log",
                )
            if "direct_identifiers" in item:
                state = (
                    "ANONYMIZED"
                    if str(item["direct_identifiers"]) == "0"
                    else "PRESENT"
                )
            else:
                has_pii = any(
                    str(item.get(k, "null")).lower() not in ("null", "none", "")
                    for k in ("customer_reference", "email", "phone", "email_hash", "matched_value")
                )
                state = "PRESENT" if has_pii else "ANONYMIZED"
            cat = self._analytics_category(rid, "ANALYTICS_PROFILE")
            return EvidenceRecord(
                fid, cap_at, obs_at, "analytics_platform", rid, cat,
                state, uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc in ("support_platform", "support_replica") or rid.startswith("support_"):
            if svc == "support_replica" or "contact_query" in ev_name or "deletion_marker" in ev_name:
                return EvidenceRecord(
                    fid, cap_at, obs_at, "support_platform", rid,
                    "SUPPORT_ATTACHMENT", "PRESENT", uid_box[0], emails, phones,
                    names, hashes, "log",
                )
            if "attachment" in ev_name:
                return EvidenceRecord(
                    fid, cap_at, obs_at, "support_platform", rid,
                    "SUPPORT_ATTACHMENT", "PRESENT", uid_box[0], emails, phones,
                    names, hashes, "log",
                )
            if ev_name == "message_read":
                msg = str(item.get("message_text", ""))
                _add_identifier(msg, emails, phones, names, hashes, uid_box)
                state = "REDACTED" if msg.strip() == "[REDACTED]" else "PRESENT"
                return EvidenceRecord(
                    fid, cap_at, obs_at, "support_platform", rid,
                    "SUPPORT_MESSAGE", state, uid_box[0], emails, phones,
                    names, hashes, "log",
                )
            if ev_name == "message_pii_scan":
                return EvidenceRecord(
                    fid, cap_at, obs_at, "support_platform", rid,
                    "SUPPORT_MESSAGE", "PRESENT", uid_box[0], emails, phones,
                    names, hashes, "log",
                )
            # support_ticket_read_completed
            has_pii = any(
                str(item.get(k, "null")).lower() not in ("null", "none", "")
                for k in ("requester_name", "requester_email", "requester_phone")
            )
            state = "PRESENT" if has_pii else "REDACTED"
            cat = self._support_category(rid, "SUPPORT_MESSAGE")
            return EvidenceRecord(
                fid, cap_at, obs_at, "support_platform", rid, cat,
                state, uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc == "backup_service" or rid.startswith("backup_"):
            if "decision" in item:
                state = "BLOCKED" if str(item["decision"]).lower() == "deny" else "ACTIVE"
            elif "eligible" in item:
                state = "ACTIVE" if str(item["eligible"]).lower() == "true" else "BLOCKED"
            else:
                state = (
                    "ACTIVE"
                    if str(item.get("restore_allowed", "")).lower() == "true"
                    else "BLOCKED"
                )
            return EvidenceRecord(
                fid, cap_at, obs_at, "backup_service", rid, "BACKUP_ARCHIVE",
                state, uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc == "integration_service" or rid.startswith("data_flow_"):
            state = (
                "ACTIVE"
                if str(item.get("enabled", "")).lower() == "true"
                else "DISABLED"
            )
            return EvidenceRecord(
                fid, cap_at, obs_at, "integration_service", rid, "DATA_FLOW",
                state, uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc == "export_service" or rid.startswith("export_"):
            state = (
                "PRESENT"
                if str(item.get("available", "true")).lower() == "true"
                else "ABSENT"
            )
            return EvidenceRecord(
                fid, cap_at, obs_at, "export_service", rid, "USER_EXPORT",
                state, uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc == "security_service" or rid.startswith("audit_record_"):
            return EvidenceRecord(
                fid, cap_at, obs_at, "security_service", rid, "SECURITY_AUDIT_LOG",
                "PRESENT", uid_box[0], emails, phones, names, hashes, "log",
            )

        if svc == "legal_service" or rid.startswith("legal_hold_"):
            return EvidenceRecord(
                fid, cap_at, obs_at, "legal_service", rid, "LEGAL_HOLD_RECORD",
                "PRESENT", uid_box[0], emails, phones, names, hashes, "log",
            )

        return None
