from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from database import SessionLocal, MiningStat, DailyReward
import subprocess
import asyncio

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/stats")
def get_stats():
    db = SessionLocal()
    # Lấy bản ghi mới nhất
    latest = db.query(MiningStat).order_by(MiningStat.id.desc()).first()
    db.close()
    
    # Kiểm tra trạng thái tiến trình
    try:
        res = subprocess.run("pgrep -f SRBMiner-MULTI", shell=True, capture_output=True, text=True)
        is_running = bool(res.stdout.strip())
    except:
        is_running = False

    return {
        "status": "Đang chạy" if is_running else "Đã dừng",
        "temp": latest.temp if latest else 0,
        "power": latest.power if latest else 0,
        "hashrate": 13.8 if is_running else 0, # Mock hashrate
        "vram": latest.vram_used if latest else 0
    }

@app.get("/api/chart_data")
def get_chart_data():
    # Giả lập dữ liệu cho biểu đồ đường và cột
    return {
        "labels": ["T2", "T3", "T4", "T5", "T6", "T7", "CN"],
        "daily": [3.1, 3.5, 3.7, 3.8, 4.0, 3.7, 3.9],
        "cumulative": [3.1, 6.6, 10.3, 14.1, 18.1, 21.8, 25.7]
    }

@app.post("/api/control/{action}")
def control_miner(action: str):
    if action == "start":
        subprocess.run("sudo systemctl start pearl-miner.service", shell=True)
    elif action == "stop":
        subprocess.run("sudo systemctl stop pearl-miner.service", shell=True)
    elif action == "oc":
        subprocess.run("sudo nvidia-smi -pm 1", shell=True)
        subprocess.run("sudo nvidia-smi --power-limit=115", shell=True)
        subprocess.run("sudo nvidia-smi --lock-gpu-clocks=1450,1450", shell=True)
        subprocess.run("DISPLAY=:0 nvidia-settings -a '[gpu:0]/GPUGraphicsClockOffset=200'", shell=True)
        subprocess.run("DISPLAY=:0 nvidia-settings -a '[gpu:0]/GPUMemoryTransferRateOffset=1000'", shell=True)
    return {"status": "ok"}

async def log_generator():
    # Đọc log trực tiếp từ journalctl
    process = await asyncio.create_subprocess_shell(
        "journalctl -u pearl-miner.service -f -n 20",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            yield line.decode('utf-8')
    finally:
        process.terminate()

@app.get("/api/logs")
async def stream_logs():
    return StreamingResponse(log_generator(), media_type="text/plain")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
