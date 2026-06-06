from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from config import base_wallet_address, load_config
from models import Base, HardwareLog, MiningReward, Setting, SystemEvent


def build_database_url() -> str | URL:
    cfg = load_config()
    if cfg.get("DATABASE_URL"):
        return cfg["DATABASE_URL"]
    return URL.create(
        "postgresql+psycopg2",
        username=cfg.get("DB_USER") or None,
        password=cfg.get("DB_PASS") or None,
        host=cfg.get("DB_HOST") or "localhost",
        port=int(cfg.get("DB_PORT") or 5432),
        database=cfg.get("DB_NAME") or "pearl_db",
    )


DATABASE_URL = build_database_url()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_runtime_columns()
    upsert_default_settings()


def _add_missing_columns(table: str, additions: dict[str, str]) -> None:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table)}
    with engine.begin() as conn:
        for column, ddl in additions.items():
            if column not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def ensure_runtime_columns() -> None:
    _add_missing_columns(
        "settings",
        {
            "key": "VARCHAR(96)",
            "value": "TEXT DEFAULT '' NOT NULL",
            "wallet_address": "VARCHAR(256) DEFAULT '' NOT NULL",
            "pool_url": "VARCHAR(256) DEFAULT '' NOT NULL",
            "telegram_chat_id": "VARCHAR(128) DEFAULT '' NOT NULL",
            "updated_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL",
        },
    )
    _add_missing_columns("hardware_logs", {"gpu_name": "VARCHAR(128) DEFAULT '' NOT NULL"})
    _add_missing_columns("mining_rewards", {"source": "VARCHAR(64) DEFAULT 'alphapool' NOT NULL"})
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM mining_rewards a
                USING mining_rewards b
                WHERE a.timestamp = b.timestamp
                  AND a.id > b.id
                """
            )
        )
        conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'uq_mining_rewards_timestamp'
                    ) THEN
                        ALTER TABLE mining_rewards
                        ADD CONSTRAINT uq_mining_rewards_timestamp UNIQUE (timestamp);
                    END IF;
                END $$;
                """
            )
        )


# Backward-compatible name for older local imports.
ensure_settings_columns = ensure_runtime_columns


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_setting(key: str, default: str = "") -> str:
    with SessionLocal() as db:
        config_row = db.query(Setting).filter(Setting.key == "default_config").first()
        if config_row and key in {"wallet_address", "pool_url", "telegram_chat_id"}:
            return getattr(config_row, key) or default
        row = db.query(Setting).filter(Setting.key == key).first()
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with session_scope() as db:
        if key in {"wallet_address", "pool_url", "telegram_chat_id"}:
            row = db.query(Setting).filter(Setting.key == "default_config").first()
            if row is None:
                row = Setting(key="default_config", value="")
                db.add(row)
            setattr(row, key, value)
            return
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))


def upsert_default_settings() -> None:
    cfg = load_config()
    defaults = {
        "wallet_address": base_wallet_address(cfg),
        "pool_url": f"{cfg.get('POOL_HOST')}:{cfg.get('POOL_PORT')}",
        "telegram_chat_id": cfg.get("TELEGRAM_CHAT_ID") or cfg.get("CHAT_ID", ""),
    }
    with session_scope() as db:
        config_row = db.query(Setting).filter(Setting.key == "default_config").first()
        if config_row is None:
            config_row = Setting(key="default_config", value="")
            db.add(config_row)
        config_row.wallet_address = defaults["wallet_address"]
        config_row.pool_url = defaults["pool_url"]
        config_row.telegram_chat_id = defaults["telegram_chat_id"]
        for key, value in defaults.items():
            row = db.query(Setting).filter(Setting.key == key).first()
            if row is None:
                db.add(Setting(key=key, value=value or ""))


def record_event(level: str, category: str, message: str, details: str = "") -> None:
    try:
        with session_scope() as db:
            db.add(SystemEvent(level=level, category=category, message=message, details=details))
    except Exception:
        print(f"[{level.upper()}] {category}: {message} {details}")


# Backward-compatible names for older local scripts.
MiningStat = HardwareLog
DailyReward = MiningReward
