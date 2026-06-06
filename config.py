from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("PEARL_CONFIG") or os.environ.get("CONFIG_FILE") or BASE_DIR / "config.env")


DEFAULTS: dict[str, str] = {
    "APP_NAME": "Pearl Miner Manager",
    "WEB_HOST": "127.0.0.1",
    "WEB_PORT": "8555",
    "LIVE_UPDATE_SECONDS": "2",
    "DB_USER": "postgres",
    "DB_PASS": "postgres",
    "DB_NAME": "pearl_db",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DATABASE_URL": "",
    "WALLET": "",
    "POOL": "",
    "WALLET_ADDRESS": "CHANGE_ME_PEARL_WALLET",
    "WORKER_NAME": "NTD_Rig",
    "POOL_HOST": "sg1.alphapool.tech",
    "POOL_PORT": "5566",
    "POOL_API_URL": "https://pearl.alphapool.tech/api/miner/{wallet}",
    "POOL_API_FALLBACK_URLS": "https://pearl.alphapool.tech/api/pools/pearl/miners/{wallet}",
    "POOL_STATS_URL": "https://pearl.alphapool.tech/api/stats",
    "POOL_CHARTS_URL": "https://pearl.alphapool.tech/api/charts",
    "PRICE_API_URL": "https://api.prlscan.com/v1/market/prl",
    "CHAIN_API_URL": "https://api.prlscan.com/v1/analytics/summary",
    "COINGECKO_COIN_ID": "",
    "USD_VND_RATE": "25500",
    "MINER_TYPE": "alpha",
    "MINER_DIR": "/home/ntd/Downloads/alpha-miner",
    "MINER_EXEC": "/home/ntd/Downloads/alpha-miner/alpha-miner",
    "MINER_ALGORITHM": "pearlhash",
    "MINER_PASSWORD": "x;d=65536",
    "MINER_EXTRA_ARGS": "--status-interval 60",
    "MINER_SERVICE": "pearl-miner.service",
    "WEB_SERVICE": "pearl-web.service",
    "BOT_SERVICE": "pearl-bot.service",
    "TELEGRAM_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "CHAT_ID": "",
    "CONTROL_API_TOKEN": "",
    "GPU_INDEX": "0",
    "DISPLAY": ":0",
    "XAUTHORITY": "",
    "TEMP_SHUTDOWN_C": "80",
    "TEMP_WARN_C": "84",
    "HASHRATE_ZERO_LIMIT": "1",
    "HASHRATE_ZERO_STOP": "0",
    "HOT_LIMIT_COUNT": "1",
    "LOCAL_MINER_STALE_SECONDS": "180",
    "POOL_STALE_AFTER_SECONDS": "180",
    "SNAPSHOT_CACHE_SECONDS": "2",
    "STARTUP_OC_PROFILE": "balance",
    "NVIDIA_SETTINGS_PERF_LEVEL": "3",
    "OC_PROFILES_JSON": "",
}


DEFAULT_OC_PROFILES: dict[str, dict[str, int | str]] = {
    "eco": {
        "label": "Eco Mode",
        "power_limit": 100,
        "gpu_clock_min": 1200,
        "gpu_clock_max": 1200,
        "core_offset": 0,
        "memory_offset": 500,
    },
    "balance": {
        "label": "Balance Undervolt",
        "power_limit": 115,
        "gpu_clock_min": 1450,
        "gpu_clock_max": 1450,
        "core_offset": 200,
        "memory_offset": 1000,
    },
    "max": {
        "label": "Max Hashrate",
        "power_limit": 130,
        "gpu_clock_min": 1500,
        "gpu_clock_max": 1500,
        "core_offset": 250,
        "memory_offset": 1200,
    },
}


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_config() -> dict[str, str]:
    config = DEFAULTS.copy()
    file_keys: set[str] = set()
    if CONFIG_FILE.exists():
        for raw_line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            normalized_key = key.strip()
            file_keys.add(normalized_key)
            config[normalized_key] = _strip_quotes(value)

    env_keys: set[str] = set()
    for key in set(config) | set(DEFAULTS):
        if key in os.environ:
            env_keys.add(key)
            config[key] = os.environ[key]

    provided_keys = file_keys | env_keys

    if config.get("WALLET") and "WALLET_ADDRESS" not in provided_keys:
        config["WALLET_ADDRESS"] = config["WALLET"]
    if config.get("POOL") and ("POOL_HOST" not in provided_keys and "POOL_PORT" not in provided_keys):
        host, _, port = config["POOL"].partition(":")
        if host:
            config["POOL_HOST"] = host
        if port:
            config["POOL_PORT"] = port
    if not config.get("TELEGRAM_CHAT_ID") and config.get("CHAT_ID"):
        config["TELEGRAM_CHAT_ID"] = config["CHAT_ID"]

    miner_dir = config.get("MINER_DIR")
    miner_type = (config.get("MINER_TYPE") or "").strip().lower()
    if miner_dir and not config.get("MINER_EXEC"):
        config["MINER_EXEC"] = str(Path(miner_dir) / ("SRBMiner-MULTI" if miner_type == "srbminer" else "alpha-miner"))

    return config


def get_float(config: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def get_int(config: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def base_wallet_address(config: dict[str, str] | None = None) -> str:
    cfg = config or load_config()
    wallet = (cfg.get("WALLET_ADDRESS") or cfg.get("WALLET") or "").strip()
    return wallet.split(".", 1)[0]


def miner_login(config: dict[str, str] | None = None) -> str:
    cfg = config or load_config()
    wallet = (cfg.get("WALLET_ADDRESS") or cfg.get("WALLET") or "").strip()
    if "." in wallet:
        return wallet
    worker = (cfg.get("WORKER_NAME") or "").strip()
    return f"{wallet}.{worker}" if wallet and worker else wallet


def pool_target(config: dict[str, str] | None = None) -> str:
    cfg = config or load_config()
    return f"{cfg.get('POOL_HOST', '').strip()}:{cfg.get('POOL_PORT', '').strip()}"


def _coerce_oc_profile(profile_id: str, profile: Any) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    try:
        power_limit = int(float(profile.get("power_limit", 0)))
        core_offset = int(float(profile.get("core_offset", 0)))
        memory_offset = int(float(profile.get("memory_offset", 0)))
        raw_clock = profile.get("gpu_clock_lock", profile.get("lock_gpu_clock", profile.get("gpu_clock_mhz")))
        if raw_clock is not None:
            if isinstance(raw_clock, str) and "," in raw_clock:
                min_part, _, max_part = raw_clock.partition(",")
                gpu_clock_min = int(float(min_part.strip()))
                gpu_clock_max = int(float(max_part.strip()))
            else:
                gpu_clock_min = gpu_clock_max = int(float(raw_clock))
        else:
            gpu_clock_min = int(float(profile.get("gpu_clock_min", 0)))
            gpu_clock_max = int(float(profile.get("gpu_clock_max", gpu_clock_min)))
    except (TypeError, ValueError):
        return None
    if power_limit < 0 or gpu_clock_min < 0 or gpu_clock_max < 0 or (gpu_clock_min and gpu_clock_max and gpu_clock_min > gpu_clock_max):
        return None
    label = str(profile.get("label") or profile_id)
    return {
        "label": label,
        "power_limit": power_limit,
        "gpu_clock_min": gpu_clock_min,
        "gpu_clock_max": gpu_clock_max,
        "core_offset": core_offset,
        "memory_offset": memory_offset,
    }


def get_oc_profiles(config: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    cfg = config or load_config()
    raw_json = cfg.get("OC_PROFILES_JSON", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                profiles = {
                    str(profile_id): coerced
                    for profile_id, profile in parsed.items()
                    if (coerced := _coerce_oc_profile(str(profile_id), profile)) is not None
                }
                if profiles:
                    return profiles
        except json.JSONDecodeError:
            pass
    return {
        profile_id: coerced
        for profile_id, profile in DEFAULT_OC_PROFILES.items()
        if (coerced := _coerce_oc_profile(profile_id, profile)) is not None
    }
