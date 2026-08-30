"""Covers the two bugs found while adding the 3-question flow:

1. A resolved request (auto-approved, or manually approved) left its admin
   alert(s) sitting with live Claim/Approve buttons forever - editing the
   alert to add an answer is not the same as marking it done.
2. An admin approving someone who already screened and agreed got sent a
   second, redundant "please accept the rules" prompt.

Also covers the real-time ping this exposed the need for: editing a Telegram
message does not notify anyone, so a claimed request that comes back with a
"needs a human" verdict has to be announced as a fresh message, not just an
edit - and it should call out the claiming admin by name when there is one.
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
GOOD_Q1, GOOD_Q2 = "A friend told me.", "I want to help because it is the wang wang memorial group."

class FakeQuery:
    def __init__(s, data, from_user, message=None):
        s.data, s.from_user, s.message = data, from_user, message
        s.answered = []
        s.edited = None
    async def answer(s, text=None, show_alert=False):
        s.answered.append((text, show_alert))
    async def edit_message_text(s, text, **kw):
        s.edited = text

class FakeUpdate:
    def __init__(s, query): s.callback_query = query

class FakeBot:
    def __init__(s, fail_chats=()):
        s.sent, s.edits, s.fail_chats = [], [], set(fail_chats)
    async def send_message(s, chat_id, text, **kw):
        if chat_id in s.fail_chats:
            from telegram.error import TelegramError
            raise TelegramError("unreachable")
        s.sent.append((chat_id, text, kw))
        class M: message_id = len(s.sent) + 5000
        return M()
    async def edit_message_text(s, chat_id, message_id, text, **kw):
        s.edits.append((chat_id, message_id, text, kw))
    async def edit_message_reply_markup(s, chat_id, message_id, reply_markup=None):
        pass
    async def approve_chat_join_request(s, chat_id, user_id):
        pass
    async def get_chat(s, target):
        class P: can_send_messages = True
        class C:
            id, first_name, username, title = target, "Ada", "ada_l", "Say No To Animal Abuse"
            permissions = P()
        return C()
    async def get_chat_member(s, chat_id, user_id):
        class M: status = "member"; can_send_messages = True; is_member = True
        return M()
    async def restrict_chat_member(s, chat_id, user_id, permissions=None,
                                   use_independent_chat_permissions=None):
        pass
    async def delete_message(s, chat_id, message_id):
        pass

class Ctx:
    def __init__(s, bot): s.bot = bot

def make_requester(uid=555001, name="Ada", username="ada_l"):
    class U:
        id, first_name, username_, full_name, is_bot = uid, name, username, f"{name} L", False
    U.username = username
    return U

def make_admin(aid, name):
    class A:
        id, first_name, username, full_name = aid, name, name.lower(), name
    return A


# ---------------------------------------------------------------- bug 1: auto-approve leaves alert stale
print("\n[1] auto-approval finalizes every alert, in every admin group")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = make_requester()
m.add_notify_chat(CHAT, NOTIFY_A)
m.add_notify_chat(CHAT, NOTIFY_B)

bot = FakeBot()
m.record_pending_request(CHAT, user.id)

async def post_initial_alert():
    return await m.notify_admin_groups_of_join(bot, CHAT, user, screening_sent=True)
asyncio.run(post_initial_alert())
check("alert posted to both admin groups",
      sorted(c for c, _, _ in bot.sent), sorted([NOTIFY_A, NOTIFY_B]))
check("both alerts have live buttons initially",
      all(kw.get("reply_markup") is not None for _, _, kw in bot.sent), True)

m.set_screening(CHAT, user.id, "answered", q1=GOOD_Q1, q2=GOOD_Q2,
                flags=m.score_answer(m.combined_answer(GOOD_Q1, GOOD_Q2)))

async def finalize():
    await m.finalize_join_alerts(bot, CHAT, user.id, user,
                                 "✅ <b>Auto-approved</b> — the passphrase matched.")
asyncio.run(finalize())
check("finalize touched BOTH admin groups' alerts, not just one",
      sorted(c for c, _, _, _ in bot.edits), sorted([NOTIFY_A, NOTIFY_B]))
check("finalized alerts have NO buttons - nothing left to do",
      all(kw.get("reply_markup") is None for _, _, _, kw in bot.edits), True)
check("finalized text carries the resolution",
      all("Auto-approved" in t for _, _, t, _ in bot.edits), True)
check("finalized text does NOT still say 'cannot see the group yet'",
      all("cannot see the group yet" not in t for _, _, t, _ in bot.edits), True)
check("their Q1/Q2 answers are preserved in the finalized text",
      all(GOOD_Q1 in t and GOOD_Q2 in t for _, _, t, _ in bot.edits), True)


# ---------------------------------------------------------------- full flow: end to end via _process_agree
print("\n[2] end-to-end: agree tap with a clean passphrase answer auto-approves and finalizes")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = make_requester(uid=555002)
m.add_notify_chat(CHAT, NOTIFY_A)
bot = FakeBot()
m.record_pending_request(CHAT, user.id)

async def post():
    return await m.notify_admin_groups_of_join(bot, CHAT, user, screening_sent=True)
asyncio.run(post())

m.set_screening(CHAT, user.id, "answered", q1=GOOD_Q1, q2=GOOD_Q2,
                flags=m.score_answer(m.combined_answer(GOOD_Q1, GOOD_Q2)))

m.record_read_ack(CHAT, user.id)
query = FakeQuery(f"tc_agree:{CHAT}", user)
update = FakeUpdate(query)
asyncio.run(m.on_agree(update, Ctx(bot)))

check("acceptance was recorded", m.has_accepted(CHAT, user.id), True)
check("the alert was finalized (edited, no live buttons)",
      any(kw.get("reply_markup") is None and "Auto-approved" in t
          for _, _, t, kw in bot.edits), True)
check("member sees the accepted confirmation, not the 'wait for admin' one",
      "Accepted" in query.edited and "wait" not in query.edited.lower(), True)
check("no admin was pinged - nothing for a human to do",
      not any("needs your decision" in t or "needs a decision" in t for _, t, _ in bot.sent), True)


# ---------------------------------------------------------------- ping on review verdict
print("\n[3] a 'needs review' verdict pings admins with a real message, not just an edit")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = make_requester(uid=555003, name="Ben")
m.add_notify_chat(CHAT, NOTIFY_A)
bot = FakeBot()
m.record_pending_request(CHAT, user.id)
asyncio.run(m.notify_admin_groups_of_join(bot, CHAT, user, screening_sent=True))
alert_message_id = bot.sent[-1][2] if False else None  # not needed directly

no_kw_answer = "Just found it, seems interesting, want to check it out."
m.set_screening(CHAT, user.id, "answered", q1=no_kw_answer, q2="",
                flags=m.score_answer(m.combined_answer(no_kw_answer, "")))

sent_before = len(bot.sent)
m.record_read_ack(CHAT, user.id)
query = FakeQuery(f"tc_agree:{CHAT}", user)
update = FakeUpdate(query)
asyncio.run(m.on_agree(update, Ctx(bot)))

check("state moved to awaiting_admin", m.get_screening(CHAT, user.id)[0], "awaiting_admin")
check("acceptance IS recorded even though a human still decides admission",
      m.has_accepted(CHAT, user.id), True)
check("a NEW message was sent (the ping), not just the edit",
      len(bot.sent) > sent_before, True)
ping_text = bot.sent[-1][1]
check("ping says a decision is needed", "needs your decision" in ping_text or "needs a decision" in ping_text, True)
check("with nobody having claimed it, the ping says so generically",
      "Nobody has claimed" in ping_text, True)
check("ping is a REPLY to the original alert (jump-to-context, no scrolling)",
      bot.sent[-1][2].get("reply_to_message_id") is not None, True)
check("member is told to wait, not that they're accepted",
      "wait" in query.edited.lower() or "hear back" in query.edited.lower(), True)

# Now with a claim on file - the ping should name the claiming admin
m2 = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user2 = make_requester(uid=555004, name="Cara")
m2.add_notify_chat(CHAT, NOTIFY_A)
bot2 = FakeBot()
m2.record_pending_request(CHAT, user2.id)
asyncio.run(m2.notify_admin_groups_of_join(bot2, CHAT, user2, screening_sent=True))
m2.record_join_claim(CHAT, user2.id, 111, "Priya", NOTIFY_A, 5000)
m2.set_screening(CHAT, user2.id, "answered", q1=no_kw_answer, q2="",
                 flags=m2.score_answer(m2.combined_answer(no_kw_answer, "")))
m2.record_read_ack(CHAT, user2.id)
query2 = FakeQuery(f"tc_agree:{CHAT}", user2)
asyncio.run(m2.on_agree(FakeUpdate(query2), Ctx(bot2)))
ping2 = bot2.sent[-1][1]
check("claimed request pings the claiming admin by name",
      "Priya" in ping2, True)
check("claimed ping addresses them via a real mention (notifies them like an @)",
      'tg://user?id=111' in ping2, True)


# ---------------------------------------------------------------- bug 2: redundant re-prompt after screening
print("\n[4] admin manually approving someone who ALREADY agreed does not re-ask them to agree")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = make_requester(uid=555005, name="Dee")
admin = make_admin(111, "Admin One")
m.add_notify_chat(CHAT, NOTIFY_A)
bot = FakeBot()
m.record_pending_request(CHAT, user.id)
asyncio.run(m.notify_admin_groups_of_join(bot, CHAT, user, screening_sent=True))

# They screen with a no-keyword answer (so it needs a human), then agree.
m.set_screening(CHAT, user.id, "answered", q1=no_kw_answer, q2="",
                flags=m.score_answer(m.combined_answer(no_kw_answer, "")))
m.record_read_ack(CHAT, user.id)
q = FakeQuery(f"tc_agree:{CHAT}", user)
asyncio.run(m.on_agree(FakeUpdate(q), Ctx(bot)))
check("they have agreed and are awaiting an admin",
      (m.has_accepted(CHAT, user.id), m.get_screening(CHAT, user.id)[0]),
      (True, "awaiting_admin"))

# Admin now approves.
sent_before = len(bot.sent)
approve_q = FakeQuery(f"jn_approve:{CHAT}:{user.id}", admin)
asyncio.run(m.on_admin_approve(FakeUpdate(approve_q), Ctx(bot)))

check("admin-approved flag is now set", m.was_admin_approved(CHAT, user.id), True)
dm_to_user = [t for c, t, kw in bot.sent[sent_before:] if c == user.id]
check("exactly one DM went to the member", len(dm_to_user), 1)
check("it's a plain confirmation, NOT another 'please accept the rules' prompt",
      "already agreed" in dm_to_user[0] and "Tap below to accept" not in dm_to_user[0], True)
approve_kw = [kw for c, t, kw in bot.sent[sent_before:] if c == user.id][0]
check("...and it carries no Agree button, since there is nothing left to agree to",
      approve_kw.get("reply_markup") is None, True)
check("the alert was finalized with the approval line",
      any("Approved by" in t for _, _, t, _ in bot.edits), True)


print("\n[5] the same admin-approve path WITHOUT prior screening still sends the T&C prompt")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = make_requester(uid=555006, name="Eve")
admin = make_admin(222, "Admin Two")
m.add_notify_chat(CHAT, NOTIFY_A)
bot = FakeBot()
m.record_pending_request(CHAT, user.id)
asyncio.run(m.notify_admin_groups_of_join(bot, CHAT, user, screening_sent=False))
# No screening answer on file at all - admin approves on judgement alone.
approve_q = FakeQuery(f"jn_approve:{CHAT}:{user.id}", admin)
asyncio.run(m.on_admin_approve(FakeUpdate(approve_q), Ctx(bot)))
dm = [t for c, t, kw in bot.sent if c == user.id][-1]
check("unscreened approval DOES get the T&C-and-button prompt",
      "Tap below to accept" in dm, True)
kw = [kw for c, t, kw in bot.sent if c == user.id][-1]
check("...with a working Agree button attached",
      kw.get("reply_markup") is not None, True)


print("\n[6] a later Agree tap after manual approval does not re-trigger the screening verdict")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = make_requester(uid=555007, name="Fay")
admin = make_admin(111, "Admin One")
m.add_notify_chat(CHAT, NOTIFY_A)
bot = FakeBot()
m.record_pending_request(CHAT, user.id)
asyncio.run(m.notify_admin_groups_of_join(bot, CHAT, user, screening_sent=False))
# Admin approves someone who never screened at all (no screening row exists).
approve_q = FakeQuery(f"jn_approve:{CHAT}:{user.id}", admin)
asyncio.run(m.on_admin_approve(FakeUpdate(approve_q), Ctx(bot)))
check("no screening row exists for them", m.get_screening(CHAT, user.id)[0], None)

# They now tap the T&C button the admin's DM sent them.
sent_before = len(bot.sent)
m.record_read_ack(CHAT, user.id)
agree_q = FakeQuery(f"tc_agree:{CHAT}", user)
asyncio.run(m.on_agree(FakeUpdate(agree_q), Ctx(bot)))
check("this does NOT get sent back into 'awaiting_admin' "
      "(would happen if the verdict gate fired again)",
      m.get_screening(CHAT, user.id)[0] != "awaiting_admin", True)
check("no second ping was sent to admins - the human already decided",
      not any("needs your decision" in t or "needs a decision" in t
              for _, t, _ in bot.sent[sent_before:]), True)
check("they end up accepted and unlocked, same as any other successful Agree",
      m.has_accepted(CHAT, user.id), True)
check("member sees the normal accepted confirmation",
      "Accepted" in agree_q.edited, True)


print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
