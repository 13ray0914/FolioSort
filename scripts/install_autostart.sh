#!/usr/bin/env bash
set -euo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
RUNNER="$ROOT/scripts/run_review_pipeline.sh"
MARK_BEGIN="# >>> review-literature-pipeline >>>"
MARK_END="# <<< review-literature-pipeline <<<"
BASHRC="$HOME/.bashrc"
if [[ ! -x "$RUNNER" ]]; then
  echo "ERROR: $RUNNER is missing or not executable"
  exit 1
fi
if grep -Fq "$MARK_BEGIN" "$BASHRC" 2>/dev/null; then
  echo "Autostart is already installed in $BASHRC"
  exit 0
fi
cat >> "$BASHRC" <<EOF

$MARK_BEGIN
# Start once in the background when the first interactive Ubuntu shell opens.
# run_review_pipeline.sh uses flock, so opening more terminals does not duplicate the job.
if [[ -x "$RUNNER" ]]; then
  nohup "$RUNNER" >/dev/null 2>&1 &
fi
$MARK_END
EOF
echo "Installed autostart in $BASHRC"
echo "It will run automatically the next time you open Ubuntu/WSL."
