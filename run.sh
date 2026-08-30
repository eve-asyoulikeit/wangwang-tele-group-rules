#!/data/data/com.termux/files/usr/bin/bash
# Launcher for the T&C gate bot under Termux.
#   chmod +x run.sh && ./run.sh
#
# Android will eventually kill this regardless of what you do. The loop below
# means it comes back rather than staying dead silently.

cd "$(dirname "$0")" || exit 1

# main13.py writes its own detailed log (LOG_FILE, default ~/bot.log).
# This captures only launcher lines plus whatever main13.py prints to stdout,
# which is now WARNING and above.
LOG="${HOME}/bot-console.log"
mkdir -p "$(dirname "$LOG")"

# CONFIG. Previously this file was never read: an interactive launch worked only
# because the variables happened to be exported in whichever shell was used,
# while a Termux:Boot launch - which starts from a near-empty environment - got
# nothing, failed on the missing BOT_TOKEN, and looped silently forever.
# gate-bot.env uses bare assignments with no `export`, so plain `.` would set
# shell variables that python3 never inherits. `set -a` is what exports them.
ENV_FILE="${GATE_BOT_ENV:-$HOME/gate-bot.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    echo "$(date -Is) loaded config from $ENV_FILE" >> "$LOG"
else
    echo "$(date -Is) FATAL: $ENV_FILE not found - refusing to start" >> "$LOG"
    exit 1
fi

# Stops Android suspending the process during doze. termux-wake-lock ships with
# Termux itself (termux-tools) - no addon app required.
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

# Outstanding-members watcher, OFF by default.
#
# This is what printed a "pending: N have not accepted" block into the terminal
# every five minutes. It is off now for two reasons, not just the noise:
#
#   * The query below has no chat_id filter, so it pools every chat the bot has
#     ever seen into one count, and it counts admins and members who have since
#     left - which is what its own "unverified" caveat is admitting to.
#   * /pending answers the same question on demand and checks each member
#     against Telegram first, so the number it gives is a real one.
#
# Set PENDING_EVERY=300 in gate-bot.env if you ever want the poller back.
PENDING_EVERY="${PENDING_EVERY:-0}"

watch_pending() {
    while true; do
        sleep "$PENDING_EVERY"
        python3 - <<'PYEOF'
import os, sqlite3, datetime
db  = os.environ.get("DB_PATH", "acceptances.db")
ver = os.environ.get("TERMS_VERSION", "1")
ts  = datetime.datetime.now().strftime("%H:%M:%S")
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = c.execute("""
        SELECT s.user_id, s.username, s.full_name
        FROM seen_users s
        LEFT JOIN acceptances a
          ON a.chat_id = s.chat_id AND a.user_id = s.user_id
         AND a.terms_version = ?
        WHERE a.user_id IS NULL
        ORDER BY s.last_seen DESC
    """, (ver,)).fetchall()
except sqlite3.Error as e:
    print("[%s] pending: cannot read %s (%s)" % (ts, db, e))
    raise SystemExit
if not rows:
    print("[%s] pending: nobody outstanding" % ts)
else:
    print("[%s] pending: %d have not accepted "
          "(unverified - may include admins and members who left)"
          % (ts, len(rows)))
    for uid, uname, fname in rows[:10]:
        print("            %s%s  id=%s"
              % (fname or uid, (" @%s" % uname) if uname else "", uid))
    if len(rows) > 10:
        print("            ...and %d more - use /pending for the verified list"
              % (len(rows) - 10))
PYEOF
    done
}

if [ "$PENDING_EVERY" -gt 0 ] 2>/dev/null; then
    watch_pending &
    WATCHER_PID=$!
fi

cleanup() {
    echo "$(date -Is) launcher stopping" >> "$LOG"
    [ -n "$WATCHER_PID" ] && kill "$WATCHER_PID" 2>/dev/null
    command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock
    exit 0
}
trap cleanup INT TERM

while true; do
    echo "$(date -Is) starting main13.py" >> "$LOG"
    # tee so the terminal sees output too. PIPESTATUS, not $?, or the exit-1
    # backoff below reads tee's status instead and never triggers.
    python3 main13.py 2>&1 | tee -a "$LOG"
    code=${PIPESTATUS[0]}
    echo "$(date -Is) main13.py exited with $code" >> "$LOG"

    # A config error (missing BOT_TOKEN, bad ADMIN_IDS) will fail instantly and
    # forever. Do not hot-loop on it.
    if [ $code -eq 1 ]; then
        echo "$(date -Is) exit 1 - likely config error, check the log" >> "$LOG"
        sleep 60
    else
        sleep 5
    fi
done