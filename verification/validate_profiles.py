"""Validate profiles.yaml against skill_aliases.yml and location_aliases.yml.

USAGE (run from the project root)
    python verification/validate_profiles.py        # exits 0 OK / 1 errors
    python -m verification.validate_profiles        # equivalent module form

LIBRARY USE
    from verification.validate_profiles import validate_profiles, ValidationError
    errors = validate_profiles()           # returns list[str]; [] means OK
    # or, to raise instead:
    validate_profiles(raise_on_error=True) # raises ValidationError on any issue

WHAT IT CHECKS
    1. All three YAML files parse and have the expected top-level shape.
    2. Every profile has the required fields with the right types.
    3. profile_name values are unique.
    4. years_of_experience is a finite float in [0, 60].
    5. Every dotted ref under `locations:` resolves into location_aliases.yml.
    6. Every rule has type in {"boolean","scored"} and a non-empty expression.
    7. Every group reference inside every expression resolves to a real path
       in skill_aliases.yml or location_aliases.yml.
    8. Boolean expressions have balanced parens.
    9. Scored expressions parse: comma-separated `ref=number` weight pairs
       followed by at least one `totalscore (>=|<=) number` threshold (and
       optionally `distinct_matches (>=|<=) number`). Weights must be numeric.

This script makes NO claim about the SEMANTICS of the rules — it only
verifies they are well-formed and reference real groups. The actual matcher
that evaluates rules against jobs is a future step.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

# Resolve project root one level above this file so the script works whether
# it's invoked as `python verification/validate_profiles.py` or as
# `python -m verification.validate_profiles`, regardless of CWD.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILES_PATH = PROJECT_ROOT / "profiles.yaml"
SKILL_ALIASES_PATH = PROJECT_ROOT / "skill_aliases.yml"
LOCATION_ALIASES_PATH = PROJECT_ROOT / "location_aliases.yml"

ALLOWED_RULE_TYPES = {"boolean", "scored"}
ALLOWED_THRESHOLD_KEYS = {"totalscore", "distinct_matches"}
MAX_REASONABLE_YOE = 60.0

# Matches a dotted ref like `skills.languages.go` or `roles.role_families.backend_engineer`.
# Allows digits in canonical names (e.g. `aws_services.s3`). Non-capturing on
# purpose — re.findall returns capture groups, not full matches, if any group
# is capturing.
REF_RE = re.compile(r"\b(?:skills|roles|locations)\.[a-z0-9_]+\.[a-z0-9_]+\b")

# Boolean DSL: only `and`, `or`, `not`, parens, refs, whitespace are allowed.
BOOLEAN_TOKEN_RE = re.compile(
    r"(?:\b(?:and|or|not)\b)"
    r"|(?:[()])"
    r"|(?:\b(?:skills|roles|locations)\.[a-z0-9_]+\.[a-z0-9_]+\b)"
    r"|(?:\s+)"
)

# Scored DSL: `ref=number` weight pairs and `key>=number` / `key<=number` thresholds.
WEIGHT_PAIR_RE = re.compile(
    r"^(?P<ref>(?:skills|roles|locations)\.[a-z0-9_]+\.[a-z0-9_]+)"
    r"\s*=\s*(?P<weight>-?\d+(?:\.\d+)?)$"
)
THRESHOLD_RE = re.compile(
    r"^(?P<key>[a-z_]+)\s*(?P<op>>=|<=)\s*(?P<num>-?\d+(?:\.\d+)?)$"
)


class ValidationError(Exception):
    """Raised by validate_profiles(raise_on_error=True) on any failure."""


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"Required file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_ref(
    ref: str,
    skills_root: dict,
    roles_root: dict,
    locations_root: dict,
) -> bool:
    """Return True iff `ref` (a dotted path) exists in the alias files."""
    parts = ref.split(".")
    if len(parts) != 3:
        return False
    top, category, canonical = parts
    root = {
        "skills": skills_root,
        "roles": roles_root,
        "locations": locations_root,
    }.get(top)
    if not isinstance(root, dict):
        return False
    cat = root.get(category)
    if not isinstance(cat, dict):
        return False
    return canonical in cat


def _check_balanced_parens(expr: str) -> bool:
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _check_boolean_only_known_tokens(expr: str) -> str | None:
    """Return None if OK, else an error message describing the bad token."""
    remaining = expr
    while remaining:
        m = BOOLEAN_TOKEN_RE.match(remaining)
        if not m:
            snippet = remaining[:30].replace("\n", " ")
            return f"unexpected token near {snippet!r}"
        remaining = remaining[m.end():]
    return None


def _validate_scored_expression(
    expr: str,
    resolver,
) -> list[str]:
    errs: list[str] = []
    parts = [p.strip() for p in expr.split(",") if p.strip()]
    if not parts:
        errs.append("scored expression is empty")
        return errs

    saw_threshold = False
    for part in parts:
        m_thr = THRESHOLD_RE.match(part)
        if m_thr:
            key = m_thr.group("key")
            if key not in ALLOWED_THRESHOLD_KEYS:
                errs.append(
                    f"unknown threshold key {key!r}; "
                    f"allowed: {sorted(ALLOWED_THRESHOLD_KEYS)}"
                )
            else:
                saw_threshold = True
            continue

        m_pair = WEIGHT_PAIR_RE.match(part)
        if m_pair:
            ref = m_pair.group("ref")
            if not resolver(ref):
                errs.append(f"unresolved group reference: {ref}")
            continue

        errs.append(f"could not parse scored clause: {part!r}")

    if not saw_threshold:
        errs.append(
            "scored expression has no threshold clause "
            "(need e.g. `totalscore>=N`)"
        )
    return errs


def validate_profiles(raise_on_error: bool = False) -> list[str]:
    """Run all validation checks. Returns a list of error strings (empty = OK).

    If raise_on_error is True, raises ValidationError instead of returning a
    non-empty list.
    """
    errors: list[str] = []

    try:
        skills_doc = _load_yaml(SKILL_ALIASES_PATH)
        locations_doc = _load_yaml(LOCATION_ALIASES_PATH)
        profiles_doc = _load_yaml(PROFILES_PATH)
    except Exception as e:
        errors.append(f"failed to load YAML: {e}")
        if raise_on_error:
            raise ValidationError("\n".join(errors)) from None
        return errors

    skills_root = skills_doc.get("skills", {}) if isinstance(skills_doc, dict) else {}
    roles_root = skills_doc.get("roles", {}) if isinstance(skills_doc, dict) else {}
    locations_root = (
        locations_doc.get("locations", {}) if isinstance(locations_doc, dict) else {}
    )

    if not isinstance(skills_root, dict) or not skills_root:
        errors.append("skill_aliases.yml: missing or empty top-level `skills:`")
    if not isinstance(roles_root, dict) or not roles_root:
        errors.append("skill_aliases.yml: missing or empty top-level `roles:`")
    if not isinstance(locations_root, dict) or not locations_root:
        errors.append("location_aliases.yml: missing or empty top-level `locations:`")

    profiles = profiles_doc.get("profiles") if isinstance(profiles_doc, dict) else None
    if not isinstance(profiles, list) or not profiles:
        errors.append("profiles.yaml: missing or empty top-level `profiles:` list")
        if raise_on_error:
            raise ValidationError("\n".join(errors))
        return errors

    def resolver(ref: str) -> bool:
        return _resolve_ref(ref, skills_root, roles_root, locations_root)

    seen_names: set[str] = set()

    for idx, prof in enumerate(profiles):
        tag = f"profiles[{idx}]"

        if not isinstance(prof, dict):
            errors.append(f"{tag}: must be a mapping, got {type(prof).__name__}")
            continue

        name = prof.get("profile_name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{tag}: missing or empty `profile_name`")
        else:
            tag = f"profile {name!r}"
            if name in seen_names:
                errors.append(f"{tag}: duplicate profile_name")
            seen_names.add(name)

        if "enable" not in prof:
            errors.append(f"{tag}: missing `enable` (bool)")
        elif not isinstance(prof["enable"], bool):
            errors.append(f"{tag}: `enable` must be bool, got {type(prof['enable']).__name__}")

        yoe = prof.get("years_of_experience")
        if not isinstance(yoe, (int, float)) or isinstance(yoe, bool):
            errors.append(
                f"{tag}: `years_of_experience` must be a number "
                f"(reminder: 2y 8m = 2.67, NOT 2.8)"
            )
        elif not (0.0 <= float(yoe) <= MAX_REASONABLE_YOE):
            errors.append(
                f"{tag}: `years_of_experience` {yoe} outside [0, {MAX_REASONABLE_YOE}]"
            )

        locs = prof.get("locations")
        if not isinstance(locs, list) or not locs:
            errors.append(f"{tag}: `locations` must be a non-empty list")
        else:
            for loc in locs:
                if not isinstance(loc, str):
                    errors.append(f"{tag}: location entry must be string, got {loc!r}")
                    continue
                if not loc.startswith("locations."):
                    errors.append(
                        f"{tag}: location entry must start with `locations.`, got {loc!r}"
                    )
                    continue
                if not resolver(loc):
                    errors.append(f"{tag}: unresolved location reference: {loc}")

        rules = prof.get("matching_rules")
        if not isinstance(rules, list) or not rules:
            errors.append(f"{tag}: `matching_rules` must be a non-empty list")
            continue

        for r_idx, rule in enumerate(rules):
            rtag = f"{tag} rule[{r_idx}]"
            if not isinstance(rule, dict):
                errors.append(f"{rtag}: must be a mapping")
                continue
            rtype = rule.get("type")
            if rtype not in ALLOWED_RULE_TYPES:
                errors.append(
                    f"{rtag}: `type` must be one of {sorted(ALLOWED_RULE_TYPES)}, "
                    f"got {rtype!r}"
                )
                continue
            expr = rule.get("expression")
            if not isinstance(expr, str) or not expr.strip():
                errors.append(f"{rtag}: `expression` must be a non-empty string")
                continue
            expr_clean = " ".join(expr.split())

            if rtype == "boolean":
                if not _check_balanced_parens(expr_clean):
                    errors.append(f"{rtag}: unbalanced parentheses")
                bad = _check_boolean_only_known_tokens(expr_clean)
                if bad:
                    errors.append(f"{rtag}: {bad}")
                for ref in REF_RE.findall(expr_clean):
                    if not resolver(ref):
                        errors.append(f"{rtag}: unresolved reference: {ref}")
            else:
                for e in _validate_scored_expression(expr_clean, resolver):
                    errors.append(f"{rtag}: {e}")

    if raise_on_error and errors:
        raise ValidationError("\n".join(errors))
    return errors


def _main() -> int:
    errors = validate_profiles()
    if errors:
        print(f"FAIL — {len(errors)} validation error(s):", flush=True)
        for e in errors:
            print(f"  - {e}", flush=True)
        return 1
    print("OK — profiles.yaml is valid.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
