#!/usr/bin/env python
"""PreToolUse hook (Edit|Write|MultiEdit).

Khi Claude sắp sửa một file source Python của project, nhắc đọc
PROJECT_DEEP_DIVE.md TRƯỚC khi sửa (theo Working Rules trong CLAUDE.md).
Chỉ nhắc 1 lần cho mỗi session để tránh spam context.
"""
import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Các file/đuôi được coi là "source hệ thống" cần đồng bộ tài liệu.
SOURCE_SUFFIXES = (".py",)
# Không nhắc với chính các file tài liệu/cấu hình.
SKIP_BASENAMES = {"PROJECT_DEEP_DIVE.md", "CLAUDE.md"}


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # input không hợp lệ → không chặn, im lặng

    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    session_id = str(data.get("session_id") or "default")

    base = os.path.basename(file_path)
    if base in SKIP_BASENAMES:
        return 0
    if not file_path.endswith(SOURCE_SUFFIXES):
        return 0

    # Một marker / session → chỉ nhắc lần đầu tiên sửa source.
    safe_sid = "".join(c for c in session_id if c.isalnum() or c in "-_")
    marker = os.path.join(tempfile.gettempdir(), f"claude_ddive_{safe_sid}")
    if os.path.exists(marker):
        return 0
    try:
        open(marker, "w").close()
    except Exception:
        pass

    reminder = (
        "[Quy tắc dự án] Bạn sắp chỉnh sửa source Python. Theo Working Rules trong "
        "CLAUDE.md: ĐỌC PROJECT_DEEP_DIVE.md để nắm đúng context phần liên quan TRƯỚC "
        "khi sửa. SAU khi sửa xong, cập nhật PROJECT_DEEP_DIVE.md (hoặc chạy /sync-trading-docs) "
        "để tài liệu cấu trúc & chức năng phản ánh đúng trạng thái mới."
    )
    out = {
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": reminder,
        },
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
