from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import (
    base_wallet_address,
    get_float,
    get_int,
    get_oc_profiles,
    load_config,
)
from database import SessionLocal, record_event
from models import HardwareLog, MiningReward, SystemEvent


HASHRATE_UNITS = {
    "H": 1.0,
    "KH": 1e3,
    "MH": 1e6,
    "GH": 1e9,
    "TH": 1e12,
    "PH": 1e15,
    "EH": 1e18,
}

_JSON_CACHE: dict[str, tuple[float, dict[str, Any] | list[Any]]] = {}
_LAST_JOURNAL_LINE = ""


@dataclass
class CommandResult:
    ok: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def run_command(args: list[str] | str, timeout: int = 15, env: dict[str, str] | None = None, sudo: bool = False) -> CommandResult:
    if isinstance(args, str):
        cmd = shlex.split(args)
    else:
        cmd = list(args)
    if sudo and (not cmd or cmd[0] != "sudo"):
        cmd[:0] = ["sudo", "-n"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return CommandResult(
            ok=result.returncode == 0,
            command=" ".join(shlex.quote(part) for part in cmd),
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(False, " ".join(cmd), exc.stdout or "", f"timeout after {timeout}s", 124)
    except Exception as exc:
        return CommandResult(False, " ".join(cmd), "", str(exc), 1)


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?", str(value).replace(",", ""), flags=re.I)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def hashrate_to_hps(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().replace("/S", "").replace("B/S", "").replace(" ", "")
    match = re.match(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[-+]?\d+)?)([KMGTPE]?H)?", text)
    if not match:
        return 0.0
    amount = safe_float(match.group(1))
    unit = match.group(2) or "H"
    return amount * HASHRATE_UNITS.get(unit, 1.0)


def hps_to_th(value: float) -> float:
    return float(value or 0.0) / 1e12


def format_hashrate_hps(value: float) -> str:
    value = float(value or 0.0)
    for unit, factor in (("EH/s", 1e18), ("PH/s", 1e15), ("TH/s", 1e12), ("GH/s", 1e9), ("MH/s", 1e6), ("KH/s", 1e3)):
        if abs(value) >= factor:
            return f"{value / factor:.2f} {unit}"
    return f"{value:.0f} H/s"


def fetch_json(url: str, timeout: int = 8, ttl: int = 20) -> dict[str, Any] | list[Any] | None:
    now = time.monotonic()
    cached = _JSON_CACHE.get(url)
    if cached and cached[0] > now:
        return cached[1]
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "PearlMinerManager/1.0"})
        response.raise_for_status()
        data = response.json()
        _JSON_CACHE[url] = (now + ttl, data)
        return data
    except Exception as exc:
        record_event("warning", "external_api", f"Cannot fetch {url}", str(exc))
        return None


def _first_number(data: dict[str, Any], keys: list[str]) -> float:
    for key in keys:
        if key in data:
            return safe_float(data.get(key), 0.0)
    return 0.0


def _first_number_deep(data: Any, keys: list[str]) -> float:
    if isinstance(data, dict):
        direct = _first_number(data, keys)
        if direct > 0:
            return direct
        for value in data.values():
            found = _first_number_deep(value, keys)
            if found > 0:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _first_number_deep(item, keys)
            if found > 0:
                return found
    return 0.0


def _normalize_workers(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [worker for worker in value.values() if isinstance(worker, dict)]
    if isinstance(value, list):
        return [worker for worker in value if isinstance(worker, dict)]
    return []


def _sum_worker_hashrate(workers: list[dict[str, Any]], keys: list[str]) -> float:
    total = 0.0
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        for key in keys:
            if worker.get(key):
                total += hashrate_to_hps(worker.get(key))
                break
    return total


def _pool_api_urls(config: dict[str, str], wallet: str) -> list[str]:
    templates = [config.get("POOL_API_URL") or "https://pearl.alphapool.tech/api/miner/{wallet}"]
    fallback_raw = config.get("POOL_API_FALLBACK_URLS", "")
    if fallback_raw:
        templates.extend(part.strip() for part in re.split(r"[\s,]+", fallback_raw) if part.strip())
    urls: list[str] = []
    for template in templates:
        try:
            url = template.format(wallet=quote(wallet), address=quote(wallet))
        except Exception:
            url = template
        if url and url not in urls:
            urls.append(url)
    return urls


def _unwrap_miner_payload(raw: Any, wallet: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if any(key in raw for key in ("balance_prl", "balance", "pending", "pendingShares", "workers", "performance", "estHash1h")):
        return raw
    for key in ("miner", "data", "result", "stats"):
        value = raw.get(key)
        if isinstance(value, dict):
            nested = _unwrap_miner_payload(value, wallet)
            if nested is not None:
                return nested
    miners = raw.get("miners")
    if isinstance(miners, dict):
        value = miners.get(wallet) or miners.get(wallet.lower()) or miners.get(wallet.upper())
        if isinstance(value, dict):
            return value
    if isinstance(miners, list):
        for item in miners:
            if not isinstance(item, dict):
                continue
            address = str(item.get("address") or item.get("wallet") or item.get("login") or "").split(".", 1)[0]
            if address == wallet:
                return item
    return raw if raw else None


def _hashrate_from_performance(value: Any) -> float:
    if isinstance(value, dict):
        for key in ("hashrate", "hashrateLive", "hashrate_live", "value"):
            if key in value:
                return hashrate_to_hps(value.get(key))
    if isinstance(value, list):
        for item in reversed(value):
            rate = _hashrate_from_performance(item)
            if rate > 0:
                return rate
    return hashrate_to_hps(value)


def fetch_pool_miner_stats(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    wallet = base_wallet_address(cfg)
    urls = _pool_api_urls(cfg, wallet)
    raw: dict[str, Any] | None = None
    url = urls[0] if urls else ""
    for candidate_url in urls:
        candidate_raw = fetch_json(candidate_url)
        candidate_payload = _unwrap_miner_payload(candidate_raw, wallet)
        if isinstance(candidate_payload, dict):
            raw = candidate_payload
            url = candidate_url
            break
    if not isinstance(raw, dict):
        return {
            "available": False,
            "url": url,
            "wallet": wallet,
            "balance_prl": 0.0,
            "total_paid_prl": 0.0,
            "hashrate_hps": 0.0,
            "hashrate_label": "N/A",
            "shares24h": 0,
            "workers": [],
            "payments": [],
            "raw": {},
        }

    workers = _normalize_workers(raw.get("workers"))
    balance = _first_number(raw, ["balance_prl", "balance", "pending", "pending_balance", "unpaid", "unpaid_prl", "pendingShares"])
    total_paid = _first_number(raw, ["total_paid_prl", "paid", "paid_prl", "totalPaid", "totalPaid_prl"])
    if balance == 0.0 and raw.get("balance_grain"):
        balance = safe_float(raw.get("balance_grain")) / 1e8
    hashrate_hps = safe_float(raw.get("estHash1hRaw"), 0.0)
    if hashrate_hps <= 0:
        hashrate_hps = hashrate_to_hps(raw.get("estHash1h") or raw.get("hashrate") or raw.get("hashrate_1h") or raw.get("hashrate_1h_hps"))
    if hashrate_hps <= 0:
        hashrate_hps = _hashrate_from_performance(raw.get("performance"))
    live_hps = _sum_worker_hashrate(workers, ["hashrate_live", "hashrateLive", "liveHashrate"])
    if live_hps > 0:
        hashrate_hps = live_hps
    shares24h = raw.get("shares24h")
    if shares24h is None:
        shares24h = raw.get("validShares") or raw.get("shares") or raw.get("sharesValid")

    return {
        "available": True,
        "url": url,
        "wallet": wallet,
        "balance_prl": balance,
        "total_paid_prl": total_paid,
        "hashrate_hps": hashrate_hps,
        "hashrate_label": format_hashrate_hps(hashrate_hps),
        "shares24h": int(safe_float(shares24h, 0.0)),
        "workers": workers,
        "payments": raw.get("payments") if isinstance(raw.get("payments"), list) else [],
        "last_seen": raw.get("last_seen") or raw.get("lastSeen"),
        "mode": raw.get("mode") or raw.get("paymentProcessing") or ("SOLO" if raw.get("is_solo") else "PPLNS"),
        "raw": raw,
    }


def fetch_pool_summary(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    stats_url = cfg.get("POOL_STATS_URL") or "https://pearl.alphapool.tech/api/stats"
    chain_url = cfg.get("CHAIN_API_URL") or ""
    stats = fetch_json(stats_url)
    chain = fetch_json(chain_url) if chain_url else None
    summary: dict[str, Any] = {
        "available": isinstance(stats, dict),
        "fee_percent": 0.0,
        "reward_prl": 0.0,
        "network_hashrate_hps": 0.0,
        "pool_hashrate_hps": 0.0,
        "block_time_seconds": 0.0,
        "miners24h": 0,
        "workers": 0,
        "blocks24h": 0,
        "raw": stats if isinstance(stats, dict) else {},
        "chain_raw": chain if isinstance(chain, dict) else {},
    }
    if isinstance(stats, dict):
        coins = stats.get("coins") if isinstance(stats.get("coins"), list) else []
        coin = next((item for item in coins if isinstance(item, dict)), {})
        pool = stats.get("pool") if isinstance(stats.get("pool"), dict) else {}
        summary.update(
            {
                "fee_percent": safe_float(stats.get("feePercent"), 0.0),
                "reward_prl": safe_float(coin.get("reward"), 0.0),
                "network_hashrate_hps": hashrate_to_hps(coin.get("network_hash")),
                "pool_hashrate_hps": hashrate_to_hps(pool.get("hashrate")),
                "miners24h": int(safe_float(pool.get("miners24h"), 0.0)),
                "workers": int(safe_float(pool.get("workers"), 0.0)),
                "blocks24h": int(safe_float(pool.get("blocks24h"), 0.0)),
                "height": (stats.get("chain") or {}).get("height") if isinstance(stats.get("chain"), dict) else None,
                "stratum": stats.get("stratum") if isinstance(stats.get("stratum"), dict) else {},
            }
        )
    if isinstance(chain, dict):
        summary["network_hashrate_hps"] = safe_float(chain.get("estimated_hashrate_hps"), summary["network_hashrate_hps"])
        summary["pool_hashrate_hps"] = safe_float(chain.get("estimated_pool_hashrate_hps"), summary["pool_hashrate_hps"])
        summary["block_time_seconds"] = safe_float(chain.get("avg_block_time_seconds"), summary["block_time_seconds"])
    if summary["block_time_seconds"] <= 0:
        summary["block_time_seconds"] = 132.86
    return summary


def fetch_price(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    price_usd = 0.0
    source = "N/A"
    raw: dict[str, Any] = {}

    price_url = cfg.get("PRICE_API_URL")
    if price_url:
        data = fetch_json(price_url)
        if isinstance(data, dict):
            raw = data
            price_usd = _first_number_deep(data, ["price_usd", "usd", "price"])
            source = str(data.get("source") or data.get("source_id") or "PRLScan")

    coin_id = cfg.get("COINGECKO_COIN_ID", "").strip()
    if price_usd <= 0 and coin_id:
        data = fetch_json(f"https://api.coingecko.com/api/v3/simple/price?ids={quote(coin_id)}&vs_currencies=usd")
        if isinstance(data, dict) and isinstance(data.get(coin_id), dict):
            raw = data
            price_usd = safe_float(data[coin_id].get("usd"), 0.0)
            source = "CoinGecko"

    usd_vnd = get_float(cfg, "USD_VND_RATE", 25500)
    return {
        "available": price_usd > 0,
        "price_usd": price_usd,
        "price_vnd": price_usd * usd_vnd if price_usd > 0 else 0.0,
        "usd_vnd_rate": usd_vnd,
        "source": source,
        "raw": raw,
    }


def get_gpu_metrics(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    query = "name,temperature.gpu,power.draw,fan.speed,memory.used,memory.total,utilization.gpu"
    result = run_command(
        [
            "nvidia-smi",
            f"--id={cfg.get('GPU_INDEX', '0')}",
            f"--query-gpu={query}",
            "--format=csv,noheader,nounits",
        ],
        timeout=10,
    )
    if not result.ok or not result.stdout:
        return {
            "available": False,
            "gpu_name": "N/A",
            "temp_c": 0.0,
            "power_w": 0.0,
            "fan_speed": 0.0,
            "vram_gb": 0.0,
            "vram_total_gb": 0.0,
            "utilization": 0.0,
            "error": result.stderr or result.stdout or "nvidia-smi unavailable",
        }

    parts = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
    while len(parts) < 7:
        parts.append("0")
    return {
        "available": True,
        "gpu_name": parts[0],
        "temp_c": safe_float(parts[1]),
        "power_w": safe_float(parts[2]),
        "fan_speed": safe_float(parts[3]),
        "vram_gb": safe_float(parts[4]) / 1024,
        "vram_total_gb": safe_float(parts[5]) / 1024,
        "utilization": safe_float(parts[6]),
        "error": "",
    }


def get_miner_status(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    service = cfg.get("MINER_SERVICE", "pearl-miner.service")
    active = run_command(["systemctl", "is-active", service], timeout=5)
    show = run_command(
        ["systemctl", "show", service, "--property=ActiveEnterTimestamp", "--property=SubState", "--property=MainPID", "--no-page"],
        timeout=5,
    )
    show_props: dict[str, str] = {}
    for line in show.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            show_props[key] = value
    main_pid = show_props.get("MainPID", "").strip()
    process_running = main_pid not in {"", "0"}
    pid = main_pid if process_running else ""
    if not process_running:
        miner_exec = Path(cfg.get("MINER_EXEC") or "").name
        process_pattern = miner_exec or ("SRBMiner-MULTI" if (cfg.get("MINER_TYPE") or "").lower() == "srbminer" else "alpha-miner")
        pgrep = run_command(["pgrep", "-f", process_pattern], timeout=5)
        process_running = bool(pgrep.stdout.strip())
        pid = pgrep.stdout.splitlines()[0] if pgrep.stdout else ""
    return {
        "service": service,
        "systemd_state": active.stdout if active.stdout else "unknown",
        "is_active": active.stdout == "active",
        "process_running": process_running,
        "pid": pid,
        "details": show.stdout,
        "status": "Đang chạy" if active.stdout == "active" else "Đã dừng",
    }


def send_telegram_notification(text: str, config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    token = str(cfg.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = str(cfg.get("TELEGRAM_CHAT_ID") or cfg.get("CHAT_ID") or "").strip()
    if not token or not chat_id:
        return {"ok": False, "skipped": True, "error": "missing_telegram_config"}
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        ok = bool(payload.get("ok"))
        if not ok:
            record_event("warning", "telegram", "Telegram notification rejected", json.dumps(payload, ensure_ascii=False))
        return {"ok": ok, "response": payload}
    except Exception as exc:
        record_event("warning", "telegram", "Cannot send Telegram notification", str(exc))
        return {"ok": False, "error": str(exc)}


def notify_miner_started(action: str = "start", config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    service = cfg.get("MINER_SERVICE", "pearl-miner.service")
    pool = f"{cfg.get('POOL_HOST', 'N/A')}:{cfg.get('POOL_PORT', 'N/A')}"
    verb = "khởi động lại" if action == "restart" else "được bật"
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    message = (
        f"✅ Máy đào Pearl đã {verb}.\n"
        f"Service: {service}\n"
        f"Pool: {pool}\n"
        f"Thời gian: {timestamp}"
    )
    return send_telegram_notification(message, cfg)


def control_miner(action: str, config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    action = action.lower().strip()
    if action not in {"start", "stop", "restart"}:
        return {"ok": False, "error": f"Unsupported action: {action}"}
    result = run_command(["systemctl", action, cfg.get("MINER_SERVICE", "pearl-miner.service")], timeout=25, sudo=True)
    if result.ok:
        record_event("info", "control", f"Miner {action} requested")
        if action in {"start", "restart"}:
            notify_miner_started(action, cfg)
    else:
        record_event("error", "control", f"Miner {action} failed", result.stderr)
    return {"ok": result.ok, "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}


def _candidate_nvidia_settings_displays(config: dict[str, str]) -> list[str]:
    displays: list[str] = []
    for value in (config.get("DISPLAY"), os.environ.get("DISPLAY"), ":1", ":0"):
        display = (value or "").strip()
        if display and display not in displays:
            displays.append(display)
    return displays or [":0"]


def apply_oc_profile(profile_id: str, config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    profiles = get_oc_profiles(cfg)
    profile = profiles.get(profile_id)
    if not profile:
        return {"ok": False, "error": f"Unknown profile: {profile_id}", "profiles": list(profiles)}
    try:
        power_limit = int(profile.get("power_limit", 0))
        gpu_clock_min = int(profile.get("gpu_clock_min", 0))
        gpu_clock_max = int(profile.get("gpu_clock_max", gpu_clock_min))
        core_offset = int(profile.get("core_offset", 0))
        memory_offset = int(profile.get("memory_offset", 0))
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Invalid profile values for {profile_id}: {exc}", "profile": profile}
    if gpu_clock_min < 0 or gpu_clock_max < 0 or (gpu_clock_min and gpu_clock_max and gpu_clock_min > gpu_clock_max):
        return {"ok": False, "error": f"Invalid GPU clock lock for {profile_id}", "profile": profile}

    gpu_index = cfg.get("GPU_INDEX", "0")
    env = os.environ.copy()
    env.update({"DISPLAY": cfg.get("DISPLAY", ":0")})
    if cfg.get("XAUTHORITY"):
        env["XAUTHORITY"] = cfg["XAUTHORITY"]
    perf_level = str(get_int(cfg, "NVIDIA_SETTINGS_PERF_LEVEL", 3))
    commands = [
        (["nvidia-smi", "-pm", "1"], True, True),
        (["nvidia-smi", f"--id={gpu_index}", f"--power-limit={power_limit}"], True, True),
    ]
    if gpu_clock_min > 0 and gpu_clock_max > 0:
        commands.append((["nvidia-smi", f"--id={gpu_index}", f"--lock-gpu-clocks={gpu_clock_min},{gpu_clock_max}"], True, True))
    else:
        commands.append((["nvidia-smi", f"--id={gpu_index}", "--reset-gpu-clocks"], True, True))
    results = []
    for cmd, needs_sudo, required in commands:
        result = run_command(cmd, timeout=15, env=env, sudo=needs_sudo)
        payload = {**result.__dict__, "required": required}
        results.append(payload)

    optional_warnings: list[str] = []
    for assignment in (
        f"[gpu:{gpu_index}]/GPUGraphicsClockOffset[{perf_level}]={core_offset}",
        f"[gpu:{gpu_index}]/GPUMemoryTransferRateOffset[{perf_level}]={memory_offset}",
    ):
        attempts = []
        for display in _candidate_nvidia_settings_displays(cfg):
            attempt_env = env.copy()
            attempt_env["DISPLAY"] = display
            result = run_command(["nvidia-settings", "-c", display, "-a", assignment], timeout=15, env=attempt_env)
            payload = {**result.__dict__, "required": False, "display": display}
            results.append(payload)
            attempts.append(payload)
            if result.ok:
                break
        if not any(item["ok"] for item in attempts):
            last = attempts[-1] if attempts else {}
            optional_warnings.append(str(last.get("stderr") or last.get("stdout") or last.get("command") or assignment))

    ok = all(item["ok"] for item in results if item["required"])
    record_event("info" if ok else "warning", "overclock", f"Applied profile {profile_id}", json.dumps(results))
    return {"ok": ok, "profile": profile, "results": results, "warnings": optional_warnings}


def collect_and_store_sample(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    gpu = get_gpu_metrics(cfg)
    miner = fetch_pool_miner_stats(cfg)
    status = get_miner_status(cfg)
    service_running = bool(status.get("is_active") and status.get("process_running"))
    hashrate_hps = miner.get("hashrate_hps", 0.0) if service_running else 0.0
    log = HardwareLog(
        temp_c=float(gpu.get("temp_c") or 0.0),
        power_w=float(gpu.get("power_w") or 0.0),
        fan_speed=float(gpu.get("fan_speed") or 0.0),
        hashrate_th=hps_to_th(hashrate_hps),
        vram_gb=float(gpu.get("vram_gb") or 0.0),
        gpu_name=str(gpu.get("gpu_name") or ""),
    )
    try:
        with SessionLocal() as db:
            db.add(log)
            db.commit()
    except Exception as exc:
        record_event("error", "database", "Cannot store hardware log", str(exc))
    return {"gpu": gpu, "miner": miner, "status": status, "hashrate_th": hps_to_th(hashrate_hps)}


def calculate_hourly_reward(pool_stats: dict[str, Any] | None = None) -> float:
    stats = pool_stats or fetch_pool_miner_stats()
    now_ts = int(time.time())
    total = 0.0
    for payment in stats.get("payments", []):
        if not isinstance(payment, dict) or payment.get("status") == "orphaned":
            continue
        ts = int(safe_float(payment.get("ts"), 0.0))
        if now_ts - 3600 <= ts <= now_ts:
            if payment.get("amount_grain") is not None:
                total += safe_float(payment.get("amount_grain")) / 1e8
            else:
                total += safe_float(payment.get("amount") or payment.get("amount_prl"))
    return total


def record_reward_if_due(config: dict[str, str] | None = None) -> None:
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    try:
        with SessionLocal() as db:
            reward = calculate_hourly_reward(fetch_pool_miner_stats(config))
            stmt = (
                pg_insert(MiningReward)
                .values(timestamp=hour_start, pearl_mined_hour=reward, source="alphapool")
                .on_conflict_do_update(
                    index_elements=["timestamp"],
                    set_={"pearl_mined_hour": reward, "source": "alphapool"},
                )
            )
            db.execute(stmt)
            db.commit()
    except Exception as exc:
        record_event("error", "database", "Cannot store hourly reward", str(exc))


def record_journal_snapshot(config: dict[str, str] | None = None, limit: int = 20) -> None:
    global _LAST_JOURNAL_LINE
    cfg = config or load_config()
    service = cfg.get("MINER_SERVICE", "pearl-miner.service")
    result = run_command(["journalctl", "-u", service, "-n", str(limit), "-o", "short-iso", "--no-pager"], timeout=10)
    if not result.ok:
        record_event("warning", "journal", "Cannot read miner journal", result.stderr or result.stdout)
        return
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return
    new_lines: list[str] = []
    if _LAST_JOURNAL_LINE and _LAST_JOURNAL_LINE in lines:
        new_lines = lines[lines.index(_LAST_JOURNAL_LINE) + 1 :]
    else:
        new_lines = lines[-5:]
    _LAST_JOURNAL_LINE = lines[-1]
    if not new_lines:
        return
    try:
        with SessionLocal() as db:
            for line in new_lines:
                db.add(SystemEvent(level="info", category="journal", message=line[:1000], details=service))
            db.commit()
    except Exception as exc:
        record_event("error", "database", "Cannot store journal snapshot", str(exc))


def today_reward_prl() -> float:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    try:
        with SessionLocal() as db:
            return float(db.query(func.coalesce(func.sum(MiningReward.pearl_mined_hour), 0.0)).filter(MiningReward.timestamp >= start).scalar() or 0.0)
    except Exception:
        return 0.0


def get_chart_data(days: int = 7) -> dict[str, Any]:
    local_now = datetime.now().astimezone()
    start = (local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)).astimezone(timezone.utc)
    try:
        with SessionLocal() as db:
            rewards = db.query(MiningReward).filter(MiningReward.timestamp >= start).order_by(MiningReward.timestamp.asc()).all()
            hardware = db.query(HardwareLog).filter(HardwareLog.timestamp >= datetime.now(timezone.utc) - timedelta(hours=24)).order_by(HardwareLog.timestamp.asc()).all()
    except Exception:
        rewards = []
        hardware = []

    daily: dict[str, float] = {}
    for reward in rewards:
        key = reward.timestamp.astimezone().strftime("%d/%m")
        daily[key] = daily.get(key, 0.0) + float(reward.pearl_mined_hour or 0.0)

    labels = []
    daily_values = []
    cumulative_values = []
    cumulative = 0.0
    for offset in range(days - 1, -1, -1):
        label = (local_now - timedelta(days=offset)).strftime("%d/%m")
        value = daily.get(label, 0.0)
        cumulative += value
        labels.append(label)
        daily_values.append(round(value, 6))
        cumulative_values.append(round(cumulative, 6))

    return {
        "labels": labels,
        "daily": daily_values,
        "cumulative": cumulative_values,
        "history_labels": [row.timestamp.astimezone().strftime("%H:%M") for row in hardware],
        "hashrate_th": [round(float(row.hashrate_th or 0.0), 4) for row in hardware],
        "temp_c": [round(float(row.temp_c or 0.0), 1) for row in hardware],
    }


def estimate_revenue(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    miner = fetch_pool_miner_stats(cfg)
    pool = fetch_pool_summary(cfg)
    price = fetch_price(cfg)
    status = get_miner_status(cfg)
    service_running = bool(status.get("is_active") and status.get("process_running"))
    miner_hps = float(miner.get("hashrate_hps") or 0.0) if service_running else 0.0
    network_hps = float(pool.get("network_hashrate_hps") or 0.0)
    reward = float(pool.get("reward_prl") or 0.0)
    block_time = float(pool.get("block_time_seconds") or 132.86)
    fee_factor = max(0.0, 1.0 - float(pool.get("fee_percent") or 0.0) / 100)
    prl_24h = 0.0
    if miner_hps > 0 and network_hps > 0 and reward > 0 and block_time > 0:
        prl_24h = (miner_hps / network_hps) * (86400 / block_time) * reward * fee_factor
    gpu = get_gpu_metrics(cfg)
    if not service_running:
        assessment = "Miner đang dừng hoặc watchdog vừa bảo vệ máy."
    elif float(gpu.get("temp_c") or 0.0) >= 78:
        assessment = "Nhiệt độ đang làm giảm hiệu năng, nên kiểm tra gió và power limit."
    elif miner_hps <= 0:
        assessment = "Chưa có hashrate từ AlphaPool hoặc miner đang dừng."
    else:
        assessment = "Tốc độ đang tối ưu theo dữ liệu AlphaPool hiện tại."
    return {
        "prl_24h": prl_24h,
        "prl_7d": prl_24h * 7,
        "usd_24h": prl_24h * price.get("price_usd", 0.0),
        "vnd_24h": prl_24h * price.get("price_vnd", 0.0),
        "assessment": assessment,
        "miner": miner,
        "pool": pool,
        "price": price,
    }


def render_hardware_chart(hours: int = 24) -> BytesIO | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        record_event("warning", "telegram", "matplotlib is not available", str(exc))
        return None

    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        with SessionLocal() as db:
            rows = db.query(HardwareLog).filter(HardwareLog.timestamp >= start).order_by(HardwareLog.timestamp.asc()).all()
    except Exception as exc:
        record_event("error", "telegram", "Cannot query hardware chart data", str(exc))
        return None
    if not rows:
        return None

    labels = [row.timestamp.astimezone().strftime("%H:%M") for row in rows]
    temps = [row.temp_c for row in rows]
    hashrates = [row.hashrate_th for row in rows]

    fig, ax1 = plt.subplots(figsize=(10, 4.8), dpi=140)
    ax1.plot(labels, hashrates, color="#2563eb", linewidth=2, label="Hashrate TH/s")
    ax1.set_ylabel("TH/s", color="#2563eb")
    ax1.tick_params(axis="y", labelcolor="#2563eb")
    ax1.tick_params(axis="x", rotation=45, labelsize=7)
    ax1.grid(True, alpha=0.25)
    ax2 = ax1.twinx()
    ax2.plot(labels, temps, color="#ef4444", linewidth=2, label="Temp C")
    ax2.set_ylabel("C", color="#ef4444")
    ax2.tick_params(axis="y", labelcolor="#ef4444")
    fig.suptitle("Pearl Miner - Hardware 24h")
    fig.tight_layout()
    buffer = BytesIO()
    fig.savefig(buffer, format="png")
    plt.close(fig)
    buffer.seek(0)
    return buffer


async def stream_journal_lines(service: str):
    process = await asyncio.create_subprocess_exec(
        "journalctl",
        "-u",
        service,
        "-f",
        "-n",
        "50",
        "-o",
        "cat",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").rstrip()
        if process.stderr is not None:
            err = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            if err:
                yield f"[journalctl] {err}"
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
