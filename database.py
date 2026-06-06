from sqlalchemy import create_engine, Column, Integer, Float, DateTime, Date, String
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os

# Đọc cấu hình DB từ config.env
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.env")
CONFIG = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                key, val = line.strip().split("=", 1)
                CONFIG[key] = val

DB_USER = CONFIG.get("DB_USER", "postgres")
DB_PASS = CONFIG.get("DB_PASS", "postgres")
DB_NAME = CONFIG.get("DB_NAME", "pearl_db")
DB_HOST = CONFIG.get("DB_HOST", "localhost")
DB_PORT = CONFIG.get("DB_PORT", "5432")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class MiningStat(Base):
    __tablename__ = "mining_stats"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    temp = Column(Float)
    power = Column(Float)
    fan = Column(Float)
    hashrate = Column(Float)  # TH/s
    vram_used = Column(Float) # GB

class DailyReward(Base):
    __tablename__ = "daily_rewards"
    date = Column(Date, primary_key=True, index=True)
    total_mined = Column(Float)

# Tự động tạo bảng nếu chưa có
def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        print("Đã khởi tạo Database thành công!")
    except Exception as e:
        print("Lỗi khởi tạo Database:", e)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
