# 🤖 LINE Slip Bot — EasySlip Verifier

บอท LINE สำหรับตรวจสอบสลิปโอนเงินผ่าน EasySlip API อัตโนมัติ

---

## ✨ ฟีเจอร์

- 📎 รับรูปสลิปจากลูกค้า → ตรวจสอบทันทีผ่าน EasySlip API
- ✅ สลิปผ่าน → ส่ง Flex Message แสดงรายละเอียดการโอน
- ❌ สลิปไม่ผ่าน → แจ้งเตือนลูกค้าพร้อมคำแนะนำ
- 🏦 พิมพ์ `บช` หรือคำเกี่ยวกับบัญชี → ส่งข้อมูลบัญชีธนาคารอัตโนมัติ

---

## 🛠️ วิธี Deploy

### 1. เตรียม API Keys

| Key | ได้จาก |
|-----|--------|
| `LINE_CHANNEL_ACCESS_TOKEN` | [LINE Developers Console](https://developers.line.biz/) |
| `LINE_CHANNEL_SECRET` | LINE Developers Console |
| `EASY_SLIP_API_KEY` | [EasySlip Developer](https://developer.easyslip.com/) |

### 2. Push ขึ้น GitHub

```bash
git init
git add .
git commit -m "Initial LINE Slip Bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/line-slip-bot.git
git push -u origin main
```

### 3. Deploy บน Railway

1. ไปที่ [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. เลือก repo `line-slip-bot`
3. ไปที่ **Variables** แล้วเพิ่ม Environment Variables ทั้ง 3 ตัว:
   ```
   LINE_CHANNEL_ACCESS_TOKEN = xxxxx
   LINE_CHANNEL_SECRET       = xxxxx
   EASY_SLIP_API_KEY         = xxxxx
   ```
4. Railway จะ deploy อัตโนมัติ รอสักครู่จนได้ URL เช่น `https://line-slip-bot.up.railway.app`

### 4. ตั้ง Webhook ใน LINE

1. ไปที่ LINE Developers Console → เลือก Channel ของคุณ
2. **Messaging API** → **Webhook URL** → ใส่:
   ```
   https://YOUR_RAILWAY_URL/callback
   ```
3. กด **Verify** → ต้องได้ ✅ Success
4. เปิด **Use webhook** = ON
5. ปิด **Auto-reply messages** และ **Greeting messages** (แนะนำ)

---

## 📁 โครงสร้างไฟล์

```
line-slip-bot/
├── app.py           # โค้ดหลักของบอท
├── requirements.txt # Python dependencies
├── Procfile         # คำสั่ง start สำหรับ Railway
├── railway.toml     # การตั้งค่า Railway
├── .env.example     # ตัวอย่าง environment variables
├── .gitignore
└── README.md
```

---

## 🧪 ทดสอบ Local

```bash
pip install -r requirements.txt
cp .env.example .env
# แก้ไข .env ใส่ API Keys จริง

python app.py
# บอทจะรันที่ http://localhost:8080
```

ใช้ [ngrok](https://ngrok.com) เพื่อ expose localhost:
```bash
ngrok http 8080
# copy URL ที่ได้ไปตั้งใน LINE Webhook
```

---

## 💬 คำสั่งที่บอทรองรับ

| ลูกค้าส่ง | บอทตอบ |
|-----------|--------|
| รูปสลิป | ตรวจสอบและแสดงผลทันที |
| `บช` / `บัญชี` / `โอนเงิน` / `เลขบัญชี` / `ธนาคาร` / `จ่ายเงิน` / `ชำระ` | ข้อมูลบัญชีธนาคาร |
| ข้อความอื่น | แนะนำให้ส่งสลิปหรือพิมพ์ `บช` |
