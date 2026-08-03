#!/bin/bash
# One-shot reminder for the next time a Claude Code session starts in
# this project. Fires exactly once (marker file), then goes quiet — this
# is meant to surface specific pending follow-ups from the prior session,
# not become a permanent nag on every future session start.
MARKER="/tmp/bazaar-services/pending-reminder-shown"
mkdir -p "$(dirname "$MARKER")"

if [ -f "$MARKER" ]; then
    exit 0
fi
touch "$MARKER"

echo '{"systemMessage": "Reminder from last session: (1) requirements-lock.txt still needs regenerating - xgboost was added to requirements.txt but the lock file was not updated. (2) Docker has not been rebuilt yet, so it is still running the old model and is missing the CSE mirror-fallback weekend-guard fix and the new XGBoost-based forward_return_rf model. Say the word when you want either done."}'
