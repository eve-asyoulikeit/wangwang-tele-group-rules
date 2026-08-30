"""
Telegram Terms & Conditions Gate Bot  (patched)
-----------------------------------------------
Mutes an entire group by default and only lets a member send messages once
they've tapped "I Agree" on a pinned message. Covers both:
  - existing members (the group-wide mute applies to everyone the moment
    /setup_gate is run, no member list needed)
  - new joiners, whether they join directly or via an approval-required
    join request

Requires the bot to be a SUPERGROUP ADMIN with "Restrict members" and
"Pin messages" rights.

KEY MODEL (this is what the original version got wrong)
-------------------------------------------------------
Telegram has two independent layers:

  1. chat default permissions  (setChatPermissions)  -> applies to everyone
  2. per-user exceptions       (restrictChatMember)  -> overrides (1) for one user

Unlocking a user means creating an *exception* in layer 2. Two traps:

  * If you pass True for EVERY field, Telegram interprets that as "lift the
    restriction entirely". The user reverts to plain `member` status, layer 2
    no longer applies to them, and layer 1 (muted) takes over again. They end
    up muted with a 200 OK response. FULL_PERMISSIONS therefore deliberately
    keeps can_change_info / can_pin_messages / can_manage_topics False so the
    exception persists. Do not "tidy" these to True.

  * restrictChatMember is supergroup-only. setChatPermissions also works on
    basic groups. Running the gate on a basic group locks everyone with no
    way out.

Every unlock is verified against getChatMember before the user is told it
worked, and the acceptance row is only written after that verification passes.
"""

import asyncio
import html
import json
import logging
import os
import sqlite3
import time
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config (all via environment variables - see .env.example)
# ---------------------------------------------------------------------------
# Load gate-bot.env sitting next to this script, if python-dotenv is installed.
# This is the same file run.sh sources (GATE_BOT_ENV, default ~/gate-bot.env).
# Real environment variables always win over the file (override=False), so
# when run.sh has already exported everything this is a harmless no-op.
try:
    from dotenv import load_dotenv
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _ENV_CANDIDATES = [
        os.environ.get("GATE_BOT_ENV", ""),          # explicit override
        os.path.join(_SCRIPT_DIR, "gate-bot.env"),    # next to the script
        os.path.expanduser("~/gate-bot.env"),         # home dir (run.sh default)
        os.path.join(_SCRIPT_DIR, ".env"),             # legacy fallback
    ]
    _ENV_PATH = next((p for p in _ENV_CANDIDATES if p and os.path.isfile(p)), None)
    if _ENV_PATH:
        _DOTENV = f"loaded from {_ENV_PATH}" if load_dotenv(_ENV_PATH, override=False) \
            else f"python-dotenv found {_ENV_PATH} but load returned False"
    else:
        _DOTENV = ("python-dotenv installed but no gate-bot.env found "
                   f"(searched: {', '.join(p for p in _ENV_CANDIDATES if p)})")
except ImportError:
    _DOTENV = "python-dotenv NOT installed - only real environment variables are used"

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
TERMS_FILE = os.environ.get("TERMS_FILE", "terms.html")
if os.path.exists(TERMS_FILE):
    with open(TERMS_FILE, "r", encoding="utf-8") as f:
        TERMS_TEXT = f.read().strip()
    TERMS_SOURCE = f"file ({os.path.abspath(TERMS_FILE)})"
elif os.environ.get("TERMS_TEXT"):
    TERMS_TEXT = os.environ["TERMS_TEXT"]
    TERMS_SOURCE = ("TERMS_TEXT env var" if "TERMS_FILE" not in os.environ
                    else f"TERMS_TEXT env var (TERMS_FILE {TERMS_FILE} not found)")
else:
    TERMS_TEXT = (
        "Please read and accept this group's Terms &amp; Conditions before you can send "
        "messages.\n\nBy tapping 'I Agree' you confirm you have read and accept the rules "
        "of this group."
    )
    TERMS_SOURCE = "HARDCODED PLACEHOLDER - no terms file, no TERMS_TEXT set"
DB_PATH = os.environ.get("DB_PATH", "acceptances.db")
RULES_CHANNEL_URL = os.environ.get("RULES_CHANNEL_URL", "").strip()

# Where /verify_import looks for snapshot_members.py's output by default.
# Relative paths resolve against the cwd the bot was launched from - same
# gotcha as DB_PATH and TERMS_FILE above.
MEMBER_SNAPSHOT_PATH = os.environ.get("MEMBER_SNAPSHOT_PATH", "member_snapshot.json").strip()

# Point the bot at a local fake Bot API (see simulate.py) instead of Telegram.
# Leave unset in production.
BOT_API_BASE = os.environ.get("BOT_API_BASE", "").strip()

# Require an explicit "I've read the rules" tap before "I Agree" is accepted.
# NOTE: Telegram does NOT report taps on URL buttons, so the bot can never know
# whether RULES_CHANNEL_URL was actually opened. This enforces a second
# deliberate tap and gives you an auditable attestation - it is not proof of
# reading, and nothing can be.
REQUIRE_READ_ACK = os.environ.get("REQUIRE_READ_ACK", "1").strip() not in ("0", "false", "no")

# Bump this whenever the T&C text changes. Acceptances are recorded against the
# version in force at the time, so a bump invalidates every prior acceptance and
# re-gates the whole group on the next /setup_gate.
TERMS_VERSION = os.environ.get("TERMS_VERSION", "1").strip()

# Make anyone who leaves and rejoins go through the flow again. Their old
# acceptance is discarded and they are explicitly re-muted on arrival - see
# on_member_joined for why the chat default alone is not enough.
REPROMPT_ON_REJOIN = os.environ.get("REPROMPT_ON_REJOIN", "1").strip() not in ("0", "false", "no")

# Only meaningful when REQUIRE_READ_ACK is on. Minimum seconds between the
# "I've read the rules" tap and the "I Agree" tap. Without this the two-step is
# theatre - both taps can land in under a second. With it you are at least
# enforcing a dwell time, though still not proving anyone read anything.
MIN_READ_SECONDS = int(os.environ.get("MIN_READ_SECONDS", "0"))

# Send admin command output to the admin's DM instead of the group, and delete
# the triggering command message. Without this every member can read your
# ADMIN_IDS, file paths, other members' user ids and acceptance state.
# Requires the admin to have started a DM with the bot at least once; if the DM
# fails, output falls back to the group so you are never left with no answer.
PRIVATE_ADMIN_REPLIES = os.environ.get("PRIVATE_ADMIN_REPLIES", "1").strip() \
    not in ("0", "false", "no")

# Delete the admin's command message from the group after running it. Off by
# default - the simpler answer is to run admin commands in a DM with the bot,
# where nothing is posted in the group in the first place. Needs the bot's
# "Delete messages" admin right.
DELETE_ADMIN_COMMANDS = os.environ.get("DELETE_ADMIN_COMMANDS", "0").strip() \
    not in ("0", "false", "no")

# Seconds before the bot deletes its own welcome / "please accept" prompts.
# 0 keeps them forever. These are transient nudges, not content - on an active
# group they otherwise pile up. Needs the bot's "Delete messages" admin right.
PROMPT_TTL_SECONDS = int(os.environ.get("PROMPT_TTL_SECONDS", "0"))

# Minimum seconds between prompts to the SAME member. Without this, someone who
# ignores the gate and keeps posting generates a prompt per message. Their posts
# are still deleted and they stay restricted - they just are not re-nagged.
PROMPT_COOLDOWN_SECONDS = int(os.environ.get("PROMPT_COOLDOWN_SECONDS", "600"))

# Delete Telegram's own "X joined the group" service messages.
DELETE_JOIN_MESSAGES = os.environ.get("DELETE_JOIN_MESSAGES", "0").strip() \
    not in ("0", "false", "no")

# DM the admins whenever the bot starts. On a phone this is how you find out it
# crashed at 3am - a burst of these means Android is killing the process.
STARTUP_NOTIFY = os.environ.get("STARTUP_NOTIFY", "0").strip() \
    not in ("0", "false", "no")

# DM the admins when something actually fails. Defaults ON: the whole problem
# with a phone deployment is that failures land in a log nobody is reading.
ERROR_NOTIFY = os.environ.get("ERROR_NOTIFY", "1").strip() \
    not in ("0", "false", "no")
# Seconds between error DMs. A crash loop must not turn into a DM flood.
ERROR_NOTIFY_COOLDOWN = int(os.environ.get("ERROR_NOTIFY_COOLDOWN", "300"))

# Show Chinese alongside English in every member-facing string. One inline
# keyboard serves the whole group, so the gate cannot be per-user: the labels
# have to carry both languages at once. Telegram's own translate button is
# opt-in and off by default, so relying on it reaches almost nobody.
# Set BILINGUAL=0 for English only.
BILINGUAL = os.environ.get("BILINGUAL", "1").strip() not in ("0", "false", "no")


def bi(en, zh, sep="\n"):
    """English first, then Simplified Chinese. sep=" \u00b7 " for button labels,
    "\n\n" for message bodies."""
    return f"{en}{sep}{zh}" if BILINGUAL else en


# TERMS_TEXT is rendered with parse_mode="HTML" - only <b>, <i>, <u>, <s>, <code>,
# <pre>, and <a href="..."> tags are supported by Telegram.

# Everything at INFO and above goes to LOG_FILE. Only WARNING and above reaches
# the terminal, so a Termux session stays readable while the file keeps the full
# httpx/unlock trail you need when diagnosing a stuck member. Tracebacks are
# logged at ERROR, so they still print.
LOG_FILE = os.environ.get("LOG_FILE", os.path.expanduser("~/bot.log"))
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(os.environ.get("LOG_BACKUPS", "3"))

_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

_root = logging.getLogger()
_root.setLevel(logging.INFO)
for _h in list(_root.handlers):
    _root.removeHandler(_h)

try:
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                              backupCount=LOG_BACKUPS, encoding="utf-8")
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(_fmt)
    _root.addHandler(_fh)
    _LOG_FILE_STATUS = f"{LOG_FILE} (rotating, {LOG_MAX_BYTES} bytes x {LOG_BACKUPS})"
except OSError as e:
    _LOG_FILE_STATUS = f"UNAVAILABLE ({e}) - console only"

_sh = logging.StreamHandler(sys.stdout)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_fmt)
_root.addHandler(_sh)

log = logging.getLogger("tc-gate-bot")
if _LOG_FILE_STATUS.startswith("UNAVAILABLE"):
    log.warning("Log file %s", _LOG_FILE_STATUS)

AGREE_CB = "tc_agree"
READ_CB = "tc_read"



# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def db():
    # FIX: default sqlite timeout is 5s and there is no busy handler, so a burst
    # of simultaneous taps produced "database is locked" exceptions that escaped
    # the handler entirely (no toast, no unlock, no log beyond the traceback).
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS acceptances (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            accepted_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gate_messages (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS read_acks (
            chat_id INTEGER,
            user_id INTEGER,
            read_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runtime (
            k TEXT PRIMARY KEY,
            v TEXT
        )"""
    )
    # Server-side record of who actually has a join request open. The chat id in
    # callback_data is attacker-controllable (MTProto lets any client send
    # arbitrary callback bytes), so the DM approve/unlock path validates against
    # this table rather than trusting the button.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_requests (
            chat_id INTEGER,
            user_id INTEGER,
            requested_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    # Telegram's Bot API cannot resolve a @username to a user id. The only way
    # to look someone up by name is to have recorded the mapping when the bot
    # saw them. Every update we handle populates this.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen_users (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            last_seen TEXT,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    # Migrate DBs created before terms versioning. Existing rows get version ''
    # which never matches a real TERMS_VERSION, so they are treated as stale and
    # those members are re-gated rather than silently grandfathered in.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(acceptances)").fetchall()}
    if "terms_version" not in cols:
        conn.execute("ALTER TABLE acceptances ADD COLUMN terms_version TEXT DEFAULT ''")

    # A one-shot import of the FULL member list from snapshot_members.py (a
    # separate Telethon script - Bot API has no "list members" call, so this
    # is the only source of a complete roster. seen_users only covers people
    # this bot has directly observed and is NOT a substitute for this table.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS member_snapshot (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            is_bot INTEGER,
            is_deleted INTEGER,
            imported_at TEXT,
            date_joined TEXT DEFAULT '',
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    # Migrate DBs created before the date_joined column was added.
    snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(member_snapshot)").fetchall()}
    if "date_joined" not in snap_cols:
        conn.execute("ALTER TABLE member_snapshot ADD COLUMN date_joined TEXT DEFAULT ''")
    # Which chats should receive join-request alerts. source_chat_id is the
    # group people are requesting to join; notify_chat_id is the admin
    # coordination group where the alert (with a Claim button) is posted.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS join_notify_chats (
            source_chat_id INTEGER,
            notify_chat_id INTEGER,
            PRIMARY KEY (source_chat_id, notify_chat_id)
        )"""
    )
    # Admin claim tracking: which admin has picked up a pending join request
    # so the team knows who is handling the outreach DM.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS join_claims (
            source_chat_id INTEGER,
            user_id INTEGER,
            admin_id INTEGER,
            admin_name TEXT,
            claimed_at TEXT,
            notify_message_id INTEGER,
            notify_chat_id INTEGER,
            PRIMARY KEY (source_chat_id, user_id)
        )"""
    )
    return conn


def record_read_ack(chat_id, user_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO read_acks (chat_id, user_id, read_at) VALUES (?,?,?)",
            (chat_id, user_id, datetime.now(timezone.utc).isoformat()),
        )


def read_ack_age_seconds(chat_id, user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT read_at FROM read_acks WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(row[0])).total_seconds()
    except ValueError:
        return None


def has_read_ack(chat_id, user_id) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM read_acks WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
    return row is not None


def set_gate_message(chat_id, message_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO gate_messages (chat_id, message_id) VALUES (?,?)",
            (chat_id, message_id),
        )


def runtime_get(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT v FROM runtime WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def runtime_set(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO runtime (k, v) VALUES (?,?)",
                     (key, str(value)))


def record_startup():
    """Track restarts so an unattended phone deployment is auditable."""
    prev = runtime_get("last_start", "never")
    count = int(runtime_get("restart_count", "0")) + 1
    runtime_set("last_start", datetime.now(timezone.utc).isoformat())
    runtime_set("restart_count", count)
    return prev, count


def version_drift():
    """Acceptance versions on file that are not the version now in force.

    Catches the config-drift failure: TERMS_VERSION bumped in one launch path
    but not another, so a reboot silently reverts it and acceptances you meant
    to invalidate become valid again. Returns [(version, count), ...].
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT terms_version, COUNT(*) FROM acceptances "
            "WHERE terms_version != ? GROUP BY terms_version",
            (TERMS_VERSION,)).fetchall()
    return [(v or "(blank)", n) for v, n in rows]


def known_user_ids(chat_id):
    """Everyone the bot has ever seen in this chat. The Bot API has no
    "list members" call, so this is the best roster available."""
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT user_id FROM seen_users WHERE chat_id=?", (chat_id,)).fetchall()]


def gate_chats():
    """Chats where /setup_gate has been run. Lets DM commands find their target."""
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT chat_id FROM gate_messages ORDER BY chat_id").fetchall()]


def forget_chat(chat_id):
    """Remove every trace of one chat. Used to prune groups that no longer
    exist, which otherwise make DM commands ambiguous forever."""
    with db() as conn:
        for t in ("gate_messages", "acceptances", "read_acks",
                  "seen_users", "pending_requests", "member_snapshot"):
            conn.execute(f"DELETE FROM {t} WHERE chat_id=?", (chat_id,))


def get_gate_message(chat_id):
    with db() as conn:
        row = conn.execute(
            "SELECT message_id FROM gate_messages WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row[0] if row else None


def build_message_link(chat, message_id):
    if message_id is None:
        return None
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    if chat.type == ChatType.SUPERGROUP and str(chat.id).startswith("-100"):
        return f"https://t.me/c/{str(chat.id)[4:]}/{message_id}"
    return None


def record_pending_request(chat_id, user_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_requests (chat_id, user_id, requested_at) "
            "VALUES (?,?,?)",
            (chat_id, user_id, datetime.now(timezone.utc).isoformat()),
        )


def has_pending_request(chat_id, user_id) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM pending_requests WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return row is not None


def clear_pending_request(chat_id, user_id):
    with db() as conn:
        conn.execute("DELETE FROM pending_requests WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))


def remember_user(chat_id, user):
    """Record a username -> user_id mapping the moment we see one."""
    if user is None or user.is_bot:
        return
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO seen_users "
            "(chat_id, user_id, username, full_name, last_seen) VALUES (?,?,?,?,?)",
            (chat_id, user.id, (user.username or "").lower(), user.full_name or "",
             datetime.now(timezone.utc).isoformat()),
        )


def resolve_user(chat_id, token: str):
    """Accept 12345, @name or name. Returns (user_id, note) or (None, why not).

    Falls back to the acceptances table for users recorded before seen_users
    existed - it has stored usernames since the first version.
    """
    token = token.strip()
    if token.lstrip("-").isdigit():
        return int(token), "numeric id"

    name = token.lstrip("@").lower()
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM seen_users WHERE chat_id=? AND username=?",
            (chat_id, name),
        ).fetchone()
        if row:
            return row[0], f"resolved @{name} from seen_users"
        row = conn.execute(
            "SELECT user_id FROM acceptances WHERE chat_id=? AND lower(username)=?",
            (chat_id, name),
        ).fetchone()
        if row:
            return row[0], f"resolved @{name} from acceptances"
    return None, (
        f"I have no id on file for @{name}. Telegram's Bot API cannot look up a "
        "username, so I can only resolve people I have already seen. Reply to one "
        "of their messages with /check instead, or forward one of their messages "
        "to @userinfobot to get the numeric id."
    )


def record_acceptance(chat_id, user_id, username, full_name):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO acceptances "
            "(chat_id, user_id, username, full_name, accepted_at, terms_version) "
            "VALUES (?,?,?,?,?,?)",
            (chat_id, user_id, username or "", full_name or "",
             datetime.now(timezone.utc).isoformat(), TERMS_VERSION),
        )


def forget_acceptance(chat_id, user_id):
    """Drop an acceptance and its read ack so the member must redo the flow."""
    with db() as conn:
        conn.execute("DELETE FROM acceptances WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))
        conn.execute("DELETE FROM read_acks WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))


def acceptance_age_seconds(chat_id, user_id):
    """Seconds since acceptance, or None. Used to tell a join-request approval
    (accepted seconds ago, in the DM, before joining) from a genuine rejoin."""
    with db() as conn:
        row = conn.execute(
            "SELECT accepted_at FROM acceptances WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(row[0])).total_seconds()
    except ValueError:
        return None


def has_accepted(chat_id, user_id) -> bool:
    """True only for an acceptance of the CURRENT terms version."""
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM acceptances WHERE chat_id=? AND user_id=? AND terms_version=?",
            (chat_id, user_id, TERMS_VERSION),
        ).fetchone()
    return row is not None


def accepted_user_ids(chat_id):
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT user_id FROM acceptances WHERE chat_id=? AND terms_version=?",
            (chat_id, TERMS_VERSION),
        ).fetchall()]


def count_accepted(chat_id) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM acceptances WHERE chat_id=? AND terms_version=?",
            (chat_id, TERMS_VERSION),
        ).fetchone()[0]



def import_member_snapshot(chat_id, members):
    """members: the "members" list from snapshot_members.py's JSON output.
    Replaces any prior snapshot for this chat wholesale - always the latest
    import, never a merge of two runs.

    Each member dict may include a "date_joined" ISO timestamp (from
    Telethon's ChannelParticipant.date). For members who pre-date the
    group's supergroup conversion, Telegram often reports the conversion
    date rather than the true join date.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM member_snapshot WHERE chat_id=?", (chat_id,))
        conn.executemany(
            "INSERT INTO member_snapshot "
            "(chat_id, user_id, username, full_name, is_bot, is_deleted, imported_at, date_joined) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(chat_id, m["user_id"], (m.get("username") or ""),
              f'{m.get("first_name","")} {m.get("last_name","")}'.strip(),
              int(bool(m.get("is_bot"))), int(bool(m.get("is_deleted"))), now,
              m.get("date_joined", ""))
             for m in members],
        )


def snapshot_members(chat_id, include_bots=False, include_deleted=False):
    """The imported roster, filtered to real accounts by default. Bots and
    already-deleted accounts can never reply, so counting them as
    non-responders would just be noise.

    Returns [(user_id, username, full_name, date_joined), ...].
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, full_name, is_bot, is_deleted, date_joined "
            "FROM member_snapshot WHERE chat_id=?", (chat_id,)
        ).fetchall()
    out = []
    for uid, uname, fname, is_bot, is_deleted, dj in rows:
        if is_bot and not include_bots:
            continue
        if is_deleted and not include_deleted:
            continue
        out.append((uid, uname, fname, dj or ""))
    return out


def snapshot_imported_at(chat_id):
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(imported_at) FROM member_snapshot WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Join-request notification & admin claim helpers
# ---------------------------------------------------------------------------
CLAIM_CB = "jn_claim"
APPROVE_CB = "jn_approve"


def add_notify_chat(source_chat_id, notify_chat_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO join_notify_chats "
            "(source_chat_id, notify_chat_id) VALUES (?,?)",
            (source_chat_id, notify_chat_id))


def remove_notify_chat(source_chat_id, notify_chat_id):
    with db() as conn:
        conn.execute(
            "DELETE FROM join_notify_chats WHERE source_chat_id=? AND notify_chat_id=?",
            (source_chat_id, notify_chat_id))


def get_notify_chats(source_chat_id):
    """All admin-group chat ids registered to receive alerts for this source."""
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT notify_chat_id FROM join_notify_chats WHERE source_chat_id=?",
            (source_chat_id,)).fetchall()]


def all_notify_sources():
    """Every source_chat_id that has at least one notify target. Used by
    on_join_request to decide whether to post an admin alert."""
    with db() as conn:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT source_chat_id FROM join_notify_chats").fetchall()}


def record_join_claim(source_chat_id, user_id, admin_id, admin_name,
                       notify_chat_id, notify_message_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO join_claims "
            "(source_chat_id, user_id, admin_id, admin_name, claimed_at, "
            "notify_chat_id, notify_message_id) VALUES (?,?,?,?,?,?,?)",
            (source_chat_id, user_id, admin_id, admin_name,
             datetime.now(timezone.utc).isoformat(),
             notify_chat_id, notify_message_id))


def get_join_claim(source_chat_id, user_id):
    """Returns (admin_id, admin_name, claimed_at) or None."""
    with db() as conn:
        row = conn.execute(
            "SELECT admin_id, admin_name, claimed_at FROM join_claims "
            "WHERE source_chat_id=? AND user_id=?",
            (source_chat_id, user_id)).fetchone()
    return row if row else None


def clear_join_claim(source_chat_id, user_id):
    with db() as conn:
        conn.execute(
            "DELETE FROM join_claims WHERE source_chat_id=? AND user_id=?",
            (source_chat_id, user_id))


def pending_join_claims(source_chat_id):
    """All unclaimed pending requests: people in pending_requests who have no
    claim row yet."""
    with db() as conn:
        return conn.execute(
            "SELECT p.user_id, s.username, s.full_name, p.requested_at "
            "FROM pending_requests p "
            "LEFT JOIN seen_users s ON s.chat_id = p.chat_id AND s.user_id = p.user_id "
            "LEFT JOIN join_claims j ON j.source_chat_id = p.chat_id AND j.user_id = p.user_id "
            "WHERE p.chat_id=? AND j.user_id IS NULL "
            "ORDER BY p.requested_at",
            (source_chat_id,)).fetchall()


# ---------------------------------------------------------------------------
# Permission sets
# ---------------------------------------------------------------------------
# NOTE: the last three MUST stay False. See the module docstring - passing True
# for every field lifts the exception and drops the user back onto the muted
# chat default.
# ---------------------------------------------------------------------------
# Permission sets
# ---------------------------------------------------------------------------
# HOW THE GATE WORKS - read this before changing anything here.
#
# Telegram has two layers, and they do NOT combine the way you might expect:
#
#   1. chat default permissions  (setChatPermissions)  - applies to everyone
#   2. per-user exception        (restrictChatMember)  - applies to one member
#
# A per-user exception can only SUBTRACT rights. It cannot grant a right the
# chat default denies. Effective right = chat default AND per-user exception.
# Verified by hand in Telegram's own Group Permissions > Exceptions UI: with
# "Send Messages" off at chat level, the per-user toggle cannot be turned on.
#
# So the gate must NOT mute the chat. Muting the chat is unrecoverable - no
# exception can ever unmute anyone, and restrictChatMember still returns 200 OK
# and still reports can_send_messages=True, so it fails completely silently.
#
# Instead: leave the chat default OPEN, and restrict each unaccepted member
# individually. Accepting lifts their restriction, and they fall back to the
# open default. This is the same design Shieldy, Rose and AutomuterBot use.

# Chat default while the gate is up: ordinary member rights.
OPEN_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
    can_change_info=False,
    can_pin_messages=False,
    can_manage_topics=False,
)

# Applied to one member who has not accepted yet.
LOCKED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_invite_users=False,
    can_change_info=False,
    can_pin_messages=False,
    can_manage_topics=False,
)

# Every field True. The Bot API treats this as "lift restrictions from a user":
# the exception is deleted and they revert to plain member status, inheriting
# OPEN_PERMISSIONS above. Under the old (broken) design this was a trap; under
# this design it is exactly what unlocking means.
LIFT_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
    can_change_info=True,
    can_pin_messages=True,
    can_manage_topics=True,
)

# Kept for compatibility with older call sites.
FULL_PERMISSIONS = LIFT_PERMISSIONS

INDEPENDENT_PERMS = True


def config_summary():
    """Resolved config, token redacted. Shows what the bot ACTUALLY loaded,
    which is not always what you think you set - relative paths in particular
    resolve against the working directory, not the script directory."""
    tok = BOT_TOKEN.split(":")[0] if ":" in BOT_TOKEN else "?"
    using_file = TERMS_SOURCE.startswith("file")
    placeholder = TERMS_SOURCE.startswith("HARDCODED")
    db_exists = os.path.exists(DB_PATH)

    lines = [
        f"cwd:                {os.getcwd()}",
        f"script dir:         {os.path.dirname(os.path.abspath(__file__))}",
        f"dotenv:             {_DOTENV}",
        f"log file:           {_LOG_FILE_STATUS}",
        f"started:            {runtime_get('last_start', '?')}",
        f"restart count:      {runtime_get('restart_count', '?')}",
        "",
        f"BOT_TOKEN:          {tok}:***REDACTED*** (len {len(BOT_TOKEN)})",
        f"ADMIN_IDS:          {sorted(ADMIN_IDS) or 'EMPTY - nobody can run commands'}",
        "",
        f"terms source:       {TERMS_SOURCE}",
        f"terms length:       {len(TERMS_TEXT)} chars",
        f"terms first line:   {TERMS_TEXT.splitlines()[0][:80] if TERMS_TEXT else '(empty)'}",
    ]
    if using_file:
        lines.append(f"TERMS_FILE:         {os.path.abspath(TERMS_FILE)}")
    lines += [
        "",
        f"DB_PATH:            {DB_PATH}",
        f"  -> absolute:      {os.path.abspath(DB_PATH)}",
        f"  -> exists:        {db_exists}",
        "",
        f"RULES_CHANNEL_URL:  {RULES_CHANNEL_URL or '(unset - no Read Full Rules button)'}",
        f"REQUIRE_READ_ACK:   {REQUIRE_READ_ACK}",
        f"MIN_READ_SECONDS:   {MIN_READ_SECONDS}"
        + ("  (ignored, REQUIRE_READ_ACK is off)" if not REQUIRE_READ_ACK else ""),
        f"TERMS_VERSION:      {TERMS_VERSION!r}",
        f"REPROMPT_ON_REJOIN: {REPROMPT_ON_REJOIN}",
        f"PROMPT_COOLDOWN:    {PROMPT_COOLDOWN_SECONDS}s",
        f"DELETE_JOIN_MSGS:   {DELETE_JOIN_MESSAGES}",
        f"PROMPT_TTL_SECONDS: {PROMPT_TTL_SECONDS}",
        f"ERROR_NOTIFY:       {ERROR_NOTIFY} (cooldown {ERROR_NOTIFY_COOLDOWN}s)",
        f"BILINGUAL:          {BILINGUAL} (EN + Simplified Chinese)"
        + ("  (prompts never deleted)" if not PROMPT_TTL_SECONDS else ""),
        f"BOT_API_BASE:       {BOT_API_BASE or '(unset - talking to real Telegram)'}",
    ]

    drift = version_drift()
    if drift:
        lines.append("")
        lines.append("other acceptance versions on file:")
        for v, n in drift:
            lines.append(f"  {v}: {n} row(s)")

    warn = []
    if drift:
        newer = [v for v, _ in drift if v > TERMS_VERSION]
        warn.append(
            f"Acceptances exist under version(s) {', '.join(v for v, _ in drift)} "
            f"but TERMS_VERSION is now {TERMS_VERSION!r}."
            + (" A HIGHER version is on file - TERMS_VERSION has probably been "
               "reverted by a launch path that does not set it (check your boot "
               "script and gate-bot.env). Members you re-gated are silently "
               "accepted again." if newer else
               " That is expected right after a deliberate bump."))
    if not ADMIN_IDS:
        warn.append("ADMIN_IDS is empty - every admin command will be refused.")
    if placeholder:
        warn.append("Terms text is the built-in placeholder. Set TERMS_TEXT (if your "
                    "full T&C lives in a channel) or TERMS_FILE (if it lives in the "
                    "pinned message). What you pin IS your consent record.")
    if using_file and not os.path.isabs(TERMS_FILE):
        warn.append("TERMS_FILE is relative and resolves against the cwd above. "
                    "Launching from elsewhere silently changes which file is read.")
    if not os.path.isabs(DB_PATH):
        warn.append("DB_PATH is relative and resolves against the cwd above. "
                    "Launching from elsewhere silently creates a DIFFERENT database, "
                    "losing every recorded acceptance.")
    if not RULES_CHANNEL_URL and not using_file and not placeholder:
        warn.append("RULES_CHANNEL_URL is unset, so there is no link to the full "
                    "terms - members only ever see the short blurb in TERMS_TEXT.")
    if using_file and RULES_CHANNEL_URL:
        warn.append("Both TERMS_FILE and RULES_CHANNEL_URL are set. Two sources for "
                    "the same terms - make sure they agree, or drop one.")
    if BOT_API_BASE:
        warn.append("BOT_API_BASE is set - SIMULATION MODE, not talking to Telegram.")
    if warn:
        lines.append("")
        lines += ["WARNINGS:"] + [f"  ! {w}" for w in warn]
    return "\n".join(lines)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def resolve_target_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Which chat does this command act on? Returns (chat_id, error, args).

    In a group: that group, arguments untouched.
    In a DM: the single gated chat on file, or one named as the first argument
    when several exist. The consumed argument is stripped from args so callers
    like /check still see their own arguments in position 0.
    """
    args = list(context.args or [])
    if update.effective_chat.type != ChatType.PRIVATE:
        return update.effective_chat.id, None, args

    if args and args[0].startswith("-") and args[0].lstrip("-").isdigit():
        return int(args.pop(0)), None, args

    chats = gate_chats()
    if len(chats) == 1:
        return chats[0], None, args
    if not chats:
        return None, ("No gated group on file yet. Run /setup_gate in the group "
                      "itself first - that is the one command that must run there, "
                      "since it posts the rules message."), args
    listed = ", ".join(str(c) for c in chats)
    return None, (f"Several gated groups on file ({listed}). Put the chat id first, "
                  f"e.g. /resync {chats[0]}"), args


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      text: str, **kw):
    """Reply to an admin privately when possible.

    Admin command output leaks ADMIN_IDS, paths, member ids and acceptance
    state. None of that belongs in a group message every member can read.
    """
    if PRIVATE_ADMIN_REPLIES:
        try:
            await context.bot.send_message(update.effective_user.id, text, **kw)
            return
        except TelegramError as e:
            log.warning("DM to admin %s failed (%s) - falling back to the group. "
                        "They should message the bot once to open a DM.",
                        update.effective_user.id, e)
    # Plain send, NOT reply_text: admin_guard has usually already deleted the
    # command message by this point, and replying to a deleted message raises
    # "Message to be replied not found".
    try:
        await context.bot.send_message(update.effective_chat.id, text, **kw)
    except TelegramError as e:
        log.error("Could not deliver admin output to chat %s: %s",
                  update.effective_chat.id, e)


async def admin_reply_long(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           text: str, chunk_size: int = 3500):
    """Like admin_reply, but splits on line boundaries across multiple
    messages when text exceeds Telegram's ~4096-char single-message limit.
    admin_reply's plain send_message() has no chunking of its own - past
    that limit it just fails, and only logs the failure rather than telling
    the admin anything went wrong. Full member lists for a group this size
    need this; admin_reply alone does not scale to them.
    """
    lines = text.split("\n")
    chunks, current = [], ""
    for line in lines:
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > chunk_size and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"[{i}/{total}]\n" if total > 1 else ""
        await admin_reply(update, context, prefix + chunk)
        if i < total:
            await asyncio.sleep(0.3)


async def admin_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """False if the caller may not run admin commands.

    Refuses silently in groups: a visible 'only admins' reply confirms the
    command surface to anyone probing it. Also deletes the command message when
    replies are private, so the command itself is not left on display.
    """
    if not is_admin(update.effective_user.id):
        if update.effective_chat.type == ChatType.PRIVATE:
            await update.message.reply_text("Only a configured admin can run this.")
        else:
            log.info("Ignored admin command from non-admin user_id=%s",
                     update.effective_user.id)
        return False

    if DELETE_ADMIN_COMMANDS and update.effective_chat.type != ChatType.PRIVATE:
        try:
            await context.bot.delete_message(update.effective_chat.id,
                                             update.message.message_id)
        except TelegramError:
            pass  # needs the 'Delete messages' admin right; not fatal
    return True


# Each label is stored as its two halves. The button shows both; a sentence
# written in one language quotes only its own half, or the prompt reads as a
# wall of duplicated text.
def agree_en():
    return "2️⃣ I Agree" if REQUIRE_READ_ACK \
        else "✅ I have read and agree to the Terms"


def agree_zh():
    # No emoji: the English half already carries it and the button shows both.
    return "我同意" if REQUIRE_READ_ACK else "我已阅读并同意条款"


def read_en():
    return "1️⃣ I've read the rules"


def read_zh():
    return "我已阅读规则"


def agree_label():
    return bi(agree_en(), agree_zh(), " · ")


def read_label():
    return bi(read_en(), read_zh(), " · ")


def gate_instruction():
    """One sentence telling a member exactly what to tap, matching the keyboard
    that gate_keyboard() actually renders. Never hardcode button text elsewhere -
    it drifts the moment REQUIRE_READ_ACK changes."""
    if REQUIRE_READ_ACK:
        return (f"tap '{read_en()}' and then '{agree_en()}'. This message will disappear 5 minutes later, refer to pinned message if you need more time to read the rules and agree to the TnCs.")
    return f"tap '{agree_en()}'"


def gate_instruction_zh():
    """Chinese counterpart of gate_instruction(). Kept as a separate function
    for the same reason: change the keyboard and both must change together."""
    if REQUIRE_READ_ACK:
        return (f"请点击“{read_zh()}”，然后点击“{agree_zh()}”。"
                "本消息将在 5 分钟后自动删除；若需更多时间阅读规则并同意条款，"
                "请参阅置顶消息。")
    return f"请点击“{agree_zh()}”"


def gate_keyboard(suffix: str = ""):
    """suffix is ':<chat_id>' for the join-request DM flow, '' for in-group."""
    rows = []
    if RULES_CHANNEL_URL:
        rows.append([InlineKeyboardButton(
            bi("📖 Read Full Rules", "阅读完整规则", " · "),
            url=RULES_CHANNEL_URL)])
    if REQUIRE_READ_ACK:
        rows.append([InlineKeyboardButton(
            read_label(), callback_data=f"{READ_CB}{suffix}")])
        rows.append([InlineKeyboardButton(
            agree_label(), callback_data=f"{AGREE_CB}{suffix}")])
    else:
        rows.append([InlineKeyboardButton(
            agree_label(), callback_data=f"{AGREE_CB}{suffix}")])
    return InlineKeyboardMarkup(rows)


async def safe_answer(query, text=None, show_alert=False, fallback_chat_id=None,
                      bot=None, mention=None):
    """Answer a callback query.

    Telegram invalidates a callback query after a short window, so a slow handler
    or an update replayed after a restart produces "query is too old ... or query
    ID is invalid". The unlock has already been applied by then - only the user's
    confirmation is lost. Where a fallback chat is given, say it there instead so
    the member is not left staring at a spinner that never resolves.
    """
    """Best-effort toast. A stale callback query must not crash the handler."""
    try:
        if text:
            await query.answer(text, show_alert=show_alert)
        else:
            await query.answer()
    except Exception as e:
        who = getattr(getattr(query, "from_user", None), "id", "?")
        log.warning("answer_callback_query failed for user_id=%s chat_id=%s "
                    "(expired or stale tap): %s", who, fallback_chat_id or "?", e)
        if text and fallback_chat_id and bot:
            who = f"{mention} - " if mention else ""
            await send_prompt(bot, fallback_chat_id, f"{who}{text}", parse_mode="HTML")


# ---------------------------------------------------------------------------
# The one function that actually unlocks somebody
# ---------------------------------------------------------------------------
_chat_perm_cache = {}   # chat_id -> (monotonic_ts, can_send_messages|None)
CHAT_PERM_TTL = 120


async def chat_allows_sending(bot, chat_id):
    """Cached chat-default lookup. Called on every unlock, and the value changes
    only when an admin edits group permissions - so a short cache removes an API
    round trip from the path that races Telegram's callback-query timeout."""
    now = time.monotonic()
    hit = _chat_perm_cache.get(chat_id)
    if hit and now - hit[0] < CHAT_PERM_TTL:
        return hit[1]
    try:
        info = await bot.get_chat(chat_id)
        val = bool(info.permissions and info.permissions.can_send_messages)
    except TelegramError:
        val = None
    _chat_perm_cache[chat_id] = (now, val)
    return val


_pending_deletes = set()
_active_prompts = {}   # (chat_id, user_id) -> message_id of their pending nudge
_last_prompted = {}   # (chat_id, user_id) -> monotonic seconds
# Members with an accept-tap currently being processed. A tap on a phone over
# 4G runs several sequential API calls, during which the button keeps spinning
# and the user taps again - each repeat re-ran the whole unlock and posted
# another confirmation. Holding this for the full handler makes it idempotent.
_agree_in_flight = set()   # {(chat_id, user_id)}
_last_notify = {}     # throttle key -> monotonic seconds


async def notify_admins(bot, text, key="error"):
    """DM every admin, throttled per key.

    Failures on this deployment are invisible otherwise: the process runs on a
    phone, writes to a file, and nobody reads the file until something is
    already broken. Throttled because a crash loop would otherwise send one DM
    per failed update.
    """
    if not ERROR_NOTIFY:
        return
    now = time.monotonic()
    last = _last_notify.get(key)
    if last is not None and now - last < ERROR_NOTIFY_COOLDOWN:
        return
    _last_notify[key] = now
    for uid in sorted(ADMIN_IDS):
        try:
            await bot.send_message(uid, text[:900])
        except TelegramError as e:
            log.warning("admin notice to %s failed: %s", uid, e)


def prompt_due(chat_id, user_id) -> bool:
    """False if this member was prompted recently. In-memory by design - a
    restart re-prompts everyone once, which is the harmless direction."""
    if PROMPT_COOLDOWN_SECONDS <= 0:
        return True
    key = (chat_id, user_id)
    now = time.monotonic()
    last = _last_prompted.get(key)
    if last is not None and now - last < PROMPT_COOLDOWN_SECONDS:
        return False
    _last_prompted[key] = now
    return True


async def send_prompt(bot, chat_id, text, for_user=None, expire=True, **kw):
    """Send a prompt, transient by default.

    Removed on whichever comes first: the member accepting (see clear_prompt) or
    PROMPT_TTL_SECONDS elapsing. Pass for_user to enable the former.

    Pass expire=False to skip the TTL deletion entirely - for messages meant
    to stay as a durable record (e.g. verify_nudge posting into a dedicated
    topic) rather than a transient reminder in the main chat.
    """
    try:
        m = await bot.send_message(chat_id, text, **kw)
    except TelegramError as e:
        log.warning("prompt send failed in chat %s: %s", chat_id, e)
        return None

    if for_user is not None:
        _active_prompts[(chat_id, for_user)] = m.message_id

    if PROMPT_TTL_SECONDS > 0 and expire:
        async def _expire():
            try:
                await asyncio.sleep(PROMPT_TTL_SECONDS)
                await bot.delete_message(chat_id, m.message_id)
                _active_prompts.pop((chat_id, for_user), None)
            except TelegramError as e:
                log.debug("prompt cleanup failed (harmless): %s", e)
            except asyncio.CancelledError:
                pass

        # Keep a reference; a bare create_task can be garbage collected mid-flight.
        task = asyncio.create_task(_expire())
        _pending_deletes.add(task)
        task.add_done_callback(_pending_deletes.discard)
    return m


async def clear_prompt(bot, chat_id, user_id):
    """Delete a member's outstanding nudge - they have just accepted, so it is
    now stale and confusing."""
    mid = _active_prompts.pop((chat_id, user_id), None)
    if mid is None:
        return
    try:
        await bot.delete_message(chat_id, mid)
    except TelegramError as e:
        log.debug("prompt already gone (harmless): %s", e)


async def relock_member(bot, chat_id: int, user_id: int):
    """Restrict one member so they cannot post until they accept.

    This is what the gate is made of. The chat default stays open; only
    unaccepted members carry a restriction.
    """
    try:
        pre = await bot.get_chat_member(chat_id, user_id)
    except TelegramError as e:
        return False, f"getChatMember failed: {e}"
    if pre.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return True, "admin/owner - not gated"
    if pre.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return False, f"not a member (status={pre.status})"

    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=LOCKED_PERMISSIONS,
            use_independent_chat_permissions=INDEPENDENT_PERMS,
        )
        return True, "restricted"
    except (BadRequest, Forbidden) as e:
        return False, f"relock rejected: {e}"
    except TelegramError as e:
        return False, f"relock failed: {e}"


async def unlock_member(bot, chat_id: int, user_id: int, pre=None):
    """Lift a member's restriction so they inherit the open chat default.

    Returns (ok, detail). Callers must not report success or write an acceptance
    row unless ok is True.
    """
    if pre is None:
        try:
            pre = await bot.get_chat_member(chat_id, user_id)
        except TelegramError as e:
            return False, f"getChatMember failed: {e}"

    if pre.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return True, "admin/owner, never restricted"
    if pre.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return False, f"not currently a member (status={pre.status})"

    for attempt in range(3):
        try:
            await bot.restrict_chat_member(
                chat_id, user_id,
                permissions=LIFT_PERMISSIONS,
                use_independent_chat_permissions=INDEPENDENT_PERMS,
            )
            break
        except RetryAfter as e:
            log.warning("restrict_chat_member rate limited, sleeping %s", e.retry_after)
            await asyncio.sleep(float(e.retry_after) + 1)
        except (BadRequest, Forbidden) as e:
            msg = str(e).lower()
            if "user not found" in msg or "participant_id_invalid" in msg:
                await asyncio.sleep(1.5)
                continue
            return False, f"restrict_chat_member rejected: {e}"
        except TelegramError as e:
            return False, f"restrict_chat_member failed: {e}"
    else:
        return False, "restrict_chat_member did not succeed after retries"

    # The chat default is what actually governs them once the exception is gone,
    # so verify BOTH: no blocking exception, and a default that permits sending.
    m = None
    verify_error = None
    for attempt in range(2):
        try:
            m = await bot.get_chat_member(chat_id, user_id)
            verify_error = None
            break
        except TelegramError as e:
            verify_error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.0)
        except Exception as e:  # noqa: BLE001 - the write already landed
            verify_error = f"{type(e).__name__}: {e}"
            log.exception("Non-Telegram error verifying unlock for user_id=%s", user_id)
            await asyncio.sleep(1.0)

    if m is None:
        return True, f"UNVERIFIED - lift succeeded but readback failed ({verify_error})"

    default_ok = await chat_allows_sending(bot, chat_id)

    if m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return True, "admin/owner"

    if m.status == ChatMemberStatus.MEMBER:
        if default_ok is False:
            # The fatal case. No exception can override this - the chat itself
            # is muted and every member is stuck until the default is restored.
            return False, ("restriction lifted, but the CHAT DEFAULT still denies "
                           "sending - run /setup_gate to restore it")
        return True, "restriction lifted; inherits open chat default"

    if m.status == ChatMemberStatus.RESTRICTED:
        if not getattr(m, "is_member", True):
            return False, "exception cleared but user is NOT in the group"
        if getattr(m, "can_send_messages", False) and default_ok is not False:
            return True, "residual exception but sending permitted"
        return False, "still restricted from sending"

    return False, f"unexpected member status after unlock: {m.status}"


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram offers /start for every bot. Handling it gives the user feedback
    and, more importantly, opening the DM is what lets the bot message them
    later - bots cannot initiate conversations."""
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE:
        return  # /start in a group is noise; ignore it
    if is_admin(user.id):
        await update.message.reply_text(
            "DM open. You can run every admin command right here except "
            "/setup_gate - nothing gets posted in the group this way.\n\n"
            "/setup_gate - lock the group and post the rules message\n"
            "/resync - re-apply exceptions for everyone who accepted\n"
            "/check <id|@name> - what Telegram reports for one member\n"
            "/status - how many have accepted\n"
            "/config - resolved configuration\n"
            "/unlock - remove the gate entirely"
        )
    else:
        await update.message.reply_text(
            "Hi. I manage the terms & conditions gate for the group.\n\n"
            + bi("To send messages there, open the pinned rules message in the "
                 f"group and {gate_instruction()}.",
                 f"若要在群组内发言，请打开群组置顶的规则消息，{gate_instruction_zh()}。",
                 "\n\n")
        )


async def cmd_setup_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not await admin_guard(update, context):
        return

    # FIX: basic groups accepted setChatPermissions but reject restrictChatMember,
    # so the old check ("group", "supergroup") could mute everyone permanently.
    if chat.type == ChatType.PRIVATE:
        await admin_reply(update, context,
            "/setup_gate has to run in the group itself - it posts and pins the "
            "rules message there. Every other admin command works from this DM."
        )
        return

    if chat.type != ChatType.SUPERGROUP:
        await admin_reply(update, context, 
            "This gate only works in a supergroup. restrictChatMember (the call that "
            "unlocks individual members) is not available in basic groups, so setting "
            "the gate up here would mute everyone with no way out.\n\n"
            "Convert the group to a supergroup first (enabling chat history for new "
            "members, or setting a public link, does it), then re-run /setup_gate."
        )
        return

    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if not getattr(me, "can_restrict_members", False):
        await admin_reply(update, context, 
            "I need to be an admin with 'Restrict members' (and ideally 'Pin messages') "
            "rights before I can set up the gate."
        )
        return

    # FIX: post the gate message FIRST. The old order locked the group and then
    # posted; if send_message threw (e.g. malformed HTML in terms.html) the group
    # was left muted with no button anywhere.
    if len(TERMS_TEXT) > 4000:
        await admin_reply(update, context,
            f"Your terms text is {len(TERMS_TEXT)} characters. Telegram caps a "
            "message at 4096, so it cannot be posted as one gate message.\n\n"
            "Either shorten it to a blurb and link the full text with "
            "RULES_CHANNEL_URL, or post the full terms as its own message in the "
            "group, pin it, and point RULES_CHANNEL_URL at its t.me link."
        )
        return

    try:
        msg = await context.bot.send_message(
            chat_id=chat.id,
            text=f"📋 {TERMS_TEXT}",
            parse_mode="HTML",
            reply_markup=gate_keyboard(),
        )
    except TelegramError as e:
        await admin_reply(update, context, 
            f"⚠️ Could not post the rules message, so I did NOT lock the group. Error: {e}\n"
            "If this mentions parse entities, check the HTML in your terms file - Telegram "
            "only supports <b>, <i>, <u>, <s>, <code>, <pre> and <a href>."
        )
        return

    set_gate_message(chat.id, msg.message_id)

    try:
        await context.bot.set_chat_permissions(
            chat.id, OPEN_PERMISSIONS, use_independent_chat_permissions=INDEPENDENT_PERMS
        )
    except TelegramError as e:
        log.warning("set_chat_permissions failed: %s", e)
        await admin_reply(update, context,
            f"Could not set the chat default permissions. Error: {e}\n"
            "Check my admin rights and try again."
        )
        return

    fresh = await context.bot.get_chat(chat.id)
    if not (fresh.permissions and fresh.permissions.can_send_messages):
        await admin_reply(update, context,
            "The chat default still denies sending. The gate CANNOT work in that "
            "state - per-user exceptions can only remove rights, never grant them, "
            "so nobody could ever be unlocked. Enable 'Send Messages' in Group "
            "Permissions, then re-run /setup_gate."
        )
        return

    # Restrict everyone we know about who has not accepted. The Bot API cannot
    # enumerate members, so this only covers people already in seen_users -
    # anyone else is caught lazily by on_group_message when they first post.
    known = known_user_ids(chat.id)
    gated, failed = 0, 0
    for uid in known:
        if has_accepted(chat.id, uid):
            continue
        ok, detail = await relock_member(context.bot, chat.id, uid)
        if ok and detail != "admin/owner - not gated":
            gated += 1
        elif not ok:
            failed += 1
            log.warning("setup_gate could not restrict user_id=%s: %s", uid, detail)
        await asyncio.sleep(0.15)

    pinned = False
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id, disable_notification=True)
        pinned = True
    except TelegramError as e:
        log.warning("Could not pin message: %s", e)

    link = build_message_link(chat, msg.message_id)
    link_line = f"\nDirect link: {link}" if link else ""

    already = count_accepted(chat.id)
    resync_hint = (
        f"\n\n{already} member(s) had already accepted - they were left alone."
        if already else ""
    )
    coverage = (
        f"\n\nRestricted {gated} known member(s)"
        + (f", {failed} failed" if failed else "")
        + ". Anyone I have not seen before is caught the first time they post."
    )

    if pinned:
        await admin_reply(update, context,
            "Gate is live. The chat default stays OPEN - it has to, because Telegram "
            "exceptions can only remove rights, never grant them - so unaccepted "
            "members are restricted individually instead. Rules message pinned; "
            f"members must {gate_instruction()} on it to post."
            f"{link_line}{coverage}{resync_hint}"
        )
    else:
        await admin_reply(update, context,
            "⚠️ Gate is live, but I could NOT pin the rules message (check my "
            "'Pin messages' admin right). Members will have to find it manually, "
            f"or use this link:{link_line or ' (no link available)'}"
            f"{coverage}{resync_hint}"
        )


async def collect_pending(bot, chat_id, limit=200):
    """Members the bot has seen who have not accepted and are still present.

    NOT the full membership. The Bot API has no "list members" call, so this
    covers only people recorded in seen_users - anyone who has posted, joined,
    or tapped a button since the bot started. Silent lurkers are invisible.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, full_name FROM seen_users WHERE chat_id=?",
            (chat_id,)).fetchall()

    pending = []
    for uid, uname, fname in rows:
        if has_accepted(chat_id, uid):
            continue
        try:
            m = await bot.get_chat_member(chat_id, uid)
        except TelegramError:
            continue
        if m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER,
                        ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            continue
        pending.append((uid, uname or "", fname or str(uid)))
        if len(pending) >= limit:
            break
        await asyncio.sleep(0.05)
    return pending


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pending - who the bot has seen but who has not accepted yet."""
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    pending = await collect_pending(context.bot, target)
    if not pending:
        await admin_reply(update, context,
            "Nobody outstanding among the members I have seen.\n\n"
            "Note: I can only see members who have posted, joined, or tapped a "
            "button since I started. Telegram gives bots no way to list a chat's "
            "full membership, so quiet members will not appear here.")
        return

    lines = [f"  {fname}" + (f" (@{uname})" if uname else "") + f" — {uid}"
             for uid, uname, fname in pending[:40]]
    more = f"\n  ...and {len(pending) - 40} more" if len(pending) > 40 else ""
    await admin_reply(update, context,
        f"{len(pending)} member(s) seen but not yet accepted:\n"
        + "\n".join(lines) + more
        + "\n\nI cannot DM them - Telegram does not let bots start a conversation. "
          "Use /nudge to post one message in the group that mentions them.")


async def cmd_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/nudge - post one group message mentioning everyone still outstanding."""
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    pending = await collect_pending(context.bot, target)
    if not pending:
        await admin_reply(update, context, "Nobody outstanding - nothing to nudge.")
        return

    batch = pending[:20]   # one message; more than this reads as spam
    mentions = ", ".join(
        f'<a href="tg://user?id={uid}">{html.escape(fname)}</a>'
        for uid, _, fname in batch)
    link = build_message_link(await context.bot.get_chat(target),
                              get_gate_message(target))
    where = f'<a href="{link}">the pinned rules message</a>' if link \
        else "the pinned rules message"

    await send_prompt(
        context.bot, target,
        bi(f"{mentions} — you have not accepted the group terms yet, so you "
           f"cannot post. Please {gate_instruction()} on {where}.",
           f"以上成员尚未同意群组条款，因此无法发言。"
           f"{gate_instruction_zh()}（见{where}）。", "\n\n"),
        parse_mode="HTML",
    )
    left = len(pending) - len(batch)
    await admin_reply(update, context,
        f"Nudged {len(batch)} member(s)."
        + (f" {left} still outstanding - run /nudge again to reach the next batch."
           if left else ""))


async def cmd_gates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gates - list gated chats, pruning ones the bot can no longer reach.

    Deleting a test group does not remove its rows, so they accumulate and make
    every DM command ambiguous. This verifies each one against Telegram and
    drops the dead entries along with their acceptance data.
    """
    if not await admin_guard(update, context):
        return

    chats = gate_chats()
    if not chats:
        await admin_reply(update, context,
                          "No gated chats on file. Run /setup_gate in a group.")
        return

    alive, pruned = [], []
    for cid in chats:
        try:
            info = await context.bot.get_chat(cid)
            n = count_accepted(cid)
            alive.append(f"{cid}  {info.title or '(no title)'}  [{n} accepted]")
        except (BadRequest, Forbidden) as e:
            # chat deleted, or the bot was removed from it
            forget_chat(cid)
            pruned.append(f"{cid}  ({e})")
        except TelegramError as e:
            alive.append(f"{cid}  UNVERIFIED ({e})")

    out = []
    if alive:
        out.append("Gated chats:\n" + "\n".join(f"  {a}" for a in alive))
    if pruned:
        out.append("Pruned (unreachable, data deleted):\n"
                   + "\n".join(f"  {p}" for p in pruned))
    if len(alive) > 1:
        out.append("More than one is live, so DM commands need the chat id first, "
                   "e.g. /resync <chat_id>.")
    await admin_reply(update, context, "\n\n".join(out))


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/forget <chat_id> - drop one chat and all its data, reachable or not."""
    if not await admin_guard(update, context):
        return
    args = context.args or []
    if not args:
        await admin_reply(update, context,
                          "Usage: /forget <chat_id>   (see /gates for the list)")
        return
    try:
        cid = int(args[0])
    except ValueError:
        await admin_reply(update, context, "That is not a numeric chat id.")
        return
    if cid not in gate_chats():
        await admin_reply(update, context, f"{cid} is not on file.")
        return
    forget_chat(cid)
    await admin_reply(update, context, f"Forgot {cid} and all its acceptance data.")


async def cmd_resync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lift the restriction from everyone in the acceptance table.

    Repair only. It reads accepted_user_ids() and calls unlock_member() - it
    never restricts anyone, so it cannot roll an accepted member back to gated.
    Use it when someone who accepted is still muted: an admin restricted them by
    hand, a rejoin re-gated them, or an unlock failed and was recorded as
    UNVERIFIED.

    Note it filters on the CURRENT terms version. After a TERMS_VERSION bump it
    will report nobody to resync, because prior acceptances are deliberately
    stale - that is the bump working, not a failure.
    """
    if not await admin_guard(update, context):
        return

    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return
    chat = type("T", (), {"id": target})()

    ids = accepted_user_ids(chat.id)
    if not ids:
        await admin_reply(update, context, "Nobody has accepted yet - nothing to resync.")
        return

    await admin_reply(update, context, f"Resyncing {len(ids)} member(s)...")
    ok_count, failures, successes = 0, [], []
    for uid in ids:
        ok, detail = await unlock_member(context.bot, chat.id, uid)
        if ok:
            ok_count += 1
            successes.append(uid)
            log.info("resync ok user_id=%s: %s", uid, detail)
        else:
            failures.append((uid, detail))
            log.warning("resync failed user_id=%s: %s", uid, detail)
        await asyncio.sleep(0.15)  # stay under the group-message rate limit

    text = f"Resync done: {ok_count}/{len(ids)} unlocked."
    if successes:
        text += "\n\nUnlocked ids: " + ", ".join(str(u) for u in successes[:20])
    if failures:
        preview = "\n".join(f"- {uid}: {d}" for uid, d in failures[:10])
        text += f"\n\nFailures (first 10):\n{preview}"
    await admin_reply(update, context, text)


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return
    chat = type("T", (), {"id": target})()
    try:
        await context.bot.set_chat_permissions(
            chat.id, FULL_PERMISSIONS, use_independent_chat_permissions=INDEPENDENT_PERMS
        )
    except TelegramError as e:
        await admin_reply(update, context, f"Could not restore permissions: {e}")
        return
    await admin_reply(update, context, "Default group permissions restored - gate disabled.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return
    chat = type("T", (), {"id": target})()
    n = count_accepted(chat.id)
    await admin_reply(update, context, f"{n} member(s) have accepted the terms so far.")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/config - dump resolved configuration. Token is redacted."""
    if not await admin_guard(update, context):
        return
    await admin_reply(update, context, f"<pre>{html.escape(config_summary())}</pre>",
                                    parse_mode="HTML")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/check <user_id|@username>, or reply to someone's message with /check."""
    user = update.effective_user
    chat = update.effective_chat
    if not await admin_guard(update, context):
        return

    gate_chat, err, args = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return
    chat = type("T", (), {"id": gate_chat})()

    target, how = None, ""
    reply = update.message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        target, how = reply.from_user.id, "from the replied-to message"
        remember_user(chat.id, reply.from_user)
    elif args:
        target, how = resolve_user(chat.id, args[0])
        if target is None:
            await admin_reply(update, context, how)
            return
    else:
        await admin_reply(update, context,
            "Usage: /check <user_id>  or  /check @username\n"
            "Reply to one of their messages with /check, or from a DM add the "
            "chat id first: /check <chat_id> @username"
        )
        return

    try:
        m = await context.bot.get_chat_member(chat.id, target)
    except TelegramError as e:
        await admin_reply(update, context, f"getChatMember failed for {target}: {e}")
        return

    accepted = has_accepted(chat.id, target)
    read_ack = has_read_ack(chat.id, target)
    lines = [
        f"user_id: {target}  ({how})",
        f"status: {m.status}",
        f"can_send_messages: {getattr(m, 'can_send_messages', 'n/a')}",
        f"is_member: {getattr(m, 'is_member', 'n/a')}",
        f"until_date: {getattr(m, 'until_date', 'n/a')}",
        f"read ack (v{TERMS_VERSION}): {read_ack}",
        f"accepted (v{TERMS_VERSION}): {accepted}",
    ]
    # "Can they send?" and "have they accepted?" are two different questions.
    # can_send_messages only exists on RESTRICTED members; on a plain member the
    # answer comes from the chat default instead. The old hint here assumed the
    # muted-default design and fired on the success state.
    if m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        lines.append("\n-> admin/owner: never gated, always able to send.")
    elif m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        lines.append("\n-> not in the group. Any accepted row above is stale.")
    elif m.status == ChatMemberStatus.MEMBER:
        default_ok = await chat_allows_sending(context.bot, chat.id)
        if default_ok is False:
            lines.append("\n-> CHAT DEFAULT denies sending, so nobody can post. "
                         "Re-open Send Messages in Group Permissions.")
        elif accepted:
            lines.append("\n-> unlocked and accepted. This is the correct end state.")
        else:
            lines.append("\n-> UNGATED: never restricted, has not accepted. The join "
                         "handler did not catch them; they will be caught the first "
                         "time they post.")
    elif m.status == ChatMemberStatus.RESTRICTED:
        if getattr(m, "can_send_messages", False):
            lines.append(
                "\n-> Telegram says they CAN send. If they still see a locked input box, "
                "have them force-quit and reopen Telegram, or check @SpamBot for an "
                "account-level restriction."
            )
        else:
            lines.append("\n-> gated, waiting on acceptance.")
    await admin_reply(update, context, "\n".join(lines))


# ---------------------------------------------------------------------------
# Membership verification (post-compromise roll call)
#
# seen_users is NOT a full membership list - see known_user_ids()'s docstring.
# It only covers people this bot has directly observed, so it silently misses
# anyone who has never posted, joined while the bot was running, or tapped a
# button - which includes exactly the "quiet account already in the group"
# case this is meant to catch. member_snapshot (populated by /verify_import
# from snapshot_members.py's output, a separate Telethon script - Bot API has
# no "list members" call) is the only complete roster available, and is what
# /verify_report and /verify_kick diff against. seen_users is not used here.
# ---------------------------------------------------------------------------
async def cmd_verify_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verify_import [path] - load the full member list produced by
    snapshot_members.py. Re-running replaces the prior snapshot outright."""
    if not await admin_guard(update, context):
        return
    target, err, args = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    path = args[0] if args else MEMBER_SNAPSHOT_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        await admin_reply(update, context,
            f"Could not read {path}: {e}\n\n"
            "Run snapshot_members.py first (it's a separate script - Bot API "
            "cannot enumerate members), or pass the path: /verify_import <path>")
        return
    except json.JSONDecodeError as e:
        await admin_reply(update, context, f"{path} is not valid JSON: {e}")
        return

    members = data.get("members", [])
    if not members:
        await admin_reply(update, context, f"{path} has no members - nothing imported.")
        return

    import_member_snapshot(target, members)
    bots = sum(1 for m in members if m.get("is_bot"))
    deleted = sum(1 for m in members if m.get("is_deleted"))
    await admin_reply(update, context,
        f"Imported {len(members)} member(s) from {path} "
        f"(captured {data.get('captured_at', 'unknown')}).\n"
        f"{bots} bot(s), {deleted} deleted account(s) on file - both excluded "
        "from /verify_report and /verify_kick by default, since neither can "
        "ever reply.")


async def cmd_verify_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verify_topic - create a plain topic for /verify_nudge (and other
    verification notices) to post into, so they stay out of General. Does
    NOT ask anyone to reply - that is /verify_start's job, only if you ever
    want the free-text flow. This just makes the container and remembers
    its thread id under the same runtime key /verify_nudge already checks
    automatically, so nothing further needs to be typed anywhere after this.
    """
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    try:
        chat = await context.bot.get_chat(target)
    except TelegramError as e:
        await admin_reply(update, context, f"getChat failed: {e}")
        return
    if not getattr(chat, "is_forum", False):
        await admin_reply(update, context,
            "This group doesn't have Topics enabled yet - Group Settings > "
            "Topics, then re-run this. No Bot API call can turn it on for you.")
        return

    try:
        me = await context.bot.get_chat_member(target, context.bot.id)
    except TelegramError as e:
        await admin_reply(update, context, f"getChatMember (self) failed: {e}")
        return
    if not getattr(me, "can_manage_topics", False):
        await admin_reply(update, context,
            "I need the 'Manage Topics' admin right before I can create a topic.")
        return

    existing = runtime_get(f"verify_thread_id:{target}")
    if existing is not None:
        await admin_reply(update, context,
            f"A topic is already on file (thread {int(existing)}) - /verify_nudge "
            "already uses it automatically. Nothing new created.")
        return

    try:
        topic = await context.bot.create_forum_topic(
            target, name="\U0001F4CB Account Verification Notices")
    except TelegramError as e:
        await admin_reply(update, context, f"Could not create the topic: {e}")
        return

    runtime_set(f"verify_thread_id:{target}", topic.message_thread_id)

    try:
        msg = await context.bot.send_message(
            target,
            bi("Automated notices about the ongoing membership check land here "
               "instead of the main chat. No reply needed.",
               "\u6709\u5173\u6b63\u5728\u8fdb\u884c\u7684\u6210\u5458\u6838\u5b9e\u7684"
               "\u81ea\u52a8\u901a\u77e5\u5c06\u53d1\u5e03\u5728\u6b64\u5904\uff0c"
               "\u800c\u975e\u4e3b\u804a\u5929\u3002\u65e0\u9700\u56de\u590d\u3002", "\n"),
            message_thread_id=topic.message_thread_id,
        )
        await context.bot.pin_chat_message(target, msg.message_id, disable_notification=True)
    except TelegramError as e:
        await admin_reply(update, context,
            f"Topic created but couldn't post/pin the note: {e}. "
            "It still works for /verify_nudge either way.")
        return

    await admin_reply(update, context,
        "Topic created. /verify_nudge posts into it automatically from now on - "
        "no argument needed.")


async def cmd_verify_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verify_gate - restrict every member in the imported snapshot who has
    NOT accepted the current terms - the same thing /setup_gate does for
    known_user_ids(), extended to the FULL roster from /verify_import.

    Anyone who has already accepted is skipped entirely: not re-restricted,
    not messaged, left alone. This exists for the people /setup_gate could
    never reach - someone who joined before this bot existed, or has simply
    never posted since, was never restricted and never saw the prompt. That
    is a gap in the gate's own coverage, not a refusal on their part. This
    closes it by giving them the same restriction (and the same pinned
    rules message with its Agree button) an active member already has, so
    that "still hasn't accepted" after this is a real signal instead of an
    artifact of never having been asked.

    Deliberately does NOT send a per-user message - looping a prompt over
    potentially 100+ people would flood the group exactly the way individual
    DMs would have. Restricted members can self-serve off the already-pinned
    rules message any time, and on_group_message still delivers the normal
    lazy-gate prompt the moment any of them next tries to post.
    """
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    try:
        me = await context.bot.get_chat_member(target, context.bot.id)
    except TelegramError as e:
        await admin_reply(update, context, f"getChatMember (self) failed: {e}")
        return
    if not getattr(me, "can_restrict_members", False):
        await admin_reply(update, context,
            "I need 'Restrict members' admin rights to gate anyone.")
        return

    roster = snapshot_members(target)
    if not roster:
        await admin_reply(update, context,
            "No member snapshot on file - run /verify_import first.")
        return

    gated, already, failed = 0, 0, 0
    for uid, uname, fname, _dj in roster:
        if has_accepted(target, uid):
            already += 1
            continue
        ok, detail = await relock_member(context.bot, target, uid)
        if ok and detail != "admin/owner - not gated":
            gated += 1
        elif not ok:
            failed += 1
            log.warning("verify_gate could not restrict user_id=%s: %s", uid, detail)
        await asyncio.sleep(0.15)  # stay under the group-action rate limit

    await admin_reply(update, context,
        f"Gated {gated} snapshot member(s) who had not accepted."
        + (f" {failed} failed." if failed else "")
        + f"\n{already} already-accepted member(s) left untouched.\n\n"
          "They can unlock themselves any time via the pinned rules message. "
          "/verify_report will show who still hasn't once you're ready to check.")



async def run_verify_nudge(bot, chat_id, thread_id=None):
    """Core of /verify_nudge, without any dependency on a live Telegram
    Update - so both the command handler and a scheduled job can call this
    the same way. Returns a dict describing what happened; callers decide
    how to report it (admin_reply for the command, a DM for a scheduled run).
    """
    roster = snapshot_members(chat_id)
    if not roster:
        return {"status": "no_snapshot"}

    confirmed_ids = set(accepted_user_ids(chat_id))
    outstanding = [(uid, uname, fname) for uid, uname, fname, _dj in roster
                  if uid not in confirmed_ids and uid not in ADMIN_IDS]
    if not outstanding:
        return {"status": "none_outstanding"}

    link = build_message_link(await bot.get_chat(chat_id), get_gate_message(chat_id))
    where = f'<a href="{link}">the pinned rules message</a>' if link else "the pinned rules message"

    send_kwargs = {"parse_mode": "HTML"}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id

    BATCH_SIZE = 20  # per message; more than this reads as spam
    batches = [outstanding[i:i + BATCH_SIZE] for i in range(0, len(outstanding), BATCH_SIZE)]

    sent_total, failed_batches = 0, 0
    for batch in batches:
        mentions = ", ".join(
            f'<a href="tg://user?id={uid}">{html.escape(fname or uname or str(uid))}</a>'
            for uid, uname, fname in batch)
        sent = await send_prompt(
            bot, chat_id,
            bi(f"{mentions} \u2014 you have not accepted the group terms yet, so you "
               f"cannot post. Please {gate_instruction()} on {where}.",
               f"\u4ee5\u4e0a\u6210\u5458\u5c1a\u672a\u540c\u610f\u7fa4\u7ec4\u6761\u6b3e\uff0c"
               f"\u56e0\u6b64\u65e0\u6cd5\u53d1\u8a00\u3002"
               f"{gate_instruction_zh()}\uff08\u89c1{where}\uff09\u3002", "\n\n"),
            **send_kwargs,
        )
        if sent is None:
            failed_batches += 1
            log.warning("verify_nudge batch failed to send (chat_id=%s thread_id=%s)",
                        chat_id, thread_id)
        else:
            sent_total += len(batch)
        await asyncio.sleep(1.0)  # space messages out - avoid flood limits and a message wall

    return {
        "status": "sent",
        "sent_total": sent_total,
        "outstanding_total": len(outstanding),
        "num_batches": len(batches),
        "failed_batches": failed_batches,
        "thread_id": thread_id,
    }


async def cmd_verify_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verify_nudge [thread_id] - nudge EVERY member still outstanding on
    the imported snapshot, in one run. Splits into multiple messages of up
    to 20 @-mentions each (more than that per message reads as spam), sent
    in sequence with a short delay between them - no need to re-run this
    manually to work through a long list.

    Unlike /nudge (which only reaches people already in seen_users), this
    draws from the full roster, so it can reach members the bot has never
    directly observed.

    Posted into a specific topic if you pass its thread_id. If you don't,
    and /verify_topic (or /verify_start) has already created a verification
    topic, it's reused automatically - so nudges land there instead of
    General with no extra step. Only falls back to General if neither
    exists yet.
    """
    if not await admin_guard(update, context):
        return
    target, err, args = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    thread_id = None
    if args:
        try:
            thread_id = int(args[0])
        except ValueError:
            await admin_reply(update, context, f"'{args[0]}' is not a topic thread id.")
            return
    else:
        stored = runtime_get(f"verify_thread_id:{target}")
        if stored is not None:
            thread_id = int(stored)

    result = await run_verify_nudge(context.bot, target, thread_id)

    if result["status"] == "no_snapshot":
        await admin_reply(update, context, "No member snapshot on file - run /verify_import first.")
        return
    if result["status"] == "none_outstanding":
        await admin_reply(update, context, "Nobody outstanding - nothing to nudge.")
        return

    text = (f"Nudged {result['sent_total']}/{result['outstanding_total']} member(s) across "
           f"{result['num_batches']} message(s)"
           + (f" in topic {thread_id}." if thread_id is not None else " in General."))
    if result["failed_batches"]:
        text += (f" {result['failed_batches']} message(s) failed to send - check the topic "
                 "still exists and the bot can post there.")
    await admin_reply(update, context, text)


async def cmd_verify_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verify_report - who from the imported member snapshot has accepted
    the terms and who hasn't. Requires /verify_import to have been run -
    without it there is no complete roster, only seen_users, which misses
    anyone who has never posted."""
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    roster = snapshot_members(target)
    if not roster:
        await admin_reply(update, context,
            "No member snapshot on file for this chat. Run /verify_import "
            "after generating one with snapshot_members.py.")
        return

    confirmed_ids = set(accepted_user_ids(target))
    confirmed = [(uid, uname, fname) for uid, uname, fname, _dj in roster
                if uid in confirmed_ids or uid in ADMIN_IDS]
    non_responders = [(uid, uname, fname) for uid, uname, fname, _dj in roster
                      if uid not in confirmed_ids and uid not in ADMIN_IDS]

    imported_at = snapshot_imported_at(target)
    thread_id = runtime_get(f"verify_thread_id:{target}")
    deadline = runtime_get(f"verify_deadline:{target}") if thread_id else None
    deadline_str = (datetime.fromtimestamp(float(deadline), tz=timezone.utc)
                    .strftime("%Y-%m-%d %H:%M UTC")) if deadline else "no topic round open"

    def fmt(rows):
        if not rows:
            return "  (none)"
        return "\n".join(f"  {fname}" + (f" (@{uname})" if uname else "") + f" \u2014 {uid}"
                        for uid, uname, fname in rows)

    text = (
        f"Snapshot: {len(roster)} member(s) (imported {imported_at}). "
        f"Verification deadline: {deadline_str}.\n\n"
        f"CONFIRMED ({len(confirmed)}):\n{fmt(confirmed)}\n\n"
        f"NOT CONFIRMED ({len(non_responders)}):\n{fmt(non_responders)}"
    )
    if non_responders:
        text += ("\n\nReview before removing anyone - a real but inactive member looks "
                 "identical to a bot at this stage. /verify_kick shows this same "
                 "not-confirmed list and removes nobody until you confirm.")
    await admin_reply_long(update, context, text)



async def cmd_verify_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verify_kick [confirm] - remove everyone on the current non-responder
    list (the same list /verify_report shows). Without 'confirm' this only
    previews the list and removes nobody.

    Removal is ban_chat_member immediately followed by unban_chat_member, NOT
    a bare ban. A bare ban is permanent and also blocks a future join
    REQUEST, which would silently defeat Approve New Members as the safety
    net for anyone caught here who shouldn't have been.
    """
    if not await admin_guard(update, context):
        return
    target, err, args = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    roster = snapshot_members(target)
    if not roster:
        await admin_reply(update, context, "No member snapshot on file - run /verify_import first.")
        return

    confirmed = set(accepted_user_ids(target))
    non_responders = [(uid, uname, fname) for uid, uname, fname, _dj in roster
                      if uid not in confirmed and uid not in ADMIN_IDS]

    if not non_responders:
        await admin_reply(update, context, "Nobody outstanding - nothing to remove.")
        return

    if not args or args[0].lower() != "confirm":
        preview = "\n".join(f"  {fname}" + (f" (@{uname})" if uname else "") + f" \u2014 {uid}"
                            for uid, uname, fname in non_responders)
        await admin_reply_long(update, context,
            f"This would remove {len(non_responders)} member(s):\n{preview}\n\n"
            "Nobody has been touched. Re-run as '/verify_kick confirm' to actually "
            "do it. Each removal is a soft kick (ban then immediate unban) - they "
            "can send a join request to come back, they are not blocked forever.")
        return

    removed, failed = [], []
    for uid, uname, fname in non_responders:
        try:
            await context.bot.ban_chat_member(target, uid)
            await context.bot.unban_chat_member(target, uid, only_if_banned=True)
            removed.append((uid, fname))
            # Clean kicked user from snapshot so /verify_report doesn't count
            # them as "not confirmed" on subsequent runs.
            with db() as conn:
                conn.execute(
                    "DELETE FROM member_snapshot WHERE chat_id=? AND user_id=?",
                    (target, uid))
            log.info("VERIFY KICK removed user_id=%s (%s)", uid, fname)
        except TelegramError as e:
            failed.append((uid, fname, str(e)))
            log.warning("VERIFY KICK failed for user_id=%s: %s", uid, e)
        await asyncio.sleep(0.15)  # stay under the group-action rate limit

    text = f"Removed {len(removed)}/{len(non_responders)} member(s)."
    if failed:
        preview = "\n".join(f"- {fname} ({uid}): {e}" for uid, fname, e in failed)
        text += f"\n\nFailures:\n{preview}"
    await admin_reply_long(update, context, text)


# ---------------------------------------------------------------------------
# Join-request admin notification & claim system
# ---------------------------------------------------------------------------
async def cmd_join_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/join_notify [source_chat_id] - register the CURRENT chat as the admin
    coordination group that receives alerts when someone requests to join
    <source_chat_id>. Each alert carries a "Claim" button so one admin can
    indicate they're handling the outreach DM for that member.

    Run this FROM the admin group chat. The source_chat_id is the main group
    whose join requests you want alerts for. If only one gated chat is on
    file, it's used automatically.

    /join_notify off - unregister this chat (stops alerts).
    """
    if not await admin_guard(update, context):
        return
    if update.effective_chat.type == ChatType.PRIVATE:
        await admin_reply(update, context,
            "Run this inside the admin coordination group, not in a DM.")
        return

    args = list(context.args or [])
    notify_chat_id = update.effective_chat.id

    # /join_notify off — unregister
    if args and args[0].lower() == "off":
        with db() as conn:
            conn.execute(
                "DELETE FROM join_notify_chats WHERE notify_chat_id=?",
                (notify_chat_id,))
        await admin_reply(update, context, "This chat will no longer receive join alerts.")
        return

    # Resolve which source chat to watch
    if args and args[0].lstrip("-").isdigit():
        source_chat_id = int(args[0])
    else:
        chats = gate_chats()
        if len(chats) == 1:
            source_chat_id = chats[0]
        elif not chats:
            await admin_reply(update, context,
                "No gated group on file yet. Run /setup_gate in the main group "
                "first, then come back here and re-run /join_notify.")
            return
        else:
            listed = ", ".join(str(c) for c in chats)
            await admin_reply(update, context,
                f"Multiple gated groups on file ({listed}). Specify which one: "
                f"/join_notify {chats[0]}")
            return

    add_notify_chat(source_chat_id, notify_chat_id)
    await admin_reply(update, context,
        f"Registered. Join requests for chat {source_chat_id} will now post "
        f"alerts here with a Claim button.\n\n"
        "When a new request arrives, any admin can tap Claim to indicate "
        "they'll handle the outreach DM for that person.")


async def cmd_join_claims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/join_claims - show all current claims: who is handling which pending
    member, and who is still unclaimed."""
    if not await admin_guard(update, context):
        return
    target, err, _ = resolve_target_chat(update, context)
    if err:
        await admin_reply(update, context, err)
        return

    with db() as conn:
        claimed = conn.execute(
            "SELECT j.user_id, s.username, s.full_name, j.admin_id, j.admin_name, j.claimed_at "
            "FROM join_claims j "
            "LEFT JOIN seen_users s ON s.chat_id = j.source_chat_id AND s.user_id = j.user_id "
            "WHERE j.source_chat_id=? "
            "ORDER BY j.claimed_at",
            (target,)).fetchall()
        unclaimed = conn.execute(
            "SELECT p.user_id, s.username, s.full_name, p.requested_at "
            "FROM pending_requests p "
            "LEFT JOIN seen_users s ON s.chat_id = p.chat_id AND s.user_id = p.user_id "
            "LEFT JOIN join_claims j ON j.source_chat_id = p.chat_id AND j.user_id = p.user_id "
            "WHERE p.chat_id=? AND j.user_id IS NULL "
            "ORDER BY p.requested_at",
            (target,)).fetchall()

    lines = []
    if claimed:
        lines.append(f"CLAIMED ({len(claimed)}):")
        for uid, uname, fname, aid, aname, cat in claimed:
            who = (fname or uname or str(uid)) + (f" (@{uname})" if uname else "")
            lines.append(f"  {who} — claimed by {aname} ({aid})")
    else:
        lines.append("CLAIMED: (none)")

    if unclaimed:
        lines.append(f"\nUNCLAIMED ({len(unclaimed)}):")
        for uid, uname, fname, rat in unclaimed:
            who = (fname or uname or str(uid)) + (f" (@{uname})" if uname else "")
            lines.append(f"  {who} — requested {rat or '?'}")
    else:
        lines.append("\nUNCLAIMED: (none)")

    await admin_reply_long(update, context, "\n".join(lines))


async def notify_admin_groups_of_join(bot, source_chat_id, user):
    """Post an alert with Claim + Approve buttons to every registered admin
    group. Under manual-approval mode this is the ONLY way the requester
    gets into the group - see on_admin_approve."""
    notify_chats = get_notify_chats(source_chat_id)
    if not notify_chats:
        return

    safe_name = html.escape(user.first_name or str(user.id))
    uname_str = f" (@{html.escape(user.username)})" if user.username else ""
    text = (
        f"📥 <b>New join request</b>\n\n"
        f"<a href=\"tg://user?id={user.id}\">{safe_name}</a>{uname_str}\n"
        f"User ID: <code>{user.id}</code>\n\n"
        "This person cannot see the group and has not been contacted. "
        "Tap <b>Claim</b> to say you'll reach out and question them; tap "
        "<b>Approve</b> once satisfied to admit them and send the T&C."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🙋 Claim",
            callback_data=f"{CLAIM_CB}:{source_chat_id}:{user.id}"),
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"{APPROVE_CB}:{source_chat_id}:{user.id}"),
    ]])

    for notify_cid in notify_chats:
        try:
            await bot.send_message(
                notify_cid, text,
                parse_mode="HTML",
                reply_markup=keyboard)
        except TelegramError as e:
            log.warning("join notify to chat %s failed: %s", notify_cid, e)


async def on_claim_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin taps 'Claim' on a join-request alert in the admin group."""
    query = update.callback_query
    admin = query.from_user
    data = query.data or ""

    # Parse: jn_claim:<source_chat_id>:<user_id>
    parts = data.split(":")
    if len(parts) != 3:
        await safe_answer(query, "Bad button data.", show_alert=True)
        return
    try:
        source_chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        await safe_answer(query, "Bad button data.", show_alert=True)
        return

    if not is_admin(admin.id):
        await safe_answer(query, "Only admins can claim.", show_alert=True)
        return

    existing = get_join_claim(source_chat_id, user_id)
    if existing:
        aid, aname, cat = existing
        if aid == admin.id:
            await safe_answer(query, "You already claimed this one.", show_alert=True)
        else:
            await safe_answer(query,
                f"Already claimed by {aname}.", show_alert=True)
        return

    admin_name = admin.full_name or admin.first_name or str(admin.id)
    notify_chat_id = query.message.chat_id if query.message else None
    notify_message_id = query.message.message_id if query.message else None

    record_join_claim(source_chat_id, user_id, admin.id, admin_name,
                       notify_chat_id, notify_message_id)

    # Update the alert message to show who claimed it. Keep the Approve
    # button live - claiming means "I'm questioning them", not "admit them".
    # The two are independent actions and either admin (same one or another)
    # still needs to tap Approve afterward.
    if query.message:
        try:
            original_text = query.message.text_html or query.message.text or ""
            updated = (
                original_text
                + f"\n\n🙋 <b>Claimed by {html.escape(admin_name)}</b>"
            )
            approve_only_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"{APPROVE_CB}:{source_chat_id}:{user_id}"),
            ]])
            await query.message.edit_text(
                updated, parse_mode="HTML", reply_markup=approve_only_kb)
        except TelegramError as e:
            log.warning("Could not update claim message: %s", e)

    await safe_answer(query,
        f"Claimed! You're handling the DM for user {user_id}.",
        show_alert=True)
    log.info("JOIN CLAIM admin_id=%s claimed user_id=%s for chat %s",
             admin.id, user_id, source_chat_id)


# ---------------------------------------------------------------------------
# Join flows
# ---------------------------------------------------------------------------
async def gate_is_active(bot, chat_id) -> bool:
    """The gate is on if /setup_gate has been run here.

    It can no longer be inferred from chat permissions: under the corrected
    design the chat default is deliberately OPEN while the gate is up.
    """
    return get_gate_message(chat_id) is not None


async def on_member_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when someone joins directly (invite link, no approval step).

    FIX: uses ChatMemberHandler rather than the new_chat_members service message.
    Supergroups do not reliably emit that service message, so joiners were
    sometimes never prompted at all.
    """
    cmu = update.chat_member
    if cmu is None:
        return
    chat = cmu.chat
    member = cmu.new_chat_member.user

    was_in = cmu.old_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    )
    now_in = cmu.new_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    )
    if was_in or not now_in or member.is_bot:
        return

    remember_user(chat.id, member)

    # Without this the join path leaves no distinctive trace, so a member gated
    # here is indistinguishable in the log from one caught later by LAZY GATE.
    log.info("JOIN GATE user_id=%s (%s)", member.id, member.full_name)

    if not await gate_is_active(context.bot, chat.id):
        log.info("JOIN GATE user_id=%s skipped: gate not active", member.id)
        return

    # FIX: the old code skipped anyone with an acceptance row. Because acceptances
    # were written before verification, a user whose unlock silently failed was
    # marked accepted and then never prompted again - permanently stuck. Now the
    # DB row is only a hint; the real check is whether they can actually send.
    if has_accepted(chat.id, member.id):
        age = acceptance_age_seconds(chat.id, member.id)
        # A join-request approval records the acceptance in the DM seconds before
        # the member actually appears in the group. That is not a rejoin.
        just_approved = age is not None and age < 120

        if REPROMPT_ON_REJOIN and not just_approved:
            forget_acceptance(chat.id, member.id)
            ok, detail = await relock_member(context.bot, chat.id, member.id)
            log.info("REJOIN re-gate user_id=%s relocked=%s (%s)",
                     member.id, ok, detail)
            # fall through to the welcome prompt below
        else:
            ok, detail = await unlock_member(context.bot, chat.id, member.id)
            if ok:
                return
            log.warning("re-unlock on rejoin failed user_id=%s: %s", member.id, detail)

    # The chat default is open, so a new member can post until we restrict them.
    # Do it before sending the welcome so the window is as small as possible.
    ok, detail = await relock_member(context.bot, chat.id, member.id)
    if not ok:
        log.warning("could not gate new member user_id=%s: %s", member.id, detail)

    gate_msg_id = get_gate_message(chat.id)
    link = build_message_link(chat, gate_msg_id)
    where = f'<a href="{link}">this message</a>' if link else "the pinned rules message"

    await send_prompt(
        context.bot, chat.id,
        bi(f"Welcome, {member.mention_html()}! Please {gate_instruction()} "
           f"on {where} to unlock messaging. This message will auto-delete after 5 minutes.",
           f"欢迎 {member.mention_html()}！{gate_instruction_zh()}"
           f"（见{where}），即可发言。这条信息将在5分钟后自动删除。", "\n\n"),
        for_user=member.id,
        parse_mode="HTML",
    )


async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove Telegram's own join/leave notices, which pile up during rollout."""
    if not DELETE_JOIN_MESSAGES:
        return
    msg = update.effective_message
    if msg is None:
        return
    try:
        await context.bot.delete_message(update.effective_chat.id, msg.message_id)
    except TelegramError as e:
        log.debug("could not delete service message: %s", e)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch members who were already in the group when the gate went up.

    The Bot API cannot list a chat's members, so there is no way to restrict
    everyone up front. Instead, the first time an unaccepted member posts, the
    message is deleted and they are restricted and prompted. A bot that is an
    admin receives all messages regardless of privacy mode, so this fires for
    everyone.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or user is None or user.is_bot:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await gate_is_active(context.bot, chat.id):
        return

    remember_user(chat.id, user)
    if has_accepted(chat.id, user.id):
        return

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return
    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return

    ok, detail = await relock_member(context.bot, chat.id, user.id)
    log.info("LAZY GATE user_id=%s restricted=%s (%s)", user.id, ok, detail)

    try:
        await context.bot.delete_message(chat.id, msg.message_id)
    except TelegramError as e:
        log.warning("could not delete pre-acceptance message: %s", e)

    link = build_message_link(chat, get_gate_message(chat.id))
    where = f'<a href="{link}">the pinned rules message</a>' if link \
        else "the pinned rules message"
    # In a forum, reply inside the topic they posted in. Without this the prompt
    # lands in General and they never see why their message vanished.
    thread = getattr(msg, "message_thread_id", None) if getattr(
        msg, "is_topic_message", False) else None
    if not prompt_due(chat.id, user.id):
        log.info("prompt suppressed (cooldown) for user_id=%s", user.id)
        return

    await send_prompt(
        context.bot, chat.id,
        bi(f"Hello {user.mention_html()} - please {gate_instruction()} on {where} "
           "before posting.",
           f"你好 {user.mention_html()}，发言前{gate_instruction_zh()}"
           f"（见{where}）。", "\n\n"),
        for_user=user.id,
        parse_mode="HTML",
        message_thread_id=thread,
    )



async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when the group has 'approve new members' turned on.

    MANUAL-APPROVAL MODE: the requester is NOT contacted and NOT approved
    automatically. They cannot see the group and cannot be messaged by the
    bot on their own initiative (Telegram forbids a bot DMing someone who
    has never started a conversation with it - see on_admin_approve for how
    this is worked around). An admin must first review/question them, then
    tap "✅ Approve" on the alert in the admin coordination group. Only
    after that manual approval does the bot approve their join AND send the
    T&C prompt - acceptance of which unlocks messaging. Approval into the
    group and permission to post are two separate gates now, not one.
    """
    req = update.chat_join_request
    user = req.from_user
    chat = req.chat
    remember_user(chat.id, user)
    record_pending_request(chat.id, user.id)

    # No DM to the requester here - that's the point. They stay invisible to
    # the group and uncontactable-by-bot-first-move until an admin approves.
    await notify_admin_groups_of_join(context.bot, chat.id, user)


async def on_admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin taps '✅ Approve' on a join-request alert in the admin group,
    after having reached out to and questioned the requester themselves.

    This is the ONLY thing that admits the person to the group under
    manual-approval mode. It also immediately attempts to DM them the T&C -
    this is the first time the bot messages them, which only works because
    approve_chat_join_request succeeding means Telegram now permits it (a
    freshly-approved member counts as reachable, unlike a bare pending
    requester the bot has never had a session with).
    """
    query = update.callback_query
    admin = query.from_user
    data = query.data or ""

    parts = data.split(":")
    if len(parts) != 3:
        await safe_answer(query, "Bad button data.", show_alert=True)
        return
    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        await safe_answer(query, "Bad button data.", show_alert=True)
        return

    if not is_admin(admin.id):
        await safe_answer(query, "Only admins can approve.", show_alert=True)
        return

    if not has_pending_request(chat_id, user_id):
        await safe_answer(query,
            "No pending request on file - may already be handled.",
            show_alert=True)
        return

    try:
        await context.bot.approve_chat_join_request(chat_id, user_id)
    except BadRequest as e:
        if "already" not in str(e).lower():
            log.warning("manual approve_chat_join_request failed: %s", e)
            await safe_answer(query, f"Approve failed: {e}", show_alert=True)
            return
    except TelegramError as e:
        log.warning("manual approve_chat_join_request failed: %s", e)
        await safe_answer(query, f"Approve failed: {e}", show_alert=True)
        return

    admin_name = admin.full_name or admin.first_name or str(admin.id)
    log.info("MANUAL APPROVE admin_id=%s approved user_id=%s chat_id=%s",
             admin.id, user_id, chat_id)

    # Now, and only now, DM the T&C - this is what unlocks messaging once
    # accepted. record_pending_request stays set: it's reused here to mean
    # "approved into the group, T&C not yet accepted" rather than being
    # cleared, since has_pending_request is what on_agree checks to accept
    # the tap as legitimate (see the forged-callback guard in _process_agree).
    try:
        user_chat = await context.bot.get_chat(user_id)
        safe_name = html.escape(user_chat.first_name or "there")
        group_chat = await context.bot.get_chat(chat_id)
        safe_title = html.escape(group_chat.title or "the group")
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                bi(f"Hi {safe_name}, an admin has approved your request to join "
                   f"<b>{safe_title}</b>. Tap below to accept the rules before "
                   f"you can post.",
                   f"你好 {safe_name}，管理员已批准您加入 <b>{safe_title}</b> "
                   f"的申请。请点击下方接受群组规则后即可发言。",
                   "\n\n") + f"\n\n{TERMS_TEXT}"
            ),
            parse_mode="HTML",
            reply_markup=gate_keyboard(f":{chat_id}"),
        )
        dm_status = "T&C sent by DM."
    except TelegramError as e:
        log.warning("Could not DM T&C after manual approve, user %s: %s", user_id, e)
        dm_status = ("⚠️ Could not DM them the T&C (they may need to message "
                    "the bot first, or have DMs closed) - they're in the group "
                    "but still muted until they receive and accept it.")

    # Update the alert message
    if query.message:
        try:
            original_text = query.message.text_html or query.message.text or ""
            updated = (
                original_text
                + f"\n\n✅ <b>Approved by {html.escape(admin_name)}</b> — {dm_status}"
            )
            await query.message.edit_text(
                updated, parse_mode="HTML", reply_markup=None)
        except TelegramError as e:
            log.warning("Could not update approve message: %s", e)

    await safe_answer(query, f"Approved. {dm_status}", show_alert=True)


# ---------------------------------------------------------------------------
# Button handler - shared by in-group taps and DM (join-request) taps
# ---------------------------------------------------------------------------
def read_ack_block_reason(chat_id, user_id):
    """None if the user may agree, else the alert text explaining why not."""
    age = read_ack_age_seconds(chat_id, user_id)
    if age is None:
        return bi(f"Please open the rules link first, then tap "
                  f"'{read_en()}' before agreeing.",
                  f"请先打开规则链接，然后点击“{read_zh()}”再同意。",
                  "\n\n")
    if MIN_READ_SECONDS and age < MIN_READ_SECONDS:
        wait = int(MIN_READ_SECONDS - age) + 1
        return (f"Please take a moment with the rules - you can agree in "
                f"about {wait} more second(s).")
    return None


async def on_read_ack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Records the 'I've read the rules' tap that gates the I Agree button."""
    query = update.callback_query
    user = query.from_user
    data = query.data or ""

    if ":" in data:
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            await safe_answer(query, "Bad button data - ping an admin.", show_alert=True)
            return
    else:
        if query.message is None:
            await safe_answer(query, "Couldn't identify the group - ping an admin.",
                              show_alert=True)
            return
        chat_id = query.message.chat_id

    if user.is_bot:
        await safe_answer(query)
        return

    if ":" in data and not has_pending_request(chat_id, user.id):
        log.warning("REJECTED forged/stale read ack: user_id=%s claimed chat_id=%s",
                    user.id, chat_id)
        await safe_answer(query, "No join request on file for you.", show_alert=True)
        return

    remember_user(chat_id, user)
    record_read_ack(chat_id, user.id)
    await safe_answer(
        query,
        bi(f"Noted. Now tap '{agree_en()}' to unlock messaging.",
           f"已记录。现在请点击“{agree_zh()}”以解除发言限制。", "\n\n"),
        show_alert=True,
    )


async def on_agree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for every I-Agree tap.

    Acknowledges the callback FIRST so Telegram clears the button spinner before
    any slow network call, then holds a per-member lock for the whole unlock so
    repeat taps (spinner still showing on a slow link) are absorbed instead of
    re-running the unlock. The lock is released only after the confirmation is
    sent - i.e. once messaging is actually unlocked - which is what "keep the
    toast valid until unlocked" means in practice: nothing else can interleave.
    """
    query = update.callback_query
    user = query.from_user
    data = query.data or ""

    # Anonymous admins post as GroupAnonymousBot / the channel itself; there is
    # no real user to unrestrict.
    if user.is_bot:
        await safe_answer(query, "Anonymous admins are already unrestricted.", show_alert=True)
        return

    # Which chat is this tap for? Needed to key the lock before we do anything.
    if ":" in data:
        try:
            lock_chat = int(data.split(":", 1)[1])
        except ValueError:
            lock_chat = None
    else:
        lock_chat = query.message.chat_id if query.message else None

    key = (lock_chat, user.id) if lock_chat is not None else None

    # Already processing a tap from this member in this chat? Acknowledge so the
    # spinner clears, then drop it - the first tap is still finishing.
    if key is not None and key in _agree_in_flight:
        await safe_answer(query, bi("Loading, please wait\u2026",
                                    "\u5904\u7406\u4e2d\uff0c\u8bf7\u7a0d\u5019\u2026", "\n"))
        log.info("duplicate agree tap ignored (in flight) user_id=%s chat_id=%s",
                 user.id, lock_chat)
        return

    if key is not None:
        _agree_in_flight.add(key)
    try:
        await _process_agree(update, context, query, user, data)
    finally:
        if key is not None:
            _agree_in_flight.discard(key)


async def _process_agree(update, context, query, user, data):
    if ":" in data:
        # ---- Join-request DM flow -------------------------------------------
        _, chat_id_str = data.split(":", 1)
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            await safe_answer(query, "Bad button data - ping an admin.", show_alert=True)
            return

        # SECURITY: chat_id came from callback_data, which any MTProto client can
        # forge. Without this check a member of any other chat this bot admins
        # could self-unmute there by crafting 'tc_agree:<that chat id>'.
        if not has_pending_request(chat_id, user.id):
            log.warning("REJECTED forged/stale callback: user_id=%s claimed chat_id=%s",
                        user.id, chat_id)
            await safe_answer(
                query,
                "I don't have a join request on file for you. Please request to "
                "join the group again.",
                show_alert=True,
            )
            return

        remember_user(chat_id, user)
        if REQUIRE_READ_ACK:
            blocked = read_ack_block_reason(chat_id, user.id)
            if blocked:
                await safe_answer(query, blocked, show_alert=True)
                return

        # Slow part starts here (approve + unlock + verify). Acknowledge now so
        # the button spinner clears and the user is not tempted to tap again.
        await safe_answer(query, bi("Loading, please wait\u2026",
                                    "\u5904\u7406\u4e2d\uff0c\u8bf7\u7a0d\u5019\u2026", "\n"))

        try:
            await context.bot.approve_chat_join_request(chat_id, user.id)
        except BadRequest as e:
            # Already in the group is fine; anything else is not.
            if "already" not in str(e).lower():
                log.warning("approve_chat_join_request failed: %s", e)
                await safe_answer(
                    query,
                    "I couldn't approve your join request - please ping an admin.",
                    show_alert=True,
                )
                return
        except TelegramError as e:
            log.warning("approve_chat_join_request failed: %s", e)
            await safe_answer(
                query, "I couldn't approve your join request - please ping an admin.",
                show_alert=True,
            )
            return

        # THE ORIGINAL BUG: this branch stopped here. The user was approved into
        # a group whose default permissions are muted, was told they were good to
        # go, and no per-user exception was ever created for them.
        ok, detail = await unlock_member(context.bot, chat_id, user.id)
        log.info("JOIN-REQUEST UNLOCK user_id=%s ok=%s detail=%s", user.id, ok, detail)

        if not ok:
            await safe_answer(
                query,
                "You're in the group, but I couldn't lift the mute automatically. "
                "Please ping an admin.",
                show_alert=True,
            )
            try:
                await query.edit_message_text(
                    "✅ Accepted and approved - but I could not unmute you automatically. "
                    "Please ask a group admin to run /resync."
                )
            except TelegramError:
                pass
            return

        clear_pending_request(chat_id, user.id)
        await clear_prompt(context.bot, chat_id, user.id)
        record_acceptance(chat_id, user.id, user.username, user.full_name)
        # Tap already acknowledged at the top of on_agree; the DM edit below is
        # the visible confirmation for the join-request flow.
        try:
            await query.edit_message_text(
                bi("✅ Accepted. Your join request has been approved and you can "
                   "send messages.",
                   "✅ 已接受。您的加入申请已获批准，现在可以发言了。", "\n\n")
            )
        except TelegramError as e:
            log.warning("edit_message_text failed (harmless): %s", e)
        return

    # ---- In-group flow ------------------------------------------------------
    if query.message is None:
        await safe_answer(query, "I couldn't tell which group this was - ping an admin.",
                          show_alert=True)
        return
    chat_id = query.message.chat_id
    remember_user(chat_id, user)

    if REQUIRE_READ_ACK:
        blocked = read_ack_block_reason(chat_id, user.id)
        if blocked:
            await safe_answer(query, blocked, show_alert=True)
            return

    # Slow part starts here. Acknowledge the tap before the unlock round-trips so
    # Telegram clears the spinner and the retap loop cannot start.
    await safe_answer(query, bi("Loading, please wait\u2026",
                                "\u5904\u7406\u4e2d\uff0c\u8bf7\u7a0d\u5019\u2026", "\n"))

    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
    except TelegramError as e:
        log.warning("get_chat_member failed: %s", e)
        await safe_answer(query, "Something went wrong - ping an admin.", show_alert=True)
        return

    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        record_acceptance(chat_id, user.id, user.username, user.full_name)
        await safe_answer(
            query,
            "You're a group admin, so you're already unrestricted - no action needed.",
            show_alert=True,
        )
        return

    ok, detail = await unlock_member(context.bot, chat_id, user.id, pre=member)
    log.info("IN-GROUP UNLOCK user_id=%s ok=%s detail=%s", user.id, ok, detail)

    # FIX: the original always showed the success toast, because the verification
    # block only logged. Now the toast follows the verified result.
    if not ok:
        await safe_answer(
            query,
            bi("I couldn't lift the mute for you - please ping an admin "
               "(they can run /resync).",
               "无法为您解除禁言，请联系群组管理员。", "\n"),
            show_alert=True, fallback_chat_id=chat_id, bot=context.bot,
            mention=user.mention_html(),
        )
        # They did everything right and are still muted. They are unlikely to
        # report it twice, so this one gets pushed rather than logged.
        await notify_admins(
            context.bot,
            f"\u26a0\ufe0f Unlock FAILED for {user.full_name} (id {user.id}) "
            f"in chat {chat_id}.\n{detail}\nRun /resync.",
            key=f"unlock-fail:{chat_id}",
        )
        return

    # FIX: acceptance is recorded only after a verified unlock, so a failed unlock
    # never leaves someone marked as done and silently muted.
    await clear_prompt(context.bot, chat_id, user.id)
    record_acceptance(chat_id, user.id, user.username, user.full_name)
    # The tap was already acknowledged with "Loading, please wait" before the
    # unlock, so no answer() here. The visible confirmation is the self-deleting
    # group message below (same TTL lifecycle as the join/lazy-gate prompts).
    thread = getattr(query.message, "message_thread_id", None) if getattr(
        query.message, "is_topic_message", False) else None
    # No for_user: this is a confirmation, not an outstanding nudge. It must
    # self-delete on the TTL only, never be picked up by clear_prompt (which
    # keys on for_user) if the member is later re-gated.
    await send_prompt(
        context.bot, chat_id,
        bi(f"{user.mention_html()} - thanks, you can now send messages! This message will auto-delete in 5 minutes.",
           f"{user.mention_html()} - 谢谢，您现在可以发言了！这条信息将在5分钟后自动删除。", "\n\n"),
        parse_mode="HTML",
        message_thread_id=thread,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception while processing update: %s", context.error)
    err = context.error
    await notify_admins(
        context.bot,
        f"\u26a0\ufe0f Gate bot error\n{type(err).__name__}: {err}",
        key="unhandled",
    )


SGT = timezone(timedelta(hours=8))

# Timed nudge schedule: absolute calendar moments (SGT). Anything already
# past by the time the bot starts is skipped, never fired late. Replace
# this list with future datetimes whenever you want another nudge window.
VERIFY_NUDGE_SCHEDULE = [
    # Example: datetime(2026, 9, 1, 22, 0, tzinfo=SGT),
]


async def job_verify_nudge(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled counterpart to /verify_nudge - identical logic, triggered
    by time instead of an admin typing the command. Reports the outcome by
    DM to every admin, since there is no command update to reply to."""
    data = context.job.data
    chat_id, thread_id = data["chat_id"], data.get("thread_id")
    result = await run_verify_nudge(context.bot, chat_id, thread_id)

    if result["status"] == "no_snapshot":
        text = "Scheduled /verify_nudge skipped - no member snapshot on file."
    elif result["status"] == "none_outstanding":
        text = "Scheduled /verify_nudge ran - nobody outstanding, nothing sent."
    else:
        text = (f"Scheduled /verify_nudge: {result['sent_total']}/{result['outstanding_total']} "
               f"member(s) nudged across {result['num_batches']} message(s)"
               + (f" in topic {thread_id}." if thread_id is not None else " in General."))
        if result["failed_batches"]:
            text += f" {result['failed_batches']} message(s) failed to send."

    for uid in sorted(ADMIN_IDS):
        try:
            await context.bot.send_message(uid, text)
        except TelegramError as e:
            log.warning("scheduled verify_nudge report to admin %s failed: %s", uid, e)


async def on_startup(app):
    prev, count = record_startup()
    log.info("Startup #%s (previous start: %s)", count, prev)

    for v, n in version_drift():
        if v > TERMS_VERSION:
            log.warning(
                "TERMS_VERSION is %r but %s acceptance(s) exist under %r. A launch "
                "path is not setting TERMS_VERSION - check your boot script and "
                "gate-bot.env. Those members count as accepted again.",
                TERMS_VERSION, n, v)

    if app.job_queue is None:
        log.warning(
            "job_queue is unavailable - the timed /verify_nudge schedule was NOT "
            'registered. Install the extra: pip install "python-telegram-bot[job-queue]"'
            " and restart.")
    else:
        chats = gate_chats()
        if len(chats) == 1:
            chat_id = chats[0]
            stored_thread = runtime_get(f"verify_thread_id:{chat_id}")
            thread_id = int(stored_thread) if stored_thread is not None else None
            now = datetime.now(SGT)
            scheduled = 0
            for when in VERIFY_NUDGE_SCHEDULE:
                if when <= now:
                    continue
                app.job_queue.run_once(
                    job_verify_nudge, when=when,
                    data={"chat_id": chat_id, "thread_id": thread_id},
                    name=f"verify_nudge_{when.isoformat()}",
                )
                scheduled += 1
            log.info("Scheduled %s timed /verify_nudge run(s) for chat %s (thread=%s)",
                     scheduled, chat_id, thread_id)
        elif chats:
            log.warning(
                "Multiple gated chats on file (%s) - timed /verify_nudge schedule "
                "was NOT registered; it only supports a single target chat.",
                ", ".join(str(c) for c in chats))
        # No gated chats on file yet: nothing to schedule, nothing to warn about.

    if not STARTUP_NOTIFY:
        return
    for uid in sorted(ADMIN_IDS):
        try:
            await app.bot.send_message(
                uid,
                f"Gate bot started (restart #{count}).\n"
                f"Previous start: {prev}\n"
                "Frequent restarts mean Android is killing the process - "
                "check battery optimisation and the wake lock.",
            )
        except TelegramError as e:
            log.warning("startup notice to admin %s failed: %s", uid, e)


def main():
    builder = ApplicationBuilder().token(BOT_TOKEN)
    if BOT_API_BASE:
        log.warning("Using local Bot API base %s - SIMULATION MODE", BOT_API_BASE)
        builder = builder.base_url(BOT_API_BASE)
    app = builder.post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setup_gate", cmd_setup_gate))
    app.add_handler(CommandHandler("resync", cmd_resync))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("nudge", cmd_nudge))
    app.add_handler(CommandHandler("gates", cmd_gates))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("verify_import", cmd_verify_import))
    app.add_handler(CommandHandler("verify_topic", cmd_verify_topic))
    app.add_handler(CommandHandler("verify_gate", cmd_verify_gate))
    app.add_handler(CommandHandler("verify_nudge", cmd_verify_nudge))
    app.add_handler(CommandHandler("verify_report", cmd_verify_report))
    app.add_handler(CommandHandler("verify_kick", cmd_verify_kick))
    app.add_handler(CommandHandler("join_notify", cmd_join_notify))
    app.add_handler(CommandHandler("join_claims", cmd_join_claims))
    app.add_handler(ChatMemberHandler(on_member_joined, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.StatusUpdate.NEW_CHAT_MEMBERS
                                   | filters.StatusUpdate.LEFT_CHAT_MEMBER),
        on_service_message))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
        on_group_message))
    app.add_handler(CallbackQueryHandler(on_read_ack, pattern=f"^{READ_CB}"))
    app.add_handler(CallbackQueryHandler(on_agree, pattern=f"^{AGREE_CB}"))
    app.add_handler(CallbackQueryHandler(on_claim_button, pattern=f"^{CLAIM_CB}:"))
    app.add_handler(CallbackQueryHandler(on_admin_approve, pattern=f"^{APPROVE_CB}:"))
    app.add_error_handler(on_error)

    log.info("Resolved configuration:\n%s", config_summary())
    log.info("Bot starting (long polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
