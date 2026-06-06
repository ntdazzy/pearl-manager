from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base


Base = declarative_base()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HardwareLog(Base):
    __tablename__ = "hardware_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    temp_c = Column(Float, default=0.0, nullable=False)
    power_w = Column(Float, default=0.0, nullable=False)
    fan_speed = Column(Float, default=0.0, nullable=False)
    hashrate_th = Column(Float, default=0.0, nullable=False)
    vram_gb = Column(Float, default=0.0, nullable=False)
    gpu_name = Column(String(128), default="", nullable=False)


class MiningReward(Base):
    __tablename__ = "mining_rewards"
    __table_args__ = (UniqueConstraint("timestamp", name="uq_mining_rewards_timestamp"),)

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    pearl_mined_hour = Column(Float, default=0.0, nullable=False)
    source = Column(String(64), default="alphapool", nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(96), unique=True, nullable=True, index=True)
    value = Column(Text, default="", nullable=False)
    wallet_address = Column(String(256), default="", nullable=False)
    pool_url = Column(String(256), default="", nullable=False)
    telegram_chat_id = Column(String(128), default="", nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    level = Column(String(24), default="info", nullable=False)
    category = Column(String(64), default="system", nullable=False)
    message = Column(Text, nullable=False)
    details = Column(Text, default="", nullable=False)


Index("ix_hardware_logs_timestamp_id", HardwareLog.timestamp, HardwareLog.id)
Index("ix_mining_rewards_timestamp_id", MiningReward.timestamp, MiningReward.id)
