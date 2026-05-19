r"""Match a profile's matching_rules against a Job Description (JD).

This module is the single source of truth for "does this JD match this
profile?" — it will be used both by:

  1. The validation use case (a candidate adds/edits a profile and pastes
     a known-good JD to verify the rules behave as expected), and
  2. The runtime pipeline matcher (Steps 4 & 5 of the pipeline; future).

The semantics are deliberately strict and mirror the conventions documented
in `context.md`, `skill_aliases.yml`, and `location_aliases.yml`.

USAGE — library
    from verification.match_profile import (
        match_profile_against_jd, MatchResult, RuleResult, GroupMatch,
        load_alias_files, load_profile,
    )

    skills, roles, locations = load_alias_files()
    profile = load_profile("yaswanth_backend_distsys")
    result = match_profile_against_jd(profile, jd_text,
                                      skills, roles, locations)
    print(result.passed)              # bool: did EVERY rule pass?
    for rr in result.per_rule:
        print(rr.rule_index, rr.passed, rr.groups)

USAGE — CLI (run from project root)
    # Single profile against a JD file
    python verification/match_profile.py <profile_name> --jd-file path/to/jd.txt

    # Single profile against an inline JD
    python verification/match_profile.py <profile_name> --jd "Software Engineer ..."

    # Single profile against a JD piped on stdin
    cat jd.txt | python verification/match_profile.py <profile_name>

    # Evaluate ALL profiles against the same JD (good for cross-profile testing)
    python verification/match_profile.py --all --jd-file path/to/jd.txt

    # Verbose: also print which aliases matched (and which didn't) per rule
    python verification/match_profile.py <profile_name> --jd-file jd.txt -v

Exit codes: 0 if the profile (or all profiles, with --all) passed; 1 if not;
2 on usage/IO errors.

ALGORITHM
    For each rule in profile.matching_rules:
      1. Collect every group reference in the rule (e.g.
         `skills.languages.go`, `roles.role_families.backend_engineer`).
      2. For each ref, walk into the alias YAML and pull the alias list.
      3. For each alias in that list, search the normalized JD with a
         word-boundary-aware regex. The group is TRUE iff at least one
         alias matched.
      4. Plug the per-group truth values into the rule's expression:
           * boolean → recursive-descent eval over `and`/`or`/`not`/parens
           * scored  → sum of matched weights, then check thresholds
                       (`totalscore>=N`, `distinct_matches>=N`/`<=N`)

    The profile passes iff EVERY rule passes (AND across the list).

MATCHING CONVENTIONS (must stay in sync with context.md / alias files)
    - Case-insensitive.
    - All whitespace runs collapsed to a single space.
    - Word-boundary matching is MANDATORY (no substring matches). The
      "word" character class is `[A-Za-z0-9_+#]` — extended beyond `\w`
      so `c++` and `c#` match cleanly without false-positive overruns.
    - Multi-word aliases are split on whitespace and rejoined with `\s+`
      so they tolerate any whitespace inside the JD.
    - Alias variants with different separators (back end / back-end /
      backend) are NOT inferred — every variant must be listed explicitly
      in `skill_aliases.yml` / `location_aliases.yml`. This is by design:
      the alias files are the spec; the matcher does not guess.

This module does NOT auto-run validation. Callers wiring it into the
runtime pipeline are still expected to call
`verification.validate_profiles.validate_profiles(raise_on_error=True)`
once at startup, per Rule 1 in context.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

# Resolve project root one level above this file so the script works whether
# it's invoked as `python verification/match_profile.py` or as
# `python -m verification.match_profile`, regardless of CWD. Mirrors the
# convention used by validate_profiles.py / try_fetch.py / audit_slugs.py.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROFILES_PATH = PROJECT_ROOT / "profiles.yaml"
SKILL_ALIASES_PATH = PROJECT_ROOT / "skill_aliases.yml"
LOCATION_ALIASES_PATH = PROJECT_ROOT / "location_aliases.yml"

ALLOWED_RULE_TYPES = {"boolean", "scored"}
ALLOWED_THRESHOLD_KEYS = {"totalscore", "distinct_matches"}

# A dotted group reference: `skills.<category>.<canonical>`,
# `roles.<category>.<canonical>`, or `locations.<region>.<canonical>`.
_REF_RE = re.compile(r"\b(?:skills|roles|locations)\.[a-z0-9_]+\.[a-z0-9_]+\b")

# Boolean DSL: refs, `and`/`or`/`not`, and `(`/`)`. Whitespace is handled by
# the tokenizer separately.
_BOOL_TOKEN_RE = re.compile(
    r"\b(?:and|or|not)\b"
    r"|[()]"
    r"|\b(?:skills|roles|locations)\.[a-z0-9_]+\.[a-z0-9_]+\b"
)

# Scored DSL: comma-separated `ref=weight` pairs and `key>=num` / `key<=num`
# thresholds. (Mirrors validate_profiles.WEIGHT_PAIR_RE / THRESHOLD_RE so a
# rule that validates also parses here.)
_WEIGHT_PAIR_RE = re.compile(
    r"^(?P<ref>(?:skills|roles|locations)\.[a-z0-9_]+\.[a-z0-9_]+)"
    r"\s*=\s*(?P<weight>-?\d+(?:\.\d+)?)$"
)
_THRESHOLD_RE = re.compile(
    r"^(?P<key>[a-z_]+)\s*(?P<op>>=|<=)\s*(?P<num>-?\d+(?:\.\d+)?)$"
)

# Custom word-boundary character class. Bare `\w` is `[A-Za-z0-9_]`, which
# means `\bc++\b` does NOT do the right thing (the `\b` after the second `+`
# requires a word char to follow). By treating `+` and `#` as "word-like"
# via lookarounds, we ensure:
#   - `c++` matches in "we use c++ heavily" but NOT in "c+++" or "cc++".
#   - `c#`  matches in "c# 7.0" but NOT in "c#5" or "ac#".
# Anything else (spaces, punctuation, line breaks) is a valid boundary.
_WORDLIKE = r"[A-Za-z0-9_+#]"
_LEFT_BOUNDARY = rf"(?<!{_WORDLIKE})"
_RIGHT_BOUNDARY = rf"(?!{_WORDLIKE})"


class MatcherError(Exception):
    """Raised on malformed rules or unresolved group references at match time.

    The validator (verification/validate_profiles.py) is supposed to catch all
    of these at config-load time, but the matcher is defensive: it never
    silently treats a malformed rule as "passed" — it surfaces the error in
    the RuleResult and fails the rule.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Result types — kept as dataclasses so callers (CLI, future webhook, tests)
# can introspect every detail of why a JD did/didn't match a profile.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GroupMatch:
    """Result of searching one group's alias list against the JD."""

    ref: str
    matched: bool
    matched_aliases: list[str] = field(default_factory=list)
    # (start, end, matched_text) tuples — useful for highlighting in UIs and
    # for debugging false positives. Indexes are into the *normalized* JD.
    matched_spans: list[tuple[int, int, str]] = field(default_factory=list)


@dataclass
class RuleResult:
    """Result of evaluating one rule in profile.matching_rules."""

    rule_index: int
    rule_type: str
    description: str
    expression: str
    passed: bool
    groups: dict[str, GroupMatch] = field(default_factory=dict)
    # Scored-rule extras (None for boolean rules).
    total_score: float | None = None
    distinct_match_count: int | None = None
    threshold_results: list[tuple[str, str, float, bool]] = field(
        default_factory=list
    )  # (key, op, num, passed)
    error: str | None = None


@dataclass
class MatchResult:
    """Result of evaluating an entire profile against a single JD."""

    profile_name: str
    passed: bool
    per_rule: list[RuleResult]
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# JD normalization + alias → regex compilation (with a process-wide cache).
# ─────────────────────────────────────────────────────────────────────────────


def normalize_jd(text: str) -> str:
    """Lowercase the JD and collapse every run of whitespace to a single space.

    We deliberately do NOT strip punctuation — `c++`, `pl/sql`, `next.js`
    must remain searchable, and the alias files already enumerate the
    common separator variants explicitly. Touching punctuation here would
    just create new false-positive surfaces.
    """
    if not isinstance(text, str):
        raise MatcherError(
            f"JD text must be a string, got {type(text).__name__}"
        )
    return re.sub(r"\s+", " ", text.lower())


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _alias_to_pattern(alias: str) -> re.Pattern[str]:
    """Compile a regex that matches `alias` in a normalized JD.

    Steps:
      1. Lowercase + strip the alias (defense in depth; the YAML may carry
         trailing whitespace).
      2. Split on whitespace, regex-escape each piece, rejoin with `\\s+`
         so the alias matches across any internal whitespace gap in the JD.
      3. Wrap with custom left/right boundaries that treat `+` and `#` as
         in-word (so c++ / c# match cleanly).

    Results are cached at module scope because alias lists are static for
    the life of the process.
    """
    cached = _PATTERN_CACHE.get(alias)
    if cached is not None:
        return cached
    a = alias.strip().lower()
    if not a:
        raise MatcherError("empty alias is not allowed")
    parts = a.split()
    body = r"\s+".join(re.escape(p) for p in parts)
    pattern = re.compile(_LEFT_BOUNDARY + body + _RIGHT_BOUNDARY)
    _PATTERN_CACHE[alias] = pattern
    return pattern


def clear_pattern_cache() -> None:
    """Drop the alias-regex cache. Useful in tests / after editing aliases."""
    _PATTERN_CACHE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Group ref → alias list → JD scan.
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_ref_to_aliases(
    ref: str,
    skills_root: dict,
    roles_root: dict,
    locations_root: dict,
) -> list[str] | None:
    """Walk a dotted ref into the alias YAML. Returns the alias list, or None
    if the ref doesn't resolve (caller turns this into a MatcherError)."""
    parts = ref.split(".")
    if len(parts) != 3:
        return None
    top, category, canonical = parts
    root = {
        "skills": skills_root,
        "roles": roles_root,
        "locations": locations_root,
    }.get(top)
    if not isinstance(root, dict):
        return None
    cat = root.get(category)
    if not isinstance(cat, dict):
        return None
    aliases = cat.get(canonical)
    if not isinstance(aliases, list):
        return None
    return [str(a) for a in aliases if isinstance(a, str) and a.strip()]


def _scan_group_in_jd(
    ref: str,
    aliases: list[str],
    jd_normalized: str,
) -> GroupMatch:
    """Search the JD for any alias of this group.

    Implementation note: we collect ALL aliases that matched (not just the
    first). The cost is small — at most one `re.search` per alias — and
    the debug benefit for the validation use case is huge ("your profile
    triggered on these 4 aliases: ...").
    """
    matched_aliases: list[str] = []
    matched_spans: list[tuple[int, int, str]] = []
    for alias in aliases:
        pat = _alias_to_pattern(alias)
        m = pat.search(jd_normalized)
        if m is not None:
            matched_aliases.append(alias)
            matched_spans.append((m.start(), m.end(), m.group(0)))
    return GroupMatch(
        ref=ref,
        matched=bool(matched_aliases),
        matched_aliases=matched_aliases,
        matched_spans=matched_spans,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Boolean DSL: tokenize + recursive-descent eval.
#
# We DELIBERATELY do not use eval()/exec() — the project's "Secure Python
# Development" rule (#3) forbids dynamic evaluation of YAML-sourced content,
# even after validation. A 60-line hand-written parser is cheap insurance.
#
# Grammar:
#   or_expr  ::= and_expr ('or'  and_expr)*
#   and_expr ::= not_expr ('and' not_expr)*
#   not_expr ::= 'not' not_expr | atom
#   atom     ::= REF | '(' or_expr ')'
# ─────────────────────────────────────────────────────────────────────────────


def _tokenize_boolean(expr: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        if expr[i].isspace():
            i += 1
            continue
        m = _BOOL_TOKEN_RE.match(expr, i)
        if not m:
            snippet = expr[i : i + 30].replace("\n", " ")
            raise MatcherError(
                f"unexpected token in boolean expression near {snippet!r}"
            )
        tokens.append(m.group(0))
        i = m.end()
    if not tokens:
        raise MatcherError("boolean expression is empty after tokenization")
    return tokens


class _BoolParser:
    def __init__(self, tokens: list[str], values: dict[str, bool]):
        self.tokens = tokens
        self.i = 0
        self.values = values

    def _peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _consume(self) -> str:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self) -> bool:
        result = self._or_expr()
        if self.i != len(self.tokens):
            raise MatcherError(
                f"unexpected trailing token: {self.tokens[self.i]!r}"
            )
        return result

    def _or_expr(self) -> bool:
        v = self._and_expr()
        while self._peek() == "or":
            self._consume()
            rhs = self._and_expr()
            v = v or rhs
        return v

    def _and_expr(self) -> bool:
        v = self._not_expr()
        while self._peek() == "and":
            self._consume()
            rhs = self._not_expr()
            v = v and rhs
        return v

    def _not_expr(self) -> bool:
        if self._peek() == "not":
            self._consume()
            return not self._not_expr()
        return self._atom()

    def _atom(self) -> bool:
        tok = self._peek()
        if tok is None:
            raise MatcherError("unexpected end of boolean expression")
        if tok == "(":
            self._consume()
            v = self._or_expr()
            if self._peek() != ")":
                raise MatcherError("missing closing parenthesis")
            self._consume()
            return v
        if tok in {"and", "or", "not", ")"}:
            raise MatcherError(f"unexpected token in atom: {tok!r}")
        ref = self._consume()
        if ref not in self.values:
            raise MatcherError(f"unknown reference in expression: {ref}")
        return self.values[ref]


def _eval_boolean(
    expr: str,
    resolver: Callable[[str], GroupMatch],
) -> tuple[bool, dict[str, GroupMatch]]:
    tokens = _tokenize_boolean(expr)
    refs_in_expr = sorted({t for t in tokens if "." in t})
    group_matches: dict[str, GroupMatch] = {}
    values: dict[str, bool] = {}
    for ref in refs_in_expr:
        gm = resolver(ref)
        group_matches[ref] = gm
        values[ref] = gm.matched
    return _BoolParser(tokens, values).parse(), group_matches


# ─────────────────────────────────────────────────────────────────────────────
# Scored DSL evaluator.
#
# Grammar (comma-separated):
#   scored ::= clause (',' clause)*
#   clause ::= REF '=' NUMBER          # weighted pair
#           |  KEY ('>=' | '<=') NUMBER # threshold (totalscore, distinct_matches)
# At least one threshold clause is required.
# ─────────────────────────────────────────────────────────────────────────────


def _eval_scored(
    expr: str,
    resolver: Callable[[str], GroupMatch],
) -> tuple[
    bool,
    dict[str, GroupMatch],
    float,
    int,
    list[tuple[str, str, float, bool]],
]:
    parts = [p.strip() for p in expr.split(",") if p.strip()]
    if not parts:
        raise MatcherError("scored expression is empty")

    pairs: list[tuple[str, float]] = []
    thresholds: list[tuple[str, str, float]] = []
    for part in parts:
        m_thr = _THRESHOLD_RE.match(part)
        if m_thr:
            key = m_thr.group("key")
            if key not in ALLOWED_THRESHOLD_KEYS:
                raise MatcherError(
                    f"unknown threshold key {key!r}; "
                    f"allowed: {sorted(ALLOWED_THRESHOLD_KEYS)}"
                )
            thresholds.append(
                (key, m_thr.group("op"), float(m_thr.group("num")))
            )
            continue
        m_pair = _WEIGHT_PAIR_RE.match(part)
        if m_pair:
            pairs.append(
                (m_pair.group("ref"), float(m_pair.group("weight")))
            )
            continue
        raise MatcherError(f"could not parse scored clause: {part!r}")

    if not thresholds:
        raise MatcherError(
            "scored expression has no threshold clause "
            "(need e.g. `totalscore>=N`)"
        )

    group_matches: dict[str, GroupMatch] = {}
    total_score = 0.0
    for ref, weight in pairs:
        if ref not in group_matches:
            group_matches[ref] = resolver(ref)
        if group_matches[ref].matched:
            total_score += weight

    # `distinct_matches` counts distinct REFS that matched, not pair
    # occurrences — so duplicating a ref in the expression (which the
    # validator currently permits) doesn't inflate the count.
    distinct = sum(
        1 for ref in {p[0] for p in pairs} if group_matches[ref].matched
    )

    threshold_results: list[tuple[str, str, float, bool]] = []
    all_pass = True
    for key, op, num in thresholds:
        value: float = total_score if key == "totalscore" else float(distinct)
        ok = (value >= num) if op == ">=" else (value <= num)
        threshold_results.append((key, op, num, ok))
        if not ok:
            all_pass = False

    return all_pass, group_matches, total_score, distinct, threshold_results


# ─────────────────────────────────────────────────────────────────────────────
# File loading helpers (small wrappers; do NOT auto-validate).
# ─────────────────────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        raise MatcherError(f"required file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_alias_files() -> tuple[dict, dict, dict]:
    """Load `(skills_root, roles_root, locations_root)` from disk.

    Returns the three top-level subtrees the matcher needs. Empty dicts on
    missing top-level keys — callers may layer validation on top.
    """
    skills_doc = _load_yaml(SKILL_ALIASES_PATH)
    locations_doc = _load_yaml(LOCATION_ALIASES_PATH)
    skills_root = (
        skills_doc.get("skills", {}) if isinstance(skills_doc, dict) else {}
    )
    roles_root = (
        skills_doc.get("roles", {}) if isinstance(skills_doc, dict) else {}
    )
    locations_root = (
        locations_doc.get("locations", {})
        if isinstance(locations_doc, dict)
        else {}
    )
    return skills_root, roles_root, locations_root


def load_profile(profile_name: str) -> dict:
    """Load one profile dict from profiles.yaml by its `profile_name`.

    Raises MatcherError if profiles.yaml is malformed or the name is missing.
    """
    doc = _load_yaml(PROFILES_PATH)
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    if not isinstance(profiles, list):
        raise MatcherError(
            "profiles.yaml: missing or non-list top-level `profiles:`"
        )
    for prof in profiles:
        if (
            isinstance(prof, dict)
            and prof.get("profile_name") == profile_name
        ):
            return prof
    raise MatcherError(f"profile {profile_name!r} not found in profiles.yaml")


def load_all_profiles() -> list[dict]:
    """Return every profile dict from profiles.yaml (no filtering)."""
    doc = _load_yaml(PROFILES_PATH)
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    if not isinstance(profiles, list):
        raise MatcherError(
            "profiles.yaml: missing or non-list top-level `profiles:`"
        )
    return [p for p in profiles if isinstance(p, dict)]


# ─────────────────────────────────────────────────────────────────────────────
# Public matcher entry point.
# ─────────────────────────────────────────────────────────────────────────────


def match_profile_against_jd(
    profile: dict,
    jd_text: str,
    skills_root: dict | None = None,
    roles_root: dict | None = None,
    locations_root: dict | None = None,
) -> MatchResult:
    """Evaluate every rule in `profile.matching_rules` against `jd_text`.

    The profile passes iff every rule passes (AND across `matching_rules`).
    A `boolean` rule passes iff its expression evaluates to True. A `scored`
    rule passes iff every threshold clause is satisfied.

    A group reference (`skills.languages.go`, etc.) is considered TRUE iff
    at least one of its aliases is found in the JD with the case-insensitive
    word-boundary semantics documented in this module's docstring.

    Args:
      profile: a profile dict as parsed from profiles.yaml. Must have a
        `matching_rules` list; `profile_name` is used only for reporting.
      jd_text: the full JD text (title + body OK). Will be normalized
        (lowercased, whitespace collapsed) before matching.
      skills_root, roles_root, locations_root: optional pre-loaded alias
        subtrees. If any is None, all three are loaded from disk. Pass
        them explicitly when matching many JDs against many profiles to
        avoid repeated file I/O.

    Returns:
      A MatchResult with per-rule pass/fail and per-group debug info.
    """
    if not isinstance(profile, dict):
        raise MatcherError("profile must be a dict")
    name = profile.get("profile_name") or "<unnamed>"
    if not isinstance(name, str):
        raise MatcherError(
            f"profile_name must be a string, got {type(name).__name__}"
        )
    rules = profile.get("matching_rules")
    if not isinstance(rules, list) or not rules:
        return MatchResult(
            profile_name=name,
            passed=False,
            per_rule=[],
            errors=[
                f"profile {name!r}: `matching_rules` is missing or empty"
            ],
        )

    if skills_root is None or roles_root is None or locations_root is None:
        skills_root, roles_root, locations_root = load_alias_files()

    jd_norm = normalize_jd(jd_text)

    # Per-match cache so repeated refs across rules don't re-scan the JD.
    group_cache: dict[str, GroupMatch] = {}

    def resolver(ref: str) -> GroupMatch:
        cached = group_cache.get(ref)
        if cached is not None:
            return cached
        aliases = _resolve_ref_to_aliases(
            ref, skills_root, roles_root, locations_root
        )
        if aliases is None:
            # Bubble up; the caller turns this into a per-rule error. We do
            # NOT silently treat an unresolved ref as `False` — that's the
            # exact failure mode validate_profiles.py exists to prevent,
            # and silently matching/non-matching jobs is the worst kind of
            # bug for this pipeline.
            raise MatcherError(f"unresolved group reference: {ref}")
        gm = _scan_group_in_jd(ref, aliases, jd_norm)
        group_cache[ref] = gm
        return gm

    per_rule: list[RuleResult] = []
    all_pass = True

    for r_idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            rr = RuleResult(
                rule_index=r_idx,
                rule_type="",
                description="",
                expression="",
                passed=False,
                error=f"rule[{r_idx}] is not a mapping",
            )
            per_rule.append(rr)
            all_pass = False
            continue

        rtype = rule.get("type")
        expr = rule.get("expression")
        desc_raw = rule.get("description") or ""
        rr = RuleResult(
            rule_index=r_idx,
            rule_type=str(rtype) if rtype else "",
            description=desc_raw.strip() if isinstance(desc_raw, str) else "",
            expression=expr if isinstance(expr, str) else "",
            passed=False,
        )

        if rtype not in ALLOWED_RULE_TYPES:
            rr.error = (
                f"rule[{r_idx}]: `type` must be one of "
                f"{sorted(ALLOWED_RULE_TYPES)}, got {rtype!r}"
            )
            per_rule.append(rr)
            all_pass = False
            continue
        if not isinstance(expr, str) or not expr.strip():
            rr.error = (
                f"rule[{r_idx}]: `expression` must be a non-empty string"
            )
            per_rule.append(rr)
            all_pass = False
            continue

        # Normalize internal whitespace once before parsing (newlines in
        # YAML block scalars become spaces, multi-spaces collapse).
        expr_clean = " ".join(expr.split())

        try:
            if rtype == "boolean":
                passed, groups = _eval_boolean(expr_clean, resolver)
                rr.passed = passed
                rr.groups = groups
            else:  # scored
                (
                    passed,
                    groups,
                    total,
                    distinct,
                    thresholds,
                ) = _eval_scored(expr_clean, resolver)
                rr.passed = passed
                rr.groups = groups
                rr.total_score = total
                rr.distinct_match_count = distinct
                rr.threshold_results = thresholds
        except MatcherError as exc:
            rr.error = str(exc)
            rr.passed = False

        if not rr.passed:
            all_pass = False
        per_rule.append(rr)

    return MatchResult(profile_name=name, passed=all_pass, per_rule=per_rule)


# ─────────────────────────────────────────────────────────────────────────────
# CLI.
# ─────────────────────────────────────────────────────────────────────────────


def _read_jd_arg(args: argparse.Namespace) -> str:
    if args.jd_file:
        # NOTE: argparse-supplied path; no user-controlled HTTP/exec. We only
        # read text for matching, never execute it. Safe per security rule #1.
        path = Path(args.jd_file)
        if not path.is_file():
            raise MatcherError(f"JD file not found: {path}")
        return path.read_text(encoding="utf-8")
    if args.jd is not None:
        return args.jd
    if sys.stdin.isatty():
        raise MatcherError(
            "no JD provided — pass --jd-file PATH, --jd TEXT, "
            "or pipe the JD into stdin"
        )
    return sys.stdin.read()


def _format_result(result: MatchResult, verbose: bool) -> str:
    out: list[str] = []
    head = "PASS" if result.passed else "FAIL"
    out.append(f"[{head}] profile: {result.profile_name}")
    for err in result.errors:
        out.append(f"  ! {err}")
    for rr in result.per_rule:
        mark = "PASS" if rr.passed else "FAIL"
        line = f"  [{mark}] rule[{rr.rule_index}] {rr.rule_type}"
        if rr.description:
            line += f" — {rr.description}"
        out.append(line)
        if rr.error:
            out.append(f"      ! error: {rr.error}")
        if rr.rule_type == "scored":
            out.append(
                f"      total_score={rr.total_score} "
                f"distinct_matches={rr.distinct_match_count}"
            )
            for key, op, num, ok in rr.threshold_results:
                out.append(
                    f"        threshold {key}{op}{num}: "
                    f"{'ok' if ok else 'miss'}"
                )
        if verbose:
            if not rr.groups:
                out.append("      (no group references in this rule)")
            for ref in sorted(rr.groups):
                gm = rr.groups[ref]
                if gm.matched:
                    aliases = ", ".join(repr(a) for a in gm.matched_aliases)
                    out.append(f"      + {ref}  via: {aliases}")
                else:
                    out.append(f"      - {ref}  no alias matched in JD")
    return "\n".join(out)


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Match a profile's matching_rules against a job description. "
            "Exit 0 if matched, 1 if not, 2 on usage/IO errors."
        )
    )
    parser.add_argument(
        "profile_name",
        nargs="?",
        help="profile_name from profiles.yaml (omit with --all)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="evaluate ALL profiles (regardless of `enable`) against the JD",
    )
    parser.add_argument(
        "--jd-file",
        dest="jd_file",
        help="path to a text file containing the JD",
    )
    parser.add_argument(
        "--jd",
        help="JD text inline (use for short snippets only)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also print per-group match details under each rule",
    )
    args = parser.parse_args()

    try:
        jd_text = _read_jd_arg(args)
        skills_root, roles_root, locations_root = load_alias_files()
    except MatcherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.all:
        try:
            profiles = load_all_profiles()
        except MatcherError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        any_failed = False
        for prof in profiles:
            res = match_profile_against_jd(
                prof, jd_text, skills_root, roles_root, locations_root
            )
            print(_format_result(res, args.verbose))
            print()
            if not res.passed:
                any_failed = True
        return 1 if any_failed else 0

    if not args.profile_name:
        print(
            "error: provide a profile_name positional argument, "
            "or use --all to evaluate every profile",
            file=sys.stderr,
        )
        return 2

    try:
        profile = load_profile(args.profile_name)
    except MatcherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    res = match_profile_against_jd(
        profile, jd_text, skills_root, roles_root, locations_root
    )
    print(_format_result(res, args.verbose))
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(_main())
