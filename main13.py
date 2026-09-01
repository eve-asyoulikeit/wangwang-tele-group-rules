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
import re
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
# This also gates the "gate is down" alerts from on_my_chat_member - turning it
# off silences those too, which is almost certainly not what you want.
ERROR_NOTIFY = os.environ.get("ERROR_NOTIFY", "1").strip() \
    not in ("0", "false", "no")

# Hours between "still running" DMs to the admins. 0 disables.
#
# This is the only way to catch the failures the bot cannot possibly report
# itself: a revoked token, a killed process, a flat battery, a phone left on
# aeroplane mode. From the outside every one of those looks identical - silence
# - and silence is indistinguishable from a quiet week. What you are watching
# for is the ping that does NOT arrive, so pick an interval you would actually
# notice a gap in. 24 is a reasonable start.
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "0") or 0)

# ---------------------------------------------------------------------------
# Join screening
# ---------------------------------------------------------------------------
# When on, the bot DMs every new join requester immediately - permitted since
# Bot API 5.5 for anyone who has sent a join request to a chat the bot
# administers with can_invite_users, even though they have never messaged it -
# links them the rules, and asks how they found the group and why they want to
# join. Their answers are attached to the admin alert, so the screening DM an
# admin used to send by hand is already answered by the time anyone looks.
#
SCREENING_ENABLED = os.environ.get("SCREENING_ENABLED", "1").strip() \
    not in ("0", "false", "no", "")

# Let an answer carrying the passphrase approve itself, with no admin involved.
#
# This is a vouching mechanism, not a spam filter, and the distinction is what
# makes it work: the keywords below are not meant to describe a good answer,
# they are a shared secret an admin hands to someone they are willing to admit.
# Knowing it stands in for "a person vouched for me". It spreads the way any
# password does - which is accepted here, because the people it spreads to are
# people a member chose to pass it to.
SCREENING_AUTO_APPROVE = os.environ.get("SCREENING_AUTO_APPROVE", "1").strip() \
    not in ("0", "false", "no", "")

# The passphrase. Case-insensitive, and matched with spacing ignored so
# "WangWang" and "Wang Wang" both count - to a person telling a friend the
# phrase, those are the same word, and a false negative here sends somebody who
# was genuinely vouched for to the back of the manual queue.
#
# Matched as plain substrings rather than tokens, so a Chinese answer works
# without word boundaries. Note the passphrase itself is Latin: a Chinese
# speaker who was told it will almost certainly type it as given, but if you
# want 汪汪 or 纪念 to pass on their own, add them here. They are deliberately
# NOT included - widening a shared secret is your decision, not a default.
SCREENING_KEYWORDS = [k.strip().lower() for k in os.environ.get(
    "SCREENING_KEYWORDS", "wang wang,memorial"
).split(",") if k.strip()]

# Answers shorter than this are treated as non-answers. Without it, "yes"
# clears the bar that asking two open questions exists to set.
SCREENING_MIN_CHARS = int(os.environ.get("SCREENING_MIN_CHARS", "25"))

# Seconds to wait after a screening reply before treating it as final and
# moving on to the next question. Mobile texters send a thought in two or
# three quick messages rather than one - without this, the first fragment
# alone became the whole answer to Q1 and the very next fragment, meant to
# finish that same thought, was filed as the answer to Q2 instead, and Q3
# fired before the person had said why they wanted to join at all. A message
# arriving inside the window resets it, so the wait is "since their last
# message," not a hard cap - only silence this long is treated as "done
# typing." 0 disables debouncing and reverts to advancing on every message
# immediately (also the automatic fallback if job_queue is unavailable).
SCREENING_DEBOUNCE_SECONDS = float(os.environ.get("SCREENING_DEBOUNCE_SECONDS", "8") or 0)
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
# unlock trail you need when diagnosing a stuck member. Tracebacks are logged at
# ERROR, so they still print. The per-request httpx lines are deliberately NOT
# part of that trail - see the httpx filter below the handler setup.
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

# httpx logs one line per HTTP request at INFO, and the URL in that line carries
# the bot token: https://api.telegram.org/bot<TOKEN>/getUpdates. Long polling
# therefore wrote the token into LOG_FILE every few seconds - thousands of copies
# a day, churning the rotation until the events worth reading had aged out of it,
# and making the log itself something that cannot be shared for debugging.
# WARNING keeps genuine transport failures without either problem.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
            alerted_at TEXT DEFAULT '',
            user_chat_id INTEGER DEFAULT 0,
            admin_approved_at TEXT DEFAULT '',
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    # Answers to the two screening questions. state walks forward through
    # asked_q1 -> asked_q2 -> answered -> (awaiting_admin, if a human has to
    # decide). Q1 and Q2 are stored separately so an admin can read them as
    # the two distinct answers they are, even though they are scored together
    # - see combined_answer()'s docstring for why scoring them apart would be
    # unfair to a short, honest Q1.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS screening (
            chat_id INTEGER,
            user_id INTEGER,
            state TEXT,
            q1_answer TEXT DEFAULT '',
            q2_answer TEXT DEFAULT '',
            answered_at TEXT DEFAULT '',
            flags TEXT DEFAULT '',
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    # Where each admin alert was posted, so it can be edited in place when the
    # requester answers rather than posting a second message about the same
    # person into the same chat.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS join_alerts (
            chat_id INTEGER,
            user_id INTEGER,
            notify_chat_id INTEGER,
            message_id INTEGER,
            PRIMARY KEY (chat_id, user_id, notify_chat_id)
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

    # Migrate DBs created before duplicate-alert suppression. Existing rows get
    # '' - treated as "never alerted" - so anything still pending when this
    # lands is alerted once more and then settles. Re-alerting a genuinely open
    # request is the harmless direction; going silent on one is not.
    pend_cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_requests)").fetchall()}
    if "alerted_at" not in pend_cols:
        conn.execute("ALTER TABLE pending_requests ADD COLUMN alerted_at TEXT DEFAULT ''")
    # Telegram does not promise from_user.id equals the id of the private chat
    # with that user, and says to use user_chat_id from the join request to
    # reach them. 0 means "we never captured one" - callers fall back to the
    # user id, which is what the bot did everywhere before this existed.
    if "user_chat_id" not in pend_cols:
        conn.execute("ALTER TABLE pending_requests ADD COLUMN user_chat_id INTEGER DEFAULT 0")
    if "admin_approved_at" not in pend_cols:
        conn.execute("ALTER TABLE pending_requests ADD COLUMN admin_approved_at TEXT DEFAULT ''")

    # Migrate DBs created before the two questions were split apart. Anyone
    # mid-conversation under the old single-message flow had whatever they had
    # already sent filed as one blob - move it into q1_answer as a best effort
    # (there is no way to know retroactively where Q1 ended and Q2 began) and
    # mark them for a fresh Q2 prompt rather than leaving them stuck.
    screen_cols = {r[1] for r in conn.execute("PRAGMA table_info(screening)").fetchall()}
    if "q1_answer" not in screen_cols:
        conn.execute("ALTER TABLE screening ADD COLUMN q1_answer TEXT DEFAULT ''")
        conn.execute("ALTER TABLE screening ADD COLUMN q2_answer TEXT DEFAULT ''")
        if "answer" in screen_cols:
            conn.execute(
                "UPDATE screening SET q1_answer=answer, "
                "state=CASE WHEN state='answered' THEN 'asked_q2' ELSE state END "
                "WHERE answer != ''")

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


def record_pending_request(chat_id, user_id, user_chat_id=None):
    """Record (or refresh) a pending join request.

    Returns True when this request still needs an admin alert: either we have
    never seen it before, or every previous attempt to deliver the alert failed
    and is worth retrying.

    Telegram redelivers any update it never got a confirmation for, and this
    process is killed and restarted by Android as a matter of routine - so the
    same join request arrives again on the next boot. The old INSERT OR REPLACE
    said nothing about whether anyone had been told, so every restart posted a
    fresh copy of the alert into the admin group, each with its own live Claim
    and Approve buttons. A member who withdraws a request and sends another one
    took the same path.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT alerted_at FROM pending_requests WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO pending_requests "
                "(chat_id, user_id, requested_at, alerted_at, user_chat_id) "
                "VALUES (?,?,?,'',?)",
                (chat_id, user_id, now, int(user_chat_id or 0)),
            )
            return True
        # Already on file. Keep alerted_at; only freshen when it was last seen.
        conn.execute(
            "UPDATE pending_requests SET requested_at=? WHERE chat_id=? AND user_id=?",
            (now, chat_id, user_id),
        )
        if user_chat_id:
            conn.execute(
                "UPDATE pending_requests SET user_chat_id=? WHERE chat_id=? AND user_id=?",
                (int(user_chat_id), chat_id, user_id),
            )
    return not (row[0] or "")


def arrival_is_expected(chat_id, user_id) -> bool:
    """True when someone showing up in the group is the arrival we were waiting
    for, rather than a member who left and came back.

    This is what stops the "I was approved, and when I finally opened Telegram I
    was muted" complaint. Under screening, consent happens when the person
    answers the bot, and an admin may not approve them until hours or days
    later. The old 120-second window read every one of those arrivals as a
    rejoin, threw the acceptance away and re-muted them - punishing exactly the
    people who are slow to check Telegram, which is who was complaining.

    An open pending_requests row is the reliable half: it exists from the moment
    they knock until they are through the door, so any arrival while it is open
    is expected no matter how long it took. The time window stays as a fallback
    for anyone admitted by a route that never created a request.
    """
    age = acceptance_age_seconds(chat_id, user_id)
    return (age is not None and age < 120) or has_pending_request(chat_id, user_id)


def dm_target(chat_id, user_id):
    """Where to DM this requester.

    Prefers the user_chat_id Telegram supplied with the join request: the Bot
    API docs are explicit that from_user.id is not guaranteed to equal the id of
    the private chat with that user, and that user_chat_id is what you should
    reply to. Falls back to the user id for anyone recorded before that was
    captured, which is what every call site did previously.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT user_chat_id FROM pending_requests WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return (row[0] if row and row[0] else None) or user_id


def mark_request_alerted(chat_id, user_id):
    """Called only once an alert has actually reached somebody, so a delivery
    that failed everywhere is retried rather than counted as done."""
    with db() as conn:
        conn.execute(
            "UPDATE pending_requests SET alerted_at=? WHERE chat_id=? AND user_id=?",
            (datetime.now(timezone.utc).isoformat(), chat_id, user_id),
        )


def has_pending_request(chat_id, user_id) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM pending_requests WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return row is not None


def mark_admin_approved(chat_id, user_id):
    """An admin has tapped Approve for this request. Checked by _process_agree
    so a later Agree tap - whether it is the screening flow's own button or
    the fallback T&C prompt on_admin_approve sends when nobody screened - just
    lets them in, instead of re-running the screening verdict against a
    decision a human already made. Without this, a person an admin had
    already approved could re-trigger "needs a human" on their own approval
    and get pinged straight back to the admins who just cleared them.
    """
    with db() as conn:
        conn.execute(
            "UPDATE pending_requests SET admin_approved_at=? "
            "WHERE chat_id=? AND user_id=?",
            (datetime.now(timezone.utc).isoformat(), chat_id, user_id),
        )


def was_admin_approved(chat_id, user_id) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT admin_approved_at FROM pending_requests WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return bool(row and row[0])


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
# Join screening storage & scoring
# ---------------------------------------------------------------------------
_LINKISH = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|@[A-Za-z0-9_]{5,})", re.I)

# CJK, plus Tamil, which the rules page is also published in.
_DENSE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
_TAMIL = re.compile(r"[஀-௿]")


def answer_weight(text: str) -> int:
    """Length of an answer in rough information units rather than characters.

    A flat character count is the wrong measure for a bilingual group. Chinese
    packs two to three times the meaning into a character - 「朋友介绍的，我想帮助
    流浪动物」 is a complete answer in under twenty - so a threshold tuned for
    English silently sets a far higher bar for Chinese, and the people it would
    turn away are precisely the members this group is written for. Weighting the
    dense scripts keeps one threshold honest across all of them.
    """
    body = text or ""
    return (len(body)
            + int(len(_DENSE.findall(body)) * 1.5)
            + int(len(_TAMIL.findall(body)) * 0.5))


SCREENING_STATES = ("asked_q1", "asked_q2", "answered", "awaiting_admin")


def set_screening(chat_id, user_id, state, q1=None, q2=None, flags=None):
    """Create or advance a screening row.

    Pass q1/q2 only when actually recording an answer to that question - the
    plain state transitions (asking Q2 once Q1 lands, moving to Q3 once Q2
    lands) call this with neither, so an existing answer is never overwritten
    with nothing.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO screening (chat_id, user_id, state) VALUES (?,?,?) "
            "ON CONFLICT(chat_id, user_id) DO NOTHING",
            (chat_id, user_id, state),
        )
        sets, params = ["state=?"], [state]
        if q1 is not None:
            sets.append("q1_answer=?"); params.append(q1)
        if q2 is not None:
            sets.append("q2_answer=?"); params.append(q2)
        if flags is not None:
            sets.append("flags=?"); params.append(",".join(flags))
        if q1 is not None or q2 is not None:
            sets.append("answered_at=?"); params.append(now)
        params += [chat_id, user_id]
        conn.execute(
            f"UPDATE screening SET {', '.join(sets)} WHERE chat_id=? AND user_id=?",
            params,
        )


def get_screening(chat_id, user_id):
    """(state, q1_answer, q2_answer, flags), or (None, '', '', []) if this
    person has no row at all."""
    with db() as conn:
        row = conn.execute(
            "SELECT state, q1_answer, q2_answer, flags FROM screening "
            "WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if not row:
        return None, "", "", []
    return (row[0], row[1] or "", row[2] or "",
            [f for f in (row[3] or "").split(",") if f])


def combined_answer(q1, q2):
    """Q1 and Q2 joined into the single string that gets scored and shown.

    Scored together rather than separately: a short, honest Q1 like "a friend
    told me" would otherwise be measured against the same length bar as a full
    paragraph, penalising someone for the question being split rather than for
    anything they actually did. The passphrase and the link check work the
    same way - either question can carry either one.
    """
    return "\n".join(p for p in (q1, q2) if p)


def awaiting_reply(user_id):
    """(chat_id, state) for a screening exchange still open with this person,
    or (None, None).

    Keyed on user only: their reply arrives in a DM, which carries no clue
    about which group it is about. 'answered' counts as still open, because
    someone who has replied to both questions but not yet tapped Agree may add
    more before they do - see on_private_message.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT chat_id, state FROM screening "
            "WHERE user_id=? AND state IN ('asked_q1','asked_q2','answered') "
            "ORDER BY rowid DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def score_answer(text: str):
    """Red flags on a screening answer. Empty list means nothing looked wrong.

    These test the SHAPE of an answer, never its truth - a fluent lie passes
    every one of them. That is the ceiling on what any automated screen can do
    here, and the reason auto-approval is opt-in.
    """
    flags = []
    body = (text or "").strip()
    if not body:
        flags.append("no-answer")
        return flags
    if answer_weight(body) < SCREENING_MIN_CHARS:
        flags.append("too-short")
    if _LINKISH.search(body):
        flags.append("contains-link")
    # Compare with spacing removed on both sides, so "WangWang", "wang wang"
    # and "Wang  Wang" are one phrase rather than three near-misses.
    low = body.lower()
    squashed = re.sub(r"\s+", "", low)
    if SCREENING_KEYWORDS and not any(
            k in low or re.sub(r"\s+", "", k) in squashed
            for k in SCREENING_KEYWORDS):
        flags.append("no-keyword")
    return flags


def screening_verdict(chat_id, user_id):
    """('auto'|'review', flags). 'auto' only ever with SCREENING_AUTO_APPROVE
    on AND a completely clean combined answer - any single flag sends it to a
    human, and not having reached Q2 yet counts as a flag."""
    state, q1, q2, flags = get_screening(chat_id, user_id)
    if state is None:
        return "review", ["not-screened"]
    if state in ("asked_q1", "asked_q2"):
        return "review", ["no-answer"]
    if not SCREENING_AUTO_APPROVE:
        return "review", flags
    return ("auto", flags) if not flags else ("review", flags)


def record_join_alert(chat_id, user_id, notify_chat_id, message_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO join_alerts "
            "(chat_id, user_id, notify_chat_id, message_id) VALUES (?,?,?,?)",
            (chat_id, user_id, notify_chat_id, message_id))


def get_join_alerts(chat_id, user_id):
    with db() as conn:
        return conn.execute(
            "SELECT notify_chat_id, message_id FROM join_alerts "
            "WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchall()


def clear_join_alerts(chat_id, user_id):
    with db() as conn:
        conn.execute("DELETE FROM join_alerts WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))


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
#
# can_invite_users is False on purpose, and it is the one field here worth
# understanding before changing.
#
# The group runs with "Approve New Members" on, so every entrant is supposed to
# arrive as a join request an admin reviews by hand. The invite right is the one
# member permission that can route somebody past that review. While the group was
# open-join it cost nothing to grant - anyone could join unaided anyway - so it
# was set True and stayed True after approval was turned on, which quietly left
# every accepted member able to walk a guest in around the door.
#
# Note this is the ONLY place the fix belongs. LIFT_PERMISSIONS below must keep
# every field True: the Bot API reads an all-True restrict call as "delete this
# user's exception", which drops them back to plain member status governed by the
# chat default set here. Setting can_invite_users=False there instead would leave
# each unlocked member sitting in `restricted` status carrying a permanent
# exception - it would still deny the invite right, but by the wrong mechanism.
# Fix the default; let the lift keep meaning "lift".
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
    can_invite_users=False,
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
        f"ERROR_NOTIFY:       {ERROR_NOTIFY} (cooldown {ERROR_NOTIFY_COOLDOWN}s)"
        + ("  - ALSO SILENCES 'gate is down' alerts" if not ERROR_NOTIFY else ""),
        f"HEARTBEAT_HOURS:    {HEARTBEAT_HOURS or 'off - nothing will tell you if the bot dies'}",
        f"SCREENING_ENABLED:  {SCREENING_ENABLED}",
        f"SCREENING_AUTO_APPROVE: {SCREENING_AUTO_APPROVE}",
        f"SCREENING_KEYWORDS: {SCREENING_KEYWORDS}"
        + ("  (ignored, screening is off)" if not SCREENING_ENABLED else ""),
        f"SCREENING_MIN_CHARS:{SCREENING_MIN_CHARS}",
        f"SCREENING_DEBOUNCE_SECONDS: {SCREENING_DEBOUNCE_SECONDS}"
        + ("  (off - every message advances immediately)"
           if SCREENING_DEBOUNCE_SECONDS <= 0 else ""),
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
    if SCREENING_ENABLED and SCREENING_AUTO_APPROVE and not SCREENING_KEYWORDS:
        warn.append(
            "SCREENING_AUTO_APPROVE is on but SCREENING_KEYWORDS is empty, so "
            "the passphrase check never fires and any answer long enough and "
            "free of links admits itself. Set a passphrase or turn auto-approve "
            "off.")
    if SCREENING_ENABLED and not RULES_CHANNEL_URL:
        warn.append("Screening is on but RULES_CHANNEL_URL is unset, so the "
                    "screening DM asks people to accept rules it cannot link.")
    if SCREENING_ENABLED and not SCREENING_KEYWORDS:
        warn.append("SCREENING_KEYWORDS is empty, so the no-keyword check never "
                    "fires and every answered request looks clean.")
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


def ttl_note_en():
    """The 'this will disappear' sentence, derived from the actual TTL.

    It used to say "5 minutes" in four hardcoded places while PROMPT_TTL_SECONDS
    defaulted to 0, meaning never - so the bot promised a deletion that was not
    coming, and changing the setting silently made every one of those sentences
    wrong. Saying nothing when nothing will be deleted is the honest version.
    """
    if PROMPT_TTL_SECONDS <= 0:
        return ""
    mins = max(1, round(PROMPT_TTL_SECONDS / 60))
    return (f" This message disappears in about {mins} minute(s) - the pinned "
            "rules message stays, so take as long as you need there.")


def ttl_note_zh():
    if PROMPT_TTL_SECONDS <= 0:
        return ""
    mins = max(1, round(PROMPT_TTL_SECONDS / 60))
    return (f"本消息将在约 {mins} 分钟后自动删除；置顶的规则消息会一直保留，"
            "您可以在那里慢慢阅读。")


def gate_instruction():
    """One sentence telling a member exactly what to tap, matching the keyboard
    that gate_keyboard() actually renders. Never hardcode button text elsewhere -
    it drifts the moment REQUIRE_READ_ACK changes."""
    if REQUIRE_READ_ACK:
        return f"tap '{read_en()}' and then '{agree_en()}'.{ttl_note_en()}"
    return f"tap '{agree_en()}'"


def gate_instruction_zh():
    """Chinese counterpart of gate_instruction(). Kept as a separate function
    for the same reason: change the keyboard and both must change together."""
    if REQUIRE_READ_ACK:
        return f"请点击“{read_zh()}”，然后点击“{agree_zh()}”。{ttl_note_zh()}"
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
    # answerCallbackQuery caps text at 200 characters and rejects the whole
    # call if it's longer - not just truncates. A caller that interpolates a
    # raw TelegramError (unbounded length, e.g. "Approve failed: {e}") can
    # blow past that, and the resulting "message too long" BadRequest was
    # being swallowed below and logged as if the tap had simply expired,
    # burying the original error it was trying to report.
    if text and len(text) > 200:
        text = text[:197] + "..."
    try:
        if text:
            await query.answer(text, show_alert=show_alert)
        else:
            await query.answer()
    except Exception as e:
        who = getattr(getattr(query, "from_user", None), "id", "?")
        log.warning("answer_callback_query failed for user_id=%s chat_id=%s "
                    "(expired/stale tap, or another answerCallbackQuery error): %s",
                    who, fallback_chat_id or "?", e)
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
_last_notify = {}     # throttle key -> wall-clock seconds (in-process fallback
                      # only; the durable copy lives in the runtime table)


async def notify_admins(bot, text, key="error"):
    """DM every admin, throttled per key.

    Failures on this deployment are invisible otherwise: the process runs on a
    phone, writes to a file, and nobody reads the file until something is
    already broken. Throttled because a crash loop would otherwise send one DM
    per failed update.

    The throttle is persisted rather than kept in memory. It used to be a bare
    dict keyed on time.monotonic(), which resets when the process does - and a
    restart loop is exactly the failure this function exists to report, so the
    one case that most needed throttling was the one case it could not throttle
    at all. run.sh respawns five seconds after a crash, so an error that recurs
    on startup sent every admin a DM every five seconds for as long as it took
    somebody to notice.

    Falls back to the in-memory dict if the database cannot be read: this runs
    on the error path, and an error reporter that raises is worse than one that
    occasionally repeats itself.
    """
    if not ERROR_NOTIFY:
        return
    now = time.time()
    rkey = f"notify_last:{key}"
    try:
        last = float(runtime_get(rkey, "") or 0.0)
        persisted = True
    except (ValueError, sqlite3.Error):
        last, persisted = _last_notify.get(key, 0.0), False

    if last and now - last < ERROR_NOTIFY_COOLDOWN:
        return

    _last_notify[key] = now
    if persisted:
        try:
            runtime_set(rkey, now)
        except sqlite3.Error as e:
            log.warning("could not persist notify throttle for %r: %s", key, e)

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
    # OPEN_PERMISSIONS, not FULL_PERMISSIONS. Disabling the T&C gate should not
    # also hand every member the invite right back and reopen a way around
    # "Approve New Members" - those are two separate decisions, and this command
    # is only being asked to make the first one.
    try:
        await context.bot.set_chat_permissions(
            chat.id, OPEN_PERMISSIONS, use_independent_chat_permissions=INDEPENDENT_PERMS
        )
    except TelegramError as e:
        await admin_reply(update, context, f"Could not restore permissions: {e}")
        return
    await admin_reply(update, context,
        "Chat default permissions restored - the gate is disabled for anyone "
        "joining from now on.\n\nMembers who are currently restricted keep their "
        "individual restriction: this only changes the chat default. Run /resync "
        "to lift the ones who had accepted.")


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


def join_alert_keyboard(source_chat_id, user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🙋 Claim", callback_data=f"{CLAIM_CB}:{source_chat_id}:{user_id}"),
        InlineKeyboardButton(
            "✅ Approve", callback_data=f"{APPROVE_CB}:{source_chat_id}:{user_id}"),
    ]])


def build_join_alert_text(user, req=None):
    """The identity block: who they are, plus whatever the join request itself
    told us for free. Every alert state (in progress, answered, resolved)
    starts from this and appends its own trailer - see _live_trailer() and
    finalize_join_alerts().

    bio and invite_link come free with every join request and were being thrown
    away. The bio is often the quickest read on a spam account, and the invite
    link tells you which of your links they came through - worth having a named
    link per channel so that answer is useful.
    """
    safe_name = html.escape(user.first_name or str(user.id))
    uname_str = f" (@{html.escape(user.username)})" if user.username else ""
    lines = [
        "📥 <b>New join request</b>\n",
        f'<a href="tg://user?id={user.id}">{safe_name}</a>{uname_str}',
        f"User ID: <code>{user.id}</code>",
    ]
    if req is not None:
        bio = getattr(req, "bio", None)
        if bio:
            lines.append(f"Bio: <i>{html.escape(bio[:200])}</i>")
        link = getattr(req, "invite_link", None)
        if link is not None:
            label = getattr(link, "name", None) or getattr(link, "invite_link", "")
            if label:
                lines.append(f"Via link: <code>{html.escape(str(label))}</code>")
    return "\n".join(lines)


def _live_trailer(waiting_for=None):
    """Appended while a request is still open and needs a Claim/Approve
    prompt. Kept separate from build_join_alert_text so a resolved alert
    (finalize_join_alerts) can leave this off entirely instead of saying
    "they cannot see the group yet" underneath a line saying they are already
    in - the two used to be glued together and could not be pulled apart.

    waiting_for names what is ACTUALLY still outstanding - None (screening
    off, nothing to report), 'unreachable' (the DM bounced), 'q1' (sent
    question 1, no reply yet), or 'agree' (both questions answered, waiting
    on the read-then-agree tap). This used to be a screening_sent bool that
    every caller passed True/False/None into, and update_join_alerts_with_answer
    hardcoded True - so once someone had answered BOTH questions, the alert
    kept insisting "waiting on question 1" directly above their two answers,
    contradicting the very block above it.
    """
    lines = []
    if waiting_for == "unreachable":
        # Distinguishing "has not replied yet" from "never heard from us" is the
        # difference between waiting and reaching out - and without this line an
        # admin sees the same blank alert either way.
        lines.append(
            "\n⚠️ <b>I could not DM them</b>, so they have not been asked "
            "anything. Their privacy settings may block me. They will need "
            "approving on judgement alone, and will arrive muted until they "
            "accept the terms."
        )
    elif waiting_for == "q1":
        lines.append("\n⏳ Asked them question 1. Waiting on a reply.")
    elif waiting_for == "agree":
        lines.append(
            "\n⏳ They have answered both questions. Waiting on them to tap "
            "Agree on the rules."
        )
    lines.append(
        "\nThey cannot see the group yet. <b>Claim</b> to say you are handling "
        "them; <b>Approve</b> once satisfied."
    )
    return "\n".join(lines)


async def notify_admin_groups_of_join(bot, source_chat_id, user, req=None,
                                      screening_sent=None):
    """Post an alert with Claim + Approve buttons to every registered admin
    group. Under manual-approval mode this is the ONLY way the requester
    gets into the group - see on_admin_approve.

    Returns True if the alert reached at least one destination.

    When no admin group is registered, or every registered one fails, this
    falls back to DMing ADMIN_IDS. It used to return silently in that case,
    which was survivable when the mute was the real barrier and an unnoticed
    request still could not talk. Now that approval is the barrier, an alert
    nobody receives is a person left waiting at the door for as long as it
    takes someone to think to check - with no warning anywhere that it
    happened. The fallback has to not depend on one group's chat id still
    being valid.
    """
    trailer_state = {True: "q1", False: "unreachable", None: None}[screening_sent]
    text = build_join_alert_text(user, req) + _live_trailer(trailer_state)
    keyboard = join_alert_keyboard(source_chat_id, user.id)

    notify_chats = get_notify_chats(source_chat_id)
    delivered = 0
    for notify_cid in notify_chats:
        try:
            sent = await bot.send_message(
                notify_cid, text,
                parse_mode="HTML",
                reply_markup=keyboard)
            delivered += 1
            if getattr(sent, "message_id", None) is not None:
                record_join_alert(source_chat_id, user.id, notify_cid, sent.message_id)
        except TelegramError as e:
            log.warning("join notify to chat %s failed: %s", notify_cid, e)

    if delivered:
        return True

    if notify_chats:
        reason = (f"None of the {len(notify_chats)} registered admin group(s) "
                  "could be reached, so this came to you directly.")
    else:
        reason = ("No admin group is registered for this chat, so this came to "
                  "you directly. Run /join_notify in your admin group to have "
                  "these posted there instead.")
    log.warning("join alert for user_id=%s falling back to admin DMs: %s",
                user.id, reason)

    for uid in sorted(ADMIN_IDS):
        try:
            sent = await bot.send_message(
                uid, f"{text}\n\n⚠️ {html.escape(reason)}",
                parse_mode="HTML",
                reply_markup=keyboard)
            delivered += 1
            if getattr(sent, "message_id", None) is not None:
                record_join_alert(source_chat_id, user.id, uid, sent.message_id)
        except TelegramError as e:
            log.warning("join alert DM to admin %s failed: %s", uid, e)

    if not delivered:
        log.error("join alert for user_id=%s in chat %s reached NOBODY - no "
                  "admin group and no admin DM succeeded", user.id, source_chat_id)
    return delivered > 0


# Three separate messages, sent one at a time as each is answered, rather than
# one message asking all three at once. Q3 is not a free-text question at all
# - it is the existing read-then-agree button flow, which is a stronger
# consent record than matching words in a reply (see gate_keyboard). Wording
# is intentionally plain; it is the first thing every prospective member reads
# and is worth your own pass before anyone sees it.
def screening_q1_text():
    return bi(
        "Thank you for joining this group! As part of the workflow, please "
        "help to answer 3 questions in order:\n\n"
        "<b>1.</b> How did you hear about this group",
        "感谢您加入本群组！作为流程的一部分，请按顺序回答以下 3 个问题：\n\n"
        "<b>1.</b> 您是如何得知本群组的",
        "\n\n———\n\n")


def screening_q2_text():
    return bi("<b>2.</b> Why do you want to join this group",
              "<b>2.</b> 您为什么想加入本群组", "\n\n")


def screening_q3_text():
    rules = (f'\n\n📖 <a href="{html.escape(RULES_CHANNEL_URL)}">Read the group rules here</a>'
             if RULES_CHANNEL_URL else "")
    rules_zh = (f'\n\n📖 <a href="{html.escape(RULES_CHANNEL_URL)}">点此阅读群组规则</a>'
                if RULES_CHANNEL_URL else "")
    return bi(
        f"<b>3.</b> Please read the terms and conditions and then confirm "
        f"that you read and agree to them.{rules}",
        f"<b>3.</b> 请阅读条款和条件，然后确认您已阅读并同意。{rules_zh}",
        "\n\n")


async def send_screening_q1(bot, chat, req):
    """DM a fresh requester question 1, the moment they knock.

    Legal without them ever having messaged the bot: since Bot API 5.5 a bot may
    contact anyone who has sent a join request to a chat it administers with
    can_invite_users. Returns True if it landed.
    """
    user = req.from_user
    target = getattr(req, "user_chat_id", None) or user.id
    try:
        await bot.send_message(target, screening_q1_text(), parse_mode="HTML")
    except TelegramError as e:
        # Not fatal, and not unusual: privacy settings can refuse it. The admin
        # alert still goes out, just flagged as unreachable.
        log.warning("screening Q1 DM to user_id=%s (chat %s) failed: %s",
                    user.id, target, e)
        return False
    set_screening(chat.id, user.id, "asked_q1")
    log.info("SCREENING asked q1 user_id=%s for chat %s", user.id, chat.id)
    return True


# Fragments waiting out the debounce window, keyed by (chat_id, user_id). Not
# persisted: lost on restart is the harmless direction, same as the rest of
# this file's in-memory state - worst case a fragment sent right at the
# restart boundary is treated as a complete answer on its own instead of
# being joined with one sent moments before, which is exactly what happens
# with debouncing off. Never read without _debounce_lock held, since a
# restart-recovered webhook replay and a live message for the same person
# could otherwise race on read-modify-write.
_pending_fragments = {}   # (chat_id, user_id) -> accumulated text so far
_debounce_lock = asyncio.Lock()


def _debounce_job_name(chat_id, user_id):
    return f"screening_debounce:{chat_id}:{user_id}"


async def _finalize_screening_stage(bot, chat_id, user, state, text):
    """The actual state transition for one screening stage, run either
    immediately (debouncing off, or no job_queue) or once the debounce
    window has passed with no further message. Shared so both paths do
    exactly the same thing.
    """
    if state == "asked_q1":
        set_screening(chat_id, user.id, "asked_q2", q1=text)
        log.info("SCREENING q1 answered user_id=%s chat=%s len=%s",
                 user.id, chat_id, len(text))
        try:
            await bot.send_message(user.id, screening_q2_text(), parse_mode="HTML")
        except TelegramError as e:
            log.warning("screening Q2 send failed user_id=%s: %s", user.id, e)

    elif state == "asked_q2":
        _, q1, _, _ = get_screening(chat_id, user.id)
        flags = score_answer(combined_answer(q1, text))
        set_screening(chat_id, user.id, "answered", q1=q1, q2=text, flags=flags)
        log.info("SCREENING q2 answered user_id=%s chat=%s flags=%s",
                 user.id, chat_id, flags or "none")
        await update_join_alerts_with_answer(bot, chat_id, user, q1, text, flags)
        try:
            await bot.send_message(user.id, screening_q3_text(), parse_mode="HTML",
                                   reply_markup=gate_keyboard(f":{chat_id}"))
        except TelegramError as e:
            log.warning("screening Q3 send failed user_id=%s: %s", user.id, e)

    elif state == "answered":
        # They are typing more before tapping Agree - Q3's buttons are already
        # on screen. Appended to Q2 rather than dropped, and rescored: it can
        # still supply the passphrase if they forgot it the first time.
        _, q1, q2, _ = get_screening(chat_id, user.id)
        q2 = f"{q2}\n{text}".strip() if q2 else text
        flags = score_answer(combined_answer(q1, q2))
        set_screening(chat_id, user.id, "answered", q1=q1, q2=q2, flags=flags)
        log.info("SCREENING follow-up user_id=%s chat=%s flags=%s",
                 user.id, chat_id, flags or "none")
        await update_join_alerts_with_answer(bot, chat_id, user, q1, q2, flags)
        # Q3's buttons were already sent; nothing more to send here.


async def _run_debounced_stage(context: ContextTypes.DEFAULT_TYPE):
    """job_queue callback: the debounce window elapsed with no further
    message, so whatever accumulated is final."""
    data = context.job.data
    chat_id, user, state = data["chat_id"], data["user"], data["state"]
    async with _debounce_lock:
        text = _pending_fragments.pop((chat_id, user.id), "").strip()
    if not text:
        return  # a race emptied it (e.g. state moved on some other way); nothing to finalize
    await _finalize_screening_stage(context.bot, chat_id, user, state, text)


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A DM that is not a command. Only meaningful from someone mid-screening:
    routes their reply to whichever of the three stages they are actually on.

    Waits SCREENING_DEBOUNCE_SECONDS of silence before treating a reply as
    final, resetting on every new message - mobile texters send a thought in
    two or three quick messages rather than one, and without this the first
    fragment alone became the whole answer to Q1, with the very next fragment
    (meant to finish that same thought) filed under Q2 instead.
    """
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or user.is_bot or not msg.text:
        return

    chat_id, state = awaiting_reply(user.id)
    if chat_id is None:
        return  # not mid-screening; nothing to say

    text = msg.text.strip()
    remember_user(chat_id, user)

    if SCREENING_DEBOUNCE_SECONDS <= 0 or context.job_queue is None:
        # Debouncing off, or the job-queue extra isn't installed - the old
        # immediate-advance behaviour. Never silently drop the message: with
        # no job_queue, waiting would mean nothing ever finalizes at all.
        await _finalize_screening_stage(context.bot, chat_id, user, state, text)
        return

    key = (chat_id, user.id)
    async with _debounce_lock:
        prior = _pending_fragments.get(key, "")
        _pending_fragments[key] = f"{prior}\n{text}".strip() if prior else text

    name = _debounce_job_name(chat_id, user.id)
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    context.job_queue.run_once(
        _run_debounced_stage, when=SCREENING_DEBOUNCE_SECONDS,
        data={"chat_id": chat_id, "user": user, "state": state},
        name=name,
    )


def format_screening_block(q1, q2, flags):
    verdict_line = "✅ no flags" if not flags else "⚠️ " + ", ".join(flags)
    return (
        f"\n\n———\n<b>Their answers</b> ({verdict_line}):\n"
        f"<b>Q1</b> (how they heard): "
        f"{html.escape((q1 or '(no answer yet)')[:400])}\n"
        f"<b>Q2</b> (why they want in): "
        f"{html.escape((q2 or '(no answer yet)')[:400])}"
    )


async def update_join_alerts_with_answer(bot, chat_id, user, q1, q2, flags):
    """Edit the alerts already posted for this person so their answers appear
    on the message admins are already looking at, rather than arriving as a
    second notification about the same request.

    This keeps the alert's CONTENT current, but editing a Telegram message
    does not generate a notification - so someone who claimed this and is not
    currently looking at the chat gets no signal at all that an answer landed.
    That is fine here: this fires after Q2, before they have even tapped
    Agree, so there is nothing actionable yet. See ping_admins_ready() for the
    point where there actually is something to decide.
    """
    for notify_chat_id, message_id in get_join_alerts(chat_id, user.id):
        try:
            await bot.edit_message_text(
                chat_id=notify_chat_id,
                message_id=message_id,
                text=(build_join_alert_text(user) + format_screening_block(q1, q2, flags)
                      + _live_trailer(waiting_for="agree")),
                parse_mode="HTML",
                reply_markup=join_alert_keyboard(chat_id, user.id),
            )
        except TelegramError as e:
            log.warning("could not attach screening answers to alert in %s: %s",
                        notify_chat_id, e)


async def ping_admins_ready(bot, chat_id, user_id, requester):
    """A real, new message - not an edit - announcing that a request has
    finished screening (agreed, verdict is 'review') and needs a human
    decision.

    Editing the alert keeps it accurate, but Telegram does not notify anyone
    when a message is edited. Without this, an admin who claimed a request and
    stepped away has no way to learn the person came back other than
    remembering to scroll up and check - which does not scale past a couple of
    open requests. Sent once, replying to the original alert so the full
    answers are one tap away, and addressed by name to whoever claimed it if
    someone did - an inline tg://user mention notifies that person the same
    way an @username would.
    """
    claim = get_join_claim(chat_id, user_id)
    safe_name = html.escape(requester.first_name or str(requester.id))
    if claim:
        admin_id, admin_name, _claimed_at = claim
        who = f'<a href="tg://user?id={admin_id}">{html.escape(admin_name)}</a>'
        text = f"🔔 {who} — {safe_name} has finished screening and needs your decision."
    else:
        text = (f"🔔 {safe_name} has finished screening and needs a decision. "
                "Nobody has claimed this yet.")

    for notify_chat_id, message_id in get_join_alerts(chat_id, user_id):
        try:
            await bot.send_message(notify_chat_id, text, parse_mode="HTML",
                                   reply_to_message_id=message_id)
        except TelegramError as e:
            log.warning("ready-for-review ping to %s failed: %s", notify_chat_id, e)


class _MinimalUser:
    """Stand-in for build_join_alert_text's `user` argument when only a bare
    id is available - e.g. get_chat failed but the alert still needs
    finalizing. Carries just the three attributes that function reads."""
    def __init__(self, user_id):
        self.id = user_id
        self.first_name = None
        self.username = None


async def finalize_join_alerts(bot, chat_id, user_id, requester, resolution, req=None):
    """Rewrite every alert posted for this person to show how the request
    ended, with the buttons removed, once there is nothing left to decide.

    Without this, a request that resolves itself - auto-approval, or a manual
    Approve tapped in one admin group - left the SAME alert live with working
    Claim/Approve buttons in every other admin group, and in the auto-approve
    case even in the one it was posted to: nothing ever touched it again. An
    admin could tap Approve on somebody already in the group. requester only
    needs .id, .first_name and .username - a Chat object for a private chat
    duck-types this exactly, so a get_chat() result works as well as a User.
    """
    _, q1, q2, flags = get_screening(chat_id, user_id)
    qa = format_screening_block(q1, q2, flags) if (q1 or q2) else ""
    text = build_join_alert_text(requester, req) + qa + f"\n\n{resolution}"
    for notify_chat_id, message_id in get_join_alerts(chat_id, user_id):
        try:
            await bot.edit_message_text(
                chat_id=notify_chat_id, message_id=message_id,
                text=text, parse_mode="HTML", reply_markup=None)
        except TelegramError as e:
            log.warning("could not finalize alert in %s: %s", notify_chat_id, e)


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
        # A join-request approval records the acceptance in the DM shortly before
        # the member actually appears in the group. That is not a rejoin.
        #
        # The open pending_requests row is the reliable half of this test. The
        # 120-second window alone only held while consent and approval happened
        # seconds apart; with screening, someone consents when they answer and
        # an admin may not approve until hours later, and every one of those
        # arrivals would have been read as a rejoin - acceptance discarded,
        # member re-muted, having done nothing wrong.
        just_approved = arrival_is_expected(chat.id, member.id)

        if REPROMPT_ON_REJOIN and not just_approved:
            forget_acceptance(chat.id, member.id)
            ok, detail = await relock_member(context.bot, chat.id, member.id)
            log.info("REJOIN re-gate user_id=%s relocked=%s (%s)",
                     member.id, ok, detail)
            # fall through to the welcome prompt below
        else:
            ok, detail = await unlock_member(context.bot, chat.id, member.id)
            if ok:
                # They are in and unmuted: the request is finished, so stop
                # counting them as pending or they linger in /join_claims and
                # keep a live Approve button on a handled alert.
                clear_pending_request(chat.id, member.id)
                clear_join_claim(chat.id, member.id)
                clear_join_alerts(chat.id, member.id)
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
           f"on {where} to unlock messaging.{ttl_note_en()}",
           f"欢迎 {member.mention_html()}！{gate_instruction_zh()}"
           f"（见{where}），即可发言。{ttl_note_zh()}", "\n\n"),
        for_user=member.id,
        parse_mode="HTML",
    )


def describe_own_rights(m):
    """The bot's own standing in a chat, reduced to what the gate depends on."""
    admin = m.status == ChatMemberStatus.ADMINISTRATOR
    return {
        "status":   m.status,
        "restrict": admin and bool(getattr(m, "can_restrict_members", False)),
        "invite":   admin and bool(getattr(m, "can_invite_users", False)),
        "delete":   admin and bool(getattr(m, "can_delete_messages", False)),
        "pin":      admin and bool(getattr(m, "can_pin_messages", False)),
    }


def own_rights_delta(was, now):
    """(lost, blocking, regained) for a change in the bot's own rights.

    blocking means the gate cannot function: approving a join request needs
    'invite users' and restricting or unmuting anyone needs 'restrict members'.
    Losing either one stops new members being processed at all; the others
    degrade the experience without stopping it.
    """
    lost = []
    if now["status"] in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        lost.append("I have been removed from the group entirely")
    elif now["status"] != ChatMemberStatus.ADMINISTRATOR:
        lost.append("I am no longer an admin")
    else:
        if was["restrict"] and not now["restrict"]:
            lost.append("'Restrict members' taken away - I cannot gate or unmute anyone")
        if was["invite"] and not now["invite"]:
            lost.append("'Invite users' taken away - I cannot approve join requests")
        if was["delete"] and not now["delete"]:
            lost.append("'Delete messages' taken away - I cannot clear old prompts")
        if was["pin"] and not now["pin"]:
            lost.append("'Pin messages' taken away - I cannot re-pin the rules")

    blocking = (now["status"] != ChatMemberStatus.ADMINISTRATOR
                or not now["restrict"] or not now["invite"])
    regained = (now["status"] == ChatMemberStatus.ADMINISTRATOR
                and now["restrict"] and now["invite"]
                and not (was["restrict"] and was["invite"]))
    return lost, blocking, regained


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The bot's OWN membership or rights in a chat changed.

    Nothing was watching this. Only ChatMemberHandler.CHAT_MEMBER was
    registered, and that reports other people - so an admin demoting the bot, or
    un-ticking one box in its admin rights, took the gate offline in complete
    silence. There is no error to catch either: the bot simply stops being able
    to approve or restrict anyone, and the only symptom is that things quietly
    fail to happen. On a group that has just tightened its door, that is the
    outage you can least afford not to hear about.

    Telegram delivers this update even when the bot is removed outright, so the
    alert still goes out from a chat the bot is no longer in.
    """
    cmu = update.my_chat_member
    if cmu is None:
        return
    chat = cmu.chat
    if chat.type == ChatType.PRIVATE:
        return  # one user blocking the bot is not a gate outage

    was = describe_own_rights(cmu.old_chat_member)
    now = describe_own_rights(cmu.new_chat_member)
    if was == now:
        return

    title = chat.title or str(chat.id)
    log.warning("OWN STATUS CHANGED in %r (%s): %s -> %s", title, chat.id, was, now)

    lost, blocking, regained = own_rights_delta(was, now)

    if lost:
        head = "\U0001F6A8 THE GATE IS DOWN" if blocking else "⚠️ My admin rights changed"
        tail = ("\n\nUntil this is restored I cannot approve join requests or "
                "restrict anyone, so new members are not being processed at all. "
                "Re-grant my admin rights, then run /setup_gate."
                if blocking else "")
        await notify_admins(
            context.bot,
            f"{head} in {title} ({chat.id}).\n\n"
            + "\n".join(f"- {item}" for item in lost) + tail,
            key=f"rights-lost:{chat.id}",
        )
    elif regained:
        await notify_admins(
            context.bot,
            f"✅ Admin rights restored in {title} ({chat.id}).\n\n"
            "Run /setup_gate to re-pin the rules message and confirm the chat "
            "default permissions are still correct.",
            key=f"rights-back:{chat.id}",
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

    if not record_pending_request(chat.id, user.id,
                                  user_chat_id=getattr(req, "user_chat_id", None)):
        log.info("join request from user_id=%s in chat %s was already alerted - "
                 "not posting a second copy", user.id, chat.id)
        return

    # With screening on, the bot speaks first: it asks question 1 immediately,
    # so the exchange is already under way by the time an admin opens the
    # alert. With screening off the requester is not contacted at all, and an
    # admin does that by hand as before.
    screening_sent = None
    if SCREENING_ENABLED:
        screening_sent = await send_screening_q1(context.bot, chat, req)

    if await notify_admin_groups_of_join(context.bot, chat.id, user, req=req,
                                         screening_sent=screening_sent):
        mark_request_alerted(chat.id, user.id)


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
    # Recorded before anything else below can fail or short-circuit: it is
    # what stops a later Agree tap from re-running the screening verdict
    # against a decision a human just made - see its own docstring.
    mark_admin_approved(chat_id, user_id)

    # record_pending_request stays set: it's reused to mean "approved into the
    # group, T&C not yet accepted (if it ever was)" rather than being cleared,
    # since has_pending_request is what on_agree checks to accept a tap as
    # legitimate (see the forged-callback guard in _process_agree).
    #
    # Telegram does not guarantee from_user.id is the id of the private chat
    # with that user, and the join request carried a user_chat_id saying where
    # to reach them. Use it when we captured one.
    already_agreed = has_accepted(chat_id, user_id)
    target = dm_target(chat_id, user_id)
    user_chat = None  # populated below if get_chat succeeds; used again after
                      # the try block to render the finalized alert either way
    try:
        user_chat = await context.bot.get_chat(target)
        safe_name = html.escape(user_chat.first_name or "there")
        group_chat = await context.bot.get_chat(chat_id)
        safe_title = html.escape(group_chat.title or "the group")
        if already_agreed:
            # They already went through screening and tapped Agree - sending
            # the T&C-and-a-button prompt again here would ask them to agree
            # a second time to something they have already agreed to.
            await context.bot.send_message(
                chat_id=target,
                text=bi(
                    f"Good news, {safe_name} — an admin has approved your "
                    f"request to join <b>{safe_title}</b>. You already agreed "
                    "to the rules, so you can post right away.",
                    f"好消息，{safe_name}——管理员已批准您加入 <b>{safe_title}</b> "
                    "的申请。您此前已同意群组规则，现在可以直接发言了。",
                    "\n\n"),
                parse_mode="HTML",
            )
            dm_status = "Confirmation sent by DM (they had already agreed)."
        else:
            await context.bot.send_message(
                chat_id=target,
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
        log.warning("Could not DM after manual approve, user %s: %s", user_id, e)
        dm_status = ("⚠️ Could not DM them" + (" a confirmation" if already_agreed
                    else " the T&C") + " (they may need to message the bot "
                    "first, or have DMs closed).")

    # Say plainly which of the two states they are about to arrive in. Approving
    # somebody who has not agreed yet is legitimate - you may simply know them -
    # but it puts them in the group muted, and that is the exact situation people
    # were finding themselves in with no idea why. Better the admin reads it here
    # than the member discovers it a week later.
    if already_agreed:
        dm_status += "\n\n✅ They had already accepted the terms."
    else:
        dm_status += ("\n\n🔇 They have NOT accepted the terms yet, so they will "
                      "arrive muted until they tap Agree on the message I just "
                      "sent them. That is expected - just be aware they cannot "
                      "post in the meantime.")

    # user_chat is None only if get_chat itself failed above; fall back to a
    # bare stand-in so the alert can still be finalized (it only needs .id,
    # .first_name and .username - see finalize_join_alerts' docstring).
    requester = user_chat or _MinimalUser(user_id)
    await finalize_join_alerts(
        context.bot, chat_id, user_id, requester,
        f"✅ <b>Approved by {html.escape(admin_name)}</b> — {dm_status}")

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

        # With screening on, agreeing is consent - not admission. Unless the
        # answer cleared every check AND auto-approval is switched on, the
        # request stops here and waits for a human, which is the whole point of
        # having turned manual approval on in the first place.
        #
        # Skipped once an admin has already approved this request by hand -
        # this same button also serves the fallback T&C prompt on_admin_approve
        # sends when nobody screened, and a human's own decision must never be
        # second-guessed by the automated verdict on their way back in.
        auto_approved_now = False
        if SCREENING_ENABLED and not was_admin_approved(chat_id, user.id):
            verdict, flags = screening_verdict(chat_id, user.id)
            if verdict != "auto":
                record_acceptance(chat_id, user.id, user.username, user.full_name)
                set_screening(chat_id, user.id, "awaiting_admin")
                log.info("SCREENING -> admin review user_id=%s chat=%s flags=%s",
                         user.id, chat_id, flags or "none")
                # Now, and only now, is there something for an admin to
                # decide - they have agreed, and the passphrase did not clear
                # them. Editing the alert alone would not notify anyone still
                # away from the chat, so send an actual message.
                await ping_admins_ready(context.bot, chat_id, user.id, user)
                try:
                    await query.edit_message_text(
                        bi("\u2705 Thank you \u2014 your agreement is recorded and your "
                           "answers are with the admins. You will hear back here "
                           "once someone has reviewed your request.",
                           "\u2705 \u8c22\u8c22\uff0c\u60a8\u7684\u540c\u610f\u5df2\u8bb0\u5f55\uff0c\u56de\u7b54\u4e5f\u5df2\u53d1\u9001\u7ed9\u7ba1\u7406\u5458\u3002"
                           "\u5ba1\u6838\u5b8c\u6210\u540e\u4f1a\u5728\u6b64\u901a\u77e5\u60a8\u3002", "\n\n\u2014\u2014\u2014\n\n")
                    )
                except TelegramError as e:
                    log.warning("edit_message_text failed (harmless): %s", e)
                return
            log.info("SCREENING -> auto-approve user_id=%s chat=%s", user.id, chat_id)
            auto_approved_now = True

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
        clear_join_claim(chat_id, user.id)
        await clear_prompt(context.bot, chat_id, user.id)
        record_acceptance(chat_id, user.id, user.username, user.full_name)
        if auto_approved_now:
            # Only true resolution point in this branch: no admin has acted,
            # so the alert is still sitting there with live buttons. If an
            # admin HAD already approved, on_admin_approve already finalized
            # it with a more specific line - do not overwrite that here.
            await finalize_join_alerts(
                context.bot, chat_id, user.id, user,
                "✅ <b>Auto-approved</b> — the passphrase matched. No action needed.")
        # finalize_join_alerts (above, when it ran) needs get_join_alerts() to
        # still have rows to find and edit - clearing them has to wait until
        # after it returns, in both branches, or the resolved text never
        # reaches the alert at all.
        clear_join_alerts(chat_id, user.id)
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
        bi(f"{user.mention_html()} - thanks, you can now send messages!{ttl_note_en()}",
           f"{user.mention_html()} - 谢谢，您现在可以发言了！{ttl_note_zh()}", "\n\n"),
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


async def job_heartbeat(context: ContextTypes.DEFAULT_TYPE):
    """Periodic proof of life. Deliberately not throttled through
    notify_admins: the entire value of this message is that it arrives on
    schedule, so suppressing one would defeat the point."""
    chats = gate_chats()
    try:
        pending = sum(len(pending_join_claims(c)) for c in chats)
    except sqlite3.Error as e:
        pending = f"unknown ({e})"
    text = (
        "✅ Gate bot still running.\n"
        f"Started: {runtime_get('last_start', '?')}\n"
        f"Restarts: {runtime_get('restart_count', '?')}\n"
        f"Gated chats: {len(chats)}\n"
        f"Unclaimed join requests: {pending}\n\n"
        "If one of these stops arriving, assume the bot is down - a dead "
        "process, a revoked token or a flat battery all look the same from "
        "your side."
    )
    for uid in sorted(ADMIN_IDS):
        try:
            await context.bot.send_message(uid, text)
        except TelegramError as e:
            log.warning("heartbeat to admin %s failed: %s", uid, e)


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

    # A passphrase printed on the door is not a passphrase. Telegram shows the
    # group's title and photo on the join-request confirmation sheet, and the
    # screening DM greets people by that same title - so a keyword that appears
    # in the title is handed to every requester before they are asked for it,
    # including the ones it exists to keep out. Only a live title can tell us,
    # which is why this runs here rather than in config_summary.
    if SCREENING_ENABLED and SCREENING_AUTO_APPROVE and SCREENING_KEYWORDS:
        for cid in gate_chats():
            try:
                info = await app.bot.get_chat(cid)
            except TelegramError as e:
                log.warning("could not check chat %s title for keyword leaks: %s", cid, e)
                continue
            title = (info.title or "").lower()
            exposed = [k for k in SCREENING_KEYWORDS if k and k in title]
            if exposed:
                msg = (
                    f"⚠️ Passphrase visible in the group name.\n\n"
                    f"{info.title!r} contains: {', '.join(exposed)}\n\n"
                    "Telegram shows the group title on the join-request screen, and "
                    "my screening DM greets people by it, so anyone requesting to "
                    "join is shown these words before I ask for them. Auto-approval "
                    "is currently letting that answer in on its own.\n\n"
                    "Either pick a passphrase that does not appear in the title "
                    "(SCREENING_KEYWORDS) or turn SCREENING_AUTO_APPROVE off."
                )
                log.warning("KEYWORD LEAK in chat %s: title %r contains %s",
                            cid, info.title, exposed)
                await notify_admins(app.bot, msg, key=f"kwleak:{cid}")

    if app.job_queue is None:
        log.warning(
            "job_queue is unavailable - the timed /verify_nudge schedule was NOT "
            'registered. Install the extra: pip install "python-telegram-bot[job-queue]"'
            " and restart.")
        if HEARTBEAT_HOURS > 0:
            log.warning("HEARTBEAT_HOURS=%s but job_queue is unavailable, so no "
                        "heartbeat will be sent. Install the job-queue extra.",
                        HEARTBEAT_HOURS)
        if SCREENING_DEBOUNCE_SECONDS > 0:
            log.warning("SCREENING_DEBOUNCE_SECONDS=%s but job_queue is unavailable, "
                        "so screening replies advance immediately on every message "
                        "regardless - the on_private_message fallback for exactly this "
                        "case, not a crash, but debouncing is not actually happening. "
                        "Install the job-queue extra.", SCREENING_DEBOUNCE_SECONDS)
    else:
        if HEARTBEAT_HOURS > 0:
            every = HEARTBEAT_HOURS * 3600
            app.job_queue.run_repeating(job_heartbeat, interval=every, first=every,
                                        name="heartbeat")
            log.info("Heartbeat every %s hour(s) to %s admin(s)",
                     HEARTBEAT_HOURS, len(ADMIN_IDS))
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
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.StatusUpdate.NEW_CHAT_MEMBERS
                                   | filters.StatusUpdate.LEFT_CHAT_MEMBER),
        on_service_message))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
        on_group_message))
    # Screening answers arrive as ordinary DMs. ~filters.COMMAND keeps this
    # clear of the admin commands, which are all registered above.
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        on_private_message))
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
