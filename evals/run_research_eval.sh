#!/usr/bin/env bash
# 投研 Agent 自动化评测流程：数据集校验 -> 运行 agent + 独立裁判 -> 生成报告（可选与基线对比）。
#
# 关键约束：被测模型与裁判模型必须不同，否则 ExperimentRunner 拒绝启动。
# 默认：被测 deepseek（profile web-da8cda1a018e），裁判 qwen（qwen-plus-latest，1M 窗口）。
# 裁判选择说明：裁判报文约 25 万 token（中文按字计），需 1M 级上下文。
#   - qwen-plus(131072) / copilot(~128k) 放不下；
#   - gemini 窗口够大，但免费层仅 20 次/天，无法支撑批量评测，故仅作备选。
#
# 用法：
#   MODE=smoke          evals/run_research_eval.sh   # 17 条指标专项子集，低成本冒烟
#   MODE=representative evals/run_research_eval.sh   # 每类抽样，覆盖面更广
#   MODE=full           evals/run_research_eval.sh   # 全量 200 条
#   BASELINE=.researchx/evaluations/<旧实验> MODE=smoke evals/run_research_eval.sh
#
# 可用环境变量覆盖：AGENT_PROFILE / JUDGE_PROFILE / DATASET / OUTPUT / MODE / REPETITIONS / BASELINE / OH
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASET="${DATASET:-$ROOT/evals/research_v1}"
SUBSET="${SUBSET:-$ROOT/evals/metrics_subset.txt}"
OUTPUT="${OUTPUT:-$ROOT/.researchx/evaluations}"
MODE="${MODE:-smoke}"
REPETITIONS="${REPETITIONS:-1}"
AGENT_PROFILE="${AGENT_PROFILE:-web-da8cda1a018e}"
JUDGE_PROFILE="${JUDGE_PROFILE:-qwen}"
# 裁判 profile 未配置 context_window_tokens，且模型不在 MODEL_WINDOWS 中，
# 裁判会在首次调用前抛 ContextBudgetError；必须显式给出窗口。
# 报文约 25 万 token，故默认 1,000,000（qwen-plus-latest 支持 1M）。
JUDGE_CONTEXT_WINDOW_TOKENS="${JUDGE_CONTEXT_WINDOW_TOKENS:-1000000}"
# 长任务预算覆盖：留空则用数据集的冻结预算（basic/intermediate/complex = 40/64/96 轮、
# 3M/10M/24M token）。只在执行期生效，不改动冻结数据集，dataset_version 与归档重评分不受影响。
# 注意：提高轮次必须同步提高 token 上限，否则会改撞 "Token 消耗达到评测预算"。
MAX_MODEL_CALLS="${MAX_MODEL_CALLS:-}"
MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-}"

# 优先使用仓库内虚拟环境，避免依赖全局 PATH。
if [[ -z "${OH:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/oh" ]]; then OH="$ROOT/.venv/bin/oh"; else OH="oh"; fi
fi
if [[ -z "${PY:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi

if [[ "$AGENT_PROFILE" == "$JUDGE_PROFILE" ]]; then
  echo "被测与裁判 profile 不能相同：$AGENT_PROFILE" >&2
  exit 2
fi

select_args=()
case "$MODE" in
  smoke)
    [[ -f "$SUBSET" ]] || { echo "缺少子集文件：$SUBSET" >&2; exit 2; }
    while read -r case_id; do
      [[ -z "$case_id" || "$case_id" == \#* ]] && continue
      select_args+=(--case-id "$case_id")
    done < "$SUBSET"
    ;;
  representative)
    select_args=(--representative)
    ;;
  full)
    ;;
  *)
    echo "未知 MODE：$MODE（可选 smoke|representative|full）" >&2
    exit 2
    ;;
esac

echo "== 1/3 数据集静态校验：$DATASET =="
"$OH" eval validate --dataset "$DATASET"

echo "== 2/3 运行评测：MODE=$MODE 被测=$AGENT_PROFILE 裁判=$JUDGE_PROFILE 重复=$REPETITIONS 轮次上限=${MAX_MODEL_CALLS:-数据集冻结} =="
budget_args=()
if [[ -n "$MAX_MODEL_CALLS" ]]; then
  budget_args+=(--max-model-calls "$MAX_MODEL_CALLS")
fi
if [[ -n "$MAX_TOTAL_TOKENS" ]]; then
  budget_args+=(--total-tokens "$MAX_TOTAL_TOKENS")
fi
"$OH" eval run \
  --profile "$AGENT_PROFILE" \
  --judge-profile "$JUDGE_PROFILE" \
  --judge-context-window-tokens "$JUDGE_CONTEXT_WINDOW_TOKENS" \
  --dataset "$DATASET" \
  --output "$OUTPUT" \
  --repetitions "$REPETITIONS" \
  "${budget_args[@]}" \
  "${select_args[@]}"

# 定位本次实验目录：run 会新建 research-<uuid>，取 OUTPUT 下最新目录。
dest="$OUTPUT/$(ls -t "$OUTPUT" | head -1)"
echo "== 3/3 实验目录：$dest =="

if [[ -n "${BASELINE:-}" ]]; then
  echo "-- 与基线对比：$BASELINE --"
  "$OH" eval report "$dest" --compare "$BASELINE"
fi

# 打印核心指标，便于 CI 日志直接判读。
if [[ -f "$dest/report.json" ]]; then
  "$PY" - "$dest/report.json" <<'PY'
import json, sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
groups = report.get("groups", {})
rows = []
for group in ("environment:fixed", "environment:live"):
    metrics = groups.get(group)
    if not metrics:
        continue
    rows.append(f"[{group}]")
    for name in (
        "task_success",
        "path_correct",
        "gap_declaration",
        "unsupported_statement_rate",
        "citation_correctness",
        "citation_coverage",
        "total_tokens",
        "model_steps",
        "end_to_end_ms",
    ):
        item = metrics.get(name)
        if not item:
            rows.append(f"  {name}: 缺失")
            continue
        mean = "N/A" if item.get("mean") is None else round(item["mean"], 3)
        rows.append(f"  {name}: {mean}  ({item['count']}/{item['total']})")
print("\n".join(rows) if rows else "报告中暂无分组指标")
PY
fi

echo "完成。报告：$dest/report.md"