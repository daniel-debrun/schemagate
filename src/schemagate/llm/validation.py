from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from schemagate.errors import ProviderResponseError
from schemagate.llm.base import MappingRequest

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
UNCITED_CONFIDENCE_CEILING = 0.5


@dataclass
class ValidatedMapping:
    source_column: str
    target_field: str | None
    confidence: float
    raw_confidence: float
    rationale: str
    evidence: list[str]
    issues: list[str] = field(default_factory=list)


def extract_json(text: str) -> Any:
    stripped = text.strip()
    m = _FENCE.match(stripped)
    if m:
        stripped = m.group(1)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProviderResponseError(f"response is not valid JSON: {exc.msg}") from exc


def validate_response(
    text: str, request: MappingRequest, cap: float | None
) -> list[ValidatedMapping]:
    """Strictly validate a model response against the request.

    Raises ProviderResponseError when the payload is structurally unusable. Individual entries that
    name unknown targets are kept with target_field=None and an issue recorded, so the reviewer can
    see what the model tried. Confidence is clamped to ``cap`` when given.
    """
    payload = extract_json(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("mappings"), list):
        raise ProviderResponseError("response must be an object with a 'mappings' list")
    field_names = {f.name for f in request.fields}
    columns = {c.name for c in request.columns}
    taken = set(request.already_mapped)
    seen_columns: set[str] = set()
    out: list[ValidatedMapping] = []
    for i, entry in enumerate(payload["mappings"]):
        if not isinstance(entry, dict):
            raise ProviderResponseError(f"mappings[{i}] is not an object")
        src = entry.get("source_column")
        if not isinstance(src, str):
            raise ProviderResponseError(f"mappings[{i}].source_column must be a string")
        if src not in columns or src in seen_columns:
            continue
        seen_columns.add(src)
        issues: list[str] = []
        target = entry.get("target_field")
        if target is not None and not isinstance(target, str):
            raise ProviderResponseError(f"mappings[{i}].target_field must be a string or null")
        conf = entry.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or math.isnan(conf):
            raise ProviderResponseError(f"mappings[{i}].confidence must be a number")
        raw_conf = float(conf)
        conf_f = min(max(raw_conf, 0.0), 1.0)
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ProviderResponseError(f"mappings[{i}].rationale must be a non-empty string")
        evidence_raw = entry.get("evidence", [])
        if not isinstance(evidence_raw, list):
            raise ProviderResponseError(f"mappings[{i}].evidence must be a list")
        evidence = [str(e) for e in evidence_raw if str(e).strip()]

        if target is not None and target not in field_names:
            issues.append(f"rejected hallucinated target '{target}' (not in schema)")
            target, conf_f = None, 0.0
        elif target is not None and target in taken:
            issues.append(f"rejected target '{target}': already mapped deterministically")
            target, conf_f = None, 0.0
        if target is not None and not evidence:
            issues.append("no evidence cited; confidence limited")
            conf_f = min(conf_f, UNCITED_CONFIDENCE_CEILING)
        if cap is not None and conf_f > cap:
            issues.append(f"confidence {conf_f:.2f} clamped to model cap {cap:.2f}")
            conf_f = cap
        out.append(ValidatedMapping(src, target, round(conf_f, 4), raw_conf, rationale.strip(),
                                    evidence, issues))
    return out
