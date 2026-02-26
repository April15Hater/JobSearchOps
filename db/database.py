import sqlite3
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level connection — used only when DB_PATH is :memory: so tables persist.
# For file-based DBs, new connections are opened per call (normal SQLite pattern).
_memory_conn: sqlite3.Connection = None


def _get_db_path() -> str:
    from config import DB_PATH
    return DB_PATH


def _is_memory_db() -> bool:
    return _get_db_path() == ":memory:"


def get_connection() -> sqlite3.Connection:
    """
    Return a sqlite3 connection with row_factory set.
    For :memory: DBs, returns a shared module-level connection so tables persist.
    For file DBs, opens a new connection (standard practice).
    """
    global _memory_conn
    if _is_memory_db():
        if _memory_conn is None:
            _memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            _memory_conn.row_factory = sqlite3.Row
            _memory_conn.execute("PRAGMA foreign_keys = ON")
        return _memory_conn
    conn = sqlite3.connect(_get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Read schema.sql and initialize all tables/triggers/views if not present."""
    schema_path = Path(__file__).parent / "schema.sql"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql not found at {schema_path}")

    sql = schema_path.read_text()
    conn = get_connection()
    conn.executescript(sql)
    conn.commit()
    logger.info("Database initialized successfully.")
    # Migrations: add new columns to existing tables (idempotent)
    for migration_sql in [
        "ALTER TABLE opportunities ADD COLUMN tailored_resume TEXT",
    ]:
        try:
            conn.execute(migration_sql)
            conn.commit()
        except Exception:
            pass  # column already exists


def execute_query(sql: str, params: tuple = (), *, fetch: str = None):
    """
    Execute a SQL query with optional params.
    fetch: None (for writes), 'one', or 'all'
    Returns lastrowid for INSERT, rowcount for UPDATE/DELETE, or rows for SELECT.
    """
    try:
        conn = get_connection()
        cur = conn.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        elif fetch == "all":
            return cur.fetchall()
        elif sql.strip().upper().startswith("INSERT"):
            conn.commit()
            return cur.lastrowid
        else:
            conn.commit()
            return cur.rowcount
    except sqlite3.Error as e:
        logger.error(f"Database error: {e} | SQL: {sql[:120]} | Params: {params}")
        raise


# Auto-initialize on import
init_db()
