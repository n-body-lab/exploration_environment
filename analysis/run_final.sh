#!/bin/bash
# HIVEMARKET: everything after the 5:00 AM stop, in one go (run on the MacBook Pro).
#
# Before running:
#   1. Put every machine's zip(s) of main runs AND the order files in ~/Desktop/hm_final/
#      (e.g. sherin_main.zip, planned_orders.csv, macmini_main.zip, macmini_block_order.csv,
#       macbook_main.zip, macbook_block_order.csv). Order-file names don't matter as long as they end in .csv.
#   2. Enter the three judge keys in this Terminal window with read -s (only needed for step 4).
#
# Usage:
#   cd "$HOME/Documents/_Claude/projects/Career - AI-Safety/HIVEMARKET_CompCheck/analysis"
#   bash run_final.sh            # coding + analysis + judges
#   bash run_final.sh nojudge    # coding + analysis only
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
IN="$HOME/Desktop/hm_final"
OUT="$IN/results"
RUNS="$IN/runs_all"
PY="$HOME/Documents/exploration_environment/.venv/bin/python"
[ -x "$PY" ] || PY=python3
mkdir -p "$RUNS" "$OUT"

echo "== 1. Unzip every zip into one runs folder"
shopt -s nullglob
for z in "$IN"/*.zip; do
  unzip -qo "$z" -d "$RUNS/tmp_unzip"
done
# move every main-s* run folder (wherever it sits inside the zips) up to runs_all/
find "$RUNS/tmp_unzip" -maxdepth 4 -type d -name 'main-s*' -prune -exec mv -n {} "$RUNS"/ \; 2>/dev/null || true
rm -rf "$RUNS/tmp_unzip"
echo "   run folders: $(ls -d "$RUNS"/main-s* 2>/dev/null | wc -l | tr -d ' ')"

echo "== 2. Order files and exit codes"
ORDER_ARGS=()
for c in "$IN"/*.csv; do
  if head -1 "$c" | grep -q "run_id"; then ORDER_ARGS+=(--order "$c"); echo "   using $(basename "$c")"; fi
done

echo "== 3. Coding (rule-based, PAP 4.2) and analysis (PAP 5)"
"$PY" -c "import scipy" 2>/dev/null || "$PY" -m pip install -q scipy
"$PY" "$HERE/LydiaAshton_HIVEMARKET_Code_L2.py" --runs "$RUNS" "${ORDER_ARGS[@]}" --out "$OUT"
"$PY" "$HERE/LydiaAshton_HIVEMARKET_Analysis.py" "$OUT/sessions.csv" "$OUT/agents.csv" > "$OUT/results.md"
echo "   results written to $OUT/results.md"

if [ "$1" != "nojudge" ]; then
  echo "== 4. Judge panel (every agent; resumable: re-run this script to continue)"
  "$PY" "$HERE/LydiaAshton_HIVEMARKET_Judge_Panel.py" --runs "$RUNS" --codes "$OUT/agents.csv" --out "$OUT" --workers 8
fi

echo "== Done. Send these to Claude: $OUT/results.md, sessions.csv, agents.csv, evidence.csv, judge_summary.md"
