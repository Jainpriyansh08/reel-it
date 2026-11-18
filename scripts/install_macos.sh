#!/usr/bin/env bash
# One-time setup on the Mac that runs the pipeline. Safe to re-run.
#   ./scripts/install_macos.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/.reel/venv"
LABEL="com.reel.tick"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
export REEL_CONFIG="$HOME/.config/reel/config.yaml"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
PY="$(brew --prefix python@3.12)/bin/python3.12"

[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$REPO[ml,dev]"

"$VENV/bin/reel" init

# Download the transcription model now (~1.6 GB) so the first night doesn't.
MODEL="$("$VENV/bin/python" -c "from reel.config import Config; print(Config.load('$REEL_CONFIG').transcribe['model'])")"
echo "Fetching Whisper model $MODEL ..."
"$VENV/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('$MODEL', device='cpu', compute_type='int8')"

WORK="$("$VENV/bin/python" -c "from reel.config import Config; print(Config.load('$REEL_CONFIG').work)")"
mkdir -p "$WORK/logs" "$(dirname "$PLIST")"
sed -e "s#__REEL__#$VENV/bin/reel#" \
    -e "s#__CONFIG__#$REEL_CONFIG#" \
    -e "s#__LOGS__#$WORK/logs#" \
    -e "s#__BREW__#$(brew --prefix)/bin#" \
    "$REPO/scripts/com.reel.tick.plist" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# Telegram bot: long-running, restarted by launchd if it dies.
BOT_LABEL="com.reel.bot"
BOT_PLIST="$HOME/Library/LaunchAgents/$BOT_LABEL.plist"
sed -e "s#__REEL__#$VENV/bin/reel#" \
    -e "s#__CONFIG__#$REEL_CONFIG#" \
    -e "s#__LOGS__#$WORK/logs#" \
    -e "s#__BREW__#$(brew --prefix)/bin#" \
    "$REPO/scripts/com.reel.bot.plist" > "$BOT_PLIST"
launchctl bootout "gui/$(id -u)/$BOT_LABEL" 2>/dev/null || true
if [ -n "${REEL_TELEGRAM_TOKEN:-}" ] || [ -f "$HOME/.config/reel/telegram_token" ]; then
  launchctl bootstrap "gui/$(id -u)" "$BOT_PLIST"
  BOT_STATE="running"
else
  BOT_STATE="NOT started: put your bot token in ~/.config/reel/telegram_token (chmod 600), then re-run this script"
fi

echo
echo "Installed. 'reel tick' now runs every 5 minutes. Telegram bot: $BOT_STATE"
echo "  Add to your shell: export REEL_CONFIG=$REEL_CONFIG; alias reel=$VENV/bin/reel"
echo "  Drop footage in:   $(grep '^inbox:' "$REEL_CONFIG" | awk '{print $2}')"
echo "  Check on it:       reel status    reel events    tail -f $WORK/logs/tick.err"
echo "  Stop it:           launchctl bootout gui/$(id -u)/$LABEL"
