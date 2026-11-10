"""Identity graph and equivalence-class resolution for Ghost Data Still Exists.

Reads `dataset/user_identities.csv` (850 rows) and `dataset/user_audit_log.csv` (306 rows)
to construct per-user equivalence classes of current and historical names, emails,
phone numbers, and 64-character SHA-256 email hashes.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Iterable


def _norm_email(val: str | None) -> str:
    if not val:
        return ""
    return val.strip().lower()


def _norm_phone(val: str | None) -> str:
    if not val:
        return ""
    return val.strip()


def _phone_digits(val: str | None) -> str:
    if not val:
        return ""
    return re.sub(r"\D", "", val)


def _norm_hash(val: str | None) -> str:
    if not val:
        return ""
    cleaned = val.strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[len("sha256:") :].strip()
    # Ignore placeholder zero-seals like sha256:00000000...
    if cleaned.startswith("000000000000"):
        return ""
    return cleaned


def _norm_name(val: str | None) -> str:
    if not val:
        return ""
    return " ".join(val.strip().lower().split())


@dataclass
class UserIdentity:
    """Equivalence class of all known current and historical identifiers for a user."""

    user_id: str
    primary_name: str
    primary_email: str
    primary_phone: str
    primary_email_hash: str
    updated_at: str
    names: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    phone_digits: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    audit_ids: list[str] = field(default_factory=list)


class IdentityIndex:
    """Lookup index mapping any current or historical identifier back to user_id."""

    def __init__(self, dataset_dir: str | Path) -> None:
        self.dataset_dir = Path(dataset_dir)
        self.users: dict[str, UserIdentity] = {}
        self.email_to_user: dict[str, str] = {}
        self.phone_to_user: dict[str, str] = {}
        self.digits_to_user: dict[str, str] = {}
        self.hash_to_user: dict[str, str] = {}
        self.name_to_users: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        identities_path = self.dataset_dir / "user_identities.csv"
        audit_log_path = self.dataset_dir / "user_audit_log.csv"

        with open(identities_path, mode="r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                uid = row["user_id"].strip()
                name = row["name"].strip()
                email = _norm_email(row["email"])
                phone = _norm_phone(row["phone"])
                ehash = _norm_hash(row["email_hash"])
                updated_at = row["updated_at"].strip()

                ident = UserIdentity(
                    user_id=uid,
                    primary_name=name,
                    primary_email=email,
                    primary_phone=phone,
                    primary_email_hash=ehash,
                    updated_at=updated_at,
                )
                self.users[uid] = ident
                self._register_name(uid, name)
                self._register_email(uid, email)
                self._register_phone(uid, phone)
                self._register_hash(uid, ehash)

        with open(audit_log_path, mode="r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                uid = row["user_id"].strip()
                if uid not in self.users:
                    continue
                ident = self.users[uid]
                ident.audit_ids.append(row["audit_id"].strip())
                field_name = row["field_name"].strip()
                old_val = row["old_value"]
                new_val = row["new_value"]

                if field_name == "email":
                    self._register_email(uid, old_val)
                    self._register_email(uid, new_val)
                elif field_name == "phone":
                    self._register_phone(uid, old_val)
                    self._register_phone(uid, new_val)
                elif field_name == "email_hash":
                    self._register_hash(uid, old_val)
                    self._register_hash(uid, new_val)
                elif field_name == "name":
                    self._register_name(uid, old_val)
                    self._register_name(uid, new_val)

    def _register_name(self, uid: str, raw_name: str | None) -> None:
        norm = _norm_name(raw_name)
        if not norm:
            return
        self.users[uid].names.add(norm)
        lst = self.name_to_users.setdefault(norm, [])
        if uid not in lst:
            lst.append(uid)

    def _register_email(self, uid: str, raw_email: str | None) -> None:
        norm = _norm_email(raw_email)
        if not norm:
            return
        self.users[uid].emails.add(norm)
        self.email_to_user[norm] = uid

    def _register_phone(self, uid: str, raw_phone: str | None) -> None:
        norm = _norm_phone(raw_phone)
        if not norm:
            return
        self.users[uid].phones.add(norm)
        self.phone_to_user[norm] = uid
        digits = _phone_digits(norm)
        if len(digits) >= 7:
            self.users[uid].phone_digits.add(digits)
            self.digits_to_user[digits] = uid

    def _register_hash(self, uid: str, raw_hash: str | None) -> None:
        norm = _norm_hash(raw_hash)
        if not norm:
            return
        self.users[uid].email_hashes.add(norm)
        self.hash_to_user[norm] = uid

    def resolve_email(self, email: str | None) -> str | None:
        norm = _norm_email(email)
        return self.email_to_user.get(norm) if norm else None

    def resolve_phone(self, phone: str | None) -> str | None:
        norm = _norm_phone(phone)
        if not norm:
            return None
        if norm in self.phone_to_user:
            return self.phone_to_user[norm]
        digits = _phone_digits(norm)
        return self.digits_to_user.get(digits) if len(digits) >= 7 else None

    def resolve_hash(self, email_hash: str | None, allow_prefix: bool = False) -> str | None:
        norm = _norm_hash(email_hash)
        if not norm:
            return None
        if norm in self.hash_to_user:
            return self.hash_to_user[norm]
        if allow_prefix and 8 <= len(norm) < 64:
            matches = [
                uid
                for full_hash, uid in self.hash_to_user.items()
                if full_hash.startswith(norm)
            ]
            if len(set(matches)) == 1:
                return matches[0]
        return None

    def resolve_any(
        self,
        user_id: str | None = None,
        emails: Iterable[str] = (),
        phones: Iterable[str] = (),
        email_hashes: Iterable[str] = (),
        names: Iterable[str] = (),
        allow_hash_prefix: bool = False,
    ) -> str | None:
        """Resolve a user_id from any available identifier in priority order."""
        if user_id:
            uid_clean = user_id.strip()
            if uid_clean in self.users:
                return uid_clean

        for e in emails:
            uid = self.resolve_email(e)
            if uid:
                return uid

        for h in email_hashes:
            uid = self.resolve_hash(h, allow_prefix=allow_hash_prefix)
            if uid:
                return uid

        for p in phones:
            uid = self.resolve_phone(p)
            if uid:
                return uid

        for n in names:
            norm = _norm_name(n)
            if norm and norm in self.name_to_users:
                candidates = self.name_to_users[norm]
                if len(candidates) == 1:
                    return candidates[0]

        return None
