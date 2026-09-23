import os
import sqlite3
import hashlib
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Query, status
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx

DB_PATH = os.environ.get("DB_PATH", "teamfund.db")
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")
SESSION_EXPIRE_DAYS = 30
OTP_EXPIRE_MINUTES = 10
SALT = os.environ.get("APP_SALT", "team-fund-salt-secret-2026")

# ----------------- Database Init & Helpers -----------------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            pin_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            avatar_url TEXT DEFAULT '',
            bank_name TEXT DEFAULT '',
            bank_account_number TEXT DEFAULT '',
            bank_account_name TEXT DEFAULT '',
            status TEXT DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            purpose TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            category TEXT DEFAULT 'Ăn uống',
            total_amount INTEGER NOT NULL,
            expense_date TEXT NOT NULL,
            payer_user_id INTEGER NOT NULL,
            source_type TEXT NOT NULL, -- PERSONAL, GROUP_FUND
            split_mode TEXT NOT NULL, -- EQUAL, ADJUSTED, CUSTOM, NONE
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            FOREIGN KEY (payer_user_id) REFERENCES users(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bill_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            share_amount INTEGER,
            payment_status TEXT DEFAULT 'UNPAID', -- UNPAID, PAYMENT_REPORTED, PAID
            payment_reported_at TEXT,
            payment_confirmed_at TEXT,
            FOREIGN KEY (bill_id) REFERENCES bills(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS fund_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL, -- CONTRIBUTION, EXPENSE, ADJUSTMENT
            amount INTEGER NOT NULL,
            user_id INTEGER,
            bill_id INTEGER,
            transaction_date TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (bill_id) REFERENCES bills(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            reference_type TEXT,
            reference_id INTEGER,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
        CREATE INDEX IF NOT EXISTS idx_bills_deleted ON bills(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_bill_participants_bill ON bill_participants(bill_id);
        CREATE INDEX IF NOT EXISTS idx_fund_tx_deleted ON fund_transactions(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);
        """)
    conn.close()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def hash_pin(pin: str) -> str:
    return hashlib.sha256(f"{pin}:{SALT}".encode()).hexdigest()

def hash_code(code: str) -> str:
    return hashlib.sha256(f"{code}:{SALT}".encode()).hexdigest()

# ----------------- n8n & Notifications Dispatcher -----------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "355037509")

# Auto-load telegram config from local .env if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line.startswith("TELEGRAM_BOT_TOKEN="):
            TELEGRAM_BOT_TOKEN = _line.split("=", 1)[1].strip("'\"")
        elif _line.startswith("TELEGRAM_CHAT_ID="):
            TELEGRAM_CHAT_ID = _line.split("=", 1)[1].strip("'\"")

async def send_telegram_admin(message: str):
    """Send notification to Admin via Telegram Bot"""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print(f"[telegram-skip] Missing bot token or chat ID. Msg: {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=6.0) as client:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            })
    except Exception as e:
        print(f"[telegram-error] {e}")

async def dispatch_event(event_type: str, payload: dict):
    """Fire and forget event to n8n webhook without blocking request"""
    if not N8N_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            await client.post(N8N_WEBHOOK_URL, json={
                "event": event_type,
                "data": payload,
                "timestamp": now_iso()
            })
    except Exception as e:
        # Log and ignore webhook failure to protect core app
        print(f"[n8n-webhook-error] {event_type}: {e}")

def create_notification(conn: sqlite3.Connection, user_id: int, ntype: str, title: str, message: str, ref_type: Optional[str] = None, ref_id: Optional[int] = None):
    conn.execute("""
        INSERT INTO notifications (user_id, type, reference_type, reference_id, title, message, is_read, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
    """, (user_id, ntype, ref_type, ref_id, title, message, now_iso()))

# ----------------- App Lifecycle & Middleware -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Team Fund & Bill Split", lifespan=lifespan)

# ----------------- Auth Helpers -----------------

def get_current_user_optional(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Optional[dict]:
    token = None
    auth_hdr = request.headers.get("Authorization")
    if auth_hdr and auth_hdr.startswith("Bearer "):
        token = auth_hdr.split(" ")[1]
    if not token:
        token = request.cookies.get("session_token")
    if not token:
        return None

    cur = conn.execute("""
        SELECT u.* FROM users u
        JOIN sessions s ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > ?
    """, (token, now_iso()))
    row = cur.fetchone()
    return dict(row) if row else None

def get_current_user(user: Optional[dict] = Depends(get_current_user_optional)) -> dict:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Vui lòng đăng nhập")
    return user

# ----------------- Pydantic Models -----------------

class EmailCheckReq(BaseModel):
    email: str

class LoginReq(BaseModel):
    email: str
    pin: str = Field(min_length=4, max_length=4)

class ProfileUpdateReq(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_account_name: Optional[str] = None

class ParticipantShare(BaseModel):
    user_id: int
    share_amount: Optional[int] = None

class BillCreateReq(BaseModel):
    title: str
    description: Optional[str] = ""
    category: Optional[str] = "Ăn uống"
    total_amount: int
    expense_date: str
    payer_user_id: int
    source_type: str # PERSONAL | GROUP_FUND
    split_mode: str # EQUAL | ADJUSTED | CUSTOM | NONE
    participants: List[ParticipantShare]

class ContributionReq(BaseModel):
    amount: int
    transaction_date: str
    note: Optional[str] = ""

class PaymentConfirmReq(BaseModel):
    user_id: int

class ChangePinReq(BaseModel):
    new_pin: str = Field(min_length=4, max_length=4)
    old_pin: Optional[str] = None

# ----------------- Auth API -----------------

@app.post("/api/auth/check-email")
async def check_email(data: EmailCheckReq, conn: sqlite3.Connection = Depends(get_db)):
    email = data.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Vui lòng nhập địa chỉ email hợp lệ.")

    row = conn.execute("SELECT id, display_name FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return {
            "exists": True,
            "email": email,
            "display_name": row["display_name"],
            "message": "Email đã đăng ký. Vui lòng nhập mã PIN 4 số."
        }

    # Email chưa đăng ký: Tự động sinh mã PIN tạm 4 số và tạo tài khoản
    temp_pin = f"{secrets.randbelow(9000) + 1000}"
    pin_h = hash_pin(temp_pin)
    now_str = now_iso()
    default_name = email.split("@")[0].replace(".", " ").title()

    with conn:
        conn.execute("""
            INSERT INTO users (email, pin_hash, display_name, status, created_at, updated_at)
            VALUES (?, ?, ?, 'PENDING_PIN_CHANGE', ?, ?)
        """, (email, pin_h, default_name, now_str, now_str))

    # Gửi Telegram cho Admin (Thanh Hải)
    time_vn = datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M:%S %d/%m/%Y")
    tele_msg = (
        f"🔔 <b>[Team Fund & Bill] Cấp mã PIN tạm thời</b>\n\n"
        f"👤 <b>Email:</b> <code>{email}</code>\n"
        f"🔑 <b>Mã PIN tạm:</b> <code>{temp_pin}</code>\n"
        f"⏰ <b>Thời gian:</b> {time_vn}\n\n"
        f"👉 <i>Gửi mã PIN này cho thành viên để đăng nhập lần đầu. Thành viên sẽ đổi PIN 2 bước ngay sau khi nhập mã này.</i>"
    )
    asyncio.create_task(send_telegram_admin(tele_msg))

    return {
        "exists": False,
        "email": email,
        "message": "Email chưa được đăng ký trong hệ thống. Vui lòng liên hệ Admin để lấy mã PIN tạm thời."
    }

@app.post("/api/auth/change-pin")
def change_pin(data: ChangePinReq, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    if not (data.new_pin.isdigit() and len(data.new_pin) == 4):
        raise HTTPException(status_code=400, detail="Mã PIN mới phải gồm đúng 4 chữ số.")

    if data.old_pin:
        if hash_pin(data.old_pin.strip()) != user["pin_hash"]:
            raise HTTPException(status_code=400, detail="Mã PIN hiện tại không chính xác.")

    new_pin_h = hash_pin(data.new_pin.strip())
    now_str = now_iso()

    with conn:
        conn.execute("UPDATE users SET pin_hash = ?, status = 'ACTIVE', updated_at = ? WHERE id = ?", (new_pin_h, now_str, user["id"]))

    return {"message": "Đổi mã PIN thành công"}

@app.post("/api/auth/login")
def login(data: LoginReq, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    email = data.email.strip().lower()
    pin_h = hash_pin(data.pin.strip())

    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or user["pin_hash"] != pin_h:
        raise HTTPException(status_code=400, detail="Email hoặc mã PIN không chính xác.")

    token = secrets.token_hex(32)
    now_str = now_iso()
    exp_session = (datetime.now(timezone.utc) + timedelta(days=SESSION_EXPIRE_DAYS)).isoformat()

    with conn:
        conn.execute("""
            INSERT INTO sessions (user_id, token, expires_at, created_at)
            VALUES (?, ?, ?, ?)
        """, (user["id"], token, exp_session, now_str))

    response.set_cookie(
        key="session_token",
        value=token,
        max_age=SESSION_EXPIRE_DAYS * 86400,
        httponly=True,
        samesite="lax"
    )

    must_change_pin = (user["status"] == "PENDING_PIN_CHANGE")

    return {
        "message": "Đăng nhập thành công",
        "token": token,
        "must_change_pin": must_change_pin,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "avatar_url": user["avatar_url"],
            "bank_name": user["bank_name"],
            "bank_account_number": user["bank_account_number"],
            "bank_account_name": user["bank_account_name"],
            "status": user["status"]
        }
    }

@app.post("/api/auth/logout")
def logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    token = request.cookies.get("session_token")
    if token:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    response.delete_cookie("session_token")
    return {"message": "Đã đăng xuất"}

@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "avatar_url": user["avatar_url"],
        "bank_name": user["bank_name"],
        "bank_account_number": user["bank_account_number"],
        "bank_account_name": user["bank_account_name"],
        "status": user["status"],
        "must_change_pin": (user["status"] == "PENDING_PIN_CHANGE")
    }

# ----------------- User & Profile API -----------------

@app.get("/api/users")
def list_users(conn: sqlite3.Connection = Depends(get_db), current_user: dict = Depends(get_current_user)):
    rows = conn.execute("""
        SELECT id, email, display_name, avatar_url, bank_name, bank_account_number, bank_account_name
        FROM users WHERE status IN ('ACTIVE', 'PENDING_PIN_CHANGE') ORDER BY display_name ASC
    """).fetchall()
    return [dict(r) for r in rows]

@app.patch("/api/users/me")
def update_profile(data: ProfileUpdateReq, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    updates = []
    params = []
    for field in ("display_name", "avatar_url", "bank_name", "bank_account_number", "bank_account_name"):
        val = getattr(data, field)
        if val is not None:
            updates.append(f"{field} = ?")
            params.append(val.strip())

    if not updates:
        return {"message": "Không có gì thay đổi", "user": user}

    now_str = now_iso()
    updates.append("updated_at = ?")
    params.append(now_str)
    params.append(user["id"])

    with conn:
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)

    updated_user = conn.execute("SELECT id, email, display_name, avatar_url, bank_name, bank_account_number, bank_account_name FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"message": "Cập nhật thông tin thành công", "user": dict(updated_user)}

# ----------------- Bill Split Calculation Logic -----------------

def calculate_splits(total_amount: int, split_mode: str, participants: List[ParticipantShare], payer_id: int):
    n = len(participants)
    if n == 0:
        raise HTTPException(status_code=400, detail="Cần chọn ít nhất 1 thành viên tham gia.")

    results = []
    if split_mode == "EQUAL":
        base = total_amount // n
        rem = total_amount % n
        for i, p in enumerate(participants):
            share = base + (1 if i < rem else 0)
            status = "PAID" if p.user_id == payer_id else "UNPAID"
            results.append({
                "user_id": p.user_id,
                "share_amount": share,
                "payment_status": status
            })
    elif split_mode in ("ADJUSTED", "CUSTOM"):
        total_shares = sum(p.share_amount or 0 for p in participants)
        if total_shares != total_amount:
            raise HTTPException(
                status_code=400,
                detail=f"Tổng tiền chia ({total_shares:,} đ) không khớp với tổng bill ({total_amount:,} đ)."
            )
        for p in participants:
            status = "PAID" if p.user_id == payer_id else "UNPAID"
            results.append({
                "user_id": p.user_id,
                "share_amount": p.share_amount,
                "payment_status": status
            })
    else: # NONE or fallback
        for p in participants:
            results.append({
                "user_id": p.user_id,
                "share_amount": None,
                "payment_status": None
            })
    return results

# ----------------- Bill Management API -----------------

@app.post("/api/bills")
async def create_bill(data: BillCreateReq, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    if data.total_amount <= 0:
        raise HTTPException(status_code=400, detail="Tổng tiền hoá đơn phải lớn hơn 0.")
    if data.source_type not in ("PERSONAL", "GROUP_FUND"):
        raise HTTPException(status_code=400, detail="Nguồn tiền không hợp lệ.")
    if not data.participants:
        raise HTTPException(status_code=400, detail="Hoá đơn phải có ít nhất 1 người tham gia.")

    now_str = now_iso()
    title = data.title.strip()
    description = data.description.strip() if data.description else ""

    with conn:
        cur = conn.execute("""
            INSERT INTO bills (title, description, category, total_amount, expense_date, payer_user_id, source_type, split_mode, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, description, data.category, data.total_amount, data.expense_date, data.payer_user_id, data.source_type, data.split_mode, user["id"], now_str, now_str))
        bill_id = cur.lastrowid

        if data.source_type == "PERSONAL":
            splits = calculate_splits(data.total_amount, data.split_mode, data.participants, data.payer_user_id)
            for s in splits:
                conn.execute("""
                    INSERT INTO bill_participants (bill_id, user_id, share_amount, payment_status)
                    VALUES (?, ?, ?, ?)
                """, (bill_id, s["user_id"], s["share_amount"], s["payment_status"]))

                # Notify participant if debtor
                if s["user_id"] != data.payer_user_id:
                    create_notification(
                        conn, s["user_id"], "BILL_CREATED",
                        "Hoá đơn mới",
                        f"Bạn có khoản phải trả {s['share_amount']:,} đ cho hoá đơn '{data.title}'",
                        ref_type="BILL", ref_id=bill_id
                    )
        else: # GROUP_FUND
            for p in data.participants:
                conn.execute("""
                    INSERT INTO bill_participants (bill_id, user_id, share_amount, payment_status)
                    VALUES (?, ?, NULL, NULL)
                """, (bill_id, p.user_id))

            # Deduct from group fund ledger
            conn.execute("""
                INSERT INTO fund_transactions (type, amount, bill_id, transaction_date, note, created_by, created_at)
                VALUES ('EXPENSE', ?, ?, ?, ?, ?, ?)
            """, (data.total_amount, bill_id, data.expense_date, f"Chi bill: {data.title}", user["id"], now_str))

    asyncio.create_task(dispatch_event("bill_created", {
        "bill_id": bill_id,
        "title": data.title,
        "total_amount": data.total_amount,
        "source_type": data.source_type,
        "created_by": user["display_name"]
    }))

    return {"message": "Tạo hoá đơn thành công", "bill_id": bill_id}

@app.get("/api/bills")
def get_bills(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    source_type: Optional[str] = None,
    payer_id: Optional[int] = None,
    participant_id: Optional[int] = None,
    category: Optional[str] = None,
    payment_status: Optional[str] = None,
    search: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    query = """
        SELECT b.*, u.display_name as payer_name, u.avatar_url as payer_avatar,
               u.bank_name, u.bank_account_number, u.bank_account_name,
               c.display_name as creator_name
        FROM bills b
        JOIN users u ON b.payer_user_id = u.id
        JOIN users c ON b.created_by = c.id
        WHERE b.deleted_at IS NULL
    """
    params = []

    if from_date:
        query += " AND b.expense_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND b.expense_date <= ?"
        params.append(to_date)
    if source_type:
        query += " AND b.source_type = ?"
        params.append(source_type)
    if payer_id:
        query += " AND b.payer_user_id = ?"
        params.append(payer_id)
    if category:
        query += " AND b.category = ?"
        params.append(category)
    if search:
        query += " AND (b.title LIKE ? OR b.description LIKE ?)"
        term = f"%{search.strip()}%"
        params.extend([term, term])
    if participant_id:
        query += " AND b.id IN (SELECT bill_id FROM bill_participants WHERE user_id = ?)"
        params.append(participant_id)
    if payment_status:
        query += " AND b.id IN (SELECT bill_id FROM bill_participants WHERE payment_status = ?)"
        params.append(payment_status)

    query += " ORDER BY b.expense_date DESC, b.id DESC"
    rows = conn.execute(query, params).fetchall()

    bill_list = []
    for r in rows:
        b = dict(r)
        # Fetch participants
        parts = conn.execute("""
            SELECT bp.*, u.display_name, u.avatar_url, u.email
            FROM bill_participants bp
            JOIN users u ON bp.user_id = u.id
            WHERE bp.bill_id = ?
        """, (b["id"],)).fetchall()
        b["participants"] = [dict(p) for p in parts]
        bill_list.append(b)

    return bill_list

@app.get("/api/bills/{bill_id}")
def get_bill_detail(bill_id: int, conn: sqlite3.Connection = Depends(get_db), user: dict = Depends(get_current_user)):
    row = conn.execute("""
        SELECT b.*, u.display_name as payer_name, u.avatar_url as payer_avatar,
               u.bank_name, u.bank_account_number, u.bank_account_name,
               c.display_name as creator_name
        FROM bills b
        JOIN users u ON b.payer_user_id = u.id
        JOIN users c ON b.created_by = c.id
        WHERE b.id = ? AND b.deleted_at IS NULL
    """, (bill_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy hoá đơn")

    b = dict(row)
    parts = conn.execute("""
        SELECT bp.*, u.display_name, u.avatar_url, u.email
        FROM bill_participants bp
        JOIN users u ON bp.user_id = u.id
        WHERE bp.bill_id = ?
    """, (bill_id,)).fetchall()
    b["participants"] = [dict(p) for p in parts]
    return b

@app.patch("/api/bills/{bill_id}")
async def update_bill(bill_id: int, data: BillCreateReq, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    bill = conn.execute("SELECT * FROM bills WHERE id = ? AND deleted_at IS NULL", (bill_id,)).fetchone()
    if not bill:
        raise HTTPException(status_code=404, detail="Hoá đơn không tồn tại")
    if bill["created_by"] != user["id"]:
        raise HTTPException(status_code=403, detail="Chỉ người tạo hoá đơn mới được phép chỉnh sửa.")

    now_str = now_iso()

    with conn:
        title = data.title.strip()
        description = data.description.strip() if data.description else ""
        conn.execute("""
            UPDATE bills
            SET title = ?, description = ?, category = ?, total_amount = ?, expense_date = ?,
                payer_user_id = ?, source_type = ?, split_mode = ?, updated_at = ?
            WHERE id = ?
        """, (title, description, data.category, data.total_amount,
              data.expense_date, data.payer_user_id, data.source_type, data.split_mode, now_str, bill_id))

        # Clear old participants
        conn.execute("DELETE FROM bill_participants WHERE bill_id = ?", (bill_id,))

        if data.source_type == "PERSONAL":
            # Neutralize any fund transactions if it was previously GROUP_FUND
            conn.execute("UPDATE fund_transactions SET deleted_at = ? WHERE bill_id = ? AND deleted_at IS NULL", (now_str, bill_id))

            splits = calculate_splits(data.total_amount, data.split_mode, data.participants, data.payer_user_id)
            for s in splits:
                conn.execute("""
                    INSERT INTO bill_participants (bill_id, user_id, share_amount, payment_status)
                    VALUES (?, ?, ?, ?)
                """, (bill_id, s["user_id"], s["share_amount"], s["payment_status"]))

                if s["user_id"] != data.payer_user_id:
                    create_notification(
                        conn, s["user_id"], "BILL_UPDATED",
                        "Hoá đơn đã cập nhật",
                        f"Hoá đơn '{data.title}' đã được cập nhật. Số tiền phải trả của bạn: {s['share_amount']:,} đ.",
                        ref_type="BILL", ref_id=bill_id
                    )
        else: # GROUP_FUND
            for p in data.participants:
                conn.execute("""
                    INSERT INTO bill_participants (bill_id, user_id, share_amount, payment_status)
                    VALUES (?, ?, NULL, NULL)
                """, (bill_id, p.user_id))

            # Update or insert fund expense
            existing_tx = conn.execute("SELECT id FROM fund_transactions WHERE bill_id = ? AND deleted_at IS NULL", (bill_id,)).fetchone()
            if existing_tx:
                conn.execute("""
                    UPDATE fund_transactions
                    SET amount = ?, transaction_date = ?, note = ?
                    WHERE id = ?
                """, (data.total_amount, data.expense_date, f"Chi bill: {data.title}", existing_tx["id"]))
            else:
                conn.execute("""
                    INSERT INTO fund_transactions (type, amount, bill_id, transaction_date, note, created_by, created_at)
                    VALUES ('EXPENSE', ?, ?, ?, ?, ?, ?)
                """, (data.total_amount, bill_id, data.expense_date, f"Chi bill: {data.title}", user["id"], now_str))

    asyncio.create_task(dispatch_event("bill_updated", {
        "bill_id": bill_id,
        "title": data.title,
        "total_amount": data.total_amount,
        "updated_by": user["display_name"]
    }))

    return {"message": "Cập nhật hoá đơn thành công"}

@app.delete("/api/bills/{bill_id}")
async def delete_bill(bill_id: int, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    bill = conn.execute("SELECT * FROM bills WHERE id = ? AND deleted_at IS NULL", (bill_id,)).fetchone()
    if not bill:
        raise HTTPException(status_code=404, detail="Hoá đơn không tồn tại")
    if bill["created_by"] != user["id"]:
        raise HTTPException(status_code=403, detail="Chỉ người tạo hoá đơn mới được phép xoá.")

    now_str = now_iso()

    with conn:
        conn.execute("UPDATE bills SET deleted_at = ? WHERE id = ?", (now_str, bill_id))
        conn.execute("UPDATE fund_transactions SET deleted_at = ? WHERE bill_id = ? AND deleted_at IS NULL", (now_str, bill_id))

        # Notify participants
        parts = conn.execute("SELECT user_id FROM bill_participants WHERE bill_id = ?", (bill_id,)).fetchall()
        for p in parts:
            if p["user_id"] != user["id"]:
                create_notification(
                    conn, p["user_id"], "BILL_DELETED",
                    "Hoá đơn đã bị xoá",
                    f"Hoá đơn '{bill['title']}' đã được xoá bởi {user['display_name']}.",
                    ref_type="BILL", ref_id=bill_id
                )

    asyncio.create_task(dispatch_event("bill_deleted", {
        "bill_id": bill_id,
        "title": bill["title"],
        "deleted_by": user["display_name"]
    }))

    return {"message": "Đã xoá hoá đơn thành công"}

# ----------------- Debt & Payment Confirmation API -----------------

@app.post("/api/bills/{bill_id}/payment-report")
async def report_payment(bill_id: int, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Debtor marks 'Tôi đã trả'"""
    bill = conn.execute("SELECT * FROM bills WHERE id = ? AND deleted_at IS NULL", (bill_id,)).fetchone()
    if not bill:
        raise HTTPException(status_code=404, detail="Hoá đơn không tồn tại")

    part = conn.execute("""
        SELECT * FROM bill_participants
        WHERE bill_id = ? AND user_id = ?
    """, (bill_id, user["id"])).fetchone()

    if not part:
        raise HTTPException(status_code=400, detail="Bạn không phải thành viên trong hoá đơn này.")
    if part["payment_status"] == "PAID":
        raise HTTPException(status_code=400, detail="Khoản nợ này đã được xác nhận hoàn tất trước đó.")

    now_str = now_iso()
    with conn:
        conn.execute("""
            UPDATE bill_participants
            SET payment_status = 'PAYMENT_REPORTED', payment_reported_at = ?
            WHERE id = ?
        """, (now_str, part["id"]))

        # Notify payer
        create_notification(
            conn, bill["payer_user_id"], "PAYMENT_REPORTED",
            "Báo đã trả tiền",
            f"{user['display_name']} đã báo thanh toán {part['share_amount']:,} đ cho hoá đơn '{bill['title']}'. Vui lòng kiểm tra và xác nhận.",
            ref_type="BILL", ref_id=bill_id
        )

    asyncio.create_task(dispatch_event("payment_reported", {
        "bill_id": bill_id,
        "user_id": user["id"],
        "user_name": user["display_name"],
        "amount": part["share_amount"],
        "payer_id": bill["payer_user_id"]
    }))

    return {"message": "Đã ghi nhận báo trả tiền. Chờ người thanh toán xác nhận."}

@app.post("/api/bills/{bill_id}/payment-confirm")
async def confirm_payment(bill_id: int, data: PaymentConfirmReq, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Payer confirms 'Đã nhận tiền'"""
    bill = conn.execute("SELECT * FROM bills WHERE id = ? AND deleted_at IS NULL", (bill_id,)).fetchone()
    if not bill:
        raise HTTPException(status_code=404, detail="Hoá đơn không tồn tại")
    if bill["payer_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Chỉ người chi trả hoá đơn mới có quyền xác nhận đã nhận tiền.")

    part = conn.execute("""
        SELECT * FROM bill_participants
        WHERE bill_id = ? AND user_id = ?
    """, (bill_id, data.user_id)).fetchone()

    if not part:
        raise HTTPException(status_code=400, detail="Thành viên không thuộc hoá đơn này.")

    now_str = now_iso()
    with conn:
        conn.execute("""
            UPDATE bill_participants
            SET payment_status = 'PAID', payment_confirmed_at = ?
            WHERE id = ?
        """, (now_str, part["id"]))

        # Notify debtor
        create_notification(
            conn, data.user_id, "PAYMENT_CONFIRMED",
            "Xác nhận nhận tiền",
            f"{user['display_name']} đã xác nhận nhận đủ {part['share_amount']:,} đ cho hoá đơn '{bill['title']}'.",
            ref_type="BILL", ref_id=bill_id
        )

    asyncio.create_task(dispatch_event("payment_confirmed", {
        "bill_id": bill_id,
        "debtor_id": data.user_id,
        "payer_id": user["id"],
        "amount": part["share_amount"]
    }))

    return {"message": "Đã xác nhận nhận tiền thành công"}

@app.get("/api/debts")
def get_debts(user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Returns debts where current user is debtor (Tôi nợ) and debts where current user is creditor (Người khác nợ tôi)"""
    # 1. Tôi nợ ai: Current user is participant, status in ('UNPAID', 'PAYMENT_REPORTED'), payer != current user
    i_owe_rows = conn.execute("""
        SELECT bp.id as participant_record_id, bp.bill_id, bp.share_amount, bp.payment_status,
               bp.payment_reported_at, b.title as bill_title, b.expense_date, b.category,
               u.id as creditor_id, u.display_name as creditor_name, u.avatar_url as creditor_avatar,
               u.bank_name, u.bank_account_number, u.bank_account_name
        FROM bill_participants bp
        JOIN bills b ON bp.bill_id = b.id
        JOIN users u ON b.payer_user_id = u.id
        WHERE bp.user_id = ?
          AND b.payer_user_id != ?
          AND bp.payment_status IN ('UNPAID', 'PAYMENT_REPORTED')
          AND b.deleted_at IS NULL
        ORDER BY b.expense_date DESC
    """, (user["id"], user["id"])).fetchall()

    # 2. Ai nợ tôi: Current user is bill payer, participant != current user, status in ('UNPAID', 'PAYMENT_REPORTED')
    others_owe_rows = conn.execute("""
        SELECT bp.id as participant_record_id, bp.bill_id, bp.share_amount, bp.payment_status,
               bp.payment_reported_at, b.title as bill_title, b.expense_date, b.category,
               u.id as debtor_id, u.display_name as debtor_name, u.avatar_url as debtor_avatar
        FROM bill_participants bp
        JOIN bills b ON bp.bill_id = b.id
        JOIN users u ON bp.user_id = u.id
        WHERE b.payer_user_id = ?
          AND bp.user_id != ?
          AND bp.payment_status IN ('UNPAID', 'PAYMENT_REPORTED')
          AND b.deleted_at IS NULL
        ORDER BY b.expense_date DESC
    """, (user["id"], user["id"])).fetchall()

    return {
        "i_owe": [dict(r) for r in i_owe_rows],
        "others_owe": [dict(r) for r in others_owe_rows]
    }

# ----------------- Group Fund API -----------------

@app.post("/api/fund/contributions")
async def add_contribution(data: ContributionReq, user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Số tiền đóng quỹ phải lớn hơn 0.")

    now_str = now_iso()
    with conn:
        cur = conn.execute("""
            INSERT INTO fund_transactions (type, amount, user_id, transaction_date, note, created_by, created_at)
            VALUES ('CONTRIBUTION', ?, ?, ?, ?, ?, ?)
        """, (data.amount, user["id"], data.transaction_date, data.note.strip() if data.note else "Đóng quỹ nhóm", user["id"], now_str))
        tx_id = cur.lastrowid

        # Notify team
        all_users = conn.execute("SELECT id FROM users WHERE id != ? AND status = 'ACTIVE'", (user["id"],)).fetchall()
        for u in all_users:
            create_notification(
                conn, u["id"], "FUND_CONTRIBUTION",
                "Đóng quỹ nhóm",
                f"{user['display_name']} vừa ghi nhận đóng quỹ {data.amount:,} đ.",
                ref_type="FUND", ref_id=tx_id
            )

    asyncio.create_task(dispatch_event("fund_contribution", {
        "user_id": user["id"],
        "user_name": user["display_name"],
        "amount": data.amount,
        "date": data.transaction_date,
        "note": data.note
    }))

    return {"message": "Ghi nhận đóng quỹ thành công", "tx_id": tx_id}

@app.get("/api/fund/summary")
def get_fund_summary(conn: sqlite3.Connection = Depends(get_db), user: dict = Depends(get_current_user)):
    # Total In
    in_row = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM fund_transactions
        WHERE type = 'CONTRIBUTION' AND deleted_at IS NULL
    """).fetchone()
    total_in = in_row["total"]

    # Total Out
    out_row = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM fund_transactions
        WHERE type = 'EXPENSE' AND deleted_at IS NULL
    """).fetchone()
    total_out = out_row["total"]

    balance = total_in - total_out

    # Member contributions breakdown
    members_breakdown = conn.execute("""
        SELECT u.id, u.display_name, u.avatar_url,
               COALESCE(SUM(ft.amount), 0) as total_contributed
        FROM users u
        LEFT JOIN fund_transactions ft ON u.id = ft.user_id AND ft.type = 'CONTRIBUTION' AND ft.deleted_at IS NULL
        WHERE u.status = 'ACTIVE'
        GROUP BY u.id
        ORDER BY total_contributed DESC, u.display_name ASC
    """).fetchall()

    return {
        "balance": balance,
        "total_in": total_in,
        "total_out": total_out,
        "members": [dict(m) for m in members_breakdown]
    }

@app.get("/api/fund/transactions")
def get_fund_transactions(
    type: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    query = """
        SELECT ft.*, u.display_name as member_name, u.avatar_url as member_avatar,
               b.title as bill_title
        FROM fund_transactions ft
        LEFT JOIN users u ON ft.user_id = u.id
        LEFT JOIN bills b ON ft.bill_id = b.id
        WHERE ft.deleted_at IS NULL
    """
    params = []
    if type:
        query += " AND ft.type = ?"
        params.append(type)
    query += " ORDER BY ft.transaction_date DESC, ft.id DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]

# ----------------- Notifications API -----------------

@app.get("/api/notifications")
def get_notifications(conn: sqlite3.Connection = Depends(get_db), user: dict = Depends(get_current_user)):
    rows = conn.execute("""
        SELECT * FROM notifications
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 50
    """, (user["id"],)).fetchall()
    unread_count = conn.execute("""
        SELECT COUNT(*) as count FROM notifications
        WHERE user_id = ? AND is_read = 0
    """, (user["id"],)).fetchone()["count"]
    return {
        "unread_count": unread_count,
        "notifications": [dict(r) for r in rows]
    }

@app.post("/api/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int, conn: sqlite3.Connection = Depends(get_db), user: dict = Depends(get_current_user)):
    with conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (notif_id, user["id"]))
    return {"message": "Đã đánh dấu đã đọc"}

@app.post("/api/notifications/read-all")
def mark_all_notifications_read(conn: sqlite3.Connection = Depends(get_db), user: dict = Depends(get_current_user)):
    with conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user["id"],))
    return {"message": "Đã đọc tất cả thông báo"}

# ----------------- Dashboard API -----------------

@app.get("/api/dashboard")
def get_dashboard(conn: sqlite3.Connection = Depends(get_db), user: dict = Depends(get_current_user)):
    # 1. Fund balance
    f_in = conn.execute("SELECT COALESCE(SUM(amount), 0) as s FROM fund_transactions WHERE type='CONTRIBUTION' AND deleted_at IS NULL").fetchone()["s"]
    f_out = conn.execute("SELECT COALESCE(SUM(amount), 0) as s FROM fund_transactions WHERE type='EXPENSE' AND deleted_at IS NULL").fetchone()["s"]
    fund_balance = f_in - f_out

    # 2. Total I owe
    i_owe = conn.execute("""
        SELECT COALESCE(SUM(bp.share_amount), 0) as s
        FROM bill_participants bp
        JOIN bills b ON bp.bill_id = b.id
        WHERE bp.user_id = ? AND b.payer_user_id != ?
          AND bp.payment_status IN ('UNPAID', 'PAYMENT_REPORTED')
          AND b.deleted_at IS NULL
    """, (user["id"], user["id"])).fetchone()["s"]

    # 3. Total others owe me
    others_owe = conn.execute("""
        SELECT COALESCE(SUM(bp.share_amount), 0) as s
        FROM bill_participants bp
        JOIN bills b ON bp.bill_id = b.id
        WHERE b.payer_user_id = ? AND bp.user_id != ?
          AND bp.payment_status IN ('UNPAID', 'PAYMENT_REPORTED')
          AND b.deleted_at IS NULL
    """, (user["id"], user["id"])).fetchone()["s"]

    # 4. Recent bills
    recent_bills_rows = conn.execute("""
        SELECT b.*, u.display_name as payer_name
        FROM bills b
        JOIN users u ON b.payer_user_id = u.id
        WHERE b.deleted_at IS NULL
        ORDER BY b.expense_date DESC, b.id DESC LIMIT 5
    """).fetchall()
    recent_bills = [dict(r) for r in recent_bills_rows]

    # 5. Recent fund transactions
    recent_fund_rows = conn.execute("""
        SELECT ft.*, u.display_name as member_name
        FROM fund_transactions ft
        LEFT JOIN users u ON ft.user_id = u.id
        WHERE ft.deleted_at IS NULL
        ORDER BY ft.transaction_date DESC, ft.id DESC LIMIT 5
    """).fetchall()
    recent_fund = [dict(r) for r in recent_fund_rows]

    # 6. Unread notifs
    unread_notifs = conn.execute("""
        SELECT COUNT(*) as c FROM notifications WHERE user_id = ? AND is_read = 0
    """, (user["id"],)).fetchone()["c"]

    return {
        "fund_balance": fund_balance,
        "total_i_owe": i_owe,
        "total_others_owe": others_owe,
        "recent_bills": recent_bills,
        "recent_fund_transactions": recent_fund,
        "unread_notifications_count": unread_notifs
    }

# ----------------- Static Frontend Hosting -----------------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        })
    return {"message": "Team Fund & Bill Split API running. Frontend static/index.html not created yet."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
