# -*- coding: utf-8 -*-
"""
app.py
======
Backend chính (Flask) cho "AI Giải Đáp Công Nghệ" - trợ lý AI chạy cục bộ.

Chức năng:
    * REST API quản lý phiên chat & tin nhắn (SQLite: database/chat_history.db)
    * Tra cứu Knowledge Base ngoài (database/knowledge.json) và bơm vào prompt
    * Gọi API XAI247 theo chuẩn OpenAI-compatible với STREAMING (SSE)
    * Xử lý ngoại lệ đầy đủ: thiếu API Key, 401/403/404/429, timeout, mất mạng

Chạy:  python app.py
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, List, Optional, Tuple

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from werkzeug.exceptions import HTTPException

from config import Config, config

# ---------------------------------------------------------------------------
# Logging (UTF-8 để log tiếng Việt không bị lỗi font trên Windows)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("chatbot")


# ---------------------------------------------------------------------------
# Flask app + cấu hình UTF-8 tuyệt đối
# ---------------------------------------------------------------------------
app = Flask(
    __name__,
    static_folder=str(Config.BASE_DIR / "static"),
    template_folder=str(Config.BASE_DIR / "templates"),
    static_url_path="/static",
)
app.config["SECRET_KEY"] = Config.SECRET_KEY
app.config["JSON_AS_ASCII"] = False
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB mỗi request
app.json.ensure_ascii = False  # Flask >= 2.3


@app.after_request
def _force_utf8_headers(response: Response) -> Response:
    """Ép kiểu nội dung UTF-8 cho mọi phản hồi, tránh lỗi font tiếng Việt.

    Chỉ thêm ``charset=utf-8`` khi header CHƯA có charset, và luôn gộp các
    charset trùng lặp (ví dụ ``...charset=utf-8; charset=utf-8``) để header
    luôn hợp lệ và sạch.
    """
    ctype = response.headers.get("Content-Type", "")
    if not ctype:
        return response

    # Tách mime type gốc và loại bỏ mọi khai báo charset trùng lặp
    parts = [segment.strip() for segment in ctype.split(";")]
    base_mime = parts[0]
    has_charset = any(segment.lower().startswith("charset") for segment in parts[1:])

    if has_charset:
        # Chuẩn hoá lại thành đúng 1 khai báo charset=utf-8
        response.headers["Content-Type"] = f"{base_mime}; charset=utf-8"
    elif base_mime.startswith("application/json") or base_mime.startswith("text/"):
        response.headers["Content-Type"] = f"{base_mime}; charset=utf-8"

    return response


# ---------------------------------------------------------------------------
# SQLite - kết nối & khởi tạo schema
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Context manager mở kết nối SQLite (row_factory = sqlite3.Row)."""
    conn = sqlite3.connect(str(Config.DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'Cuộc trò chuyện mới',
    model       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    sources     TEXT,
    model       TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions (updated_at DESC);
"""


def init_db() -> None:
    """Khởi tạo schema SQLite (idempotent - chạy lại an toàn)."""
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
        # Tự sửa lỗi schema cũ: bổ sung cột nếu bảng đã tồn tại từ phiên bản trước
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        for column, ddl in (("sources", "TEXT"), ("model", "TEXT")):
            if column not in existing:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {ddl}")
    logger.info("SQLite đã sẵn sàng: %s", Config.DB_PATH)


# ---------------------------------------------------------------------------
# Tiện ích chuỗi & UTF-8
# ---------------------------------------------------------------------------
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(raw: Any, max_chars: int = 0) -> str:
    """Làm sạch đầu vào: bỏ ký tự điều khiển, gộp khoảng trắng thừa, chuẩn hóa Unicode."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    # Chuẩn hóa NFC giúp tiếng Việt luôn ở dạng tổ hợp thống nhất
    text = unicodedata.normalize("NFC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS_RE.sub("", text)
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in text.split("\n")]
    text = "\n".join(lines).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def strip_accents(text: str) -> str:
    """Bỏ dấu tiếng Việt (dùng cho tìm kiếm không phân biệt dấu)."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


_WORD_RE = re.compile(r"[0-9a-z_]+", re.UNICODE)
_STOPWORDS = {
    "la", "va", "cua", "cho", "voi", "thi", "ma", "nhu", "the", "nao", "gi",
    "co", "khong", "duoc", "trong", "tren", "duoi", "ra", "vao", "den", "tu",
    "mot", "cac", "nhung", "nay", "do", "khi", "de", "hay", "boi", "bang",
    "toi", "ban", "minh", "and", "for", "with", "that", "this", "you", "are",
}


def tokenize(text: str, remove_stopwords: bool = True) -> List[str]:
    """Tách token tiếng Việt đơn giản: thường hóa + bỏ dấu + lọc stopword."""
    lowered = strip_accents(text.lower())
    tokens = _WORD_RE.findall(lowered)
    if remove_stopwords:
        tokens = [t for t in tokens if len(t) > 1 and t not in _STOPWORDS]
    return tokens


def truncate(text: str, limit: int = 160) -> str:
    """Rút gọn văn bản để hiển thị/hiển thị tiêu đề."""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Thông báo lỗi thân thiện (tiếng Việt) cho mọi tình huống thất bại
# ---------------------------------------------------------------------------
def friendly_error(status: Optional[int] = None, body: str = "", exc: Optional[BaseException] = None) -> str:
    """Chuyển lỗi kỹ thuật thành thông báo dễ hiểu cho người dùng cuối.

    Args:
        status: mã HTTP (nếu có).
        body:   nội dung phản hồi thô từ API (nếu có), dùng để trích xuất thông điệp gốc.
        exc:    ngoại lệ Python (Timeout, ConnectionError, ...).

    Returns:
        Chuỗi tiếng Việt mô tả lỗi và cách khắc phục.
    """
    detail = ""
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or err.get("type") or "")
                elif err:
                    detail = str(err)
                detail = detail or str(parsed.get("message") or "")
            elif isinstance(parsed, list) and parsed:
                detail = str(parsed[0])
        except (json.JSONDecodeError, TypeError, ValueError):
            detail = truncate(re.sub(r"<[^>]+>", " ", body or ""), 200)

    detail = truncate(sanitize_text(detail), 200)

    # Lỗi do ngoại lệ mạng trước tiên
    if isinstance(exc, requests.exceptions.Timeout):
        return (
            "Máy chủ AI phản hồi quá chậm (hết thời gian chờ). "
            "Vui lòng thử lại, hoặc tăng REQUEST_TIMEOUT trong file config.py."
        )
    if isinstance(exc, requests.exceptions.SSLError):
        return "Lỗi chứng chỉ SSL khi kết nối tới máy chủ AI. Hãy kiểm tra BASE_URL trong file .env."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return (
            "Không kết nối được tới máy chủ AI. Hãy kiểm tra kết nối Internet, "
            "BASE_URL trong file .env và thử lại."
        )
    if isinstance(exc, requests.exceptions.RequestException):
        return f"Lỗi kết nối tới máy chủ AI: {type(exc).__name__}."

    if status == 400:
        base = "Yêu cầu không hợp lệ (HTTP 400). Có thể câu hỏi quá dài hoặc sai định dạng."
    elif status == 401:
        base = (
            "API Key không hợp lệ hoặc đã hết hạn (HTTP 401). "
            "Hãy kiểm tra lại API_KEY trong file .env."
        )
    elif status == 402:
        base = "Tài khoản AI đã hết hạn mức/credit (HTTP 402). Vui lòng nạp thêm hoặc đổi API Key."
    elif status == 403:
        base = (
            "API Key không có quyền truy cập model này (HTTP 403). "
            "Hãy kiểm tra MODEL_NAME và quyền của API Key."
        )
    elif status == 404:
        base = (
            "Không tìm thấy endpoint hoặc model (HTTP 404). "
            "Hãy kiểm tra BASE_URL và MODEL_NAME trong file .env."
        )
    elif status == 408:
        base = "Máy chủ AI hết thời gian chờ yêu cầu (HTTP 408). Vui lòng thử lại."
    elif status == 429:
        base = (
            "Bạn đã gửi quá nhiều yêu cầu (HTTP 429 - rate limit). "
            "Vui lòng đợi vài giây rồi thử lại."
        )
    elif status is not None and 500 <= status < 600:
        base = f"Máy chủ AI đang gặp sự cố (HTTP {status}). Vui lòng thử lại sau ít phút."
    elif status is not None:
        base = f"Máy chủ AI trả về lỗi HTTP {status}."
    else:
        base = "Đã xảy ra lỗi không xác định khi gọi máy chủ AI."

    if detail:
        return f"{base}\n\nChi tiết từ máy chủ: {detail}"
    return base


# ---------------------------------------------------------------------------
# Knowledge Base ngoài (database/knowledge.json)
# ---------------------------------------------------------------------------
_KB_CACHE: dict = {"mtime": None, "docs": []}


def load_knowledge(reload_if_changed: bool = True) -> List[dict]:
    """Đọc Knowledge Base từ JSON, có cache theo mtime để không đọc lại mỗi request.

    Hỗ trợ 3 dạng file:
        - {"documents": [ ... ]}
        - {"items": [ ... ]}
        - [ ... ]  (mảng phẳng)
    """
    path = Config.KNOWLEDGE_PATH
    if not path.exists():
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _KB_CACHE["docs"]

    if reload_if_changed and _KB_CACHE["mtime"] == mtime and _KB_CACHE["docs"]:
        return _KB_CACHE["docs"]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Không đọc được knowledge.json: %s", exc)
        return _KB_CACHE["docs"]

    if isinstance(raw, dict):
        items = raw.get("documents") or raw.get("items") or raw.get("data") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    docs: List[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        content = sanitize_text(item.get("content", ""))
        if not content:
            continue
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        docs.append(
            {
                "id": str(item.get("id") or f"kb-{index + 1:03d}"),
                "title": sanitize_text(item.get("title", "Không có tiêu đề")),
                "content": content,
                "tags": [sanitize_text(str(t)) for t in tags],
                "source": sanitize_text(item.get("source", "")),
                "priority": int(item.get("priority") or 5),
            }
        )

    _KB_CACHE["mtime"] = mtime
    _KB_CACHE["docs"] = docs
    logger.info("Đã nạp %d tài liệu tri thức từ knowledge.json", len(docs))
    return docs


def search_knowledge(query: str, top_k: Optional[int] = None) -> List[dict]:
    """Tìm tài liệu liên quan nhất bằng chấm điểm từ khóa (title x4, tags x3, content x1)."""
    docs = load_knowledge()
    if not docs:
        return []

    top_k = top_k or Config.KNOWLEDGE_TOP_K
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    query_set = set(query_tokens)
    scored: List[Tuple[float, dict]] = []

    for doc in docs:
        title_set = set(tokenize(doc["title"]))
        tag_set = set(tokenize(" ".join(doc["tags"])))
        content_set = set(tokenize(doc["content"]))

        score = 0.0
        score += 4.0 * len(query_set & title_set)
        score += 3.0 * len(query_set & tag_set)
        score += 1.0 * len(query_set & content_set)

        # Thưởng điểm khi cụm từ trong câu hỏi xuất hiện nguyên vẹn trong tài liệu
        lowered_query = strip_accents(query.lower()).strip()
        haystack = strip_accents(
            f"{doc['title']} {' '.join(doc['tags'])} {doc['content']}".lower()
        )
        if len(lowered_query) > 8 and lowered_query in haystack:
            score += 5.0

        if score > 0:
            score += min(doc["priority"], 10) * 0.1
            scored.append((score, doc))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = []
    for score, doc in scored[:top_k]:
        enriched = dict(doc)
        enriched["score"] = round(score, 3)
        results.append(enriched)
    return results


def build_knowledge_context(
    query: str, enabled: Optional[bool] = None
) -> Tuple[str, List[dict]]:
    """Tạo khối ngữ cảnh tri thức để chèn vào prompt.

    enabled: bật/tắt tra cứu cho riêng request này. Khi None thì dùng
    Config.KNOWLEDGE_ENABLED. Truyền tham số (thay vì sửa thuộc tính class)
    để an toàn khi server chạy threaded=True.

    Trả về (context_text, sources) - sources là danh sách gọn để hiển thị ở UI.
    """
    if not (Config.KNOWLEDGE_ENABLED if enabled is None else enabled):
        return "", []

    hits = search_knowledge(query)
    if not hits:
        return "", []

    blocks: List[str] = []
    sources: List[dict] = []
    used_chars = 0

    for index, doc in enumerate(hits, start=1):
        block = (
            f"[Tài liệu {index}] {doc['title']}\n"
            + (f"Nguồn: {doc['source']}\n" if doc["source"] else "")
            + f"Nội dung:\n{doc['content']}"
        )
        if used_chars + len(block) > Config.KNOWLEDGE_MAX_CHARS:
            break
        blocks.append(block)
        used_chars += len(block)
        sources.append(
            {
                "id": doc["id"],
                "title": doc["title"],
                "source": doc["source"],
                "score": doc["score"],
            }
        )

    if not blocks:
        return "", []

    context_text = (
        "=== NGỮ CẢNH TRI THỨC (tài liệu nội bộ, hãy ưu tiên sử dụng) ===\n"
        + "\n\n".join(blocks)
        + "\n=== HẾT NGỮ CẢNH TRI THỨC ==="
    )
    return context_text, sources


# ---------------------------------------------------------------------------
# CRUD: Sessions & Messages (dùng parameterized query - chống SQL Injection)
# ---------------------------------------------------------------------------
def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def create_session(title: str = "", model: str = "") -> dict:
    session_id = uuid.uuid4().hex
    now = _now_iso()
    clean_title = sanitize_text(title, 120) or "Cuộc trò chuyện mới"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, clean_title, model or Config.MODEL_NAME, now, now),
        )
    return {
        "id": session_id,
        "title": clean_title,
        "model": model or Config.MODEL_NAME,
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
    }


def get_session(session_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row_to_dict(row) if row else None


def list_sessions(limit: int = 100) -> List[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                   AS message_count
            FROM sessions s
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def ensure_session(session_id: Optional[str]) -> dict:
    """Lấy session theo id, tự tạo mới nếu không tồn tại (self-healing)."""
    if session_id:
        existing = get_session(session_id)
        if existing:
            return existing
    return create_session()


def update_session_title(session_id: str, title: str) -> None:
    clean_title = sanitize_text(title, 120)
    if not clean_title:
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (clean_title, _now_iso(), session_id),
        )


def touch_session(session_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (_now_iso(), session_id),
        )


def delete_session(session_id: str) -> bool:
    with get_db() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cursor.rowcount > 0


def delete_all_sessions() -> int:
    with get_db() as conn:
        conn.execute("DELETE FROM messages")
        cursor = conn.execute("DELETE FROM sessions")
    return cursor.rowcount


def add_message(
    session_id: str,
    role: str,
    content: str,
    sources: Optional[List[dict]] = None,
    model: str = "",
) -> dict:
    now = _now_iso()
    sources_json = json.dumps(sources, ensure_ascii=False) if sources else None
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, sources, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, content, sources_json, model or None, now),
        )
        message_id = cursor.lastrowid
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
    return {
        "id": message_id,
        "session_id": session_id,
        "role": role,
        "content": content,
        "sources": sources or [],
        "model": model,
        "created_at": now,
    }


def get_messages(session_id: str) -> List[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    messages = []
    for row in rows:
        item = row_to_dict(row)
        raw_sources = item.pop("sources", None)
        try:
            item["sources"] = json.loads(raw_sources) if raw_sources else []
        except (TypeError, json.JSONDecodeError):
            item["sources"] = []
        messages.append(item)
    return messages


def get_context_messages(session_id: str, limit: int = 0) -> List[dict]:
    """Lấy N tin nhắn gần nhất (theo thứ tự thời gian) để làm ngữ cảnh cho API."""
    limit = limit or Config.MAX_CONTEXT_MESSAGES
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE session_id = ? AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


# ---------------------------------------------------------------------------
# Gọi API XAI247 (chuẩn OpenAI-compatible) - có streaming
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """Lỗi có cấu trúc từ phía API (kèm mã trạng thái HTTP)."""

    def __init__(self, status_code: int, body: str = "", message: str = ""):
        self.status_code = status_code
        self.body = body or ""
        self.message = message or friendly_error(status_code, self.body)
        super().__init__(self.message)


def build_payload(messages: List[dict], stream: bool) -> dict:
    payload = {
        "model": Config.MODEL_NAME,
        "messages": messages,
        "temperature": Config.TEMPERATURE,
        "max_tokens": Config.MAX_TOKENS,
        "top_p": Config.TOP_P,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": False}
    return payload


def build_headers() -> dict:
    """Header gửi tới XAI247.

    ``Authorization`` BẮT BUỘC đúng dạng ``Bearer <API_KEY>``. Chuỗi token được
    làm sạch (bỏ dấu nháy / dấu bao ``[]`` ``<>``) để tránh hoàn toàn lỗi
    HTTP 401 Unauthorized.
    """
    return {
        "Authorization": Config.auth_header(),
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "text/event-stream, application/json",
        "User-Agent": "AI-Giai-Dap-Cong-Nghe/1.0 (+python-requests)",
    }


def build_final_messages(
    user_input: str, session_id: str, use_knowledge: Optional[bool] = None
) -> Tuple[List[dict], List[dict]]:
    """Ghép: system prompt + ngữ cảnh tri thức + lịch sử hội thoại + câu hỏi mới."""
    context_text, sources = build_knowledge_context(user_input, use_knowledge)
 # Ép cứng lời chào và danh tính mới vào đây
    system_prompt = (
        "Xin chào! 👋 Tôi là AI Giải Đáp Công Nghệ. Rất vui được gặp bạn!\n"
        "Bạn là một trợ lý AI chuyên nghiệp, hỗ trợ giải đáp các vấn đề về công nghệ, kỹ thuật và lập trình. "
        "Tuyệt đối không bao giờ nhận mình là Claude hay thuộc về Anthropic."
    )
    if context_text:
        system_prompt = (
            f"{system_prompt}\n\n{context_text}\n\n"
            "Hãy dùng ngữ cảnh trên khi nó thực sự liên quan tới câu hỏi. "
            "Nếu ngữ cảnh không liên quan, hãy bỏ qua và trả lời bằng kiến thức chung."
        )
       
    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(get_context_messages(session_id))
    messages.append({"role": "user", "content": user_input})
    return messages, sources


def iter_stream_deltas(response: requests.Response) -> Iterator[str]:
    """Nhận generator các dòng SSE từ requests và trả về phần text của từng delta.

    Xử lý cả 2 định dạng phổ biến:
        - OpenAI:  {"choices":[{"delta":{"content":"..."}}]}
        - Một số gateway trả: {"choices":[{"delta":{"reasoning_content":"..."}}]}
    """
    for raw_line in response.iter_lines(decode_unicode=False):
        if raw_line is None:
            continue
        try:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        except (UnicodeDecodeError, AttributeError):
            continue
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        # API trả lỗi ngay trong luồng stream
        if isinstance(chunk, dict) and chunk.get("error"):
            err = chunk["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(message or "API trả về lỗi trong luồng stream")

        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        text = delta.get("content")
        if text:
            yield text
        # Một số model trả "reasoning_content" - bỏ qua để không lẫn vào câu trả lời
        elif delta.get("reasoning_content"):
            continue


def extract_message_content(data: dict) -> str:
    """Lấy nội dung text từ phản hồi non-stream."""
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Một số gateway trả content dạng mảng block
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in (None, "text")
        ]
        return "".join(parts)
    return content or ""


def call_api_non_stream(messages: List[dict]) -> Tuple[str, dict]:
    """Gọi API không streaming (dự phòng khi streaming không khả dụng)."""
    url = Config.chat_completions_url()
    payload = build_payload(messages, stream=False)
    response = requests.post(
        url,
        headers=build_headers(),
        json=payload,
        timeout=(Config.CONNECT_TIMEOUT, Config.REQUEST_TIMEOUT),
    )
    if response.status_code >= 400:
        raise ApiError(response.status_code, response.text)
    data = response.json()
    return extract_message_content(data), data.get("usage") or {}


def call_api_stream(messages: List[dict]) -> Iterator[str]:
    """Gọi API ở chế độ streaming, trả về generator text từng phần."""
    url = Config.chat_completions_url()
    payload = build_payload(messages, stream=True)
    response = requests.post(
        url,
        headers=build_headers(),
        json=payload,
        stream=True,
        timeout=(Config.CONNECT_TIMEOUT, Config.REQUEST_TIMEOUT),
    )

    if response.status_code >= 400:
        body = ""
        try:
            body = response.text
        finally:
            response.close()
        raise ApiError(response.status_code, body)

    try:
        yield from iter_stream_deltas(response)
    finally:
        response.close()


# ---------------------------------------------------------------------------
# Routes - giao diện & REST API
# ---------------------------------------------------------------------------
def sse_event(event: str, data: dict) -> str:
    """Đóng gói một sự kiện SSE (UTF-8, an toàn với mọi ký tự)."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@app.route("/")
def index():
    """Trang giao diện chính."""
    return render_template(
        "index.html",
        model_name=Config.MODEL_NAME,
        base_url=Config.BASE_URL,
        knowledge_enabled=Config.KNOWLEDGE_ENABLED,
    )


@app.route("/api/health", methods=["GET"])
def api_health():
    """Kiểm tra tình trạng backend, cấu hình API Key và số tài liệu tri thức."""
    return jsonify(
        {
            "status": "ok",
            "app_name": "AI Giải Đáp Công Nghệ",
            "model": Config.MODEL_NAME,
            "base_url": Config.BASE_URL,
            "api_key_configured": Config.api_key_configured(),
            "knowledge_enabled": Config.KNOWLEDGE_ENABLED,
            "knowledge_documents": len(load_knowledge()),
            "dotenv_available": Config.DOTENV_AVAILABLE,
            "time": _now_iso(),
        }
    )


# --------------------------- Sessions CRUD --------------------------------
@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    """Liệt kê các phiên chat, mới nhất lên đầu."""
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    return jsonify({"sessions": list_sessions(limit)})


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    """Tạo phiên chat mới."""
    body = request.get_json(silent=True) or {}
    title = sanitize_text(body.get("title", ""), 120)
    session = create_session(title)
    return jsonify({"session": session}), 201


@app.route("/api/sessions/<session_id>", methods=["GET"])
def api_get_session(session_id: str):
    """Lấy chi tiết một phiên chat kèm toàn bộ tin nhắn."""
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    return jsonify({"session": session, "messages": get_messages(session_id)})


@app.route("/api/sessions/<session_id>", methods=["PATCH", "PUT"])
def api_rename_session(session_id: str):
    """Đổi tiêu đề phiên chat."""
    if not get_session(session_id):
        return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    body = request.get_json(silent=True) or {}
    title = sanitize_text(body.get("title", ""), 120)
    if not title:
        return jsonify({"error": "Tiêu đề không được để trống."}), 400
    update_session_title(session_id, title)
    return jsonify({"session": get_session(session_id)})


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id: str):
    """Xóa một phiên chat và toàn bộ tin nhắn của nó."""
    if not delete_session(session_id):
        return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    return jsonify({"deleted": True, "session_id": session_id})


@app.route("/api/sessions", methods=["DELETE"])
def api_delete_all_sessions():
    """Xóa toàn bộ lịch sử trò chuyện."""
    removed = delete_all_sessions()
    return jsonify({"deleted": True, "sessions_removed": removed})


@app.route("/api/sessions/<session_id>/messages", methods=["GET"])
def api_get_messages(session_id: str):
    """Lấy danh sách tin nhắn của một phiên."""
    if not get_session(session_id):
        return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    return jsonify({"messages": get_messages(session_id)})


@app.route("/api/knowledge", methods=["GET"])
def api_knowledge():
    """Cho phép tra cứu thử Knowledge Base từ trình duyệt."""
    query = sanitize_text(request.args.get("q", ""), 500)
    if not query:
        return jsonify({"error": "Thiếu tham số q."}), 400
    hits = search_knowledge(query)
    return jsonify(
        {
            "query": query,
            "count": len(hits),
            "results": [
                {
                    "id": doc["id"],
                    "title": doc["title"],
                    "source": doc["source"],
                    "score": doc["score"],
                    "snippet": truncate(doc["content"], 200),
                }
                for doc in hits
            ],
        }
    )


# --------------------------- Chat (SSE streaming) --------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Nhận câu hỏi và trả lời dạng SSE: meta -> delta* -> done (hoặc error)."""
    body = request.get_json(silent=True) or {}
    user_input = sanitize_text(body.get("message", ""), 8000)

    if not user_input:
        return jsonify({"error": "Vui lòng nhập nội dung câu hỏi."}), 400

    # Bật/tắt tra cứu tri thức cho RIÊNG request này (mặc định theo cấu hình).
    # Dùng biến cục bộ thay vì ghi vào Config.KNOWLEDGE_ENABLED để tránh
    # tranh chấp giữa các request khi server chạy threaded=True.
    use_knowledge = bool(body.get("use_knowledge", Config.KNOWLEDGE_ENABLED))

    # Thiếu API Key -> trả lỗi JSON ngay, không mở stream
    if not Config.api_key_configured():
        return (
            jsonify(
                {
                    "error": (
                        "Chưa cấu hình API Key. Hãy mở file .env, điền giá trị cho "
                        "API_KEY rồi khởi động lại ứng dụng."
                    ),
                    "code": "missing_api_key",
                }
            ),
            400,
        )

    session = ensure_session(sanitize_text(body.get("session_id", ""), 64) or None)
    session_id = session["id"]

    def generate():
        # Lưu câu hỏi của người dùng trước khi gọi API
        try:
            add_message(session_id, "user", user_input)
        except sqlite3.Error as exc:
            logger.error("Không lưu được tin nhắn người dùng: %s", exc)
            yield sse_event("error", {"error": "Không lưu được tin nhắn vào cơ sở dữ liệu."})
            return

        # Đặt tiêu đề phiên theo câu hỏi đầu tiên
        try:
            if len(get_messages(session_id)) <= 1:
                update_session_title(session_id, truncate(user_input, 60))
        except sqlite3.Error:
            pass

        messages, sources = build_final_messages(user_input, session_id, use_knowledge)

        yield sse_event(
            "meta",
            {
                "session_id": session_id,
                "model": Config.MODEL_NAME,
                "knowledge_used": bool(sources),
                "sources": sources,
            },
        )

        answer_parts: List[str] = []
        try:
            for chunk in call_api_stream(messages):
                if not chunk:
                    continue
                answer_parts.append(chunk)
                yield sse_event("delta", {"text": chunk})

            answer = "".join(answer_parts).strip()

            # Model không hỗ trợ stream -> thử lại ở chế độ thường
            if not answer:
                logger.warning("Stream rỗng, chuyển sang chế độ non-stream.")
                answer, _usage = call_api_non_stream(messages)
                answer = (answer or "").strip()
                if answer:
                    yield sse_event("delta", {"text": answer})

            if not answer:
                yield sse_event(
                    "error",
                    {"error": "AI không trả về nội dung nào. Vui lòng thử lại với câu hỏi khác."},
                )
                return

            saved = add_message(
                session_id,
                "assistant",
                answer,
                sources=sources,
                model=Config.MODEL_NAME,
            )
            current_session = get_session(session_id)
            yield sse_event(
                "done",
                {
                    "session_id": session_id,
                    "message_id": saved["id"],
                    "content": answer,
                    "model": Config.MODEL_NAME,
                    "title": current_session["title"] if current_session else "",
                },
            )

        except ApiError as exc:
            logger.error("ApiError: %s", exc)
            yield sse_event("error", {"status": exc.status_code, "error": exc.message})
        except requests.exceptions.Timeout as exc:
            logger.error("Timeout: %s", exc)
            yield sse_event("error", {"error": friendly_error(None, exc=exc)})
        except requests.exceptions.ConnectionError as exc:
            logger.error("ConnectionError: %s", exc)
            yield sse_event("error", {"error": friendly_error(None, exc=exc)})
        except Exception as exc:  # pragma: no cover - lưới an toàn cuối cùng
            logger.exception("Lỗi không lường trước trong /api/chat")
            yield sse_event("error", {"error": friendly_error(None, exc=exc)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------- Xử lý lỗi toàn cục ---------------------------
def _index_500(status: int):
    return (
        render_template(
            "index.html",
            model_name=Config.MODEL_NAME,
            base_url=Config.BASE_URL,
            knowledge_enabled=Config.KNOWLEDGE_ENABLED,
        ),
        status,
    )


@app.errorhandler(404)
def handle_404(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Không tìm thấy tài nguyên yêu cầu (HTTP 404)."}), 404
    return _index_500(404)


@app.errorhandler(405)
def handle_405(error):
    return jsonify({"error": "Phương thức HTTP không được hỗ trợ (HTTP 405)."}), 405


@app.errorhandler(413)
def handle_413(error):
    return jsonify({"error": "Nội dung gửi lên quá lớn (giới hạn 2 MB)."}), 413


@app.errorhandler(500)
def handle_500(error):
    logger.exception("Lỗi máy chủ nội bộ")
    if request.path.startswith("/api/"):
        return jsonify({"error": "Lỗi máy chủ nội bộ. Vui lòng thử lại sau."}), 500
    return _index_500(500)


@app.errorhandler(Exception)
def handle_unexpected(error):
    # Cho phép HTTPException (404/405...) đi qua handler riêng của Flask
    if isinstance(error, HTTPException):
        return error
    logger.exception("Ngoại lệ không được xử lý: %s", error)
    if request.path.startswith("/api/"):
        return jsonify({"error": friendly_error(None, exc=error)}), 500
    return _index_500(500)


# ---------------------------------------------------------------------------
# Khởi động ứng dụng
# ---------------------------------------------------------------------------
def bootstrap() -> None:
    """Khởi tạo database + nạp Knowledge Base (gọi trước khi serve)."""
    init_db()
    if Config.KNOWLEDGE_ENABLED:
        docs = load_knowledge()
        logger.info("Knowledge Base: %d tài liệu", len(docs))

    if not Config.api_key_configured():
        logger.warning(
            "CHƯA cấu hình API_KEY trong file .env - giao diện vẫn mở được "
            "nhưng chat sẽ báo lỗi cho tới khi bạn điền API Key."
        )
    logger.info("Model: %s | Base URL: %s", Config.MODEL_NAME, Config.BASE_URL)


bootstrap()


def run_dev_server() -> None:
    """Chạy Flask dev server (có reloader, tiện khi đang phát triển)."""
    app.run(
        host=Config.HOST,
        port=Config.PORT,
        debug=Config.DEBUG,
        threaded=True,
        use_reloader=False,
    )


def run_production_server() -> None:
    """Chạy bằng waitress - WSGI server ổn định, ổn định hơn dev server.

    Dùng tự động khi đặt biến môi trường ``USE_WAITRESS=true`` trong file .env.
    Nếu chưa cài waitress thì tự động quay về Flask dev server (self-healing).
    """
    try:
        from waitress import serve  # type: ignore
    except ImportError:
        logger.warning("Chưa cài waitress -> tạm dùng Flask dev server.")
        run_dev_server()
        return

    logger.info("Đang phục vụ bằng waitress (WSGI) tại http://%s:%s", Config.HOST, Config.PORT)
    serve(app, host=Config.HOST, port=Config.PORT, threads=8, channel_timeout=300)


if __name__ == "__main__":
    print("=" * 66)
    print("  AI Giải Đáp Công Nghệ - trợ lý AI chạy cục bộ (Local)")
    print(f"  Mở trình duyệt: http://{Config.HOST}:{Config.PORT}")
    print(f"  Model: {Config.MODEL_NAME} | Base URL: {Config.BASE_URL}")
    if not Config.api_key_configured():
        print("  [!] CHƯA cấu hình API_KEY trong file .env (chat sẽ báo lỗi).")
    print("=" * 66)

    if Config.USE_WAITRESS and not Config.DEBUG:
        run_production_server()
    else:
        run_dev_server()
