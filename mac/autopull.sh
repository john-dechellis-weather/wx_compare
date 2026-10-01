#!/bin/zsh
# BlueMet auto-deploy on the Mac mini: every few minutes, if GitHub
# main has moved, pull it and restart the Streamlit service.
# Installed as a LaunchDaemon (org.bluemet.autopull.plist), so it runs
# as root; git runs as the site user so file ownership stays theirs.
# Log: ~USER/bluemet/autopull.log
USER_NAME="${BLUEMET_USER:-johndechellis}"
REPO="/Users/$USER_NAME/bluemet/wx_compare"
LOG="/Users/$USER_NAME/bluemet/autopull.log"
BRANCH="main"

log() { echo "$(date -u '+%Y-%m-%d %H:%MZ') $*" >> "$LOG"; }
asuser() { su -l "$USER_NAME" -c "cd '$REPO' && $*"; }

[ -d "$REPO/.git" ] || { log "no repo at $REPO"; exit 0; }

# A lock file left by a crashed git blocks everything; older than 10
# minutes means nobody is using it.
if [ -f "$REPO/.git/index.lock" ] && [ -n "$(find "$REPO/.git/index.lock" -mmin +10)" ]; then
  rm -f "$REPO/.git/index.lock" && log "removed stale index.lock"
fi

asuser "git fetch -q origin $BRANCH" || { log "fetch failed"; exit 0; }
LOCAL=$(asuser "git rev-parse HEAD")
REMOTE=$(asuser "git rev-parse origin/$BRANCH")
[ "$LOCAL" = "$REMOTE" ] && exit 0

# Only fast-forward. Local edits or commits that are not on GitHub
# stop the pull rather than being merged or thrown away.
if asuser "git pull -q --ff-only origin $BRANCH"; then
  log "updated ${LOCAL:0:7} -> ${REMOTE:0:7}: $(asuser "git log -1 --format=%s")"
  launchctl kickstart -k system/org.bluemet.streamlit && log "streamlit restarted"
else
  log "pull NOT fast-forward (local changes?) - left alone; fix by hand: git status"
fi
