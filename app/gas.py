"""Google Apps Script push mode.

Instead of the app reading Google Drive (needs a Google Cloud service
account), a small Apps Script running under the user's own Google account
scans the plan-load folder every 10 minutes and pushes new/changed files here.

    script  --POST /api/push/plan/check-->  app answers which files it still needs
    script  --POST /api/push/plan------->  one file (base64) per call
    script  --POST /api/push/plan/done-->  run summary (shown on the Import page)

Admin "Sync ตอนนี้" and the daily cron call the script's Web App URL, which
runs the same scan immediately.
"""
import hmac
import json
import os
from datetime import datetime, timezone

import httpx

from .db import get_state, set_state

PUSH_SECRET = os.environ.get("PLAN_PUSH_SECRET", "").strip()
GAS_WEBAPP_URL = os.environ.get("GAS_WEBAPP_URL", "").strip()
STALE_MINUTES = int(os.environ.get("PUSH_STALE_MINUTES", "60"))


def configured() -> bool:
    return len(PUSH_SECRET) >= 16


def secret_ok(given: str) -> bool:
    return configured() and hmac.compare_digest((given or "").strip(), PUSH_SECRET)


def ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def needed(c, files):
    """files: [{id, name, modified}] -> ids the app doesn't have in this version."""
    ids = [f.get("id") for f in files if f.get("id")]
    known = {r["drive_file_id"]: r for r in c.execute(
        "SELECT drive_file_id, modified_time FROM plan_files WHERE drive_file_id = ANY(%s)",
        (ids,)).fetchall()}
    out = []
    for f in files:
        k = known.get(f.get("id"))
        mt = ts(f.get("modified"))
        if k and k["modified_time"] and mt and k["modified_time"] >= mt:
            continue
        out.append(f["id"])
    return out


def record_run(c, summary: dict):
    now = datetime.now(timezone.utc).isoformat()
    set_state(c, "last_run_at", now)
    set_state(c, "last_sync_at", now)
    set_state(c, "last_sync_via", "apps_script")
    errs = [x for x in summary.get("errors", []) if x]
    set_state(c, "last_sync_error", "; ".join(str(e) for e in errs)[:500] if errs else "")
    set_state(c, "last_sync_result", json.dumps(summary, ensure_ascii=False, default=str)[:4000])


def trigger(timeout=55.0):
    """Ask the Apps Script Web App to run a scan now. Returns a dict for the UI."""
    if not GAS_WEBAPP_URL:
        return {"ran": False, "reason": "no_webapp",
                "error": "ยังไม่ได้ตั้งค่า GAS_WEBAPP_URL — สคริปต์ยังทำงานเองทุก 10 นาทีตามปกติ"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as cl:
            r = cl.get(GAS_WEBAPP_URL, params={"key": PUSH_SECRET, "action": "sync"})
    except httpx.TimeoutException:
        return {"ran": True, "imported": [], "pending": 0,
                "note": "สคริปต์ยังทำงานอยู่ (ไฟล์เยอะ) — รีเฟรชหน้านี้อีกครั้งในอีกสักครู่"}
    except httpx.HTTPError as e:
        return {"ran": True, "error": f"เรียก Apps Script ไม่ได้: {e}"}
    try:
        data = r.json()
    except ValueError:
        return {"ran": True, "error": f"Apps Script ตอบกลับไม่ใช่ JSON (HTTP {r.status_code}) — "
                                      "ตรวจว่า deploy เป็น Web App แบบ Anyone แล้ว"}
    if not data.get("ok"):
        return {"ran": True, "error": data.get("error") or "Apps Script แจ้งข้อผิดพลาด"}
    return {"ran": True, "imported": data.get("pushed", []), "pending": data.get("pending", 0),
            "errors": data.get("errors", [])}


def status(c):
    last = get_state(c, "last_sync_at")
    stale = True
    if last:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
        stale = age > STALE_MINUTES
    return {"push_configured": configured(), "webapp_configured": bool(GAS_WEBAPP_URL),
            "stale": stale and configured(), "stale_minutes": STALE_MINUTES,
            "via": get_state(c, "last_sync_via", "")}
