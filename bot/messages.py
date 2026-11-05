"""Unified user-facing messages for download failures."""

from bot.config import MAX_FILE_SIZE

_REASONS = {
    "TOO_LARGE": "файл больше лимита ({max_size})",
    "PERMANENT_FAIL": "видео недоступно (live / premiere / private / members-only)",
    "DUPLICATE": "уже скачивается другим запросом",
    "CANCELLED": "загрузка убрана из очереди (/cancel)",
    "NO_SPACE": "нет места на диске сервера",
    "AUTH_REQUIRED": "YouTube требует вход / cookies (анти-бот защита)",
    "AGE_RESTRICTED": "возрастное ограничение (нужны cookies)",
    "NETWORK": "сетевая ошибка",
}

_ADVICE = {
    "TOO_LARGE": "Увеличьте MAX_FILE_SIZE или выберите качество ниже.",
    "PERMANENT_FAIL": "Если это премьера — попробуйте после её начала.",
    "NO_SPACE": "Освободите место на сервере и повторите позже.",
    "AUTH_REQUIRED": "Это временно — попробуйте позже.",
    "AGE_RESTRICTED": "Настройте YTDLP_COOKIES — см. README.",
}


def failure_reason(code: str | None, detail: str = "", max_size: str = MAX_FILE_SIZE) -> str:
    """Short machine code → human-readable reason (used in admin reports)."""
    reason = _REASONS.get(code or "", "неизвестная ошибка").format(max_size=max_size)
    return f"{reason}: {detail}" if detail else reason


def user_error_message(code: str | None, title: str, detail: str = "") -> str:
    """Full user-facing failure message (used by all download flows)."""
    lines = [f"⚠️ {title}", f"Причина: {failure_reason(code, detail)}"]
    advice = _ADVICE.get(code or "")
    if advice:
        lines.extend(["", advice])
    return "\n".join(lines)
