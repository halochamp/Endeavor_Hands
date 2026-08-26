# คู่มือตั้งค่า ChatGPT ให้ใช้งาน Endeavor บน Mac

เอกสารนี้อธิบายการเชื่อม ChatGPT Web เข้ากับ `ENDEAVOR_AGENT_CHATGPT` ที่รันอยู่บน Mac เครื่องนี้ เพื่อให้ ChatGPT อ่านไฟล์, สร้าง/แก้ไฟล์, รันคำสั่ง, ตรวจ test/Git, อ่านภาพ และควบคุมแอปบน Mac ได้ผ่าน OpenAI Secure MCP Tunnel

> ขอบเขตปัจจุบัน: ChatGPT ทำงานกับไฟล์บน `~/Desktop` ได้ แต่ระบบไม่อนุญาตให้ลบไฟล์

## ภาพรวม

```text
ChatGPT Web
  → Developer-mode app
  → OpenAI-hosted Secure MCP Tunnel
  → tunnel-client บน Mac
  → ENDEAVOR_AGENT_CHATGPT/server.py
  → ไฟล์ / คำสั่ง / แอปบน Mac
```

Tunnel เป็นการเชื่อมต่อ HTTPS ขาออกจาก Mac ไปยัง OpenAI เท่านั้น จึงไม่ต้องเปิดพอร์ตจากอินเทอร์เน็ตเข้ามาใน Mac โดยตรง ดูรายละเอียดได้จาก [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## สิ่งที่ต้องมี

1. Clone repo นี้ไว้ที่ไหนก็ได้บนเครื่อง แล้วติดตั้งตาม [README.md](../README.md) ให้เรียบร้อยก่อน

2. มี tunnel ใน OpenAI Platform แล้ว — หลังสร้าง คุณจะได้ Tunnel ID รูปแบบ:

   ```text
   tunnel_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

   ตั้งชื่อ tunnel ที่จำง่าย เช่น `my-endeavor-mac`

3. Platform organization ที่เป็นเจ้าของ tunnel และ ChatGPT workspace ที่จะใช้ ต้องถูก associate เข้าด้วยกัน และผู้สร้าง app ต้องมีสิทธิ์ **Tunnels: Read + Use**

4. บัญชี/Workspace ChatGPT ต้องเปิดใช้ Developer mode สำหรับการสร้าง custom MCP app ได้

5. ใช้ ChatGPT **บนเว็บ** สำหรับการสร้างและใช้งาน Developer-mode app

## เปิด Endeavor Tunnel

### ครั้งแรก

1. ดับเบิลคลิก `scripts/start_tunnel.command` ในโฟลเดอร์โปรเจกต์ (หรือคัดลอกไปวางบน Desktop ก็ได้)
2. Terminal จะถาม runtime API key โดยไม่แสดงตัวอักษร
3. วาง key แล้วกด Enter
4. launcher เก็บ key ใน macOS Login Keychain ภายใต้ชื่อ:

   ```text
   endeavor-chatgpt-tunnel-runtime
   ```

5. รอจน Terminal แสดงว่า tunnel-client เริ่มทำงานแล้ว

### ครั้งถัดไป

ดับเบิลคลิกไฟล์เดิมได้เลย launcher จะอ่าน key จาก Keychain และเริ่ม tunnel ให้โดยไม่ถาม key อีก

**ต้องปล่อยหน้าต่าง Terminal นี้เปิดค้างไว้** ระหว่างที่ต้องการให้ ChatGPT เรียก Endeavor ได้

### ตรวจสถานะ

เปิด local status UI:

```text
http://127.0.0.1:8765/ui
```

หากขึ้น `ready` ที่ `http://127.0.0.1:8765/readyz` แปลว่า client พร้อมรับงาน

> พอร์ต 8765 เป็นค่า default ของ tunnel-client เอง — ถ้าเครื่องคุณมีโปรแกรมอื่นใช้พอร์ตนี้อยู่แล้ว ให้เปลี่ยน `listen_addr` ในไฟล์ profile ของ tunnel-client เป็นพอร์ตว่าง แล้วแก้ `STATUS_URL` ใน `scripts/start_tunnel.command` ให้ตรงกัน

## เพิ่ม Endeavor เข้า ChatGPT

1. เปิด ChatGPT Web ใน workspace เดียวกับที่ associate tunnel ไว้
2. เข้า **Plugins** หรือ **Apps** (ชื่อหน้าจออาจต่างกันตามการ rollout)
3. กดปุ่ม **+** เพื่อสร้าง Developer-mode app
4. ในหัวข้อ **Connection** เลือก **Tunnel**
5. เลือก tunnel ที่คุณสร้างไว้จากรายการ (ตามชื่อที่ตั้งไว้ในขั้นตอนก่อนหน้า)

   หากไม่พบในรายการ ให้ใส่ Tunnel ID ของคุณเองแทน

6. กด **Scan Tools** แล้วสร้าง app
7. เปิดแชตใหม่ เลือก Endeavor จากเมนู Tools หรือเรียกด้วย `@ชื่อแอป`

ไม่ต้องใส่ URL local `127.0.0.1:8765` และไม่ต้องใส่ URL `https://api.openai.com/v1/tunnel/...` ในช่อง endpoint ของ ChatGPT — ให้เลือก **Tunnel** และเลือก Tunnel ID เท่านั้น

## Tools ที่ ChatGPT เห็นในปัจจุบัน

ปัจจุบัน schema มี 11 tools:

| งาน | Tool |
|---|---|
| รันคำสั่งสั้น, ค้นหา, Git, test, build | `bash` |
| รันงาน shell แบบ background | `bash_bg` |
| รัน Python สำหรับวิเคราะห์หรือ test | `python_exec` |
| อ่านโค้ด, เอกสาร, เสียง/วิดีโอ และภาพ | `read_file` |
| สร้างไฟล์ใหม่หรือเขียนทั้งไฟล์ | `write_file` |
| แก้ไฟล์เดิมเฉพาะจุด | `edit` |
| ดู/ควบคุมแอปบน Mac | `computer` |
| เชื่อมต่อ MCP server อื่น | `mcp_list_tools`, `mcp_call_tool`, `mcp_add_server`, `mcp_remove_server` |

### การอ่านภาพ

ใช้ `read_file` โดยตรงสำหรับ PNG, JPG/JPEG, GIF, BMP, WebP, HEIC/HEIF และ TIFF เช่น:

```text
ใช้ Endeavor อ่าน ~/Desktop/screenshot.png แล้วอธิบายภาพ
```

รูปจะถูกแปลงเป็น PNG และส่งเข้า ChatGPT เพื่อวิเคราะห์โดยตรง

ข้อจำกัดภาพ:

- ขนาดไฟล์ไม่เกิน 50 MB
- ความละเอียดไม่เกิน 40,000,000 pixels
- ด้านยาวถูกย่อให้ไม่เกิน 2048 pixels ก่อนส่ง
- `line_start`, `line_end`, page range, search filter และ `doc_mode` ใช้กับรูปไม่ได้

## `edit`/`write_file` permission gate

ครั้งแรกที่ ChatGPT พยายามแก้ไฟล์ที่มีอยู่แล้ว (`edit`, หรือ `write_file` แบบ `overwrite=true`) ในโฟลเดอร์ระดับบนสุดใต้ `~/Desktop` โฟลเดอร์ไหนก็ตามที่ยังไม่เคยอนุญาต ระบบจะตอบกลับเป็น `[permission_required]` พร้อมรหัสยืนยันครั้งเดียว (nonce) — ChatGPT ควรถามคุณตรงๆ ในแชทว่าจะอนุญาตให้แก้ไฟล์ในโฟลเดอร์นั้นหรือไม่ ถ้าคุณตอบตกลง ChatGPT จะเรียกเครื่องมือซ้ำพร้อมรหัสนั้นเพื่อปลดล็อก โฟลเดอร์ที่อนุญาตแล้วจะแก้ไฟล์ได้ตลอด session นี้ (จนกว่าจะปิด tunnel/server) — โฟลเดอร์อื่นต้องขอแยกกัน

## วิธีสั่งงานที่แนะนำ

### อ่านอย่างเดียว

```text
ใช้ Endeavor ตรวจโครงสร้างโปรเจกต์ ~/Desktop/my-project
ห้ามแก้ไขไฟล์ และสรุปไฟล์สำคัญพร้อมจุดเริ่มต้นของโปรเจกต์
```

### แก้โค้ดอย่างปลอดภัย

```text
ใช้ Endeavor อ่านไฟล์ ~/Desktop/my-project/src/app.py
แก้เฉพาะส่วนที่ทำให้ฟังก์ชัน login validation ผิดพลาด
จากนั้นรัน test ที่เกี่ยวข้องและแสดง git diff สรุปก่อนจบ
ห้ามลบไฟล์ใด ๆ
```

### สร้างไฟล์ใหม่

```text
ใช้ Endeavor สร้าง ~/Desktop/my-project/docs/setup.md
อธิบายวิธีติดตั้งและรันโปรเจกต์จาก README เดิม
```

### ตรวจภาพหรือ UI mockup

```text
ใช้ Endeavor อ่าน ~/Desktop/mockup.png
วิเคราะห์ลำดับชั้นของหน้าจอและเสนอจุดที่ควรปรับ
```

## ขอบเขตและความปลอดภัย

- `V2_WORKSPACE` ถูกตั้งเป็น `~/Desktop`: อ่าน, สร้าง และแก้ไขไฟล์ภายใต้ Desktop ได้
- การลบไฟล์ถูกปิดไว้: shell/Python sandbox ปฏิเสธการ remove/unlink และ computer tool ปฏิเสธ UI action ที่สื่อถึงการลบ
- การลบบรรทัดหรือแทนที่เนื้อหาในไฟล์ผ่าน `edit` ยังทำได้ เพราะเป็นการแก้เนื้อหา ไม่ใช่ลบไฟล์ — แต่ต้องผ่าน permission gate ด้านบนก่อน
- runtime API key อยู่ใน Keychain เท่านั้น; ไม่อยู่ใน launcher, profile หรือ source code
- อย่าส่ง runtime API key, รหัสผ่าน, OTP หรือข้อมูลบัตรเข้า ChatGPT
- หากใช้ `computer` ครั้งแรก macOS อาจขอ Accessibility และ Screen Recording permission ให้ Terminal/tunnel-client/Python ตามที่ macOS แสดง

## เมื่อแก้โค้ดของ MCP Server เอง

หลังแก้ไฟล์ใต้ `ENDEAVOR_AGENT_CHATGPT` เช่น `server.py` หรือไฟล์ใน `tools/` ให้:

1. ปิด Terminal ที่รัน tunnel เดิม
2. เปิด `scripts/start_tunnel.command` ใหม่
3. เปิดแชตใหม่ใน ChatGPT

หากมีการเพิ่ม/ลบ/เปลี่ยน schema ของ tool ให้กลับไปที่หน้าจอ app ใน ChatGPT แล้วกด **Refresh / Scan Tools**; หาก workspace UI ไม่มีปุ่มนี้ ให้ reconnect หรือสร้าง Developer-mode app ใหม่

## แก้ปัญหาเบื้องต้น

| อาการ | วิธีตรวจ/แก้ |
|---|---|
| `address already in use` ที่พอร์ต 8765 | มีโปรแกรมอื่นใช้พอร์ตนี้อยู่ — เปลี่ยนพอร์ตตามหัวข้อ "ตรวจสถานะ" ด้านบน |
| ChatGPT ไม่เห็น tunnel | ตรวจ association ของ Platform organization กับ ChatGPT workspace และสิทธิ์ Tunnels: Read + Use |
| ChatGPT ไม่เห็น tool ที่เพิ่งเพิ่ม | restart tunnel แล้ว Refresh/Scan Tools หรือ reconnect app |
| `read_file` อ่านรูปไม่สำเร็จ | ตรวจว่ารูปอยู่ใต้ 50 MB, ไม่เกิน 40 ล้าน pixels และ restart tunnel หลังแก้ server ล่าสุด |
| `computer` ใช้งานไม่ได้ | อนุญาต Accessibility/Screen Recording ใน macOS แล้วลองใหม่ |
| tunnel หลุด | ดู `http://127.0.0.1:8765/ui`, ตรวจว่า Terminal launcher ยังเปิดอยู่ และเริ่ม launcher ใหม่หากจำเป็น |

## Checklist ก่อนใช้งาน

- [ ] Terminal launcher แสดงว่า tunnel-client เริ่มทำงานแล้ว
- [ ] `http://127.0.0.1:8765/readyz` ตอบ `ready`
- [ ] Endeavor app ถูกเลือกใน ChatGPT Web
- [ ] เริ่มแชตใหม่ก่อนงานที่ต้องเรียก tool
- [ ] ระบุ path และขอบเขตการแก้ไขให้ชัด
- [ ] สั่งให้รัน test และสรุป `git diff` หลังแก้โค้ดสำคัญ
