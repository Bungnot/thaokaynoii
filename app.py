import os
import re
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, TextMessageContent

app = Flask(__name__)

# LINE credentials from environment
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
EASY_SLIP_API_KEY = os.environ.get("EASY_SLIP_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

ACCOUNT_MESSAGE = (
    "━━━━━━━━━━━━━━\n"
    "🏦 แจ้งเลขบัญชีฝากเงิน\n"
    "🔢 เลขบัญชี : 0748441328\n"
    "🏛 ธนาคาร : กสิกรไทย\n"
    "👤 ชื่อบัญชี : กิตติเชษฐ์ บุญอินทร์\n"
    "━━━━━━━━━━━━━━\n"
    "⚠️ เพื่อป้องกันมิจฉาชีพ\n"
    "ชื่อผู้ฝาก-ถอน ต้องเป็นชื่อเดียวกันเท่านั้น ✅"
)

ACCOUNT_KEYWORDS = ["บช", "บัญชี", "account", "โอนเงิน", "เลขบัญชี", "ธนาคาร", "จ่ายเงิน", "ชำระ"]


def verify_slip_with_easyslip(image_content: bytes) -> dict:
    """ส่งรูปสลิปไปยัง EasySlip API และคืนผลลัพธ์"""
    url = "https://developer.easyslip.com/api/v1/verify"
    headers = {"Authorization": f"Bearer {EASY_SLIP_API_KEY}"}
    files = {"file": ("slip.jpg", image_content, "image/jpeg")}
    try:
        response = requests.post(url, headers=headers, files=files, timeout=30)
        return response.json()
    except Exception as e:
        return {"status": 500, "message": str(e)}


def build_success_flex(data: dict) -> dict:
    """สร้าง Flex Message เมื่อตรวจสอบสลิปสำเร็จ"""
    payment = data.get("data", {})
    amount = payment.get("amount", {}).get("amount", "-")
    sender_name = payment.get("sender", {}).get("displayName", "-")
    receiver_name = payment.get("receiver", {}).get("displayName", "-")
    date_str = payment.get("date", "-")
    trans_ref = payment.get("transRef", "-")
    bank_name = payment.get("receiver", {}).get("bank", {}).get("name", "-")

    flex_body = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "✅ ตรวจสอบสลิปสำเร็จ",
                    "color": "#ffffff",
                    "size": "md",
                    "weight": "bold",
                    "align": "center",
                }
            ],
            "backgroundColor": "#27AE60",
            "paddingAll": "15px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                _flex_row("👤 ผู้โอน", sender_name),
                _flex_row("🏦 ผู้รับ", receiver_name),
                _flex_row("🏛 ธนาคาร", bank_name),
                _flex_row("💰 จำนวนเงิน", f"{amount} บาท"),
                _flex_row("📅 วันที่", date_str),
                _flex_row("🔖 เลขอ้างอิง", trans_ref),
            ],
            "spacing": "sm",
            "paddingAll": "15px",
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "ขอบคุณที่ใช้บริการ 🙏",
                    "color": "#888888",
                    "size": "xs",
                    "align": "center",
                }
            ],
        },
    }
    return flex_body


def _flex_row(label: str, value: str) -> dict:
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#555555", "flex": 3},
            {"type": "text", "text": str(value), "size": "sm", "color": "#111111", "flex": 5, "wrap": True},
        ],
        "margin": "sm",
    }


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
    """จัดการข้อความตัวอักษร — ถ้าเกี่ยวกับบัญชีให้ส่งข้อมูลบัญชี"""
    text = event.message.text.strip().lower()
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        if any(kw in text for kw in ACCOUNT_KEYWORDS):
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=ACCOUNT_MESSAGE)],
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(
                            text="📎 กรุณาส่งรูปสลิปเพื่อตรวจสอบการชำระเงิน\nหรือพิมพ์ 'บช' เพื่อดูเลขบัญชี"
                        )
                    ],
                )
            )


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    """รับรูปภาพ → ตรวจสอบกับ EasySlip → ตอบกลับผลลัพธ์"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # ดึงรูปภาพจาก LINE
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = b"".join(message_content)

        # ส่งไปตรวจสอบกับ EasySlip
        result = verify_slip_with_easyslip(image_data)
        status = result.get("status", 500)

        if status == 200:
            flex_body = build_success_flex(result)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        FlexMessage(
                            alt_text="✅ ตรวจสอบสลิปสำเร็จ",
                            contents=FlexContainer.from_dict(flex_body),
                        )
                    ],
                )
            )
        else:
            err_msg = result.get("message", "ไม่สามารถตรวจสอบสลิปได้")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(
                            text=(
                                f"❌ ตรวจสอบสลิปไม่สำเร็จ\n"
                                f"สาเหตุ: {err_msg}\n\n"
                                "กรุณาตรวจสอบ:\n"
                                "• สลิปต้องเป็นสลิปจริง ไม่ใช่ภาพถ่ายหน้าจอซ้ำ\n"
                                "• รูปภาพต้องชัดเจน ครบถ้วน\n"
                                "• หากมีปัญหา กรุณาติดต่อเจ้าหน้าที่"
                            )
                        )
                    ],
                )
            )


@app.route("/", methods=["GET"])
def health():
    return "LINE Slip Bot is running! 🤖"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
