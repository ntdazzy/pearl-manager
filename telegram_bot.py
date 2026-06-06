#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
from io import BytesIO
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

from config import get_float, get_int, get_oc_profiles, load_config
from database import init_db, record_event
from miner_services import (
    apply_oc_profile,
    collect_and_store_sample,
    control_miner,
    estimate_revenue,
    fetch_pool_miner_stats,
    fetch_price,
    get_gpu_metrics,
    get_miner_status,
    record_reward_if_due,
    render_hardware_chart,
)


STATE: dict[str, int | bool] = {
    "hot_count": 0,
    "zero_hash_count": 0,
    "crash_count": 0,
    "alerted_hot": False,
    "alerted_zero": False,
    "alerted_crash": False,
}


def cfg() -> dict[str, str]:
    return load_config()


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
        ]
    )


def control_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Bật Máy", callback_data="miner:start"),
                InlineKeyboardButton("⏹ Tắt Máy", callback_data="miner:stop"),
            ],
            [
                InlineKeyboardButton("🔄 Restart", callback_data="miner:restart"),
                InlineKeyboardButton("⚡ Chế Độ Ép Xung", callback_data="oc_menu"),
            ],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
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
    if update.callback_query:
        message = update.callback_query.message
        if message and (message.photo or message.caption is not None):
            await update.callback_query.edit_message_caption(
                caption=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    elif update.message:
        await update.message.reply_text(
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def send_alert(context: ContextTypes.DEFAULT_TYPE, chat_id: str, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        record_event("error", "telegram", "Cannot deliver watchdog alert", str(exc))


def menu_text() -> str:
    status = get_miner_status()
    gpu = get_gpu_metrics()
    miner = fetch_pool_miner_stats()
    return (
        "<b>⚒ Pearl Miner Manager</b>\n"
        f"Trạng thái: <b>{esc(status.get('status'))}</b>\n"
        f"GPU: <code>{esc(gpu.get('gpu_name', 'N/A'))}</code>\n"
        f"Hashrate AlphaPool: <b>{esc(miner.get('hashrate_label', 'N/A'))}</b>\n"
        f"Nhiệt độ: <b>{fmt_num(gpu.get('temp_c'), 0)}°C</b> | Điện: <b>{fmt_num(gpu.get('power_w'), 1)}W</b>"
    )


def render_overview_image() -> BytesIO | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        record_event("warning", "telegram", "Cannot render overview image", str(exc))
        return None

    status = get_miner_status()
    gpu = get_gpu_metrics()
    miner = fetch_pool_miner_stats()
    fig, ax = plt.subplots(figsize=(8.8, 4.4), dpi=140)
    fig.patch.set_facecolor("#0b1220")
    ax.set_facecolor("#0b1220")
    ax.axis("off")
    lines = [
        ("Pearl Miner Manager", 22, "#ffffff", 0.90),
        (f"Service: {status.get('status', 'N/A')}", 15, "#93c5fd", 0.72),
        (f"GPU: {gpu.get('gpu_name', 'N/A')}", 14, "#e2e8f0", 0.58),
        (f"Hashrate: {miner.get('hashrate_label', 'N/A')}", 18, "#86efac", 0.42),
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
            await update.message.reply_photo(
                photo=InputFile(image, filename="pearl-overview.png"),
                caption=caption,
                reply_markup=main_menu(),
                parse_mode=ParseMode.HTML,
            )
            return
    await reply_or_edit(update, await asyncio.to_thread(menu_text), main_menu())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_authorized(update):
        await query.edit_message_text("Unauthorized chat.")
        return

    data = query.data or "menu"
    if data == "menu":
        await reply_or_edit(update, await asyncio.to_thread(menu_text), main_menu())
    elif data == "stats":
        await show_stats(update)
    elif data == "balance":
        await show_balance(update)
    elif data == "predict":
        await show_prediction(update)
    elif data == "controls":
        await reply_or_edit(update, "<b>🎮 Điều Khiển Miner</b>\nChọn hành động cần chạy.", control_menu())
    elif data == "settings":
        await show_settings(update)
    elif data == "chart":
        await send_chart(update, context)
    elif data == "oc_menu":
        await reply_or_edit(update, "<b>⚡ Chế Độ Ép Xung</b>\nChọn profile cho RTX 3060.", oc_menu())
    elif data.startswith("miner:"):
        await run_miner_action(update, data.split(":", 1)[1])
    elif data.startswith("oc:"):
        await run_oc_profile(update, data.split(":", 1)[1])


async def show_stats(update: Update) -> None:
    gpu, status, miner = await asyncio.gather(
        asyncio.to_thread(get_gpu_metrics),
        asyncio.to_thread(get_miner_status),
        asyncio.to_thread(fetch_pool_miner_stats),
    )
    text = (
        "<b>📊 Thống Kê Hiện Tại</b>\n"
        f"Trạng thái: <b>{esc(status.get('status'))}</b>\n"
        f"GPU: <code>{esc(gpu.get('gpu_name', 'N/A'))}</code>\n"
        f"Hashrate: <b>{esc(miner.get('hashrate_label', 'N/A'))}</b>\n"
        f"Nhiệt độ: <b>{fmt_num(gpu.get('temp_c'), 0)}°C</b>\n"
        f"Điện năng: <b>{fmt_num(gpu.get('power_w'), 1)}W</b>\n"
        f"Quạt: <b>{fmt_num(gpu.get('fan_speed'), 0)}%</b>\n"
        f"VRAM: <b>{fmt_num(gpu.get('vram_gb'), 2)} / {fmt_num(gpu.get('vram_total_gb'), 1)} GB</b>\n"
        f"Shares 24h: <b>{esc(miner.get('shares24h', 0))}</b>"
    )
    await reply_or_edit(update, text, main_menu())


async def show_balance(update: Update) -> None:
    miner, price = await asyncio.gather(
        asyncio.to_thread(fetch_pool_miner_stats),
        asyncio.to_thread(fetch_price),
    )
    balance = float(miner.get("balance_prl") or 0.0)
    text = (
        "<b>💰 Số Dư AlphaPool</b>\n"
        f"Wallet: <code>{esc(miner.get('wallet', 'N/A'))}</code>\n"
        f"Pending: <b>{fmt_num(balance, 8)} PRL</b>\n"
        f"Đã trả: <b>{fmt_num(miner.get('total_paid_prl'), 8)} PRL</b>\n"
        f"Giá PRL: <b>${fmt_num(price.get('price_usd'), 4)}</b> | <b>{fmt_num(price.get('price_vnd'), 0)} VNĐ</b>\n"
        f"Quy đổi pending: <b>${fmt_num(balance * float(price.get('price_usd') or 0.0), 2)}</b> | "
        f"<b>{fmt_num(balance * float(price.get('price_vnd') or 0.0), 0)} VNĐ</b>\n"
        f"Nguồn giá: <code>{esc(price.get('source', 'N/A'))}</code>"
    )
    await reply_or_edit(update, text, main_menu())


async def show_prediction(update: Update) -> None:
    pred = await asyncio.to_thread(estimate_revenue)
    text = (
        "<b>🧠 Dự Đoán & Phân Tích</b>\n"
        f"Ước tính 24h: <b>{fmt_num(pred.get('prl_24h'), 6)} PRL</b>\n"
        f"Ước tính 7 ngày: <b>{fmt_num(pred.get('prl_7d'), 6)} PRL</b>\n"
        f"USD 24h: <b>${fmt_num(pred.get('usd_24h'), 2)}</b>\n"
        f"VNĐ 24h: <b>{fmt_num(pred.get('vnd_24h'), 0)} VNĐ</b>\n\n"
        f"<b>Đánh giá:</b> {esc(pred.get('assessment', 'N/A'))}"
    )
    await reply_or_edit(update, text, main_menu())


async def show_settings(update: Update) -> None:
    config = cfg()
    profiles = get_oc_profiles(config)
    text = (
        "<b>⚙️ Cài Đặt</b>\n"
        f"Pool: <code>{esc(config.get('POOL_HOST'))}:{esc(config.get('POOL_PORT'))}</code>\n"
        f"API: <code>{esc(config.get('POOL_API_URL'))}</code>\n"
        f"Service: <code>{esc(config.get('MINER_SERVICE'))}</code>\n"
        f"Watchdog temp: <b>{esc(config.get('TEMP_SHUTDOWN_C'))}°C</b>\n"
        f"OC profiles: <code>{', '.join(profiles.keys())}</code>"
    )
    await reply_or_edit(update, text, main_menu())


async def send_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chart = await asyncio.to_thread(render_hardware_chart)
    if chart is None:
        await reply_or_edit(update, "<b>📈 Biểu đồ</b>\nChưa có dữ liệu 24h hoặc chưa cài matplotlib.", main_menu())
        return
    if query and query.message:
        await query.message.reply_photo(photo=InputFile(chart, filename="pearl-hardware-24h.png"), caption="Pearl Miner - Hashrate/Nhiệt độ 24h")
        await reply_or_edit(update, await asyncio.to_thread(menu_text), main_menu())


async def run_miner_action(update: Update, action: str) -> None:
    result = await asyncio.to_thread(control_miner, action)
    status = "OK" if result.get("ok") else "FAILED"
    text = (
        f"<b>🎮 Miner {esc(action)}</b>\n"
        f"Kết quả: <b>{status}</b>\n"
        f"<code>{esc(result.get('stderr') or result.get('stdout') or '')}</code>"
    )
    await reply_or_edit(update, text, control_menu())


async def run_oc_profile(update: Update, profile: str) -> None:
    result = await asyncio.to_thread(apply_oc_profile, profile)
    status = "OK" if result.get("ok") else "FAILED"
    p = result.get("profile") or {}
    text = (
        f"<b>⚡ OC Profile: {esc(profile)}</b>\n"
        f"Kết quả: <b>{status}</b>\n"
        f"Power: <b>{esc(p.get('power_limit', 'N/A'))}W</b>\n"
        f"Lock core: <b>{esc(p.get('gpu_clock_min', 'N/A'))}-{esc(p.get('gpu_clock_max', 'N/A'))} MHz</b>\n"
        f"Core offset: <b>{esc(p.get('core_offset', 'N/A'))}</b>\n"
        f"Memory offset: <b>{esc(p.get('memory_offset', 'N/A'))}</b>\n"
        f"{esc(result.get('error', ''))}"
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
    temp = float(gpu.get("temp_c") or 0.0)
    hashrate = float(miner.get("hashrate_hps") or 0.0)
    temp_limit = get_float(config, "TEMP_SHUTDOWN_C", 80)
    hot_limit_count = get_int(config, "HOT_LIMIT_COUNT", 3)
    zero_limit = get_int(config, "HASHRATE_ZERO_LIMIT", 2)

    if temp >= temp_limit:
        STATE["hot_count"] = int(STATE["hot_count"]) + 1
    else:
        STATE["hot_count"] = 0
        STATE["alerted_hot"] = False

    if int(STATE["hot_count"]) >= hot_limit_count and not STATE["alerted_hot"]:
        await asyncio.to_thread(control_miner, "stop", config)
        await send_alert(
            context,
            chat_id,
            f"🔥 BÁO ĐỘNG: GPU {temp:.0f}°C >= {temp_limit:.0f}°C trong {hot_limit_count} chu kỳ. Đã gửi lệnh dừng miner để bảo vệ máy.",
        )
        STATE["alerted_hot"] = True

    if status.get("is_active") and miner.get("available") and hashrate <= 0:
        STATE["zero_hash_count"] = int(STATE["zero_hash_count"]) + 1
    else:
        STATE["zero_hash_count"] = 0
        STATE["alerted_zero"] = False

    if int(STATE["zero_hash_count"]) >= zero_limit and not STATE["alerted_zero"]:
        await asyncio.to_thread(control_miner, "stop", config)
        await send_alert(
            context,
            chat_id,
            "⚠️ Hashrate từ AlphaPool đang bằng 0 trong nhiều chu kỳ. Đã gửi lệnh dừng miner để kiểm tra an toàn.",
        )
        STATE["alerted_zero"] = True

    if status.get("systemd_state") == "active" and not status.get("process_running"):
        STATE["crash_count"] = int(STATE["crash_count"]) + 1
    else:
        STATE["crash_count"] = 0
        STATE["alerted_crash"] = False

    if int(STATE["crash_count"]) >= 1 and not STATE["alerted_crash"]:
        await asyncio.to_thread(control_miner, "stop", config)
        await send_alert(context, chat_id, "🚨 Miner service active nhưng process miner không còn chạy. Đã gửi lệnh dừng miner để tránh vòng lỗi.")
        STATE["alerted_crash"] = True


def build_application() -> Application:
    config = cfg()
    token = config.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN in config.env")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_cmd))
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
