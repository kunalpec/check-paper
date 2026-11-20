"""SQLite access helpers for the five-table application schema."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]


def db_path() -> Path:
    """Return configured database path, resolved from project root."""
    path = Path(os.getenv("DATABASE_PATH", "storage/evaluation.sqlite3"))
    return path if path.is_absolute() else ROOT / path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a transactional SQLite connection with foreign keys enabled."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize() -> None:
    """Create the exact five application tables if they do not exist."""
    schema = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    with connect() as connection:
        connection.executescript(schema)


def encode_json(value: Any) -> str:
    """Serialize and validate JSON before persisting it."""
    encoded = json.dumps(value, ensure_ascii=False)
    json.loads(encoded)
    return encoded


def decode_json(value: str | None, fallback: Any = None) -> Any:
    """Decode stored JSON with a caller-provided empty value."""
    return json.loads(value) if value else fallback
