"""Covers SCREENING_DEBOUNCE_SECONDS: a fast multi-message reply to Q1 or Q2
must be joined into one answer, not have its second half misfiled under the
next question. No real waiting - the fake job_queue lets tests fire a
scheduled callback on demand.
"""
import asyncio, importlib, os, sys, tempfile

TMP = tempfile.mkdtemp()
BASE = dict(BOT_TOKEN="123:FAKE", ADMIN_IDS="111", LOG_FILE=os.path.join(TMP, "t.log"),
            TERMS_TEXT="T", RULES_CHANNEL_URL="https://example.org/rules",
            SCREENING_ENABLED="1")
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

CHAT, USER = -1001234567890, 99001


class FakeJob:
    def __init__(s, name, callback, data):
        s.name, s.callback, s.data, s.removed = name, callback, data, False
    def schedule_removal(s):
        s.removed = True

class FakeJobQueue:
    """Mirrors just enough of PTB's JobQueue: run_once registers a job,
    get_jobs_by_name finds live (non-removed) ones so on_private_message can
    cancel-and-reschedule, and the test fires one manually via run_due()
    rather than waiting SCREENING_DEBOUNCE_SECONDS in real time."""
    def __init__(s): s.jobs = []
    def run_once(s, callback, when, data=None, name=None):
        job = FakeJob(name, callback, data)
        s.jobs.append(job)
        return job
    def get_jobs_by_name(s, name):
        return [j for j in s.jobs if j.name == name and not j.removed]
    async def run_due(s, name):
        """Simulate the debounce window elapsing for one job name: run the
        latest live job under that name, exactly as PTB would once nothing
        cancels it first."""
        live = s.get_jobs_by_name(name)
        assert live, f"no live job named {name!r} to fire"
        job = live[-1]

        class FakeJobContext:
            def __init__(s2): s2.data = job.data
        class FakeCtx:
            def __init__(s2, bot): s2.bot = bot; s2.job = FakeJobContext()
        await job.callback(FakeCtx(s._bot))

class FakeMsg:
    def __init__(s, text, chat_id): s.text, s.chat_id = text, chat_id
class FakeEffUser:
    def __init__(s, uid, name="Ada"):
        s.id, s.first_name, s.username, s.full_name, s.is_bot = uid, name, "ada_l", f"{name} L", False
class FakeUpdate:
    def __init__(s, text, user): s.effective_message = FakeMsg(text, user.id); s.effective_user = user

class FakeBot:
    def __init__(s):
        s.sent = []
        s.edits = []
    async def send_message(s, chat_id, text, **kw):
        s.sent.append((chat_id, text, kw))
        class M: message_id = 9999
        return M()
    async def edit_message_text(s, chat_id, message_id, text, **kw):
        s.edits.append((chat_id, message_id, text, kw))

class FakeCtx:
    def __init__(s, bot, job_queue):
        s.bot = bot
        s.job_queue = job_queue
        job_queue._bot = bot   # so run_due() can build its own FakeCtx later


print("\n[1] a message alone still finalizes once the window elapses")
m = load(SCREENING_AUTO_APPROVE=1)
user = FakeEffUser(USER)
m.set_gate_message(CHAT, 1)
m.record_pending_request(CHAT, user.id)
m.set_screening(CHAT, user.id, "asked_q1")
jq = FakeJobQueue()
bot = FakeBot()
ctx = FakeCtx(bot, jq)

asyncio.run(m.on_private_message(FakeUpdate("My friend told me.", user), ctx))
check("state has NOT advanced yet - still waiting out the window",
      m.get_screening(CHAT, user.id)[0], "asked_q1")
check("nothing sent yet either", bot.sent, [])

name = m._debounce_job_name(CHAT, user.id)
asyncio.run(jq.run_due(name))
check("after the window fires, state advances", m.get_screening(CHAT, user.id)[0], "asked_q2")
check("Q1 recorded", m.get_screening(CHAT, user.id)[1], "My friend told me.")
check("Q2 was sent", "Why do you want to join this group" in bot.sent[-1][1], True)


print("\n[2] the exact reported scenario: a reply split across two quick messages")
m = load(SCREENING_AUTO_APPROVE=1)
user = FakeEffUser(555002, "Nibbles")
m.set_gate_message(CHAT, 1)
m.record_pending_request(CHAT, user.id)
m.set_screening(CHAT, user.id, "asked_q1")
jq = FakeJobQueue()
bot = FakeBot()
ctx = FakeCtx(bot, jq)

asyncio.run(m.on_private_message(FakeUpdate("Updates on", user), ctx))
name = m._debounce_job_name(CHAT, user.id)
check("first fragment schedules exactly one job", len(jq.get_jobs_by_name(name)), 1)

# Second fragment arrives BEFORE the window elapses.
asyncio.run(m.on_private_message(FakeUpdate("justice", user), ctx))
check("still on asked_q1 - the second fragment did not get filed as Q2",
      m.get_screening(CHAT, user.id)[0], "asked_q1")  # pre-seeded; finalize hasn't run yet
check("the stale first job was cancelled", jq.get_jobs_by_name(name)[0].data["state"], "asked_q1")
check("exactly one LIVE job remains (old one cancelled, not left to double-fire)",
      len(jq.get_jobs_by_name(name)), 1)

asyncio.run(jq.run_due(name))
check("both fragments joined into ONE Q1 answer, not split across Q1/Q2",
      m.get_screening(CHAT, user.id), ("asked_q2", "Updates on\njustice", "", []))
check("Q2 was sent only once, only after the joined Q1 was finalized",
      len(bot.sent), 1)


print("\n[3] three-message burst, then the real Q2 answer")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = FakeEffUser(555003, "Gita")
m.set_gate_message(CHAT, 1)
m.record_pending_request(CHAT, user.id)
m.set_screening(CHAT, user.id, "asked_q1")
jq = FakeJobQueue()
bot = FakeBot()
ctx = FakeCtx(bot, jq)
name = m._debounce_job_name(CHAT, user.id)

for frag in ["A friend", "sent me", "the link on Instagram"]:
    asyncio.run(m.on_private_message(FakeUpdate(frag, user), ctx))
check("only one live job survives three rapid fragments", len(jq.get_jobs_by_name(name)), 1)
asyncio.run(jq.run_due(name))
check("all three fragments joined in order",
      m.get_screening(CHAT, user.id)[1], "A friend\nsent me\nthe link on Instagram")

asyncio.run(m.on_private_message(FakeUpdate("I want updates on the wang wang memorial case", user), ctx))
asyncio.run(jq.run_due(name))
state, q1, q2, flags = m.get_screening(CHAT, user.id)
check("Q2 recorded separately from the Q1 burst", q2, "I want updates on the wang wang memorial case")
check("passphrase in Q2 is found by combined scoring", flags, [])
check("-> auto-approves", m.screening_verdict(CHAT, user.id), ("auto", []))


print("\n[4] a follow-up added after both questions are answered (state='answered')")
m = load(SCREENING_ENABLED=1, SCREENING_AUTO_APPROVE=1)
user = FakeEffUser(555004, "Hana")
m.set_gate_message(CHAT, 1)
m.record_pending_request(CHAT, user.id)
m.add_notify_chat(CHAT, -1009999999999)
jq = FakeJobQueue()
bot = FakeBot()
ctx = FakeCtx(bot, jq)
name = m._debounce_job_name(CHAT, user.id)

m.set_screening(CHAT, user.id, "answered", q1="Found it online",
                q2="just curious", flags=m.score_answer(m.combined_answer("Found it online", "just curious")))
m.record_join_alert(CHAT, user.id, -1009999999999, 4242)

asyncio.run(m.on_private_message(FakeUpdate("also it's the wang wang memorial group", user), ctx))
check("edit not sent yet - still debouncing", bot.edits, [])
asyncio.run(jq.run_due(name))
check("follow-up appended to q2, not overwriting it",
      "just curious" in m.get_screening(CHAT, user.id)[2]
      and "wang wang memorial" in m.get_screening(CHAT, user.id)[2], True)
check("alert was edited exactly once after the window fired", len(bot.edits), 1)
check("verdict now clears via the added passphrase",
      m.screening_verdict(CHAT, user.id), ("auto", []))


print("\n[5] SCREENING_DEBOUNCE_SECONDS=0 disables debouncing entirely")
m = load(SCREENING_DEBOUNCE_SECONDS=0)
check("resolved to off", m.SCREENING_DEBOUNCE_SECONDS, 0)
user = FakeEffUser(555005)
m.set_gate_message(CHAT, 1)
m.record_pending_request(CHAT, user.id)
m.set_screening(CHAT, user.id, "asked_q1")
jq = FakeJobQueue()
bot = FakeBot()
ctx = FakeCtx(bot, jq)
asyncio.run(m.on_private_message(FakeUpdate("Instagram friend", user), ctx))
check("advances immediately - no waiting", m.get_screening(CHAT, user.id)[0], "asked_q2")
check("no job was ever scheduled", jq.jobs, [])


print("\n[6] missing job_queue extra falls back to immediate, never silently drops the message")
m = load(SCREENING_AUTO_APPROVE=1)  # debounce ON (default), but...
user = FakeEffUser(555006)
m.set_gate_message(CHAT, 1)
m.record_pending_request(CHAT, user.id)
m.set_screening(CHAT, user.id, "asked_q1")
bot = FakeBot()
class NoJobQueueCtx:
    def __init__(s, bot): s.bot = bot; s.job_queue = None   # matches real PTB when the extra isn't installed
asyncio.run(m.on_private_message(FakeUpdate("Instagram, a friend showed me", user), NoJobQueueCtx(bot)))
check("still advances immediately rather than waiting forever with job_queue absent",
      m.get_screening(CHAT, user.id)[0], "asked_q2")


print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
