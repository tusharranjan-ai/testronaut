#!/usr/bin/env bash
# Synthesizes narration for docs/assets/demo.gif via OpenAI TTS and muxes it
# in, producing docs/assets/demo.mp4. Requires OPENAI_API_KEY and ffmpeg.
set -euo pipefail
cd "$(dirname "$0")/.."

NARRATION="${1:-$(cat <<'EOF'
Testronaut turns an OpenAPI spec into reviewed, traceable test cases.
Every run goes through two approval gates. Gate one drafts functional
requirements per endpoint. Gate two generates test cases from those
approved requirements, each one traceable back to the requirement it
exercises. Filter by category, check coverage, then export to JSON or
Excel, ready for automation.
EOF
)}"

OUT_DIR=docs/assets
MP3="$(mktemp -t demo-voiceover).mp3"
MP4="$OUT_DIR/demo.mp4"
trap 'rm -f "$MP3"' EXIT

curl -sS https://api.openai.com/v1/audio/speech \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"model":"tts-1","voice":"onyx","input":sys.argv[1]}))' "$NARRATION")" \
  -o "$MP3"

DURATION="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$MP3")"

ffmpeg -y -stream_loop -1 -i "$OUT_DIR/demo.gif" -i "$MP3" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -t "$DURATION" \
  -vf "fps=10,scale=1512:-2" "$MP4"

echo "Wrote $MP4"
