#!/usr/bin/env python
"""Stop hook.

Khi Claude kết thúc lượt làm việc, nếu có file source .py thay đổi nhưng
PROJECT_DEEP_DIVE.md chưa được đụng tới trong cùng đợt thay đổi đó, nhắc
người dùng/Claude chạy /sync-docs để đồng bộ tài liệu hệ thống.
"""
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# project root = .../<root>/.claude/hooks/stop_sync_docs_reminder.py → lên 2 cấp
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def changed_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout
    except Exception:
        return []
    files = []
    for line in out.splitlines():
        # định dạng porcelain: "XY <path>" (path bắt đầu ở cột 4)
        path = line[3:].strip().strip('"')
        if path:
            files.append(path)
    return files


def main() -> int:
    files = changed_files()
    py_changed = [f for f in files if f.endswith(".py")]
    docs_changed = any(os.path.basename(f) == "PROJECT_DEEP_DIVE.md" for f in files)

    if py_changed and not docs_changed:
        n = len(py_changed)
        sample = ", ".join(os.path.basename(f) for f in py_changed[:4])
        if n > 4:
            sample += f", … (+{n - 4})"
        msg = (
            f"📝 {n} file .py đã thay đổi ({sample}) nhưng PROJECT_DEEP_DIVE.md chưa "
            f"cập nhật. Cân nhắc chạy /sync-trading-docs để đồng bộ tài liệu cấu trúc & chức năng."
        )
        print(json.dumps({"systemMessage": msg}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
