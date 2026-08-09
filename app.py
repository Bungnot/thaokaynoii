import os
import hmac
import hashlib
import base64
from datetime import datetime

import requests
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# =========================
# Environment variables
# =========================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
EASYSLIP_API_KEY = os.getenv("EASYSLIP_API_KEY", "").strip()

# EasySlip V2
EASYSLIP_VERIFY_URL = "https://api.easyslip.com/v2/verify/bank"

# Account shown by command "บช"
ACCOUNT_NUMBER = os.getenv("TARGET_ACCOUNT_NUMBER", "0748441328").strip()
ACCOUNT_BANK = os.getenv("TARGET_ACCOUNT_BANK", "กสิกรไทย").strip()
ACCOUNT_BANK_SHORT = os.getenv("TARGET_ACCOUNT_BANK_SHORT", "KBANK").strip()
ACCOUNT_NAME = os.getenv("TARGET_ACCOUNT_NAME", "กิตติเชษฐ์ บุญอินทร์").strip()

# EasySlip V2 Account Matching
VERIFY_MATCH_ACCOUNT = os.getenv("VERIFY_MATCH_ACCOUNT", "true").lower() == "true"

# Maximum image size supported by EasySlip V2 = 4 MB
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# Optional Railway PostgreSQL for a second duplicate-protection layer
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


# =========================
# Optional PostgreSQL
# =========================
def get_db():
    if not DATABASE_URL:
        return None

    import psycopg2

    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    return psycopg2.connect(db_url)


def init_db():
    if not DATABASE_URL:
        print("[DB] DATABASE_URL not set. Using EasySlip duplicate checking only.")
        return

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS used_slips (
                id BIGSERIAL PRIMARY KEY,
                trans_ref TEXT UNIQUE NOT NULL,
                amount NUMERIC(12,2),
                sender_name TEXT,
                sender_bank TEXT,
                receiver_name TEXT,
                receiver_bank TEXT,
                slip_date TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        conn.commit()
        cur.close()
        print("[DB] used_slips table ready.")
    except Exception as exc:
        print("[DB] init error:", exc)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def claim_trans_ref(data: dict) -> bool:
    """
    Returns:
      True  = transRef was newly inserted
      False = transRef already exists
    If no DATABASE_URL is configured, returns True and relies on EasySlip.
    """
    if not DATABASE_URL:
        return True

    raw = data.get("rawSlip") or {}
    trans_ref = str(raw.get("transRef") or "").strip()

    if not trans_ref:
        # If EasySlip didn't provide a transRef, don't block a valid response.
        return True

    amount = safe_float((((raw.get("amount") or {}).get("amount"))))
    sender = raw.get("sender") or {}
    receiver = raw.get("receiver") or {}

    sender_name = get_party_name(sender)
    receiver_name = get_party_name(receiver)
    sender_bank = ((sender.get("bank") or {}).get("short") or "").strip()
    receiver_bank = ((receiver.get("bank") or {}).get("short") or "").strip()
    slip_date = str(raw.get("date") or "")

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO used_slips
                (trans_ref, amount, sender_name, sender_bank,
                 receiver_name, receiver_bank, slip_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (trans_ref) DO NOTHING
            RETURNING id
            """,
            (
                trans_ref,
                amount,
                sender_name,
                sender_bank,
                receiver_name,
                receiver_bank,
                slip_date,
            ),
        )
        inserted = cur.fetchone()
        conn.commit()
        cur.close()
        return inserted is not None
    except Exception as exc:
        # Do not make the bot unusable if DB is temporarily unavailable.
        print("[DB] claim_trans_ref error:", exc)
        if conn:
            conn.rollback()
        return True
    finally:
        if conn:
            conn.close()


# =========================
# Helpers
# =========================
def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_party_name(party: dict) -> str:
    account = party.get("account") or {}
    names = account.get("name") or {}
    return (
        str(names.get("th") or "").strip()
        or str(names.get("en") or "").strip()
        or "-"
    )


def verify_line_signature(raw_body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).digest()

    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_headers():
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def reply_line(reply_token: str, messages: list):
    url = "https://api.line.me/v2/bot/message/reply"
    payload = {
        "replyToken": reply_token,
        "messages": messages[:5],
    }

    resp = requests.post(
        url,
        headers=line_headers(),
        json=payload,
        timeout=15,
    )

    if not resp.ok:
        print("[LINE] reply failed:", resp.status_code, resp.text)

    return resp


def text_message(text: str) -> dict:
    return {
        "type": "text",
        "text": text,
    }


ACCOUNT_MESSAGE = f"""━━━━━━━━━━━━━━
🏦 แจ้งเลขบัญชีฝากเงิน
🔢 เลขบัญชี : {ACCOUNT_NUMBER}
🏛 ธนาคาร : {ACCOUNT_BANK}
👤 ชื่อบัญชี : {ACCOUNT_NAME}
━━━━━━━━━━━━━━
⚠️ เพื่อป้องกันมิจฉาชีพ
ชื่อผู้ฝาก-ถอน ต้องเป็นชื่อเดียวกันเท่านั้น ✅"""


def download_line_image(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.content


def verify_with_easyslip(image_bytes: bytes) -> dict:
    """
    EasySlip API V2:
      POST https://api.easyslip.com/v2/verify/bank
      multipart/form-data:
        image
        matchAccount=true
        checkDuplicate=true
    """
    headers = {
        "Authorization": f"Bearer {EASYSLIP_API_KEY}",
    }

    files = {
        "image": ("slip.jpg", image_bytes, "image/jpeg"),
    }

    form = {
        "checkDuplicate": "true",
        "matchAccount": "true" if VERIFY_MATCH_ACCOUNT else "false",
        "remark": "LINE BOT เถ้าแก่น้อย",
    }

    resp = requests.post(
        EASYSLIP_VERIFY_URL,
        headers=headers,
        files=files,
        data=form,
        timeout=30,
    )

    try:
        payload = resp.json()
    except ValueError:
        payload = {
            "success": False,
            "error": {
                "code": "EASYSLIP_INVALID_RESPONSE",
                "message": resp.text[:500] or "EasySlip response is not JSON",
            },
        }

    payload["_http_status"] = resp.status_code
    return payload


def normalize_easyslip_error(payload: dict):
    """
    EasySlip documentation shows both:
      {"success": false, "error": {"code": "...", "message": "..."}}
    and some reference examples:
      {"status": 400, "message": "duplicate_slip", "data": {...}}
    This function supports both.
    """
    error = payload.get("error") or {}

    code = str(
        error.get("code")
        or payload.get("message")
        or "UNKNOWN_ERROR"
    ).strip()

    message = str(
        error.get("message")
        or payload.get("message")
        or "เกิดข้อผิดพลาดในการตรวจสอบสลิป"
    ).strip()

    return code.lower(), message


def display_date(value: str) -> str:
    if not value:
        return "-"

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return value


# =========================
# Flex Message
# =========================
def flex_row(label: str, value: str, value_color="#123F5A"):
    return {
        "type": "box",
        "layout": "horizontal",
        "margin": "md",
        "contents": [
            {
                "type": "text",
                "text": label,
                "size": "sm",
                "color": "#6B7C85",
                "flex": 4,
                "wrap": True,
            },
            {
                "type": "text",
                "text": str(value),
                "size": "sm",
                "color": value_color,
                "weight": "bold",
                "align": "end",
                "flex": 6,
                "wrap": True,
            },
        ],
    }


def success_flex(data: dict) -> dict:
    raw = data.get("rawSlip") or {}
    amount = safe_float(((raw.get("amount") or {}).get("amount")))
    sender = raw.get("sender") or {}
    receiver = raw.get("receiver") or {}

    sender_name = get_party_name(sender)
    receiver_name = get_party_name(receiver)
    sender_bank = ((sender.get("bank") or {}).get("short") or "-")
    receiver_bank = ((receiver.get("bank") or {}).get("short") or "-")
    trans_ref = str(raw.get("transRef") or "-")
    date_text = display_date(str(raw.get("date") or ""))

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#06C755",
            "paddingAll": "20px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "text",
                            "text": "✓",
                            "size": "3xl",
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "flex": 0,
                        },
                        {
                            "type": "text",
                            "text": "สลิปถูกต้อง",
                            "size": "xl",
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "gravity": "center",
                            "margin": "md",
                            "flex": 1,
                        },
                    ],
                }
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F5FBF5",
            "paddingAll": "22px",
            "contents": [
                {
                    "type": "text",
                    "text": f"฿{amount:,.2f}",
                    "size": "3xl",
                    "weight": "bold",
                    "color": "#0B4A6B",
                },
                {
                    "type": "text",
                    "text": "ตรวจสอบกับ EasySlip API V2 สำเร็จ",
                    "size": "sm",
                    "color": "#7A8C94",
                    "margin": "sm",
                },
                {
                    "type": "separator",
                    "margin": "xl",
                    "color": "#D9E7DB",
                },
                flex_row("ผู้โอน", sender_name),
                flex_row("ธนาคารผู้โอน", sender_bank),
                flex_row("ผู้รับ", receiver_name),
                flex_row("ธนาคารผู้รับ", receiver_bank),
                flex_row("วันที่", date_text),
                flex_row("เลขอ้างอิง", trans_ref[-18:] if len(trans_ref) > 18 else trans_ref),
                {
                    "type": "separator",
                    "margin": "xl",
                    "color": "#D9E7DB",
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "lg",
                    "contents": [
                        {
                            "type": "text",
                            "text": "✓",
                            "size": "xl",
                            "weight": "bold",
                            "color": "#2B78B8",
                            "flex": 0,
                        },
                        {
                            "type": "box",
                            "layout": "vertical",
                            "margin": "md",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "สลิปจริงตรวจสอบโดย เถ้าแก่น้อย",
                                    "weight": "bold",
                                    "size": "sm",
                                    "color": "#1A5276",
                                    "wrap": True,
                                },
                                {
                                    "type": "text",
                                    "text": "ไม่สามารถใช้สลิปซ้ำได้",
                                    "size": "xs",
                                    "color": "#66757F",
                                    "margin": "xs",
                                },
                            ],
                        },
                    ],
                },
            ],
        },
    }

    return {
        "type": "flex",
        "altText": f"สลิปถูกต้อง ฿{amount:,.2f}",
        "contents": bubble,
    }


ERROR_TEXTS = {
    "duplicate_slip": (
        "สลิปนี้ถูกใช้แล้ว",
        "ไม่สามารถใช้สลิปซ้ำได้ กรุณาใช้สลิปใหม่เท่านั้น",
    ),
    "slip_not_found": (
        "ไม่พบข้อมูลสลิป",
        "กรุณาส่งรูปสลิปที่มี QR Code ชัดเจน",
    ),
    "qrcode_not_found": (
        "ไม่พบ QR Code",
        "กรุณาส่งรูปสลิปเต็มใบและให้ QR Code มองเห็นชัดเจน",
    ),
    "invalid_image": (
        "รูปภาพไม่ถูกต้อง",
        "รองรับ JPEG, PNG, GIF และ WebP",
    ),
    "invalid_image_format": (
        "รูปภาพไม่ถูกต้อง",
        "รองรับ JPEG, PNG, GIF และ WebP",
    ),
    "image_size_too_large": (
        "รูปภาพใหญ่เกินไป",
        "EasySlip V2 รองรับรูปไม่เกิน 4 MB",
    ),
    "slip_expired": (
        "สลิปหมดอายุ",
        "กรุณาใช้สลิปที่ยังสามารถตรวจสอบได้",
    ),
    "slip_pending": (
        "สลิปกำลังรอตรวจสอบ",
        "หากเป็นธนาคารกรุงเทพ กรุณารอสักครู่แล้วลองใหม่",
    ),
    "account_not_match": (
        "บัญชีผู้รับไม่ตรง",
        "สลิปนี้ไม่ได้โอนเข้าบัญชีที่กำหนด",
    ),
    "amount_not_match": (
        "จำนวนเงินไม่ตรง",
        "ยอดเงินในสลิปไม่ตรงกับยอดที่กำหนด",
    ),
    "quota_exceeded": (
        "โควต้า EasySlip หมด",
        "กรุณาติดต่อผู้ดูแลระบบ",
    ),
    "unauthorized": (
        "EasySlip API Key ไม่ถูกต้อง",
        "กรุณาตรวจสอบ EASYSLIP_API_KEY",
    ),
    "forbidden": (
        "EasySlip ไม่อนุญาตให้ใช้งาน",
        "กรุณาตรวจสอบสิทธิ์ API หรือ IP Whitelist",
    ),
}


def error_flex(code: str, detail: str = "") -> dict:
    normalized = (code or "unknown_error").lower()
    title, desc = ERROR_TEXTS.get(
        normalized,
        ("ตรวจสอบสลิปไม่สำเร็จ", detail or "กรุณาลองใหม่อีกครั้ง"),
    )

    # Dedicated copy for duplicate slips
    if normalized == "duplicate_slip":
        badge = "!"
        header = "#E53935"
    else:
        badge = "×"
        header = "#D64545"

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": header,
            "paddingAll": "20px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "text",
                            "text": badge,
                            "size": "3xl",
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "flex": 0,
                        },
                        {
                            "type": "text",
                            "text": title,
                            "size": "xl",
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "gravity": "center",
                            "margin": "md",
                            "flex": 1,
                            "wrap": True,
                        },
                    ],
                }
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#FFF8F7",
            "paddingAll": "22px",
            "contents": [
                {
                    "type": "text",
                    "text": desc,
                    "size": "md",
                    "weight": "bold",
                    "color": "#8A2F2F",
                    "wrap": True,
                },
                {
                    "type": "separator",
                    "margin": "xl",
                    "color": "#F0D5D2",
                },
                {
                    "type": "text",
                    "text": "เถ้าแก่น้อย • EasySlip API V2",
                    "size": "sm",
                    "color": "#52636C",
                    "weight": "bold",
                    "margin": "lg",
                },
                {
                    "type": "text",
                    "text": "ระบบป้องกันการนำสลิปเดิมกลับมาใช้ซ้ำ",
                    "size": "xs",
                    "color": "#7A8C94",
                    "margin": "xs",
                    "wrap": True,
                },
            ],
        },
    }

    return {
        "type": "flex",
        "altText": title,
        "contents": bubble,
    }


# =========================
# Event handlers
# =========================
def handle_text(event: dict):
    text = str(((event.get("message") or {}).get("text") or "")).strip()
    reply_token = event.get("replyToken")

    if text == "บช":
        reply_line(reply_token, [text_message(ACCOUNT_MESSAGE)])


def handle_image(event: dict):
    reply_token = event.get("replyToken")
    message = event.get("message") or {}
    message_id = str(message.get("id") or "")

    if not EASYSLIP_API_KEY:
        reply_line(
            reply_token,
            [error_flex("unauthorized", "ยังไม่ได้ตั้งค่า EASYSLIP_API_KEY")],
        )
        return

    try:
        image_bytes = download_line_image(message_id)
    except Exception as exc:
        print("[LINE] image download error:", exc)
        reply_line(
            reply_token,
            [error_flex("image_download_error", "ดาวน์โหลดรูปจาก LINE ไม่สำเร็จ")],
        )
        return

    if len(image_bytes) > MAX_IMAGE_BYTES:
        reply_line(reply_token, [error_flex("image_size_too_large")])
        return

    try:
        result = verify_with_easyslip(image_bytes)
    except requests.RequestException as exc:
        print("[EasySlip] request error:", exc)
        reply_line(
            reply_token,
            [error_flex("easyslip_unavailable", "เชื่อมต่อ EasySlip ไม่สำเร็จ กรุณาลองใหม่")],
        )
        return

    # EasySlip V2 success
    if result.get("success") is True:
        data = result.get("data") or {}

        # EasySlip can expose isDuplicate in success data.
        if data.get("isDuplicate") is True:
            reply_line(reply_token, [error_flex("duplicate_slip")])
            return

        # When matchAccount=true, require a matched account.
        if VERIFY_MATCH_ACCOUNT and data.get("matchedAccount") is None:
            reply_line(reply_token, [error_flex("account_not_match")])
            return

        # Optional second duplicate-protection layer using PostgreSQL.
        if not claim_trans_ref(data):
            reply_line(reply_token, [error_flex("duplicate_slip")])
            return

        reply_line(reply_token, [success_flex(data)])
        return

    code, detail = normalize_easyslip_error(result)

    # Support uppercase error codes from some V2 responses.
    alias = {
        "slip_not_found": "slip_not_found",
        "qrcode_not_found": "qrcode_not_found",
        "invalid_image_format": "invalid_image_format",
        "image_size_too_large": "image_size_too_large",
        "slip_pending": "slip_pending",
    }
    code = alias.get(code, code)

    reply_line(reply_token, [error_flex(code, detail)])


# =========================
# Flask routes
# =========================
@app.get("/")
def home():
    return jsonify(
        {
            "ok": True,
            "service": "LINE Slip Bot - เถ้าแก่น้อย",
            "easyslip": "v2",
        }
    )


@app.get("/health")
def health():
    return "OK", 200


@app.post("/webhook")
def webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(raw_body, signature):
        abort(400, description="Invalid LINE signature")

    payload = request.get_json(silent=True) or {}
    events = payload.get("events") or []

    for event in events:
        try:
            if event.get("type") != "message":
                continue

            msg_type = ((event.get("message") or {}).get("type") or "").lower()

            if msg_type == "text":
                handle_text(event)
            elif msg_type == "image":
                handle_image(event)

        except Exception as exc:
            # Always keep webhook endpoint alive.
            print("[WEBHOOK] event error:", exc)

    return "OK", 200


init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
