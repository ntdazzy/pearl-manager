#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

from config import get_float, get_int, get_oc_profiles, load_config
from database import init_db, record_event
from miner_services import (
    apply_oc_profile,
    collect_and_store_sample,
    collect_telemetry_snapshot,
    control_miner,
    record_reward_if_due,
    render_hardware_chart,
)


STATE: dict[str, int | float | bool] = {
    "hot_count": 0,
    "zero_hash_count": 0,
    "crash_count": 0,
    "last_zero_alert_ts": 0,
    "last_hot_alert_ts": 0,
    "last_crash_alert_ts": 0,
    "last_snapshot_error_ts": 0,
    "alerted_hot": False,
    "alerted_zero": False,
    "alerted_crash": False,
}


MINER_ACTIONS = {"start", "stop", "restart"}
TELEGRAM_CAPTION_LIMIT = 1024


def cfg() -> dict[str, str]:
    return load_config()


def quiet_bot_config() -> dict[str, str]:
    config = cfg().copy()
    config["TELEGRAM_TOKEN"] = ""
    config["TELEGRAM_CHAT_ID"] = ""
    config["CHAT_ID"] = ""
    return config


def allowed_chat_id() -> str:
    config = cfg()
    return str(config.get("TELEGRAM_CHAT_ID") or config.get("CHAT_ID") or "").strip()


def is_authorized(update: Update) -> bool:
    allowed = allowed_chat_id()
    return bool(allowed and update.effective_chat and str(update.effective_chat.id) == allowed)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def fmt_num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "ok", "online", "active", "connected", "alive"}:
        return True
    if text in {"0", "false", "no", "n", "offline", "inactive", "dead"}:
        return False
    return bool(value)


def is_service_running(status: dict[str, Any]) -> bool:
    return bool(status.get("is_active") and status.get("process_running"))


def snapshot(use_cache: bool = True) -> dict[str, Any]:
    return collect_telemetry_snapshot(cfg(), use_cache=use_cache)


def snapshot_safe(use_cache: bool = True) -> dict[str, Any]:
    try:
        return snapshot(use_cache=use_cache)
    except Exception as exc:
        now = time.monotonic()
        if now - float(STATE.get("last_snapshot_error_ts") or 0) >= 60:
            STATE["last_snapshot_error_ts"] = now
            record_event("error", "telegram", "Telemetry snapshot failed", str(exc))
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "system": {"status": "unknown", "systemd_state": "unknown", "process_running": False, "is_active": False},
            "gpu": {"gpu_name": "N/A", "temp_c": 0.0, "power_w": 0.0, "fan_speed": 0.0, "vram_gb": 0.0, "vram_total_gb": 0.0},
            "local_miner": {"available": False, "hashrate_hps": 0.0, "hashrate_label": "N/A", "stale": True},
            "pool_miner": {"available": False, "hashrate_hps": 0.0, "hashrate_label": "N/A", "workers": [], "error": str(exc)},
            "pool": {"available": False, "network_hashrate_hps": 0.0, "reward_prl": 0.0},
            "price": {"available": False, "price_usd": 0.0, "price_vnd": 0.0, "source": "N/A"},
            "effective_hashrate": {"hashrate_hps": 0.0, "hashrate_label": "0 H/s", "source": "snapshot_error"},
            "prediction": {"assessment": "Telemetry snapshot failed; xem log bot để kiểm tra."},
            "finance": {},
            "safety": {"level": "unknown", "reasons": ["Telemetry snapshot failed"]},
            "_error": str(exc),
        }


def compact_source(value: Any, max_len: int = 80) -> str:
    text = str(value or "").strip()
    if not text:
        return "N/A"
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        text = f"{parsed.netloc}{parsed.path}"
    if len(text) > max_len:
        return f"{text[: max_len - 3]}..."
    return text


def first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    try:
        if text.isdigit():
            return parse_timestamp(float(text))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def freshness_note(data: dict[str, Any]) -> str:
    stale = first_present(data, ("stale", "is_stale", "cache_stale"))
    if stale is not None:
        return "stale" if as_bool(stale) else "fresh"
    age = first_present(data, ("age_seconds", "cache_age_seconds", "stale_seconds", "stale_age_seconds", "last_signal_age_seconds"))
    if age is not None:
        seconds = max(0.0, as_float(age))
        return f"{seconds / 60:.1f} phút tuổi" if seconds >= 60 else f"{seconds:.0f}s tuổi"
    raw_time = first_present(data, ("updated_at", "fetched_at", "timestamp", "last_seen", "lastSeen", "last_status_at", "last_share_at"))
    timestamp = parse_timestamp(raw_time)
    if timestamp is not None:
        seconds = max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())
        return f"cách {seconds / 60:.1f} phút" if seconds >= 60 else f"cách {seconds:.0f}s"
    if raw_time:
        return f"last seen: {raw_time}"
    return ""


def source_line(label: str, data: dict[str, Any], fallback_source: Any = "") -> str:
    available = as_bool(data.get("available"))
    source = first_present(data, ("source", "source_url", "url", "api_url")) or fallback_source
    parts = [
        f"{label}: <b>{'OK' if available else 'N/A'}</b>",
        f"nguồn <code>{esc(compact_source(source))}</code>",
    ]
    freshness = freshness_note(data)
    if freshness:
        parts.append(esc(freshness))
    return " | ".join(parts)


def snapshot_issue_line(data: dict[str, Any]) -> str:
    error = data.get("_error")
    return f"\n⚠️ Snapshot lỗi: <code>{esc(compact_source(error, 120))}</code>" if error else ""


def limit_text(value: Any, max_len: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > max_len:
        return f"{text[: max_len - 3]}..."
    return text


def command_output(result: dict[str, Any]) -> str:
    output = limit_text(result.get("stderr") or result.get("stdout") or result.get("error") or "", 500)
    return output or "systemctl không trả thêm output."


def action_label(action: str) -> str:
    return {"start": "Bật máy", "stop": "Tắt máy", "restart": "Khởi động lại"}.get(action, action)


def friendly_source(source: Any) -> str:
    key = str(source or "").lower()
    sources = {
        "local": "đọc trực tiếp trên máy",
        "pool": "AlphaPool đang ghi nhận",
        "pool_stale": "AlphaPool đang dùng số cũ",
        "service_stopped": "máy đào đang dừng",
        "none": "chưa có tốc độ hợp lệ",
        "snapshot_error": "lỗi đọc số liệu",
        "unknown": "chưa rõ",
    }
    return sources.get(key, str(source or "chưa rõ"))


def friendly_safety(level: Any) -> str:
    key = str(level or "").lower()
    if key == "critical":
        return "nguy hiểm"
    if key == "warning":
        return "cần chú ý"
    if key == "ok":
        return "ổn"
    return str(level or "chưa rõ")


def friendly_assessment(text: Any) -> str:
    value = str(text or "N/A")
    replacements = {
        "Miner": "Máy đào",
        "miner": "máy đào",
        "GPU": "Card",
        "Hashrate": "Tốc độ",
        "hashrate": "tốc độ",
        "local": "trên máy",
        "Local": "Trên máy",
        "pool API": "AlphaPool",
        "Pool API": "AlphaPool",
        "API": "kết nối",
        "stale": "đã cũ",
        "service": "bộ quản lý",
        "watchdog": "bảo vệ tự động",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value.replace("Dự đoán doanh thu tạm đặt 0.", "Ước tính doanh thu tạm đặt 0.")


def hashrate_lines(data: dict[str, Any]) -> str:
    status = data.get("system", {})
    effective = data.get("effective_hashrate", {})
    local = data.get("local_miner", {})
    miner = data.get("pool_miner", {})
    local_label = local.get("hashrate_label") or "N/A"
    if not is_service_running(status):
        local_label = "0 H/s (service/process local dừng)"
    elif as_bool(local.get("stale")):
        local_label = f"{local_label} (stale)"
    pool_label = str(miner.get("hashrate_label") or "N/A")
    source = friendly_source(effective.get("source", "unknown"))
    return (
        f"Tốc độ chính: <b>{esc(effective.get('hashrate_label', '0 H/s'))}</b> "
        f"(<code>{esc(source)}</code>)\n"
        f"Trên máy: <b>{esc(local_label)}</b>\n"
        f"AlphaPool: <b>{esc(pool_label)}</b>"
    )


def worker_summary(miner: dict[str, Any]) -> str:
    workers = miner.get("workers")
    if not isinstance(workers, list):
        return "N/A"
    online = 0
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        marker = first_present(worker, ("online", "connected", "alive", "active", "status"))
        if as_bool(marker):
            online += 1
    return f"{online}/{len(workers)} máy đang chạy" if workers else "0"


def screen_menu(refresh_callback: str, primary_callback: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Làm mới", callback_data=refresh_callback)]]
    if primary_callback and primary_callback != refresh_callback:
        labels = {
            "stats": "📊 Thống kê",
            "controls": "🎮 Điều khiển",
            "balance": "💰 Số dư",
            "predict": "🧠 Dự đoán",
            "settings": "⚙️ Cài đặt",
        }
        rows.append([InlineKeyboardButton(labels.get(primary_callback, "Mở"), callback_data=primary_callback)])
    rows.extend(
        [
            [
                InlineKeyboardButton("📊 Thống kê", callback_data="stats"),
                InlineKeyboardButton("🎮 Điều khiển", callback_data="controls"),
            ],
            [
                InlineKeyboardButton("💰 Số dư", callback_data="balance"),
                InlineKeyboardButton("⚙️ Cài đặt", callback_data="settings"),
            ],
            [
                InlineKeyboardButton("❓ Trợ giúp", callback_data="help"),
                InlineKeyboardButton("⬅️ Menu", callback_data="menu"),
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


def control_text() -> str:
    data = snapshot_safe()
    config = cfg()
    status = data.get("system", {})
    gpu = data.get("gpu", {})
    miner = data.get("pool_miner", {})
    safety = data.get("safety", {})
    service = status.get("service") or config.get("MINER_SERVICE", "pearl-miner.service")
    return (
        "<b>🎮 Điều khiển máy đào</b>\n"
        f"Bộ quản lý: <code>{esc(service)}</code>\n"
        f"Trạng thái: <b>{esc(status.get('status', 'N/A'))}</b> | "
        f"chạy thật: <b>{'có' if status.get('process_running') else 'không'}</b>"
        f"{' | phiên ' + esc(status.get('pid')) if status.get('pid') else ''}\n"
        f"Card: <code>{esc(gpu.get('gpu_name', 'N/A'))}</code> | "
        f"{fmt_num(gpu.get('temp_c'), 0)}°C | {fmt_num(gpu.get('power_w'), 1)}W\n"
        f"{hashrate_lines(data)}\n"
        f"Máy trên AlphaPool: <b>{esc(worker_summary(miner))}</b>\n"
        f"Bảo vệ: <b>{esc(friendly_safety(safety.get('level', 'N/A')))}</b>"
        f"{' | ' + esc(', '.join(safety.get('reasons', [])[:2])) if safety.get('reasons') else ''}\n"
        f"{source_line('AlphaPool', miner)}\n\n"
        "Tắt/khởi động lại cần xác nhận. AlphaPool lỗi chỉ cảnh báo, không tự tắt máy."
        f"{snapshot_issue_line(data)}"
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Thống Kê", callback_data="stats"),
                InlineKeyboardButton("🎮 Điều Khiển", callback_data="controls"),
            ],
            [
                InlineKeyboardButton("💰 Số Dư", callback_data="balance"),
                InlineKeyboardButton("📈 Xem Biểu Đồ", callback_data="chart"),
            ],
            [
                InlineKeyboardButton("🧠 Dự Đoán", callback_data="predict"),
                InlineKeyboardButton("⚙️ Cài Đặt", callback_data="settings"),
            ],
            [InlineKeyboardButton("❓ Trợ Giúp", callback_data="help")],
        ]
    )


def control_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Bật máy", callback_data="miner:start"),
                InlineKeyboardButton("⏹ Tắt máy...", callback_data="miner:stop"),
            ],
            [
                InlineKeyboardButton("🔄 Khởi động lại...", callback_data="miner:restart"),
                InlineKeyboardButton("⚡ Mức điện", callback_data="oc_menu"),
            ],
            [InlineKeyboardButton("📊 Refresh trạng thái", callback_data="controls")],
            [
                InlineKeyboardButton("❓ Trợ giúp", callback_data="help"),
                InlineKeyboardButton("⬅️ Menu", callback_data="menu"),
            ],
        ]
    )


def confirm_miner_menu(action: str) -> InlineKeyboardMarkup:
    labels = {"stop": "tắt máy đào", "restart": "khởi động lại máy đào"}
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ Xác nhận {labels.get(action, action)}", callback_data=f"miner_run:{action}")],
            [InlineKeyboardButton("⬅️ Quay lại điều khiển", callback_data="controls")],
        ]
    )


def oc_menu() -> InlineKeyboardMarkup:
    icons = {"eco": "🌱", "balance": "⚖️", "max": "🚀"}
    rows = []
    for profile_id, profile in get_oc_profiles().items():
        label = profile.get("label") or profile_id
        rows.append([InlineKeyboardButton(f"{icons.get(profile_id, '⚡')} {label}", callback_data=f"oc:{profile_id}")])
    rows.append([InlineKeyboardButton("⬅️ Điều Khiển", callback_data="controls")])
    return InlineKeyboardMarkup(rows)


async def reply_or_edit(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    text = text or "N/A"
    if update.callback_query:
        message = update.callback_query.message
        try:
            if message and (message.photo or message.caption is not None) and len(text) <= TELEGRAM_CAPTION_LIMIT:
                await update.callback_query.edit_message_caption(
                    caption=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
                return
            if message and (message.photo or message.caption is not None):
                await message.reply_text(
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return
            if message:
                try:
                    await message.reply_text(
                        text=text,
                        reply_markup=markup,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except TelegramError as inner_exc:
                    record_event("warning", "telegram", "Cannot send callback fallback", str(inner_exc))
                return
            record_event("warning", "telegram", "Cannot edit callback message", str(exc))
        except TelegramError as exc:
            if message:
                try:
                    await message.reply_text(
                        text=text,
                        reply_markup=markup,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    return
                except TelegramError as inner_exc:
                    record_event("warning", "telegram", "Cannot send callback fallback", str(inner_exc))
            record_event("warning", "telegram", "Cannot edit callback message", str(exc))
    elif update.message:
        try:
            await update.message.reply_text(
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            record_event("warning", "telegram", "Cannot send Telegram reply", str(exc))


async def send_alert(context: ContextTypes.DEFAULT_TYPE, chat_id: str, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    except Exception as exc:
        record_event("error", "telegram", "Cannot deliver watchdog alert", str(exc))


def can_send_throttled(kind: str, cooldown_seconds: int) -> bool:
    now = time.monotonic()
    state_key = f"last_{kind}_alert_ts"
    return now - float(STATE.get(state_key) or 0) >= cooldown_seconds


def mark_throttled(kind: str) -> None:
    STATE[f"last_{kind}_alert_ts"] = time.monotonic()


async def confirm_miner_action(update: Update, action: str) -> None:
    if action not in {"stop", "restart"}:
        await reply_or_edit(update, "<b>🎮 Điều Khiển Miner</b>\nThao tác này không cần xác nhận.", control_menu())
        return
    data = await asyncio.to_thread(snapshot_safe)
    status = data.get("system", {})
    miner = data.get("pool_miner", {})
    titles = {
        "stop": "⏹ Xác nhận dừng miner",
        "restart": "🔄 Xác nhận restart miner",
    }
    note = "Lệnh này tác động trực tiếp tới service local."
    if action == "restart":
        note = "Miner sẽ ngắt phiên hiện tại rồi khởi động lại service local."
    text = (
        f"<b>{esc(titles.get(action, 'Xác nhận thao tác'))}</b>\n"
        f"Trạng thái: <b>{esc(status.get('status', 'N/A'))}</b> | "
        f"chạy thật: <b>{'có' if status.get('process_running') else 'không'}</b>\n"
        f"Tốc độ AlphaPool: <b>{esc(miner.get('hashrate_label', 'N/A'))}</b>\n"
        f"{source_line('AlphaPool', miner)}\n\n"
        f"{esc(note)}"
    )
    await reply_or_edit(update, text, confirm_miner_menu(action))


def menu_text() -> str:
    data = snapshot_safe()
    status = data.get("system", {})
    gpu = data.get("gpu", {})
    miner = data.get("pool_miner", {})
    safety = data.get("safety", {})
    return (
        "<b>⚒ Pearl Miner Manager</b>\n"
        f"Trạng thái: <b>{esc(status.get('status'))}</b> | chạy thật: <b>{'có' if status.get('process_running') else 'không'}</b>\n"
        f"Card: <code>{esc(gpu.get('gpu_name', 'N/A'))}</code>\n"
        f"{hashrate_lines(data)}\n"
        f"Nhiệt độ: <b>{fmt_num(gpu.get('temp_c'), 0)}°C</b> | Điện: <b>{fmt_num(gpu.get('power_w'), 1)}W</b>\n"
        f"Bảo vệ: <b>{esc(friendly_safety(safety.get('level', 'N/A')))}</b>\n"
        f"{source_line('AlphaPool', miner)}"
        f"{snapshot_issue_line(data)}"
    )


def help_text() -> str:
    config = cfg()
    temp_shutdown = config.get("TEMP_SHUTDOWN_C", "90")
    temp_warn = config.get("TEMP_WARN_C", "84")
    startup_profile = config.get("STARTUP_OC_PROFILE", "balance")
    miner_type = config.get("MINER_TYPE", "alpha")
    pool = f"{config.get('POOL_HOST', 'N/A')}:{config.get('POOL_PORT', 'N/A')}"
    return (
        "<b>❓ Trợ giúp Pearl Miner</b>\n"
        "Bot này dùng nút bấm là chính; bạn không cần nhớ nhiều lệnh.\n\n"
        "<b>Lệnh nhanh</b>\n"
        "/start - mở menu chính và ảnh tổng quan\n"
        "/help - xem hướng dẫn này\n\n"
        "<b>Các nút chính</b>\n"
        "📊 Thống kê: xem máy đang chạy không, tốc độ, nhiệt, điện và số AlphaPool đang thấy.\n"
        "💰 Số dư: xem PRL chờ trả, giá USD/VNĐ và số máy trên AlphaPool.\n"
        "📈 Xem biểu đồ: gửi ảnh tốc độ/nhiệt độ 24h gần đây.\n"
        "🎮 Điều khiển: bật, tắt, khởi động lại máy đào và đổi mức điện.\n"
        "⚙️ Cài đặt: xem pool, ngưỡng bảo vệ và các mức điện đang có.\n\n"
        "<b>Cách đọc số liệu</b>\n"
        "Trên máy = số đọc trực tiếp từ máy đào local.\n"
        "AlphaPool = số pool ghi nhận, có thể chậm hơn vài phút.\n"
        "Tốc độ chính = số hệ thống chọn để tính ước lượng, ưu tiên số đáng tin hơn.\n\n"
        "<b>An toàn hiện tại</b>\n"
        f"Miner: <code>{esc(miner_type)}</code>\n"
        f"Pool: <code>{esc(pool)}</code>\n"
        f"Mức điện khởi động: <code>{esc(startup_profile)}</code>\n"
        f"Cảnh báo/dừng nhiệt: <b>{esc(temp_warn)}/{esc(temp_shutdown)}°C</b>\n"
        "Các lệnh tắt hoặc khởi động lại đều có màn hình xác nhận trước."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        if update.message:
            await update.message.reply_text("Unauthorized chat.")
        return
    await reply_or_edit(update, await asyncio.to_thread(help_text), screen_menu("help", "stats"))


def render_overview_image() -> BytesIO | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        record_event("warning", "telegram", "Cannot render overview image", str(exc))
        return None

    data = snapshot_safe()
    status = data.get("system", {})
    gpu = data.get("gpu", {})
    effective = data.get("effective_hashrate", {})
    fig, ax = plt.subplots(figsize=(8.8, 4.4), dpi=140)
    fig.patch.set_facecolor("#0b1220")
    ax.set_facecolor("#0b1220")
    ax.axis("off")
    lines = [
        ("Pearl Miner Manager", 22, "#ffffff", 0.90),
        (f"Local: {status.get('status', 'N/A')} / process {'OK' if status.get('process_running') else 'N/A'}", 15, "#93c5fd", 0.72),
        (f"GPU: {gpu.get('gpu_name', 'N/A')}", 14, "#e2e8f0", 0.58),
        (f"Effective hashrate: {effective.get('hashrate_label', 'N/A')} ({effective.get('source', 'N/A')})", 18, "#86efac", 0.42),
        (f"Temp {fmt_num(gpu.get('temp_c'), 0)} C  |  Power {fmt_num(gpu.get('power_w'), 1)} W  |  VRAM {fmt_num(gpu.get('vram_gb'), 2)} GB", 13, "#cbd5e1", 0.27),
    ]
    for text, size, color, y in lines:
        ax.text(0.06, y, text, transform=ax.transAxes, fontsize=size, color=color, fontweight="bold")
    ax.text(0.06, 0.10, "AlphaPool realtime dashboard", transform=ax.transAxes, fontsize=11, color="#94a3b8")
    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buffer.seek(0)
    return buffer


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        if update.message:
            await update.message.reply_text("Unauthorized chat.")
        return
    if update.message:
        image = await asyncio.to_thread(render_overview_image)
        caption = await asyncio.to_thread(menu_text)
        if image is not None:
            try:
                await update.message.reply_photo(
                    photo=InputFile(image, filename="pearl-overview.png"),
                    caption=caption,
                    reply_markup=main_menu(),
                    parse_mode=ParseMode.HTML,
                )
                return
            except TelegramError as exc:
                record_event("warning", "telegram", "Cannot send overview image", str(exc))
    await reply_or_edit(update, await asyncio.to_thread(menu_text), main_menu())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except TelegramError as exc:
        record_event("warning", "telegram", "Cannot answer callback", str(exc))
    if not is_authorized(update):
        try:
            await query.answer("Unauthorized chat.", show_alert=True)
        except TelegramError:
            pass
        await reply_or_edit(update, "Unauthorized chat.")
        return

    data = query.data or "menu"
    try:
        if data == "menu":
            await reply_or_edit(update, await asyncio.to_thread(menu_text), main_menu())
        elif data == "help":
            await reply_or_edit(update, await asyncio.to_thread(help_text), screen_menu("help", "stats"))
        elif data == "stats":
            await show_stats(update)
        elif data == "balance":
            await show_balance(update)
        elif data == "predict":
            await show_prediction(update)
        elif data == "controls":
            await reply_or_edit(update, await asyncio.to_thread(control_text), control_menu())
        elif data == "settings":
            await show_settings(update)
        elif data == "chart":
            await send_chart(update, context)
        elif data == "oc_menu":
            await reply_or_edit(update, "<b>⚡ Mức điện card</b>\nChọn cấu hình cho RTX 3060.", oc_menu())
        elif data.startswith("miner_run:"):
            await run_miner_action(update, data.split(":", 1)[1])
        elif data.startswith("miner:"):
            action = data.split(":", 1)[1]
            if action in {"stop", "restart"}:
                await confirm_miner_action(update, action)
            else:
                await run_miner_action(update, action)
        elif data.startswith("oc:"):
            await run_oc_profile(update, data.split(":", 1)[1])
        else:
            await reply_or_edit(update, "<b>⚒ Pearl Miner Manager</b>\nNút này đã cũ hoặc không còn hợp lệ.", main_menu())
    except Exception as exc:
        record_event("error", "telegram", f"Callback failed: {data}", str(exc))
        await reply_or_edit(update, f"<b>⚠️ Telegram callback lỗi</b>\n<code>{esc(limit_text(exc, 500))}</code>", main_menu())


async def show_stats(update: Update) -> None:
    data = await asyncio.to_thread(snapshot_safe)
    gpu = data.get("gpu", {})
    status = data.get("system", {})
    miner = data.get("pool_miner", {})
    local = data.get("local_miner", {})
    safety = data.get("safety", {})
    text = (
        "<b>📊 Thống Kê Hiện Tại</b>\n"
        f"Trạng thái: <b>{esc(status.get('status'))}</b>\n"
        f"Chạy thật: <b>{'có' if status.get('process_running') else 'không'}</b>\n"
        f"Card: <code>{esc(gpu.get('gpu_name', 'N/A'))}</code>\n"
        f"{hashrate_lines(data)}\n"
        f"Tốc độ theo lượt gửi: <b>{esc(local.get('share_equiv_label', 'N/A'))}</b> | lượt gửi: <b>{esc(local.get('submitted_shares', 'N/A'))}</b>\n"
        f"Nhiệt độ: <b>{fmt_num(gpu.get('temp_c'), 0)}°C</b>\n"
        f"Điện năng: <b>{fmt_num(gpu.get('power_w'), 1)}W</b>\n"
        f"Quạt: <b>{fmt_num(gpu.get('fan_speed'), 0)}%</b>\n"
        f"VRAM: <b>{fmt_num(gpu.get('vram_gb'), 2)} / {fmt_num(gpu.get('vram_total_gb'), 1)} GB</b>\n"
        f"Điểm đào 24h: <b>{esc(miner.get('shares24h', 0))}</b>\n"
        f"Máy trên AlphaPool: <b>{esc(worker_summary(miner))}</b>\n"
        f"Bảo vệ: <b>{esc(friendly_safety(safety.get('level', 'N/A')))}</b>\n"
        f"{source_line('AlphaPool', miner)}"
        f"{snapshot_issue_line(data)}"
    )
    await reply_or_edit(update, text, screen_menu("stats", "controls"))


async def show_balance(update: Update) -> None:
    data = await asyncio.to_thread(snapshot_safe)
    miner = data.get("pool_miner", {})
    price = data.get("price", {})
    balance = float(miner.get("balance_prl") or 0.0)
    text = (
        "<b>💰 Số Dư AlphaPool</b>\n"
        f"Ví: <code>{esc(miner.get('wallet', 'N/A'))}</code>\n"
        f"PRL chờ trả: <b>{fmt_num(balance, 8)} PRL</b>\n"
        f"Đã trả: <b>{fmt_num(miner.get('total_paid_prl'), 8)} PRL</b>\n"
        f"Giá PRL: <b>${fmt_num(price.get('price_usd'), 4)}</b> | <b>{fmt_num(price.get('price_vnd'), 0)} VNĐ</b>\n"
        f"Quy đổi chờ trả: <b>${fmt_num(balance * float(price.get('price_usd') or 0.0), 2)}</b> | "
        f"<b>{fmt_num(balance * float(price.get('price_vnd') or 0.0), 0)} VNĐ</b>\n"
        f"{source_line('AlphaPool', miner)}\n"
        f"{source_line('Giá PRL', price, price.get('source', 'N/A'))}"
        f"{snapshot_issue_line(data)}"
    )
    await reply_or_edit(update, text, screen_menu("balance", "stats"))


async def show_prediction(update: Update) -> None:
    data = await asyncio.to_thread(snapshot_safe)
    pred = data.get("prediction", {})
    miner = data.get("pool_miner", {})
    local = data.get("local_miner", {})
    effective = data.get("effective_hashrate", {})
    pool = data.get("pool", {})
    price = data.get("price", {})
    text = (
        "<b>🧠 Dự Đoán & Phân Tích</b>\n"
        f"Ước tính 24h: <b>{fmt_num(pred.get('prl_24h'), 6)} PRL</b>\n"
        f"Ước tính 7 ngày: <b>{fmt_num(pred.get('prl_7d'), 6)} PRL</b>\n"
        f"USD 24h: <b>${fmt_num(pred.get('usd_24h'), 2)}</b>\n"
        f"VNĐ 24h: <b>{fmt_num(pred.get('vnd_24h'), 0)} VNĐ</b>\n"
        f"Tốc độ chính: <b>{esc(effective.get('hashrate_label', 'N/A'))}</b> (<code>{esc(friendly_source(effective.get('source', 'N/A')))}</code>)\n"
        f"Trên máy: <b>{esc(local.get('hashrate_label', 'N/A'))}</b> | AlphaPool: <b>{esc(miner.get('hashrate_label', 'N/A'))}</b>\n"
        f"Tổng tốc độ mạng: <b>{fmt_num(as_float(pool.get('network_hashrate_hps')) / 1e18, 2)} EH/s</b> | "
        f"PRL mỗi lần thưởng: <b>{fmt_num(pool.get('reward_prl'), 2)} PRL</b>\n"
        f"{source_line('AlphaPool', miner)}\n"
        f"{source_line('Mạng Pearl', pool)}\n"
        f"{source_line('Giá PRL', price, price.get('source', 'N/A'))}\n\n"
        f"<b>Đánh giá:</b> {esc(friendly_assessment(pred.get('assessment', 'N/A')))}"
        f"{snapshot_issue_line(data)}"
    )
    await reply_or_edit(update, text, screen_menu("predict", "stats"))


async def show_settings(update: Update) -> None:
    config = cfg()
    profiles = get_oc_profiles(config)
    data = await asyncio.to_thread(snapshot_safe)
    miner = data.get("pool_miner", {})
    price = data.get("price", {})
    safety = data.get("safety", {})
    zero_limit = max(2, get_int(config, "HASHRATE_ZERO_LIMIT", 2))
    text = (
        "<b>⚙️ Cài Đặt</b>\n"
        f"AlphaPool: <code>{esc(config.get('POOL_HOST'))}:{esc(config.get('POOL_PORT'))}</code>\n"
        f"Bộ quản lý: <code>{esc(config.get('MINER_SERVICE'))}</code>\n"
        f"Cảnh báo/dừng nhiệt: <b>{esc(config.get('TEMP_WARN_C', '84'))}/{esc(config.get('TEMP_SHUTDOWN_C'))}°C</b>\n"
        f"Tốc độ 0 từ AlphaPool: <b>{zero_limit} chu kỳ, chỉ cảnh báo</b>\n"
        f"Tốc độ 0 trên máy: <b>{'tự dừng nếu bật HASHRATE_ZERO_STOP=1' if get_int(config, 'HASHRATE_ZERO_STOP', 0) == 1 else 'chỉ cảnh báo'}</b>\n"
        f"Bảo vệ hiện tại: <b>{esc(friendly_safety(safety.get('level', 'N/A')))}</b>\n"
        f"Mức điện: <code>{', '.join(profiles.keys())}</code>\n"
        f"{source_line('AlphaPool hiện tại', miner)}\n"
        f"{source_line('Giá hiện tại', price, price.get('source', 'N/A'))}"
        f"{snapshot_issue_line(data)}"
    )
    await reply_or_edit(update, text, screen_menu("settings", "controls"))


async def send_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chart = await asyncio.to_thread(render_hardware_chart)
    if chart is None:
        await reply_or_edit(update, "<b>📈 Biểu đồ</b>\nChưa có dữ liệu 24h hoặc chưa cài matplotlib.", screen_menu("chart", "stats"))
        return
    if query and query.message:
        try:
            await query.message.reply_photo(photo=InputFile(chart, filename="pearl-hardware-24h.png"), caption="Pearl Miner - Tốc độ/Nhiệt độ 24h")
        except TelegramError as exc:
            record_event("warning", "telegram", "Cannot send hardware chart", str(exc))
            await reply_or_edit(update, f"<b>📈 Biểu đồ</b>\nKhông gửi được ảnh chart: <code>{esc(limit_text(exc, 300))}</code>", screen_menu("chart", "stats"))
            return
        await reply_or_edit(update, await asyncio.to_thread(menu_text), main_menu())


async def run_miner_action(update: Update, action: str) -> None:
    action = action.lower().strip()
    if action not in MINER_ACTIONS:
        await reply_or_edit(update, f"<b>🎮 Miner</b>\nThao tác không hợp lệ: <code>{esc(action)}</code>", control_menu())
        return
    result = await asyncio.to_thread(control_miner, action, quiet_bot_config())
    data = await asyncio.to_thread(snapshot_safe, False)
    status_data = data.get("system", {})
    gpu = data.get("gpu", {})
    effective = data.get("effective_hashrate", {})
    status = "OK" if result.get("ok") else "FAILED"
    text = (
        f"<b>🎮 {esc(action_label(action))}</b>\n"
        f"Kết quả: <b>{status}</b>\n"
        f"Trạng thái sau lệnh: <b>{esc(status_data.get('status', status_data.get('systemd_state', 'N/A')))}</b> | "
        f"chạy thật: <b>{'có' if status_data.get('process_running') else 'không'}</b>\n"
        f"Tốc độ chính: <b>{esc(effective.get('hashrate_label', 'N/A'))}</b> (<code>{esc(friendly_source(effective.get('source', 'N/A')))}</code>)\n"
        f"Card: <b>{fmt_num(gpu.get('temp_c'), 0)}°C</b> | <b>{fmt_num(gpu.get('power_w'), 1)}W</b>\n"
        f"Thông báo hệ thống: <code>{esc(command_output(result))}</code>"
        f"{snapshot_issue_line(data)}"
    )
    await reply_or_edit(update, text, control_menu())


async def run_oc_profile(update: Update, profile: str) -> None:
    result = await asyncio.to_thread(apply_oc_profile, profile)
    status = "OK" if result.get("ok") else "FAILED"
    p = result.get("profile") or {}
    text = (
        f"<b>⚡ Mức điện: {esc(profile)}</b>\n"
        f"Kết quả: <b>{status}</b>\n"
        f"Giới hạn điện: <b>{esc(p.get('power_limit', 'N/A'))}W</b>\n"
        f"Khóa xung nhân: <b>{esc(p.get('gpu_clock_min', 'N/A'))}-{esc(p.get('gpu_clock_max', 'N/A'))} MHz</b>\n"
        f"Bù xung nhân: <b>{esc(p.get('core_offset', 'N/A'))}</b>\n"
        f"Bù xung nhớ: <b>{esc(p.get('memory_offset', 'N/A'))}</b>\n"
        f"{esc(limit_text(result.get('error') or '; '.join(result.get('warnings') or []), 500))}"
    )
    await reply_or_edit(update, text, oc_menu())


async def watchdog_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = cfg()
    chat_id = allowed_chat_id()
    if not chat_id:
        return
    try:
        sample = await asyncio.to_thread(collect_and_store_sample, config)
        await asyncio.to_thread(record_reward_if_due, config)
    except Exception as exc:
        record_event("error", "watchdog", "Sampling failed", str(exc))
        return

    gpu = sample.get("gpu", {})
    status = sample.get("status", {})
    miner = sample.get("miner", {})
    local = sample.get("local_miner", {})
    effective = sample.get("effective_hashrate", {})
    safety = sample.get("safety", {})
    temp = float(gpu.get("temp_c") or 0.0)
    local_hashrate = float(local.get("hashrate_hps") or 0.0)
    effective_hashrate = float(effective.get("hashrate_hps") or 0.0)
    service_running = is_service_running(status)
    temp_limit = get_float(config, "TEMP_SHUTDOWN_C", 90)
    hot_limit_count = get_int(config, "HOT_LIMIT_COUNT", 3)
    zero_limit = max(2, get_int(config, "HASHRATE_ZERO_LIMIT", 2))
    zero_stop_enabled = get_int(config, "HASHRATE_ZERO_STOP", 0) == 1
    zero_alert_cooldown = max(300, get_int(config, "ZERO_HASH_ALERT_COOLDOWN_SECONDS", 1800))
    hot_alert_cooldown = max(300, get_int(config, "HOT_ALERT_COOLDOWN_SECONDS", 1800))
    crash_alert_cooldown = max(300, get_int(config, "CRASH_ALERT_COOLDOWN_SECONDS", 1800))

    if temp >= temp_limit:
        STATE["hot_count"] = int(STATE["hot_count"]) + 1
    else:
        STATE["hot_count"] = 0
        STATE["alerted_hot"] = False

    if int(STATE["hot_count"]) >= hot_limit_count and (not STATE["alerted_hot"] or can_send_throttled("hot", hot_alert_cooldown)):
        result = await asyncio.to_thread(control_miner, "stop", config)
        await send_alert(
            context,
            chat_id,
            (
                f"🔥 Watchdog nhiệt độ cao\n"
                f"GPU: {temp:.0f}°C >= {temp_limit:.0f}°C trong {hot_limit_count} chu kỳ.\n"
                f"Service: {status.get('systemd_state', 'unknown')} | process: {'OK' if status.get('process_running') else 'N/A'}\n"
                f"Lệnh stop: {'OK' if result.get('ok') else 'FAILED'}."
            ),
        )
        STATE["alerted_hot"] = True
        mark_throttled("hot")

    local_zero = service_running and as_bool(local.get("available")) and not as_bool(local.get("stale")) and local_hashrate <= 0
    pool_zero = service_running and as_bool(miner.get("available")) and not as_bool(miner.get("stale")) and float(miner.get("hashrate_hps") or 0.0) <= 0
    if local_zero or (pool_zero and effective_hashrate <= 0 and effective.get("source") != "local"):
        STATE["zero_hash_count"] = int(STATE["zero_hash_count"]) + 1
    elif effective_hashrate > 0:
        STATE["zero_hash_count"] = 0
        STATE["alerted_zero"] = False
    else:
        STATE["zero_hash_count"] = 0

    now = time.monotonic()
    can_alert_zero = now - float(STATE.get("last_zero_alert_ts") or 0) >= zero_alert_cooldown
    if int(STATE["zero_hash_count"]) >= zero_limit and (not STATE["alerted_zero"] or can_alert_zero):
        if zero_stop_enabled and local_zero:
            result = await asyncio.to_thread(control_miner, "stop", config)
            message = (
                f"⚠️ Watchdog local hashrate bằng 0\n"
                f"Local miner báo 0 H/s trong {zero_limit} chu kỳ.\n"
                f"Đã gửi stop theo HASHRATE_ZERO_STOP=1: {'OK' if result.get('ok') else 'FAILED'}."
            )
            record_event("warning", "watchdog", "Local hashrate is zero; miner stopped", str(local.get("last_status_line") or ""))
        else:
            message = (
                f"⚠️ Watchdog hashrate thấp\n"
                f"Effective: {effective.get('hashrate_label', '0 H/s')} ({effective.get('source', 'unknown')}).\n"
                "Miner vẫn được giữ chạy; pool API lỗi/stale chỉ tạo cảnh báo, không tự stop."
            )
            record_event("warning", "watchdog", "Zero hashrate observed; miner left running", str(safety.get("reasons") or miner.get("url") or ""))
        await send_alert(context, chat_id, message)
        STATE["alerted_zero"] = True
        STATE["last_zero_alert_ts"] = now

    if status.get("systemd_state") == "active" and not status.get("process_running"):
        STATE["crash_count"] = int(STATE["crash_count"]) + 1
    else:
        STATE["crash_count"] = 0
        STATE["alerted_crash"] = False

    if int(STATE["crash_count"]) >= 1 and (not STATE["alerted_crash"] or can_send_throttled("crash", crash_alert_cooldown)):
        result = await asyncio.to_thread(control_miner, "stop", config)
        await send_alert(
            context,
            chat_id,
            (
                "🚨 Watchdog phát hiện service active nhưng process miner không chạy.\n"
                f"Service: {status.get('systemd_state', 'unknown')} | PID: {status.get('pid') or 'N/A'}\n"
                f"Lệnh stop: {'OK' if result.get('ok') else 'FAILED'}."
            ),
        )
        STATE["alerted_crash"] = True
        mark_throttled("crash")


def build_application() -> Application:
    config = cfg()
    token = config.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN in config.env")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    chat_id = allowed_chat_id()
    if chat_id:
        app.job_queue.run_repeating(watchdog_task, interval=60, first=10, chat_id=chat_id)
    return app


def main() -> None:
    config = cfg()
    if not config.get("TELEGRAM_TOKEN"):
        print("Missing TELEGRAM_TOKEN in config.env")
        return
    if not allowed_chat_id():
        print("Missing TELEGRAM_CHAT_ID or CHAT_ID in config.env")
        return
    init_db()
    print("Starting Pearl Telegram Bot...")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
