"""DB connection helper for the token store (DO PG 17 acquirely_rel)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import DesignConfig

log = logging.getLogger(__name__)


@contextmanager
def get_conn(config: DesignConfig | None = None) -> Iterator[psycopg.Connection]:
    """Get a short-lived psycopg connection to DO PG 17. Use in `with` blocks.

    Example:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            print(cur.fetchone())
    """
    cfg = config or DesignConfig.from_env()
    conn = psycopg.connect(
        host=cfg.token_db_host,
        port=cfg.token_db_port,
        dbname=cfg.token_db_name,
        user=cfg.token_db_user,
        password=cfg.token_db_password,
        row_factory=dict_row,
        autocommit=False,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
