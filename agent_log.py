"""agent_log.py — structured activity log for agent turns

บันทึกทุก event ของ agent (tool calls, results, final response) ลงไฟล์ JSONL
สูงสุด LOG_MAX_ENTRIES (default 5000) entries แบบ ring-buffer

ไฟล์: logs/agent_activity.jsonl
Format: 1 JSON object ต่อบรรทัด — ใช้ `tail -f` หรือ `cat | python -m json.tool` ดูได้

Event types:
  turn_start      — user query เริ่มต้น turn
  tool_call       — agent เรียก tool (name, args_preview)
  tool_result     — ผลจาก tool (name, content_preview)
  final_response  — คำตอบสุดท้ายของ agent
  synth_retry     — synthesis retry triggered (final was empty)
  error           — exception เกิดขึ้นระหว่าง turn
"""
from __future__ import annotations
import hashlib
import json
import os
import pathlib
import uuid
from collections import deque
from datetime import datetime
from typing import Any

from config import LOG_DIR, LOG_MAX_ENTRIES

_LOG_PATH      = os.path.join(LOG_DIR, "agent_activity.jsonl")
_SNAPSHOT_DIR  = os.path.join(LOG_DIR, "system_snapshots")
_MAX_QUERY_CHARS  = 500  # กัน accepted max-size input ทำให้ ring log/flush โตตาม query
_MAX_ARG_CHARS    = 300  # ตัด args ที่ยาวก่อน log
_MAX_RESULT_CHARS = 150  # ตัด tool result ก่อน log
_MAX_RESP_CHARS   = 150  # ตัด final response ก่อน log


def _truncate(v: Any, limit: int) -> str:
    s = str(v) if not isinstance(v, str) else v
    return s if len(s) <= limit else s[:limit] + f"…(+{len(s) - limit})"


class AgentLogger:
    """Ring-buffer logger — เก็บ maxlen entries ล่าสุด ทั้งใน memory และบนดิสก์"""

    def __init__(self, path: str = _LOG_PATH, maxlen: int = LOG_MAX_ENTRIES) -> None:
        self.path = path
        self.maxlen = maxlen
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._turn_start: dict[str, datetime] = {}
        self._load()

    def _load(self) -> None:
        """โหลด log เก่าจากไฟล์เข้า deque (ถ้ามี)"""
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._buf.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def new_turn_id(self) -> str:
        return str(uuid.uuid4())[:8]

    def log(self, event: str, data: dict[str, Any], turn_id: str | None = None) -> None:
        """บันทึก event 1 รายการ — append ทั้ง deque และไฟล์"""
        now = datetime.now()
        entry: dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds"),
            "turn_id": turn_id,
            "event": event,
        }
        if turn_id and turn_id in self._turn_start:
            entry["elapsed_s"] = round((now - self._turn_start[turn_id]).total_seconds(), 2)
            if event in ("final_response", "error", "cancelled"):
                self._turn_start.pop(turn_id, None)
        entry.update(data)
        self._buf.append(entry)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def flush(self) -> None:
        """Rewrite ไฟล์ให้เหลือแค่ maxlen entries ล่าสุด (rotation)
        เรียกครั้งเดียวตอนจบแต่ละ turn เพื่อลด I/O
        """
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for entry in self._buf:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ── convenience helpers ────────────────────────────────────────────────

    def turn_start(self, query: str, turn_id: str) -> None:
        self._turn_start[turn_id] = datetime.now()
        self.log("turn_start", {"query": _truncate(query, _MAX_QUERY_CHARS)}, turn_id=turn_id)

    def tool_call(self, name: str, args: dict, turn_id: str) -> None:
        args_preview = {k: _truncate(v, _MAX_ARG_CHARS) for k, v in args.items()}
        self.log("tool_call", {"name": name, "args": args_preview}, turn_id=turn_id)

    def tool_result(self, name: str, content: str, turn_id: str) -> None:
        self.log("tool_result", {
            "name": name,
            "content": _truncate(content, _MAX_RESULT_CHARS),
        }, turn_id=turn_id)

    def final_response(self, response: str, turn_id: str) -> None:
        self.log("final_response", {
            "response": _truncate(response, _MAX_RESP_CHARS),
        }, turn_id=turn_id)

    def synth_retry(self, turn_id: str, ok: bool) -> None:
        self.log("synth_retry", {"ok": ok}, turn_id=turn_id)

    def error(self, exc: Exception, turn_id: str) -> None:
        self.log("error", {"exc": str(exc)}, turn_id=turn_id)

    def tool_error(self, name: str, exc: Exception, turn_id: str) -> None:
        self.log("error", {"name": name, "exc": str(exc)}, turn_id=turn_id)

    def system_prompt_snapshot(self, prompt: str, turn_id: str) -> str:
        """Log system prompt hash; write full text to system_snapshots/<hash>.txt only on first seen."""
        h = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        path = os.path.join(_SNAPSHOT_DIR, f"{h}.txt")
        if not os.path.exists(path):
            try:
                pathlib.Path(_SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(prompt)
            except Exception:
                pass
        self.log("system_prompt", {"hash": h}, turn_id=turn_id)
        return h
