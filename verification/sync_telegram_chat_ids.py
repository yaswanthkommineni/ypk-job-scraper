r"""Sync `chat_id` fields in `profiles.yaml` from Telegram bot messages.

The workflow this script implements is:

  1. A candidate creates / owns a profile in `profiles.yaml`. The
     `chat_id` field starts empty.
  2. From their personal Telegram account, they open a DM with the bot
     (the bot whose token is in `local_secrets.telegram_bot_token`) and
     send a message of the form:

         profile=<profile_name>

     where `<profile_name>` is the exact `profile_name` (the unique id)
     of one of the profiles in `profiles.yaml`.
  3. Someone runs this script. It calls Telegram's `getUpdates` API,
     scans every recent message for `profile=<name>`, validates that
     `<name>` is a real profile, and writes the message's `chat.id`
     into that profile's `chat_id` field.

USAGE (run from project root)
    python verification/sync_telegram_chat_ids.py            # apply updates
    python verification/sync_telegram_chat_ids.py --dry-run  # preview only
    python verification/sync_telegram_chat_ids.py -v         # verbose

Exit codes
    0   No errors. Either nothing to update, or updates applied
        successfully (or previewed in --dry-run mode).
    1   Validation, parsing, or network error.
    2   Configuration error (missing token, missing profiles.yaml, etc.).

DESIGN NOTES
  * The Telegram bot token NEVER appears in logs or error messages.
  * The HTTP call uses HTTPS exclusively (Telegram's API rejects HTTP).
  * The `getUpdates` request has a hard timeout — we never block the
    pipeline on a hung Telegram call.
  * `profiles.yaml` is edited line-by-line with surgical regex/state-
    machine logic, preserving every comment and the original
    formatting. A round-trip through PyYAML would strip the file's
    extensive cheat-sheet header and per-rule comments, which are the
    spec the project relies on (see `context.md`).
  * If the same `profile=<name>` appears in multiple messages, the
    update with the HIGHEST `update_id` (i.e. the latest) wins —
    Telegram returns updates in ascending order.
  * Per project Rule 1, after writing a change you should still run
    `python verification/validate_profiles.py` — this script does NOT
    auto-invoke the validator, on purpose, so the existing CI / pre-
    commit story is unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Standard project-root resolution used by every script in `verification/`.
# Adding the project root to sys.path lets `import local_secrets` succeed
# regardless of CWD.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROFILES_PATH = PROJECT_ROOT / "profiles.yaml"

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_HTTP_TIMEOUT_SECONDS = 10.0
TELEGRAM_GET_UPDATES_LIMIT = 100  # API maximum per call

# We restrict the parsed profile name to a conservative character class to
# keep arbitrary user input from becoming a YAML / file-path / regex
# weapon. The validator already enforces this same character class on
# `profile_name`, so any legal profile name will be matched.
#
# Security rule (Secure Python Development #2): all external input must
# be sanitized and validated before use in logic — this is the validation.
_PROFILE_REF_RE = re.compile(
    r"\bprofile\s*=\s*(?P<name>[A-Za-z0-9_-]+)\b", re.IGNORECASE
)

# Telegram chat IDs are 64-bit integers (negative for groups/channels).
_CHAT_ID_RE = re.compile(r"^-?\d+$")


# ─────────────────────────────────────────────────────────────────────────────
# Result types — small dataclasses keep `_main()` readable and let unit
# tests assert on the planned changes without parsing stdout.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PendingUpdate:
    profile_name: str
    chat_id: int
    update_id: int      # for "latest wins" tie-breaking
    source_text: str    # the raw message text (truncated when logged)


class SyncError(Exception):
    """Any non-config error raised by this script — surfaced to the CLI."""


# ─────────────────────────────────────────────────────────────────────────────
# Telegram client (stdlib only, no extra deps).
# ─────────────────────────────────────────────────────────────────────────────


def fetch_updates(token: str) -> list[dict[str, Any]]:
    """Call Telegram's `getUpdates` and return the raw update list.

    We do NOT advance the offset — repeated runs read the same window
    (~24h on Telegram's side), which is exactly what we want for an
    idempotent sync tool.

    Raises:
      SyncError on HTTP/transport/JSON-shape problems. The bot token is
      NEVER included in the message text.
    """
    if not isinstance(token, str) or not token.strip():
        raise SyncError(
            "telegram_bot_token is empty — set it in local_secrets.py"
        )
    # Build the URL via urllib's path-quoting to keep the token from
    # accidentally injecting URL syntax (defense in depth — Telegram
    # tokens are well-formed by spec, but we don't trust user-edited
    # secrets blindly).
    quoted_token = urllib.parse.quote(token.strip(), safe="")
    url = (
        f"{TELEGRAM_API_BASE}/bot{quoted_token}/getUpdates"
        f"?limit={TELEGRAM_GET_UPDATES_LIMIT}"
    )

    # Hard-require HTTPS. Telegram itself rejects http, but we double-
    # check here so any future refactor that introduces a base override
    # still fails closed (Secure Python Development rule: prefer HTTPS).
    if not url.startswith("https://"):
        raise SyncError("Telegram API base must be HTTPS")

    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "ypk-job-scraper/sync-chat-ids"}
    )
    try:
        # nosec: URL is built from a constant base + locally-stored bot
        # token (never end-user input), and HTTPS is enforced above.
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=TELEGRAM_HTTP_TIMEOUT_SECONDS
        ) as resp:
            status = resp.getcode()
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        # `exc.read()` may contain a Telegram error payload — include the
        # status but NOT the URL (which contains the token).
        raise SyncError(
            f"Telegram getUpdates HTTP {exc.code}: {exc.reason}"
        ) from None
    except urllib.error.URLError as exc:
        raise SyncError(f"Telegram getUpdates transport error: {exc.reason}") from None
    except TimeoutError:
        raise SyncError(
            f"Telegram getUpdates timed out after "
            f"{TELEGRAM_HTTP_TIMEOUT_SECONDS}s"
        ) from None

    if status != 200:
        raise SyncError(f"Telegram getUpdates non-200 status: {status}")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"Telegram getUpdates returned invalid JSON: {exc}") from None

    if not isinstance(payload, dict) or not payload.get("ok"):
        # Telegram's error shape: {"ok": false, "error_code": N, "description": "..."}
        desc = payload.get("description") if isinstance(payload, dict) else None
        raise SyncError(
            f"Telegram getUpdates returned not-ok: {desc or payload!r}"
        )

    result = payload.get("result")
    if not isinstance(result, list):
        raise SyncError(
            "Telegram getUpdates payload missing list `result` field"
        )
    return result


def send_message(
    token: str,
    chat_id: int,
    text: str,
) -> None:
    """Send a single message via Telegram's `sendMessage` HTTP API.

    The message body is interpreted as Telegram `HTML` (parse_mode=HTML),
    so callers MUST html-escape any untrusted content before composing
    the text (Secure Python Development rule #7 — escape output for
    HTML contexts). See `main.notify` for the canonical call site.

    Args:
      token: the Telegram bot token (typically
        `local_secrets.telegram_bot_token`). Never logged or included
        in raised exception messages.
      chat_id: the target chat id (Telegram int — may be negative for
        groups). MUST be an int — the caller validates.
      text: the message body. Telegram's hard cap is 4096 characters;
        anything longer is rejected by the API and surfaces as a
        SyncError so the caller can shorten and retry.

    Raises:
      SyncError on any token / network / API-shape problem. Mirrors
      `fetch_updates`'s defensive style so library callers can catch
      a single exception type for all Telegram I/O.
    """
    if not isinstance(token, str) or not token.strip():
        raise SyncError(
            "telegram_bot_token is empty — set it in local_secrets.py"
        )
    if not isinstance(chat_id, int):
        # Defensive: bool is a subclass of int but doesn't make sense as a
        # chat_id, and an int-typed chat_id is what every Telegram chat
        # actually uses. Anything else is a programmer bug at the call site.
        raise SyncError(
            f"send_message: chat_id must be int, got {type(chat_id).__name__}"
        )
    if not isinstance(text, str) or not text:
        raise SyncError("send_message: text must be a non-empty string")

    # URL-quote the token (defense in depth — Telegram tokens are
    # well-formed by spec, but we don't trust user-edited secrets blindly).
    quoted_token = urllib.parse.quote(token.strip(), safe="")
    url = f"{TELEGRAM_API_BASE}/bot{quoted_token}/sendMessage"

    # HTTPS-only — fail closed if a future refactor swaps the base.
    if not url.startswith("https://"):
        raise SyncError("Telegram API base must be HTTPS")

    # urlencode handles all the escaping; we never build raw query strings
    # by concatenation (Secure Python Development rule #7).
    form = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            # Show the link preview card — for job postings the preview
            # is genuinely useful context for the recipient.
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=form,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ypk-job-scraper/notify",
        },
    )
    try:
        # nosec: URL is built from a constant base + locally-stored bot
        # token (never end-user input), and HTTPS is enforced above.
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=TELEGRAM_HTTP_TIMEOUT_SECONDS
        ) as resp:
            status = resp.getcode()
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        # Include status but NOT the URL (which contains the token).
        raise SyncError(
            f"Telegram sendMessage HTTP {exc.code}: {exc.reason}"
        ) from None
    except urllib.error.URLError as exc:
        raise SyncError(
            f"Telegram sendMessage transport error: {exc.reason}"
        ) from None
    except TimeoutError:
        raise SyncError(
            f"Telegram sendMessage timed out after "
            f"{TELEGRAM_HTTP_TIMEOUT_SECONDS}s"
        ) from None

    if status != 200:
        raise SyncError(f"Telegram sendMessage non-200 status: {status}")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncError(
            f"Telegram sendMessage returned invalid JSON: {exc}"
        ) from None

    if not isinstance(payload, dict) or not payload.get("ok"):
        desc = payload.get("description") if isinstance(payload, dict) else None
        raise SyncError(
            f"Telegram sendMessage returned not-ok: {desc or payload!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Update-parsing & ranking.
# ─────────────────────────────────────────────────────────────────────────────


def _extract_pending_updates(
    updates: list[dict[str, Any]],
    known_profile_names: set[str],
) -> list[PendingUpdate]:
    """Walk the Telegram updates list and pick out every message that
    references a known profile.

    Inputs are validated defensively — Telegram's payload shape is
    documented, but we treat it as untrusted external data anyway
    (Secure Python Development rule #2).
    """
    pending: list[PendingUpdate] = []
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        update_id = upd.get("update_id")
        if not isinstance(update_id, int):
            continue
        # Accept the regular `message` payload as well as `edited_message`
        # (an edit to a previous message). Channel posts have no chat
        # owner to bind, so we skip them.
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            continue
        text = msg.get("text")
        if not isinstance(text, str) or not text:
            continue
        chat = msg.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            continue

        m = _PROFILE_REF_RE.search(text)
        if not m:
            continue
        name = m.group("name")
        if name not in known_profile_names:
            # Quietly skip — message references a profile that doesn't
            # exist. Verbose mode in `_main` surfaces these.
            continue

        pending.append(
            PendingUpdate(
                profile_name=name,
                chat_id=chat_id,
                update_id=update_id,
                source_text=text,
            )
        )
    return pending


def _pick_latest_per_profile(
    pending: list[PendingUpdate],
) -> dict[str, PendingUpdate]:
    """If the same profile is referenced by multiple messages, the one
    with the highest `update_id` (i.e. the most recent) wins."""
    chosen: dict[str, PendingUpdate] = {}
    for upd in sorted(pending, key=lambda p: p.update_id):
        chosen[upd.profile_name] = upd
    return chosen


# ─────────────────────────────────────────────────────────────────────────────
# profiles.yaml read + surgical edit.
# ─────────────────────────────────────────────────────────────────────────────


def load_known_profile_names(profiles_path: Path) -> set[str]:
    """Read `profiles.yaml` and return the set of valid `profile_name`s.

    Treat any malformed structure as a hard error — we don't want to
    silently sync zero profiles when the user actually broke the file.
    """
    if not profiles_path.is_file():
        raise SyncError(f"profiles file not found: {profiles_path}")
    with profiles_path.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    if not isinstance(profiles, list):
        raise SyncError(
            "profiles.yaml: missing or non-list top-level `profiles:`"
        )
    names: set[str] = set()
    for p in profiles:
        if isinstance(p, dict):
            name = p.get("profile_name")
            if isinstance(name, str) and name.strip():
                names.add(name)
    return names


def _set_chat_id_in_text(
    text: str, profile_name: str, new_chat_id: int
) -> str:
    """Return `text` with the `chat_id:` line of `profile_name`
    rewritten to `chat_id: <new_chat_id>`, preserving every comment and
    the original formatting elsewhere in the file.

    Implementation: a tiny line-walking state machine that tracks "are we
    currently inside the target profile block?" by watching the
    `- profile_name:` line of every profile. The chat_id line of the
    target profile is then rewritten in place; everything else is passed
    through byte-for-byte.

    Raises KeyError if the target profile has no `chat_id:` line to
    update — that means the schema-add step never ran, and the caller
    should fix the file first.
    """
    if not _CHAT_ID_RE.match(str(new_chat_id)):
        raise ValueError(
            f"chat_id must be an integer (incl. negatives), got "
            f"{new_chat_id!r}"
        )

    profile_line_re = re.compile(r"^\s*-\s*profile_name:\s*(\S+)")
    chat_id_line_re = re.compile(r"^(?P<lead>\s+)chat_id:\s*(?P<rest>.*)$")

    out_lines: list[str] = []
    inside_target = False
    replaced = False
    for line in text.splitlines(keepends=True):
        prof_m = profile_line_re.match(line)
        if prof_m:
            # Strip a trailing `#comment` from the value if present.
            raw_name = prof_m.group(1).split("#", 1)[0].strip()
            inside_target = (raw_name == profile_name)
            out_lines.append(line)
            continue

        if inside_target and not replaced:
            cm = chat_id_line_re.match(line.rstrip("\n"))
            if cm:
                # Preserve the trailing inline comment if there is one
                # so devs keep the breadcrumb to this script.
                rest = cm.group("rest")
                comment = ""
                if "#" in rest:
                    _, comment_text = rest.split("#", 1)
                    comment = f"   # {comment_text.strip()}"
                newline = "\n" if line.endswith("\n") else ""
                out_lines.append(
                    f"{cm.group('lead')}chat_id: {new_chat_id}{comment}{newline}"
                )
                replaced = True
                # Once replaced, we stay inside_target=True so we don't
                # accidentally re-match another chat_id line later in
                # this profile (there shouldn't be one, but safety).
                continue

        out_lines.append(line)

    if not replaced:
        raise KeyError(
            f"no `chat_id:` line found inside profile "
            f"{profile_name!r} — add the field first (see "
            f"profiles.yaml header)"
        )
    return "".join(out_lines)


def apply_updates(
    profiles_path: Path,
    chosen: dict[str, PendingUpdate],
    dry_run: bool,
) -> list[str]:
    """Apply each PendingUpdate to `profiles.yaml` and return a list of
    human-readable change descriptions.

    When `dry_run=True`, returns the same description list but does NOT
    touch the filesystem.
    """
    if not chosen:
        return []
    text = profiles_path.read_text(encoding="utf-8")
    descriptions: list[str] = []
    for name in sorted(chosen):
        upd = chosen[name]
        try:
            new_text = _set_chat_id_in_text(text, name, upd.chat_id)
        except KeyError as exc:
            raise SyncError(str(exc)) from None
        if new_text == text:
            descriptions.append(
                f"  = profile {name!r}: chat_id already set to "
                f"{upd.chat_id} (no change)"
            )
            continue
        descriptions.append(
            f"  + profile {name!r}: chat_id <- {upd.chat_id} "
            f"(from update_id {upd.update_id})"
        )
        text = new_text
    if not dry_run:
        profiles_path.write_text(text, encoding="utf-8")
    return descriptions


# ─────────────────────────────────────────────────────────────────────────────
# Public high-level entry point.
#
# `sync_chat_ids()` wraps the full fetch -> parse -> apply flow in a single
# call so library consumers (main.py, future schedulers, tests) don't need
# to know the internal sequencing. The CLI uses this same function — there
# is exactly one code path for "sync chat_ids", which keeps the CLI and the
# main-loop integration from drifting apart.
# ─────────────────────────────────────────────────────────────────────────────


def sync_chat_ids(
    token: str,
    profiles_path: Path = PROFILES_PATH,
    dry_run: bool = False,
) -> list[str]:
    """Run one full chat_id sync cycle.

    Steps:
      1. Fetch recent updates from Telegram for the bot identified by
         `token` (HTTPS, bounded timeout — never logs the token).
      2. Discover every legal `profile_name` from `profiles_path`.
      3. Pick the most recent `profile=<name>` message per known profile.
      4. Rewrite each matching profile's `chat_id:` line in place,
         preserving all comments and the rest of the file byte-for-byte.

    Args:
      token: the Telegram bot token (typically `local_secrets.telegram_bot_token`).
      profiles_path: path to `profiles.yaml`. Defaults to the project's.
      dry_run: when True, no file is written; returned list still
        describes what would have changed.

    Returns:
      A list of one-line human-readable change descriptions. An empty
      list means no message referenced a known profile, or every
      mentioned profile already had the same chat_id.

    Raises:
      SyncError on any token / network / parsing / file error. Library
      callers should treat `SyncError` as recoverable (transient
      Telegram outage, missing token) and not crash the host process.
    """
    updates = fetch_updates(token)
    known = load_known_profile_names(profiles_path)
    pending = _extract_pending_updates(updates, known)
    chosen = _pick_latest_per_profile(pending)
    return apply_updates(profiles_path, chosen, dry_run=dry_run)


# ─────────────────────────────────────────────────────────────────────────────
# CLI.
# ─────────────────────────────────────────────────────────────────────────────


def _load_token() -> str:
    # Import inside the function so a missing local_secrets.py doesn't
    # break `--help`. Per project convention, local_secrets.py lives at
    # the project root and is git-ignored.
    try:
        import local_secrets  # noqa: WPS433 (intentional runtime import)
    except ModuleNotFoundError:
        raise SyncError(
            "local_secrets.py not found at project root — create it with "
            "a `telegram_bot_token = \"...\"` line"
        ) from None
    token = getattr(local_secrets, "telegram_bot_token", None)
    if not isinstance(token, str) or not token.strip():
        raise SyncError(
            "local_secrets.telegram_bot_token is missing or empty"
        )
    return token


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sync Telegram chat_ids into profiles.yaml. Looks for "
            "`profile=<profile_name>` in recent bot messages and writes "
            "the sender's chat_id into the matching profile."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without touching profiles.yaml",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="also print each scanned message's verdict",
    )
    args = parser.parse_args()

    try:
        token = _load_token()
    except SyncError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        known = load_known_profile_names(PROFILES_PATH)
    except SyncError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if not known:
        print(
            "no profiles defined in profiles.yaml — nothing to sync",
            file=sys.stderr,
        )
        return 0

    # In verbose mode we want to log per-message candidates; that requires
    # walking the pipeline manually instead of calling `sync_chat_ids()`.
    if args.verbose:
        try:
            updates = fetch_updates(token)
        except SyncError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"fetched {len(updates)} update(s) from Telegram")
        pending = _extract_pending_updates(updates, known)
        if not pending:
            print("no message matched `profile=<name>` for a known profile")
        for p in pending:
            preview = p.source_text.strip().replace("\n", " ")
            if len(preview) > 60:
                preview = preview[:57] + "..."
            print(
                f"  candidate: profile={p.profile_name} "
                f"chat_id={p.chat_id} update_id={p.update_id} "
                f"text={preview!r}"
            )
        chosen = _pick_latest_per_profile(pending)
        if not chosen:
            print("nothing to update.")
            return 0
        try:
            changes = apply_updates(
                PROFILES_PATH, chosen, dry_run=args.dry_run
            )
        except SyncError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        # Non-verbose: go through the one-call public surface so the CLI
        # exercises the same code path library callers use.
        try:
            changes = sync_chat_ids(
                token, PROFILES_PATH, dry_run=args.dry_run
            )
        except SyncError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not changes:
            print("nothing to update.")
            return 0

    header = "DRY-RUN — would apply:" if args.dry_run else "Applied:"
    print(header)
    for line in changes:
        print(line)

    if not args.dry_run and changes:
        print(
            "\nRemember: per project Rule 1, run "
            "`python verification/validate_profiles.py` to confirm "
            "profiles.yaml still validates."
        )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
