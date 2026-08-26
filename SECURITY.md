# Security policy

**English** | [ภาษาไทย](#ภาษาไทย)

ENDEAVOR_AGENT_CHATGPT gives an MCP client (ChatGPT web, via the OpenAI
Secure MCP Tunnel, or any other MCP client) real capability on this Mac:
shell commands, Python execution, file read/write/edit, and guarded
screen/mouse/keyboard control. Do not report suspected vulnerabilities in a
public issue.

Send a private report to **champoomwat@gmail.com** with:

- a clear description and affected version/commit;
- reproducible steps or a minimal proof of concept;
- the potential impact; and
- any suggested mitigation, if available.

Please do not include real credentials, personal files, or destructive
payloads in the report. We will acknowledge a valid report, investigate it
privately, and coordinate disclosure after a fix is available.

## Security boundaries

- **Tunnel is outbound-only.** The OpenAI Secure MCP Tunnel is an HTTPS
  connection this Mac makes out to OpenAI; nothing needs to be exposed on an
  inbound port. The MCP server itself talks stdio only, to the local
  `tunnel-client` process — it does not listen on any network port.
- **File scope defaults to `~/Desktop`** (`V2_WORKSPACE`). Files can be read,
  created, and edited there. A curated list of protected paths (SSH/AWS/GPG
  keys, Keychain, browser/app credential stores, this repo's own runtime
  token) is refused regardless of `V2_WORKSPACE`.
- **File deletion is disabled everywhere.** `bash`/`python_exec` refuse
  `rm`/`unlink`-class operations, and `computer` refuses UI actions that
  read as delete/remove. This is enforced in code, not left to the model's
  judgment.
- **Existing-file modification needs a session grant.** The first `edit`
  call, or `write_file` with `overwrite=true` on a file that already
  exists, targeting a given top-level workspace folder fails with
  `[permission_required]` and a one-time nonce until the user explicitly
  approves that folder in the same conversation
  (`tools/_edit_grants.py`). This is a workflow gate reinforced by the
  model's own instructions, not a cryptographic guarantee — treat it as
  friction against an accidental or careless edit, not as a hard boundary
  against a model that has been deliberately jailbroken.
- **`computer` requires macOS Accessibility permission** and refuses to
  interact with password/secure-text fields. It also refuses actions whose
  target text reads as delete/remove-related, independent of the
  file-deletion guard above.
- **Outside `~/Desktop`, existing files are never modified in place.**
  `edit`/`write_file` redirect to a sibling `name.edited.ext` working copy;
  the original is untouched.
- **`bash`/`bash_bg`/`python_exec` run inside a sandbox profile** scoped to
  the workspace directory, and are explicitly *not* covered by the
  `edit`/`write_file` permission gate above — a model could still write
  files via shell redirection without triggering that gate. This is a known,
  accepted trade-off (narrower friction on the two tools most likely to
  clobber a file the user already has, rather than blocking every mutating
  surface) — do not assume shell access is otherwise sandboxed against file
  writes.

None of the above is a guarantee that this server can safely execute
arbitrary instructions from an untrusted or compromised MCP client — these
are defense-in-depth controls for a locally-run, single-user assistant, not
a multi-tenant security boundary.

---

# ภาษาไทย

[English](#security-policy)

ENDEAVOR_AGENT_CHATGPT ให้ MCP client (ChatGPT web ผ่าน OpenAI Secure MCP
Tunnel หรือ MCP client อื่นใด) มีความสามารถจริงบนเครื่อง Mac นี้: รันคำสั่ง
shell, รัน Python, อ่าน/เขียน/แก้ไฟล์, และควบคุมหน้าจอ/เมาส์/คีย์บอร์ดแบบ
มีการ์ด อย่ารายงานช่องโหว่ที่สงสัยผ่าน public issue

ส่งรายงานแบบส่วนตัวไปที่ **champoomwat@gmail.com** พร้อม:

- คำอธิบายที่ชัดเจนและ version/commit ที่ได้รับผลกระทบ
- ขั้นตอนทำซ้ำได้ หรือ proof of concept แบบย่อ
- ผลกระทบที่อาจเกิดขึ้น
- ข้อเสนอแนะการแก้ไข (ถ้ามี)

โปรดอย่าใส่ credential จริง, ไฟล์ส่วนตัว, หรือ payload ที่ทำลายระบบใน
รายงาน เราจะตอบรับรายงานที่ถูกต้อง สืบสวนแบบส่วนตัว และประสานงานเปิดเผย
หลังมีการแก้ไขแล้ว

## ขอบเขตความปลอดภัย

- **Tunnel เป็นขาออกเท่านั้น** OpenAI Secure MCP Tunnel คือการเชื่อมต่อ
  HTTPS ที่ Mac เครื่องนี้เชื่อมออกไปหา OpenAI เอง ไม่ต้องเปิดอะไรให้
  อินเทอร์เน็ตเข้าถึงเลย ตัว MCP server เองคุยผ่าน stdio กับ
  `tunnel-client` ที่รันอยู่ในเครื่องเท่านั้น — ไม่ listen พอร์ตเครือข่าย
  ใดๆ
- **ขอบเขตไฟล์ default อยู่ที่ `~/Desktop`** (`V2_WORKSPACE`) อ่าน, สร้าง,
  และแก้ไฟล์ได้ในนั้น มีรายการ path คุ้มครองที่กำหนดไว้ (SSH/AWS/GPG key,
  Keychain, ที่เก็บ credential ของ browser/แอป, runtime token ของ repo
  นี้เอง) ที่ถูกปฏิเสธเสมอไม่ว่า `V2_WORKSPACE` จะตั้งเป็นอะไร
- **การลบไฟล์ถูกปิดไว้ทุกที่** `bash`/`python_exec` ปฏิเสธคำสั่งประเภท
  `rm`/`unlink` และ `computer` ปฏิเสธ UI action ที่อ่านได้ว่าเป็นการ
  ลบ/ทำลาย บังคับในโค้ด ไม่ปล่อยให้โมเดลตัดสินใจเอง
- **การแก้ไฟล์เดิมต้องได้รับอนุญาตในระดับ session** การเรียก `edit` หรือ
  `write_file` แบบ `overwrite=true` บนไฟล์ที่มีอยู่แล้ว ครั้งแรกที่แตะ
  โฟลเดอร์ระดับบนสุดในแต่ละ session จะ fail ด้วย `[permission_required]`
  พร้อมรหัสครั้งเดียว จนกว่าผู้ใช้จะอนุญาตโฟลเดอร์นั้นในบทสนทนาเดียวกัน
  (`tools/_edit_grants.py`) นี่คือ workflow gate ที่เสริมด้วยคำสั่งของ
  โมเดลเอง ไม่ใช่การรับประกันทางการเข้ารหัส — ให้มองว่าเป็น friction
  ป้องกันการแก้ไขโดยไม่ได้ตั้งใจหรือประมาท ไม่ใช่ขอบเขตที่แข็งแกร่งต่อ
  โมเดลที่ถูก jailbreak โดยเจตนา
- **`computer` ต้องมีสิทธิ์ macOS Accessibility** และปฏิเสธการโต้ตอบกับ
  ช่องรหัสผ่าน/secure-text นอกจากนี้ยังปฏิเสธ action ที่ target text อ่าน
  ได้ว่าเกี่ยวกับการลบ/ทำลาย แยกต่างหากจาก guard การลบไฟล์ด้านบน
- **นอก `~/Desktop` ไฟล์เดิมจะไม่ถูกแก้ในที่เดิมเลย** `edit`/`write_file`
  จะ redirect ไปแก้ที่สำเนา `name.edited.ext` ข้างๆ — ต้นฉบับไม่ถูกแตะ
- **`bash`/`bash_bg`/`python_exec` รันใน sandbox profile** ที่จำกัดใน
  workspace และ**ไม่ได้อยู่ใน**ขอบเขตของ permission gate ของ
  `edit`/`write_file` ด้านบนอย่างชัดเจน — โมเดลยังเขียนไฟล์ผ่าน shell
  redirection ได้โดยไม่ต้องผ่าน gate นั้น นี่คือ trade-off ที่รู้และ
  ยอมรับไว้แล้ว (เลือก friction แคบเฉพาะ 2 tool ที่เสี่ยงทำลายไฟล์เดิม
  มากที่สุด แทนที่จะบล็อกทุกช่องทางที่แก้ไขได้) — อย่าสมมติว่า shell
  access ถูก sandbox กันการเขียนไฟล์ไว้ด้วยเช่นกัน

ไม่มีข้อใดข้างต้นที่รับประกันว่า server นี้จะรันคำสั่งใดๆ จาก MCP client
ที่ไม่น่าเชื่อถือหรือถูกโจมตีแล้วได้อย่างปลอดภัย — ทั้งหมดนี้คือ
defense-in-depth control สำหรับผู้ช่วยที่รันในเครื่องเดียว ผู้ใช้คนเดียว
ไม่ใช่ขอบเขตความปลอดภัยแบบ multi-tenant
