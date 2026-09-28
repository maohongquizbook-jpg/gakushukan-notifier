#!/usr/bin/env bash
# GitHub Actions のチェックアウト内で使用。状態ファイル以外はコミットしない。
set -euo pipefail

state_file="${1:?state file is required}"
branch="${2:?branch is required}"
retry_delay="${STATE_PUSH_RETRY_DELAY:-7}"

case "$state_file" in
  state.json|state_fureai.json|state_chiiki.json) ;;
  *) echo "Unsupported state file" >&2; exit 1 ;;
esac
git check-ref-format "refs/heads/$branch"
if [ ! -f "$state_file" ]; then
  echo "No state created; nothing to persist"
  exit 0
fi

git config user.name "monitor-bot"
git config user.email "actions@users.noreply.github.com"
git add -- "$state_file"
if git diff --cached --quiet -- "$state_file"; then
  echo "State unchanged"
  exit 0
fi
git commit -m "update $state_file [skip ci]" -- "$state_file"

for attempt in 1 2 3 4 5; do
  # 別の監視の状態更新・コード更新を取り込んでから通常のpushを行う。
  if git pull --rebase origin "$branch" && git push origin "HEAD:refs/heads/$branch"; then
    echo "State persisted: $state_file"
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  echo "State persistence retry $attempt/5" >&2
  if [ "$attempt" -lt 5 ]; then
    sleep "$((attempt * retry_delay))"
  fi
done

echo "::error::State persistence failed. Restore the state artifact before rerunning notifications."
exit 1
