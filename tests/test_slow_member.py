"""The 'I was approved but came back to find myself muted' complaint.

Reproduces the old behaviour and confirms the current code no longer does it.
"""
import os, sys, tempfile, sqlite3
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:FAKE", ADMIN_IDS="111",
                  DB_PATH=os.path.join(TMP, "t.db"),
                  LOG_FILE=os.path.join(TMP, "t.log"), TERMS_TEXT="T",
                  REPROMPT_ON_REJOIN="1")
sys.path.insert(0, "/home/user/wangwang-tele-group-rules")
import main13 as m

ok = fail = 0
def check(l, g, w):
    global ok, fail
    if g == w: ok += 1; print(f"  PASS  {l}")
    else: fail += 1; print(f"  FAIL  {l}: got={g!r} want={w!r}")

CHAT = -1001234567890

def age_acceptance(user_id, **delta):
    """Backdate someone's acceptance to simulate time passing."""
    when = (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()
    with m.db() as c:
        c.execute("UPDATE acceptances SET accepted_at=? WHERE chat_id=? AND user_id=?",
                  (when, CHAT, user_id))

print("\n[1] the slow member: agrees, then opens Telegram days later")
SLOW = 1001
m.record_pending_request(CHAT, SLOW, user_chat_id=900001)   # they knocked
m.record_acceptance(CHAT, SLOW, "slow", "Slow Member")      # they agreed in DM
age_acceptance(SLOW, days=3)                                # admin approved 3 days on

check("acceptance really is old", m.acceptance_age_seconds(CHAT, SLOW) > 120, True)
check("the 120s window alone would have called this a rejoin",
      m.acceptance_age_seconds(CHAT, SLOW) < 120, False)
check("their request is still open, so the arrival is expected",
      m.arrival_is_expected(CHAT, SLOW), True)
check("-> they are NOT re-muted, and keep the acceptance they gave",
      m.has_accepted(CHAT, SLOW), True)

print("\n[2] the fast member: approved within seconds (unchanged)")
FAST = 1002
m.record_acceptance(CHAT, FAST, "fast", "Fast Member")
check("fresh acceptance is expected on the time window alone",
      m.arrival_is_expected(CHAT, FAST), True)

print("\n[3] a genuine rejoin is still caught")
LEAVER = 1003
m.record_acceptance(CHAT, LEAVER, "old", "Old Member")
age_acceptance(LEAVER, days=90)
check("no open request and an old acceptance -> treated as a rejoin",
      m.arrival_is_expected(CHAT, LEAVER), False)

print("\n[4] once through the door, the request is closed")
m.clear_pending_request(CHAT, SLOW)
age_acceptance(SLOW, days=3)
check("a later re-entry by the same person is a rejoin again",
      m.arrival_is_expected(CHAT, SLOW), False)

print("\n[5] someone an admin approves without screening still arrives muted")
UNSCREENED = 1004
m.record_pending_request(CHAT, UNSCREENED, user_chat_id=900004)
check("no acceptance on file -> the gate still applies to them",
      m.has_accepted(CHAT, UNSCREENED), False)
check("...which is the one path where the old complaint can still happen",
      m.screening_verdict(CHAT, UNSCREENED)[0], "review")

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
