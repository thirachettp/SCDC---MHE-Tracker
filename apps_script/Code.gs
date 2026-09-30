/**
 * Summary Trip MHE — ส่งไฟล์แผนโหลดจาก Google Drive เข้าแอปบน Vercel
 *
 * ทำงานด้วยสิทธิ์ของบัญชี Google ที่ติดตั้งสคริปต์ (ไม่ต้องใช้ Google Cloud)
 *   - ทุก 10 นาที: ดูไฟล์ในโฟลเดอร์ → ถามแอปว่าไฟล์ไหนใหม่/ถูกแก้ → ส่งเฉพาะไฟล์นั้น
 *   - Web App (doGet): ให้ปุ่ม "Sync ตอนนี้" ในแอปสั่งให้ทำงานทันที
 *
 * ติดตั้ง: ดูหัวข้อ "เชื่อม Google Drive ด้วย Apps Script" ใน README.md
 */

// ============================== ตั้งค่า (แก้ 2 บรรทัดแรก) ==============================
const CONFIG = {
  APP_URL: 'https://YOUR-APP.vercel.app',                 // URL ของแอป (ไม่ต้องมี / ท้าย)
  PUSH_SECRET: 'PASTE-THE-SAME-VALUE-AS-PLAN_PUSH_SECRET', // ค่าเดียวกับ env PLAN_PUSH_SECRET บน Vercel
  FOLDER_ID: '18HOet6f4lq6KHEBc3ACXXi_OKH6zdRhp',          // โฟลเดอร์ TSP_Rawdata
  FILE_PREFIX: 'Summary plan load daily report',           // ชื่อไฟล์ต้องขึ้นต้นด้วยคำนี้
  LOOKBACK_DAYS: 14,                                       // ดูเฉพาะไฟล์ที่แก้ไขภายในกี่วัน
  MAX_RUN_MS: 5 * 60 * 1000,                               // Apps Script จำกัด 6 นาทีต่อรอบ
};
// =====================================================================================

const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const GSHEET_MIME = 'application/vnd.google-apps.spreadsheet';

/** รันครั้งเดียวตอนติดตั้ง: สร้าง trigger ทุก 10 นาที แล้วซิงค์รอบแรก */
function setup() {
  checkConfig_();
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'syncPlans')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncPlans').timeBased().everyMinutes(10).create();
  const r = syncPlans();
  console.log('ติดตั้งเรียบร้อย · trigger ทุก 10 นาที\n' + JSON.stringify(r, null, 2));
}

/** ทดสอบว่าเชื่อมแอปได้และรหัสตรงกัน โดยยังไม่ส่งไฟล์ */
function testConnection() {
  checkConfig_();
  const files = listFiles_();
  const need = api_('/api/push/plan/check', { files: files.map(meta_) }).needed || [];
  console.log('เชื่อมแอปได้ ✓  พบไฟล์ในโฟลเดอร์ ' + files.length + ' ไฟล์ · แอปต้องการ ' + need.length + ' ไฟล์');
  files.forEach(f => console.log((need.indexOf(f.id) >= 0 ? '[ใหม่] ' : '[มีแล้ว] ') + f.name + '  ' + f.modified));
}

/** งานหลัก: เรียกจาก trigger ทุก 10 นาที และจากปุ่ม Sync ตอนนี้ */
function syncPlans() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(2000)) return { ok: true, skipped: 'running', pushed: [], errors: [] };
  const started = Date.now();
  const result = { ok: true, seen: 0, pushed: [], errors: [], pending: 0 };
  try {
    const files = listFiles_();
    result.seen = files.length;
    const need = files.length
      ? (api_('/api/push/plan/check', { files: files.map(meta_) }).needed || [])
      : [];
    const byId = {};
    files.forEach(f => { byId[f.id] = f; });
    for (let i = 0; i < need.length; i++) {
      if (Date.now() - started > CONFIG.MAX_RUN_MS) { result.pending = need.length - i; break; }
      const f = byId[need[i]];
      if (!f) continue;
      try {
        const bytes = xlsxBytes_(f);
        const r = api_('/api/push/plan', Object.assign(meta_(f), { content_b64: Utilities.base64Encode(bytes) }));
        result.pushed.push({ name: f.name, status: r.status, rows: r.rows || 0 });
        if (r.status === 'error') result.errors.push(f.name + ': ' + r.error);
      } catch (e) {
        result.errors.push(f.name + ': ' + (e.message || e));
      }
    }
    done_(result);
  } catch (e) {
    result.ok = false;
    result.error = String(e.message || e);
    result.errors.push(result.error);
    try { done_(result); } catch (ignored) { /* app unreachable */ }
  } finally {
    lock.releaseLock();
  }
  console.log(JSON.stringify(result));
  return result;
}

/** Web App: แอปเรียก URL นี้เมื่อ admin กด "Sync ตอนนี้" */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!CONFIG.PUSH_SECRET || p.key !== CONFIG.PUSH_SECRET) {
    return json_({ ok: false, error: 'unauthorized (PUSH_SECRET ไม่ตรงกัน)' });
  }
  return json_(syncPlans());
}

// ------------------------------------------------------------------ helpers
function listFiles_() {
  const since = new Date(Date.now() - CONFIG.LOOKBACK_DAYS * 86400000);
  const q = "'" + CONFIG.FOLDER_ID + "' in parents and trashed = false and modifiedDate > '" +
            Utilities.formatDate(since, 'UTC', "yyyy-MM-dd'T'HH:mm:ss") + "'";
  const want = norm_(CONFIG.FILE_PREFIX);
  const out = [];
  const it = DriveApp.searchFiles(q);
  while (it.hasNext()) {
    const f = it.next();
    const mime = f.getMimeType();
    if (mime !== XLSX_MIME && mime !== GSHEET_MIME) continue;
    if (norm_(f.getName()).indexOf(want) !== 0) continue;   // ไม่สนช่องว่าง / _ / ตัวพิมพ์
    out.push({ id: f.getId(), name: f.getName(), modified: f.getLastUpdated().toISOString(), mime: mime, file: f });
  }
  out.sort((a, b) => (a.modified < b.modified ? 1 : -1));   // ใหม่สุดก่อน
  return out;
}

function xlsxBytes_(f) {
  if (f.mime === GSHEET_MIME) {   // Google Sheet → แปลงเป็น .xlsx
    const url = 'https://docs.google.com/spreadsheets/d/' + f.id + '/export?format=xlsx';
    return UrlFetchApp.fetch(url, { headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() } }).getContent();
  }
  return f.file.getBlob().getBytes();
}

function api_(path, body) {
  const res = UrlFetchApp.fetch(CONFIG.APP_URL.replace(/\/+$/, '') + path, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(body),
    headers: { 'X-Push-Secret': CONFIG.PUSH_SECRET },
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  const text = res.getContentText();
  let data;
  try { data = JSON.parse(text); } catch (e) { throw new Error('แอปตอบกลับไม่ใช่ JSON (HTTP ' + code + ')'); }
  if (code >= 300) throw new Error(data.error || ('HTTP ' + code));
  return data;
}

function done_(r) {
  api_('/api/push/plan/done', { seen: r.seen, pushed: r.pushed, errors: r.errors, pending: r.pending });
}

function meta_(f) { return { id: f.id, name: f.name, modified: f.modified }; }
function norm_(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function checkConfig_() {
  if (/YOUR-APP/.test(CONFIG.APP_URL)) throw new Error('ยังไม่ได้ใส่ APP_URL ใน CONFIG');
  if (!CONFIG.PUSH_SECRET || /PASTE-THE-SAME/.test(CONFIG.PUSH_SECRET)) throw new Error('ยังไม่ได้ใส่ PUSH_SECRET ใน CONFIG');
}
