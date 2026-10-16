#!/bin/zsh
# Optional: start the Fast Deploy controller and tunnel at login and restart them after a crash (macOS launchd).
# Requires the GitHub token already saved in the Keychain by one interactive run of scripts/start.sh.
# Remove with: launchctl bootout gui/$(id -u)/edu.utsa.lab-deploy && rm ~/Library/LaunchAgents/edu.utsa.lab-deploy.plist
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
D="${LAB_DEPLOY_HOME:-$HOME/.local/share/lab-deploy}"
PLIST="$HOME/Library/LaunchAgents/edu.utsa.lab-deploy.plist"
mkdir -p "$D"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>edu.utsa.lab-deploy</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$REPO/scripts/start.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$D/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$D/launchd.err.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/edu.utsa.lab-deploy" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. The controller now starts at login and restarts after a crash. Logs: $D/launchd.*.log"
