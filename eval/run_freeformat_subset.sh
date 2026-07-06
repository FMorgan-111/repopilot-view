#!/usr/bin/env bash
# C1 targeted-subset eval on gpt-5.5: run only the non-infra samples that can
# trigger the search-hallucination gate, each in isolation, and merge results.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
OUT=eval/freeformat_gpt55_subset.json
LOG=/tmp/eval_freeformat_gpt55.log
IDS=(
  "scrapy/scrapy#6343:6349"
  "scrapy/scrapy#5383:5384"
  "ansible/ansible#86228:86302"
  "ansible/ansible#66679:86143"
  "ansible/ansible#86395:86403"
  "pandas-dev/pandas#50690:64916"
  "python/mypy#20532:20643"
)
echo "[]" > "$OUT"
: > "$LOG"
for id in "${IDS[@]}"; do
  echo "==================== $id ====================" | tee -a "$LOG"
  $PY eval/harness.py --agent-v2 --seed-gold-files --sample-id "$id" \
      --max-retries 2 --token-budget 100000 >> "$LOG" 2>&1
  # merge this sample's single-item result into the accumulator
  $PY - "$OUT" >> "$LOG" 2>&1 <<'PYEOF'
import json, sys
acc_path = sys.argv[1]
acc = json.load(open(acc_path))
new = json.load(open("eval/eval_results.json"))
acc.extend(new)
json.dump(acc, open(acc_path, "w"), indent=2)
print(f"[merge] accumulator now has {len(acc)} sample(s)")
PYEOF
done
echo "C1_SUBSET_DONE" | tee -a "$LOG"
