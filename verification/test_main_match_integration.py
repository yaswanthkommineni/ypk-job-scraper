"""Integration tests for the matching / notify wiring in `main.py`.

These tests cover the pieces added when the pipeline grew the ability to
match every new job against every enabled profile and notify on a match:

  * `_extract_jd_text` — concatenates the right job-model attributes,
    skips missing/empty ones, returns "" only when nothing useful exists.
  * `MatchingState` — dataclass shape + sensible defaults.
  * `load_matching_state` — pre-filters to `enable: true` and pulls live
    alias trees from disk.
  * `notify` — the stub never raises and emits the expected one-liner.
  * `_match_and_notify` — calls `notify` exactly once per (job, enabled
    profile) pair that matches, never for non-matching profiles, never
    when matching_state is None / empty, and survives a per-profile
    matcher exception without dropping subsequent profiles or notifies.
  * `process_fetch_result` integration — only NEW jobs (not repeats, not
    too-old jobs) reach `_match_and_notify`.

Run from project root:

    pytest verification/test_main_match_integration.py -v
    pytest verification/test_main_match_integration.py -v -k match_and_notify

Security note: no subprocess, no eval, no network. The tests construct
job mocks via `types.SimpleNamespace` and use an in-memory SQLite DB.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Make the project root importable regardless of pytest's cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from main import (  # noqa: E402
    JD_TEXT_ATTRS,
    MatchingState,
    _extract_jd_text,
    _match_and_notify,
    load_matching_state,
    notify,
    process_fetch_result,
)
from verification.match_profile import (  # noqa: E402
    MatchResult,
    MatcherError,
    RuleResult,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_job(**attrs) -> SimpleNamespace:
    """Build a fake jobhive job model with arbitrary attributes."""
    return SimpleNamespace(**attrs)


def _make_profile(
    name: str,
    *,
    enable: bool = True,
    rule_expression: str = "skills.languages.python",
    rule_type: str = "boolean",
    chat_id: object = None,
) -> dict:
    """Build a minimal but well-formed profile dict.

    Default rule: one boolean rule that requires `skills.languages.python`.
    Tweak via params to test different rule shapes.

    `chat_id` defaults to None (the "owner hasn't bound a Telegram chat
    yet" case) so notify-touching tests don't accidentally trigger the
    Telegram-delivery code path unless they explicitly opt in.
    """
    return {
        "profile_name": name,
        "chat_id": chat_id,
        "enable": enable,
        "years_of_experience": 2.0,
        "locations": ["locations.india.bengaluru"],
        "matching_rules": [
            {
                "type": rule_type,
                "description": f"test rule for {name}",
                "expression": rule_expression,
            }
        ],
    }


@pytest.fixture
def alias_trees() -> tuple[dict, dict, dict]:
    """Tiny alias trees that cover the refs used in this file's profiles.

    Kept local rather than reading the live YAMLs so these tests don't
    couple to the real config (which evolves frequently).
    """
    skills = {
        "languages": {
            "python": ["python", "python3", "py"],
            "go": ["golang", "go programming"],
            "java": ["java", "jdk"],
        },
        "cloud_providers": {
            "aws": ["aws", "amazon web services"],
        },
    }
    roles = {
        "role_families": {
            "backend_engineer": ["backend engineer", "back-end engineer"],
        },
        "seniority_modifiers": {
            "intern": ["intern", "internship"],
        },
    }
    locations = {
        "india": {
            "bengaluru": ["bengaluru", "bangalore", "blr"],
        },
    }
    return skills, roles, locations


@pytest.fixture
def matching_state(alias_trees) -> MatchingState:
    """A `MatchingState` containing two enabled profiles + one disabled."""
    skills, roles, locations = alias_trees
    return MatchingState(
        enabled_profiles=[
            _make_profile("py_only", rule_expression="skills.languages.python"),
            _make_profile(
                "go_only",
                rule_expression="skills.languages.go",
            ),
        ],
        skills_root=skills,
        roles_root=roles,
        locations_root=locations,
    )


@pytest.fixture
def memdb() -> sqlite3.Connection:
    """In-memory SQLite with the same schema main.init_db creates.

    We duplicate the DDL here (rather than calling `init_db(":memory:")`)
    to keep this test file independent of where exactly init_db lives.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE company_last_fetch (
            slug TEXT PRIMARY KEY,
            last_fetch_ts REAL NOT NULL
        );
        CREATE TABLE ats_platform_last_fetch (
            platform TEXT PRIMARY KEY,
            last_fetch_ts REAL NOT NULL
        );
        CREATE TABLE ats_platform_rate_limit (
            platform TEXT PRIMARY KEY,
            cooldown_period REAL NOT NULL,
            last_rate_limited_at REAL NOT NULL
        );
        CREATE TABLE recent_job_ids (
            job_key TEXT PRIMARY KEY,
            fetched_at REAL NOT NULL
        );
        CREATE INDEX idx_recent_job_ids_fetched_at
            ON recent_job_ids (fetched_at);
        """
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# _extract_jd_text
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractJDText:
    def test_pulls_title_when_only_field_present(self):
        job = _make_job(title="Backend Engineer")
        assert _extract_jd_text(job) == "Backend Engineer"

    def test_concatenates_all_present_attrs_with_newlines(self):
        job = _make_job(
            title="Senior Backend Engineer",
            description="We build distributed systems on AWS.",
            location="Bengaluru, India",
        )
        text = _extract_jd_text(job)
        assert "Senior Backend Engineer" in text
        assert "distributed systems on AWS" in text
        assert "Bengaluru, India" in text
        # Newline-separated so the matcher can be normalize_jd'd without
        # touching field boundaries.
        assert "\n" in text

    def test_preserves_attr_order(self):
        # title before description before location.
        job = _make_job(
            location="Remote",
            description="Some desc.",
            title="A title",
        )
        text = _extract_jd_text(job)
        i_title = text.index("A title")
        i_desc = text.index("Some desc.")
        i_loc = text.index("Remote")
        assert i_title < i_desc < i_loc

    def test_skips_missing_attrs(self):
        # Only `title` exists; no AttributeError for missing description etc.
        job = _make_job(title="Software Engineer")
        text = _extract_jd_text(job)
        assert text == "Software Engineer"

    @pytest.mark.parametrize("falsy", [None, "", 0, False, []])
    def test_skips_falsy_values(self, falsy):
        job = _make_job(title="The Title", description=falsy)
        text = _extract_jd_text(job)
        assert text == "The Title"

    def test_returns_empty_string_when_nothing_present(self):
        job = _make_job()
        assert _extract_jd_text(job) == ""

    def test_coerces_non_string_truthy_values_to_str(self):
        # Some connectors return rich objects; we str() them so the matcher
        # gets text to work with rather than crashing.
        class WeirdField:
            def __str__(self):
                return "team-distsys-platform"

        job = _make_job(title="X", team=WeirdField())
        text = _extract_jd_text(job)
        assert "team-distsys-platform" in text

    def test_attr_list_constant_matches_implementation(self):
        # If someone adds a field to JD_TEXT_ATTRS but forgets to test it,
        # this catches the mismatch — every attr we promise to read must
        # be reachable via getattr.
        job = _make_job(**{attr: f"VAL-{attr}" for attr in JD_TEXT_ATTRS})
        text = _extract_jd_text(job)
        for attr in JD_TEXT_ATTRS:
            assert f"VAL-{attr}" in text


# ─────────────────────────────────────────────────────────────────────────────
# MatchingState
# ─────────────────────────────────────────────────────────────────────────────


class TestMatchingState:
    def test_default_constructor_yields_empty_state(self):
        ms = MatchingState()
        assert ms.enabled_profiles == []
        assert ms.skills_root == {}
        assert ms.roles_root == {}
        assert ms.locations_root == {}

    def test_holds_provided_values(self, alias_trees):
        skills, roles, locations = alias_trees
        profiles = [_make_profile("p1")]
        ms = MatchingState(
            enabled_profiles=profiles,
            skills_root=skills,
            roles_root=roles,
            locations_root=locations,
        )
        assert ms.enabled_profiles is profiles
        assert ms.skills_root is skills
        assert ms.roles_root is roles
        assert ms.locations_root is locations


# ─────────────────────────────────────────────────────────────────────────────
# load_matching_state — uses the REAL files on disk, so it's an integration
# smoke test against the live config. Stays close to what main() does.
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadMatchingState:
    def test_returns_matching_state(self):
        ms = load_matching_state()
        assert isinstance(ms, MatchingState)

    def test_alias_trees_have_expected_top_categories(self):
        # If someone renames `skills` / `roles` / `locations` in the YAMLs,
        # this test loudly tells them the runtime loader needs an update.
        ms = load_matching_state()
        assert "languages" in ms.skills_root, (
            "skills_root missing `languages` — has skill_aliases.yml "
            "changed its top-level shape?"
        )
        assert "role_families" in ms.roles_root, (
            "roles_root missing `role_families`"
        )
        assert (
            "india" in ms.locations_root
            or "us_california" in ms.locations_root
        ), "locations_root missing expected regions"

    def test_enabled_profiles_only_has_enable_true(self):
        ms = load_matching_state()
        for prof in ms.enabled_profiles:
            assert prof.get("enable") is True, (
                f"profile {prof.get('profile_name')!r} leaked into "
                "enabled_profiles despite enable != True"
            )


# ─────────────────────────────────────────────────────────────────────────────
# notify (stub)
# ─────────────────────────────────────────────────────────────────────────────


class TestNotifyStub:
    def test_does_not_raise_with_full_args(self, capsys):
        profile = _make_profile("p1")
        job = _make_job(title="Engineer", url="https://example.com/x")
        result = MatchResult(profile_name="p1", passed=True, per_rule=[])
        notify(profile, job, result)
        out = capsys.readouterr().out
        assert "[MATCH]" in out
        assert "profile=p1" in out
        assert "Engineer" in out
        assert "https://example.com/x" in out

    def test_handles_missing_profile_name(self, capsys):
        notify(
            {},
            _make_job(title="t"),
            MatchResult(profile_name="<unnamed>", passed=True, per_rule=[]),
        )
        out = capsys.readouterr().out
        assert "profile=<unnamed>" in out

    def test_handles_missing_url(self, capsys):
        notify(
            _make_profile("p1"),
            _make_job(title="t"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )
        out = capsys.readouterr().out
        assert "url=None" in out

    def test_falls_back_to_apply_url_when_url_missing(self, capsys):
        notify(
            _make_profile("p1"),
            _make_job(title="t", apply_url="https://example.com/a"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )
        out = capsys.readouterr().out
        assert "https://example.com/a" in out


# ─────────────────────────────────────────────────────────────────────────────
# notify — Telegram delivery wiring
#
# These tests cover the half of `notify` that DOES touch I/O. The strategy
# in every case is to monkeypatch `main.send_message` (the imported alias
# of `verification.sync_telegram_chat_ids.send_message`) with a recording
# stub, AND inject a fake `local_secrets` module via `sys.modules` so the
# real `local_secrets.py` on disk is never read by the test process. This
# ensures the test suite cannot accidentally hit the live Telegram API
# even if a developer's `local_secrets.py` carries a real bot token.
# ─────────────────────────────────────────────────────────────────────────────


def _install_fake_local_secrets(
    monkeypatch, *, token: str = "fake-test-token"
) -> None:
    """Replace any real `local_secrets` import with a SimpleNamespace.

    Use `token=""` to simulate an empty / unset token. Use
    `monkeypatch.setitem(sys.modules, "local_secrets", None)` directly
    in a test if you instead want to simulate the file being absent.
    """
    fake = SimpleNamespace(telegram_bot_token=token)
    monkeypatch.setitem(sys.modules, "local_secrets", fake)


class TestNotifyTelegramDelivery:
    def _record_sends(self, monkeypatch) -> list[tuple]:
        """Replace `main.send_message` with a recorder; return the buffer."""
        sent: list[tuple] = []

        def fake(token: str, chat_id: int, text: str) -> None:
            sent.append((token, chat_id, text))

        monkeypatch.setattr(main, "send_message", fake)
        return sent

    def test_sends_telegram_message_when_chat_id_and_token_present(
        self, monkeypatch, capsys
    ):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        profile = _make_profile("yaswanth_backend", chat_id=42)
        job = _make_job(
            title="Senior Backend Engineer",
            url="https://example.com/jobs/99",
            company="Acme",
            location="Bengaluru",
        )
        notify(
            profile,
            job,
            MatchResult(profile_name="yaswanth_backend", passed=True, per_rule=[]),
        )

        assert len(sent) == 1, "expected exactly one Telegram send_message call"
        token, chat_id, text = sent[0]
        assert token == "bot-token-123"
        assert chat_id == 42
        # The message body must carry the job title, company, location,
        # the matched profile name, and a clickable link to the URL.
        assert "Senior Backend Engineer" in text
        assert "Acme" in text
        assert "Bengaluru" in text
        assert "yaswanth_backend" in text
        assert 'href="https://example.com/jobs/99"' in text

        out = capsys.readouterr().out
        # Local visibility line preserved unchanged.
        assert "[MATCH]" in out
        # No skip / failure breadcrumb when delivery succeeded.
        assert "Telegram delivery skipped" not in out
        assert "Telegram delivery failed" not in out

    def test_skips_telegram_when_chat_id_missing(self, monkeypatch, capsys):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        # No chat_id key on the profile at all (legacy / hand-built dict).
        profile = {"profile_name": "p_no_chat", "enable": True}
        notify(
            profile,
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p_no_chat", passed=True, per_rule=[]),
        )

        assert sent == [], "Telegram must not be called when chat_id is absent"
        out = capsys.readouterr().out
        assert "[MATCH]" in out
        assert "has no chat_id" in out

    def test_skips_telegram_when_chat_id_is_none(self, monkeypatch, capsys):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        notify(
            _make_profile("p1", chat_id=None),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert sent == []
        assert "has no chat_id" in capsys.readouterr().out

    def test_skips_telegram_when_chat_id_is_empty_string(
        self, monkeypatch, capsys
    ):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        notify(
            _make_profile("p1", chat_id=""),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert sent == []
        assert "has no chat_id" in capsys.readouterr().out

    def test_logs_malformed_chat_id_and_does_not_send(
        self, monkeypatch, capsys
    ):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        # str-with-content is "malformed", not "unbound" — we want a
        # different breadcrumb so the user can spot the broken yaml.
        notify(
            _make_profile("p1", chat_id="not-an-int"),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert sent == []
        out = capsys.readouterr().out
        assert "malformed chat_id" in out

    def test_skips_telegram_when_local_secrets_missing(
        self, monkeypatch, capsys
    ):
        # Make `import local_secrets` raise ImportError.
        monkeypatch.setitem(sys.modules, "local_secrets", None)
        sent = self._record_sends(monkeypatch)

        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert sent == []
        assert "local_secrets.py not found" in capsys.readouterr().out

    def test_skips_telegram_when_token_empty(self, monkeypatch, capsys):
        _install_fake_local_secrets(monkeypatch, token="")
        sent = self._record_sends(monkeypatch)

        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert sent == []
        assert "telegram_bot_token is empty" in capsys.readouterr().out

    def test_skips_telegram_when_token_whitespace_only(
        self, monkeypatch, capsys
    ):
        _install_fake_local_secrets(monkeypatch, token="   \n  ")
        sent = self._record_sends(monkeypatch)

        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert sent == []
        assert "telegram_bot_token is empty" in capsys.readouterr().out

    def test_swallows_sync_error_from_send_message(
        self, monkeypatch, capsys
    ):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")

        def boom(*_args, **_kwargs):
            # Use the real exception class so the catch site is exercised.
            from verification.sync_telegram_chat_ids import SyncError
            raise SyncError("Telegram sendMessage HTTP 400: Bad Request")

        monkeypatch.setattr(main, "send_message", boom)

        # Must not raise.
        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )
        out = capsys.readouterr().out
        assert "Telegram delivery failed" in out
        # The token itself must never appear in the log line (defense-
        # in-depth — `SyncError` messages are token-scrubbed at the
        # source, but `notify` interpolates the exc message verbatim,
        # so we keep this assertion as a regression canary).
        assert "bot-token-123" not in out

    def test_swallows_unexpected_exception_from_send_message(
        self, monkeypatch, capsys
    ):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")

        def boom(*_args, **_kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(main, "send_message", boom)

        # Must not raise even on an exception type notify wasn't expecting.
        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title="t", url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )
        assert "unexpected error" in capsys.readouterr().out

    def test_html_escapes_job_title(self, monkeypatch):
        """Untrusted job-board content must never be interpolated raw."""
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        evil = '<script>alert("xss")</script>'
        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title=evil, url="https://example.com/x"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert len(sent) == 1
        _, _, text = sent[0]
        # The raw `<script>` must be escaped — Telegram's HTML parser
        # would otherwise reject the entire message anyway, but the
        # security property we're enforcing is "never trust JD text".
        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    def test_html_escapes_url_attribute(self, monkeypatch):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        # A URL with an embedded `"` would otherwise break out of the
        # href attribute. html.escape(quote=True) defuses that.
        dirty_url = 'https://example.com/x"onmouseover="bad()'
        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title="t", url=dirty_url),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert len(sent) == 1
        _, _, text = sent[0]
        # The double quote in the URL must be escaped so the href
        # attribute can't be broken out of.
        assert '"onmouseover=' not in text
        assert "&quot;onmouseover=" in text

    def test_omits_link_line_when_job_has_no_url(self, monkeypatch):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        notify(
            _make_profile("p1", chat_id=42),
            # No url + no apply_url.
            _make_job(title="Some Job"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert len(sent) == 1
        _, _, text = sent[0]
        # Title still present; link line absent.
        assert "Some Job" in text
        assert "Apply / view job" not in text
        assert "href=" not in text

    def test_falls_back_to_apply_url_when_url_missing_in_message(
        self, monkeypatch
    ):
        _install_fake_local_secrets(monkeypatch, token="bot-token-123")
        sent = self._record_sends(monkeypatch)

        notify(
            _make_profile("p1", chat_id=42),
            _make_job(title="t", apply_url="https://example.com/apply"),
            MatchResult(profile_name="p1", passed=True, per_rule=[]),
        )

        assert len(sent) == 1
        _, _, text = sent[0]
        assert 'href="https://example.com/apply"' in text


# ─────────────────────────────────────────────────────────────────────────────
# _match_and_notify
# ─────────────────────────────────────────────────────────────────────────────


class TestMatchAndNotify:
    def test_no_state_is_a_no_op(self, monkeypatch):
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        _match_and_notify(_make_job(title="Python Engineer"), None)
        spy.assert_not_called()

    def test_empty_enabled_profiles_is_a_no_op(
        self, monkeypatch, alias_trees
    ):
        skills, roles, locations = alias_trees
        ms = MatchingState(
            enabled_profiles=[],
            skills_root=skills,
            roles_root=roles,
            locations_root=locations,
        )
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        _match_and_notify(_make_job(title="Python Engineer"), ms)
        spy.assert_not_called()

    def test_empty_jd_text_is_a_no_op(self, monkeypatch, matching_state):
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        # Job with no readable attrs at all → no JD content → skip.
        _match_and_notify(_make_job(), matching_state)
        spy.assert_not_called()

    def test_whitespace_only_jd_is_a_no_op(
        self, monkeypatch, matching_state
    ):
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        _match_and_notify(_make_job(title="   \n  \t  "), matching_state)
        spy.assert_not_called()

    def test_calls_notify_once_per_matching_profile(
        self, monkeypatch, matching_state
    ):
        # JD mentions Python only → py_only matches, go_only doesn't.
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        job = _make_job(
            title="Senior Python Engineer",
            description="We build platforms in Python.",
        )
        _match_and_notify(job, matching_state)
        assert spy.call_count == 1
        called_profile = spy.call_args.args[0]
        assert called_profile["profile_name"] == "py_only"

    def test_calls_notify_for_all_matching_profiles(
        self, monkeypatch, matching_state
    ):
        # JD mentions BOTH python and golang → both profiles match.
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        job = _make_job(
            title="Backend Engineer",
            description="Stack: Python and Golang microservices on AWS.",
        )
        _match_and_notify(job, matching_state)
        names = sorted(
            call.args[0]["profile_name"] for call in spy.call_args_list
        )
        assert names == ["go_only", "py_only"]

    def test_passes_job_and_match_result_to_notify(
        self, monkeypatch, matching_state
    ):
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        job = _make_job(title="Python Engineer", description="we love python")
        _match_and_notify(job, matching_state)
        assert spy.call_count == 1
        prof, called_job, result = spy.call_args.args
        assert called_job is job, "notify must receive the original job"
        assert isinstance(result, MatchResult)
        assert result.passed is True
        # The matched profile should be the py_only one.
        assert prof["profile_name"] == "py_only"

    def test_no_notify_when_no_profile_matches(
        self, monkeypatch, matching_state
    ):
        # JD has neither python nor golang.
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        job = _make_job(
            title="Frontend Engineer",
            description="HTML and CSS work primarily.",
        )
        _match_and_notify(job, matching_state)
        spy.assert_not_called()

    def test_disabled_profiles_not_in_enabled_list_are_skipped(
        self, monkeypatch, alias_trees
    ):
        # A disabled profile sneaking into enabled_profiles would be a bug
        # in load_matching_state, but _match_and_notify shouldn't rely on
        # that filter being correct — it just iterates whatever it's given.
        # This test documents that contract: callers MUST pre-filter.
        skills, roles, locations = alias_trees
        ms = MatchingState(
            enabled_profiles=[
                _make_profile("py", rule_expression="skills.languages.python"),
            ],
            skills_root=skills,
            roles_root=roles,
            locations_root=locations,
        )
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        _match_and_notify(
            _make_job(title="Python Engineer"), ms
        )
        assert spy.call_count == 1

    def test_profile_with_unresolved_ref_fails_silently_others_still_notified(
        self, monkeypatch, alias_trees
    ):
        # `match_profile_against_jd` catches MatcherError per-rule and
        # converts it to `RuleResult.error` + `passed=False`. So a profile
        # with an unresolved ref doesn't raise — its rule just fails and
        # notify isn't called for it. The key property to verify is:
        # subsequent profiles in the loop are still evaluated and notified.
        # (The startup validator is what's supposed to catch unresolved
        # refs; this is just defense-in-depth.)
        skills, roles, locations = alias_trees
        ms = MatchingState(
            enabled_profiles=[
                _make_profile(
                    "broken",
                    rule_expression="skills.nonexistent.totally_fake",
                ),
                _make_profile(
                    "py_ok", rule_expression="skills.languages.python"
                ),
            ],
            skills_root=skills,
            roles_root=roles,
            locations_root=locations,
        )
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        _match_and_notify(
            _make_job(title="Python Engineer"), ms
        )
        # Broken profile's rule failed → no notify for it.
        # OK profile's rule passed → exactly one notify, for that profile.
        assert spy.call_count == 1
        assert spy.call_args.args[0]["profile_name"] == "py_ok"

    def test_notify_exception_does_not_block_subsequent_profiles(
        self, monkeypatch, matching_state, capsys
    ):
        # Make notify raise on the first call, succeed on the second.
        call_count = {"n": 0}

        def flaky_notify(profile, job, result):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated slack outage")

        monkeypatch.setattr(main, "notify", flaky_notify)

        # JD matches both profiles. Even though the first notify raises,
        # the second profile's notify must still be attempted.
        job = _make_job(
            title="Backend Engineer",
            description="Python and Golang on AWS.",
        )
        _match_and_notify(job, matching_state)
        assert call_count["n"] == 2, (
            "second profile's notify must be attempted even after the "
            "first raises"
        )
        out = capsys.readouterr().out
        assert "notify failed for profile" in out

    def test_unexpected_exception_from_matcher_is_caught(
        self, monkeypatch, matching_state, capsys
    ):
        # Force `match_profile_against_jd` to raise an unexpected
        # exception (not MatcherError) and confirm we still proceed.
        def boom(*_args, **_kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(main, "match_profile_against_jd", boom)
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        _match_and_notify(
            _make_job(title="Python Engineer"), matching_state
        )
        spy.assert_not_called()
        out = capsys.readouterr().out
        # Two enabled profiles → two error lines (one per profile attempt).
        assert out.count("match error for profile") == 2

    def test_scored_rule_path(self, monkeypatch, alias_trees):
        # Exercise the scored branch end-to-end (not just boolean).
        skills, roles, locations = alias_trees
        scored_profile = _make_profile(
            "scored",
            rule_type="scored",
            rule_expression=(
                "skills.languages.python=3, "
                "skills.cloud_providers.aws=2, "
                "totalscore>=4"
            ),
        )
        ms = MatchingState(
            enabled_profiles=[scored_profile],
            skills_root=skills,
            roles_root=roles,
            locations_root=locations,
        )
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)

        # python(3) + aws(2) = 5, threshold 4 → match.
        _match_and_notify(
            _make_job(title="Python on AWS", description="we use aws and python"),
            ms,
        )
        assert spy.call_count == 1

        # python only = 3, threshold 4 → no match.
        spy.reset_mock()
        _match_and_notify(_make_job(title="Python only"), ms)
        spy.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# process_fetch_result integration
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessFetchResultIntegration:
    """Verify that process_fetch_result hooks the matcher in at the right
    spot in the pipeline — exactly for NEW jobs, never for repeats or
    too-old jobs, and not at all for non-success fetch results."""

    def test_match_and_notify_called_for_each_new_job(
        self, monkeypatch, memdb, matching_state
    ):
        spy = MagicMock()
        monkeypatch.setattr(main, "_match_and_notify", spy)

        job1 = _make_job(id="J1", title="Python Engineer")
        job2 = _make_job(id="J2", title="Go Engineer")
        result = {"status": "success", "jobs": [job1, job2], "error": None}
        company = {"slug": "acme", "ats_platform": "greenhouse"}

        process_fetch_result(memdb, company, result, matching_state)

        assert spy.call_count == 2
        called_jobs = [call.args[0] for call in spy.call_args_list]
        assert job1 in called_jobs and job2 in called_jobs
        # The matching_state passed through must be the same object.
        for call in spy.call_args_list:
            assert call.args[1] is matching_state

    def test_repeat_jobs_skip_match_and_notify(
        self, monkeypatch, memdb, matching_state
    ):
        spy = MagicMock()
        monkeypatch.setattr(main, "_match_and_notify", spy)

        job = _make_job(id="J1", title="Python Engineer")
        company = {"slug": "acme", "ats_platform": "greenhouse"}
        result = {"status": "success", "jobs": [job], "error": None}

        # First call: NEW → spy hit.
        process_fetch_result(memdb, company, result, matching_state)
        assert spy.call_count == 1

        # Second call with same job: REPEAT → spy NOT hit again.
        process_fetch_result(memdb, company, result, matching_state)
        assert spy.call_count == 1

    def test_jobs_without_id_are_skipped(
        self, monkeypatch, memdb, matching_state
    ):
        spy = MagicMock()
        monkeypatch.setattr(main, "_match_and_notify", spy)
        # No id, no url, no apply_url → _extract_job_id returns None →
        # job is counted as skipped, never reaches match_and_notify.
        job = _make_job(title="Python Engineer")
        result = {"status": "success", "jobs": [job], "error": None}
        process_fetch_result(
            memdb,
            {"slug": "acme", "ats_platform": "greenhouse"},
            result,
            matching_state,
        )
        spy.assert_not_called()

    def test_rate_limited_result_does_not_match(
        self, monkeypatch, memdb, matching_state
    ):
        spy = MagicMock()
        monkeypatch.setattr(main, "_match_and_notify", spy)
        result = {
            "status": "rate_limited",
            "jobs": [],
            "error": "429 too many requests",
        }
        process_fetch_result(
            memdb,
            {"slug": "acme", "ats_platform": "greenhouse"},
            result,
            matching_state,
        )
        spy.assert_not_called()

    def test_failed_result_does_not_match(
        self, monkeypatch, memdb, matching_state
    ):
        spy = MagicMock()
        monkeypatch.setattr(main, "_match_and_notify", spy)
        result = {"status": "failed", "jobs": [], "error": "boom"}
        process_fetch_result(
            memdb,
            {"slug": "acme", "ats_platform": "greenhouse"},
            result,
            matching_state,
        )
        spy.assert_not_called()

    def test_default_matching_state_is_none_backward_compat(
        self, monkeypatch, memdb
    ):
        # Calling process_fetch_result without `matching_state` (legacy
        # signature) must still work — matching becomes a silent no-op.
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)

        job = _make_job(id="J1", title="Python Engineer")
        result = {"status": "success", "jobs": [job], "error": None}
        # No matching_state argument here.
        process_fetch_result(
            memdb,
            {"slug": "acme", "ats_platform": "greenhouse"},
            result,
        )
        spy.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end with the LIVE alias trees: ensures the integration works
# against the real shapes in skill_aliases.yml / location_aliases.yml.
# This is the test that catches regressions when someone edits the YAMLs.
# ─────────────────────────────────────────────────────────────────────────────


class TestLiveAliasFiles:
    @pytest.fixture
    def live_state(self) -> MatchingState:
        ms = load_matching_state()
        # Build a synthetic enabled profile that uses real refs so we don't
        # depend on which profiles happen to be enabled in profiles.yaml.
        ms.enabled_profiles = [
            _make_profile(
                "synthetic_py_aws",
                rule_type="scored",
                rule_expression=(
                    "skills.languages.python=3, "
                    "skills.cloud_providers.aws=2, "
                    "totalscore>=4"
                ),
            ),
        ]
        return ms

    def test_python_aws_jd_triggers_notify(self, monkeypatch, live_state):
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        job = _make_job(
            title="Senior Software Engineer",
            description=(
                "We build distributed services in Python on AWS. "
                "Strong scalability fundamentals required."
            ),
        )
        _match_and_notify(job, live_state)
        assert spy.call_count == 1
        assert spy.call_args.args[0]["profile_name"] == "synthetic_py_aws"

    def test_unrelated_jd_does_not_trigger(self, monkeypatch, live_state):
        spy = MagicMock()
        monkeypatch.setattr(main, "notify", spy)
        job = _make_job(
            title="Marketing Manager",
            description="Lead our content strategy across social channels.",
        )
        _match_and_notify(job, live_state)
        spy.assert_not_called()
