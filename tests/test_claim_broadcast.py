"""Covers a bug where claiming a join request in one admin group left the
copies of that same alert in every OTHER registered admin group untouched:
still showing live Claim + Approve buttons, with tapping Claim there only
producing an "Already claimed by X" popup and no visible change to the
message itself. notify_admin_groups_of_join fans one alert out to every
notify chat (see join_alerts); on_claim_button only ever edited the one
message the tap came from.
"""
import asyncio, importlib, os, sys, tempfile

TMP = tempfile.mkdtemp()
BASE = dict(BOT_TOKEN="123:FAKE", ADMIN_IDS="111,222",
            LOG_FILE=os.path.join(TMP, "t.log"), TERMS_TEXT="Terms text.",
            RULES_CHANNEL_URL="https://example.org/rules")
sys.path.insert(0, "/home/user/wangwang-tele-group-rules")

ok = fail = 0
def check(label, got, want):
    global ok, fail
    if got == want: ok += 1; print(f"  PASS  {label}")
    else: fail += 1; print(f"  FAIL  {label}\n          got={got!r}\n         want={want!r}")

def load(**over):
    for k in list(os.environ):
        if k.startswith("SCREENING") or k in BASE or k == "DB_PATH":
            os.environ.pop(k, None)
    os.environ.update(BASE)
    os.environ["DB_PATH"] = os.path.join(TMP, f"db{len(over)}{hash(str(over))&0xffff}.db")
    os.environ.update({k: str(v) for k, v in over.items()})
    sys.modules.pop("main13", None)
    return importlib.import_module("main13")

CHAT = -1001234567890
NOTIFY_A, NOTIFY_B = -1009000001, -1009000002

class FakeMessage:
    def __init__(s, chat_id, message_id, text):
        s.chat_id, s.message_id, s.text_html, s.text = chat_id, message_id, text, text

class FakeQuery:
    def __init__(s, data, from_user, message=None):
        s.data, s.from_user, s.message = data, from_user, message
        s.answered = []
    async def answer(s, text=None, show_alert=False):
        s.answered.append((text, show_alert))

class FakeUpdate:
    def __init__(s, query): s.callback_query = query

class FakeBot:
    def __init__(s):
        s.sent, s.edits = [], []
    async def send_message(s, chat_id, text, **kw):
        s.sent.append((chat_id, text, kw))
        class M: message_id = len(s.sent) + 5000
        return M()
    async def edit_message_text(s, chat_id, message_id, text, **kw):
        s.edits.append((chat_id, message_id, text, kw))

class Ctx:
    def __init__(s, bot): s.bot = bot

def make_requester(uid=555001, name="Angel"):
    class U:
        id, first_name, username, full_name, is_bot = uid, name, None, name, False
    return U

def make_admin(aid, name):
    class A:
        id, first_name, username, full_name = aid, name, name.lower(), name
    return A


print("\n[1] claiming in one admin group updates the alert copy in every other one too")
m = load(SCREENING_ENABLED=1)
user = make_requester()
admin = make_admin(111, "Elizabeth")
m.add_notify_chat(CHAT, NOTIFY_A)
m.add_notify_chat(CHAT, NOTIFY_B)
bot = FakeBot()
m.record_pending_request(CHAT, user.id)

asyncio.run(m.notify_admin_groups_of_join(bot, CHAT, user, screening_sent=True))
alerts = dict(m.get_join_alerts(CHAT, user.id))
check("alert recorded in both admin groups", sorted(alerts.keys()), sorted([NOTIFY_A, NOTIFY_B]))

# Admin taps Claim on the copy that landed in NOTIFY_A.
origin_text = bot.sent[[c for c, _, _ in bot.sent].index(NOTIFY_A)][1]
origin_message = FakeMessage(NOTIFY_A, alerts[NOTIFY_A], origin_text)
query = FakeQuery(f"jn_claim:{CHAT}:{user.id}", admin, message=origin_message)
asyncio.run(m.on_claim_button(FakeUpdate(query), Ctx(bot)))

check("claim recorded", m.get_join_claim(CHAT, user.id)[1], "Elizabeth")
edited_chats = sorted(c for c, _, _, _ in bot.edits)
check("BOTH admin groups' alert copies were edited, not just the one tapped",
      edited_chats, sorted([NOTIFY_A, NOTIFY_B]))
check("every edited copy shows who claimed it",
      all("Claimed by Elizabeth" in t for _, _, t, _ in bot.edits), True)
check("every edited copy still has a live Approve button (claiming isn't approving)",
      all(kw.get("reply_markup") is not None for _, _, _, kw in bot.edits), True)
check("no Claim button survives on any copy - only Approve remains",
      all(not any("Claim" in btn.text for row in kw["reply_markup"].inline_keyboard for btn in row)
          for _, _, _, kw in bot.edits), True)

# A second admin taps Claim on the OTHER group's (now-stale) copy.
other_message = FakeMessage(NOTIFY_B, alerts[NOTIFY_B], origin_text)
admin2 = make_admin(222, "Christina")
query2 = FakeQuery(f"jn_claim:{CHAT}:{user.id}", admin2, message=other_message)
asyncio.run(m.on_claim_button(FakeUpdate(query2), Ctx(bot)))
check("second admin is told it's already claimed, by name",
      any(txt and "Elizabeth" in txt for txt, _ in query2.answered), True)
check("no additional claim edit happened from the rejected second tap",
      len(bot.edits), 2)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
