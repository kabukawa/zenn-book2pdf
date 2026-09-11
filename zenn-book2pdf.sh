#!/usr/bin/env bash
# zenn-book2pdf.sh -- Zenn の本を HTML/PDF/EPUB 化する起動ラッパー
# このファイルがあるフォルダを PATH に入れれば、どこからでも呼べます。
# EPUB には Calibre (ebook-convert) が必要です。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$ROOT/fetch_zenn_book.py"

if [[ ! -f "$PY_SCRIPT" ]]; then
  echo "fetch_zenn_book.py が見つかりません: $PY_SCRIPT" >&2
  exit 1
fi

python_ok() {
  # Windows の Git Bash では python3 が別系統で、依存が入っていないことがある
  "$1" -c "import requests,bs4,markdownify,markdown" >/dev/null 2>&1
}

pick_python() {
  local cand
  for cand in python python3; do
    if command -v "$cand" >/dev/null 2>&1 && python_ok "$cand"; then
      echo "$cand"
      return 0
    fi
  done
  if command -v py >/dev/null 2>&1 && py -3 -c "import requests,bs4,markdownify,markdown" >/dev/null 2>&1; then
    echo py
    return 0
  fi
  return 1
}

if ! PY="$(pick_python)"; then
  echo "必要な Python パッケージが見つかりません。" >&2
  echo "  pip install -r \"$ROOT/requirements.txt\"" >&2
  echo "  playwright install chromium" >&2
  exit 1
fi

cd "$ROOT"
if [[ "$PY" == py ]]; then
  exec py -3 "$PY_SCRIPT" "$@"
else
  exec "$PY" "$PY_SCRIPT" "$@"
fi
