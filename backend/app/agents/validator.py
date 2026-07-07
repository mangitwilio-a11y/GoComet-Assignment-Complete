"""
Validator Agent — compares extracted fields against a per-customer rule set.

Design stance: most validation is deterministic and should NOT go through an LLM.
Exact/enum/regex/numeric checks are plain Python — fast, free, and impossible to
hallucinate. The LLM is invoked ONLY for genuine semantic equality ("Shanghai" vs
"Port of Shanghai, CN") and even then all such checks are batched into ONE cheap
text call per document.

Three pre-checks run before any rule and always force `uncertain` (never silent
approval): missing value, ungrounded value (no source_quote), or confidence below
the customer's threshold.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from app.llm.client import LLMResult, get_client
from app.models import (
    ExtractedDocument,
    ExtractedField,
    FieldStatus,
    FieldValidation,
    ValidationResult,
)

RULES_DIR = Path(__file__).resolve().parent.parent / "config" / "rules"


def load_rules(customer: str) -> dict:
    """Load a customer's rule set. One engine, many configs: each customer is a
    file in config/rules/. An unknown customer is a hard error — we never silently
    fall back to another customer's rules."""
    path = RULES_DIR / f"{customer}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No rule set configured for customer '{customer}'. "
            f"Add config/rules/{customer}.json. Configured: {available_customers()}"
        )
    return json.loads(path.read_text())


def available_customers() -> list[str]:
    if not RULES_DIR.exists():
        return []
    return sorted(p.stem for p in RULES_DIR.glob("*.json"))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


# Common unit aliases normalised to a canonical token so "kgs"/"kilograms" == "kg".
_UNIT_ALIASES = {
    "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "mt": "t", "t": "t", "ton": "t", "tons": "t", "tonne": "t", "tonnes": "t",
    "g": "g", "gram": "g", "grams": "g",
}


def _qty(s: str) -> tuple[Optional[float], Optional[str]]:
    """Parse a quantity into (number, canonical_unit). Unit is None if absent."""
    m = re.search(r"([-+]?\d[\d,]*\.?\d*)\s*([a-zA-Z]+)?", s)
    if not m:
        return None, None
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower() if m.group(2) else None
    if unit is not None:
        unit = _UNIT_ALIASES.get(unit, unit)
    return num, unit


# ---------------------------------------------------------------------------
# Deterministic rule evaluation. Returns a FieldValidation, OR a "semantic
# request" dict when the LLM is needed to judge fuzzy equality.
# ---------------------------------------------------------------------------
def _eval_rule(field: str, ef: ExtractedField, rule: dict, threshold: float):
    value = ef.value

    # --- pre-checks: always surface, never silently pass ---
    if value is None:
        return FieldValidation(field=field, status=FieldStatus.UNCERTAIN, found=None,
                               expected=_expected_str(rule), confidence=ef.confidence,
                               reason="Field not found in document — needs human/supplier confirmation.")
    if not ef.source_quote:
        return FieldValidation(field=field, status=FieldStatus.UNCERTAIN, found=value,
                               expected=_expected_str(rule), confidence=ef.confidence,
                               reason="Value is ungrounded (no source text) — possible hallucination.")
    if ef.confidence < threshold:
        return FieldValidation(field=field, status=FieldStatus.UNCERTAIN, found=value,
                               expected=_expected_str(rule), confidence=ef.confidence,
                               reason=f"Extraction confidence {ef.confidence:.2f} below threshold {threshold:.2f}.")

    rtype = rule.get("type")

    if rtype == "regex":
        ok = bool(re.match(rule["pattern"], value.strip()))
        return _det(field, ef, ok, value, rule["pattern"],
                    "Matches required format." if ok else "Does not match required format.")

    if rtype == "allowlist":
        # Token-aware membership. Incoterms in real docs are written "<CODE> <named
        # place>" (e.g. "FOB Shanghai") — Incoterms 2020 require a named place, so the
        # bare code is the leading token, not the whole string. Accept the exact value
        # OR any allowlisted code appearing as a token, so "FOB Shanghai" matches "FOB".
        allow = {_norm(x) for x in rule["allowlist"]}
        tokens = set(_norm(value).split())
        ok = _norm(value) in allow or bool(allow & tokens)
        return _det(field, ef, ok, value, "/".join(rule["allowlist"]),
                    "Value is in allowed set." if ok else "Value not in allowed set.")

    if rtype == "regex_and_allowlist":
        fmt = bool(re.match(rule["pattern"], value.strip()))
        inlist = any(_norm(value).startswith(_norm(x)) or _norm(value) == _norm(x)
                     for x in rule["allowlist"])
        ok = fmt and inlist
        reason = ("Valid format and a contracted code." if ok
                  else ("Bad format." if not fmt else "Valid format but not a contracted code."))
        return _det(field, ef, ok, value, "/".join(rule["allowlist"]), reason)

    if rtype == "numeric_tolerance":
        got, unit = _qty(value)
        exp = float(rule["expected_value"])
        exp_unit = str(rule.get("unit", "")).strip().lower()
        exp_unit = _UNIT_ALIASES.get(exp_unit, exp_unit)
        if got is None:
            return _det(field, ef, False, value, f"{exp} {exp_unit}",
                        "Could not parse a number from the value.")
        # Unit safety: a value in the WRONG unit must never silently pass. This is
        # the difference between "12,500 kg" and "12,500 lb" (~5,670 kg).
        if exp_unit:
            if unit is None:
                return FieldValidation(
                    field=field, status=FieldStatus.UNCERTAIN, found=value,
                    expected=f"{exp} {exp_unit}", confidence=ef.confidence,
                    reason=f"Value has no unit; expected {exp_unit}. Cannot confirm magnitude — needs review.",
                )
            if unit != exp_unit:
                return _det(field, ef, False, value, f"{exp} {exp_unit}",
                            f"Unit mismatch: found '{unit}', expected '{exp_unit}'.")
        tol = exp * rule["tolerance_pct"] / 100.0
        ok = abs(got - exp) <= tol
        return _det(field, ef, ok, value, f"{exp} {exp_unit} (+/-{rule['tolerance_pct']}%)",
                    f"{got} {exp_unit} within tolerance." if ok
                    else f"{got} {unit or ''} outside +/-{rule['tolerance_pct']}% of {exp} {exp_unit}.")

    if rtype in ("exact_or_semantic", "allowlist_semantic", "must_contain_any_semantic"):
        # Try the cheap deterministic path first.
        det_ok = _try_deterministic_semantic(rtype, value, rule)
        if det_ok:
            return _det(field, ef, True, value, _expected_str(rule), "Exact/keyword match.")
        if rule.get("semantic_ok"):
            # Defer to the LLM batch.
            return {"field": field, "value": value, "ef": ef, "rule": rule, "rtype": rtype}
        return _det(field, ef, False, value, _expected_str(rule), "No exact/keyword match.")

    # Unknown rule type — fail safe to uncertain rather than approve.
    return FieldValidation(field=field, status=FieldStatus.UNCERTAIN, found=value,
                           expected=_expected_str(rule), confidence=ef.confidence,
                           reason=f"Unknown rule type '{rtype}'.")


def _try_deterministic_semantic(rtype: str, value: str, rule: dict) -> bool:
    v = _norm(value)
    if rtype == "exact_or_semantic":
        return v == _norm(rule["expected"])
    if rtype == "allowlist_semantic":
        return any(_norm(x) in v or v in _norm(x) for x in rule["allowlist"])
    if rtype == "must_contain_any_semantic":
        return any(_norm(k) in v for k in rule["keywords"])
    return False


def _det(field, ef, ok, found, expected, reason) -> FieldValidation:
    return FieldValidation(
        field=field, status=FieldStatus.MATCH if ok else FieldStatus.MISMATCH,
        found=found, expected=expected, confidence=ef.confidence, reason=reason, method="deterministic",
    )


def _expected_str(rule: dict) -> str:
    if "expected" in rule:
        return str(rule["expected"])
    if "allowlist" in rule:
        return "/".join(rule["allowlist"])
    if "keywords" in rule:
        return "one of: " + ", ".join(rule["keywords"])
    if "expected_value" in rule:
        return f"{rule['expected_value']} {rule.get('unit','')}".strip()
    if "pattern" in rule:
        return rule["pattern"]
    return ""


# ---------------------------------------------------------------------------
# Batched semantic resolution: one LLM call for all deferred fields.
# ---------------------------------------------------------------------------
SEM_SYSTEM = """You are a validation assistant. For each item, decide if the FOUND value is
semantically equivalent to / satisfies the EXPECTED criterion in a trade-document context
(e.g. 'Port of Shanghai, CN' satisfies 'Shanghai'; 'Acme Imports Limited' satisfies
'Acme Imports Ltd.'). Return ONLY JSON: {"results":[{"field":..,"match":true|false,"reason":..}]}."""


def _resolve_semantic(requests: list[dict]) -> tuple[dict[str, FieldValidation], Optional[LLMResult]]:
    if not requests:
        return {}, None
    items = [
        {"field": r["field"], "found": r["value"], "expected": _expected_str(r["rule"])}
        for r in requests
    ]
    user = "Judge each item:\n" + json.dumps(items, indent=2)
    # The LLM call itself is wrapped: a provider/network failure must degrade the
    # deferred fields to `uncertain`, never crash the whole validation run.
    try:
        llm = get_client().text_json(SEM_SYSTEM, user)
    except Exception:
        llm = None
    verdicts: dict = {}
    if llm is not None:
        try:
            verdicts = {v["field"]: v for v in llm.json().get("results", []) if isinstance(v, dict)}
        except Exception:
            verdicts = {}

    out: dict[str, FieldValidation] = {}
    for r in requests:
        ef = r["ef"]
        v = verdicts.get(r["field"])
        match = v.get("match") if isinstance(v, dict) else None
        # Accept ONLY a real boolean. A missing verdict, malformed JSON, a provider
        # outage, or a stringy "false"/"true" (bool("false") is True!) is
        # INCONCLUSIVE -> uncertain (human review), never a silent (mis)match.
        if not isinstance(match, bool):
            out[r["field"]] = FieldValidation(
                field=r["field"], status=FieldStatus.UNCERTAIN, found=r["value"],
                expected=_expected_str(r["rule"]), confidence=ef.confidence,
                reason="Semantic check inconclusive or unavailable — escalating to human review.",
                method="semantic",
            )
            continue
        out[r["field"]] = FieldValidation(
            field=r["field"], status=FieldStatus.MATCH if match else FieldStatus.MISMATCH,
            found=r["value"], expected=_expected_str(r["rule"]), confidence=ef.confidence,
            reason=(v.get("reason") or "Semantic judgement."), method="semantic",
        )
    return out, llm


def validate(doc: ExtractedDocument, customer: str) -> tuple[ValidationResult, Optional[LLMResult]]:
    rules_cfg = load_rules(customer)
    rules = rules_cfg["rules"]
    threshold = float(rules_cfg.get("routing_policy", {}).get("min_field_confidence", 0.6))
    field_map = {k: ExtractedField(**v) for k, v in doc.as_field_map().items()}

    results: list[FieldValidation] = []
    semantic_requests: list[dict] = []
    for field, rule in rules.items():
        ef = field_map.get(field, ExtractedField())
        outcome = _eval_rule(field, ef, rule, threshold)
        if isinstance(outcome, dict):
            semantic_requests.append(outcome)
        else:
            results.append(outcome)

    semantic_results, llm = _resolve_semantic(semantic_requests)
    results.extend(semantic_results.values())

    # Stable ordering by the rule definition order.
    order = list(rules.keys())
    results.sort(key=lambda r: order.index(r.field) if r.field in order else 999)

    return ValidationResult(
        results=results,
        has_mismatch=any(r.status == FieldStatus.MISMATCH for r in results),
        has_uncertain=any(r.status == FieldStatus.UNCERTAIN for r in results),
    ), llm
