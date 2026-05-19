"""Exhaustive test suite for verification.match_profile.

Run from the project root:

    pytest verification/test_match_profile.py -v
    pytest verification/test_match_profile.py -v -k boolean
    pytest verification/test_match_profile.py --tb=short

Coverage targets every public entry point AND every private helper that
participates in matching correctness:

  * JD normalization (`normalize_jd`)
  * Alias → regex compilation (`_alias_to_pattern`) — every word-boundary
    edge case the project explicitly calls out (c++, c#, ml/html, js/jsx,
    multi-word with collapsed whitespace, regex-special characters).
  * Pattern cache (`clear_pattern_cache`).
  * Ref → alias-list resolution (`_resolve_ref_to_aliases`) — malformed
    refs, unknown tops/categories/canonicals, non-list payloads, strip
    behavior, non-string entries.
  * Group scanning (`_scan_group_in_jd`) — multi-alias hits, span output,
    no-match case.
  * Boolean DSL — tokenizer (unknown tokens, empty input), parser
    (precedence, parens, unbalanced, trailing tokens, unknown refs,
    short-circuit-but-eager-resolution semantics).
  * Scored DSL — weight pairs, multiple thresholds, unknown threshold
    keys, distinct_matches with repeated refs, no-threshold error,
    negative weights, `>=` / `<=` semantics, scored-with-no-pairs.
  * YAML loaders (`_load_yaml`, `load_alias_files`, `load_profile`,
    `load_all_profiles`) — missing files, malformed structure, missing
    profile, empty file.
  * `match_profile_against_jd` — end-to-end behavior, malformed profile
    shapes, bad rule shapes, unresolved refs become per-rule errors (not
    silent False), per-match ref caching, AND-across-rules semantics.
  * CLI surface (`_read_jd_arg`, `_format_result`, `_main`) — every flag
    combination, stdin tty detection, file-not-found, --all, exit codes
    0/1/2.
  * Integration smoke tests against the REAL profiles.yaml /
    skill_aliases.yml / location_aliases.yml — guard against regressions
    in the live config and the matcher together.

Security note: this test file only constructs strings and dicts inline
and reads files via the project-internal path constants. No user input
ever reaches eval/exec/subprocess. Subprocess is not used at all.
"""

from __future__ import annotations

import io
import sys
import textwrap
from pathlib import Path
from typing import Callable

import pytest

# Make the project root importable so `from verification import ...` works
# regardless of pytest's cwd. Mirrors the convention used by every script
# inside the verification package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from verification import match_profile as mp  # noqa: E402
from verification.match_profile import (  # noqa: E402
    GroupMatch,
    MatchResult,
    MatcherError,
    RuleResult,
    _alias_to_pattern,
    _BoolParser,
    _eval_boolean,
    _eval_scored,
    _resolve_ref_to_aliases,
    _scan_group_in_jd,
    _tokenize_boolean,
    clear_pattern_cache,
    load_alias_files,
    load_all_profiles,
    load_profile,
    match_profile_against_jd,
    normalize_jd,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_pattern_cache():
    """Pattern cache is module-global; reset between tests so a test
    that mutates an alias string can't poison neighbors."""
    clear_pattern_cache()
    yield
    clear_pattern_cache()


@pytest.fixture
def skills_root() -> dict:
    """Hand-rolled skills tree mirroring real-world shape but smaller.

    The contents are chosen to exercise every regex edge case the matcher
    promises to handle (c++, c#, multi-word, two-letter abbrev, digits).
    """
    return {
        "languages": {
            "python": ["python", "python3", "py"],
            "go": ["golang", "go programming", "go lang"],
            "java": ["java", "jdk", "jvm", "java 17"],
            "cpp": ["c++", "cpp", "cplusplus", "c plus plus"],
            "c_sharp": ["c#", "csharp"],
            "javascript": ["javascript", "js", "ecmascript"],
            "typescript": ["typescript", "ts"],
        },
        "concepts": {
            "distributed_systems": ["distributed systems", "distsys"],
            "microservices": ["microservices", "micro services"],
            "machine_learning": ["machine learning", "ml"],
        },
        "messaging": {
            "kafka": ["kafka", "apache kafka"],
            "grpc": ["grpc", "g rpc"],
        },
        "cloud_providers": {
            "aws": ["aws", "amazon web services"],
            "gcp": ["gcp", "google cloud"],
        },
        "aws_services": {
            "s3": ["s3", "amazon s3", "aws s3"],
        },
        "frameworks": {
            # contains regex-special chars to make sure escape works.
            "nextjs": ["next.js", "nextjs"],
        },
    }


@pytest.fixture
def roles_root() -> dict:
    return {
        "role_families": {
            "backend_engineer": [
                "backend engineer",
                "back-end engineer",
                "backend developer",
            ],
            "frontend_engineer": ["frontend engineer", "front-end engineer"],
        },
        "seniority_modifiers": {
            "intern": ["intern", "internship"],
            "new_grad": ["new grad", "new graduate", "early career"],
        },
        "generic_swe": {
            "software_engineer": [
                "software engineer",
                "swe",
                "software developer",
            ],
        },
    }


@pytest.fixture
def locations_root() -> dict:
    return {
        "india": {
            "bengaluru": ["bengaluru", "bangalore", "blr"],
        },
        "us_california": {
            "san_francisco_bay_area": ["san francisco", "sf", "bay area"],
        },
        "work_modes": {
            "remote": ["remote", "work from home", "wfh"],
            "hybrid": ["hybrid"],
        },
    }


@pytest.fixture
def matched_resolver() -> Callable[[str], GroupMatch]:
    """A resolver that returns a matched GroupMatch for any ref ending in
    `.matched`, and an un-matched one otherwise. Lets the boolean/scored
    DSL tests stay completely independent of the JD scanner."""

    def _resolver(ref: str) -> GroupMatch:
        if ref.endswith(".matched"):
            return GroupMatch(
                ref=ref, matched=True, matched_aliases=["x"],
                matched_spans=[(0, 1, "x")],
            )
        if ref.endswith(".error"):
            raise MatcherError(f"unresolved group reference: {ref}")
        return GroupMatch(ref=ref, matched=False)

    return _resolver


# ─────────────────────────────────────────────────────────────────────────────
# normalize_jd
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalizeJD:
    def test_lowercases(self):
        assert normalize_jd("Hello WORLD") == "hello world"

    def test_collapses_all_whitespace_runs_to_single_space(self):
        # tabs, newlines, multiple spaces — all collapse identically.
        raw = "alpha\t\tbeta\n\n\ngamma    delta\r\nepsilon"
        assert normalize_jd(raw) == "alpha beta gamma delta epsilon"

    def test_preserves_punctuation(self):
        # The docstring is explicit: punctuation MUST survive normalization
        # because aliases like c++, pl/sql, next.js depend on it.
        out = normalize_jd("We use C++ and PL/SQL and Next.JS!")
        assert out == "we use c++ and pl/sql and next.js!"

    def test_empty_string(self):
        assert normalize_jd("") == ""

    def test_whitespace_only(self):
        # A pure-whitespace input collapses to a single space — the function
        # does not strip. Documented behavior: it does exactly `re.sub` +
        # `lower`, nothing more.
        assert normalize_jd("   \t \n  ") == " "

    def test_unicode_is_preserved(self):
        out = normalize_jd("Café Münchën")
        assert out == "café münchën"

    @pytest.mark.parametrize("bad", [None, 123, 4.5, ["a", "b"], {"k": "v"}])
    def test_non_string_raises(self, bad):
        with pytest.raises(MatcherError, match="must be a string"):
            normalize_jd(bad)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# _alias_to_pattern (the security-critical word-boundary regex)
# ─────────────────────────────────────────────────────────────────────────────


class TestAliasToPattern:
    def _hits(self, alias: str, text: str) -> bool:
        return _alias_to_pattern(alias).search(normalize_jd(text)) is not None

    # ── happy path ────────────────────────────────────────────────────────
    @pytest.mark.parametrize(
        ("alias", "text"),
        [
            ("python", "we use python heavily"),
            ("python", "Python is great"),
            ("kafka", "stream events via kafka"),
            ("aws", "AWS for everything"),
            ("ml", "ml engineer wanted"),
            ("js", "looking for js skills"),
            ("ts", "ts is required"),
        ],
    )
    def test_basic_hits(self, alias, text):
        assert self._hits(alias, text)

    # ── word boundary: must NOT match inside another word ────────────────
    @pytest.mark.parametrize(
        ("alias", "text"),
        [
            ("ml", "html5 is required"),       # `ml` inside `html`
            ("ml", "mlops platform"),           # `ml` inside `mlops`
            ("js", "the jsx framework"),        # `js` inside `jsx`
            ("aws", "awsome work"),             # `aws` inside `awsome`
            ("kafka", "kafkaesque process"),    # `kafka` inside `kafkaesque`
            ("python", "monty pythons"),        # trailing `s` blocks match
            ("python", "cpython3"),             # leading char blocks match
        ],
    )
    def test_word_boundary_rejects_substring(self, alias, text):
        assert not self._hits(alias, text)

    # ── plus/hash treated as in-word (the bespoke part of the boundary) ──
    @pytest.mark.parametrize(
        ("alias", "text", "expected"),
        [
            ("c++", "we use c++ heavily",   True),
            ("c++", "C++ everywhere",       True),
            ("c++", "cc++ legacy",          False),  # left char `c` is word-like
            ("c++", "c+++ extension",       False),  # right char `+` is word-like
            ("c#",  "c# 7.0 stack",         True),
            ("c#",  "C# is required",       True),
            ("c#",  "c#5 build",            False),  # right char `5` is word-like
            ("c#",  "ac# language",         False),  # left char `a` is word-like
        ],
    )
    def test_plus_hash_word_boundary(self, alias, text, expected):
        assert self._hits(alias, text) is expected

    # ── multi-word aliases tolerate arbitrary whitespace inside the JD ───
    @pytest.mark.parametrize(
        "text",
        [
            "go programming basics",
            "go  programming basics",       # double space
            "go\tprogramming basics",       # tab
            "GO\nPROGRAMMING basics",       # newline
        ],
    )
    def test_multi_word_alias_tolerates_internal_whitespace(self, text):
        # `normalize_jd` collapses to single spaces; the `\s+` in the pattern
        # is still belt-and-suspenders for pre-normalized buffers.
        assert _alias_to_pattern("go programming").search(
            normalize_jd(text)
        ) is not None

    # ── regex special characters in the alias are escaped, not interpreted ─
    def test_regex_metacharacters_are_literal(self):
        # If escaping is broken, `next.js` would match `next-js` too.
        assert self._hits("next.js", "use Next.js for SSR")
        assert not self._hits("next.js", "we built next-js shim")

    # ── digits in the alias ──────────────────────────────────────────────
    @pytest.mark.parametrize(
        ("alias", "text", "expected"),
        [
            ("python3",  "python3 backend",     True),
            ("python3",  "Python3.11 runtime",  True),   # `.` is not word-like
            ("python3",  "python34 build",      False),  # `4` is word-like
            ("java 17",  "Java 17 required",    True),
            ("java 17",  "java 17.0.2 lts",     True),
            ("java 17",  "java 170 build",      False),
        ],
    )
    def test_digit_boundaries(self, alias, text, expected):
        assert self._hits(alias, text) is expected

    # ── whitespace tolerance on the alias side itself ────────────────────
    def test_alias_with_leading_trailing_whitespace_is_stripped(self):
        # Defense in depth — YAML strings can carry stray spaces.
        assert _alias_to_pattern("   golang   ").search("golang rocks")

    def test_alias_is_lowercased(self):
        # Aliases are stored lowercase but defensiveness is documented.
        assert _alias_to_pattern("KAFKA").search(normalize_jd("Kafka"))

    # ── empty alias is rejected (would otherwise match everywhere) ───────
    @pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
    def test_empty_alias_raises(self, bad):
        with pytest.raises(MatcherError, match="empty alias"):
            _alias_to_pattern(bad)


# ─────────────────────────────────────────────────────────────────────────────
# Pattern cache
# ─────────────────────────────────────────────────────────────────────────────


class TestPatternCache:
    def test_returns_same_compiled_pattern_for_same_alias(self):
        a = _alias_to_pattern("kafka")
        b = _alias_to_pattern("kafka")
        # `re.compile` returns a new object each time, so identity here
        # proves the cache is actually being used.
        assert a is b

    def test_clear_pattern_cache_drops_entries(self):
        _alias_to_pattern("kafka")
        assert "kafka" in mp._PATTERN_CACHE
        clear_pattern_cache()
        assert "kafka" not in mp._PATTERN_CACHE


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_ref_to_aliases
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveRefToAliases:
    def test_resolves_skills(self, skills_root, roles_root, locations_root):
        out = _resolve_ref_to_aliases(
            "skills.languages.python", skills_root, roles_root, locations_root
        )
        assert out == ["python", "python3", "py"]

    def test_resolves_roles(self, skills_root, roles_root, locations_root):
        out = _resolve_ref_to_aliases(
            "roles.seniority_modifiers.intern",
            skills_root, roles_root, locations_root,
        )
        assert out == ["intern", "internship"]

    def test_resolves_locations(
        self, skills_root, roles_root, locations_root
    ):
        out = _resolve_ref_to_aliases(
            "locations.work_modes.remote",
            skills_root, roles_root, locations_root,
        )
        assert out == ["remote", "work from home", "wfh"]

    @pytest.mark.parametrize(
        "bad_ref",
        [
            "skills.languages",           # 2 parts
            "skills",                     # 1 part
            "skills.languages.python.x",  # 4 parts
            "",
        ],
    )
    def test_malformed_ref_returns_none(
        self, bad_ref, skills_root, roles_root, locations_root
    ):
        assert _resolve_ref_to_aliases(
            bad_ref, skills_root, roles_root, locations_root
        ) is None

    def test_unknown_top_returns_none(
        self, skills_root, roles_root, locations_root
    ):
        # `foo.bar.baz` — `foo` is not skills/roles/locations.
        assert _resolve_ref_to_aliases(
            "foo.bar.baz", skills_root, roles_root, locations_root
        ) is None

    def test_unknown_category_returns_none(
        self, skills_root, roles_root, locations_root
    ):
        assert _resolve_ref_to_aliases(
            "skills.no_such_category.python",
            skills_root, roles_root, locations_root,
        ) is None

    def test_unknown_canonical_returns_none(
        self, skills_root, roles_root, locations_root
    ):
        assert _resolve_ref_to_aliases(
            "skills.languages.cobol",
            skills_root, roles_root, locations_root,
        ) is None

    def test_non_list_aliases_returns_none(self, roles_root, locations_root):
        # Caller fed a dict where the value should be a list.
        skills_root = {"languages": {"python": {"not": "a list"}}}
        assert _resolve_ref_to_aliases(
            "skills.languages.python",
            skills_root, roles_root, locations_root,
        ) is None

    def test_non_string_entries_filtered_out(
        self, roles_root, locations_root
    ):
        skills_root = {
            "languages": {
                "python": ["python", None, 42, "py", "  ", "python3"],
            }
        }
        out = _resolve_ref_to_aliases(
            "skills.languages.python",
            skills_root, roles_root, locations_root,
        )
        # `None`, `42`, and the blank string are dropped.
        assert out == ["python", "py", "python3"]

    def test_non_dict_root_returns_none(self):
        # If a root is itself missing or wrong-typed, every lookup fails
        # gracefully — caller turns this into MatcherError.
        assert _resolve_ref_to_aliases(
            "skills.languages.python", None, {}, {}  # type: ignore[arg-type]
        ) is None


# ─────────────────────────────────────────────────────────────────────────────
# _scan_group_in_jd
# ─────────────────────────────────────────────────────────────────────────────


class TestScanGroupInJD:
    def test_returns_all_matched_aliases(self):
        out = _scan_group_in_jd(
            "skills.languages.python",
            ["python", "python3", "java"],
            normalize_jd("We need python and python3 experience"),
        )
        assert out.matched is True
        assert out.matched_aliases == ["python", "python3"]
        # Two spans, one per matched alias.
        assert len(out.matched_spans) == 2
        for start, end, text in out.matched_spans:
            assert end > start
            assert text  # never empty

    def test_returns_no_matches_cleanly(self):
        out = _scan_group_in_jd(
            "skills.languages.python",
            ["python", "py"],
            normalize_jd("We use rust here"),
        )
        assert out.matched is False
        assert out.matched_aliases == []
        assert out.matched_spans == []

    def test_empty_alias_list_returns_no_match(self):
        out = _scan_group_in_jd(
            "skills.languages.unknown", [], normalize_jd("anything")
        )
        assert out.matched is False
        assert out.matched_aliases == []


# ─────────────────────────────────────────────────────────────────────────────
# Boolean DSL — tokenizer
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenizeBoolean:
    def test_basic_tokens(self):
        toks = _tokenize_boolean(
            "skills.languages.python and roles.role_families.backend_engineer"
        )
        assert toks == [
            "skills.languages.python",
            "and",
            "roles.role_families.backend_engineer",
        ]

    def test_parens_and_not(self):
        toks = _tokenize_boolean(
            "( skills.languages.python or not skills.languages.java )"
        )
        assert toks == [
            "(", "skills.languages.python", "or", "not",
            "skills.languages.java", ")",
        ]

    @pytest.mark.parametrize(
        "expr",
        [
            "skills.languages.python & skills.languages.java",  # bad operator
            "skills.languages.python xor skills.languages.java",  # bad keyword
            "skills.LANGUAGES.python",  # upper-case category not allowed
            "skills..python",           # missing category
            "@@@",                       # gibberish
        ],
    )
    def test_unexpected_token_raises(self, expr):
        with pytest.raises(MatcherError, match="unexpected token"):
            _tokenize_boolean(expr)

    @pytest.mark.parametrize("expr", ["", "   ", "\n\t  "])
    def test_empty_after_tokenize_raises(self, expr):
        with pytest.raises(MatcherError, match="empty after tokenization"):
            _tokenize_boolean(expr)


# ─────────────────────────────────────────────────────────────────────────────
# Boolean DSL — parser & evaluator
# ─────────────────────────────────────────────────────────────────────────────


def _make_values(true_refs: set[str], all_refs: set[str]) -> dict[str, bool]:
    return {r: (r in true_refs) for r in all_refs}


class TestBoolParser:
    def _eval(self, expr: str, true_refs: set[str]) -> bool:
        toks = _tokenize_boolean(expr)
        all_refs = {t for t in toks if "." in t}
        return _BoolParser(toks, _make_values(true_refs, all_refs)).parse()

    # ── single ref ───────────────────────────────────────────────────────
    def test_single_ref_true(self):
        assert self._eval("skills.languages.python", {"skills.languages.python"})

    def test_single_ref_false(self):
        assert not self._eval("skills.languages.python", set())

    # ── or / and / not ───────────────────────────────────────────────────
    def test_or_short_circuit_value(self):
        assert self._eval(
            "skills.languages.python or skills.languages.java",
            {"skills.languages.python"},
        )

    def test_or_both_false(self):
        assert not self._eval(
            "skills.languages.python or skills.languages.java", set()
        )

    def test_and_one_false(self):
        assert not self._eval(
            "skills.languages.python and skills.languages.java",
            {"skills.languages.python"},
        )

    def test_and_both_true(self):
        assert self._eval(
            "skills.languages.python and skills.languages.java",
            {"skills.languages.python", "skills.languages.java"},
        )

    def test_not_inverts(self):
        assert self._eval("not skills.languages.python", set())
        assert not self._eval(
            "not skills.languages.python", {"skills.languages.python"}
        )

    def test_double_not(self):
        # `not not X` ≡ X — exercises the recursive _not_expr path.
        assert self._eval(
            "not not skills.languages.python", {"skills.languages.python"}
        )

    # ── precedence: not > and > or ───────────────────────────────────────
    def test_precedence_not_binds_tighter_than_and(self):
        # `not A and B`  ≡  `(not A) and B`
        # With A=False, B=True → (not False) and True = True
        assert self._eval(
            "not skills.languages.python and skills.languages.java",
            {"skills.languages.java"},
        )
        # With A=True, B=True → (not True) and True = False
        assert not self._eval(
            "not skills.languages.python and skills.languages.java",
            {"skills.languages.python", "skills.languages.java"},
        )

    def test_precedence_and_binds_tighter_than_or(self):
        # `A or B and C` ≡ `A or (B and C)`
        # A=False, B=True, C=False → False or (True and False) = False
        assert not self._eval(
            "skills.languages.python or skills.languages.java "
            "and skills.languages.go",
            {"skills.languages.java"},
        )
        # A=True, B=False, C=False → True or anything = True
        assert self._eval(
            "skills.languages.python or skills.languages.java "
            "and skills.languages.go",
            {"skills.languages.python"},
        )

    def test_parentheses_override_precedence(self):
        # `(A or B) and C` — A=True, B=False, C=False → True and False = False
        assert not self._eval(
            "(skills.languages.python or skills.languages.java) "
            "and skills.languages.go",
            {"skills.languages.python"},
        )
        # Same shape, with C=True → True
        assert self._eval(
            "(skills.languages.python or skills.languages.java) "
            "and skills.languages.go",
            {"skills.languages.python", "skills.languages.go"},
        )

    def test_nested_parens(self):
        assert self._eval(
            "((skills.languages.python or skills.languages.java) "
            "and not skills.languages.go)",
            {"skills.languages.python"},
        )

    # ── errors ───────────────────────────────────────────────────────────
    def test_missing_closing_paren(self):
        with pytest.raises(MatcherError, match="missing closing parenthesis"):
            self._eval("(skills.languages.python", set())

    def test_unexpected_trailing_token(self):
        # Two refs with no operator between them.
        toks = ["skills.languages.python", "skills.languages.java"]
        values = {"skills.languages.python": True, "skills.languages.java": True}
        with pytest.raises(MatcherError, match="unexpected trailing token"):
            _BoolParser(toks, values).parse()

    def test_unexpected_close_paren(self):
        # ')' shows up where an atom was expected.
        toks = ["(", ")"]
        with pytest.raises(MatcherError, match="unexpected token in atom"):
            _BoolParser(toks, {}).parse()

    def test_unknown_reference_raises(self):
        # Ref appears in tokens but not in values dict.
        toks = ["skills.languages.python"]
        with pytest.raises(MatcherError, match="unknown reference"):
            _BoolParser(toks, {}).parse()

    def test_unexpected_end_of_expression(self):
        toks = ["not"]
        with pytest.raises(MatcherError, match="unexpected end"):
            _BoolParser(toks, {}).parse()


class TestEvalBoolean:
    def test_resolves_each_unique_ref_once(self, matched_resolver):
        calls: list[str] = []

        def counting(ref: str) -> GroupMatch:
            calls.append(ref)
            return matched_resolver(ref)

        result, groups = _eval_boolean(
            "skills.a.matched and skills.a.matched and skills.b.matched",
            counting,
        )
        assert result is True
        # `skills.a.matched` appears twice but should be resolved once.
        assert calls.count("skills.a.matched") == 1
        assert set(groups) == {"skills.a.matched", "skills.b.matched"}

    def test_eager_resolution_surfaces_unresolved_ref(self, matched_resolver):
        # Even though `a.matched or anything` would be True via short-circuit,
        # resolution is eager — unresolved refs MUST fail loudly. This is the
        # exact behavior documented in match_profile.py's docstring.
        with pytest.raises(MatcherError, match="unresolved group reference"):
            _eval_boolean(
                "skills.a.matched or skills.b.error", matched_resolver
            )


# ─────────────────────────────────────────────────────────────────────────────
# Scored DSL
# ─────────────────────────────────────────────────────────────────────────────


class TestEvalScored:
    def test_basic_totalscore_pass(self, matched_resolver):
        passed, groups, total, distinct, thresholds = _eval_scored(
            "skills.a.matched=3, skills.b.matched=2, totalscore>=4",
            matched_resolver,
        )
        assert passed is True
        assert total == 5.0
        assert distinct == 2
        assert thresholds == [("totalscore", ">=", 4.0, True)]
        assert set(groups) == {"skills.a.matched", "skills.b.matched"}

    def test_totalscore_fail(self, matched_resolver):
        passed, _, total, _, thresholds = _eval_scored(
            "skills.a.matched=1, skills.b.unmatched=10, totalscore>=5",
            matched_resolver,
        )
        assert passed is False
        # b doesn't end with `.matched`, so it's NOT matched → only `a`'s 1.
        assert total == 1.0
        assert thresholds == [("totalscore", ">=", 5.0, False)]

    def test_distinct_matches_with_repeated_refs(self, matched_resolver):
        # Same ref repeated in the expression — `total` includes both weights
        # (3+2=5 if matched) but `distinct` counts unique refs only.
        passed, _, total, distinct, _ = _eval_scored(
            "skills.a.matched=3, skills.a.matched=2, totalscore>=5, "
            "distinct_matches>=1",
            matched_resolver,
        )
        assert passed is True
        assert total == 5.0
        assert distinct == 1

    def test_distinct_matches_le_threshold(self, matched_resolver):
        # `<=` semantics. Two distinct matches but rule wants ≤1 → fail.
        passed, _, _, distinct, thresholds = _eval_scored(
            "skills.a.matched=1, skills.b.matched=1, totalscore>=0, "
            "distinct_matches<=1",
            matched_resolver,
        )
        assert passed is False
        assert distinct == 2
        assert ("distinct_matches", "<=", 1.0, False) in thresholds

    def test_totalscore_le_threshold(self, matched_resolver):
        # Quirky but legal: enforce a CEILING via `<=`.
        passed, _, total, _, _ = _eval_scored(
            "skills.a.matched=5, totalscore<=10",
            matched_resolver,
        )
        assert passed is True and total == 5.0

    def test_negative_weights(self, matched_resolver):
        # Negative weights are a valid penalty pattern.
        passed, _, total, _, _ = _eval_scored(
            "skills.a.matched=5, skills.b.matched=-3, totalscore>=1",
            matched_resolver,
        )
        assert passed is True
        assert total == 2.0

    def test_multiple_thresholds_all_must_pass(self, matched_resolver):
        # Both thresholds must hold (AND across threshold clauses).
        passed, _, _, _, results = _eval_scored(
            "skills.a.matched=2, totalscore>=2, distinct_matches>=2",
            matched_resolver,
        )
        # distinct=1, so `distinct_matches>=2` is False → overall False.
        assert passed is False
        assert any(not ok for *_, ok in results)

    def test_unknown_threshold_key_raises(self, matched_resolver):
        with pytest.raises(MatcherError, match="unknown threshold key"):
            _eval_scored(
                "skills.a.matched=1, weirdkey>=1", matched_resolver
            )

    def test_no_threshold_raises(self, matched_resolver):
        with pytest.raises(MatcherError, match="no threshold clause"):
            _eval_scored("skills.a.matched=1", matched_resolver)

    def test_unparseable_clause_raises(self, matched_resolver):
        with pytest.raises(MatcherError, match="could not parse"):
            _eval_scored(
                "skills.a.matched=1, this is junk, totalscore>=1",
                matched_resolver,
            )

    @pytest.mark.parametrize("expr", ["", "  ", ", , ,"])
    def test_empty_expression_raises(self, expr, matched_resolver):
        with pytest.raises(MatcherError, match="scored expression"):
            _eval_scored(expr, matched_resolver)

    def test_scored_with_only_threshold_no_pairs(self, matched_resolver):
        # No pairs at all — total=0, distinct=0. `totalscore>=0` passes.
        passed, groups, total, distinct, _ = _eval_scored(
            "totalscore>=0", matched_resolver
        )
        assert passed is True
        assert total == 0.0
        assert distinct == 0
        assert groups == {}

    def test_scored_resolves_each_ref_once_per_call(self, matched_resolver):
        calls: list[str] = []

        def counting(ref: str) -> GroupMatch:
            calls.append(ref)
            return matched_resolver(ref)

        _eval_scored(
            "skills.a.matched=1, skills.a.matched=2, "
            "skills.b.matched=3, totalscore>=1",
            counting,
        )
        # Each unique ref called once inside scored eval.
        assert sorted(calls) == ["skills.a.matched", "skills.b.matched"]

    def test_float_weights(self, matched_resolver):
        passed, _, total, _, _ = _eval_scored(
            "skills.a.matched=1.5, skills.b.matched=2.25, totalscore>=3.5",
            matched_resolver,
        )
        assert passed is True
        assert total == pytest.approx(3.75)


# ─────────────────────────────────────────────────────────────────────────────
# YAML loaders
# ─────────────────────────────────────────────────────────────────────────────


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


class TestLoadYaml:
    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(MatcherError, match="required file not found"):
            mp._load_yaml(tmp_path / "nope.yml")

    def test_empty_file_returns_empty_dict(self, tmp_path: Path):
        p = tmp_path / "empty.yml"
        p.write_text("", encoding="utf-8")
        assert mp._load_yaml(p) == {}

    def test_parses_valid_yaml(self, tmp_path: Path):
        p = tmp_path / "x.yml"
        _write_yaml(p, "a: 1\nb: [x, y]\n")
        assert mp._load_yaml(p) == {"a": 1, "b": ["x", "y"]}


class TestLoadAliasFiles:
    def test_returns_empty_dicts_when_yaml_is_unstructured(
        self, tmp_path: Path, monkeypatch
    ):
        skills_p = tmp_path / "skills.yml"
        loc_p = tmp_path / "loc.yml"
        # A top-level list instead of a dict — caller-tolerant: zero out.
        _write_yaml(skills_p, "- not a mapping\n")
        _write_yaml(loc_p, "- still not a mapping\n")
        monkeypatch.setattr(mp, "SKILL_ALIASES_PATH", skills_p)
        monkeypatch.setattr(mp, "LOCATION_ALIASES_PATH", loc_p)
        s, r, lroot = load_alias_files()
        assert s == {} and r == {} and lroot == {}

    def test_returns_subtrees(self, tmp_path: Path, monkeypatch):
        skills_p = tmp_path / "skills.yml"
        loc_p = tmp_path / "loc.yml"
        _write_yaml(
            skills_p,
            """
            skills:
              languages:
                python: [python]
            roles:
              role_families:
                backend_engineer: [backend engineer]
            """,
        )
        _write_yaml(
            loc_p,
            """
            locations:
              work_modes:
                remote: [remote]
            """,
        )
        monkeypatch.setattr(mp, "SKILL_ALIASES_PATH", skills_p)
        monkeypatch.setattr(mp, "LOCATION_ALIASES_PATH", loc_p)
        s, r, lroot = load_alias_files()
        assert s == {"languages": {"python": ["python"]}}
        assert r == {"role_families": {"backend_engineer": ["backend engineer"]}}
        assert lroot == {"work_modes": {"remote": ["remote"]}}


class TestLoadProfile:
    def test_loads_by_name(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "profiles.yaml"
        _write_yaml(
            p,
            """
            profiles:
              - profile_name: alpha
                enable: false
                years_of_experience: 1.5
                locations: [locations.work_modes.remote]
                matching_rules:
                  - type: boolean
                    expression: skills.languages.python
              - profile_name: beta
                enable: true
                years_of_experience: 0
                locations: [locations.work_modes.remote]
                matching_rules:
                  - type: boolean
                    expression: skills.languages.java
            """,
        )
        monkeypatch.setattr(mp, "PROFILES_PATH", p)
        prof = load_profile("beta")
        assert prof["profile_name"] == "beta"
        assert prof["enable"] is True

    def test_missing_name_raises(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "profiles.yaml"
        _write_yaml(p, "profiles: []\n")
        monkeypatch.setattr(mp, "PROFILES_PATH", p)
        with pytest.raises(MatcherError, match="not found"):
            load_profile("ghost")

    def test_missing_top_level_profiles_key_raises(
        self, tmp_path: Path, monkeypatch
    ):
        p = tmp_path / "profiles.yaml"
        _write_yaml(p, "not_profiles: []\n")
        monkeypatch.setattr(mp, "PROFILES_PATH", p)
        with pytest.raises(MatcherError, match="missing or non-list"):
            load_profile("anything")

    def test_top_level_is_not_dict_raises(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "profiles.yaml"
        _write_yaml(p, "- a\n- b\n")  # list at top
        monkeypatch.setattr(mp, "PROFILES_PATH", p)
        with pytest.raises(MatcherError, match="missing or non-list"):
            load_profile("x")


class TestLoadAllProfiles:
    def test_returns_only_dict_entries(self, tmp_path: Path, monkeypatch):
        p = tmp_path / "profiles.yaml"
        _write_yaml(
            p,
            """
            profiles:
              - profile_name: a
                enable: false
                years_of_experience: 1
                locations: [locations.work_modes.remote]
                matching_rules: [{type: boolean, expression: skills.languages.python}]
              - "I am a stray string, please ignore me"
              - profile_name: b
                enable: false
                years_of_experience: 2
                locations: [locations.work_modes.remote]
                matching_rules: [{type: boolean, expression: skills.languages.java}]
            """,
        )
        monkeypatch.setattr(mp, "PROFILES_PATH", p)
        profs = load_all_profiles()
        assert [p["profile_name"] for p in profs] == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────────────
# match_profile_against_jd — the public matcher
# ─────────────────────────────────────────────────────────────────────────────


def _profile(rules: list[dict], name: str = "test_profile") -> dict:
    """Minimal valid profile shape for the matcher (no other fields are
    consulted by the matcher itself)."""
    return {"profile_name": name, "matching_rules": rules}


class TestMatchProfileAgainstJD:
    # ── input validation ────────────────────────────────────────────────
    @pytest.mark.parametrize("bad", [None, "x", 1, [1, 2]])
    def test_non_dict_profile_raises(self, bad):
        with pytest.raises(MatcherError, match="profile must be a dict"):
            match_profile_against_jd(bad, "some jd", {}, {}, {})  # type: ignore[arg-type]

    def test_profile_name_must_be_string(
        self, skills_root, roles_root, locations_root
    ):
        with pytest.raises(MatcherError, match="profile_name must be a string"):
            match_profile_against_jd(
                {"profile_name": 123,
                 "matching_rules": [{"type": "boolean",
                                     "expression": "skills.languages.python"}]},
                "python",
                skills_root, roles_root, locations_root,
            )

    def test_missing_matching_rules_returns_failed_result(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            {"profile_name": "x"}, "python",
            skills_root, roles_root, locations_root,
        )
        assert isinstance(res, MatchResult)
        assert res.passed is False
        assert res.per_rule == []
        assert res.errors and "matching_rules" in res.errors[0]

    def test_empty_matching_rules_returns_failed_result(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            {"profile_name": "x", "matching_rules": []}, "python",
            skills_root, roles_root, locations_root,
        )
        assert res.passed is False and res.per_rule == []

    def test_unnamed_profile_falls_back_to_placeholder(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            {"matching_rules": [
                {"type": "boolean", "expression": "skills.languages.python"},
            ]},
            "we use python",
            skills_root, roles_root, locations_root,
        )
        assert res.profile_name == "<unnamed>"
        assert res.passed is True

    # ── rule-shape errors come back as per-rule errors, not exceptions ──
    def test_rule_not_a_dict_fails_only_that_rule(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([
                "i am not a dict",  # bad
                {"type": "boolean", "expression": "skills.languages.python"},
            ]),
            "we love python here",
            skills_root, roles_root, locations_root,
        )
        assert res.passed is False
        assert res.per_rule[0].passed is False
        assert "not a mapping" in (res.per_rule[0].error or "")
        # Second rule still evaluated and passes.
        assert res.per_rule[1].passed is True

    @pytest.mark.parametrize("bad_type", [None, "wrong", 42, ""])
    def test_unknown_rule_type_fails(
        self, bad_type, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([{"type": bad_type, "expression": "x"}]),
            "anything", skills_root, roles_root, locations_root,
        )
        assert res.passed is False
        assert "type" in (res.per_rule[0].error or "")

    @pytest.mark.parametrize("bad_expr", [None, "", "   ", 42])
    def test_empty_or_non_string_expression_fails(
        self, bad_expr, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([{"type": "boolean", "expression": bad_expr}]),
            "anything", skills_root, roles_root, locations_root,
        )
        assert res.passed is False
        assert "expression" in (res.per_rule[0].error or "")

    # ── correct end-to-end behavior ─────────────────────────────────────
    def test_boolean_rule_passes(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([
                {"type": "boolean",
                 "expression": "skills.languages.python or skills.languages.go"},
            ]),
            "we work with golang every day",
            skills_root, roles_root, locations_root,
        )
        assert res.passed is True
        rr = res.per_rule[0]
        assert rr.passed is True
        # Both refs were resolved (eager), but only `go` matched.
        assert rr.groups["skills.languages.go"].matched is True
        assert rr.groups["skills.languages.python"].matched is False

    def test_and_across_rules_one_fail_fails_all(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([
                {"type": "boolean",
                 "expression": "skills.languages.python"},
                {"type": "boolean",
                 "expression": "skills.languages.java"},
            ]),
            "we use python and javascript",
            skills_root, roles_root, locations_root,
        )
        assert res.passed is False
        # First passes; second fails.
        assert res.per_rule[0].passed is True
        assert res.per_rule[1].passed is False

    def test_yaml_block_scalar_newlines_normalized(
        self, skills_root, roles_root, locations_root
    ):
        # Multi-line block scalar expressions are how the real profiles.yaml
        # writes rules. Newlines inside the expression must be collapsed to
        # spaces before parsing — otherwise tokenization would fail on a
        # solitary "\n" between two refs.
        expr = "skills.languages.python\n   or\n   skills.languages.go\n"
        res = match_profile_against_jd(
            _profile([{"type": "boolean", "expression": expr}]),
            "we use python here",
            skills_root, roles_root, locations_root,
        )
        assert res.passed is True

    def test_unresolved_ref_in_boolean_becomes_per_rule_error(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([{
                "type": "boolean",
                "expression": "skills.languages.python "
                              "or skills.languages.unknown_lang",
            }]),
            "python python python",
            skills_root, roles_root, locations_root,
        )
        # Documented behavior: never silently treat unresolved as False.
        assert res.passed is False
        assert "unresolved" in (res.per_rule[0].error or "")

    def test_scored_rule_pass_and_threshold_details(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([{
                "type": "scored",
                "description": "demo",
                "expression": (
                    "skills.concepts.distributed_systems=3,"
                    "skills.messaging.kafka=2,"
                    "skills.languages.python=1,"
                    "totalscore>=5"
                ),
            }]),
            "distributed systems engineer who has run kafka clusters in python",
            skills_root, roles_root, locations_root,
        )
        rr = res.per_rule[0]
        assert rr.passed is True
        assert rr.total_score == 6.0
        assert rr.distinct_match_count == 3
        assert rr.threshold_results == [("totalscore", ">=", 5.0, True)]
        assert res.passed is True

    def test_scored_rule_fail_below_threshold(
        self, skills_root, roles_root, locations_root
    ):
        res = match_profile_against_jd(
            _profile([{
                "type": "scored",
                "expression": (
                    "skills.concepts.distributed_systems=3,"
                    "totalscore>=10"
                ),
            }]),
            "distributed systems team",
            skills_root, roles_root, locations_root,
        )
        rr = res.per_rule[0]
        assert rr.passed is False
        assert rr.total_score == 3.0
        assert res.passed is False

    def test_ref_cache_avoids_double_scan_across_rules(
        self, skills_root, roles_root, locations_root, monkeypatch
    ):
        # If two rules share a ref, the matcher should only scan once. We
        # spy on `_scan_group_in_jd` to verify.
        calls: list[str] = []
        real = mp._scan_group_in_jd

        def spy(ref, aliases, jd_norm):
            calls.append(ref)
            return real(ref, aliases, jd_norm)

        monkeypatch.setattr(mp, "_scan_group_in_jd", spy)

        match_profile_against_jd(
            _profile([
                {"type": "boolean",
                 "expression": "skills.languages.python"},
                {"type": "boolean",
                 "expression": "skills.languages.python "
                               "or skills.languages.java"},
            ]),
            "python and java are both used here",
            skills_root, roles_root, locations_root,
        )
        # python appears in both rules but should be scanned once.
        assert calls.count("skills.languages.python") == 1
        assert calls.count("skills.languages.java") == 1

    def test_loads_alias_files_when_not_provided(
        self, tmp_path: Path, monkeypatch
    ):
        # If callers don't pass alias roots, the matcher loads from disk.
        skills_p = tmp_path / "skills.yml"
        loc_p = tmp_path / "loc.yml"
        _write_yaml(skills_p, "skills:\n  languages:\n    python: [python]\n")
        _write_yaml(loc_p, "locations:\n  work_modes:\n    remote: [remote]\n")
        monkeypatch.setattr(mp, "SKILL_ALIASES_PATH", skills_p)
        monkeypatch.setattr(mp, "LOCATION_ALIASES_PATH", loc_p)
        res = match_profile_against_jd(
            _profile([{"type": "boolean",
                       "expression": "skills.languages.python"}]),
            "python is great",
        )
        assert res.passed is True


# ─────────────────────────────────────────────────────────────────────────────
# CLI surface
# ─────────────────────────────────────────────────────────────────────────────


class _Args:
    """Minimal stand-in for argparse.Namespace for _read_jd_arg tests."""

    def __init__(self, *, jd_file=None, jd=None):
        self.jd_file = jd_file
        self.jd = jd


class TestReadJDArg:
    def test_jd_file_reads_contents(self, tmp_path: Path):
        p = tmp_path / "jd.txt"
        p.write_text("Backend Engineer — Python", encoding="utf-8")
        assert mp._read_jd_arg(_Args(jd_file=str(p))) == \
            "Backend Engineer — Python"

    def test_jd_file_missing_raises(self, tmp_path: Path):
        with pytest.raises(MatcherError, match="JD file not found"):
            mp._read_jd_arg(_Args(jd_file=str(tmp_path / "nope.txt")))

    def test_inline_jd_used_directly(self):
        assert mp._read_jd_arg(_Args(jd=" inline ")) == " inline "

    def test_inline_empty_string_is_returned_as_is(self):
        # `--jd ""` is a deliberate, valid choice — return the empty string
        # rather than falling through to stdin.
        assert mp._read_jd_arg(_Args(jd="")) == ""

    def test_stdin_used_when_no_args_and_not_tty(self, monkeypatch):
        fake = io.StringIO("piped jd text\n")
        fake.isatty = lambda: False  # type: ignore[assignment]
        monkeypatch.setattr(sys, "stdin", fake)
        assert mp._read_jd_arg(_Args()) == "piped jd text\n"

    def test_no_args_and_tty_raises(self, monkeypatch):
        fake = io.StringIO("")
        fake.isatty = lambda: True  # type: ignore[assignment]
        monkeypatch.setattr(sys, "stdin", fake)
        with pytest.raises(MatcherError, match="no JD provided"):
            mp._read_jd_arg(_Args())


class TestFormatResult:
    def _result(self, **kw) -> MatchResult:
        defaults = dict(profile_name="p", passed=True, per_rule=[])
        defaults.update(kw)
        return MatchResult(**defaults)  # type: ignore[arg-type]

    def test_pass_header(self):
        out = mp._format_result(self._result(passed=True), verbose=False)
        assert out.startswith("[PASS] profile: p")

    def test_fail_header_and_top_level_errors(self):
        out = mp._format_result(
            self._result(passed=False, errors=["broken thing"]),
            verbose=False,
        )
        assert "[FAIL] profile: p" in out
        assert "! broken thing" in out

    def test_renders_rule_lines(self):
        rr = RuleResult(
            rule_index=0, rule_type="boolean",
            description="must use python",
            expression="skills.languages.python", passed=True,
        )
        out = mp._format_result(
            self._result(per_rule=[rr]), verbose=False
        )
        assert "[PASS] rule[0] boolean" in out
        assert "must use python" in out

    def test_renders_scored_threshold_lines(self):
        rr = RuleResult(
            rule_index=1, rule_type="scored", description="",
            expression="...", passed=True,
            total_score=4.5, distinct_match_count=2,
            threshold_results=[("totalscore", ">=", 4.0, True)],
        )
        out = mp._format_result(
            self._result(per_rule=[rr]), verbose=False
        )
        assert "total_score=4.5" in out
        assert "distinct_matches=2" in out
        assert "threshold totalscore>=4.0: ok" in out

    def test_verbose_renders_matched_and_unmatched_groups(self):
        rr = RuleResult(
            rule_index=0, rule_type="boolean",
            description="", expression="x or y", passed=True,
            groups={
                "skills.languages.python": GroupMatch(
                    ref="skills.languages.python",
                    matched=True, matched_aliases=["python", "py"],
                ),
                "skills.languages.java": GroupMatch(
                    ref="skills.languages.java", matched=False,
                ),
            },
        )
        out = mp._format_result(self._result(per_rule=[rr]), verbose=True)
        assert "+ skills.languages.python  via: 'python', 'py'" in out
        assert "- skills.languages.java  no alias matched in JD" in out

    def test_verbose_with_no_groups_says_so(self):
        rr = RuleResult(
            rule_index=0, rule_type="boolean", description="",
            expression="not_a_real_expression_just_a_string", passed=False,
            error="bad",
        )
        out = mp._format_result(self._result(per_rule=[rr]), verbose=True)
        assert "(no group references in this rule)" in out
        assert "! error: bad" in out


class TestMainCLI:
    """End-to-end CLI exit-code tests via direct `_main()` invocation.

    Uses the REAL profiles.yaml / alias files so failures surface
    cross-file regressions, not just matcher bugs in isolation.
    """

    def _run(self, monkeypatch, *argv, stdin: str = "") -> int:
        monkeypatch.setattr(sys, "argv", ["match_profile.py", *argv])
        fake = io.StringIO(stdin)
        fake.isatty = lambda: not bool(stdin)  # type: ignore[assignment]
        monkeypatch.setattr(sys, "stdin", fake)
        return mp._main()

    def test_no_args_at_all_returns_2(self, monkeypatch, capsys):
        # Neither profile_name nor --all → usage error → exit 2.
        # stdin is set as non-tty with empty content so _read_jd_arg
        # doesn't bail first.
        rc = self._run(monkeypatch, stdin="some jd")
        err = capsys.readouterr().err
        assert rc == 2
        assert "profile_name" in err or "--all" in err

    def test_bad_jd_file_returns_2(self, monkeypatch, capsys, tmp_path):
        rc = self._run(
            monkeypatch, "yaswanth_backend_distsys",
            "--jd-file", str(tmp_path / "does_not_exist.txt"),
        )
        assert rc == 2
        assert "JD file not found" in capsys.readouterr().err

    def test_unknown_profile_returns_2(self, monkeypatch, capsys):
        rc = self._run(
            monkeypatch, "no_such_profile_anywhere",
            "--jd", "anything",
        )
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_inline_jd_matching_profile_returns_0(self, monkeypatch, capsys):
        # Craft a JD that satisfies every rule of the real
        # `yaswanth_backend_distsys` profile.
        jd = (
            "Senior Backend Engineer — distributed systems\n"
            "We are hiring a backend engineer (no interns, no new grads) to "
            "work on distributed systems and microservices on AWS using "
            "python and kafka and grpc. Experience with distributed tracing "
            "and Lambda/S3/Redis a plus."
        )
        rc = self._run(
            monkeypatch, "yaswanth_backend_distsys", "--jd", jd,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "[PASS] profile: yaswanth_backend_distsys" in out

    def test_inline_jd_failing_profile_returns_1(self, monkeypatch, capsys):
        # Misses the language requirement AND cloud requirement.
        jd = "Frontend designer working in CSS only. No cloud."
        rc = self._run(
            monkeypatch, "yaswanth_backend_distsys", "--jd", jd,
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "[FAIL] profile: yaswanth_backend_distsys" in out

    def test_all_runs_every_profile_and_exits_1_on_any_fail(
        self, monkeypatch, capsys
    ):
        # An obviously-irrelevant JD will fail every real profile.
        rc = self._run(monkeypatch, "--all", "--jd", "we sell shoes online")
        out = capsys.readouterr().out
        assert rc == 1
        assert "yaswanth_backend_distsys" in out
        assert "yaswanth_fullstack_broad" in out

    def test_verbose_prints_group_details(self, monkeypatch, capsys):
        rc = self._run(
            monkeypatch, "yaswanth_backend_distsys",
            "--jd", "frontend designer", "-v",
        )
        out = capsys.readouterr().out
        # Verbose mode shows which refs matched / didn't.
        assert "no alias matched in JD" in out
        # And of course this JD doesn't match this profile.
        assert rc == 1

    def test_jd_via_file(self, monkeypatch, capsys, tmp_path):
        jdf = tmp_path / "jd.txt"
        jdf.write_text(
            "Backend Engineer in python with aws and kafka and "
            "distributed systems. No interns, no new grads.",
            encoding="utf-8",
        )
        rc = self._run(
            monkeypatch, "yaswanth_backend_distsys",
            "--jd-file", str(jdf),
        )
        assert rc == 0
        assert "[PASS]" in capsys.readouterr().out

    def test_jd_via_stdin(self, monkeypatch, capsys):
        rc = self._run(
            monkeypatch,
            "yaswanth_backend_distsys",
            stdin=(
                "Backend Engineer working with python on AWS, kafka, "
                "distributed systems."
            ),
        )
        assert rc == 0
        assert "[PASS]" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# Integration: real profiles + real alias files.
#
# These tests are deliberately tight against actual config so a typo in
# profiles.yaml or skill_aliases.yml that slips past validate_profiles.py
# (because it's semantic, not structural) still gets caught here.
# ─────────────────────────────────────────────────────────────────────────────


class TestIntegrationRealProfiles:
    @pytest.fixture(scope="class")
    def alias_roots(self):
        return load_alias_files()

    def test_real_alias_files_have_content(self, alias_roots):
        skills, roles, locations = alias_roots
        assert skills and roles and locations, (
            "Real alias YAML files appear empty — check that they exist and "
            "have the expected top-level keys."
        )

    def test_yaswanth_backend_distsys_loads_and_matches_realistic_jd(
        self, alias_roots
    ):
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = textwrap.dedent(
            """
            Senior Backend Engineer — Distributed Systems

            We're hiring a backend engineer to build distributed systems on
            AWS. You will work with python, kafka, grpc, microservices, and
            distributed tracing (no interns, no new grads). Bonus points for
            experience with Redis, DynamoDB, Lambda, and S3.

            Location: Bengaluru (hybrid).
            """
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        assert res.passed is True, (
            "Realistic backend distsys JD should match the profile end-to-end. "
            "Per-rule status:\n"
            + "\n".join(
                f"  rule[{rr.rule_index}] {rr.rule_type} "
                f"passed={rr.passed} error={rr.error!r}"
                for rr in res.per_rule
            )
        )

    def test_yaswanth_backend_distsys_rejects_intern_jd(self, alias_roots):
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = (
            "Backend Engineering Intern — Distributed Systems (python, kafka, "
            "AWS). Summer internship program."
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        # Specifically the intern rule must fail.
        intern_rule = next(
            (rr for rr in res.per_rule
             if "intern" in rr.description.lower()),
            None,
        )
        assert intern_rule is not None
        assert intern_rule.passed is False
        assert res.passed is False

    def test_yaswanth_backend_distsys_rejects_frontend_only_jd(
        self, alias_roots
    ):
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = (
            "Senior Frontend Designer — CSS/HTML only. No backend, no cloud, "
            "no databases. Internship-free, full-time."
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        assert res.passed is False

    def test_yaswanth_fullstack_broad_matches_lighter_jd(self, alias_roots):
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_fullstack_broad")
        # Only needs an SWE-ish role, NOT a frontend/security/data role, AND
        # one core skill. A plain "software engineer using java" should pass.
        jd = "Software Engineer working in Java on internal tools. Full-time."
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        assert res.passed is True

    def test_no_jd_text_fails_gracefully(self, alias_roots):
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_fullstack_broad")
        res = match_profile_against_jd(prof, "", skills, roles, locations)
        # Empty JD can't match any alias → at least one rule fails →
        # overall False, but the matcher does NOT raise.
        assert res.passed is False
        assert all(rr.error is None for rr in res.per_rule)

    def test_word_boundary_does_not_let_html_match_ml(self, alias_roots):
        # If word boundaries ever regress, a JD that only mentions HTML
        # would suddenly start "matching" `skills.ml_concepts.machine_learning`
        # (because `ml` lives inside `html`). Guard against that here using
        # the REAL skills YAML so we catch both matcher and alias-file regressions.
        skills, _roles, _locations = alias_roots
        ml_category = skills.get("ml_concepts", {})
        aliases = ml_category.get("machine_learning")
        assert isinstance(aliases, list) and "ml" in aliases, (
            "Expected `skills.ml_concepts.machine_learning` to include the "
            "`ml` alias — if this changed, update the test and confirm the "
            "rest of skill_aliases.yml is consistent."
        )
        gm = _scan_group_in_jd(
            "skills.ml_concepts.machine_learning",
            aliases,
            normalize_jd("Frontend role: html5 and css only. mlops? html."),
        )
        assert gm.matched is False, (
            f"`ml` falsely matched in HTML/CSS-only JD; "
            f"aliases triggered: {gm.matched_aliases}"
        )

    def test_word_boundary_allows_standalone_ml_in_real_aliases(
        self, alias_roots
    ):
        # Inverse of the test above: a JD with a bare `ml` token MUST match.
        skills, _roles, _locations = alias_roots
        aliases = skills.get("ml_concepts", {}).get("machine_learning")
        assert isinstance(aliases, list) and aliases
        gm = _scan_group_in_jd(
            "skills.ml_concepts.machine_learning",
            aliases,
            normalize_jd("Hiring an ML engineer to build ranking models."),
        )
        assert gm.matched is True
        assert "ml" in gm.matched_aliases

    def test_real_profile_caching_does_not_re_scan_shared_refs(
        self, alias_roots, monkeypatch
    ):
        # Real-config sanity: scanning the same ref across rules of the same
        # profile must hit the per-match cache. Specifically the python-
        # language ref appears nowhere shared in this profile, but the
        # `_resolve_ref_to_aliases` cache should still be deduped across
        # rule evaluations. Drive a real profile and confirm no ref is
        # scanned twice.
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        calls: list[str] = []
        real = mp._scan_group_in_jd

        def spy(ref, aliases, jd_norm):
            calls.append(ref)
            return real(ref, aliases, jd_norm)

        monkeypatch.setattr(mp, "_scan_group_in_jd", spy)
        match_profile_against_jd(
            prof,
            "Backend Engineer python aws kafka distributed systems.",
            skills, roles, locations,
        )
        # Every ref appears at most once in the scan log.
        assert len(calls) == len(set(calls)), (
            f"ref scanned more than once — cache regression. calls={calls}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end scenario suite: real profiles × hand-crafted JDs.
#
# Each scenario is a (label, JD-text, expected verdict) triple. We run the
# matcher and then assert:
#   * the overall pass/fail matches expectation, and
#   * for FAIL scenarios, the specific rule(s) we *expect* to fail are the
#     ones that actually failed — so a fail for the wrong reason still
#     gets caught.
#
# Each FAIL scenario is deliberately constructed to violate exactly one
# rule of the strict profile (with the other rules satisfied), so a
# regression in any single rule of the matcher is isolated immediately.
# ─────────────────────────────────────────────────────────────────────────────


def _diag(res: MatchResult) -> str:
    """Human-readable per-rule trace used in assertion messages so test
    failures point straight at the culprit rule."""
    lines = [f"profile={res.profile_name!r} overall_passed={res.passed}"]
    for rr in res.per_rule:
        bits = [
            f"  rule[{rr.rule_index}] {rr.rule_type} passed={rr.passed}",
            f"desc={rr.description!r}",
        ]
        if rr.error:
            bits.append(f"error={rr.error!r}")
        if rr.rule_type == "scored":
            bits.append(
                f"total={rr.total_score} distinct={rr.distinct_match_count}"
            )
        matched_refs = sorted(
            r for r, gm in rr.groups.items() if gm.matched
        )
        unmatched_refs = sorted(
            r for r, gm in rr.groups.items() if not gm.matched
        )
        bits.append(f"matched={matched_refs}")
        bits.append(f"unmatched={unmatched_refs}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


# Sentinel substrings used to identify each rule of the strict profile by
# its `description` text. If a future edit re-words the descriptions, the
# tests will still pass IF the new description contains one of these
# substrings; otherwise the assertion message tells you which rule was
# expected to be referenced.
_DESC_ROLE = "Role must be a backend"
_DESC_NOT_INTERN = "Excludes interns"
_DESC_LANGUAGE = "Must use at least one of the languages"
_DESC_CLOUD = "Must touch some cloud"
_DESC_SCORED = "Quality signal"


def _rule_by_desc(res: MatchResult, needle: str) -> RuleResult:
    for rr in res.per_rule:
        if needle.lower() in rr.description.lower():
            return rr
    raise AssertionError(
        f"No rule whose description contains {needle!r}.\n{_diag(res)}"
    )


# ── PASSING JDs for yaswanth_backend_distsys ────────────────────────────────
# Each one explicitly satisfies all five rules and is designed to clear the
# scored threshold with a known margin.

_DISTSYS_PASS_SCENARIOS = [
    pytest.param(
        "vanilla_backend_python_aws_kafka_distsys",
        textwrap.dedent(
            """
            Senior Backend Engineer — Platform Services

            We're hiring a Backend Engineer to build APIs in python on AWS.
            Our stack is distributed systems with microservices wired
            together over kafka.
            """
        ),
        id="distsys-pass-vanilla",
    ),
    pytest.param(
        "platform_engineer_golang_gcp_microservices",
        textwrap.dedent(
            """
            Platform Engineer

            Hiring a Platform Engineer fluent in golang on GCP. You'll work
            on distributed systems and microservices.
            """
        ),
        id="distsys-pass-platform-golang-gcp",
    ),
    pytest.param(
        "mts_java_oci_fault_tolerance_scalability_tracing",
        textwrap.dedent(
            """
            Member of Technical Staff (MTS) — Cloud Services

            We are hiring a Member of Technical Staff to build cloud
            services on OCI in java. Strong focus on fault tolerance,
            scalability, and distributed tracing using opentelemetry.
            """
        ),
        id="distsys-pass-mts-java-oci",
    ),
    pytest.param(
        "application_developer_typescript_azure_kafka_redis_dynamodb",
        textwrap.dedent(
            """
            Application Developer

            Hiring an Application Developer fluent in typescript working on
            azure. You will build microservices that handle high throughput
            with kafka. Experience with redis cache and dynamodb is a plus.
            """
        ),
        id="distsys-pass-app-dev-ts-azure",
    ),
    pytest.param(
        "distributed_systems_engineer_scala_aws_grpc",
        textwrap.dedent(
            """
            Distributed Systems Engineer

            Looking for a Distributed Systems Engineer with strong scala on
            AWS. You'll design microservices and own grpc APIs.
            """
        ),
        id="distsys-pass-distsys-engineer-scala",
    ),
    pytest.param(
        "software_engineer_python_aws_distsys_kafka",
        textwrap.dedent(
            """
            Software Engineer

            Software Engineer fluent in python on aws. Build distributed
            systems backed by kafka.
            """
        ),
        id="distsys-pass-swe-python-aws-distsys-kafka",
    ),
    pytest.param(
        "all_caps_jd_should_still_match",
        textwrap.dedent(
            """
            SENIOR BACKEND ENGINEER

            WE NEED A BACKEND ENGINEER FLUENT IN PYTHON ON AWS.
            BUILD DISTRIBUTED SYSTEMS AND MICROSERVICES WITH KAFKA.
            """
        ),
        id="distsys-pass-allcaps",
    ),
    pytest.param(
        "fullstack_with_javascript_and_azure",
        textwrap.dedent(
            """
            Fullstack Engineer

            Fullstack Engineer fluent in javascript on azure. You will own
            microservices behind a React UI, talking to kafka and redis.
            """
        ),
        id="distsys-pass-fullstack-js-azure",
    ),
    pytest.param(
        # Plural forms of intern/new-grad words are SAFE because the matcher's
        # word boundary treats `s` as in-word — so `interns`/`new grads` do
        # not trip the singular `intern`/`new grad` aliases. This JD bakes
        # that property into the regression suite.
        "plural_intern_words_do_not_trip_negation",
        textwrap.dedent(
            """
            Senior Backend Engineer (no interns, no new grads, no trainees)

            Hiring a Backend Engineer fluent in python on aws. You will
            build distributed systems and microservices with kafka.
            """
        ),
        id="distsys-pass-plural-intern-words-safe",
    ),
]


# ── FAILING JDs for yaswanth_backend_distsys ────────────────────────────────
# Each one fails EXACTLY ONE rule (with the others satisfied) so we can
# pinpoint regressions.

# Each FAIL JD is engineered to violate exactly ONE rule while satisfying
# every other rule of the strict profile. That isolation matters: it means
# a per-rule regression surfaces as a single targeted failure, not as a
# noise cascade. The follow-up test `test_failing_jds_isolate_a_single_rule`
# verifies the isolation invariant; if it fails, the JD itself drifted.
_DISTSYS_FAIL_SCENARIOS = [
    pytest.param(
        "frontend_only_role_fails_rule1",
        textwrap.dedent(
            """
            Senior Frontend Designer

            Hiring a Frontend Designer to craft beautiful UIs in
            javascript on aws. Some exposure to distributed systems,
            microservices, kafka, redis.
            """
        ),
        _DESC_ROLE,
        id="distsys-fail-frontend-role",
    ),
    pytest.param(
        "intern_role_fails_rule2",
        textwrap.dedent(
            """
            Backend Engineer Intern

            Summer role — Backend Engineer in python on aws. You'll learn
            distributed systems and microservices and kafka.
            """
        ),
        _DESC_NOT_INTERN,
        id="distsys-fail-intern",
    ),
    pytest.param(
        "new_grad_role_fails_rule2",
        textwrap.dedent(
            """
            Backend Engineer — New Grad Program

            Backend Engineer for our new grad program. You'll work in
            python on aws building distributed systems, microservices,
            kafka.
            """
        ),
        _DESC_NOT_INTERN,
        id="distsys-fail-new-grad",
    ),
    pytest.param(
        "rust_only_fails_rule3_language",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Hiring a Backend Engineer fluent in Rust. We work on AWS and
            build distributed systems with microservices and kafka.
            """
        ),
        _DESC_LANGUAGE,
        id="distsys-fail-rust-only",
    ),
    pytest.param(
        "on_prem_no_cloud_fails_rule4_cloud",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Hiring a Backend Engineer fluent in python. We work in our
            on-prem datacenter building distributed systems, microservices,
            and kafka.
            """
        ),
        _DESC_CLOUD,
        id="distsys-fail-no-cloud",
    ),
    pytest.param(
        "score_just_under_threshold_fails_rule5",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. You will own our
            microservices and the kafka pipeline.
            """
        ),
        _DESC_SCORED,
        id="distsys-fail-score-under",
    ),
    pytest.param(
        "score_zero_signal_fails_rule5",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Hiring a Backend Engineer fluent in python on aws. CRUD apps,
            simple admin tools, basic workflows.
            """
        ),
        _DESC_SCORED,
        id="distsys-fail-score-zero",
    ),
    pytest.param(
        "mobile_role_fails_rule1",
        textwrap.dedent(
            """
            Senior Mobile Engineer — Android

            Hiring a Mobile Engineer for Android development in java on
            AWS. You'll touch distributed systems, microservices, kafka.
            """
        ),
        _DESC_ROLE,
        id="distsys-fail-mobile",
    ),
    pytest.param(
        "security_role_fails_rule1",
        textwrap.dedent(
            """
            Application Security Engineer

            Hiring an Application Security Engineer to harden services
            written in python on aws. Familiarity with distributed systems,
            microservices, and kafka required.
            """
        ),
        _DESC_ROLE,
        id="distsys-fail-security",
    ),
    pytest.param(
        "data_scientist_role_fails_rule1",
        textwrap.dedent(
            """
            Senior Data Scientist

            Hiring a Data Scientist to build models in python on aws.
            Some distributed systems, microservices, and kafka exposure
            preferred.
            """
        ),
        _DESC_ROLE,
        id="distsys-fail-data-scientist",
    ),
]


class TestEndToEndBackendDistsys:
    """End-to-end pass/fail scenarios for `yaswanth_backend_distsys`.

    The profile has 5 rules (4 boolean + 1 scored, threshold ≥5). We feed
    realistic JDs and assert the overall verdict; for FAIL scenarios we
    also assert WHICH rule failed, so a regression in any single rule of
    the matcher (or in any single canonical's alias list) is caught
    independently.
    """

    @pytest.fixture(scope="class")
    def alias_roots(self):
        return load_alias_files()

    @pytest.fixture(scope="class")
    def profile(self):
        return load_profile("yaswanth_backend_distsys")

    @pytest.mark.parametrize(("label", "jd"), _DISTSYS_PASS_SCENARIOS)
    def test_passing_jds(self, label, jd, profile, alias_roots):
        skills, roles, locations = alias_roots
        res = match_profile_against_jd(
            profile, jd, skills, roles, locations
        )
        assert res.passed is True, (
            f"Expected PASS for {label!r} but got FAIL.\n{_diag(res)}"
        )
        # Every rule must have evaluated without internal errors —
        # an `error` set means a malformed expression or unresolved ref.
        for rr in res.per_rule:
            assert rr.error is None, (
                f"Rule had an internal error for {label!r}: {rr.error}\n"
                f"{_diag(res)}"
            )

    @pytest.mark.parametrize(
        ("label", "jd", "expected_failing_desc"), _DISTSYS_FAIL_SCENARIOS
    )
    def test_failing_jds_fail_for_the_right_reason(
        self, label, jd, expected_failing_desc, profile, alias_roots
    ):
        skills, roles, locations = alias_roots
        res = match_profile_against_jd(
            profile, jd, skills, roles, locations
        )
        assert res.passed is False, (
            f"Expected FAIL for {label!r} but got PASS.\n{_diag(res)}"
        )
        rr = _rule_by_desc(res, expected_failing_desc)
        assert rr.passed is False, (
            f"Scenario {label!r} should fail at rule "
            f"matching description {expected_failing_desc!r}, but that "
            f"rule actually PASSED.\n{_diag(res)}"
        )
        assert rr.error is None, (
            f"Rule {expected_failing_desc!r} fail should be a clean "
            f"semantic fail, not an evaluation error: {rr.error}\n"
            f"{_diag(res)}"
        )

    @pytest.mark.parametrize(
        ("label", "jd", "expected_failing_desc"), _DISTSYS_FAIL_SCENARIOS
    )
    def test_failing_jds_isolate_a_single_rule(
        self, label, jd, expected_failing_desc, profile, alias_roots
    ):
        """Every FAIL JD in the parameter set is engineered to violate
        exactly ONE rule. If two rules go red on the same JD, the JD
        is over-constrained — and that means the per-rule diagnostic
        loses its precision. This test pins that invariant."""
        skills, roles, locations = alias_roots
        res = match_profile_against_jd(
            profile, jd, skills, roles, locations
        )
        failing = [rr for rr in res.per_rule if not rr.passed]
        assert len(failing) == 1, (
            f"Scenario {label!r} expected to fail exactly one rule; "
            f"actually failed {len(failing)} rule(s). Tighten the JD or "
            f"the rule descriptions.\n{_diag(res)}"
        )
        assert (
            expected_failing_desc.lower() in failing[0].description.lower()
        )


# ── End-to-end suite for the looser fullstack_broad profile ─────────────────


_FULLSTACK_PASS_SCENARIOS = [
    pytest.param(
        "swe_java_no_cloud_needed",
        "Software Engineer working in Java on internal tools. Full-time.",
        id="fullstack-pass-swe-java",
    ),
    pytest.param(
        "backend_engineer_python",
        "Backend Engineer building services in python.",
        id="fullstack-pass-backend-python",
    ),
    pytest.param(
        "fullstack_engineer_react",
        "Fullstack Engineer fluent in React.",
        id="fullstack-pass-fullstack-react",
    ),
    pytest.param(
        "platform_engineer_golang",
        "Platform Engineer fluent in golang.",
        id="fullstack-pass-platform-golang",
    ),
    pytest.param(
        "swe_spring_boot",
        "Software Engineer experienced with spring boot.",
        id="fullstack-pass-swe-spring-boot",
    ),
    pytest.param(
        "swe_helidon",
        "Software Engineer with helidon experience.",
        id="fullstack-pass-swe-helidon",
    ),
]


_FULLSTACK_FAIL_SCENARIOS = [
    pytest.param(
        "mobile_engineer_java",
        "Mobile Engineer building Android apps in java.",
        id="fullstack-fail-mobile",
    ),
    pytest.param(
        "security_engineer_python",
        "Application Security Engineer working in python.",
        id="fullstack-fail-security",
    ),
    pytest.param(
        "data_scientist_python",
        "Data Scientist building models in python.",
        id="fullstack-fail-data-scientist",
    ),
    pytest.param(
        "swe_rust_only_no_listed_skill",
        "Software Engineer fluent in Rust building backend services.",
        id="fullstack-fail-rust-only",
    ),
    pytest.param(
        "designer_no_engineering_role",
        "Visual Designer. We need a designer who can craft beautiful UIs.",
        id="fullstack-fail-designer",
    ),
]


class TestEndToEndFullstackBroad:
    @pytest.fixture(scope="class")
    def alias_roots(self):
        return load_alias_files()

    @pytest.fixture(scope="class")
    def profile(self):
        return load_profile("yaswanth_fullstack_broad")

    @pytest.mark.parametrize(("label", "jd"), _FULLSTACK_PASS_SCENARIOS)
    def test_passing_jds(self, label, jd, profile, alias_roots):
        skills, roles, locations = alias_roots
        res = match_profile_against_jd(
            profile, jd, skills, roles, locations
        )
        assert res.passed is True, (
            f"Expected PASS for {label!r} but got FAIL.\n{_diag(res)}"
        )
        for rr in res.per_rule:
            assert rr.error is None, (
                f"Internal rule error for {label!r}: {rr.error}\n"
                f"{_diag(res)}"
            )

    @pytest.mark.parametrize(("label", "jd"), _FULLSTACK_FAIL_SCENARIOS)
    def test_failing_jds(self, label, jd, profile, alias_roots):
        skills, roles, locations = alias_roots
        res = match_profile_against_jd(
            profile, jd, skills, roles, locations
        )
        assert res.passed is False, (
            f"Expected FAIL for {label!r} but got PASS.\n{_diag(res)}"
        )
        for rr in res.per_rule:
            assert rr.error is None


# ── Cross-profile: one JD, two verdicts ─────────────────────────────────────


class TestEndToEndCrossProfile:
    """The same JD should produce different verdicts on the two profiles
    according to each profile's own rules. This catches regressions where
    profile-evaluation state leaks across calls."""

    @pytest.fixture(scope="class")
    def alias_roots(self):
        return load_alias_files()

    def test_swe_java_no_cloud_passes_broad_fails_strict(self, alias_roots):
        # A plain SWE with java but no cloud:
        #   - fullstack_broad: PASS (role + java OK)
        #   - backend_distsys: FAIL on the cloud rule (and scored rule)
        skills, roles, locations = alias_roots
        jd = "Software Engineer working in Java on internal tools. Full-time."
        broad = match_profile_against_jd(
            load_profile("yaswanth_fullstack_broad"),
            jd, skills, roles, locations,
        )
        strict = match_profile_against_jd(
            load_profile("yaswanth_backend_distsys"),
            jd, skills, roles, locations,
        )
        assert broad.passed is True, _diag(broad)
        assert strict.passed is False, _diag(strict)
        # Specifically the cloud rule must be the / a failing rule.
        cloud_rule = _rule_by_desc(strict, _DESC_CLOUD)
        assert cloud_rule.passed is False

    def test_mobile_engineer_fails_both_profiles_for_role(self, alias_roots):
        skills, roles, locations = alias_roots
        jd = (
            "Senior Mobile Engineer — Android. java on AWS. distributed "
            "systems, microservices, kafka. Full-time, no internship."
        )
        broad = match_profile_against_jd(
            load_profile("yaswanth_fullstack_broad"),
            jd, skills, roles, locations,
        )
        strict = match_profile_against_jd(
            load_profile("yaswanth_backend_distsys"),
            jd, skills, roles, locations,
        )
        assert broad.passed is False
        assert strict.passed is False
        # The strict profile fails specifically the role rule.
        role_rule = _rule_by_desc(strict, _DESC_ROLE)
        assert role_rule.passed is False


# ── Scored-rule boundary suite ──────────────────────────────────────────────
#
# Each JD here is engineered to push the scored rule to a precise
# `total_score` so we can verify the `>=5` threshold flips at exactly the
# right spot. All four boolean rules of the strict profile are also
# satisfied — so the overall verdict tracks the scored rule alone.


_SCORED_BOUNDARY_SCENARIOS = [
    # (label, JD, expected_total_score, expected_overall_passed)
    #
    # All four boolean rules of the strict profile must pass for every
    # scenario below — the JDs deliberately avoid intern/new-grad alias
    # words so the scored rule's threshold is the *only* moving piece.
    pytest.param(
        "score_0",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. CRUD apps, simple
            admin tools, basic workflows.
            """
        ),
        0.0,
        False,
        id="scored-boundary-0",
    ),
    pytest.param(
        "score_3_distsys_only",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. You'll build
            distributed systems.
            """
        ),
        3.0,   # distributed_systems = 3
        False,
        id="scored-boundary-3",
    ),
    pytest.param(
        "score_4_microservices_plus_kafka",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. You'll own our
            microservices and the kafka pipeline.
            """
        ),
        4.0,   # microservices(2) + kafka(2) = 4
        False,
        id="scored-boundary-4",
    ),
    pytest.param(
        "score_5_distsys_plus_kafka_exact_threshold",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. You'll work on
            distributed systems with kafka.
            """
        ),
        5.0,   # distributed_systems(3) + kafka(2) = 5
        True,
        id="scored-boundary-5-exact",
    ),
    pytest.param(
        "score_5_microservices_scalability_lambda",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. You'll build
            scalable microservices on aws lambda.
            """
        ),
        5.0,   # microservices(2) + scalability(2) + lambda(1) = 5
        True,
        id="scored-boundary-5-alt-mix",
    ),
    pytest.param(
        "score_7_distsys_microservices_kafka",
        textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. You'll build
            distributed systems and microservices and kafka pipelines.
            """
        ),
        7.0,   # distributed_systems(3) + microservices(2) + kafka(2) = 7
        True,
        id="scored-boundary-7",
    ),
]


class TestScoredRuleBoundary:
    """Pin down the scored-rule threshold behavior with surgical JDs.

    For each scenario the JD satisfies rules 1-4 cleanly; the only thing
    moving across rows is the scored total. This catches regressions in:
      - weight summation (e.g. accidentally counting unmatched groups)
      - threshold direction (>= vs >)
      - alias coverage of any individual scored canonical
    """

    @pytest.fixture(scope="class")
    def alias_roots(self):
        return load_alias_files()

    @pytest.fixture(scope="class")
    def profile(self):
        return load_profile("yaswanth_backend_distsys")

    @pytest.mark.parametrize(
        ("label", "jd", "expected_total", "expected_passed"),
        _SCORED_BOUNDARY_SCENARIOS,
    )
    def test_score_and_verdict(
        self, label, jd, expected_total, expected_passed,
        profile, alias_roots,
    ):
        skills, roles, locations = alias_roots
        res = match_profile_against_jd(
            profile, jd, skills, roles, locations
        )

        # First, the four boolean rules must all pass — otherwise we
        # aren't really testing the scored rule's threshold in isolation.
        for rr in res.per_rule:
            if rr.rule_type == "boolean":
                assert rr.passed, (
                    f"Boolean rule {rr.description!r} unexpectedly failed "
                    f"for scenario {label!r} — the scored boundary test "
                    f"isn't isolated.\n{_diag(res)}"
                )

        scored_rule = _rule_by_desc(res, _DESC_SCORED)
        assert scored_rule.rule_type == "scored"
        assert scored_rule.total_score == pytest.approx(expected_total), (
            f"Scenario {label!r}: expected total_score={expected_total}, "
            f"got {scored_rule.total_score}.\n{_diag(res)}"
        )
        assert scored_rule.passed is expected_passed, (
            f"Scenario {label!r}: scored rule expected "
            f"passed={expected_passed}, got {scored_rule.passed}.\n"
            f"{_diag(res)}"
        )
        assert res.passed is expected_passed


# ── Realism / robustness end-to-end edge cases ──────────────────────────────


class TestEndToEndEdgeCases:
    """End-to-end JDs that probe realism: weird whitespace, repeated
    aliases, JD-format quirks, and the documented word-boundary
    invariants — all evaluated through `match_profile_against_jd` rather
    than poking helpers directly."""

    @pytest.fixture(scope="class")
    def alias_roots(self):
        return load_alias_files()

    def test_same_canonical_via_multiple_aliases_does_not_double_count(
        self, alias_roots
    ):
        # `kafka` and `apache kafka` are both aliases of the same canonical
        # (`skills.messaging.kafka`). A JD mentioning both should still
        # count kafka as ONE matched group, contributing one weight (2),
        # not two (4). This is the documented behavior of distinct_matches
        # and of the `if group_matches[ref].matched: total_score += weight`
        # pattern in _eval_scored.
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. We use kafka
            extensively — specifically Apache Kafka — for our event
            streaming.
            """
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        scored = _rule_by_desc(res, _DESC_SCORED)
        # Only kafka triggers in the scored rule → total = 2 (NOT 4).
        assert scored.total_score == pytest.approx(2.0), (
            f"Kafka triggered via two aliases inflated the score from 2 to "
            f"{scored.total_score}. Distinct grouping is broken.\n"
            f"{_diag(res)}"
        )
        # As a corollary, the rule fails (2 < 5 threshold).
        assert scored.passed is False
        assert res.passed is False

    def test_jd_with_noisy_whitespace_and_punctuation_still_matches(
        self, alias_roots
    ):
        # Bulleted JD with leading whitespace, parentheses, slashes, and
        # weird line wraps. The matcher should normalize and still match.
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = (
            "  Senior Backend Engineer\t\t(Platform team)  \n\n"
            "  • What you'll do:\n"
            "      - build APIs in   python   on   aws\n"
            "      - own our distributed   systems / microservices stack\n"
            "      - wire services together via kafka\n\n"
            "  • Not for: interns, new grads.\n"
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        assert res.passed is True, _diag(res)

    def test_word_boundary_does_not_let_html_substring_satisfy_ml_role(
        self, alias_roots
    ):
        # End-to-end version of the unit boundary test: even though the
        # `ml` alias exists on machine_learning, a frontend HTML-only JD
        # must NOT match the backend_distsys profile. The strict profile
        # has no `ml` ref directly, but the broader sanity-check is: a
        # purely-HTML JD must still fail every backend rule.
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = (
            "Senior HTML/CSS Designer. We craft pixel-perfect interfaces in "
            "html5 and css. No backend, no cloud, no databases. "
            "Internship-free."
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        assert res.passed is False
        # In particular, the language rule must NOT have been satisfied
        # by an `ml` false-positive on `html`.
        lang_rule = _rule_by_desc(res, _DESC_LANGUAGE)
        matched_langs = sorted(
            r for r, gm in lang_rule.groups.items() if gm.matched
        )
        assert matched_langs == [], (
            f"Language rule falsely matched on a HTML/CSS-only JD: "
            f"{matched_langs}.\n{_diag(res)}"
        )

    def test_repeated_invocation_of_matcher_is_deterministic(
        self, alias_roots
    ):
        # Run the same (profile, JD) pair multiple times — no state should
        # leak across calls (e.g. through the pattern cache or per-match
        # caches) such that the verdict changes.
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = (
            "Senior Backend Engineer in python on aws. Distributed systems "
            "and kafka."
        )
        verdicts = [
            match_profile_against_jd(
                prof, jd, skills, roles, locations
            ).passed
            for _ in range(5)
        ]
        assert all(v is True for v in verdicts), verdicts

    def test_scored_distinct_matches_counts_unique_refs(self, alias_roots):
        # `distinct_match_count` on the scored RuleResult should equal the
        # number of UNIQUE scored canonicals that fired, not the number of
        # alias occurrences. End-to-end check using the real profile.
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = textwrap.dedent(
            """
            Senior Backend Engineer

            Backend Engineer fluent in python on aws. You will build
            distributed systems and microservices and kafka pipelines on
            top of redis. Full-time, no internship.
            """
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        scored = _rule_by_desc(res, _DESC_SCORED)
        # distributed_systems + microservices + kafka + redis = 4 distinct.
        assert scored.distinct_match_count == 4, _diag(res)
        # And the total is 3+2+2+2 = 9.
        assert scored.total_score == pytest.approx(9.0), _diag(res)
        assert scored.passed is True

    def test_alias_variants_with_explicit_hyphenation_match(
        self, alias_roots
    ):
        # The README and context.md both say the matcher does NOT auto-
        # infer variants like `back-end` ⇄ `backend` — the alias file
        # must list each variant explicitly. End-to-end check that the
        # explicit `back-end engineer` alias does fire.
        skills, roles, locations = alias_roots
        prof = load_profile("yaswanth_backend_distsys")
        jd = (
            "Senior Back-End Engineer fluent in python on aws. Distributed "
            "systems and kafka."
        )
        res = match_profile_against_jd(prof, jd, skills, roles, locations)
        # If `back-end engineer` is in the alias list (it is per current
        # config), the role rule should pass; the rest should follow.
        role_rule = _rule_by_desc(res, _DESC_ROLE)
        assert role_rule.passed is True, (
            "The explicit `back-end engineer` alias should let the role "
            f"rule match.\n{_diag(res)}"
        )
        assert res.passed is True


# ─────────────────────────────────────────────────────────────────────────────
# Documented matcher quirks that REAL JDs trigger.
#
# These tests pin down behavior the matcher exhibits today by design (or
# by simple "we don't try to be smart") but that surprises first-time
# readers. If any of these starts failing, somebody intentionally changed
# the matcher's contract — re-read context.md before "fixing" the test.
# ─────────────────────────────────────────────────────────────────────────────


class TestMatcherWordingQuirks:
    """End-to-end tests for matcher characteristics that bite in real JDs."""

    @pytest.fixture(scope="class")
    def alias_roots(self):
        return load_alias_files()

    @pytest.fixture(scope="class")
    def profile(self):
        return load_profile("yaswanth_backend_distsys")

    def test_no_internship_phrasing_trips_the_intern_rule(
        self, profile, alias_roots
    ):
        # The matcher does keyword matching, NOT negation parsing. The
        # alias list for `intern` includes `internship`, so a JD that says
        # "Senior Backend Engineer — full-time, no internship" will be
        # rejected by `not intern` even though a human reader sees the
        # negation. This is intentional: per context.md, the alias files
        # are the spec; the matcher does not guess. The right fix in real
        # JDs is to rephrase (e.g. "no interns"); the wrong fix is to make
        # the matcher context-aware.
        skills, roles, locations = alias_roots
        jd = (
            "Senior Backend Engineer — full-time, no internship. Python "
            "on AWS. Distributed systems and kafka."
        )
        res = match_profile_against_jd(
            profile, jd, skills, roles, locations
        )
        assert res.passed is False, _diag(res)
        intern_rule = _rule_by_desc(res, _DESC_NOT_INTERN)
        assert intern_rule.passed is False
        # Specifically `internship` was the alias that fired.
        intern_group = intern_rule.groups["roles.seniority_modifiers.intern"]
        assert intern_group.matched is True
        assert "internship" in intern_group.matched_aliases

    def test_plural_intern_words_do_not_trip_singular_alias(
        self, profile, alias_roots
    ):
        # The escape hatch for the test above: pluralizing the negation
        # word lets the matcher's word boundary do its job. `interns` has a
        # trailing `s` (word-like) so the `intern` alias's right-boundary
        # check fails — no match.
        skills, roles, locations = alias_roots
        jd = (
            "Senior Backend Engineer (no interns, no new grads). Python "
            "on AWS. Distributed systems and kafka."
        )
        res = match_profile_against_jd(profile, jd, skills, roles, locations)
        assert res.passed is True, _diag(res)
        intern_rule = _rule_by_desc(res, _DESC_NOT_INTERN)
        intern_group = intern_rule.groups["roles.seniority_modifiers.intern"]
        ng_group = intern_rule.groups["roles.seniority_modifiers.new_grad"]
        assert intern_group.matched is False, intern_group.matched_aliases
        assert ng_group.matched is False, ng_group.matched_aliases

    def test_engineering_does_not_match_engineer_alias(
        self, profile, alias_roots
    ):
        # Word boundary: `engineer` followed by `i` (in "engineering") is
        # word-like, so the boundary check fails. A JD that ONLY uses the
        # word "Engineering" in its title therefore does NOT match the
        # `backend engineer` alias. This is the source of many surprised
        # "but my JD says backend engineering!" reports — the fix is to
        # add `backend engineering` to the alias list explicitly if
        # desired.
        skills, roles, locations = alias_roots
        jd = (
            "Backend Engineering Specialist working in python on aws. "
            "Distributed systems and kafka."
        )
        res = match_profile_against_jd(profile, jd, skills, roles, locations)
        role_rule = _rule_by_desc(res, _DESC_ROLE)
        # None of the rule-1 role aliases should have fired.
        matched = [r for r, gm in role_rule.groups.items() if gm.matched]
        assert matched == [], (
            f"`Backend Engineering` falsely matched a role alias: {matched}.\n"
            f"{_diag(res)}"
        )
        assert role_rule.passed is False
        assert res.passed is False

    def test_bare_go_does_not_match_go_language(
        self, profile, alias_roots
    ):
        # Bare `go` is deliberately omitted from the `go` aliases (only
        # `golang`, `go programming`, `go lang`, `go language` are listed).
        # A JD that just says "go" should NOT match the language rule, even
        # though it would be a real-world false positive otherwise ("let's
        # go build the platform"). This pins that omission.
        skills, roles, locations = alias_roots
        jd = (
            "Senior Backend Engineer. We want you to go build distributed "
            "systems on aws with kafka."
        )
        res = match_profile_against_jd(profile, jd, skills, roles, locations)
        lang_rule = _rule_by_desc(res, _DESC_LANGUAGE)
        go_group = lang_rule.groups["skills.languages.go"]
        assert go_group.matched is False, (
            f"Bare `go` should not match the Go language: aliases hit = "
            f"{go_group.matched_aliases}"
        )
        # Whole rule fails because no language matched.
        assert lang_rule.passed is False
        assert res.passed is False

    def test_golang_does_match(self, profile, alias_roots):
        # Inverse of the test above: the compound form `golang` is in the
        # alias list and must match.
        skills, roles, locations = alias_roots
        jd = (
            "Senior Backend Engineer. We want you to write golang services "
            "on aws with distributed systems and kafka."
        )
        res = match_profile_against_jd(profile, jd, skills, roles, locations)
        assert res.passed is True, _diag(res)
