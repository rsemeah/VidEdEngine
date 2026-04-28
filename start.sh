#!/bin/bash
# VideoEngine — RedLantern Studios
set -e
cd "$(dirname "$0")"

echo ""
echo "=================================================="
echo "  VideoEngine — Pre-flight check"
echo "=================================================="

# Python 3
if ! command -v python3 &>/dev/null; then
  echo "❌ python3 not found. Install from python.org"
  exit 1
fi
echo "✓ python3: $(python3 --version)"

# ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "⚠  ffmpeg not found. Install with: brew install ffmpeg"
else
  echo "✓ ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
fi

# pip packages
for pkg in anthropic whisper; do
  python3 -c "import $pkg" 2>/dev/null && echo "✓ $pkg" || echo "⚠  $pkg not installed (pip install $pkg)"
done

echo ""
echo "  Starting server on port 8765..."
echo "=================================================="
echo ""

python3 server.py
