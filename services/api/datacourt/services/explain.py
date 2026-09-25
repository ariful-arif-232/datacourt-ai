"""Case narratives: Gemini (optional) with a deterministic template fallback.

The LLM receives only structured evidence (no images, no file paths, no user notes),
is instructed never to invent metrics or change the verdict, and must return JSON. The
verdict itself is never read from the LLM. Its output is then checked: any number that
does not appear in the evidence is flagged as unverified in the UI.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any

import httpx

from datacourt.config import get_settings
from datacourt.logging_setup import log
from datacourt.security import sha256_json

logger = logging.getLogger("datacourt.explain")

SYSTEM_PROMPT = """You explain dataset-audit evidence to ML engineers.
Rules:
- Use ONLY the facts in the provided JSON. Never invent metrics, counts, percentages or sample details.
- Never change, soften or overrule the jury verdict; explain it.
- Distinguish measured evidence, heuristics, model predictions and model estimates.
- Model predictions are evidence, not ground truth. Similarity is not identity.
- Do not describe or identify people. Do not speculate about image content you were not given.
- State uncertainty plainly.
Return JSON with keys: prosecutor (string, <=90 words), defense (string, <=90 words),
plain_summary (string, <=70 words), reviewer_question (string, one question a human reviewer should answer)."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "prosecutor": {"type": "STRING"},
        "defense": {"type": "STRING"},
        "plain_summary": {"type": "STRING"},
        "reviewer_question": {"type": "STRING"},
    },
    "required": ["prosecutor", "defense", "plain_summary", "reviewer_question"],
}


def build_evidence_payload(case: dict, evidence: list[dict], sample: dict) -> dict[str, Any]:
    return {
        "case_number": case["case_number"],
        "current_label": sample["label"],
        "split": sample["split"],
        "jury_verdict": case["verdict"],
        "reason_codes": case["reason_codes"],
        "uncertainty": case["uncertainty"],
        "evidence": [
            {
                "witness": e["witness"],
                "stance": e["stance"],
                "kind": e["evidence_kind"],
                "title": e["title"],
                "detail": e["detail"],
                "values": _slim(e.get("value") or {}),
            }
            for e in evidence
        ],
        "limitations": [
            "The baseline model is a linear head on frozen embeddings; its predictions are evidence, not ground truth.",
            "Visual similarity does not prove two images show the same subject.",
            "Influence values are first-order estimates.",
        ],
    }


# Never sent to an LLM, even if a future witness adds them to its evidence values.
_PRIVATE_KEYS = {
    "path",
    "relative_path",
    "file",
    "filename",
    "name",
    "sha256",
    "note",
    "notes",
    "email",
    "reviewer",
}


def _slim(v: dict) -> dict:
    out = {}
    for k, x in v.items():
        if k.lower() in _PRIVATE_KEYS:
            continue
        if (
            isinstance(x, (int, float, str, bool))
            or x is None
            or isinstance(x, list)
            and len(x) <= 6
            and all(isinstance(y, (int, float, str)) for y in x)
        ):
            out[k] = x
    return out


_NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(%?)")


def unverified_numbers(text: str, payload: dict) -> list[str]:
    """Numbers in `text` that cannot be traced to the evidence payload (allowing % and rounding)."""
    source = json.dumps(payload)
    known: set[float] = set()
    for num, _ in _NUM.findall(source):
        with contextlib.suppress(ValueError):
            known.add(float(num))
    bad = []
    for num, pct in _NUM.findall(text):
        v = float(num)
        cands = {v, v / 100.0} if pct else {v}
        ok = any(abs(c - k) <= max(0.006, abs(k) * 0.02) for c in cands for k in known) or v in (0, 1, 2, 3)
        if not ok:
            bad.append(num + pct)
    return bad


def template_narrative(payload: dict) -> dict[str, Any]:
    pro = [e for e in payload["evidence"] if e["stance"] == "prosecution"]
    de = [e for e in payload["evidence"] if e["stance"] == "defense"]

    def join(items: list[dict], empty: str) -> str:
        if not items:
            return empty
        return " ".join(f"{e['title']} ({e['kind'].replace('_', ' ')})." for e in items[:4])

    verdict = payload["jury_verdict"].replace("_", " ").lower()
    question = {
        "POSSIBLE_RELABEL": "Does the image actually show the class the evidence points to?",
        "LIKELY_RARE": "Is this an unusual but correct example that should be kept?",
        "LEAKAGE_ACTION_NEEDED": "Should the evaluation copy be removed or moved so it no longer overlaps training data?",
        "POSSIBLE_REMOVE": "Is this sample unusable or redundant for training?",
    }.get(payload["jury_verdict"], f"Is the label '{payload['current_label']}' correct for this image?")
    return {
        "prosecutor": join(pro, "No witness argues against the current label or split."),
        "defense": join(de, "No witness supports keeping the sample as it is."),
        "plain_summary": (
            f"The deterministic jury returned {verdict} with {payload['uncertainty']} uncertainty, "
            f"based on reason codes: {', '.join(payload['reason_codes'][:6])}."
        ),
        "reviewer_question": question,
    }


def explain(case: dict, evidence: list[dict], sample: dict) -> dict[str, Any]:
    """Returns {source, model, input_hash, content}. Never raises for provider problems."""
    settings = get_settings()
    payload = build_evidence_payload(case, evidence, sample)
    input_hash = sha256_json(payload)
    if not settings.gemini_api_key:
        return {
            "source": "template",
            "model": None,
            "input_hash": input_hash,
            "content": {
                **template_narrative(payload),
                "note": "Gemini is not configured; deterministic template narrative.",
            },
        }
    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
        )
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(payload)}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "maxOutputTokens": 800,
            },
        }
        with httpx.Client(timeout=settings.gemini_timeout_seconds) as client:
            r = client.post(url, json=body, headers={"x-goog-api-key": settings.gemini_api_key})
        if r.status_code != 200:
            raise RuntimeError(f"provider status {r.status_code}")
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        content = json.loads(text)
        if not all(
            isinstance(content.get(k), str)
            for k in ("prosecutor", "defense", "plain_summary", "reviewer_question")
        ):
            raise ValueError("malformed response")
        flags = {
            k: unverified_numbers(content[k], payload) for k in ("prosecutor", "defense", "plain_summary")
        }
        content["unverified_numbers"] = {k: v for k, v in flags.items() if v}
        return {
            "source": "gemini",
            "model": settings.gemini_model,
            "input_hash": input_hash,
            "content": content,
        }
    except Exception as exc:  # noqa: BLE001 - explanation must never break the case
        # Do not log provider payloads (may echo user content); log only the failure class.
        log(
            logger,
            logging.WARNING,
            "gemini explanation failed; using template",
            error_type=type(exc).__name__,
        )
        return {
            "source": "template",
            "model": None,
            "input_hash": input_hash,
            "content": {
                **template_narrative(payload),
                "note": "LLM explanation unavailable; deterministic template narrative.",
            },
        }
