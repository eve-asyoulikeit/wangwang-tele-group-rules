"""Exercise the 3-message join-screening flow against the real functions."""
import asyncio, importlib, os, sys, tempfile

TMP = tempfile.mkdtemp()
BASE = dict(BOT_TOKEN="123:FAKE", ADMIN_IDS="111,222",
            LOG_FILE=os.path.join(TMP, "t.log"), TERMS_TEXT="T",
            RULES_CHANNEL_URL="https://example.org/rules")
sys.path.insert(0, "/home/user/wangwang-tele-group-rules")

ok = fail = 0
def check(label, got, want):
    global ok, fail
    if got == want: ok += 1; print(f"  PASS  {label}")
    else: fail += 1; print(f"  FAIL  {label}\n          got={got!r}\n         want={want!r}")

def load(**over):
    """Re-import main13 with a fresh DB and given config."""
    for k in list(os.environ):
        if k.startswith("SCREENING") or k in BASE or k == "DB_PATH":
            os.environ.pop(k, None)
    os.environ.update(BASE)
    os.environ["DB_PATH"] = os.path.join(TMP, f"db{len(over)}{hash(str(over))&0xffff}.db")
    os.environ.update({k: str(v) for k, v in over.items()})
    sys.modules.pop("main13", None)
    return importlib.import_module("main13")

CHAT, USER = -1001234567890, 99001
GOOD_Q1 = "My friend told me."
GOOD_Q2 = "I want to help because it is the wang wang memorial group."

# ------------------------------------------------------- passphrase scoring
print("\n[1] the passphrase decides, length and links veto (unchanged)")
m = load(SCREENING_ENABLED=1)
check("configured passphrase is exactly the two agreed terms",
      m.SCREENING_KEYWORDS, ["wang wang", "memorial"])
check("substantive, on-topic combined answer -> clean",
      m.score_answer(m.combined_answer(GOOD_Q1, GOOD_Q2)), [])
check("empty -> no-answer", m.score_answer("   "), ["no-answer"])
check("bare passphrase is too short to be an answer",
      sorted(m.score_answer("wang wang")), ["too-short"])
check("passphrase plus a promo link -> vetoed",
      m.score_answer("here for the wang wang memorial, also join https://t.me/spam"),
      ["contains-link"])
check("'WangWang' (no space) still matches",
      m.score_answer("A friend told me about the WangWang group and I want to help."),
      [])

print("\n[2] combined scoring is fair to a short-but-honest Q1")
alone = m.score_answer(GOOD_Q1)
check("Q1 alone ('a friend sent me the link') fails length on its own",
      "too-short" in alone, True)
combined = m.score_answer(m.combined_answer(GOOD_Q1, GOOD_Q2))
check("but combined with Q2, the same Q1 is not penalised", combined, [])

# ------------------------------------------------------- state machine
print("\n[3] the state machine: asked_q1 -> asked_q2 -> answered")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
check("nobody awaiting anything yet", m.awaiting_reply(USER), (None, None))

m.set_screening(CHAT, USER, "asked_q1")
check("after Q1 is sent, we are waiting on a reply to it",
      m.awaiting_reply(USER), (CHAT, "asked_q1"))
check("get_screening reflects the fresh state",
      m.get_screening(CHAT, USER), ("asked_q1", "", "", []))

m.set_screening(CHAT, USER, "asked_q2", q1=GOOD_Q1)
check("Q1 stored, now waiting on Q2", m.awaiting_reply(USER), (CHAT, "asked_q2"))
check("Q1 text preserved exactly",
      m.get_screening(CHAT, USER)[1], GOOD_Q1)

flags = m.score_answer(m.combined_answer(GOOD_Q1, GOOD_Q2))
m.set_screening(CHAT, USER, "answered", q1=GOOD_Q1, q2=GOOD_Q2, flags=flags)
check("both answers on file, verdict is auto (clean + auto-approve on)",
      m.screening_verdict(CHAT, USER), ("auto", []))
check("'answered' still counts as awaiting - they may add more before Agree",
      m.awaiting_reply(USER), (CHAT, "answered"))

print("\n[4] set_screening never blanks an answer it wasn't given")
m2 = load(SCREENING_ENABLED=1)
m2.set_screening(CHAT, 5001, "asked_q2", q1="first answer")
m2.set_screening(CHAT, 5001, "answered", q2="second answer")  # q1 omitted
check("advancing state without q1 leaves q1 untouched",
      m2.get_screening(CHAT, 5001)[1], "first answer")
check("q2 was recorded", m2.get_screening(CHAT, 5001)[2], "second answer")

# ------------------------------------------------------- verdict edge cases
print("\n[5] verdict")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
m.set_screening(CHAT, USER, "answered", q1=GOOD_Q1, q2=GOOD_Q2,
                flags=m.score_answer(m.combined_answer(GOOD_Q1, GOOD_Q2)))
check("vouched, substantive, link-free -> auto", m.screening_verdict(CHAT, USER), ("auto", []))

no_kw = "Found it on Instagram, I care about animals a lot and want to help out here."
m.set_screening(CHAT, USER, "answered", q1=no_kw, q2="",
                flags=m.score_answer(m.combined_answer(no_kw, "")))
check("no passphrase -> a human looks", m.screening_verdict(CHAT, USER)[0], "review")

m.set_screening(CHAT, USER, "asked_q1")
check("still on Q1 -> review, no-answer", m.screening_verdict(CHAT, USER), ("review", ["no-answer"]))
m.set_screening(CHAT, USER, "asked_q2", q1=GOOD_Q1)
check("on Q2, Q1 answered but Q2 not yet -> still review",
      m.screening_verdict(CHAT, USER), ("review", ["no-answer"]))
check("never screened -> review", m.screening_verdict(CHAT, 777), ("review", ["not-screened"]))

m3 = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=0)
m3.set_screening(CHAT, USER, "answered", q1=GOOD_Q1, q2=GOOD_Q2,
                 flags=m3.score_answer(m3.combined_answer(GOOD_Q1, GOOD_Q2)))
check("with auto-approve off, even a vouched answer waits",
      m3.screening_verdict(CHAT, USER), ("review", []))

# ------------------------------------------------------- the 3 DMs, in order
print("\n[6] on_join_request -> on_private_message drives Q1, Q2, Q3 in order")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)

class Link:  name = "IG bio Aug26"
class Req:
    bio = "crypto trader DM me"
    invite_link = Link()
    user_chat_id = 900900
    class from_user:
        id, first_name, username, full_name, is_bot = USER, "Ada", "ada_l", "Ada L", False
class ChatObj: id, title = CHAT, "Say No To Animal Abuse"

class Msg:
    _next = [1000]
    def __init__(s, i=None):
        if i is None:
            Msg._next[0] += 1; i = Msg._next[0]
        s.message_id = i
class Bot:
    def __init__(s): s.sent, s.edits = [], []
    async def send_message(s, chat_id, text, **kw):
        s.sent.append((chat_id, text, kw)); return Msg()
    async def edit_message_text(s, chat_id, message_id, text, **kw):
        s.edits.append((chat_id, message_id, text, kw))
    async def approve_chat_join_request(s, chat_id, user_id): pass
    async def get_chat_member(s, chat_id, user_id):
        class M: status = "member"
        return M()

async def scenario_full_flow():
    b = Bot()
    m.add_notify_chat(CHAT, -1009999999999)
    m.record_pending_request(CHAT, USER, user_chat_id=900900)

    sent = await m.send_screening_q1(b, ChatObj(), Req())
    check("Q1 DM sent successfully", sent, True)
    check("state advanced to asked_q1", m.get_screening(CHAT, USER)[0], "asked_q1")
    q1_text = b.sent[0][1]
    check("Q1 message matches the agreed wording",
          "How did you hear about this group" in q1_text, True)
    check("Q2 and Q3 text are NOT in the first message",
          "Why do you want to join" not in q1_text
          and "terms and conditions" not in q1_text, True)

    ok_ = await m.notify_admin_groups_of_join(b, CHAT, Req.from_user, req=Req(),
                                              screening_sent=sent)
    check("initial admin alert posted", ok_, True)
    check("alert shows 'waiting on a reply', not the old wording",
          "Waiting on a reply" in b.sent[-1][1], True)

    class UpdMsg:
        def __init__(s, text, chat_id=USER):
            s.text, s.chat_id = text, chat_id
    class EffUser:
        id, first_name, username, full_name, is_bot = USER, "Ada", "ada_l", "Ada L", False
    class Upd:
        def __init__(s, text): s.effective_message = UpdMsg(text); s.effective_user = EffUser()
    class Ctx:
        def __init__(s, bot): s.bot = bot

    await m.on_private_message(Upd(GOOD_Q1), Ctx(b))
    check("after answering Q1, state moves to asked_q2",
          m.get_screening(CHAT, USER)[0], "asked_q2")
    check("Q1 stored verbatim", m.get_screening(CHAT, USER)[1], GOOD_Q1)
    q2_text = b.sent[-1][1]
    check("Q2 was sent next", "Why do you want to join this group" in q2_text, True)
    check("Q2 message does not repeat Q1 or jump to Q3",
          "How did you hear" not in q2_text and "terms and conditions" not in q2_text, True)

    before_edits = len(b.edits)
    await m.on_private_message(Upd(GOOD_Q2), Ctx(b))
    check("after answering Q2, state moves to answered",
          m.get_screening(CHAT, USER)[0], "answered")
    check("the admin alert was EDITED (not a new message) with both answers",
          len(b.edits), before_edits + 1)
    check("edited alert shows Q1 and Q2 separately",
          GOOD_Q1 in b.edits[-1][2] and GOOD_Q2 in b.edits[-1][2], True)
    check("edited alert keeps live buttons (still a decision pending)",
          b.edits[-1][3].get("reply_markup") is not None, True)
    q3_text = b.sent[-1][1]
    check("Q3 (terms) sent after Q2 answered",
          "terms and conditions" in q3_text, True)
    check("Q3 carries the rules link",
          "example.org/rules" in q3_text, True)
    check("Q3 message came with the Agree/Read buttons",
          b.sent[-1][2].get("reply_markup") is not None, True)

    # A follow-up before tapping Agree: appended, rescored, no new buttons sent
    sent_before = len(b.sent)
    await m.on_private_message(Upd("also I love dogs"), Ctx(b))
    check("follow-up text appended to q2",
          "also I love dogs" in m.get_screening(CHAT, USER)[2], True)
    check("no additional outbound message for a follow-up (buttons not resent)",
          len(b.sent), sent_before)

asyncio.run(scenario_full_flow())

# ------------------------------------------------------- alert content
print("\n[7] admin alert carries bio and invite link")
m = load(SCREENING_ENABLED=1)
txt = m.build_join_alert_text(Req.from_user, Req())
check("bio surfaced on the alert", "crypto trader DM me" in txt, True)
check("which link they used is surfaced", "IG bio Aug26" in txt, True)
check("no request object still renders", "New join request" in m.build_join_alert_text(Req.from_user), True)
check("trailer: could not reach them",
      "could not DM them" in (m.build_join_alert_text(Req.from_user) + m._live_trailer(False)), True)
check("trailer: waiting on a reply",
      "Waiting on a reply" in (m.build_join_alert_text(Req.from_user) + m._live_trailer(True)), True)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
