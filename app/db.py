"""Postgres access + auto-migration.

Serverless-friendly: one short-lived connection per request. Use the *pooled*
Neon endpoint in DATABASE_URL (host contains "-pooler").
"""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "")

SCHEMA_VERSION = 2

SCHEMA = [
    # ---- users & auth
    """
    CREATE TABLE IF NOT EXISTS users (
        id            BIGSERIAL PRIMARY KEY,
        email         TEXT NOT NULL UNIQUE,
        display_name  TEXT NOT NULL,
        cost_center   TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
        active        BOOLEAN NOT NULL DEFAULT TRUE,
        token_version INTEGER NOT NULL DEFAULT 0,
        must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login_at TIMESTAMPTZ
    )
    """,
    # ---- Cost Center master (maintained by admins)
    """
    CREATE TABLE IF NOT EXISTS cost_centers (
        code       TEXT PRIMARY KEY,
        name       TEXT NOT NULL DEFAULT '',
        active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ---- key/value app state (last sync time, etc.)
    """
    CREATE TABLE IF NOT EXISTS app_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ---- plan-load files imported from Google Drive
    """
    CREATE TABLE IF NOT EXISTS plan_files (
        id            BIGSERIAL PRIMARY KEY,
        drive_file_id TEXT NOT NULL UNIQUE,
        name          TEXT NOT NULL,
        modified_time TIMESTAMPTZ,
        imported_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        status        TEXT NOT NULL,            -- ok | error
        row_count     INTEGER NOT NULL DEFAULT 0,
        trip_count    INTEGER NOT NULL DEFAULT 0,
        error         TEXT NOT NULL DEFAULT ''
    )
    """,
    # one row per (load, store). A newer file replaces a load's lines entirely.
    """
    CREATE TABLE IF NOT EXISTS plan_lines (
        id          BIGSERIAL PRIMARY KEY,
        file_id     BIGINT NOT NULL REFERENCES plan_files(id) ON DELETE CASCADE,
        load_no     TEXT NOT NULL,
        load_key    TEXT NOT NULL,
        trip_no     TEXT NOT NULL DEFAULT '',
        trip_key    TEXT NOT NULL DEFAULT '',
        truck_id    TEXT NOT NULL DEFAULT '',
        seq         INTEGER NOT NULL DEFAULT 0,
        store_code  TEXT NOT NULL,
        store_name  TEXT NOT NULL DEFAULT '',
        plan_pallet   NUMERIC,
        plan_rollcage NUMERIC,
        plan_boxes    NUMERIC,
        plan_date   DATE
    )
    """,
    "CREATE INDEX IF NOT EXISTS plan_lines_load_key ON plan_lines(load_key)",
    "CREATE INDEX IF NOT EXISTS plan_lines_trip_key ON plan_lines(trip_key)",
    """
    CREATE TABLE IF NOT EXISTS stores (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        bu   TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ---- what LP keyed. One record per document + leg.
    """
    CREATE TABLE IF NOT EXISTS trip_records (
        id          BIGSERIAL PRIMARY KEY,
        doc_no      TEXT NOT NULL,
        doc_key     TEXT NOT NULL,
        leg         TEXT NOT NULL CHECK (leg IN ('out','ret')),
        load_no     TEXT NOT NULL DEFAULT '',
        load_key    TEXT NOT NULL DEFAULT '',
        trip_no     TEXT NOT NULL DEFAULT '',
        trip_key    TEXT NOT NULL DEFAULT '',
        truck_id    TEXT NOT NULL DEFAULT '',
        door_no     INTEGER,
        in_plan     BOOLEAN NOT NULL DEFAULT FALSE,
        complete    BOOLEAN NOT NULL DEFAULT FALSE,
        expected_stores INTEGER NOT NULL DEFAULT 0,
        entered_stores  INTEGER NOT NULL DEFAULT 0,
        created_by  BIGINT REFERENCES users(id),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by  BIGINT REFERENCES users(id),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (doc_key, leg)
    )
    """,
    "CREATE INDEX IF NOT EXISTS trip_records_created ON trip_records(created_at)",
    "CREATE INDEX IF NOT EXISTS trip_records_load_key ON trip_records(load_key)",
    "CREATE INDEX IF NOT EXISTS trip_records_trip_key ON trip_records(trip_key)",
    """
    CREATE TABLE IF NOT EXISTS record_lines (
        id          BIGSERIAL PRIMARY KEY,
        record_id   BIGINT NOT NULL REFERENCES trip_records(id) ON DELETE CASCADE,
        store_code  TEXT NOT NULL,
        store_name  TEXT NOT NULL DEFAULT '',
        in_plan     BOOLEAN NOT NULL DEFAULT FALSE,
        deleted     BOOLEAN NOT NULL DEFAULT FALSE,
        pallet      INTEGER NOT NULL DEFAULT 0,
        totebox     INTEGER NOT NULL DEFAULT 0,
        rollcage    INTEGER NOT NULL DEFAULT 0,
        box         INTEGER NOT NULL DEFAULT 0,
        comment     TEXT NOT NULL DEFAULT '',
        sort_order  INTEGER NOT NULL DEFAULT 0,
        UNIQUE (record_id, store_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS record_history (
        id        BIGSERIAL PRIMARY KEY,
        record_id BIGINT NOT NULL REFERENCES trip_records(id) ON DELETE CASCADE,
        user_id   BIGINT REFERENCES users(id),
        at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        action    TEXT NOT NULL,          -- create | update
        changes   JSONB NOT NULL DEFAULT '[]'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS record_history_rec ON record_history(record_id)",
    # ---- MHE Daily Movement files for Reconcile
    """
    CREATE TABLE IF NOT EXISTS mhe_uploads (
        id          BIGSERIAL PRIMARY KEY,
        filename    TEXT NOT NULL,
        uploaded_by BIGINT REFERENCES users(id),
        uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        row_count   INTEGER NOT NULL DEFAULT 0,
        trip_count  INTEGER NOT NULL DEFAULT 0,
        date_min    DATE,
        date_max    DATE
    )
    """,
    # aggregated per (trip, store, type); a newer upload replaces the same key
    """
    CREATE TABLE IF NOT EXISTS mhe_lines (
        id          BIGSERIAL PRIMARY KEY,
        upload_id   BIGINT NOT NULL REFERENCES mhe_uploads(id) ON DELETE CASCADE,
        trip_no     TEXT NOT NULL,
        trip_key    TEXT NOT NULL,
        store_code  TEXT NOT NULL,
        store_name  TEXT NOT NULL DEFAULT '',
        bu          TEXT NOT NULL DEFAULT '',
        mtype       TEXT NOT NULL CHECK (mtype IN ('pallet','totebox')),
        tn_date     DATE,
        key_date    DATE,
        qty_in      INTEGER NOT NULL DEFAULT 0,
        qty_out     INTEGER NOT NULL DEFAULT 0,
        UNIQUE (trip_key, store_code, mtype)
    )
    """,
    "CREATE INDEX IF NOT EXISTS mhe_lines_tn ON mhe_lines(tn_date)",
]


def _conninfo():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return DATABASE_URL


@contextmanager
def conn():
    """Transaction-scoped connection. Commits on success, rolls back on error."""
    # prepare_threshold=None: required behind pgbouncer (Neon pooled endpoint)
    with psycopg.connect(_conninfo(), row_factory=dict_row, prepare_threshold=None,
                         connect_timeout=10) as c:
        yield c


_migrated = False


def migrate():
    """Idempotent. Runs once per cold start; guarded by an advisory lock."""
    global _migrated
    if _migrated:
        return
    with conn() as c:
        c.execute("SELECT pg_advisory_xact_lock(724001)")
        for stmt in SCHEMA:
            c.execute(stmt)
        c.execute(
            "INSERT INTO app_state(key, value) VALUES ('schema_version', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (str(SCHEMA_VERSION),),
        )
    _migrated = True


def get_state(c, key, default=None):
    row = c.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_state(c, key, value):
    c.execute(
        "INSERT INTO app_state(key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, str(value)),
    )
