import os, sys, tempfile
TMP=tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:FAKE", ADMIN_IDS="111", DB_PATH=os.path.join(TMP,"t.db"),
                  LOG_FILE=os.path.join(TMP,"t.log"), TERMS_TEXT="T")
sys.path.insert(0,"/home/user/wangwang-tele-group-rules")
import main13 as m
from telegram import ChatMemberAdministrator, ChatMemberMember, ChatMemberLeft, ChatMemberBanned, User

BOT = User(id=1, first_name="gate", is_bot=True)
def admin(restrict=True, invite=True, delete=True, pin=True):
    return ChatMemberAdministrator(user=BOT, can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=delete, can_manage_video_chats=False,
        can_restrict_members=restrict, can_promote_members=False, can_change_info=False,
        can_invite_users=invite, can_pin_messages=pin,
        can_post_stories=False, can_edit_stories=False, can_delete_stories=False)

ok=fail=0
def check(l,g,w):
    global ok,fail
    if g==w: ok+=1; print(f"  PASS  {l}")
    else: fail+=1; print(f"  FAIL  {l}\n          got={g!r}\n         want={w!r}")

def delta(a,b):
    lost, blocking, regained = m.own_rights_delta(m.describe_own_rights(a), m.describe_own_rights(b))
    return (len(lost), blocking, regained)

print("\nbot's own rights transitions -> (n_lost, blocking, regained)")
check("nothing changed -> no loss, not blocking",
      delta(admin(), admin()), (0, False, False))
check("demoted to plain member -> blocking",
      delta(admin(), ChatMemberMember(user=BOT)), (1, True, False))
check("removed from group -> blocking",
      delta(admin(), ChatMemberLeft(user=BOT)), (1, True, False))
check("banned -> blocking",
      delta(admin(), ChatMemberBanned(user=BOT, until_date=None)), (1, True, False))
check("lost 'restrict members' only -> blocking (cannot gate anyone)",
      delta(admin(), admin(restrict=False)), (1, True, False))
check("lost 'invite users' only -> blocking (cannot approve requests)",
      delta(admin(), admin(invite=False)), (1, True, False))
check("lost 'pin' only -> degraded, NOT blocking",
      delta(admin(), admin(pin=False)), (1, False, False))
check("lost 'delete' only -> degraded, NOT blocking",
      delta(admin(), admin(delete=False)), (1, False, False))
check("lost both delete and pin -> two losses, not blocking",
      delta(admin(), admin(delete=False, pin=False)), (2, False, False))
check("promoted from member back to full admin -> regained",
      delta(ChatMemberMember(user=BOT), admin()), (0, False, True))
check("restrict right handed back -> regained",
      delta(admin(restrict=False), admin()), (0, False, True))
check("partial restore (still missing invite) -> not yet regained",
      delta(ChatMemberMember(user=BOT), admin(invite=False)), (0, True, False))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
