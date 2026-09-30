# Summary Trip MHE

แอปบันทึกจำนวน MHE (Pallet · Totebox · Rollcage · Box) รายสาขาต่อ Trip สำหรับทีม LP
ทั้ง **ขากลับเข้าคลัง** (งานหลัก) และ **ขาออกจากคลัง** (เก็บเป็นข้อมูลตั้งต้น) พร้อมเมนู Reconcile
เทียบกับไฟล์ MHE Daily Movement

Stack: FastAPI (Python) + vanilla JS ไฟล์เดียว (`static/index.html`) + Postgres (Neon) บน Vercel (sin1)
Version stamp: `app/version.py` (รูปแบบ `TTLOD.yymmdd.hhmm`)

## โครงสร้าง

```
public/             ไฟล์ static ที่เปิดให้เข้าถึงได้ (มีโฟลเดอร์นี้เพื่อไม่ให้ Vercel เสิร์ฟ source code เป็นไฟล์ static)
app/main.py         entrypoint ของ Vercel (FastAPI) + API ทั้งหมด + auth + bootstrap admin
app/db.py           schema + auto-migration (รันเองตอน cold start)
app/auth.py         hash รหัสผ่าน (PBKDF2) + token
app/drive.py        ดึงไฟล์แผนโหลดจาก Google Drive
app/parsing.py      อ่าน Excel แผนโหลด / ไฟล์ MHE
app/reconcile.py    ตรรกะ Reconcile
app/gas.py          รับไฟล์แผนโหลดจาก Apps Script
apps_script/Code.gs สคริปต์ Google Apps Script (วางใน script.google.com)
static/index.html   หน้าจอทั้งหมด
```

## Deploy (Vercel + Neon)

1. **Neon** – สร้าง project region **Singapore (ap-southeast-1)** แล้วคัดลอก connection string แบบ **Pooled** (host มี `-pooler`)
2. **Vercel** – import repo นี้ (Framework Preset: **FastAPI** — Vercel ตรวจเจอเอง) แล้วตั้ง Environment Variables ตาม `.env.example`
   - `DATABASE_URL`, `SECRET_KEY` (≥ 32 ตัวอักษร), `ADMIN_EMAIL` — จำเป็น
   - `CRON_SECRET` — ใส่ค่าสุ่มอะไรก็ได้ Vercel จะส่งให้ cron รอบ 06:00 เอง
   - Region ถูกล็อกเป็น `sin1` ใน `vercel.json` แล้ว (ให้อยู่ใกล้ Neon)
3. Deploy แล้วเปิดเว็บ 1 ครั้ง → ไปที่ Vercel › Logs จะเห็นบรรทัด
   `BOOTSTRAP ADMIN CREATED email=... password=...` (แสดงครั้งเดียวตอนฐานข้อมูลยังว่าง)
   เข้าสู่ระบบด้วยรหัสนั้น ระบบจะบังคับให้ตั้งรหัสผ่านใหม่ทันที
4. ไม่มีบัญชีทดสอบ/เดโมในระบบ ผู้ใช้อื่นสมัครเองได้ (ได้สิทธิ์ User) แล้ว Admin ตั้งเป็น Admin ได้ในเมนู "ผู้ใช้"

## เชื่อม Google Drive ด้วย Apps Script (แนะนำ — ไม่ต้องใช้ Google Cloud)

สคริปต์ `apps_script/Code.gs` ทำงานด้วยบัญชี Google ของคนติดตั้ง ทุก 10 นาทีจะดูโฟลเดอร์แผนโหลด
แล้วส่งเฉพาะไฟล์ใหม่/ไฟล์ที่ถูกแก้เข้าแอป ตัวแอปยังอยู่บน Vercel + Neon เหมือนเดิม

1. **สุ่มรหัสลับ** ยาว 16 ตัวขึ้นไป แล้วตั้ง env บน Vercel: `PLAN_PUSH_SECRET=<รหัสนั้น>` → Redeploy
2. ใช้บัญชี Google ที่เปิดโฟลเดอร์ TSP_Rawdata ได้ (ควรเป็นบัญชีที่ใช้ระยะยาว) เข้า <https://script.google.com> → **New project** → ตั้งชื่อ `MHE Plan Sync`
3. ลบโค้ดเดิมใน `Code.gs` → วางเนื้อหาไฟล์ `apps_script/Code.gs` ทั้งหมด → แก้ 2 บรรทัดใน `CONFIG`: `APP_URL` (URL แอป) และ `PUSH_SECRET` (ค่าเดียวกับข้อ 1) → Save
4. เลือกฟังก์ชัน **testConnection** → Run → กด **Review permissions** แล้วอนุญาต
   (ถ้าขึ้น "Google hasn't verified this app" → Advanced → Go to MHE Plan Sync) ดูผลที่ Execution log ควรเห็น `เชื่อมแอปได้ ✓`
5. เลือกฟังก์ชัน **setup** → Run → สร้าง trigger ทุก 10 นาทีและซิงค์รอบแรก
6. (สำหรับปุ่ม **Sync ตอนนี้** ในแอป) **Deploy › New deployment** → ประเภท **Web app** → Execute as: **Me**, Who has access: **Anyone** → Deploy
   คัดลอก URL ที่ลงท้าย `/exec` ไปตั้ง env บน Vercel: `GAS_WEBAPP_URL=<URL>` → Redeploy
   (URL นี้ปลอดภัย เพราะสคริปต์จะทำงานเฉพาะเมื่อแนบรหัส PUSH_SECRET ที่ถูกต้อง)

ตรวจผลได้ที่เมนู **นำเข้า** ในแอป ถ้าไม่ได้รับข้อมูลจากสคริปต์นานเกิน 60 นาทีจะมีแถบเตือน
(ดูประวัติการทำงานของสคริปต์ได้ที่ script.google.com › Executions)

- ถ้าแก้โค้ดสคริปต์ภายหลัง: Deploy › Manage deployments › แก้ไข › Version: New version (URL เดิมใช้ต่อได้)
- เงื่อนไขไฟล์: อยู่ในโฟลเดอร์โดยตรง, ชื่อขึ้นต้นด้วย `Summary plan load daily report` (ไม่สนช่องว่าง/ขีดล่าง/ตัวพิมพ์), แก้ไขภายใน 14 วัน, เป็น .xlsx หรือ Google Sheet

### ทางเลือก: Service account (Google Cloud)
ถ้าไม่ได้ตั้ง `PLAN_PUSH_SECRET` แอปจะใช้ `DRIVE_FOLDER_ID` + `GOOGLE_SERVICE_ACCOUNT_JSON` ดึงเอง
(ต้องเปิด Google Drive API และแชร์โฟลเดอร์ให้อีเมล service account) — ดึงเมื่อมีคนเปิดแอปและข้อมูลเก่ากว่า 10 นาที + รอบสำรอง 06:00

## Cost Center

- Admin จัดการรายการที่เมนู **ผู้ใช้ › Cost Center** (รหัส + ชื่อ เช่น `82899 · Mgmt`): เพิ่ม, แก้ชื่อ, ปิดการใช้งาน, ลบ (ลบได้เฉพาะรหัสที่ไม่มีผู้ใช้)
- หน้าสมัครให้**เลือก**จากรายการที่เปิดใช้งานอยู่ ถ้ายังไม่มีรายการเลย ช่องนี้จะถูกซ่อนไว้ก่อน
- Admin เปลี่ยน Cost Center ของผู้ใช้ได้จากเมนู ⋯ ของผู้ใช้แต่ละคน

## ไฟล์แผนโหลด (Summary plan load daily report)

- อ่านจากชีต **Data**: หัวตารางแถว 3, `Trip No.` คอลัมน์แรก = Trip, `Trip No.` คอลัมน์ท้าย (AO) = Load No.
- ใช้ค่า Load No. แถวแรกของแต่ละ Trip เท่านั้น (บางแถวคอลัมน์นี้มีเลข Trip ซ้ำอยู่) และเติม 0 ข้างหน้าให้ครบ 9 หลัก เช่น `1461288` → `001461288` ให้ตรงกับไฟล์ MHE
- ถ้ามีชีต **Master Store** จะนำรหัส/ชื่อ/BU สาขาเข้าฐานข้อมูลด้วย (ใช้ตอนค้นหาเพื่อเพิ่มสาขา)

## กติกาข้อมูลที่ควรรู้

- เลข Document/Trip/Load เทียบกันแบบ **ไม่สนเลข 0 นำหน้า** (Excel มักตัด `001449276` เป็น `1449276`)
- 1 Document + 1 ขา = 1 รายการ บันทึกซ้ำ = แก้ไข (เก็บประวัติว่าใครแก้อะไร) ถ้ามีคนแก้ซ้อนกัน ระบบจะเตือนให้โหลดข้อมูลล่าสุด
- สาขาที่กด **ลบ** จะถูกบันทึกเป็น 0 ทุกช่อง
- **บันทึกครบ** = ทุกสาขาในแผน (+ สาขาที่อยู่ในขาออก สำหรับขากลับ) ถูกกรอกหรือลบแล้ว ไม่ครบก็บันทึกได้ สถานะเป็น "ยังไม่ครบ"
- ข้อมูลที่กรอกค้างไว้จะถูกเก็บเป็นฉบับร่างในเครื่อง ถ้าแอปปิดไปก่อนบันทึก เปิด Trip เดิมจะกู้คืนให้
- Reconcile จับคู่ด้วย Trip + สาขา + ประเภท (Pallet, Totebox) · ไฟล์ MHE มองจากสาขา ส่วน LP กรอกโดยมองจากคลัง จึงจับคู่ **ขากลับเข้าคลัง ↔ MHE `Out`** และ **ขาออกจากคลัง ↔ MHE `In`**
  คู่ที่เป็น 0 ทั้งสองฝั่งไม่นับ, % ความถูกต้องคิดเฉพาะ Trip ที่มีทั้งสองฝั่ง

## รันในเครื่อง

```bash
pip install -r requirements.txt uvicorn
export DATABASE_URL=postgresql://... SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))") ADMIN_EMAIL=you@company.com
uvicorn app.main:app --reload
```
