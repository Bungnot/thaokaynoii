import os
import hmac
import hashlib
import base64
import re
import csv
import threading
import time
import sqlite3
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# เวลาไทยสำหรับกติกา “สลิปใช้ได้เฉพาะวันปัจจุบัน”
# ประเทศไทยใช้ UTC+7 ตลอดปี (ไม่มี DST)
THAI_TZ = timezone(timedelta(hours=7))

# =========================
# Environment variables
# =========================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
EASYSLIP_API_KEY = os.getenv("EASYSLIP_API_KEY", "").strip()

# EasySlip V2
EASYSLIP_VERIFY_URL = "https://api.easyslip.com/v2/verify/bank"

# Account shown by command "บช" (สลับไปเรื่อยๆ ทุกครั้งที่ลูกค้าพิม)
ACCOUNTS = [
    {
        "number": "074-843-7118",
        "bank": "กสิกรไทย",
        "bank_short": "KBANK",
        "name": "ธนะวัฒน์ ครองยุติ",
    },
    {
        "number": "020480908233",
        "bank": "ออมสิน",
        "bank_short": "GSB",
        "name": "ครรชิต ครองยุติ",
    },
]

# ตัวนับสำหรับสลับบัญชี (thread-safe)
_ACCOUNT_INDEX = 0
_ACCOUNT_INDEX_LOCK = threading.Lock()


def get_accounts_ordered() -> list:
    """
    คืนบัญชีทั้ง 2 ใบ โดยสลับลำดับทุกครั้งที่ถูกเรียก (thread-safe)
    ครั้งคี่  → [กสิกร, ออมสิน]
    ครั้งคู่  → [ออมสิน, กสิกร]
    """
    global _ACCOUNT_INDEX
    with _ACCOUNT_INDEX_LOCK:
        idx = _ACCOUNT_INDEX % len(ACCOUNTS)
        _ACCOUNT_INDEX += 1
    return ACCOUNTS[idx:] + ACCOUNTS[:idx]


# ค่า backward-compat (ใช้ใน verify_with_easyslip / ACCOUNT_MESSAGE เดิม)
ACCOUNT_NUMBER = ACCOUNTS[0]["number"]
ACCOUNT_BANK = ACCOUNTS[0]["bank"]
ACCOUNT_BANK_SHORT = ACCOUNTS[0]["bank_short"]
ACCOUNT_NAME = ACCOUNTS[0]["name"]

# EasySlip V2 Account Matching
VERIFY_MATCH_ACCOUNT = os.getenv("VERIFY_MATCH_ACCOUNT", "true").lower() == "true"

# Maximum image size supported by EasySlip V2 = 4 MB
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# กันลูกค้าส่งหลายรูปติดกันในแชท 1-1 แล้วกินโควต้า EasySlip หลายครั้ง
# จะตรวจเฉพาะรูปแรกในช่วงเวลานี้ รูปถัดไปจะเงียบและไม่เรียก EasySlip
try:
    PRIVATE_IMAGE_BURST_SECONDS = max(1.0, float(os.getenv("PRIVATE_IMAGE_BURST_SECONDS", "10")))
except (TypeError, ValueError):
    PRIVATE_IMAGE_BURST_SECONDS = 10.0

# Railway Volume + SQLite
# Railway จะสร้าง RAILWAY_VOLUME_MOUNT_PATH ให้อัตโนมัติเมื่อ Volume ถูก mount กับ service
VOLUME_MOUNT_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
HAS_RAILWAY_VOLUME = bool(VOLUME_MOUNT_PATH)

# ถ้ารัน local ให้ fallback ไปโฟลเดอร์ data ข้าง app.py
DATA_DIR = (
    VOLUME_MOUNT_PATH
    if HAS_RAILWAY_VOLUME
    else os.path.join(os.path.dirname(__file__), "data")
)
os.makedirs(DATA_DIR, exist_ok=True)

SQLITE_PATH = os.path.join(DATA_DIR, "bot.db")

# =========================
# PEH / เปะ (เฉพาะฟีเจอร์นี้จากไฟล์อ้างอิง)
# =========================
_DEFAULT_ADMIN_UIDS = {
    "U255dd67c1fef32fb0eae127149c7cadc",
    "Uf7e207bfdd69d8e41806436fa7a86c14",
    "U163186c5013c8f1e4820291b7b1d86bd",
    "Uc2013ea8397da6d19cbe0f931a04c949",
    "U2f156aa5effee7c1ee349b9320a35381"
}

# ถ้าตั้ง ADMIN_UIDS ใน Railway จะใช้ค่าจาก Railway แทนค่าเดิม
# รูปแบบ: Uxxxx,Uyyyy,Uzzzz
_admin_env = os.getenv("ADMIN_UIDS", "").strip()
ADMIN_UIDS = (
    {uid.strip() for uid in _admin_env.split(",") if uid.strip()}
    if _admin_env
    else _DEFAULT_ADMIN_UIDS
)

PEH_LIST = {}   # dict[source_key] = ["ข้อความ..."]
TARGET_GROUP_NAME = os.getenv(
    "TARGET_GROUP_NAME",
    "🚀บั้งไฟน้อย • เถ้าแก่น้อย •"
).strip()



# =========================
# UID directory / รายชื่อผู้ที่เคยทักบอท
# =========================
USERS_TXT_PATH = os.path.join(DATA_DIR, "oa_users.txt")
_USERS_LOCK = threading.Lock()

# =========================
# Railway Volume + SQLite
# =========================
_DB_LOCK = threading.RLock()

# กัน image burst ต่อผู้ใช้ในแชท 1-1
_PRIVATE_IMAGE_BURST_LOCK = threading.Lock()
_PRIVATE_IMAGE_LAST_ACCEPTED = {}  # dict[user_id] = monotonic timestamp


def _claim_private_image_slot(user_id: str) -> bool:
    """
    True  = รูปแรกของชุด ส่งเข้าตรวจได้
    False = รูปถัดไปที่ส่งติดกัน ให้เงียบและไม่เรียก EasySlip
    """
    uid = str(user_id or "").strip()
    if not uid:
        # source type=user ปกติควรมี userId; ถ้าไม่มีให้เงียบเพื่อไม่เสี่ยงเสียโควต้า
        return False

    now = time.monotonic()

    with _PRIVATE_IMAGE_BURST_LOCK:
        last = _PRIVATE_IMAGE_LAST_ACCEPTED.get(uid)
        if last is not None and (now - last) < PRIVATE_IMAGE_BURST_SECONDS:
            return False

        _PRIVATE_IMAGE_LAST_ACCEPTED[uid] = now

        # ป้องกัน dict โตค้างนาน หากมีผู้ใช้จำนวนมาก
        if len(_PRIVATE_IMAGE_LAST_ACCEPTED) > 5000:
            stale_before = now - max(60.0, PRIVATE_IMAGE_BURST_SECONDS * 10)
            stale_uids = [
                saved_uid
                for saved_uid, ts in _PRIVATE_IMAGE_LAST_ACCEPTED.items()
                if ts < stale_before
            ]
            for saved_uid in stale_uids:
                _PRIVATE_IMAGE_LAST_ACCEPTED.pop(saved_uid, None)

    return True


def get_db():
    """เปิด SQLite ที่อยู่บน Railway Volume"""
    conn = sqlite3.connect(
        SQLITE_PATH,
        timeout=20,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    # เหมาะกับ bot ที่มี concurrent requests เล็กน้อย
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def init_db():
    """สร้างฐานข้อมูลถาวรบน Volume"""
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS used_slips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trans_ref TEXT UNIQUE NOT NULL,
                amount REAL,
                sender_name TEXT,
                sender_bank TEXT,
                receiver_name TEXT,
                receiver_bank TEXT,
                slip_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS line_users (
                uid TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_admins (
                uid TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                added_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Seed แอดมินเดิมเข้า SQLite
        for uid in ADMIN_UIDS:
            cur.execute(
                """
                INSERT OR IGNORE INTO bot_admins
                    (uid, display_name, added_by)
                VALUES (?, ?, ?)
                """,
                (uid, "", "bootstrap"),
            )

        # โหลดแอดมินถาวรกลับเข้า memory
        cur.execute("SELECT uid FROM bot_admins")
        for row in cur.fetchall():
            saved_uid = str(row["uid"] or "").strip()
            if saved_uid:
                ADMIN_UIDS.add(saved_uid)

        conn.commit()
        cur.close()

        storage_type = "Railway Volume" if HAS_RAILWAY_VOLUME else "local non-persistent storage"
        print(
            f"[DB] SQLite ready: {SQLITE_PATH} "
            f"({storage_type}), admins={len(ADMIN_UIDS)}"
        )
    except Exception as exc:
        print("[DB] SQLite init error:", exc)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def _upsert_line_user_db(uid: str, display_name: str):
    """เก็บ UID + ชื่อใน SQLite"""
    if not uid:
        return

    conn = None
    try:
        with _DB_LOCK:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO line_users (uid, display_name, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(uid)
                DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (uid, display_name or ""),
            )
            conn.commit()
            cur.close()
    except Exception as exc:
        print("[DB] line_users upsert error:", exc)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def _search_users_db(name_query: str, limit: int = 10):
    """ค้น UID จาก display name ใน SQLite"""
    query = (name_query or "").strip()
    if not query:
        return []

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT uid, display_name
            FROM line_users
            WHERE display_name LIKE ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (f"%{query}%", int(limit)),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            (str(row["uid"]), str(row["display_name"] or ""))
            for row in rows
        ]
    except Exception as exc:
        print("[DB] user search error:", exc)
        return []
    finally:
        if conn:
            conn.close()


def is_admin_uid(uid: str) -> bool:
    """
    ตรวจสิทธิ์แอดมินจาก memory และ SQLite
    """
    if not uid:
        return False

    if uid in ADMIN_UIDS:
        return True

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM bot_admins WHERE uid = ? LIMIT 1",
            (uid,),
        )
        found = cur.fetchone() is not None
        cur.close()

        if found:
            ADMIN_UIDS.add(uid)

        return found
    except Exception as exc:
        print("[DB] admin lookup error:", exc)
        return False
    finally:
        if conn:
            conn.close()


def add_admin_persistent(uid: str, display_name: str, added_by: str):
    """
    เพิ่มแอดมินลง SQLite บน Railway Volume
    return: (ok, already_exists, message)
    """
    if not HAS_RAILWAY_VOLUME:
        return (
            False,
            False,
            "ไม่พบ Railway Volume ใน runtime "
            "(RAILWAY_VOLUME_MOUNT_PATH ไม่มีค่า)"
        )

    if not uid:
        return False, False, "ไม่พบ UID ของผู้ใช้"

    conn = None
    try:
        with _DB_LOCK:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                "SELECT 1 FROM bot_admins WHERE uid = ? LIMIT 1",
                (uid,),
            )
            already = cur.fetchone() is not None

            cur.execute(
                """
                INSERT INTO bot_admins (uid, display_name, added_by)
                VALUES (?, ?, ?)
                ON CONFLICT(uid)
                DO UPDATE SET
                    display_name = excluded.display_name
                """,
                (uid, display_name or "", added_by or ""),
            )

            conn.commit()
            cur.close()

        ADMIN_UIDS.add(uid)
        return True, already, "OK"

    except Exception as exc:
        if conn:
            conn.rollback()
        print("[DB] add admin error:", exc)
        return False, False, str(exc)
    finally:
        if conn:
            conn.close()


def list_admins_persistent():
    """คืนรายชื่อแอดมินถาวรจาก SQLite พร้อมชื่อที่บอทเคยเก็บไว้"""
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.uid AS uid,
                COALESCE(
                    NULLIF(a.display_name, ''),
                    NULLIF(u.display_name, ''),
                    ''
                ) AS display_name,
                a.added_by AS added_by,
                a.created_at AS created_at
            FROM bot_admins AS a
            LEFT JOIN line_users AS u ON u.uid = a.uid
            ORDER BY a.created_at ASC, a.uid ASC
            """
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "uid": str(row["uid"] or ""),
                "display_name": str(row["display_name"] or ""),
                "added_by": str(row["added_by"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
            if str(row["uid"] or "").strip()
        ]
    except Exception as exc:
        print("[DB] list admins error:", exc)
        # fallback ให้คำสั่งยังใช้ได้ แม้ DB อ่านพลาดชั่วคราว
        return [
            {"uid": uid, "display_name": "", "added_by": "", "created_at": ""}
            for uid in sorted(ADMIN_UIDS)
        ]
    finally:
        if conn:
            conn.close()


def claim_trans_ref(data: dict) -> bool:
    """
    กันสลิปซ้ำอีกชั้นด้วย SQLite
    True  = transRef ใหม่
    False = transRef เคยใช้แล้ว
    """
    raw = data.get("rawSlip") or {}
    trans_ref = str(raw.get("transRef") or "").strip()

    if not trans_ref:
        return True

    amount = safe_float(((raw.get("amount") or {}).get("amount")))
    sender = raw.get("sender") or {}
    receiver = raw.get("receiver") or {}

    sender_name = get_party_name(sender)
    receiver_name = get_party_name(receiver)
    sender_bank = ((sender.get("bank") or {}).get("short") or "").strip()
    receiver_bank = ((receiver.get("bank") or {}).get("short") or "").strip()
    slip_date = str(raw.get("date") or "")

    conn = None
    try:
        with _DB_LOCK:
            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                INSERT OR IGNORE INTO used_slips
                    (
                        trans_ref, amount, sender_name, sender_bank,
                        receiver_name, receiver_bank, slip_date
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
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

            inserted = cur.rowcount == 1
            conn.commit()
            cur.close()
            return inserted

    except Exception as exc:
        print("[DB] claim_trans_ref error:", exc)
        if conn:
            conn.rollback()

        # EasySlip V2 ยังมี checkDuplicate=true อยู่ จึงไม่ทำให้ bot ล่ม
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


def push_line(to: str, messages: list):
    """ส่ง push message (ไม่ต้องใช้ replyToken)"""
    url = "https://api.line.me/v2/bot/message/push"
    payload = {
        "to": to,
        "messages": messages[:5],
    }

    resp = requests.post(
        url,
        headers=line_headers(),
        json=payload,
        timeout=15,
    )

    if not resp.ok:
        print("[LINE] push failed:", resp.status_code, resp.text)

    return resp


def text_message(text: str) -> dict:
    return {
        "type": "text",
        "text": text,
    }


def build_account_message(acc: dict) -> str:
    return (
        f"━━━━━━━━━━━━━━\n"
        f"🏦 แจ้งเลขบัญชีฝากเงิน\n"
        f"🔢 เลขบัญชี : {acc['number']}\n"
        f"🏛 ธนาคาร : {acc['bank']}\n"
        f"👤 ชื่อบัญชี : {acc['name']}\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚠️ เพื่อป้องกันมิจฉาชีพ\n"
        f"ชื่อผู้ฝาก-ถอน ต้องเป็นชื่อเดียวกันเท่านั้น ✅"
    )


# backward-compat (ใช้จุดอื่นที่อ้าง ACCOUNT_MESSAGE โดยตรง)
ACCOUNT_MESSAGE = build_account_message(ACCOUNTS[0])


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


def parse_slip_datetime_th(value: str):
    """แปลง rawSlip.date ของ EasySlip เป็น datetime เวลาไทย (UTC+7)"""
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

    # ถ้า API ส่ง datetime ที่ไม่มี timezone ให้ถือว่าเป็นเวลาไทย
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=THAI_TZ)

    return dt.astimezone(THAI_TZ)


def validate_slip_is_today(data: dict):
    """
    ตรวจว่าสลิปเป็น “วันนี้” ตามเวลาไทยหรือไม่
    return: (is_valid, detail_message)
    """
    raw_slip = (data or {}).get("rawSlip") or {}
    slip_date_raw = str(raw_slip.get("date") or "").strip()
    slip_dt = parse_slip_datetime_th(slip_date_raw)

    today_th = datetime.now(THAI_TZ).date()

    if slip_dt is None:
        return (
            False,
            "ไม่สามารถอ่านวันที่จากสลิปได้\n"
            "ระบบรับเฉพาะสลิปที่โอนภายในวันนี้เท่านั้น",
        )

    if slip_dt.date() != today_th:
        return (
            False,
            f"สลิปนี้เป็นวันที่ {slip_dt.strftime('%d/%m/%Y')}\n"
            f"รับเฉพาะสลิปของวันนี้ {today_th.strftime('%d/%m/%Y')} เท่านั้น",
        )

    return True, ""


def display_date(value: str) -> str:
    if not value:
        return "-"

    dt = parse_slip_datetime_th(value)
    if dt is not None:
        return dt.strftime("%d/%m/%Y %H:%M")

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
        "size": "giga",
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
    "slip_wrong_date": (
        "สลิปไม่ใช่ของวันนี้",
        "ระบบรับเฉพาะสลิปที่โอนภายในวันนี้เท่านั้น",
    ),
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
        "หากเป็นธนาคารกรุงเทพ กรุณารอสักครู่ 1-2 นาทีแล้วลองใหม่",
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

    # กรณีวันสลิปไม่ตรง ใช้รายละเอียดแบบไดนามิกเพื่อบอกทั้งวันสลิปและวันนี้
    if normalized == "slip_wrong_date" and detail:
        desc = detail

    # Dedicated copy for duplicate / wrong-date slips
    if normalized == "duplicate_slip":
        badge = "!"
        header = "#E53935"
    elif normalized == "slip_wrong_date":
        badge = "!"
        header = "#F59E0B"
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
# UID helpers
# =========================
def _line_get_json(url: str):
    """GET LINE profile endpoint และคืน dict; ถ้าพลาดคืน {}"""
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=10,
        )
        if resp.ok:
            return resp.json()
        print("[LINE] profile lookup failed:", resp.status_code, resp.text[:300])
    except Exception as exc:
        print("[LINE] profile lookup error:", exc)
    return {}


def _display_name_from_event(event: dict) -> str:
    """
    ดึง displayName ของผู้ส่ง
    - กลุ่ม: group member profile
    - room: room member profile
    - แชทส่วนตัว: profile
    """
    source = event.get("source") or {}
    user_id = source.get("userId")
    if not user_id:
        return ""

    source_type = source.get("type")
    group_id = source.get("groupId")
    room_id = source.get("roomId")

    if source_type == "group" and group_id:
        data = _line_get_json(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"
        )
    elif source_type == "room" and room_id:
        data = _line_get_json(
            f"https://api.line.me/v2/bot/room/{room_id}/member/{user_id}"
        )
    else:
        data = _line_get_json(
            f"https://api.line.me/v2/bot/profile/{user_id}"
        )

    return str(data.get("displayName") or "").strip()


def _load_users_txt() -> dict:
    """อ่าน oa_users.txt คืนค่า dict[uid] = display_name"""
    data = {}
    if not os.path.exists(USERS_TXT_PATH):
        return data

    try:
        with open(USERS_TXT_PATH, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter="\t")
            for row in reader:
                if not row:
                    continue
                uid = row[0].strip()
                name = row[1].strip() if len(row) > 1 else ""
                if uid:
                    data[uid] = name
    except Exception as exc:
        print("[UID] load error:", exc)

    return data


def _save_user_to_txt(uid: str, display_name: str):
    """บันทึก/อัปเดต UID และชื่อแบบ atomic"""
    if not uid:
        return

    display_name = display_name or ""

    with _USERS_LOCK:
        data = _load_users_txt()

        if data.get(uid) == display_name:
            return

        data[uid] = display_name
        tmp_path = USERS_TXT_PATH + ".tmp"

        try:
            with open(tmp_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, delimiter="\t")
                for saved_uid in sorted(data.keys()):
                    writer.writerow([saved_uid, data[saved_uid]])

            os.replace(tmp_path, USERS_TXT_PATH)
        except Exception as exc:
            print("[UID] save error:", exc)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    # เก็บลง PostgreSQL อีกชั้น เพื่อไม่หายหลัง reboot/redeploy
    _upsert_line_user_db(uid, display_name)


def _search_uid_by_name(name_query: str, limit: int = 10):
    """ค้นชื่อจาก PostgreSQL ก่อน แล้ว fallback ไป oa_users.txt"""
    raw_query = (name_query or "").strip()
    query = raw_query.casefold()

    if not query:
        return []

    results = []
    seen = set()

    # 1) PostgreSQL (ถาวร)
    for uid, name in _search_users_db(raw_query, limit=limit):
        if uid not in seen:
            results.append((uid, name))
            seen.add(uid)
        if len(results) >= limit:
            return results

    # 2) local txt fallback
    data = _load_users_txt()
    for uid, name in data.items():
        if query in (name or "").casefold() and uid not in seen:
            results.append((uid, name))
            seen.add(uid)
        if len(results) >= limit:
            break

    return results


def _extract_mentioned_user_ids(event: dict):
    """
    ดึง userId จาก LINE mention metadata
    รองรับกรณีแอดมินใช้ @mention จริงในกลุ่ม
    """
    message = event.get("message") or {}
    mention = message.get("mention") or {}
    mentionees = mention.get("mentionees") or []

    user_ids = []
    for item in mentionees:
        if item.get("type") != "user":
            continue

        # ไม่เอา mention ที่ชี้มาที่ตัว bot เอง
        if item.get("isSelf") is True:
            continue

        uid = str(item.get("userId") or "").strip()
        if uid and uid not in user_ids:
            user_ids.append(uid)

    return user_ids


def _display_name_for_uid(event: dict, target_uid: str) -> str:
    """พยายามอ่านชื่อ LINE ของ UID เป้าหมายจากบริบทห้อง/กลุ่ม"""
    if not target_uid:
        return ""

    source = event.get("source") or {}
    source_type = source.get("type")
    group_id = source.get("groupId")
    room_id = source.get("roomId")

    if source_type == "group" and group_id:
        data = _line_get_json(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{target_uid}"
        )
    elif source_type == "room" and room_id:
        data = _line_get_json(
            f"https://api.line.me/v2/bot/room/{room_id}/member/{target_uid}"
        )
    else:
        data = _line_get_json(
            f"https://api.line.me/v2/bot/profile/{target_uid}"
        )

    name = str(data.get("displayName") or "").strip()

    if name:
        _save_user_to_txt(target_uid, name)

    return name




# =========================
# PEH helpers
# =========================
def _source_key(event: dict) -> str:
    source = event.get("source") or {}
    return (
        source.get("groupId")
        or source.get("roomId")
        or source.get("userId")
        or "global"
    )


def _add_peh_item(event: dict, text: str):
    """
    เพิ่มรายการ PEH ลงในห้อง/กลุ่ม/แชทนั้น ๆ
    หากชื่อซ้ำจะใส่ (2), (3), (4)... ต่อท้ายอัตโนมัติ
    """
    key = _source_key(event)

    if key not in PEH_LIST:
        PEH_LIST[key] = []

    # ตรวจสอบชื่อซ้ำ: เปรียบเทียบแบบ case-sensitive ตรงๆ
    existing = PEH_LIST[key]

    # สร้าง set ของรายการที่มีอยู่แล้ว (ข้อความดิบ)
    existing_set = set(existing)

    # ถ้าชื่อซ้ำ ให้ใส่ (2), (3), ... ต่อท้าย
    candidate = text
    counter = 2
    while candidate in existing_set:
        candidate = f"{text} ({counter})"
        counter += 1

    PEH_LIST[key].append(candidate)
    return PEH_LIST[key]


# ====== สถานะของรายการ "เปะ" ======
PEH_STATUS = {
    "✅": {"key": "win",  "label": "ชนะ"},
    "❌": {"key": "lose", "label": "แพ้"},
    "⛔": {"key": "draw", "label": "จาว"},
}

# จำนวนรายการต่อ 1 หน้าใน Carousel
# 20 รายการ/หน้า × 4 หน้า × 2 carousel = 120 รายการ
PEH_ITEMS_PER_PAGE = 20

# LINE Carousel รองรับสูงสุด 12 bubbles ต่อ 1 carousel
# แต่จำกัดไว้ที่ 4 bubbles (~40KB) เพื่อไม่เกิน 50KB
# 2 carousel × 4 bubbles × 20 items = 160 รายการสูงสุด
PEH_MAX_BUBBLES_PER_CAROUSEL = 6


def _peh_parse_status(item: str):
    """
    อ่านสถานะจาก emoji:
      ✅ = ชนะ
      ❌ = แพ้
      ⛔ = จาว

    กติกา:
    - 1 รายการนับเป็น 1 ผลเท่านั้น ไม่ได้นับตามจำนวน emoji
    - ใน FLEX แสดง emoji ของผลนั้นสูงสุด 2 ตัว
    - ถ้าเผลอใส่หลายชนิดในบรรทัดเดียว จะยึดชนิดที่ปรากฏก่อนสุด
    """
    raw = str(item or "").strip()

    found = []
    for symbol in PEH_STATUS:
        pos = raw.find(symbol)
        if pos >= 0:
            found.append((pos, symbol))

    if not found:
        clean = re.sub(r"\s+", " ", raw).strip()
        return None, clean

    _, symbol = min(found, key=lambda x: x[0])

    # ลบ emoji สถานะทุกชนิดออกก่อน
    clean_text = re.sub(r"[✅❌⛔]+", "", raw)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()

    # ต่อให้พิมพ์ 3, 5, 10 ตัว ก็แสดงสูงสุด 2 ตัว
    symbol_count = min(2, max(1, raw.count(symbol)))
    # ไม่ใส่ space ระหว่างข้อความกับ emoji เพื่อกัน LINE ตัดบรรทัด
    display_text = f"{clean_text}{symbol * symbol_count}"

    return symbol, display_text



def _peh_split_row(display_text: str, symbol: str | None):
    """
    แยกข้อความสำหรับแสดงในตาราง:
      "เทสระบบ3 320❌❌"
    เป็น
      left  = "เทสระบบ3"
      right = "320❌❌"

    รองรับตัวเลขเช่น 320, 1,250, 320.50
    ถ้าไม่พบตัวเลขท้ายข้อความ จะให้ข้อความทั้งหมดอยู่ฝั่งซ้าย
    และ emoji อยู่ฝั่งขวา
    """
    text = str(display_text or "").strip()

    # ตัด emoji สถานะออกชั่วคราว เพื่อหาเลขท้าย
    clean = re.sub(r"[✅❌⛔️\ufe0f]+", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()

    # ตัด suffix ซ้ำ เช่น " (2)", " (3)" ออกก่อนหาตัวเลข แล้วเก็บไว้ใส่ฝั่งซ้ายคืน
    dup_suffix = ""
    dup_m = re.search(r"\s*\(\d+\)$", clean)
    if dup_m:
        dup_suffix = dup_m.group(0)
        clean = clean[: dup_m.start()]

    m = re.match(r"^(.*?)(?:\s+)([-+]?\d[\d,]*(?:\.\d+)?)$", clean)

    emoji_text = ""
    if symbol:
        count = min(2, max(1, text.count(symbol)))
        emoji_text = symbol * count

    if m:
        left = m.group(1).strip() + dup_suffix
        amount = m.group(2).strip()
        # ติดเลขกับ emoji ไม่มีช่องว่าง เพื่อกัน LINE ตัดบรรทัดระหว่างกัน
        right = f"{amount}{emoji_text}" if emoji_text else amount
        return left, right

    return (clean + dup_suffix).strip(), emoji_text


def _peh_build_stats(items):
    stats = {"win": 0, "lose": 0, "draw": 0}
    parsed_items = []

    for item in items:
        symbol, display_text = _peh_parse_status(item)
        if symbol:
            stats[PEH_STATUS[symbol]["key"]] += 1
        parsed_items.append((display_text, symbol))

    return stats, parsed_items


def _peh_stat_cell(title: str, value: int, emoji: str) -> dict:
    """ช่องสรุปหัวตารางแบบขาว เรียบ สบายตา"""
    return {
        "type": "box",
        "layout": "vertical",
        "flex": 1,
        "paddingAll": "5px",
        "contents": [
            {
                "type": "text",
                "text": title,
                "size": "xxs",
                "color": "#7A8790",
                "align": "center",
                "weight": "bold",
            },
            {
                "type": "text",
                "text": f"{emoji} {value}",
                "size": "sm",
                "color": "#1F2937",
                "align": "center",
                "weight": "bold",
                "margin": "xs",
            },
        ],
    }


def _peh_row(number: int, display_text: str, symbol: str | None) -> dict:
    """slim row — ตัด field ที่ไม่จำเป็นออกเพื่อลดขนาด JSON ให้ 6 bubble อยู่ใน 50KB"""
    number_color = {
        "✅": "#15803D",
        "❌": "#B91C1C",
        "⛔": "#A16207",
    }.get(symbol, "#60717C")

    left_text, right_text = _peh_split_row(display_text, symbol)

    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {
                "type": "text",
                "text": str(number),
                "size": "xs",
                "color": number_color,
                "align": "center",
                "flex": 1,
            },
            {
                "type": "text",
                "text": left_text,
                "size": "xs",
                "wrap": True,
                "flex": 5,
            },
            {
                "type": "text",
                "text": right_text,
                "size": "xs",
                "color": "#24313A",
                "align": "end",
                "flex": 4,
            },
        ],
    }



def _peh_page_bubble(page_items, page_no: int, page_total: int, stats: dict, total: int):
    """
    1 bubble = 1 หน้า
    - หน้า 1: แสดงหัว + สรุป ชนะ/แพ้/จาว
    - หน้า 2 เป็นต้นไป: แสดงเฉพาะหัว + เลขหน้า + รายการ
    """
    rows = []

    for idx, (display_text, symbol) in enumerate(page_items):
        global_no = (page_no - 1) * PEH_ITEMS_PER_PAGE + idx + 1
        rows.append(_peh_row(global_no, display_text, symbol))

        if idx != len(page_items) - 1:
            rows.append({"type": "separator"})

    if not rows:
        rows = [{
            "type": "text",
            "text": "ยังไม่มีรายการ",
            "size": "sm",
            "color": "#94A3AB",
            "align": "center",
            "margin": "lg",
        }]

    # -------------------------
    # Header ทุกหน้า
    # -------------------------
    header_contents = [
        {
            "type": "box",
            "layout": "horizontal",
            "alignItems": "center",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "width": "5px",
                    "height": "26px",
                    "cornerRadius": "8px",
                    "backgroundColor": "#62B89B",
                    "contents": []
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "sm",
                    "flex": 1,
                    "contents": [
                        {
                            "type": "text",
                            "text": "สกอบั้งไฟวันนี้",
                            "size": "md",
                            "weight": "bold",
                            "color": "#26353D",
                        },
                        {
                            "type": "text",
                            "text": f"ทั้งหมด {total} รายการ",
                            "size": "xxs",
                            "color": "#8B989F",
                            "margin": "xs",
                        },
                    ]
                },
                {
                    "type": "text",
                    "text": f"หน้า {page_no}/{page_total}",
                    "size": "xxs",
                    "color": "#9AA5AB",
                    "align": "end",
                    "gravity": "center",
                    "flex": 0,
                },
            ],
        }
    ]

    # -------------------------
    # สรุปผล แสดงเฉพาะหน้า 1
    # -------------------------
    if page_no == 1:
        header_contents += [
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "cornerRadius": "10px",
                        "backgroundColor": "#F1FAF5",
                        "paddingAll": "7px",
                        "contents": [
                            {
                                "type": "text",
                                "text": "ชนะ",
                                "size": "xxs",
                                "color": "#708078",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": f"✅ {stats['win']}",
                                "size": "sm",
                                "weight": "bold",
                                "color": "#267A55",
                                "align": "center",
                                "margin": "xs",
                            },
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "cornerRadius": "10px",
                        "backgroundColor": "#FFF5F5",
                        "paddingAll": "7px",
                        "contents": [
                            {
                                "type": "text",
                                "text": "แพ้",
                                "size": "xxs",
                                "color": "#857474",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": f"❌ {stats['lose']}",
                                "size": "sm",
                                "weight": "bold",
                                "color": "#B84A4A",
                                "align": "center",
                                "margin": "xs",
                            },
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "cornerRadius": "10px",
                        "backgroundColor": "#FFF9EF",
                        "paddingAll": "7px",
                        "contents": [
                            {
                                "type": "text",
                                "text": "จาว",
                                "size": "xxs",
                                "color": "#837A68",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": f"⛔ {stats['draw']}",
                                "size": "sm",
                                "weight": "bold",
                                "color": "#9A711F",
                                "align": "center",
                                "margin": "xs",
                            },
                        ],
                    },
                ],
            }
        ]

    header_contents.append({
        "type": "separator",
        "margin": "md",
        "color": "#E8EDF0",
    })

    return {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#FFFFFF",
            "paddingStart": "14px",
            "paddingEnd": "14px",
            "paddingTop": "12px",
            "paddingBottom": "7px",
            "contents": header_contents,
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#FFFFFF",
            "paddingStart": "14px",
            "paddingEnd": "14px",
            "paddingTop": "3px",
            "paddingBottom": "8px",
            "contents": rows,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#FAFBFC",
            "paddingAll": "6px",
            "contents": [
                {
                    "type": "text",
                    "text": TARGET_GROUP_NAME,
                    "size": "xxs",
                    "color": "#98A4AA",
                    "align": "center",
                    "wrap": True,
                }
            ],
        },
    }



def peh_flex_messages(event: dict) -> list:
    """
    คืนค่าเป็น list ของ Flex messages

    - 20 รายการ / bubble
    - สูงสุด 6 bubbles / carousel (JSON ไม่เกิน 50 KB)
    - 120 รายการ = 6 bubbles ใน Flex เดียว
    - ถ้าเกิน 120 รายการ จะตัดเป็น Flex carousel ชุดถัดไปอัตโนมัติ
    """
    key = _source_key(event)
    items = PEH_LIST.get(key, [])
    total = len(items)

    stats, parsed_items = _peh_build_stats(items)

    # อย่างน้อยให้มี 1 หน้า แม้ยังไม่มีรายการ
    if not parsed_items:
        pages = [[]]
    else:
        pages = [
            parsed_items[i:i + PEH_ITEMS_PER_PAGE]
            for i in range(0, len(parsed_items), PEH_ITEMS_PER_PAGE)
        ]

    page_total = len(pages)

    bubbles = [
        _peh_page_bubble(
            page_items=page_items,
            page_no=page_no,
            page_total=page_total,
            stats=stats,
            total=total,
        )
        for page_no, page_items in enumerate(pages, start=1)
    ]

    # แบ่ง carousel ละไม่เกิน 12 bubbles ตามข้อจำกัดของ LINE
    carousel_groups = [
        bubbles[i:i + PEH_MAX_BUBBLES_PER_CAROUSEL]
        for i in range(0, len(bubbles), PEH_MAX_BUBBLES_PER_CAROUSEL)
    ]

    messages = []

    for group_index, group in enumerate(carousel_groups, start=1):
        if len(group) == 1:
            contents = group[0]
        else:
            contents = {
                "type": "carousel",
                "contents": group,
            }

        messages.append({
            "type": "flex",
            "altText": (
                f"สกอบั้งไฟวันนี้ • "
                f"ชนะ {stats['win']} แพ้ {stats['lose']} จาว {stats['draw']} "
                f"• รวม {total} รายการ"
            ),
            "contents": contents,
        })

    return messages



# =========================
# FLEX ปุ่มส่งสลิป สำหรับคำสั่ง "บช"
# =========================
SLIP_SEND_URL = "https://page.line.me/812anmhp"


def account_send_slip_flex() -> dict:
    """
    FLEX ปุ่มส่งสลิปแบบกะทัดรัด แต่กว้างพอให้เห็นคำว่า
    'ส่งสลิปที่นี่' ครบถ้วนบนมือถือ
    """
    return {
        "type": "flex",
        "altText": "📤 ส่งสลิปที่นี่",
        "contents": {
            "type": "bubble",
            "size": "micro",
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#FFFFFF",
                "paddingAll": "12px",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "height": "sm",
                        "color": "#06C755",
                        "action": {
                            "type": "uri",
                            "label": "ส่งสลิปที่นี่",
                            "uri": SLIP_SEND_URL
                        }
                    }
                ]
            }
        }
    }


# =========================
# Event handlers
# =========================
def handle_text(event: dict):
    text = str(((event.get("message") or {}).get("text") or "")).strip()
    reply_token = event.get("replyToken")

    source = event.get("source") or {}
    user_id = source.get("userId")
    is_admin = is_admin_uid(user_id)


    # ====== UID / ค้น UID ======

    # เก็บ UID + ชื่อของทุกคนที่พิมพ์ข้อความเข้ามาหาบอท
    if user_id:
        try:
            display_name = _display_name_from_event(event)
            _save_user_to_txt(user_id, display_name)
        except Exception as exc:
            print("[UID] capture error:", exc)

    # คำสั่ง "uid" = ดู UID ตัวเอง
    if text.lower() == "uid":
        reply_line(
            reply_token,
            [text_message(f"🔍 UID ของคุณคือ:\n{user_id or 'ไม่พบ UID'}")]
        )
        return

    # คำสั่ง "@ชื่อไลน์ uid" = แอดมินค้น UID ของคนที่บอทเคยเห็น
    m_uid_lookup = re.match(r"^@(.+?)\s+uid$", text, re.IGNORECASE)
    if m_uid_lookup:
        if not is_admin:
            reply_line(
                reply_token,
                [text_message("⛔ คำสั่งค้น UID ของผู้อื่นใช้ได้เฉพาะแอดมิน")]
            )
            return

        query_name = m_uid_lookup.group(1).strip()
        matches = _search_uid_by_name(query_name, limit=10)

        if not matches:
            reply_line(
                reply_token,
                [text_message(
                    f"ไม่พบชื่อที่ตรงกับ “{query_name}” ในระบบ\n"
                    "หมายเหตุ: บอทจะค้นได้เฉพาะคนที่เคยส่งข้อความในแชท/กลุ่มที่บอทอยู่"
                )]
            )
            return

        if len(matches) == 1:
            uid_found, name_found = matches[0]
            reply_line(
                reply_token,
                [text_message(
                    f"🔍 UID ของ {name_found or 'ไม่ทราบชื่อ'} คือ:\n{uid_found}"
                )]
            )
            return

        lines = [f"พบหลายคนที่ชื่อคล้าย “{query_name}”:"]
        for uid_found, name_found in matches:
            lines.append(f"• {name_found or '(ไม่มีชื่อ)'}\n  {uid_found}")

        reply_line(reply_token, [text_message("\n".join(lines))])
        return



    # ====== คำสั่ง "เช็คแอดมิน" ======
    # ใช้ได้เฉพาะแอดมิน เพื่อดูรายชื่อแอดมินที่เก็บถาวรใน SQLite
    if text in {"เช็คแอดมิน", "เช็กแอดมิน", "เชคแอดมิน"}:
        if not is_admin:
            reply_line(
                reply_token,
                [text_message("⛔ คำสั่งเช็คแอดมิน ใช้ได้เฉพาะแอดมินเท่านั้น")]
            )
            return

        admins = list_admins_persistent()

        if not admins:
            reply_line(reply_token, [text_message("ยังไม่มีรายชื่อแอดมินในระบบ")])
            return

        lines = [f"👑 แอดมินทั้งหมด {len(admins)} คน"]
        for index, admin in enumerate(admins, start=1):
            name = admin.get("display_name") or "ไม่ทราบชื่อ"
            uid = admin.get("uid") or "-"
            lines.append(f"{index}. 👤 {name}\n   🆔 {uid}")

        # LINE จำกัดจำนวนข้อความต่อ reply; แบ่งเป็นก้อนเพื่อกันข้อความยาวเกินไป
        chunks = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}".strip()
            if len(candidate) > 4500 and current:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        reply_line(reply_token, [text_message(chunk) for chunk in chunks[:5]])
        return


    # ====== คำสั่ง "เพิ่มแอด @ชื่อไลน์" ======
    # ใช้ได้เฉพาะแอดมินปัจจุบัน
    if re.match(r"^เพิ่มแอด(?:\s+|$)", text, re.IGNORECASE):
        if not is_admin:
            reply_line(
                reply_token,
                [text_message("⛔ คำสั่งเพิ่มแอดมิน ใช้ได้เฉพาะแอดมินเท่านั้น")]
            )
            return

        if not HAS_RAILWAY_VOLUME:
            reply_line(
                reply_token,
                [text_message(
                    "⚠️ ไม่พบ Railway Volume ใน runtime\n"
                    "ให้ตรวจว่า Volume ผูกกับ service web และมี Mount Path แล้ว\n"
                    "จากนั้น Redeploy อีกครั้ง"
                )]
            )
            return

        target_uid = None
        target_name = ""

        # วิธีที่ 1: ใช้ @mention จริงของ LINE — แม่นที่สุด
        mentioned_uids = _extract_mentioned_user_ids(event)
        if mentioned_uids:
            target_uid = mentioned_uids[0]
            target_name = _display_name_for_uid(event, target_uid)

        # วิธีที่ 2: ถ้าไม่มี mention metadata ให้ค้นจากชื่อที่บอทเคยเก็บ
        if not target_uid:
            m_add_admin = re.match(
                r"^เพิ่มแอด\s+@(.+?)\s*$",
                text,
                re.IGNORECASE
            )

            if not m_add_admin:
                reply_line(
                    reply_token,
                    [text_message(
                        "รูปแบบคำสั่ง:\n"
                        "เพิ่มแอด @ชื่อไลน์\n\n"
                        "แนะนำให้กด @mention สมาชิกจริงในกลุ่ม"
                    )]
                )
                return

            query_name = m_add_admin.group(1).strip()
            matches = _search_uid_by_name(query_name, limit=10)

            if not matches:
                reply_line(
                    reply_token,
                    [text_message(
                        f"❌ ไม่พบชื่อ “{query_name}” ในระบบ\n"
                        "ให้สมาชิกคนนั้นพิมพ์ข้อความในกลุ่ม/แชทที่บอทอยู่ก่อน "
                        "หรือใช้ @mention จริงแล้วลองใหม่"
                    )]
                )
                return

            if len(matches) > 1:
                lines = [
                    f"⚠️ พบชื่อคล้าย “{query_name}” หลายคน",
                    "กรุณา @mention คนที่ต้องการเพิ่มโดยตรง:"
                ]
                for uid_found, name_found in matches[:5]:
                    lines.append(
                        f"• {name_found or '(ไม่มีชื่อ)'}  ({uid_found[:8]}…)"
                    )

                reply_line(
                    reply_token,
                    [text_message("\n".join(lines))]
                )
                return

            target_uid, target_name = matches[0]

        # กันกรณีเพิ่มตัวเอง/คนเดิมซ้ำ (ยังตอบได้ปกติ)
        if not target_name:
            target_name = _display_name_for_uid(event, target_uid) or "ไม่ทราบชื่อ"

        ok, already, result_msg = add_admin_persistent(
            target_uid,
            target_name,
            user_id,
        )

        if not ok:
            reply_line(
                reply_token,
                [text_message(
                    "❌ เพิ่มแอดมินไม่สำเร็จ\n"
                    f"{result_msg}"
                )]
            )
            return

        if already:
            reply_text = (
                "ℹ️ คนนี้เป็นแอดมินอยู่แล้ว\n"
                f"👤 {target_name}\n"
                f"🆔 {target_uid}"
            )
        else:
            reply_text = (
                "✅ เพิ่มแอดมินถาวรเรียบร้อย\n"
                f"👤 {target_name}\n"
                f"🆔 {target_uid}\n\n"
                "ข้อมูลถูกเก็บใน Railway Volume (SQLite) แล้ว "
                "รีบอทหรือ Redeploy รายชื่อก็ไม่หาย"
            )

        reply_line(reply_token, [text_message(reply_text)])
        return


    # ====== คำสั่ง "สกอ" ดูรายการทั้งหมด ======
    # ทุกคนในห้อง/กลุ่มสามารถดูสกอของห้องนั้นได้
    if text == "สกอ":
        key = _source_key(event)
        if not PEH_LIST.get(key):
            reply_line(
                reply_token,
                [text_message("📋 ยังไม่มีรายการเปะในห้องนี้")]
            )
            return

        reply_line(reply_token, peh_flex_messages(event))
        return

    # ====== PEH / "เปะ" (เฉพาะแอดมิน) ======
    # รองรับทั้ง 1 บรรทัด:
    #   เปะ ข้อความ
    #
    # และหลายบรรทัดในข้อความเดียว:
    #   เปะ รายการแรก
    #   เปะ รายการที่สอง
    if is_admin and "เปะ" in text:
        lines = text.split("\n")
        added = False

        for line in lines:
            m = re.match(r"^เปะ\s+(.+)$", line.strip())
            if m:
                item_text = m.group(1).strip()
                _add_peh_item(event, item_text)
                added = True

        if added:
            reply_line(reply_token, peh_flex_messages(event))
            return

    # ====== คำสั่ง "ลบ <ตัวเลข>" ลบรายการสกอตามหมายเลข (เฉพาะแอดมิน) ======
    m_delete = re.match(r"^ลบ\s+(\d+)$", text)
    if m_delete:
        if not is_admin:
            reply_line(
                reply_token,
                [text_message("⛔ คำสั่งลบรายการ ใช้ได้เฉพาะแอดมินเท่านั้น")]
            )
            return

        key = _source_key(event)
        items = PEH_LIST.get(key, [])
        del_no = int(m_delete.group(1))

        if not items:
            reply_line(
                reply_token,
                [text_message("📋 ยังไม่มีรายการเปะในห้องนี้")]
            )
            return

        if del_no < 1 or del_no > len(items):
            reply_line(
                reply_token,
                [text_message(
                    f"❌ ไม่มีรายการที่ {del_no}\n"
                    f"มีรายการทั้งหมด {len(items)} รายการ"
                )]
            )
            return

        removed = items.pop(del_no - 1)
        PEH_LIST[key] = items

        if not items:
            reply_line(
                reply_token,
                [text_message(f'🗑️ ลบรายการที่ {del_no} แล้ว\n\u201c{removed}\u201d\n\n(ไม่มีรายการเหลือแล้ว)')]
            )
        else:
            reply_line(
                reply_token,
                [text_message(f'🗑️ ลบรายการที่ {del_no} แล้ว\n\u201c{removed}\u201d')]
            )
        return

    # ====== ล้างรายการ PEH (เฉพาะแอดมิน) ======
    if text == "ล้างรายการ" and is_admin:
        key = _source_key(event)
        PEH_LIST[key] = []
        reply_line(reply_token, [text_message("ล้างรายการเรียบร้อย")])
        return

    # ====== คำสั่ง บช (สลับบัญชีทุกครั้ง) ======
    _BCC_KEYWORDS = re.compile(
        r"^(บช|บันชี|บัญชี|บันขี|เลขบัญชี|เลข\.บัญชี|เลขบันชี|บัณชี|ขอบัญชี|ลบช)$"
    )
    if _BCC_KEYWORDS.match(text):
        ordered = get_accounts_ordered()  # [บัญชีแรก, บัญชีที่สอง] (สลับลำดับทุกครั้ง)
        combined = (
            f"━━━━━━━━━━━━━━\n"
            f"🏦 แจ้งเลขบัญชีฝากเงิน\n"
            f"━━━━━━━━━━━━━━\n"
            f"🔢 บัญชีที่ 1\n"
            f"   เลขบัญชี : {ordered[0]['number']}\n"
            f"   ธนาคาร   : {ordered[0]['bank']}\n"
            f"   ชื่อบัญชี : {ordered[0]['name']}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🔢 บัญชีที่ 2\n"
            f"   เลขบัญชี : {ordered[1]['number']}\n"
            f"   ธนาคาร   : {ordered[1]['bank']}\n"
            f"   ชื่อบัญชี : {ordered[1]['name']}\n"
            f"━━━━━━━━━━━━━━\n"
            f"⚠️ เพื่อป้องกันมิจฉาชีพ\n"
            f"ชื่อผู้ฝาก-ถอน ต้องเป็นชื่อเดียวกันเท่านั้น ✅"
        )
        reply_line(
            reply_token,
            [
                text_message(combined),
                account_send_slip_flex()
            ]
        )
        return


def handle_image(event: dict):
    # ====== ตรวจสลิปเฉพาะแชท 1-1 เท่านั้น ======
    # ถ้าลูกค้าส่งรูปใน group / room ให้บอทเงียบ ไม่ตอบ ไม่เรียก EasySlip
    source = event.get("source") or {}
    source_type = str(source.get("type") or "").lower()

    if source_type != "user":
        print(f"[SLIP] ignore image from source_type={source_type}")
        return

    user_id = str(source.get("userId") or "").strip()

    # ถ้าลูกค้าส่งหลายภาพติดกันในแชท 1-1:
    # ตรวจเฉพาะภาพแรก ภาพถัดไปเงียบ และไม่ดาวน์โหลด/ไม่เรียก EasySlip
    if not _claim_private_image_slot(user_id):
        print(
            f"[SLIP] ignore burst image user={user_id[:10]}... "
            f"window={PRIVATE_IMAGE_BURST_SECONDS:g}s"
        )
        return

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

        # ====== รับเฉพาะสลิปของ “วันนี้” ตามเวลาไทย ======
        # ต้องเช็กก่อน isDuplicate / claim_trans_ref เพื่อให้สลิปคนละวัน
        # ถูกแจ้งว่า “ไม่ใช่ของวันนี้” และไม่ถูกบันทึกว่าใช้งานแล้วในฐานข้อมูลบอท
        is_today, date_detail = validate_slip_is_today(data)
        if not is_today:
            reply_line(
                reply_token,
                [error_flex("slip_wrong_date", date_detail)],
            )
            return

        # EasySlip can expose isDuplicate in success data.
        if data.get("isDuplicate") is True:
            reply_line(reply_token, [error_flex("duplicate_slip")])
            return

        # When matchAccount=true, require a matched account.
        if VERIFY_MATCH_ACCOUNT and data.get("matchedAccount") is None:
            reply_line(reply_token, [error_flex("account_not_match")])
            return

        # Optional second duplicate-protection layer using SQLite.
        if not claim_trans_ref(data):
            reply_line(reply_token, [error_flex("duplicate_slip")])
            return

        reply_line(reply_token, [success_flex(data)])
        return

    # บาง error response ของ EasySlip อาจแนบ data/rawSlip กลับมาด้วย
    # ถ้ามีวันที่สลิปและเป็นคนละวัน ให้กติกา “วันนี้เท่านั้น” มาก่อน error อื่น
    error_data = result.get("data") or {}
    if isinstance(error_data, dict) and ((error_data.get("rawSlip") or {}).get("date")):
        is_today, date_detail = validate_slip_is_today(error_data)
        if not is_today:
            reply_line(
                reply_token,
                [error_flex("slip_wrong_date", date_detail)],
            )
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


@app.get("/storage-status")
def storage_status():
    return jsonify({
        "volume_detected": HAS_RAILWAY_VOLUME,
        "mount_path": VOLUME_MOUNT_PATH or None,
        "sqlite_path": SQLITE_PATH,
        "sqlite_exists": os.path.exists(SQLITE_PATH),
    }), 200


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
