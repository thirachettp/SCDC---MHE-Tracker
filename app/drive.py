"""Google Drive -> plan_lines importer.

Auth: a Google service account (JSON in GOOGLE_SERVICE_ACCOUNT_JSON). Share the
plan-load folder with the service account's e-mail (Viewer is enough).
Only files that are new or modified since their last import are read.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import httpx

from . import parsing
from .db import get_state, set_state

FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
FILE_PREFIX = os.environ.get("PLAN_FILE_PREFIX", "Summary plan load daily report")
LOOKBACK_DAYS = int(os.environ.get("PLAN_LOOKBACK_DAYS", "14"))
MAX_FILES_PER_RUN = int(os.environ.get("PLAN_MAX_FILES_PER_RUN", "6"))
AUTO_SYNC_MINUTES = int(os.environ.get("AUTO_SYNC_MINUTES", "10"))

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET = "application/vnd.google-apps.spreadsheet"


class SyncError(Exception):
    pass


def configured() -> bool:
    return bool(FOLDER_ID and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))


def _token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SyncError("GOOGLE_SERVICE_ACCOUNT_JSON ไม่ใช่ JSON ที่ถูกต้อง") from e
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    creds.refresh(Request())
    return creds.token


def _list_files(client, token):
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    q = (f"'{FOLDER_ID}' in parents and trashed = false and "
         f"modifiedTime > '{since}' and name contains '{FILE_PREFIX.replace(chr(39), '')}'")
    files, page = [], None
    while True:
        params = {
            "q": q, "orderBy": "modifiedTime desc", "pageSize": 200,
            "fields": "nextPageToken, files(id,name,mimeType,modifiedTime)",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        r = client.get("https://www.googleapis.com/drive/v3/files", params=params,
                       headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise SyncError(f"Drive list error {r.status_code}: {r.text[:200]}")
        data = r.json()
        files += [f for f in data.get("files", []) if f["mimeType"] in (XLSX, GSHEET)]
        page = data.get("nextPageToken")
        if not page:
            return files


def _download(client, token, f) -> bytes:
    h = {"Authorization": f"Bearer {token}"}
    if f["mimeType"] == GSHEET:
        url = f"https://www.googleapis.com/drive/v3/files/{f['id']}/export"
        r = client.get(url, params={"mimeType": XLSX}, headers=h)
    else:
        url = f"https://www.googleapis.com/drive/v3/files/{f['id']}"
        r = client.get(url, params={"alt": "media", "supportsAllDrives": "true"}, headers=h)
    if r.status_code != 200:
        raise SyncError(f"ดาวน์โหลดไม่ได้ ({r.status_code})")
    return r.content


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def import_plan_content(c, *, drive_file_id, name, modified_time, content):
    """Parse + store one plan file. Returns dict summary. Never raises ParseError."""
    try:
        lines = parsing.parse_plan(content)
    except parsing.ParseError as e:
        _upsert_file(c, drive_file_id, name, modified_time, "error", 0, 0, str(e))
        return {"name": name, "status": "error", "error": str(e)}

    fid = _upsert_file(c, drive_file_id, name, modified_time, "ok", len(lines),
                       len({l["load_no"] for l in lines}), "")
    plan_date = (modified_time or datetime.now(timezone.utc)).astimezone(
        timezone(timedelta(hours=7))).date()
    # Newer file wins. Drop this load's lines coming from older (or same) files,
    # keep lines that came from a file modified later than this one.
    c.execute("DELETE FROM plan_lines WHERE file_id = %s", (fid,))
    keys = sorted({parsing.doc_key(l["load_no"]) for l in lines})
    c.execute(
        """DELETE FROM plan_lines pl USING plan_files pf
           WHERE pl.file_id = pf.id AND pl.load_key = ANY(%s)
             AND (pf.modified_time IS NULL OR %s::timestamptz IS NULL OR pf.modified_time <= %s)""",
        (keys, modified_time, modified_time))
    newer = {r["load_key"] for r in c.execute(
        "SELECT DISTINCT load_key FROM plan_lines WHERE load_key = ANY(%s)", (keys,)).fetchall()}
    lines = [l for l in lines if parsing.doc_key(l["load_no"]) not in newer]
    with c.cursor() as cur:
        cur.executemany(
            """INSERT INTO plan_lines(file_id, load_no, load_key, trip_no, trip_key, truck_id, seq,
                   store_code, store_name, plan_pallet, plan_rollcage, plan_boxes, plan_date,
                   truck_type, transporter)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(fid, l["load_no"], parsing.doc_key(l["load_no"]), l["trip_no"],
              parsing.doc_key(l["trip_no"]), l["truck_id"], l["seq"], l["store_code"],
              l["store_name"], l["plan_pallet"], l["plan_rollcage"], l["plan_boxes"], plan_date,
              l.get("truck_type", ""), l.get("transporter", ""))
             for l in lines])
        stores = {}
        for l in lines:
            if l["store_name"]:
                stores[l["store_code"]] = l["store_name"]
        cur.executemany(
            """INSERT INTO stores(code, name) VALUES (%s, %s)
               ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, updated_at = now()""",
            list(stores.items()))
        master = parsing.parse_store_master(content)
        if master:
            cur.executemany(
                """INSERT INTO stores(code, name, bu) VALUES (%s, %s, %s)
                   ON CONFLICT (code) DO UPDATE SET
                       name = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE stores.name END,
                       bu = CASE WHEN EXCLUDED.bu <> '' THEN EXCLUDED.bu ELSE stores.bu END,
                       updated_at = now()""", master)
    return {"name": name, "status": "ok", "rows": len(lines), "stores_master": len(master)}


def _upsert_file(c, drive_file_id, name, modified_time, status, rows, trips, error):
    r = c.execute(
        """INSERT INTO plan_files(drive_file_id, name, modified_time, status, row_count, trip_count, error)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (drive_file_id) DO UPDATE SET name = EXCLUDED.name,
               modified_time = EXCLUDED.modified_time, status = EXCLUDED.status,
               row_count = EXCLUDED.row_count, trip_count = EXCLUDED.trip_count,
               error = EXCLUDED.error, imported_at = now()
           RETURNING id""",
        (drive_file_id, name, modified_time, status, rows, trips, error)).fetchone()
    return r["id"]


def _claim_lock(c) -> bool:
    """Row-based lock (works behind pgbouncer transaction pooling, unlike
    session advisory locks). Expires after 3 minutes in case a run dies."""
    r = c.execute(
        """INSERT INTO app_state(key, value) VALUES ('sync_lock', (now() + interval '3 minutes')::text)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
           WHERE app_state.value::timestamptz < now()
           RETURNING key""").fetchone()
    c.commit()
    return r is not None


def _release_lock(c):
    c.execute("UPDATE app_state SET value = '1970-01-01T00:00:00+00:00' WHERE key = 'sync_lock'")
    c.commit()


def sync(conn_factory, *, force=False):
    """Run one sync. Returns summary dict. Safe to call concurrently."""
    if not configured():
        return {"ran": False, "reason": "not_configured"}
    with conn_factory() as c:
        if not force:
            last = get_state(c, "last_sync_at")
            if last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(minutes=AUTO_SYNC_MINUTES):
                return {"ran": False, "reason": "fresh", "last_sync_at": last}
        if not _claim_lock(c):
            return {"ran": False, "reason": "running"}
        try:
            result = _run(c)
            now_iso = datetime.now(timezone.utc).isoformat()
            set_state(c, "last_run_at", now_iso)
            if not result["pending"]:
                # files left over -> don't mark fresh, the next app open continues
                set_state(c, "last_sync_at", now_iso)
            set_state(c, "last_sync_result", json.dumps(result, ensure_ascii=False, default=str))
            set_state(c, "last_sync_error", "")
            c.commit()
            return {"ran": True, **result}
        except Exception as e:  # noqa: BLE001 - report any failure to the admin page
            c.rollback()
            msg = str(e) if isinstance(e, SyncError) else f"{type(e).__name__}: {e}"
            set_state(c, "last_sync_error", msg[:500])
            set_state(c, "last_sync_at", datetime.now(timezone.utc).isoformat())
            c.commit()
            return {"ran": True, "error": msg}
        finally:
            _release_lock(c)


TIME_BUDGET_SEC = 40


def _run(c):
    import time
    started = time.monotonic()
    token = _token()
    done, pending = [], 0
    with httpx.Client(timeout=30) as client:
        files = _list_files(client, token)
        known = {r["drive_file_id"]: r for r in c.execute(
            "SELECT drive_file_id, modified_time, status FROM plan_files").fetchall()}
        todo = []
        for f in files:
            k = known.get(f["id"])
            mt = _ts(f.get("modifiedTime"))
            if k and k["modified_time"] and mt and k["modified_time"] >= mt:
                continue
            todo.append((f, mt))
        # newest first so today's plan is usable right away; import_plan_content
        # keeps lines from newer files when an older file arrives later
        todo.sort(key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for i, (f, mt) in enumerate(todo):
            if i >= MAX_FILES_PER_RUN or time.monotonic() - started > TIME_BUDGET_SEC:
                pending = len(todo) - i
                break
            try:
                content = _download(client, token, f)
            except SyncError as e:
                _upsert_file(c, f["id"], f["name"], None, "error", 0, 0, str(e))
                done.append({"name": f["name"], "status": "error", "error": str(e)})
                c.commit()
                continue
            done.append(import_plan_content(c, drive_file_id=f["id"], name=f["name"],
                                            modified_time=mt, content=content))
            c.commit()
    return {"files_seen": len(files), "imported": done, "pending": pending}
