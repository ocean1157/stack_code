from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"


@dataclass(frozen=True)
class Settings:
    postgres_host: str = os.getenv("QUANT_DB_HOST", "127.0.0.1")
    postgres_port: int = int(os.getenv("QUANT_DB_PORT", "5432"))
    postgres_user: str = os.getenv("QUANT_DB_USER", "quantuser")
    postgres_password: str = os.getenv("QUANT_DB_PASSWORD", "quant123")
    postgres_database: str = os.getenv("QUANT_DB_NAME", "quant")
    history_start: str = "20180101"
    transaction_cost_bps: float = 15.0
    top_k: int = 10
    request_timeout: int = 25
    workers: int = 8
    random_seed: int = 42


SETTINGS = Settings()
