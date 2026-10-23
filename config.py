# -*- coding: utf-8 -*-
"""
config.py
=========
Quản lý cấu hình của ứng dụng: đọc file .env, các tham số API (OpenAI-compatible),
đường dẫn thư mục database và các thiết lập chung (system prompt, giới hạn context...).

Tất cả chuỗi tiếng Việt đều là Unicode (UTF-8) - không dùng escape, không dùng
mojibake để tránh lỗi phông chữ.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. Đảm bảo console/stdout luôn là UTF-8 (chống lỗi chữ ô vuông / dấu ?)
#    Trên Windows, mặc định cp1252/cp437 sẽ làm hỏng tiếng Việt khi print().
# ---------------------------------------------------------------------------
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - phụ thuộc môi trường
            pass

# ---------------------------------------------------------------------------
# 2. Đường dẫn gốc dự án
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
DATABASE_DIR: Path = BASE_DIR / "database"
STATIC_DIR: Path = BASE_DIR / "static"
TEMPLATES_DIR: Path = BASE_DIR / "templates"

DB_PATH: Path = DATABASE_DIR / "chat_history.db"
KNOWLEDGE_PATH: Path = DATABASE_DIR / "knowledge.json"
ENV_PATH: Path = BASE_DIR / ".env"

# Tạo sẵn thư mục database để SQLite không báo lỗi "unable to open database file"
DATABASE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 3. Làm sạch giá trị cấu hình (self-healing)
# ---------------------------------------------------------------------------
def _clean_value(raw: Any) -> str:
    """Làm sạch giá trị cấu hình (self-healing cho mọi kiểu điền sai phổ biến).

    Người dùng rất hay dán giá trị kèm dấu bao ngoài theo mẫu tài liệu, ví dụ:
        API_KEY="[sk-xai-abc]"    -> phải thành  sk-xai-abc
        API_KEY=<sk-xai-abc>      -> phải thành  sk-xai-abc
        API_KEY='sk-xai-abc'      -> phải thành  sk-xai-abc

    Nếu giữ nguyên dấu [] hoặc <> thì header gửi đi sẽ là
    ``Authorization: Bearer [sk-xai-abc]`` và máy chủ trả về HTTP 401
    Unauthorized. Vì vậy phải cắt bỏ các dấu bao này ở nhiều lớp.
    """
    if raw is None:
        return ""
    value = str(raw).strip()

    # Cắt nhiều lớp dấu nháy và dấu bao (ví dụ "\"[sk-abc]\"")
    for _ in range(3):
        before = value
        value = value.strip().strip('"').strip("'").strip()
        # Chỉ cắt dấu [] / <> / {} khi chúng bao trọn cả giá trị
        for opener, closer in (("[", "]"), ("<", ">"), ("{", "}")):
            if (
                len(value) >= 2
                and value.startswith(opener)
                and value.endswith(closer)
                and opener not in value[1:-1]
                and closer not in value[1:-1]
            ):
                value = value[1:-1].strip()
        # Bỏ tiền tố "Bearer " nếu người dùng dán cả header
        if value[:7].lower() == "bearer ":
            value = value[7:].strip()
        if value == before:
            break

    # Ký tự xuống dòng / tab lọt vào khi dán key
    return value.replace("\r", "").replace("\n", "").strip()


# ---------------------------------------------------------------------------
# 4. Đọc file .env (python-dotenv nếu có, fallback parser thủ công)
# ---------------------------------------------------------------------------
def _manual_env_load(env_file: Path) -> None:
    """Parser .env dự phòng khi chưa cài python-dotenv.

    Hỗ trợ: KEY=VALUE, KEY="VALUE", KEY='VALUE', bỏ qua comment (#) và dòng trống.
    Không ghi đè biến môi trường đã tồn tại trong hệ thống.
    """
    if not env_file.exists():
        return
    try:
        content = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _clean_value(value)
        if key and key not in os.environ:
            os.environ[key] = value


try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(dotenv_path=str(ENV_PATH), encoding="utf-8")
    DOTENV_AVAILABLE = True
except Exception:  # pragma: no cover - chỉ chạy khi thiếu thư viện
    _manual_env_load(ENV_PATH)
    DOTENV_AVAILABLE = False


def _env(key: str, default: str = "") -> str:
    """Đọc biến môi trường và tự động làm sạch khoảng trắng / dấu nháy / dấu bao."""
    value = os.environ.get(key, default)
    if value is None:
        return default
    return _clean_value(value)


# ---------------------------------------------------------------------------
# 5. Cấu hình API (chuẩn OpenAI-compatible - XAI247 Claude)
# ---------------------------------------------------------------------------
class Config:
    """Cấu hình trung tâm của ứng dụng."""

    # --- Flask ---
    SECRET_KEY: str = _env("SECRET_KEY", "ai-giai-dap-cong-nghe-local-secret")
    HOST: str = _env("HOST", "127.0.0.1")
    PORT: int = int(_env("PORT", "5000") or 5000)
    DEBUG: bool = _env("FLASK_DEBUG", "true").lower() in ("1", "true", "yes", "on")
    # Dùng waitress (WSGI production) khi USE_WAITRESS=true và FLASK_DEBUG=false
    USE_WAITRESS: bool = _env("USE_WAITRESS", "false").lower() in ("1", "true", "yes", "on")
    JSON_AS_ASCII: bool = False  # Bắt buộc False => JSON trả về tiếng Việt nguyên bản

    # --- API XAI247 Claude ---
    # Không hardcode khóa bí mật trong mã nguồn: chỉ đọc từ .env / biến môi trường.
    BASE_URL: str = _env("BASE_URL", "https://xai247.xyz/v1").rstrip("/")
    API_KEY: str = _env("API_KEY", "")
    MODEL_NAME: str = _env("MODEL_NAME", "claude-sonnet-5") or "claude-sonnet-5"

    # --- Tham số sinh văn bản ---
    TEMPERATURE: float = float(_env("TEMPERATURE", "0.7") or 0.7)
    MAX_TOKENS: int = int(_env("MAX_TOKENS", "4096") or 4096)
    TOP_P: float = float(_env("TOP_P", "1.0") or 1.0)

    # --- Thời gian chờ (giây) ---
    REQUEST_TIMEOUT: int = int(_env("REQUEST_TIMEOUT", "180") or 180)
    CONNECT_TIMEOUT: int = int(_env("CONNECT_TIMEOUT", "15") or 15)

    # --- Ngữ cảnh hội thoại ---
    MAX_CONTEXT_MESSAGES: int = int(_env("MAX_CONTEXT_MESSAGES", "20") or 20)
    MAX_INPUT_CHARS: int = int(_env("MAX_INPUT_CHARS", "8000") or 8000)

    # --- Knowledge Base ---
    KNOWLEDGE_TOP_K: int = int(_env("KNOWLEDGE_TOP_K", "4") or 4)
    KNOWLEDGE_MAX_CHARS: int = int(_env("KNOWLEDGE_MAX_CHARS", "6000") or 6000)
    KNOWLEDGE_ENABLED: bool = _env("KNOWLEDGE_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    # --- Đường dẫn ---
    BASE_DIR: Path = BASE_DIR
    DATABASE_DIR: Path = DATABASE_DIR
    DB_PATH: Path = DB_PATH
    KNOWLEDGE_PATH: Path = KNOWLEDGE_PATH

    # --- Trạng thái môi trường ---
    # Cho biết python-dotenv có được dùng để đọc .env hay không
    # (False = đang dùng parser .env dự phòng viết tay).
    DOTENV_AVAILABLE: bool = DOTENV_AVAILABLE

    # --- System prompt (tiếng Việt, UTF-8) ---
    SYSTEM_PROMPT: str = _env(
        "SYSTEM_PROMPT",
        (
            "Bạn là một trợ lý AI thông minh, thân thiện và chính xác, trả lời bằng "
            "tiếng Việt trừ khi người dùng yêu cầu ngôn ngữ khác.\n"
            "Hãy trả lời rõ ràng, có cấu trúc (dùng Markdown: tiêu đề, danh sách, "
            "bảng, khối code khi cần) và đi thẳng vào trọng tâm câu hỏi.\n"
            "Khi phần NGỮ CẢNH TRI THỨC được cung cấp, hãy ưu tiên dùng thông tin đó "
            "và nêu rõ khi thông tin đến từ tài liệu nội bộ. Nếu ngữ cảnh không chứa "
            "câu trả lời, hãy nói rõ và trả lời bằng kiến thức chung của bạn.\n"
            "Tuyệt đối không bịa đặt thông tin; nếu không chắc chắn, hãy nói rằng bạn "
            "không chắc chắn."
        ),
    )

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    @classmethod
    def chat_completions_url(cls) -> str:
        """Endpoint chat completions đầy đủ (chuẩn OpenAI-compatible)."""
        return f"{cls.BASE_URL}/chat/completions"

    @classmethod
    def models_url(cls) -> str:
        """Endpoint liệt kê model khả dụng."""
        return f"{cls.BASE_URL}/models"

    @classmethod
    def api_key_configured(cls) -> bool:
        """Kiểm tra API key đã được điền thật hay chưa (không còn placeholder)."""
        key = cls.API_KEY
        if not key or len(key) < 8:
            return False
        placeholders = (
            "your",
            "dien_api_key",
            "điền_api_key",
            "xxx",
            "changeme",
            "api_key_cua_ban",
        )
        lowered = key.lower()
        return not any(token in lowered for token in placeholders)

    @classmethod
    def auth_header(cls) -> str:
        """Giá trị header ``Authorization`` đúng chuẩn: ``Bearer <API_KEY>``.

        Đây là điểm duy nhất tạo header xác thực cho toàn bộ ứng dụng, giúp
        bảo đảm không bao giờ lọt dấu ``[]`` / ``<>`` / dấu nháy vào token
        (nguyên nhân phổ biến nhất của lỗi HTTP 401 Unauthorized).
        """
        return f"Bearer {_clean_value(cls.API_KEY)}"


config = Config()