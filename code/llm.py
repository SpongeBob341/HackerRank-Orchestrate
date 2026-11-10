"""LLM and Multimodal Extraction module for Ghost Data Still Exists.

Follows `project-rules` Rule 5 (Use the model only for judgment calls):
1. Request Intent Classification: Deduplicates `request_text` strings, settles
   unambiguous affirmative English templates in code, and sends only ambiguous,
   negated, or multilingual texts in a single batch to the model.
2. Multimodal Evidence Extraction: Extracts structured records from the 15 binary
   `.png` and `.pdf` evidence files.

Responses and real token usage metrics are persisted in `code/cache/*.json` so
re-runs are fast, deterministic, and reproducible without re-billing tokens.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
import urllib.request

INTENT_CLASSIFICATION_PROMPT = """You are a privacy compliance triage classifier for Northstar Shop.
For each customer request text below, classify whether the customer is requesting
deletion/erasure/closure of their account and personal data ("DELETION"), or making
a non-deletion request such as updating contact details, resetting login credentials,
requesting a data export/invoice copy, correcting a name, or unsubscribing from
marketing emails while keeping the account active ("NON_DELETION").

Pay careful attention to:
1. Multilingual requests (Spanish, French, Portuguese, Korean, Japanese) requesting
   permanent account deletion -> classify as "DELETION".
2. Negations such as "Do not close the account", "without removing any of my records",
   "Please keep the account as it is", "not a request to erase it", or "please don't
   delete or change anything" -> classify as "NON_DELETION".
3. Requests asking to verify that an already-submitted account deletion completed ->
   classify as "DELETION".

Return a JSON object mapping each input string's integer index to "DELETION" or "NON_DELETION".
"""

MULTIMODAL_EXTRACTION_PROMPT = """You are a forensic privacy audit extraction model.
Examine the supplied evidence image or PDF document and extract every customer data
record shown in the console screenshot or report table.

Return a JSON object with a single key `"records"` containing an array of objects,
where each object has the exact schema:
{
  "system_id": "account_service | search_service | support_platform | analytics_platform | export_service | object_storage | marketing_platform | integration_service | backup_service | security_service | legal_service | identity_service",
  "resource_id": "string identifier or reference shown in the evidence",
  "data_category": "USER_PROFILE | SEARCH_INDEX_ENTRY | SUPPORT_MESSAGE | SUPPORT_ATTACHMENT | ANALYTICS_PROFILE | USER_EXPORT | MARKETING_PROFILE | DATA_FLOW | BACKUP_ARCHIVE | SECURITY_AUDIT_LOG | LEGAL_HOLD_RECORD | ACCOUNT_TOMBSTONE",
  "state": "PRESENT | ABSENT | REDACTED | ANONYMIZED | ACTIVE | DISABLED | BLOCKED",
  "user_id": "user_XX if shown else null",
  "email": "email if shown else null",
  "phone": "phone if shown else null",
  "full_name": "name if shown else null",
  "email_hash": "hash if shown else null",
  "notes": "brief description of the record state in the screenshot/document"
}
"""

AFFIRMATIVE_DELETION_PHRASES = (
    "permanently delete the account",
    "erase the account",
    "request for permanent account deletion",
    "requesting deletion of the account",
    "delete this account and its personal data",
    "remove my account and the personal information",
    "close my account and process the associated personal-data deletion",
    "close this account and have its associated personal data deleted",
    "account associated with this address to be erased",
    "begin the deletion process for the account",
    "permanent account-deletion request",
    "no longer use this account and would like it permanently deleted",
)

AMBIGUITY_OR_NEGATION_MARKERS = (
    "do not",
    "don't",
    "not asking",
    "not a request",
    "without",
    "keep the account",
    "leave the account",
    "leave my profile",
    "continue using",
    "still use it",
    "still need",
    "still want",
    "out of date",
    "promotional",
    "marketing lists only",
    "portable format",
    "resetting",
    "misspelled",
    "invoices",
    "change the email",
    "information request",
)


def is_unambiguous_english_deletion(text: str) -> bool:
    """Return True only if text is plain ASCII, matches an affirmative deletion phrase,
    and contains zero negation or non-deletion support markers."""
    cleaned = text.strip()
    if not cleaned.isascii():
        return False
    low = cleaned.lower()
    if any(marker in low for marker in AMBIGUITY_OR_NEGATION_MARKERS):
        return False
    return any(phrase in low for phrase in AFFIRMATIVE_DELETION_PHRASES)


class LLMClient:
    """Manages intent classification and multimodal extraction with disk caching."""

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.intent_cache_path = self.cache_dir / "intent_cache.json"
        self.multimodal_cache_path = self.cache_dir / "multimodal_cache.json"
        self.usage_metrics_path = self.cache_dir / "usage_metrics.json"

        self.intent_cache: dict[str, str] = self._load_json(self.intent_cache_path, {})
        self.multimodal_cache: dict[str, list[dict[str, Any]]] = self._load_json(
            self.multimodal_cache_path, {}
        )
        self.usage_metrics: dict[str, Any] = self._load_json(
            self.usage_metrics_path,
            {"model": "gemini-2.5-flash", "calls": []},
        )

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if path.exists():
            with open(path, mode="r", encoding="utf-8") as f:
                return json.load(f)
        return default

    def _save_caches(self) -> None:
        with open(self.intent_cache_path, mode="w", encoding="utf-8") as f:
            json.dump(self.intent_cache, f, indent=2, ensure_ascii=False, sort_keys=True)
        with open(self.multimodal_cache_path, mode="w", encoding="utf-8") as f:
            json.dump(self.multimodal_cache, f, indent=2, ensure_ascii=False, sort_keys=True)
        with open(self.usage_metrics_path, mode="w", encoding="utf-8") as f:
            json.dump(self.usage_metrics, f, indent=2, ensure_ascii=False)

    def classify_requests(self, request_texts: list[str]) -> dict[str, str]:
        """Classify a list of request_text strings into DELETION or NON_DELETION.

        1. Deduplicates input texts.
        2. Settles unambiguous affirmative ASCII English deletion requests in code.
        3. Looks up remaining ambiguous/negated/multilingual texts in `intent_cache.json`
           (or calls the live Gemini API if uncached and `GEMINI_API_KEY` is set).
        """
        results: dict[str, str] = {}
        needs_model: list[str] = []

        for raw_text in dict.fromkeys(t.strip() for t in request_texts):
            if is_unambiguous_english_deletion(raw_text):
                results[raw_text] = "DELETION"
            elif raw_text in self.intent_cache:
                results[raw_text] = self.intent_cache[raw_text]
            else:
                needs_model.append(raw_text)

        if needs_model:
            batch_preds = self._call_intent_model_batch(needs_model)
            for txt, label in batch_preds.items():
                self.intent_cache[txt] = label
                results[txt] = label
            self._save_caches()

        return results

    def extract_multimodal_evidence(
        self, file_id: str, file_path: Path
    ) -> list[dict[str, Any]]:
        """Return structured records extracted from a PNG or PDF evidence file."""
        if file_id in self.multimodal_cache:
            return self.multimodal_cache[file_id]

        extracted = self._call_multimodal_model(file_id, file_path)
        self.multimodal_cache[file_id] = extracted
        self._save_caches()
        return extracted

    def _call_intent_model_batch(self, texts: list[str]) -> dict[str, str]:
        api_key = os.environ.get("GEMINI_API_KEY")
        indexed_payload = {str(i): t for i, t in enumerate(texts)}
        user_prompt = json.dumps(indexed_payload, ensure_ascii=False, indent=2)

        if api_key:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.5-flash:generateContent?key={api_key}"
            )
            body = {
                "system_instruction": {"parts": [{"text": INTENT_CLASSIFICATION_PROMPT}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0},
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw_json)
            usage = data.get("usageMetadata", {})
            self.usage_metrics.setdefault("calls", []).append(
                {
                    "task": "intent_classification_batch",
                    "batch_size": len(texts),
                    "input_tokens": usage.get("promptTokenCount", 0),
                    "output_tokens": usage.get("candidatesTokenCount", 0),
                }
            )
            return {texts[int(k)]: v for k, v in parsed.items()}

        raise RuntimeError(
            f"Intent cache miss for {len(texts)} text(s) and GEMINI_API_KEY is not set."
        )

    def _call_multimodal_model(
        self, file_id: str, file_path: Path
    ) -> list[dict[str, Any]]:
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            mime_type, _ = mimetypes.guess_type(str(file_path))
            mime_type = mime_type or "application/octet-stream"
            b64_data = base64.b64encode(file_path.read_bytes()).decode("ascii")
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.5-flash:generateContent?key={api_key}"
            )
            body = {
                "system_instruction": {"parts": [{"text": MULTIMODAL_EXTRACTION_PROMPT}]},
                "contents": [
                    {
                        "parts": [
                            {"inline_data": {"mime_type": mime_type, "data": b64_data}},
                            {"text": f"Extract all records from {file_id} ({file_path.name})."},
                        ]
                    }
                ],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0},
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw_json)
            usage = data.get("usageMetadata", {})
            self.usage_metrics.setdefault("calls", []).append(
                {
                    "task": f"multimodal_extraction:{file_id}",
                    "file_id": file_id,
                    "input_tokens": usage.get("promptTokenCount", 0),
                    "output_tokens": usage.get("candidatesTokenCount", 0),
                }
            )
            return parsed.get("records", [])

        raise RuntimeError(
            f"Multimodal cache miss for {file_id} and GEMINI_API_KEY is not set."
        )
