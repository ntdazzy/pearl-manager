from __future__ import annotations

import asyncio
import ipaddress
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import BASE_DIR, get_float, get_oc_profiles, load_config
from database import init_db, record_event
from database import SessionLocal
from miner_services import (
    apply_oc_profile,
    collect_and_store_sample,
    collect_telemetry_snapshot,
    control_miner,
    estimate_revenue,
    fetch_pool_miner_stats,
    fetch_pool_summary,
    fetch_price,
    format_hashrate_hps,
    get_chart_data,
    get_gpu_metrics,
    get_miner_status,
    record_journal_snapshot,
    record_reward_if_due,
    stream_journal_lines,
    today_reward_prl,
)
from models import SystemEvent


class ProfileRequest(BaseModel):
    profile: str


background_tasks: set[asyncio.Task[Any]] = set()
STARTUP_ERRORS: list[str] = []


def _is_local_client(request: Request) -> bool:
    # Cloudflare Tunnel reaches the app from localhost, but the request is
    # internet-originated. Treat Cloudflare-marked requests as remote so the
    # dashboard/control token policy still applies.
    if any(request.headers.get(name) for name in ("cf-ray", "cf-connecting-ip", "cf-access-jwt-assertion")):
        return False
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return (
        request.headers.get("x-pearl-control-token")
        or request.headers.get("x-pearl-token")
        or request.query_params.get("token")
        or ""
    ).strip()


def require_control_access(request: Request) -> None:
    token = load_config().get("CONTROL_API_TOKEN", "").strip()
    supplied = _request_token(request)
    if token:
        if supplied == token:
            return
        raise HTTPException(status_code=401, detail={"ok": False, "error": "control_auth_required"})
    if _is_local_client(request):
        return
    raise HTTPException(
        status_code=403,
        detail={"ok": False, "error": "CONTROL_API_TOKEN is required for non-local control requests"},
    )


def require_dashboard_access(request: Request) -> None:
    token = load_config().get("CONTROL_API_TOKEN", "").strip()
    supplied = _request_token(request)
    if token:
        if supplied == token:
            return
        if _is_local_client(request):
            return
        raise HTTPException(status_code=401, detail={"ok": False, "error": "dashboard_auth_required"})
    if _is_local_client(request):
        return
    raise HTTPException(
        status_code=403,
        detail={"ok": False, "error": "CONTROL_API_TOKEN is required for non-local dashboard requests"},
    )


async def metrics_worker() -> None:
    while True:
        try:
            cfg = load_config()
            await asyncio.to_thread(collect_and_store_sample, cfg)
            await asyncio.to_thread(record_reward_if_due, cfg)
            await asyncio.to_thread(record_journal_snapshot, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_event("error", "worker", "Background metrics worker failed", str(exc))
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await asyncio.to_thread(init_db)
    except Exception as exc:
        STARTUP_ERRORS.append(str(exc))
        record_event("error", "startup", "Database initialization failed", str(exc))
    task = asyncio.create_task(metrics_worker())
    background_tasks.add(task)
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        background_tasks.discard(task)


app = FastAPI(title="Pearl Miner Manager", version="1.0.0", lifespan=lifespan)

static_dir = BASE_DIR / "static"
templates_dir = BASE_DIR / "templates"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(templates_dir))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    record_event("error", "api", f"Unhandled error on {request.url.path}", str(exc))
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_server_error"},
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    require_dashboard_access(request)
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/favicon.ico")
def favicon() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#2563eb"/>'
        '<text x="32" y="42" text-anchor="middle" font-family="Arial,sans-serif" '
        'font-size="34" font-weight="700" fill="white">P</text>'
        "</svg>"
    )
    return Response(svg, media_type="image/svg+xml")


def _finance_payload(miner: dict[str, Any] | None = None, price: dict[str, Any] | None = None) -> dict[str, Any]:
    miner = miner if miner is not None else fetch_pool_miner_stats()
    price = price if price is not None else fetch_price()
    balance = float(miner.get("balance_prl") or 0.0)
    total_paid = float(miner.get("total_paid_prl") or 0.0)
    return {
        "available": bool(miner.get("available")),
        "wallet": miner.get("wallet", ""),
        "balance_prl": balance,
        "total_paid_prl": total_paid,
        "balance_usd": balance * float(price.get("price_usd") or 0.0),
        "balance_vnd": balance * float(price.get("price_vnd") or 0.0),
        "price": price,
        "shares24h": miner.get("shares24h", 0),
        "workers": miner.get("workers", []),
        "hashrate_label": miner.get("hashrate_label", "N/A"),
        "mode": miner.get("mode", "N/A"),
        "source_url": miner.get("url", ""),
    }


def _safe_price(price: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(price, dict):
        return {}
    return {key: value for key, value in price.items() if key != "raw"}


def _safe_dict(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key != "raw"}


def _sanitized_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    clean = dict(snapshot)
    clean["pool_miner"] = _safe_dict(snapshot.get("pool_miner"))
    clean["pool"] = _safe_dict(snapshot.get("pool"))
    clean["price"] = _safe_price(snapshot.get("price", {}))
    clean["finance"] = dict(snapshot.get("finance") or {})
    clean["gpu"] = dict(snapshot.get("gpu") or {})
    clean["gpu"]["coin_today_prl"] = today_reward_prl()
    clean["coin_today_prl"] = clean["gpu"]["coin_today_prl"]
    if isinstance(clean["finance"].get("price"), dict):
        clean["finance"]["price"] = _safe_price(clean["finance"]["price"])
    return clean


def _recent_events(limit: int = 30) -> list[dict[str, Any]]:
    safe_limit = min(max(int(limit or 30), 1), 100)
    try:
        with SessionLocal() as db:
            rows = db.query(SystemEvent).order_by(SystemEvent.timestamp.desc(), SystemEvent.id.desc()).limit(safe_limit).all()
    except Exception as exc:
        record_event("warning", "api", "Cannot read system events", str(exc))
        return []
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp.isoformat() if row.timestamp else "",
            "level": row.level,
            "category": row.category,
            "message": row.message,
            "details": row.details,
        }
        for row in rows
    ]


def _system_payload(status: dict[str, Any] | None = None, gpu: dict[str, Any] | None = None) -> dict[str, Any]:
    status = status if status is not None else get_miner_status()
    gpu = gpu if gpu is not None else get_gpu_metrics()
    return {
        **status,
        "gpu_name": gpu.get("gpu_name", "N/A"),
        "uptime": status.get("details", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _gpu_payload(
    gpu: dict[str, Any] | None = None,
    miner: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
    prediction: dict[str, Any] | None = None,
    finance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gpu = gpu if gpu is not None else get_gpu_metrics()
    miner = miner if miner is not None else fetch_pool_miner_stats()
    status = status if status is not None else get_miner_status()
    prediction = prediction if prediction is not None else estimate_revenue()
    service_running = bool(status.get("is_active") and status.get("process_running"))
    hashrate_hps = float(miner.get("hashrate_hps") or 0.0) if service_running else 0.0
    hashrate_th = hashrate_hps / 1e12
    return {
        **gpu,
        "status": status.get("status"),
        "is_active": status.get("is_active"),
        "process_running": status.get("process_running"),
        "hashrate_th": round(hashrate_th, 4),
        "hashrate_label": format_hashrate_hps(hashrate_hps) if hashrate_hps > 0 else "0 H/s",
        "coin_today_prl": today_reward_prl(),
        "finance": finance if finance is not None else _finance_payload(),
        "prediction": {
            "prl_24h": prediction.get("prl_24h", 0.0),
            "prl_7d": prediction.get("prl_7d", 0.0),
            "usd_24h": prediction.get("usd_24h", 0.0),
            "vnd_24h": prediction.get("vnd_24h", 0.0),
            "assessment": prediction.get("assessment", "N/A"),
        },
    }


def _compact_live_pool(pool: dict[str, Any]) -> dict[str, Any]:
    pool_hps = float(pool.get("pool_hashrate_hps") or 0.0)
    network_hps = float(pool.get("network_hashrate_hps") or 0.0)
    return {
        "available": bool(pool.get("available")),
        "fee_percent": pool.get("fee_percent", 0.0),
        "reward_prl": pool.get("reward_prl", 0.0),
        "pool_hashrate_hps": pool_hps,
        "pool_hashrate_label": format_hashrate_hps(pool_hps) if pool_hps > 0 else "N/A",
        "network_hashrate_hps": network_hps,
        "network_hashrate_label": format_hashrate_hps(network_hps) if network_hps > 0 else "N/A",
        "block_time_seconds": pool.get("block_time_seconds", 0.0),
        "miners24h": pool.get("miners24h", 0),
        "workers": pool.get("workers", 0),
        "blocks24h": pool.get("blocks24h", 0),
        "height": pool.get("height", 0),
        "stratum": pool.get("stratum", {}),
    }


def _live_payload() -> dict[str, Any]:
    snapshot = collect_telemetry_snapshot()
    gpu_metrics = snapshot.get("gpu", {})
    status = snapshot.get("system", {})
    effective = snapshot.get("effective_hashrate", {})
    prediction = snapshot.get("prediction", {})
    finance = dict(snapshot.get("finance", {}))
    if isinstance(finance.get("price"), dict):
        finance["price"] = _safe_price(finance["price"])
    finance["prediction"] = prediction
    finance["pool"] = _compact_live_pool(snapshot.get("pool", {}))
    finance["local_miner"] = snapshot.get("local_miner", {})
    finance["effective_hashrate"] = snapshot.get("effective_hashrate", {})
    finance["pool_miner"] = {key: value for key, value in snapshot.get("pool_miner", {}).items() if key != "raw"}
    gpu = {
        **gpu_metrics,
        "status": status.get("status"),
        "is_active": status.get("is_active"),
        "process_running": status.get("process_running"),
        "hashrate_th": round(float(effective.get("hashrate_th") or 0.0), 4),
        "hashrate_label": effective.get("hashrate_label", "0 H/s"),
        "hashrate_source": effective.get("source", "unknown"),
        "hashrate_stale": bool(effective.get("stale")),
        "coin_today_prl": today_reward_prl(),
        "prediction": prediction,
        "safety": snapshot.get("safety", {}),
        "local_miner": snapshot.get("local_miner", {}),
    }
    return {
        "system": _system_payload(status, gpu_metrics),
        "gpu": gpu,
        "finance": finance,
        "safety": snapshot.get("safety", {}),
        "timestamp": snapshot.get("timestamp") or datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    db_ok = True
    try:
        from sqlalchemy import text

        from database import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        record_event("warning", "health", "Database health check failed", str(exc))
    return {
        "ok": db_ok and not STARTUP_ERRORS,
        "database": db_ok,
        "startup_errors": STARTUP_ERRORS[-3:],
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/system/status")
def api_system_status(request: Request) -> dict[str, Any]:
    require_dashboard_access(request)
    payload = _live_payload()
    return payload["system"]


@app.get("/api/gpu/metrics")
def api_gpu_metrics(request: Request) -> dict[str, Any]:
    require_dashboard_access(request)
    payload = _live_payload()
    return payload["gpu"]


@app.get("/api/mining/finance")
def api_mining_finance(request: Request) -> dict[str, Any]:
    require_dashboard_access(request)
    payload = _live_payload()
    return payload["finance"]


@app.get("/api/chart_data")
def api_chart_data(request: Request) -> dict[str, Any]:
    require_dashboard_access(request)
    return get_chart_data()


@app.get("/api/stats")
def api_stats_legacy(request: Request) -> dict[str, Any]:
    require_dashboard_access(request)
    gpu = _live_payload()["gpu"]
    return {
        "status": gpu.get("status", "N/A"),
        "temp": gpu.get("temp_c", 0.0),
        "power": gpu.get("power_w", 0.0),
        "fan": gpu.get("fan_speed", 0.0),
        "hashrate": gpu.get("hashrate_th", 0.0),
        "hashrate_label": gpu.get("hashrate_label", "N/A"),
        "vram": gpu.get("vram_gb", 0.0),
        "coin_today_prl": gpu.get("coin_today_prl", 0.0),
        "prediction": gpu.get("prediction", {}),
        "finance": gpu.get("finance", {}),
    }


@app.get("/api/live")
async def api_live(request: Request) -> StreamingResponse:
    require_dashboard_access(request)

    async def event_generator():
        retry_ms = int(max(1.0, get_float(load_config(), "LIVE_UPDATE_SECONDS", 2.0)) * 1000)
        yield f"retry: {retry_ms}\n\n"
        if await request.is_disconnected():
            return
        try:
            payload = await asyncio.to_thread(_live_payload)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_event("warning", "api", "Live metrics stream failed", str(exc))
            error_payload = {
                "ok": False,
                "error": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/admin/snapshot")
def api_admin_snapshot(request: Request) -> dict[str, Any]:
    require_dashboard_access(request)
    return _sanitized_snapshot(collect_telemetry_snapshot())


@app.get("/api/admin/events")
def api_admin_events(request: Request, limit: int = 30) -> dict[str, Any]:
    require_dashboard_access(request)
    return {"events": _recent_events(limit)}


@app.post("/api/control/{action}")
def api_control(action: str, request: Request) -> dict[str, Any]:
    require_control_access(request)
    result = control_miner(action)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/gpu/profile")
def api_gpu_profile(payload: ProfileRequest, request: Request) -> dict[str, Any]:
    require_control_access(request)
    result = apply_oc_profile(payload.profile)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.get("/api/gpu/profiles")
def api_gpu_profiles(request: Request) -> dict[str, Any]:
    require_dashboard_access(request)
    return {"profiles": get_oc_profiles()}


@app.get("/api/logs/stream")
async def api_logs_stream(request: Request) -> StreamingResponse:
    require_dashboard_access(request)
    cfg = load_config()
    service = cfg.get("MINER_SERVICE", "pearl-miner.service")
    reconnect_ms = int(max(1.0, get_float(cfg, "LIVE_UPDATE_SECONDS", 2.0)) * 1000)

    async def event_generator():
        yield f"retry: {reconnect_ms}\n\n"
        async for line in stream_journal_lines(
            service,
            stop_check=request.is_disconnected,
            max_seconds=4.0,
            max_lines=80,
        ):
            yield f"data: {json.dumps({'line': line, 'service': service})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/logs")
async def api_logs_plain(request: Request) -> StreamingResponse:
    require_dashboard_access(request)
    cfg = load_config()
    service = cfg.get("MINER_SERVICE", "pearl-miner.service")

    async def text_generator():
        async for line in stream_journal_lines(service, stop_check=request.is_disconnected):
            yield f"{line}\n"

    return StreamingResponse(text_generator(), media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "app:app",
        host=cfg.get("WEB_HOST", "127.0.0.1"),
        port=int(cfg.get("WEB_PORT", "8555")),
        reload=False,
    )
