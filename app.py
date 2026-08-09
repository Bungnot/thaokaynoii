import os
import json
import threading
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
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, TextMessageContent

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
EASY_SLIP_API_KEY = os.environ.get("EASY_SLIP_API_KEY")

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
    url = "https://developer.easyslip.com/api/v1/verify"
    headers = {"Authorization": f"Bearer {EASY_SLIP_API_KEY}"}
    files = {"file": ("slip.jpg", image_content, "image/jpeg")}
    try:
        response = requests.post(url, headers=headers, files=files, timeout=30)
        return response.json()
    except Exception as e:
        return {"status": 500, "message": str(e)}


def build_success_flex(data: dict) -> dict:
    payment = data.get("data", {})
    amount = payment.get("amount", {}).get("amount", "-")
    sender_name = payment.get("sender", {}).get("displayName", "-")
    receiver_name = payment.get("receiver", {}).get("displayName", "-")
    date_str = payment.get("date", "-")
    trans_ref = payment.get("transRef", "-")
    bank_name = payment.get("receiver", {}).get("bank", {}).get("name", "-")

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [{"type": "text", "text": "✅ ตรวจสอบสลิปสำเร็จ",
                          "color": "#ffffff", "size": "md", "weight": "bold", "align": "center"}],
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
            "contents": [{"type": "text", "text": "ขอบคุณที่ใช้บริการ 🙏",
                          "color": "#888888", "size": "xs", "align": "center"}],
        },
    }


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


def process_slip_async(user_id: str, image_data: bytes):
    """ประมวลผลสลิปใน background thread แล้ว push ผลกลับ"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        result = verify_slip_with_easyslip(image_data)
        status = result.get("status", 500)

        if status == 200:
            trans_ref = result.get("data", {}).get("transRef", "")
            payment = result.get("data", {})

            # ตรวจสอบว่าโอนมาที่บัญชีของเราหรือเปล่า
            OUR_ACCOUNT = "0748441328"
            receiver_account = payment.get("receiver", {}).get("account", {}).get("value", "")
            receiver_account_clean = receiver_account.replace("-", "").replace(" ", "")
            if OUR_ACCOUNT not in receiver_account_clean:
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=(
                            "❌ สลิปนี้ไม่ได้โอนมายังบัญชีของเรา\n\n"
                            f"🏦 บัญชีผู้รับในสลิป: {receiver_account}\n\n"
                            "กรุณาโอนเงินมาที่บัญชีของเราเท่านั้น\n"
                            "พิมพ์ \'บช\' เพื่อดูเลขบัญชีที่ถูกต้อง"
                        ))],
                    )
                )
                return

            if trans_ref and is_slip_used(trans_ref):
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=(
                            "⚠️ สลิปนี้เคยถูกใช้งานแล้ว\n\n"
                            f"🔖 เลขอ้างอิง: {trans_ref}\n\n"
                            "กรุณาส่งสลิปใหม่ที่ยังไม่เคยใช้ หรือติดต่อเจ้าหน้าที่"
                        ))],
                    )
                )
                return

            if trans_ref:
                save_used_slip(trans_ref)

            flex_body = build_success_flex(result)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[FlexMessage(
                        alt_text="✅ ตรวจสอบสลิปสำเร็จ",
                        contents=FlexContainer.from_dict(flex_body),
                    )],
                )
            )
        else:
            err_msg = result.get("message", "ไม่สามารถตรวจสอบสลิปได้")
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=(
                        f"❌ ตรวจสอบสลิปไม่สำเร็จ\n"
                        f"สาเหตุ: {err_msg}\n\n"
                        "กรุณาตรวจสอบ:\n"
                        "• สลิปต้องเป็นสลิปจริง ไม่ใช่ภาพถ่ายหน้าจอซ้ำ\n"
                        "• รูปภาพต้องชัดเจน ครบถ้วน\n"
                        "• หากมีปัญหา กรุณาติดต่อเจ้าหน้าที่"
                    ))],
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
                    messages=[TextMessage(
                        text="📎 กรุณาส่งรูปสลิปเพื่อตรวจสอบการชำระเงิน\nหรือพิมพ์ 'บช' เพื่อดูเลขบัญชี"
                    )],
                )
            )


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)

        # ดึงรูปก่อน
        message_content = blob_api.get_message_content(event.message.id)
        image_data = message_content

        # ตอบกลับทันทีว่ากำลังตรวจสอบ
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="🔍 กำลังตรวจสอบสลิป กรุณารอสักครู่...")],
            )
        )

    # ประมวลผลใน background ไม่ block
    t = threading.Thread(target=process_slip_async, args=(user_id, image_data))
    t.daemon = True
    t.start()


@app.route("/", methods=["GET"])
def health():
    return "LINE Slip Bot is running! 🤖"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
