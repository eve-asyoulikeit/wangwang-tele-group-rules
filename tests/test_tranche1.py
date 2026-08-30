"""Exercise the tranche-1 changes against the real functions in main13.py."""
import asyncio, os, sqlite3, sys, tempfile

TMP = tempfile.mkdtemp()
DB = os.path.join(TMP, "t.db")
os.environ.update(
    BOT_TOKEN="123456:FAKE_TOKEN_FOR_TESTS",
    ADMIN_IDS="111,222",
    DB_PATH=DB,
    LOG_FILE=os.path.join(TMP, "t.log"),
    TERMS_TEXT="Test terms.",
)
sys.path.insert(0, "/home/user/wangwang-tele-group-rules")
import main13 as m
from telegram.error import TelegramError

ok = fail = 0
def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1; print(f"  PASS  {label}")
    else:
        fail += 1; print(f"  FAIL  {label}\n          got={got!r} want={want!r}")

CHAT, USER = -1001234567890, 99001

# ---------------------------------------------------------------- dedup
print("\n[1] duplicate-alert suppression")
check("first sighting asks for an alert",
      m.record_pending_request(CHAT, USER), True)
check("redelivered before any alert landed -> still asks (retry)",
      m.record_pending_request(CHAT, USER), True)
m.mark_request_alerted(CHAT, USER)
check("redelivered after a successful alert -> suppressed",
      m.record_pending_request(CHAT, USER), False)
check("...and again (restart loop)",
      m.record_pending_request(CHAT, USER), False)
check("row still counts as pending (approve path unaffected)",
      m.has_pending_request(CHAT, USER), True)
m.clear_pending_request(CHAT, USER)
check("after clear, a genuine re-request alerts again",
      m.record_pending_request(CHAT, USER), True)
m.clear_pending_request(CHAT, USER)

# ---------------------------------------------------------------- migration
print("\n[2] migration of a pre-existing database")
OLD = os.path.join(TMP, "old.db")
c = sqlite3.connect(OLD)
c.execute("""CREATE TABLE pending_requests (chat_id INTEGER, user_id INTEGER,
             requested_at TEXT, PRIMARY KEY (chat_id, user_id))""")
c.execute("INSERT INTO pending_requests VALUES (?,?,?)", (CHAT, 55555, "2026-08-01T00:00:00+00:00"))
c.commit(); c.close()

m.DB_PATH = OLD
with m.db() as conn:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_requests)")}
check("alerted_at added to old table", "alerted_at" in cols, True)
check("pre-existing pending row survived", m.has_pending_request(CHAT, 55555), True)
check("legacy row treated as never-alerted -> alerts once",
      m.record_pending_request(CHAT, 55555), True)
m.DB_PATH = DB

# ---------------------------------------------------------------- permissions
print("\n[3] the invite hole")
check("chat default denies inviting", m.OPEN_PERMISSIONS.can_invite_users, False)
check("chat default still allows sending", m.OPEN_PERMISSIONS.can_send_messages, True)
check("locked set unchanged", m.LOCKED_PERMISSIONS.can_invite_users, False)
lift = m.LIFT_PERMISSIONS.to_dict()
check("LIFT stays all-True so a lift still means 'delete the exception'",
      sorted(k for k, v in lift.items() if v is not True), [])

# ---------------------------------------------------------------- alert fallback
print("\n[4] admin alert delivery")

class FakeUser:
    id, first_name, username, full_name = USER, "Ada", "ada_l", "Ada L"

class FakeBot:
    def __init__(self, failing=()):
        self.sent, self.failing = [], set(failing)
    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.failing:
            raise TelegramError(f"chat {chat_id} unreachable")
        self.sent.append((chat_id, text))

async def scenarios():
    NOTIFY = -1009999999999
    # A: registered admin group, delivery works
    m.add_notify_chat(CHAT, NOTIFY)
    b = FakeBot()
    r = await m.notify_admin_groups_of_join(b, CHAT, FakeUser())
    check("A: delivered to the admin group", (r, [c for c, _ in b.sent]), (True, [NOTIFY]))

    # C: registered group is unreachable -> fall back to admin DMs
    b = FakeBot(failing=[NOTIFY])
    r = await m.notify_admin_groups_of_join(b, CHAT, FakeUser())
    check("C: falls back to admin DMs", (r, sorted(c for c, _ in b.sent)), (True, [111, 222]))
    check("C: fallback explains itself", "came to you directly" in b.sent[0][1], True)

    # B: no admin group registered at all
    m.remove_notify_chat(CHAT, NOTIFY)
    b = FakeBot()
    r = await m.notify_admin_groups_of_join(b, CHAT, FakeUser())
    check("B: unregistered still reaches admins", (r, sorted(c for c, _ in b.sent)), (True, [111, 222]))
    check("B: tells them to run /join_notify", "/join_notify" in b.sent[0][1], True)

    # D: nothing reachable at all
    b = FakeBot(failing=[111, 222])
    r = await m.notify_admin_groups_of_join(b, CHAT, FakeUser())
    check("D: reports total failure instead of claiming success", (r, b.sent), (False, []))

    # D': and the caller must NOT mark it alerted, so it retries next time
    m.clear_pending_request(CHAT, USER)
    m.record_pending_request(CHAT, USER)
    if not r:
        pass  # mark_request_alerted deliberately not called
    check("D': un-delivered request stays eligible for a retry",
          m.record_pending_request(CHAT, USER), True)

asyncio.run(scenarios())

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
