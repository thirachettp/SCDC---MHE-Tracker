# Summary Trip MHE

แอปบันทึกจำนวน MHE (Pallet · Totebox · Rollcage · Box) รายสาขาต่อ Trip สำหรับทีม LP
ทั้ง **ขากลับเข้าคลัง** (งานหลัก) และ **ขาออกจากคลัง** (เก็บเป็นข้อมูลตั้งต้น) พร้อมเมนู Reconcile
เทียบกับไฟล์ MHE Daily Movement

Stack: FastAPI (Python) + vanilla JS ไฟล์เดียว (`static/index.html`) + Postgres (Neon) บน Vercel (sin1)
Version stamp: `app/version.py` (รูปแบบ `TTLOD.yymmdd.hhmm`)

## โครงสร้าง

```
public/             ไฟล์ static ที่เปิดให้เข้าถึงได้ (มีโฟลเดอร์นี้เพื่อไม่ให้ Vercel เสิร์ฟ source code เป็นไฟล์ static)
api/index.py        entrypoint ของ Vercel (ทุก route rewrite มาที่นี่)
app/main.py         API ทั้งหมด + auth + bootstrap admin
app/db.py           schema + auto-migration (รันเองตอน cold start)
app/auth.py         hash รหัสผ่าน (PBKDF2) + token
app/drive.py        ดึงไฟล์แผนโหลดจาก Google Drive
app/parsing.py      อ่าน Excel แผนโหลด / ไฟล์ MHE
app/reconcile.py    ตรรกะ Reconcile
static/index.html   หน้าจอทั้งหมด
```

## Deploy (Vercel + Neon)

1. **Neon** – สร้าง project region **Singapore (ap-southeast-1)** แล้วคัดลอก connection string แบบ **Pooled** (host มี `-pooler`)
2. **Vercel** – import repo นี้ (Framework: Other) แล้วตั้ง Environment Variables ตาม `.env.example`
   - `DATABASE_URL`, `SECRET_KEY` (≥ 32 ตัวอักษร), `ADMIN_EMAIL` — จำเป็น
   - `CRON_SECRET` — ใส่ค่าสุ่มอะไรก็ได้ Vercel จะส่งให้ cron รอบ 06:00 เอง
   - Region ถูกล็อกเป็น `sin1` ใน `vercel.json` แล้ว (ให้อยู่ใกล้ Neon)
3. Deploy แล้วเปิดเว็บ 1 ครั้ง → ไปที่ Vercel › Logs จะเห็นบรรทัด
   `BOOTSTRAP ADMIN CREATED email=... password=...` (แสดงครั้งเดียวตอนฐานข้อมูลยังว่าง)
   เข้าสู่ระบบด้วยรหัสนั้น ระบบจะบังคับให้ตั้งรหัสผ่านใหม่ทันที
4. ไม่มีบัญชีทดสอบ/เดโมในระบบ ผู้ใช้อื่นสมัครเองได้ (ได้สิทธิ์ User) แล้ว Admin ตั้งเป็น Admin ได้ในเมนู "ผู้ใช้"

## เชื่อม Google Drive (ไฟล์แผนโหลด)

1. Google Cloud Console → สร้าง project → เปิด **Google Drive API**
2. สร้าง **Service Account** → Keys → Add key → JSON → ดาวน์โหลด
3. เปิดโฟลเดอร์ Drive ที่ automate เอาไฟล์ไปวาง → **Share** ให้อีเมลของ service account (สิทธิ์ Viewer)
4. ตั้ง env บน Vercel
   - `DRIVE_FOLDER_ID` = ส่วนท้าย URL ของโฟลเดอร์ (`drive.google.com/drive/folders/<ID>`)
   - `GOOGLE_SERVICE_ACCOUNT_JSON` = เนื้อหาไฟล์ JSON ทั้งก้อน
5. Redeploy → เมนู "นำเข้า" กด **Sync ตอนนี้** เพื่อทดสอบ

การดึงข้อมูล
- อัตโนมัติเมื่อมีคนเปิดแอปและข้อมูลเก่ากว่า `AUTO_SYNC_MINUTES` (ค่าเริ่มต้น 10 นาที)
- รอบสำรองทุกวัน 06:00 (Vercel Cron, `0 23 * * *` UTC)
- อ่านเฉพาะไฟล์ใหม่/ไฟล์ที่ถูกแก้ ภายใน `PLAN_LOOKBACK_DAYS` วันล่าสุด; ถ้า Load เดียวกันอยู่หลายไฟล์ ใช้ไฟล์ที่แก้ไขล่าสุด
- ถ้ายังไม่ได้ตั้งค่า Drive Admin อัปโหลดไฟล์แผนโหลดเองได้ในเมนู "นำเข้า"

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
