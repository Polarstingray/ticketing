#!/usr/bin/env bash
#
# Encode the recorded walkthrough into the two committed deliverables.
#
# record-walkthrough.mjs writes a raw .webm (gitignored); this turns it into the
# MP4 linked from the README and the GIF embedded in it. Kept as a script rather
# than prose because the GIF needs a two-pass palette to avoid the banded, muddy
# output a naive one-liner produces.
#
#   cd frontend && node scripts/record-walkthrough.mjs
#   frontend/scripts/encode-walkthrough.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../docs/video" && pwd)"
SRC="$DIR/walkthrough.webm"
[[ -f "$SRC" ]] || { echo "no $SRC — run scripts/record-walkthrough.mjs first" >&2; exit 1; }

# MP4: full resolution, the "higher-quality" link in the README. A flat dark UI
# has very little high-frequency detail, so a high CRF costs nothing visible and
# keeps the file small enough to sit in the repo.
ffmpeg -y -loglevel error -i "$SRC" \
  -c:v libx264 -crf 30 -preset veryslow -pix_fmt yuv420p -movflags +faststart \
  "$DIR/walkthrough.mp4"

# GIF: 900px wide at 10fps — small enough to embed inline, legible enough to read
# the UI. Two passes: generate a palette from the whole clip, then apply it.
# The palette is capped and dithering disabled on purpose: the UI is flat dark
# panels and text, so dithering mostly adds per-frame noise that defeats the
# GIF's inter-frame compression and doubles the file for no visible gain.
PAL="$(mktemp --suffix=.png)"
FILTERS="fps=10,scale=900:-1:flags=lanczos"
ffmpeg -y -loglevel error -i "$SRC" \
  -vf "$FILTERS,palettegen=max_colors=128:stats_mode=diff" "$PAL"
ffmpeg -y -loglevel error -i "$SRC" -i "$PAL" \
  -lavfi "$FILTERS[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
  "$DIR/walkthrough.gif"
rm -f "$PAL"

# --apparent-size: plain `du` reports blocks allocated, which on some filesystems
# reads nearly double the real size and makes a fine GIF look bloated.
for f in walkthrough.mp4 walkthrough.gif; do
  printf '  %-18s %s\n' "$f" "$(du -h --apparent-size "$DIR/$f" | cut -f1)"
done
