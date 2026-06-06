#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/shell_env.sh"

CONFIG_FILE="${PEARL_CONFIG:-${CONFIG_FILE:-$DIR/config.env}}"
export PEARL_CONFIG="$CONFIG_FILE"
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Missing config file: $CONFIG_FILE" >&2
  exit 1
fi
pearl_load_env_file "$CONFIG_FILE"

MINER_SERVICE="${MINER_SERVICE:-pearl-miner.service}"
GPU_INDEX="${GPU_INDEX:-0}"
POOL_HOST="${POOL_HOST:-sg1.alphapool.tech}"
POOL_PORT="${POOL_PORT:-5566}"
POOL_TARGET="${POOL_HOST}:${POOL_PORT}"
WALLET_ADDRESS="${WALLET_ADDRESS:-${WALLET:-}}"
WORKER_NAME="${WORKER_NAME:-NTD_Rig}"
MINER_ALGORITHM="${MINER_ALGORITHM:-pearlhash}"
MINER_PASSWORD="${MINER_PASSWORD:-x;d=65536}"

ALPHA_MINER_EXEC="${ALPHA_MINER_EXEC:-/home/ntd/Downloads/alpha-miner/alpha-miner}"
SRBMINER_EXEC="${SRBMINER_EXEC:-/home/ntd/Downloads/SRBMiner-Multi-3-3-4/SRBMiner-MULTI}"
ALPHA_MINER_EXTRA_ARGS="${ALPHA_MINER_EXTRA_ARGS:---status-interval 10 --color never}"
SRBMINER_EXTRA_ARGS="${SRBMINER_EXTRA_ARGS:-}"
SRBMINER_DEV_FEE_PERCENT="${SRBMINER_DEV_FEE_PERCENT:-3}"

DURATION="${BENCHMARK_SECONDS:-900}"
PROFILE="${BENCHMARK_PROFILE:-balance}"
SAMPLE_SECONDS="${BENCHMARK_SAMPLE_SECONDS:-5}"
ORDER="${BENCHMARK_ORDER:-alpha,srbminer}"
OUTPUT_DIR="${BENCHMARK_OUTPUT_DIR:-$DIR/benchmarks/$(date +%Y%m%d-%H%M%S)}"
COOLDOWN_ENABLED="${BENCHMARK_COOLDOWN_ENABLED:-1}"
COOLDOWN_MIN_SECONDS="${BENCHMARK_COOLDOWN_MIN_SECONDS:-30}"
COOLDOWN_TIMEOUT_SECONDS="${BENCHMARK_COOLDOWN_TIMEOUT_SECONDS:-900}"
COOLDOWN_TEMP_DELTA_C="${BENCHMARK_COOLDOWN_TEMP_DELTA_C:-3}"
COOLDOWN_POWER_DELTA_W="${BENCHMARK_COOLDOWN_POWER_DELTA_W:-10}"
BASELINE_SAMPLES="${BENCHMARK_BASELINE_SAMPLES:-5}"
BASELINE_SAMPLE_SECONDS="${BENCHMARK_BASELINE_SAMPLE_SECONDS:-2}"
DRY_RUN=0
ASSUME_YES=0
STOP_SERVICE=1
BASELINE_TEMP_C=""
BASELINE_POWER_W=""

usage() {
  cat <<'EOF'
Usage: ./benchmark_miners.sh [options]

Runs alpha-miner and SRBMiner one after another with the same pool, wallet,
power limit, and clock profile, then writes a comparison report.

Options:
  --duration VALUE       Per-miner duration, e.g. 600, 10m, 1h. Default: 900
  --profile NAME         OC profile to apply before each run. Default: balance
  --sample-seconds N     GPU sample interval. Default: 5
  --order LIST           alpha,srbminer or srbminer,alpha. Default: alpha,srbminer
  --output-dir DIR       Report/log directory. Default: ./benchmarks/<timestamp>
  --no-cooldown          Skip waiting for GPU temp/power to return to baseline
  --yes                  Do not ask before stopping the miner service
  --no-stop-service      Do not stop pearl-miner.service before benchmarking
  --dry-run              Print planned commands only
  -h, --help             Show this help
EOF
}

parse_duration() {
  local value="$1"
  case "$value" in
    *h) echo "$(( ${value%h} * 3600 ))" ;;
    *m) echo "$(( ${value%m} * 60 ))" ;;
    *s) echo "${value%s}" ;;
    *) echo "$value" ;;
  esac
}

quote_cmd() {
  local arg
  for arg in "$@"; do
    printf '%q ' "$arg"
  done
}

escape_srb_password() {
  local value="$1"
  value="${value//!/#!}"
  value="${value//;/#;}"
  printf '%s' "$value"
}

trim_spaces() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration) DURATION="${2:?}"; shift 2 ;;
    --profile) PROFILE="${2:?}"; shift 2 ;;
    --sample-seconds) SAMPLE_SECONDS="${2:?}"; shift 2 ;;
    --order) ORDER="${2:?}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?}"; shift 2 ;;
    --no-cooldown) COOLDOWN_ENABLED=0; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --no-stop-service) STOP_SERVICE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

DURATION_SECONDS="$(parse_duration "$DURATION")"
if [[ ! "$DURATION_SECONDS" =~ ^[0-9]+$ || "$DURATION_SECONDS" -lt 60 ]]; then
  echo "Invalid --duration. Use at least 60 seconds." >&2
  exit 2
fi
if [[ ! "$SAMPLE_SECONDS" =~ ^[0-9]+$ || "$SAMPLE_SECONDS" -lt 1 ]]; then
  echo "Invalid --sample-seconds." >&2
  exit 2
fi
for value_name in COOLDOWN_MIN_SECONDS COOLDOWN_TIMEOUT_SECONDS BASELINE_SAMPLES BASELINE_SAMPLE_SECONDS; do
  value="${!value_name}"
  if [[ ! "$value" =~ ^[0-9]+$ || "$value" -lt 1 ]]; then
    echo "Invalid $value_name." >&2
    exit 2
  fi
done
if [[ -z "$WALLET_ADDRESS" || "$WALLET_ADDRESS" == "CHANGE_ME_PEARL_WALLET" ]]; then
  echo "Set WALLET_ADDRESS in $CONFIG_FILE before benchmarking." >&2
  exit 1
fi

MINER_LOGIN="$WALLET_ADDRESS"
if [[ "$MINER_LOGIN" != *.* && -n "$WORKER_NAME" ]]; then
  MINER_LOGIN="${WALLET_ADDRESS}.${WORKER_NAME}"
fi

read -r -a ALPHA_EXTRA <<< "$ALPHA_MINER_EXTRA_ARGS"
read -r -a SRB_EXTRA <<< "$SRBMINER_EXTRA_ARGS"
SRBMINER_PASSWORD="${SRBMINER_PASSWORD:-$(escape_srb_password "$MINER_PASSWORD")}"

alpha_cmd=("$ALPHA_MINER_EXEC" --pool "stratum+tcp://$POOL_TARGET" --address "$WALLET_ADDRESS")
if [[ -n "$WORKER_NAME" ]]; then
  alpha_cmd+=(--worker "$WORKER_NAME")
fi
alpha_cmd+=(--password "$MINER_PASSWORD")
alpha_cmd+=("${ALPHA_EXTRA[@]}")

srb_cmd=("$SRBMINER_EXEC" --disable-cpu --algorithm "$MINER_ALGORITHM" --pool "$POOL_TARGET" --wallet "$MINER_LOGIN")
if [[ -n "$SRBMINER_PASSWORD" ]]; then
  srb_cmd+=(--password "$SRBMINER_PASSWORD")
fi
srb_cmd+=("${SRB_EXTRA[@]}")

IFS=',' read -r -a MINER_ORDER <<< "$ORDER"
for miner in "${MINER_ORDER[@]}"; do
  if [[ "$miner" != "alpha" && "$miner" != "srbminer" ]]; then
    echo "Invalid miner in --order: $miner" >&2
    exit 2
  fi
done

if [[ ! -x "$ALPHA_MINER_EXEC" ]]; then
  echo "alpha-miner executable not found: $ALPHA_MINER_EXEC" >&2
  exit 1
fi
if [[ ! -x "$SRBMINER_EXEC" ]]; then
  echo "SRBMiner executable not found: $SRBMINER_EXEC" >&2
  exit 1
fi

echo "================================================="
echo " PEARL MINER A/B BENCHMARK"
echo "================================================="
echo "Config:       $CONFIG_FILE"
echo "Pool:         $POOL_TARGET"
echo "Wallet:       ${WALLET_ADDRESS:0:16}..."
echo "Worker:       $WORKER_NAME"
echo "Profile:      $PROFILE"
echo "Duration:     ${DURATION_SECONDS}s per miner"
echo "Cooldown:     $([[ "$COOLDOWN_ENABLED" == "1" ]] && echo "on, <= baseline + ${COOLDOWN_TEMP_DELTA_C}C/${COOLDOWN_POWER_DELTA_W}W" || echo "off")"
echo "Output:       $OUTPUT_DIR"
echo "alpha-miner:  $(quote_cmd "${alpha_cmd[@]}")"
echo "SRBMiner:     $(quote_cmd "${srb_cmd[@]}")"
echo "================================================="

if [[ "$DRY_RUN" == "1" ]]; then
  exit 0
fi

if [[ "$STOP_SERVICE" == "1" && "$ASSUME_YES" != "1" ]]; then
  read -r -p "This will stop $MINER_SERVICE while benchmarking. Continue? [y/N] " answer
  if [[ "${answer,,}" != "y" && "${answer,,}" != "yes" ]]; then
    echo "Cancelled."
    exit 0
  fi
fi

mkdir -p "$OUTPUT_DIR"

apply_profile() {
  "$DIR/venv/bin/python" - "$PROFILE" <<'PY'
import sys
from miner_services import apply_oc_profile

profile = sys.argv[1]
result = apply_oc_profile(profile)
print(result)
if not result.get("ok"):
    raise SystemExit(1)
PY
}

sample_gpu() {
  local output="$1"
  while true; do
    printf '%s,' "$(date +%s)"
    nvidia-smi --id="$GPU_INDEX" \
      --query-gpu=temperature.gpu,power.draw,utilization.gpu,clocks.gr,clocks.mem \
      --format=csv,noheader,nounits
    sleep "$SAMPLE_SECONDS"
  done >> "$output"
}

read_gpu_metrics() {
  local raw temp power util core mem
  raw="$(nvidia-smi --id="$GPU_INDEX" \
    --query-gpu=temperature.gpu,power.draw,utilization.gpu,clocks.gr,clocks.mem \
    --format=csv,noheader,nounits)"
  IFS=',' read -r temp power util core mem <<< "$raw"
  temp="$(trim_spaces "$temp")"
  power="$(trim_spaces "$power")"
  util="$(trim_spaces "$util")"
  core="$(trim_spaces "$core")"
  mem="$(trim_spaces "$mem")"
  printf '%s %s %s %s %s\n' "$temp" "$power" "$util" "$core" "$mem"
}

record_baseline() {
  local output="$OUTPUT_DIR/baseline_samples.csv"
  local i temp power util core mem
  : > "$output"
  echo "Measuring idle baseline..."
  for ((i = 0; i < BASELINE_SAMPLES; i++)); do
    read -r temp power util core mem < <(read_gpu_metrics)
    printf '%s,%s,%s,%s,%s,%s\n' "$(date +%s)" "$temp" "$power" "$util" "$core" "$mem" >> "$output"
    sleep "$BASELINE_SAMPLE_SECONDS"
  done
  read -r BASELINE_TEMP_C BASELINE_POWER_W < <("$DIR/venv/bin/python" - "$output" <<'PY'
import csv
import statistics
import sys

temps = []
powers = []
with open(sys.argv[1], newline="", encoding="utf-8") as handle:
    for row in csv.reader(handle):
        if len(row) < 3:
            continue
        try:
            temps.append(float(row[1]))
            powers.append(float(row[2]))
        except ValueError:
            pass

if not temps or not powers:
    raise SystemExit("Cannot read GPU baseline")
print(f"{statistics.fmean(temps):.2f} {statistics.fmean(powers):.2f}")
PY
)
  printf 'Baseline: %.2f C, %.2f W\n' "$BASELINE_TEMP_C" "$BASELINE_POWER_W" | tee "$OUTPUT_DIR/baseline.txt"
}

wait_for_cooldown() {
  local label="$1"
  local log="$OUTPUT_DIR/cooldown.log"
  local start now elapsed temp power util core mem ready
  if [[ "$COOLDOWN_ENABLED" != "1" ]]; then
    return 0
  fi
  if [[ -z "$BASELINE_TEMP_C" || -z "$BASELINE_POWER_W" ]]; then
    record_baseline
  fi
  echo "Cooldown before $label: waiting at least ${COOLDOWN_MIN_SECONDS}s and until GPU is near baseline..."
  printf '\n[%s] cooldown before %s, target <= %.2f C and <= %.2f W\n' \
    "$(date --iso-8601=seconds)" "$label" \
    "$(awk -v base="$BASELINE_TEMP_C" -v delta="$COOLDOWN_TEMP_DELTA_C" 'BEGIN { print base + delta }')" \
    "$(awk -v base="$BASELINE_POWER_W" -v delta="$COOLDOWN_POWER_DELTA_W" 'BEGIN { print base + delta }')" >> "$log"
  start="$(date +%s)"
  while true; do
    read -r temp power util core mem < <(read_gpu_metrics)
    now="$(date +%s)"
    elapsed="$((now - start))"
    printf '%s,%s,%s,%s,%s,%s,%s,%s\n' "$now" "$label" "$elapsed" "$temp" "$power" "$util" "$core" "$mem" >> "$log"
    ready="$(awk \
      -v elapsed="$elapsed" \
      -v min_s="$COOLDOWN_MIN_SECONDS" \
      -v temp="$temp" \
      -v power="$power" \
      -v base_t="$BASELINE_TEMP_C" \
      -v base_p="$BASELINE_POWER_W" \
      -v delta_t="$COOLDOWN_TEMP_DELTA_C" \
      -v delta_p="$COOLDOWN_POWER_DELTA_W" \
      'BEGIN { print (elapsed >= min_s && temp <= base_t + delta_t && power <= base_p + delta_p) ? 1 : 0 }')"
    if [[ "$ready" == "1" ]]; then
      printf 'Cooldown ready for %s: %s C, %s W after %ss\n' "$label" "$temp" "$power" "$elapsed"
      break
    fi
    if (( elapsed >= COOLDOWN_TIMEOUT_SECONDS )); then
      printf 'Cooldown timeout for %s: continuing at %s C, %s W after %ss\n' "$label" "$temp" "$power" "$elapsed" | tee -a "$log"
      break
    fi
    sleep "$SAMPLE_SECONDS"
  done
}

run_one() {
  local miner="$1"
  local log_file="$OUTPUT_DIR/${miner}.log"
  local gpu_file="$OUTPUT_DIR/${miner}_gpu.csv"
  local status_file="$OUTPUT_DIR/${miner}.status"
  local start_file="$OUTPUT_DIR/${miner}_start_gpu.txt"
  local -a cmd

  if [[ "$miner" == "alpha" ]]; then
    cmd=("${alpha_cmd[@]}")
  else
    cmd=("${srb_cmd[@]}")
  fi

  echo
  echo "---- $miner ----"
  echo "Applying OC profile: $PROFILE"
  apply_profile | tee "$OUTPUT_DIR/${miner}_profile.txt"
  sleep 2
  wait_for_cooldown "$miner"
  read_gpu_metrics > "$start_file"

  : > "$gpu_file"
  sample_gpu "$gpu_file" &
  local sampler_pid=$!
  local cmd_string
  cmd_string="$(quote_cmd "${cmd[@]}")"

  set +e
  timeout --signal=INT --kill-after=20s "$DURATION_SECONDS" script -qefc "$cmd_string" /dev/null > "$log_file" 2>&1
  local exit_code=$?
  set -e

  kill "$sampler_pid" >/dev/null 2>&1 || true
  wait "$sampler_pid" >/dev/null 2>&1 || true
  echo "$exit_code" > "$status_file"
  echo "$miner finished with exit code $exit_code"
}

if [[ "$STOP_SERVICE" == "1" ]]; then
  sudo -n systemctl stop "$MINER_SERVICE" || true
fi

apply_profile | tee "$OUTPUT_DIR/baseline_profile.txt"
sleep 2
record_baseline

for miner in "${MINER_ORDER[@]}"; do
  run_one "$miner"
  sleep 5
done

"$DIR/venv/bin/python" - "$OUTPUT_DIR" "$SRBMINER_DEV_FEE_PERCENT" <<'PY'
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

out = Path(sys.argv[1])
srb_fee_percent = float(sys.argv[2])

unit_factors = {
    "h": 1e-12,
    "kh": 1e-9,
    "mh": 1e-6,
    "gh": 1e-3,
    "th": 1.0,
    "ph": 1e3,
    "eh": 1e6,
}


def parse_hashrates(text: str) -> list[float]:
    values: list[float] = []
    for match in re.finditer(r"hashrate_th_s\s*=\s*([0-9]+(?:\.[0-9]+)?)", text, re.I):
        values.append(float(match.group(1)))
    for match in re.finditer(r"(?<![A-Za-z])([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?H)\s*/?\s*s", text, re.I):
        value = float(match.group(1))
        unit = match.group(2).lower()
        values.append(value * unit_factors.get(unit, 0.0))
    return [value for value in values if value >= 0]


def parse_gpu(path: Path) -> dict[str, float | int | None]:
    temps: list[float] = []
    powers: list[float] = []
    utils: list[float] = []
    if not path.exists():
        return {"samples": 0, "avg_temp_c": None, "max_temp_c": None, "avg_power_w": None, "avg_util_pct": None}
    for line in path.read_text(errors="replace").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            temps.append(float(parts[1]))
            powers.append(float(parts[2]))
            utils.append(float(parts[3]))
        except ValueError:
            continue
    return {
        "samples": len(powers),
        "avg_temp_c": round(statistics.fmean(temps), 2) if temps else None,
        "max_temp_c": round(max(temps), 2) if temps else None,
        "avg_power_w": round(statistics.fmean(powers), 2) if powers else None,
        "avg_util_pct": round(statistics.fmean(utils), 2) if utils else None,
    }


def summarize(miner: str) -> dict[str, object]:
    log_path = out / f"{miner}.log"
    status_path = out / f"{miner}.status"
    text = log_path.read_text(errors="replace") if log_path.exists() else ""
    hashrates = parse_hashrates(text)
    avg_hashrate = statistics.fmean(hashrates) if hashrates else None
    last_hashrate = hashrates[-1] if hashrates else None
    gpu = parse_gpu(out / f"{miner}_gpu.csv")
    fee_percent = srb_fee_percent if miner == "srbminer" else 0.0
    adjusted = avg_hashrate * (1 - fee_percent / 100) if avg_hashrate is not None else None
    power = gpu.get("avg_power_w")
    efficiency = adjusted / power if adjusted is not None and isinstance(power, (int, float)) and power > 0 else None
    lower = text.lower()
    pool_protocol_errors = len(
        re.findall(r"parse error|method is not supported|not connected to a pool|pool login failed|authorization failed", lower)
    )
    connected_lines = len(re.findall(r"connected to|component=pool connected|challenge_received|mining_params", lower))
    status = "ok"
    if pool_protocol_errors:
        status = "pool_protocol_error"
    elif not hashrates:
        status = "no_hashrate_detected"
    return {
        "miner": miner,
        "status": status,
        "exit_code": int(status_path.read_text().strip()) if status_path.exists() else None,
        "avg_hashrate_th_s": round(avg_hashrate, 4) if avg_hashrate is not None else None,
        "last_hashrate_th_s": round(last_hashrate, 4) if last_hashrate is not None else None,
        "fee_percent": fee_percent,
        "adjusted_hashrate_th_s": round(adjusted, 4) if adjusted is not None else None,
        "efficiency_th_per_w": round(efficiency, 6) if efficiency is not None else None,
        "connected_log_lines": connected_lines,
        "pool_protocol_errors": pool_protocol_errors,
        "submitted_or_accepted_lines": len(re.findall(r"submitted|accepted|share accepted|result accepted", lower)),
        "rejected_or_invalid_lines": len(re.findall(r"rejected|invalid", lower)),
        "gpu": gpu,
        "log": str(log_path),
    }


results = [summarize(miner) for miner in ("alpha", "srbminer") if (out / f"{miner}.log").exists()]
report = {"results": results}

def winner(key: str) -> str | None:
    ranked = [item for item in results if isinstance(item.get(key), (int, float))]
    if not ranked:
        return None
    return max(ranked, key=lambda item: float(item[key]))["miner"]  # type: ignore[index]

report["winner_raw_hashrate"] = winner("avg_hashrate_th_s")
report["winner_after_fee"] = winner("adjusted_hashrate_th_s")
report["winner_efficiency"] = winner("efficiency_th_per_w")

(out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

lines = ["# Pearl Miner Benchmark", ""]
for item in results:
    gpu = item["gpu"]
    assert isinstance(gpu, dict)
    lines.extend(
        [
            f"## {item['miner']}",
            f"- Status: {item['status']}",
            f"- Exit code: {item['exit_code']}",
            f"- Avg speed: {item['avg_hashrate_th_s']} TH/s",
            f"- Last speed: {item['last_hashrate_th_s']} TH/s",
            f"- Fee used for adjustment: {item['fee_percent']}%",
            f"- After-fee speed: {item['adjusted_hashrate_th_s']} TH/s",
            f"- Avg power: {gpu.get('avg_power_w')} W",
            f"- Avg temp: {gpu.get('avg_temp_c')} C",
            f"- Max temp: {gpu.get('max_temp_c')} C",
            f"- Efficiency: {item['efficiency_th_per_w']} TH/W",
            f"- Connected log lines: {item['connected_log_lines']}",
            f"- Pool/protocol error lines: {item['pool_protocol_errors']}",
            f"- Submitted/accepted log lines: {item['submitted_or_accepted_lines']}",
            f"- Rejected/invalid log lines: {item['rejected_or_invalid_lines']}",
            f"- Log: {item['log']}",
            "",
        ]
    )
lines.extend(
    [
        "## Winners",
        f"- Raw speed: {report['winner_raw_hashrate']}",
        f"- After fee: {report['winner_after_fee']}",
        f"- Efficiency: {report['winner_efficiency']}",
        "",
        "Short runs are noisy. Prefer 20-30 minutes per miner for a decision.",
    ]
)
(out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
PY

echo
echo "Report written to: $OUTPUT_DIR/report.md"
