import os
import json
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, TextMessageContent

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
EASY_SLIP_API_KEY = os.environ.get("EASY_SLIP_API_KEY")
OUR_ACCOUNT = os.environ.get("OUR_ACCOUNT", "0748441328")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

USED_SLIPS_FILE = "/tmp/used_slips.json"

def load_used_slips() -> set:
    try:
        with open(USED_SLIPS_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_used_slip(trans_ref: str):
    slips = load_used_slips()
    slips.add(trans_ref)
    with open(USED_SLIPS_FILE, "w") as f:
        json.dump(list(slips), f)

def is_slip_used(trans_ref: str) -> bool:
    return trans_ref in load_used_slips()

ACCOUNT_KEYWORDS = ["บช", "บัญชี", "account", "โอนเงิน", "เลขบัญชี", "ธนาคาร", "จ่ายเงิน", "ชำระ"]

def verify_slip_with_easyslip(image_content: bytes) -> dict:
    url = "https://developer.easyslip.com/api/v1/verify"
    headers = {"Authorization": f"Bearer {EASY_SLIP_API_KEY}"}
    files = {"file": ("slip.jpg", image_content, "image/jpeg")}
    try:
        response = requests.post(url, headers=headers, files=files, timeout=30)
        return response.json()
    except Exception as e:
        return {"status": 500, "message": str(e)}

def _row(label: str, value: str) -> dict:
    return {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#555555", "flex": 3},
            {"type": "text", "text": str(value), "size": "sm", "color": "#111111", "flex": 5, "wrap": True},
        ],
        "margin": "sm",
    }

def make_flex(header_text: str, color: str, rows: list, footer: str) -> dict:
    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": header_text,
                          "color": "#ffffff", "size": "md", "weight": "bold", "align": "center"}],
            "backgroundColor": color, "paddingAll": "15px",
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [_row(l, v) for l, v in rows],
            "spacing": "sm", "paddingAll": "15px",
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": footer,
                          "color": "#888888", "size": "xs", "align": "center", "wrap": True}],
            "paddingAll": "10px",
        },
    }

def flex_success(data: dict) -> dict:
    p = data.get("data", {})
    return make_flex("✅ ตรวจสอบสลิปสำเร็จ", "#27AE60", [
        ("👤 ผู้โอน",      p.get("sender", {}).get("displayName", "-")),
        ("🏦 ผู้รับ",      p.get("receiver", {}).get("displayName", "-")),
        ("🏛 ธนาคาร",     p.get("receiver", {}).get("bank", {}).get("name", "-")),
        ("💰 จำนวนเงิน",  f"{p.get('amount', {}).get('amount', '-')} บาท"),
        ("📅 วันที่",      p.get("date", "-")),
        ("🔖 เลขอ้างอิง", p.get("transRef", "-")),
    ], "ขอบคุณที่ใช้บริการ 🙏")

def flex_wrong_account(receiver_account: str) -> dict:
    return make_flex("❌ บัญชีผู้รับไม่ถูกต้อง", "#E74C3C", [
        ("🏦 บัญชีในสลิป",  receiver_account or "-"),
        ("✅ บัญชีที่ถูกต้อง", OUR_ACCOUNT),
        ("🏛 ธนาคาร",        "กสิกรไทย"),
        ("👤 ชื่อบัญชี",     "กิตติเชษฐ์ บุญอินทร์"),
    ], "กรุณาโอนมายังบัญชีของเราเท่านั้น\nพิมพ์ 'บช' เพื่อดูเลขบัญชี")

def flex_duplicate(trans_ref: str) -> dict:
    return make_flex("⚠️ สลิปถูกใช้งานแล้ว", "#E67E22", [
        ("🔖 เลขอ้างอิง", trans_ref),
        ("สถานะ",         "เคยใช้สลิปนี้ไปแล้ว"),
    ], "กรุณาส่งสลิปใหม่ หรือติดต่อเจ้าหน้าที่")

def flex_pending() -> dict:
    return make_flex("⏳ สลิปอยู่ระหว่างประมวลผล", "#8E44AD", [
        ("สถานะ",    "ธนาคารยังประมวลผลไม่เสร็จ"),
        ("คำแนะนำ", "รอ 1-2 นาที แล้วส่งสลิปใหม่อีกครั้ง"),
    ], "พบบ่อยในสลิปธนาคารกรุงเทพ / กรุงไทย")

def flex_error(reason: str) -> dict:
    return make_flex("❌ ตรวจสอบไม่สำเร็จ", "#95A5A6", [
        ("สาเหตุ",    reason),
        ("คำแนะนำ",  "สลิปต้องชัดเจน ไม่ใช่ภาพถ่ายซ้ำ"),
    ], "หากมีปัญหา กรุณาติดต่อเจ้าหน้าที่")

def flex_account() -> dict:
    return make_flex("🏦 แจ้งเลขบัญชีฝากเงิน", "#2980B9", [
        ("🔢 เลขบัญชี", "0748441328"),
        ("🏛 ธนาคาร",   "กสิกรไทย"),
        ("👤 ชื่อบัญชี", "กิตติเชษฐ์ บุญอินทร์"),
    ], "⚠️ ชื่อผู้ฝาก-ถอน ต้องเป็นชื่อเดียวกันเท่านั้น ✅")

def reply_flex(reply_token: str, alt: str, body: dict):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text=alt, contents=FlexContainer.from_dict(body))],
            )
        )

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip().lower()
    if any(kw in text for kw in ACCOUNT_KEYWORDS):
        reply_flex(event.reply_token, "🏦 แจ้งเลขบัญชีฝากเงิน", flex_account())
    else:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="📎 กรุณาส่งรูปสลิปเพื่อตรวจสอบการชำระเงิน\nหรือพิมพ์ 'บช' เพื่อดูเลขบัญชี"
                    )],
                )
            )

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_data = blob_api.get_message_content(event.message.id)

    result = verify_slip_with_easyslip(image_data)
    status = result.get("status", 500)

    if status == 200:
        payment = result.get("data", {})
        trans_ref = payment.get("transRef", "")
        receiver_account = payment.get("receiver", {}).get("account", {}).get("value", "")

        # เช็คเลขบัญชีโดยเปรียบเทียบเฉพาะส่วนที่ไม่ใช่ x
        # เช่น 074-8-xxx328 → เช็คว่า 0748 และ 328 อยู่ใน OUR_ACCOUNT ไหม
        import re
        digits_in_slip = re.sub(r'[^0-9]', '', receiver_account)  # เอาแค่ตัวเลขจริง (ไม่รวม x)
        known_parts = [p for p in re.split(r'x+', receiver_account.replace('-','')) if p]
        account_match = all(p in OUR_ACCOUNT for p in known_parts) if known_parts else False

        if not account_match:
            reply_flex(event.reply_token, "❌ บัญชีผู้รับไม่ถูกต้อง", flex_wrong_account(receiver_account))
            return

        if trans_ref and is_slip_used(trans_ref):
            reply_flex(event.reply_token, "⚠️ สลิปซ้ำ", flex_duplicate(trans_ref))
            return

        if trans_ref:
            save_used_slip(trans_ref)
        reply_flex(event.reply_token, "✅ ตรวจสอบสลิปสำเร็จ", flex_success(result))

    else:
        err = result.get("message", "")
        if "pending" in err.lower():
            reply_flex(event.reply_token, "⏳ สลิปอยู่ระหว่างประมวลผล", flex_pending())
        else:
            reply_flex(event.reply_token, "❌ ตรวจสอบไม่สำเร็จ", flex_error(err or "ไม่สามารถตรวจสอบสลิปได้"))

@app.route("/", methods=["GET"])
def health():
    return "LINE Slip Bot is running! 🤖"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

