import asyncio, os, sys, tempfile
TMP = tempfile.mkdtemp(); DB = os.path.join(TMP, "t.db")
os.environ.update(BOT_TOKEN="123:FAKE", ADMIN_IDS="111,222", DB_PATH=DB,
                  LOG_FILE=os.path.join(TMP, "t.log"), TERMS_TEXT="T",
                  ERROR_NOTIFY_COOLDOWN="300")
sys.path.insert(0, "/home/user/wangwang-tele-group-rules")
import main13 as m

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(chat_id)

ok = fail = 0
def check(l, g, w):
    global ok, fail
    (globals().__setitem__('ok', ok+1), print(f"  PASS  {l}")) if g == w else \
    (globals().__setitem__('fail', fail+1), print(f"  FAIL  {l}: got={g!r} want={w!r}"))

async def main():
    b = Bot()
    await m.notify_admins(b, "boom", key="unhandled")
    check("first error DMs both admins", sorted(b.sent), [111, 222])

    await m.notify_admins(b, "boom", key="unhandled")
    check("same process, within cooldown -> silent", sorted(b.sent), [111, 222])

    # simulate the process dying and run.sh respawning it: in-memory state gone,
    # database survives.
    m._last_notify.clear()
    b2 = Bot()
    await m.notify_admins(b2, "boom", key="unhandled")
    check("after a restart, still throttled (the actual fix)", b2.sent, [])

    # a different failure is a different key and must still get through
    b3 = Bot()
    await m.notify_admins(b3, "other", key="unlock-fail:-100123")
    check("an unrelated key is not suppressed", sorted(b3.sent), [111, 222])

    # once the cooldown genuinely elapses it speaks again
    m.runtime_set("notify_last:unhandled", 1.0)
    m._last_notify.clear()
    b4 = Bot()
    await m.notify_admins(b4, "boom", key="unhandled")
    check("after the cooldown elapses it reports again", sorted(b4.sent), [111, 222])

asyncio.run(main())
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
