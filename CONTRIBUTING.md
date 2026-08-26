# Contributing

**English** | [ภาษาไทย](#ภาษาไทย)

## Development setup

Use macOS on Apple Silicon with Python 3.11. Run the installer, which creates
a project-local `.venv` and installs the hash-locked dependencies:

```bash
bash install_library/install.sh
source .venv/bin/activate
python3 server.py
```

There is no separate build step for the three optional Swift helpers
(`tools/_accessibility.swift`, `tools/_ocr.py`'s Vision OCR helper, and the
speech-transcription helper): each compiles itself on first use if Xcode
Command Line Tools are present, and self-heals on the next call if the
source changed since the last compile.

Do not commit `.venv/`, `logs/`, `workspace/`, `bin/tunnel-client`, or any
`.env` file — all already covered by `.gitignore`.

## Change expectations

- Keep MCP protocol traffic on stdout only; send all diagnostics to stderr
  or `logs/agent_activity.jsonl` via the existing `_logged` wrapper in
  `server.py`.
- Model-facing tool documentation lives in `server.py`'s own
  `@mcp.tool()`-decorated functions, not in `tools/*.py`'s own docstrings —
  see the comment at the top of `server.py` before changing what a tool
  tells ChatGPT.
- Treat file paths, credentials, tokens, and personal documents as
  sensitive. Never hardcode a real Tunnel ID, API key, or a specific
  developer's home-directory path — keep examples generic.
- Preserve `computer`'s safety behavior (Accessibility requirement,
  password-field refusal, destructive-action guard, fresh-observation
  checks, and post-action verification) and the `edit`/`write_file`
  per-folder permission gate (`tools/_edit_grants.py`) — these are the
  project's actual security boundary, not incidental behavior.
- Run the "Before sharing" privacy check in `README.md` before opening a
  pull request that touches anything outside `tools/`.

---

# ภาษาไทย

[English](#contributing)

## เตรียมเครื่องสำหรับพัฒนา

ใช้ macOS บน Apple Silicon กับ Python 3.11 รันตัวติดตั้ง ซึ่งจะสร้าง
`.venv` เฉพาะโปรเจกต์และติดตั้ง dependency ที่ lock hash ไว้:

```bash
bash install_library/install.sh
source .venv/bin/activate
python3 server.py
```

Swift helper เสริมทั้ง 3 ตัว (`tools/_accessibility.swift`, ตัว Vision OCR
ใน `tools/_ocr.py`, และตัวถอดเสียงพูด) ไม่มีขั้นตอน build แยกต่างหาก —
แต่ละตัว compile ตัวเองตอนถูกเรียกใช้ครั้งแรกถ้ามี Xcode Command Line
Tools อยู่ และ self-heal ในการเรียกครั้งถัดไปถ้า source เปลี่ยนไปตั้งแต่
compile ล่าสุด

ห้าม commit `.venv/`, `logs/`, `workspace/`, `bin/tunnel-client`, หรือไฟล์
`.env` ใดๆ — ทั้งหมดนี้ถูกกันไว้ใน `.gitignore` อยู่แล้ว

## สิ่งที่คาดหวังเมื่อแก้โค้ด

- ให้ MCP protocol traffic ใช้ stdout เท่านั้น ส่ง diagnostics ทั้งหมดไปที่
  stderr หรือ `logs/agent_activity.jsonl` ผ่าน wrapper `_logged` ที่มีอยู่
  แล้วใน `server.py`
- เอกสารของ tool ที่โมเดลเห็นอยู่ใน `@mcp.tool()`-decorated function ของ
  `server.py` เอง ไม่ใช่ docstring ของ `tools/*.py` — อ่าน comment ด้านบน
  ของ `server.py` ก่อนแก้สิ่งที่ tool บอก ChatGPT
- ปฏิบัติกับ path ไฟล์, credential, token, และเอกสารส่วนตัวเสมือนข้อมูล
  sensitive ห้าม hardcode Tunnel ID จริง, API key จริง, หรือ path
  home-directory ของ developer คนใดคนหนึ่ง — ใช้ตัวอย่างแบบ generic
- รักษาพฤติกรรมความปลอดภัยของ `computer` ไว้ (ต้องมีสิทธิ์ Accessibility,
  ปฏิเสธช่องรหัสผ่าน, destructive-action guard, fresh-observation check,
  และ post-action verification) รวมถึง permission gate ต่อโฟลเดอร์ของ
  `edit`/`write_file` (`tools/_edit_grants.py`) — นี่คือขอบเขตความปลอดภัย
  จริงของโปรเจกต์ ไม่ใช่พฤติกรรมข้างเคียง
- รันการตรวจสอบความเป็นส่วนตัว "Before sharing" ใน `README.md` ก่อนเปิด
  pull request ที่แตะอะไรก็ตามนอก `tools/`
