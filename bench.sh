#!/usr/bin/env bash
# bench.sh — Lumina Ollama benchmark script
# Usage: bash bench.sh <label>
# Example: bash bench.sh baseline
#          bash bench.sh post-systemd
#          bash bench.sh qwen3-30b-a3b
#
# Runs 3 inference passes, extracts eval rate (t/s) and load duration,
# logs results to bench_results.log, and prints a summary.
#
# The model tested is read from OLLAMA_MODEL env var, defaulting to
# llama3.1:8b-instruct-q4_K_M. Override per-run:
#   OLLAMA_MODEL=qwen3:30b-a3b bash bench.sh qwen3-30b-a3b

set -euo pipefail

LABEL="${1:-unlabeled}"
MODEL="${OLLAMA_MODEL:-llama3.1:8b-instruct-q4_K_M}"
LOGFILE="bench_results.log"
RUNS=3
PROMPT="Explain the key differences between classical and operant conditioning in psychology. Be thorough."

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}=== Lumina Benchmark ===${NC}"
echo -e "Label:  ${YELLOW}${LABEL}${NC}"
echo -e "Model:  ${YELLOW}${MODEL}${NC}"
echo -e "Runs:   ${RUNS}"
echo -e "Prompt: \"${PROMPT:0:60}...\""
echo ""

# ── Collect system info ───────────────────────────────────────────────────────
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
RAM_SPEED=$(sudo dmidecode -t memory 2>/dev/null | grep -i "configured memory speed" | head -1 | awk '{print $NF, $(NF-1)}' || echo "unknown")
CPU_GOVERNOR=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
OLLAMA_ENV=$(sudo systemctl show ollama --property=Environment 2>/dev/null | head -1 || echo "unknown")

# ── Run benchmarks ────────────────────────────────────────────────────────────
declare -a EVAL_RATES
declare -a PROMPT_EVAL_RATES
declare -a LOAD_DURATIONS

for i in $(seq 1 $RUNS); do
    echo -n "  Run $i/$RUNS ... "

    # Capture verbose output
    RAW=$(ollama run "$MODEL" "$PROMPT" --verbose 2>&1 || true)

    # Parse prompt eval rate — "prompt eval rate: 312.45 tokens/s"
    # (this is prefill speed — fast but NOT generation speed)
    PROMPT_EVAL_RATE=$(echo "$RAW" | grep -i "prompt eval rate" | grep -oP '[\d.]+(?=\s*tokens)' | head -1 || echo "0")

    # Parse generation eval rate — "eval rate: 38.20 tokens/s"
    # Must exclude "prompt eval rate" line — this is the real inference speed
    EVAL_RATE=$(echo "$RAW" | grep -i "eval rate" | grep -vi "prompt" | grep -oP '[\d.]+(?=\s*tokens)' | head -1 || echo "0")

    # Parse load duration — line like: "load duration:  1.234s" or "123ms"
    LOAD_RAW=$(echo "$RAW" | grep -i "load duration" | head -1 || echo "")
    if echo "$LOAD_RAW" | grep -q "ms"; then
        LOAD_MS=$(echo "$LOAD_RAW" | grep -oP '[\d.]+(?=ms)' | head -1 || echo "0")
    elif echo "$LOAD_RAW" | grep -q "s"; then
        LOAD_S=$(echo "$LOAD_RAW" | grep -oP '[\d.]+(?=s)' | tail -1 || echo "0")
        LOAD_MS=$(python3 -c "print(round(float('${LOAD_S}') * 1000))" 2>/dev/null || echo "0")
    else
        LOAD_MS="0"
    fi

    EVAL_RATES+=("$EVAL_RATE")
    PROMPT_EVAL_RATES+=("$PROMPT_EVAL_RATE")
    LOAD_DURATIONS+=("$LOAD_MS")

    echo -e "${GREEN}${EVAL_RATE} t/s gen${NC} | ${PROMPT_EVAL_RATE} t/s prefill | load: ${LOAD_MS}ms"

    # Brief pause between runs so model is warm but not cached weirdly
    sleep 2
done

# ── Compute medians ───────────────────────────────────────────────────────────
MEDIAN_TPS=$(python3 - <<EOF
import statistics
vals = [float(x) for x in "${EVAL_RATES[*]}".split()]
vals = [v for v in vals if v > 0]
print(round(statistics.median(vals), 2) if vals else 0)
EOF
)

MEDIAN_PROMPT_TPS=$(python3 - <<EOF
import statistics
vals = [float(x) for x in "${PROMPT_EVAL_RATES[*]}".split()]
vals = [v for v in vals if v > 0]
print(round(statistics.median(vals), 2) if vals else 0)
EOF
)

MEDIAN_LOAD=$(python3 - <<EOF
import statistics
vals = [float(x) for x in "${LOAD_DURATIONS[*]}".split()]
vals = [v for v in vals if v >= 0]
print(round(statistics.median(vals)) if vals else 0)
EOF
)

# ── Get current MHz snapshot ──────────────────────────────────────────────────
AVG_MHZ=$(grep "cpu MHz" /proc/cpuinfo | awk '{sum+=$4; n++} END {printf "%.0f", sum/n}')

# ── Print summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}── Results ──────────────────────────────────────────${NC}"
echo -e "  Median generation rate:  ${GREEN}${MEDIAN_TPS} t/s${NC}   ← the real number"
echo -e "  Median prefill rate:     ${MEDIAN_PROMPT_TPS} t/s   (prompt eval — not generation)"
echo -e "  Median load time:        ${MEDIAN_LOAD}ms"
echo -e "  Avg CPU MHz:             ${AVG_MHZ} MHz"
echo -e "  RAM speed:               ${RAM_SPEED}"
echo ""

# ── Append to log ─────────────────────────────────────────────────────────────
LOG_LINE="${TIMESTAMP} | label=${LABEL} | model=${MODEL} | tps=${MEDIAN_TPS} | prompt_tps=${MEDIAN_PROMPT_TPS} | load_ms=${MEDIAN_LOAD} | cpu_mhz=${AVG_MHZ} | ram=${RAM_SPEED} | governor=${CPU_GOVERNOR} | runs=${RUNS} | raw_tps=${EVAL_RATES[*]}"
echo "$LOG_LINE" >> "$LOGFILE"
echo -e "  Logged to ${LOGFILE}"
echo ""

# ── Compare to baseline if available ─────────────────────────────────────────
BASELINE=$(grep "label=baseline" "$LOGFILE" 2>/dev/null | tail -1 | grep -oP 'tps=\K[\d.]+' || echo "")
if [[ -n "$BASELINE" && "$LABEL" != "baseline" ]]; then
    DELTA=$(python3 -c "
b=float('${BASELINE}')
n=float('${MEDIAN_TPS}')
if b > 0:
    pct = round(((n-b)/b)*100, 1)
    sign = '+' if pct >= 0 else ''
    print(f'{sign}{pct}% vs baseline ({b} t/s)')
else:
    print('baseline was 0, cannot compare')
" 2>/dev/null || echo "")
    echo -e "  vs baseline:             ${YELLOW}${DELTA}${NC}"
fi

echo ""
echo -e "${CYAN}=== Done ===${NC}"