"""Exercise the join-screening flow against the real functions."""
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

# ------------------------------------------------------- answer scoring
print("\n[1] red flags on an answer")
m = load(SCREENING_ENABLED=1)
GOOD = "A friend sent it to me on Instagram. I want to help with animal rescue in my area."
check("substantive, on-topic answer -> clean", m.score_answer(GOOD), [])
check("empty -> no-answer", m.score_answer("   "), ["no-answer"])
check("'yes' -> too short and off-topic",
      sorted(m.score_answer("yes")), ["no-keyword", "too-short"])
check("promo link is flagged even when it says the right words",
      sorted(m.score_answer("I love animal rescue, join my channel https://t.me/spam now")),
      ["contains-link"])
check("bare @handle is flagged too",
      "contains-link" in m.score_answer("found via @somepromochannel, i like animals and rescue work"), True)
check("Chinese answer matches without word boundaries",
      m.score_answer("朋友介绍的，我想帮助流浪动物，反对虐待动物。"), [])
check("long but entirely off-topic -> no-keyword",
      m.score_answer("I was just browsing around one evening and thought this looked interesting enough"),
      ["no-keyword"])

# ------------------------------------------------------- verdict, auto OFF
print("\n[2] verdict with SCREENING_AUTO_APPROVE off (the default)")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=0)
m.set_screening(CHAT, USER, "answered", answer=GOOD, flags=m.score_answer(GOOD))
check("even a perfect answer goes to a human", m.screening_verdict(CHAT, USER), ("review", []))

# ------------------------------------------------------- verdict, auto ON
print("\n[3] verdict with SCREENING_AUTO_APPROVE on")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
m.set_screening(CHAT, USER, "answered", answer=GOOD, flags=m.score_answer(GOOD))
check("clean answer -> auto", m.screening_verdict(CHAT, USER), ("auto", []))

m.set_screening(CHAT, USER, "answered", answer="yes", flags=m.score_answer("yes"))
v, f = m.screening_verdict(CHAT, USER)
check("'yes' cannot buy its way in", v, "review")

spam = "I love animal rescue and stray dogs, see https://t.me/x"
m.set_screening(CHAT, USER, "answered", answer=spam, flags=m.score_answer(spam))
check("keywords present but a link -> still a human", m.screening_verdict(CHAT, USER)[0], "review")

m.set_screening(CHAT, USER, "asked")
check("asked but never answered -> review", m.screening_verdict(CHAT, USER), ("review", ["no-answer"]))
check("never screened at all -> review", m.screening_verdict(CHAT, 777), ("review", ["not-screened"]))

# ------------------------------------------------------- state machine
print("\n[4] who owes an answer")
m = load(SCREENING_ENABLED=1)
check("nobody outstanding initially", m.awaiting_answer(USER), None)
m.set_screening(CHAT, USER, "asked")
check("after being asked, we are waiting on them", m.awaiting_answer(USER), CHAT)
m.set_screening(CHAT, USER, "answered", answer=GOOD, flags=[])
check("after answering, no longer waiting", m.awaiting_answer(USER), None)
state, ans, flags = m.get_screening(CHAT, USER)
check("answer is stored verbatim", (state, ans), ("answered", GOOD))

# ------------------------------------------------------- dm target (finding 03)
print("\n[5] DM target uses user_chat_id")
m = load(SCREENING_ENABLED=1)
m.record_pending_request(CHAT, USER, user_chat_id=555000111)
check("prefers the user_chat_id from the request", m.dm_target(CHAT, USER), 555000111)
m.record_pending_request(CHAT, 4242)
check("falls back to user id when none captured", m.dm_target(CHAT, 4242), 4242)
check("unknown user falls back too", m.dm_target(CHAT, 31337), 31337)

# ------------------------------------------------------- alert content + edit
print("\n[6] admin alert carries bio and invite link, and is edited in place")
m = load(SCREENING_ENABLED=1)

class Link:  name = "IG bio Aug26"
class Req:
    bio = "crypto trader DM me"
    invite_link = Link()
    user_chat_id = 900900
class U:
    id, first_name, username, full_name, is_bot = USER, "Ada", "ada_l", "Ada L", False

txt = m.build_join_alert_text(U(), Req())
check("bio surfaced on the alert", "crypto trader DM me" in txt, True)
check("which link they used is surfaced", "IG bio Aug26" in txt, True)
check("no request object still renders", "New join request" in m.build_join_alert_text(U()), True)

class Msg:
    def __init__(s, i): s.message_id = i
class Bot:
    def __init__(s): s.sent, s.edits = [], []
    async def send_message(s, chat_id, text, **kw):
        s.sent.append((chat_id, text)); return Msg(1000 + len(s.sent))
    async def edit_message_text(s, chat_id, message_id, text, **kw):
        s.edits.append((chat_id, message_id, text))

async def flow():
    NOTIFY = -1009999999999
    m.add_notify_chat(CHAT, NOTIFY)
    b = Bot()
    m.record_pending_request(CHAT, USER, user_chat_id=900900)
    await m.notify_admin_groups_of_join(b, CHAT, U(), req=Req())
    check("alert message id remembered for later editing",
          [c for c, _ in m.get_join_alerts(CHAT, USER)], [NOTIFY])

    await m.update_join_alerts_with_answer(b, CHAT, U(), GOOD, [])
    check("answer attached by EDITING, not a second message",
          (len(b.sent), len(b.edits)), (1, 1))
    check("edited alert shows the answer", GOOD in b.edits[0][2], True)
    check("edited alert shows the verdict", "no flags" in b.edits[0][2], True)

    await m.update_join_alerts_with_answer(b, CHAT, U(), "yes", ["too-short", "no-keyword"])
    check("flags are spelled out for the admin", "too-short" in b.edits[1][2], True)

asyncio.run(flow())

# ------------------------------------------------------- script fairness
print("\n[7] one threshold, several scripts")
m = load(SCREENING_ENABLED=1)
cases = [
    ("English, substantive", "A friend sent it to me on Instagram, I want to help with animal rescue.", []),
    ("Chinese, substantive", "朋友介绍的，我想帮助流浪动物，反对虐待动物。", []),
    ("Chinese, terse brush-off", "动物", ["too-short"]),
    ("Malay, substantive", "Kawan saya hantar. Saya mahu tolong animal rescue di sini.", []),
    ("English one-worder", "animals", ["too-short"]),
]
for label, text, want in cases:
    check(f"{label} -> {want or 'clean'}", m.score_answer(text), want)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
