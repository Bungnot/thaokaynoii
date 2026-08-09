# app.py
import os
import json
import re
import requests
from datetime import datetime
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
OUR_ACCOUNT = "0748441328"

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

USED_SLIPS_FILE = "/tmp/used_slips.json"


# ─────────────────────────── Slip storage ────────────────────────────

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


# ─────────────────────────── Helpers ─────────────────────────────────

ACCOUNT_KEYWORDS = [
    "บช", "บัญชี", "account", "โอนเงิน",
    "เลขบัญชี", "ธนาคาร", "จ่ายเงิน", "ชำระ",
]


def _dig(d: dict, *paths):
    """ลองหลาย key-path แล้วคืนค่าแรกที่เจอ"""
    for path in paths:
        cur = d
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur not in (None, "", {}):
            return cur
    return ""


def _format_date(raw: str) -> str:
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return raw


def _get_receiver_account(receiver: dict) -> str:
    """ดึงเลขบัญชีผู้รับ รองรับทั้ง V1 และ V2 field"""
    return _dig(
        receiver,
        ["accountNo"],      # V2 /verify/bank
        ["account", "bank", "account"],     # V1 masked
        ["account", "proxy", "account"],    # V1 PromptPay
        ["account", "value"],
    )


def account_matches(slip_acc: str, our_acc: str) -> bool:
    """เทียบเลขบัญชี รองรับเลข mask เช่น xxx-x-x1234-x"""
    if not slip_acc:
        return True  # ไม่มีข้อมูล → ผ่าน (พึ่ง EasySlip dashboard config แทน)
    clean = re.sub(r"[-\s]", "", slip_acc)
    known_parts = [p for p in re.split(r"x+", clean, flags=re.IGNORECASE) if p]
    return all(p in our_acc for p in known_parts) if known_parts else True


# ─────────────────────────── EasySlip V2 API ─────────────────────────

def verify_slip_with_easyslip(image_content: bytes) -> dict:
    """
    เรียก EasySlip V2 endpoint: POST https://api.easyslip.com/v2/verify/bank
    Header: Authorization: Bearer <API_KEY>
    Body  : multipart/form-data  field "file"
    """
    url = "https://api.easyslip.com/v2/verify/bank"
    headers = {"Authorization": f"Bearer {EASY_SLIP_API_KEY}"}
    files = {"file": ("slip.jpg", image_content, "image/jpeg")}
    try:
        response = requests.post(url, headers=headers, files=files, timeout=30)
        data = response.json()
        # Log raw response ไว้ดูใน Cloud logs
        app.logger.info("EasySlip V2 raw response: %s",
                        json.dumps(data, ensure_ascii=False))
        return data
    except Exception as e:
        app.logger.error("EasySlip V2 request error: %s", str(e))
        return {"status": 500, "message": str(e)}


# ─────────────────────────── Flex builders ───────────────────────────

def flex_success(data: dict) -> dict:
    p = data.get("data", {})

    amount_val = _dig(p, ["amount", "amount"], ["amount"]) or 0
    try:
        amount = f"{float(amount_val):,.2f}"
    except Exception:
        amount = str(amount_val) or "-"

    sender = (
        _dig(p,["sender", "account", "name", "th"],
             ["sender", "account", "name", "en"],
             ["sender", "name"],
             ["sender", "displayName"]) or "-"
    )
    receiver = (
        _dig(p,
             ["receiver", "account", "name", "th"],
             ["receiver", "account", "name", "en"],
             ["receiver", "name"],
             ["receiver", "displayName"]) or "-"
    )
    bank = (
        _dig(p,
             ["receiver", "bank", "name"],
             ["receiver", "bank", "short"],
             ["receiver", "bankName"]) or "-"
    )
    date_str = _format_date(p.get("date", "") or p.get("transDate", ""))
    trans_ref = p.get("transRef", "") or p.get("referenceNo", "-")

    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#2ECC71",
            "contents": [{
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "✅", "size": "xl", "flex": 0},
                    {"type": "text", "text": " สลิปถูกต้อง", "size": "xl",
                     "weight": "bold", "color": "#ffffff", "flex": 1},
                ]
            }]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "lg",
            "backgroundColor": "#F0FFF4", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": f"฿{amount}", "size": "3xl",
                 "weight": "bold", "color": "#1a3c5e"},
                {"type": "separator", "color": "#C8E6C9"},
                {
                    "type": "box", "layout": "vertical", "spacing": "md",
                    "contents": [
                        {
                            "type": "box", "layout": "horizontal", "spacing": "md",
                            "contents": [
                                {
                                    "type": "box", "layout": "vertical", "flex": 1,
                                    "backgroundColor": "#E8F5E9", "cornerRadius": "12px",
                                    "paddingAll": "12px", "spacing": "xs",
                                    "contents": [
                                        {"type": "text", "text": "👤 ผู้โอน",
                                         "size": "xs", "color": "#666666"},
                                        {"type": "text", "text": sender, "size": "sm",
                                         "weight": "bold", "color": "#1a3c5e", "wrap": True},
                                    ]
                                },
                                {
                                    "type": "box", "layout": "vertical", "flex": 1,
                                    "backgroundColor": "#E8F5E9", "cornerRadius": "12px",
                                    "paddingAll": "12px", "spacing": "xs",
                                    "contents": [
                                        {"type": "text", "text": "🏦 ผู้รับ",
                                         "size": "xs", "color": "#666666"},
                                        {"type": "text", "text": receiver, "size": "sm",
                                         "weight": "bold", "color": "#1a3c5e", "wrap": True},
                                    ]
                                },
                            ]
                        },
                        {
                            "type": "box", "layout": "horizontal", "spacing": "md",
                            "contents": [
                                {
                                    "type": "box", "layout": "vertical", "flex": 1,
                                    "backgroundColor": "#E8F5E9", "cornerRadius": "12px",
                                    "paddingAll": "12px", "spacing": "xs",
                                    "contents": [
                                        {"type": "text", "text": "🏛 ธนาคาร",
                                         "size": "xs", "color": "#666666"},
                                        {"type": "text", "text": bank, "size": "sm",
                                         "weight": "bold", "color": "#1a3c5e", "wrap": True},
                                    ]
                                },
                                {
                                    "type": "box", "layout": "vertical", "flex": 1,
                                    "backgroundColor": "#E8F5E9", "cornerRadius": "12px",
                                    "paddingAll": "12px", "spacing": "xs",
                                    "contents": [
                                        {"type": "text", "text": "📅 วันที่",
                                         "size": "xs", "color": "#666666"},
                                        {"type": "text", "text": date_str, "size": "sm",
                                         "weight": "bold", "color": "#1a3c5e", "wrap": True},
                                    ]
                                },
                            ]
                        },
                        {
                            "type": "box", "layout": "vertical",
                            "backgroundColor": "#E8F5E9", "cornerRadius": "12px",
                            "paddingAll": "12px", "spacing": "xs",
                            "contents": [
                                {"type": "text", "text": "🔖 เลขอ้างอิง",
                                 "size": "xs", "color": "#666666"},
                                {"type": "text", "text": str(trans_ref), "size": "sm",
                                 "weight": "bold", "color": "#1a3c5e", "wrap": True},
                            ]
                        },
                    ]
                },
            ]
        },
        "footer": {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "backgroundColor": "#E8F5E9", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "🔒", "size": "sm", "flex": 0},
                {"type": "text", "text": "  ตรวจสอบโดยระบบอัตโนมัติ",
                 "size": "xs", "color": "#2ECC71", "weight": "bold"},
            ]
        }
    }


def _info_card(label: str, value: str, bg: str = "#FFF3F3") -> dict:
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "12px",
        "paddingAll": "12px", "spacing": "xs", "margin": "sm",
        "contents": [
            {"type": "text", "text": label, "size": "xs", "color": "#888888"},
            {"type": "text", "text": value or "-", "size": "sm",
             "weight": "bold", "color": "#333333", "wrap": True},
        ]
    }


def flex_wrong_account(receiver_account: str) -> dict:
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#E74C3C",
            "contents": [{
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "❌", "size": "xl", "flex": 0},
                    {"type": "text", "text": " บัญชีไม่ถูกต้อง", "size": "xl",
                     "weight": "bold", "color": "#ffffff", "flex": 1},
                ]
            }]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#FFF5F5", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "สลิปนี้โอนไปบัญชีอื่น ไม่ใช่บัญชีของเรา",
                 "size": "sm", "color": "#666666", "wrap": True},
                _info_card("🏦 บัญชีในสลิป", receiver_account or "-"),
                _info_card("✅ บัญชีที่ถูกต้อง", OUR_ACCOUNT, "#F0FFF4"),
                _info_card("🏛 ธนาคาร", "กสิกรไทย", "#F0FFF4"),
                _info_card("👤 ชื่อบัญชี", "กิตติเชษฐ์ บุญอินทร์", "#F0FFF4"),]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "backgroundColor": "#FFF0F0",
            "contents": [{"type": "text", "text": "พิมพ์ 'บช' เพื่อดูเลขบัญชีที่ถูกต้อง",
                          "size": "xs", "color": "#E74C3C", "align": "center"}]
        }
    }


def flex_duplicate(trans_ref: str) -> dict:
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#E67E22",
            "contents": [{
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "⚠️", "size": "xl", "flex": 0},
                    {"type": "text", "text": " สลิปซ้ำ", "size": "xl",
                     "weight": "bold", "color": "#ffffff", "flex": 1},
                ]
            }]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#FFFBF0", "spacing": "xs",
            "contents": [
                {"type": "text", "text": "สลิปนี้เคยถูกใช้งานไปแล้ว",
                 "size": "sm", "color": "#666666", "wrap": True},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#FFF3E0",
                    "cornerRadius": "12px", "paddingAll": "12px", "margin": "md",
                    "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": "🔖 เลขอ้างอิง",
                         "size": "xs", "color": "#888888"},
                        {"type": "text", "text": str(trans_ref), "size": "sm",
                         "weight": "bold", "color": "#333333", "wrap": True},
                    ]
                },
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "backgroundColor": "#FFF3E0",
            "contents": [{"type": "text",
                          "text": "กรุณาส่งสลิปใหม่ หรือติดต่อเจ้าหน้าที่",
                          "size": "xs", "color": "#E67E22", "align": "center"}]
        }
    }


def flex_pending() -> dict:
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#8E44AD",
            "contents": [{
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "⏳", "size": "xl", "flex": 0},
                    {"type": "text", "text": " กำลังประมวลผล", "size": "xl",
                     "weight": "bold", "color": "#ffffff", "flex": 1},
                ]
            }]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#FAF5FF", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "ธนาคารยังประมวลผลสลิปไม่เสร็จ",
                 "size": "sm", "color": "#666666", "wrap": True},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#EDE7F6",
                    "cornerRadius": "12px", "paddingAll": "12px", "margin": "md",
                    "contents": [
                        {"type": "text",
                         "text": "💡 รอ 1-2 นาที แล้วส่งสลิปใหม่อีกครั้ง",
                         "size": "sm", "color": "#6A1B9A", "wrap": True},
                    ]
                },
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "backgroundColor": "#EDE7F6",
            "contents": [{"type": "text",
                          "text": "พบบ่อยในสลิปธนาคารกรุงเทพ / กรุงไทย",
                          "size": "xs", "color": "#8E44AD", "align": "center"}]
        }
    }


def flex_error(reason: str) -> dict:
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#7F8C8D",
            "contents": [{
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "❌", "size": "xl", "flex": 0},
                    {"type": "text", "text": " ตรวจสอบไม่สำเร็จ", "size": "xl",
                     "weight": "bold", "color": "#ffffff", "flex": 1},
                ]
            }]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#F8F9FA", "spacing": "xs",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#ECEFF1",
                    "cornerRadius": "12px", "paddingAll": "12px", "margin": "sm",
                    "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": "สาเหตุ", "size": "xs", "color": "#888888"},
                        {"type": "text",
                         "text": reason or "ไม่สามารถตรวจสอบสลิปได้",
                         "size": "sm", "weight": "bold", "color": "#333333", "wrap": True},
                    ]
                },{
                    "type": "box", "layout": "vertical", "backgroundColor": "#ECEFF1",
                    "cornerRadius": "12px", "paddingAll": "12px", "margin": "sm",
                    "contents": [
                        {"type": "text",
                         "text": "• สลิปต้องชัดเจน ไม่ใช่ภาพถ่ายซ้ำ\n• รูปต้องครบถ้วน ไม่ถูกตัด",
                         "size": "sm", "color": "#555555", "wrap": True},]
                },
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "backgroundColor": "#ECEFF1",
            "contents": [{"type": "text", "text": "หากมีปัญหา กรุณาติดต่อเจ้าหน้าที่",
                          "size": "xs", "color": "#7F8C8D", "align": "center"}]
        }
    }


def flex_account() -> dict:
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#2980B9",
            "contents": [{
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "🏦", "size": "xl", "flex": 0},
                    {"type": "text", "text": " บัญชีรับโอนเงิน", "size": "xl",
                     "weight": "bold", "color": "#ffffff", "flex": 1},
                ]
            }]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "20px",
            "backgroundColor": "#F0F8FF", "spacing": "sm",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#DBEAFE",
                    "cornerRadius": "16px", "paddingAll": "16px", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "กสิกรไทย (KBANK)",
                         "size": "md", "weight": "bold", "color": "#1a3c5e"},
                        {"type": "text", "text": "0748441328",
                         "size": "xxl", "weight": "bold", "color": "#2980B9"},
                        {"type": "text", "text": "กิตติเชษฐ์ บุญอินทร์",
                         "size": "sm", "color": "#555555"},
                    ]
                },
                {
                    "type": "box", "layout": "horizontal", "backgroundColor": "#FFF3CD",
                    "cornerRadius": "10px", "paddingAll": "10px", "margin": "md",
                    "contents": [
                        {"type": "text", "text": "⚠️ ", "flex": 0, "size": "sm"},
                        {"type": "text",
                         "text": "ชื่อผู้ฝาก-ถอน ต้องเป็นชื่อเดียวกันเท่านั้น",
                         "size": "xs", "color": "#856404", "flex": 1, "wrap": True},
                    ]
                }
            ]
        }
    }


# ─────────────────────────── Reply helper ────────────────────────────

def reply_flex(reply_token: str, alt: str, body: dict):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(
                    alt_text=alt,
                    contents=FlexContainer.from_dict(body),
                )],
            )
        )


# ─────────────────────────── Routes ──────────────────────────────────

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
                        text="📎 กรุณาส่งรูปสลิปเพื่อตรวจสอบการชำระเงิน\n"
                             "หรือพิมพ์ 'บช' เพื่อดูเลขบัญชี"
                    )],
                )
            )


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    # 1. ดึงรูปจาก LINE
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_data = blob_api.get_message_content(event.message.id)

    # 2. ส่งไปตรวจที่ EasySlip V2
    result = verify_slip_with_easyslip(image_data)
    status = result.get("status", 500)

    if status == 200:
        payment = result.get("data", {})
        trans_ref = payment.get("transRef", "") or payment.get("referenceNo", "")

        # 3. ตรวจเลขบัญชีผู้รับ
        receiver = payment.get("receiver", {})
        receiver_account = _get_receiver_account(receiver)

        if not account_matches(receiver_account, OUR_ACCOUNT):
            reply_flex(event.reply_token, "❌ บัญชีผู้รับไม่ถูกต้อง",
                       flex_wrong_account(receiver_account))
            return

        # 4. ตรวจสลิปซ้ำ
        if trans_ref and is_slip_used(trans_ref):
            reply_flex(event.reply_token, "⚠️ สลิปซ้ำ", flex_duplicate(trans_ref))
            return

        # 5. บันทึกและแจ้งสำเร็จ
        if trans_ref:
            save_used_slip(trans_ref)reply_flex(event.reply_token, "✅ ตรวจสอบสลิปสำเร็จ", flex_success(result))

    else:
        err = result.get("message", "") or result.get("error", "")
        if "pending" in str(err).lower():
            reply_flex(event.reply_token, "⏳ สลิปอยู่ระหว่างประมวลผล", flex_pending())
        else:
            reply_flex(event.reply_token, "❌ ตรวจสอบไม่สำเร็จ",
                       flex_error(err or "ไม่สามารถตรวจสอบสลิปได้"))


@app.route("/", methods=["GET"])
def health():
    return "LINE Slip Bot V2 is running! 🤖"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
