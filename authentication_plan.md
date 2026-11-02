# Kế hoạch phân quyền: role nào được vào route nào

> Trạng thái: **ĐÃ CHỐT** (2026-10-31, sau 3 vòng review). Tài liệu mô tả chính sách phân quyền cuối cùng, đã đối chiếu với code (`app/auth/`, `app/api/`, `app/tools/`, `app/workflows/`, `app/forms/engine.py`, `app/exports/`).
>
> Ký hiệu **†** = đã chốt nhưng **code chưa làm**; mã `C1`…`C9` trỏ tới việc cần làm ở mục 9.2, `G1`…`G8` là lỗ hổng ở mục 9.1. Thứ tự triển khai ở mục 9.3.

---

## 0. Nguyên tắc

1. **Role luôn do backend gắn.** Lời tự xưng trong chat ("tôi là leader") không có tác dụng.
2. **Tài khoản chỉ chứng minh danh tính (email).** Role lấy từ cột Role trong sheet `Members` mỗi request (cache ngắn), riêng admin lấy từ `config.auth.admins`. "Gán role" cho ai đó nghĩa là sửa cột Role trong `Members` (chỉ admin).
3. **Kiểm tra ở 3 lớp:** route (HTTP) → tool (LLM chỉ thấy tool của role mình, khi chạy kiểm tra lại) → nghiệp vụ (form, action, dữ liệu trả về).
4. **Thao tác ghi dữ liệu thật phải qua nút Xác nhận** (endpoint backend, không phải tool LLM). Lúc bấm Xác nhận phải kiểm tra lại quyền theo role *hiện tại*.
5. **Không lộ sự tồn tại.** Tài nguyên không thuộc mình hoặc đã hết hạn đều trả 404/410 giống nhau.
6. **Lỗi thì hạ quyền.** Không đọc được `Members` → coi là guest.
7. **Guest không cần tài khoản.** Tài khoản chỉ cấp cho người có trong `Members` hoặc `auth.admins`.

---

## 1. Các role và cách xác định

| Role | Mã | Ai | Nguồn |
|---|---|---|---|
| Khách | `guest` | Người chưa đăng nhập (không cần tài khoản) | Mặc định |
| Thành viên | `member` | Email có trong `Members`, cột Role = `Member` (hoặc giá trị lạ) | Sheet `Members` |
| Thư ký | `thu_ki` | Cột Role = `Secretary` | Sheet `Members` |
| Trưởng nhóm | `leader` | Cột Role = `Leader` | Sheet `Members` |
| Admin | `admin` | 2 email trong `config.auth.admins`: thầy hướng dẫn và dev, **mỗi người một tài khoản**, không cần có trong `Members`. Mỗi email gắn với một họ tên trong config †C1 | `config.yaml` |

Nhóm quyền (các nhóm cộng dồn: leader có mọi quyền của `MEMBER_PLUS`, `STAFF`, `LEADER_ADMIN`; admin có tất cả):

| Nhóm | Gồm | Dùng cho |
|---|---|---|
| `Guest` | guest | Tra KB external (nội quy lab, web data crawl), chỉ được điền đơn xin gia nhập |
| `MEMBER_PLUS` | member, thu_ki, leader, admin | Tra cứu toàn bộ kb gồm cả nội bộ ( nội dung cuộc họp,...), xuất dữ liệu, điền đơn trong templates (trừ đơn xin gia nhập), tra cứu đơn của bản thân đã tạo, nhờ chatbot điền lịch trực của mình và xuất lịch trực †C9 |
| `STAFF` | thu_ki, leader, admin | Thấy SĐT, MSSV, cựu thành viên, lịch sử đã trả, đẩy nội dung ( cuộc họp, nội quy mới) vào kb internal/public mà không cần duyệt thêm †C4, view/export toàn bộ file trong submissions †C3 |
| `BORROWERS` | member, thu_ki, leader, admin †C1 | Mượn / trả / gia hạn thiết bị |
| `LEADER_ADMIN` | leader, admin | Xem danh sách đơn gia nhập chờ duyệt, edit thông tin trong data folder (trừ sheet `Members`) †C6, thêm templates †C5 |
| `ADMIN` | admin | Duyệt / từ chối đơn gia nhập, cho rời lab †C8, sửa sheet `Members`, đổi role, tạo / xoá / sửa tài khoản †C2, xem `AuditLog`, chi phí, KB web (crawl ngay, áp dụng lần crawl bị chặn) |

> Thư ký **không** duyệt / từ chối đơn gia nhập. Leader cũng **không** (chỉ xem). Mọi thay đổi nhân sự (thêm người, cho rời lab, đổi role) và tài khoản đều do admin làm.

### 1.1 Luồng xác định role mỗi request (`app/auth/deps.py: current_user`)

```
cookie session ──► middleware nạp/tạo session (guest nếu chưa có)
                     │
                     ├─ auth_bypass bật (chỉ dev)  → role lấy từ cookie la_dev_role
                     ├─ dev_login_name (chỉ test)  → tìm theo họ tên trong Members
                     ├─ account_id (đã đăng nhập)
                     │     ├─ tài khoản bị khoá/xoá      → guest
                     │     ├─ email ∈ auth.admins       → admin (họ tên lấy từ config)
                     │     ├─ email ∈ Members           → leader | thu_ki | member
                     │     └─ email ∉ Members           → huỷ session, báo "Tài khoản không còn hiệu lực" †C2
                     │                                    (code hiện tại: hạ về guest)
                     └─ không có gì                     → guest
   lỗi bất kỳ khi đọc Members → guest tạm thời, KHÔNG huỷ session (ghi log role_resolve_failed)
```

- `Members` được cache `auth.role_cache_seconds` = 60 giây ⇒ đổi role / xoá khỏi `Members` có hiệu lực **chậm tối đa 60 giây**.
- Đăng nhập, đăng xuất đều **tạo session mới** (chống session fixation) ⇒ mọi bản nháp, file xuất, hội thoại của session cũ không còn dùng được.

### 1.2 Vòng đời tài khoản

| Sự kiện | Việc hệ thống làm |
|---|---|
| Admin duyệt đơn gia nhập (bấm Xác nhận) | Thêm vào `Members` **và tự tạo tài khoản**, gửi mật khẩu tạm qua mail †C2 |
| Lần đăng nhập đầu | Bắt buộc đổi mật khẩu trước khi dùng bất kỳ chức năng nào (G2) |
| Admin đổi role | Sửa cột Role trong `Members`; có hiệu lực trong 60 giây |
| Admin cho rời lab | Chuyển sang tab "Cựu thành viên"; tài khoản hết hiệu lực ngay ở request kế tiếp †C2 |
| Admin khoá / mở khoá / reset mật khẩu | Có sẵn |
| Admin xoá tài khoản | Xoá tài khoản và mọi session; `AuditLog` vẫn giữ email †C2 |
| Admin tạo tài khoản tay | Chỉ cho email có trong `Members` hoặc `auth.admins` †C2 |

---

## 2. Ma trận route HTTP

Ký hiệu: ✅ được · ❌ bị chặn · **chủ** = chỉ session đã tạo ra tài nguyên đó · **CSRF** = mọi POST đều cần token đôi (cookie + header).

### 2.1 Trang và tài nguyên tĩnh

| Route | guest | member | thu_ki | leader | admin | Ghi chú |
|---|:-:|:-:|:-:|:-:|:-:|---|
| `GET /` (chat) | ✅ | ✅ | ✅ | ✅ | ✅ | |
| `GET /login` | ✅ | ✅ | ✅ | ✅ | ✅ | |
| `GET /static/*` | ✅ | ✅ | ✅ | ✅ | ✅ | Không tạo session |
| `GET /admin/kb/web` (HTML) | ❌ | ❌ | ❌ | ✅ | ✅ | Leader chỉ thấy mục "Mẫu đơn" †C5. **Hiện ai cũng mở được trang** (G4); dữ liệu vẫn được bảo vệ ở `/admin/api` |

### 2.2 Xác thực `/auth/*`

| Route | guest | member | thu_ki | leader | admin | Kiểm tra thêm |
|---|:-:|:-:|:-:|:-:|:-:|---|
| `GET /auth/me` | ✅ | ✅ | ✅ | ✅ | ✅ | Trả role, họ tên, CSRF token |
| `POST /auth/login` | ✅ | ✅ | ✅ | ✅ | ✅ | CSRF; giới hạn theo IP + khoá tài khoản 5 lần sai / 15 phút; lỗi chung chung. Đúng mật khẩu nhưng email không còn trong `Members`/`admins` → 403 "Tài khoản không còn hiệu lực" †C2 |
| `POST /auth/logout` | ✅ | ✅ | ✅ | ✅ | ✅ | CSRF; đổi session |
| `POST /auth/change-password` | ❌ | ✅ | ✅ | ✅ | ✅ | Phải có tài khoản; huỷ mọi session khác |
| `GET/POST /auth/dev-login` | — | — | — | — | — | Chỉ khi `APP_ENV≠production` **và** `test_mode` **và** `test.dev_login`; ngược lại 404 |
| `POST /auth/dev-role` | — | — | — | — | — | Chỉ khi `test.auth_bypass`; ngược lại 404 |

### 2.3 Chat, upload, đơn, thao tác chờ xác nhận, file xuất

| Route | guest | member | thu_ki | leader | admin | Kiểm tra thêm |
|---|:-:|:-:|:-:|:-:|:-:|---|
| `POST /chat` (SSE) | ✅ | ✅ | ✅ | ✅ | ✅ | CSRF; chặn nếu `must_change_password`; rate limit: guest 20/giờ/session + 60/giờ/IP, member+ 120/giờ; budget token |
| `GET /chat/{id}/history` | chủ | chủ | chủ | chủ | chủ | Hội thoại phải thuộc session |
| `POST /uploads` | ✅ | ✅ | ✅ | ✅ | ✅ | CSRF; pdf/docx ≤ 10 MB, chặn macro/nhúng. Guest cần để nộp đơn gia nhập. **Chưa có rate limit** (G5) |
| `GET /drafts/{id}/preview`, `/status` | chủ | chủ | chủ | chủ | chủ | Bản nháp thuộc session, còn hạn |
| `POST /drafts/{id}/confirm` | chủ | chủ | chủ | chủ | chủ | CSRF; khớp `shown_hash`. **Chưa kiểm tra lại role của mẫu đơn** (G3) |
| `POST /drafts/{id}/cancel` | chủ | chủ | chủ | chủ | chủ | CSRF |
| `GET /forms/{form_id}/blank?exp&sig` | link | link | link | link | link | Ai có link ký còn hạn đều tải được mẫu trắng (không gắn session) |
| `POST /actions/{id}/confirm` | ❌ | chủ + role | chủ + role | chủ + role | chủ + role | CSRF; kiểm tra lại role theo **loại** action (mục 4) |
| `POST /actions/{id}/cancel` | chủ | chủ | chủ | chủ | chủ | CSRF; chỉ cần là chủ |
| `GET /exports/{id}` | chủ | chủ | chủ | chủ | chủ | Thuộc session, còn hạn. Thực tế chỉ member+ tạo được file xuất |

> **Khác `code_plan.md` §7.13:** bảng đó ghi `/actions/*` là "Leader/admin". Thực tế quyền theo loại action. Cần sửa bảng (G6).

### 2.4 Admin `/admin/api/*`

Mọi POST cần CSRF. Hiện cả router dùng `Depends(require_admin)`; sau C5 tách phần mẫu đơn ra dùng `require_role(LEADER, ADMIN)`.

| Route | leader | admin | Chức năng |
|---|:-:|:-:|---|
| `GET /admin/api/kb/web` | ❌ | ✅ | Danh sách trang web đã crawl, lần chạy gần nhất |
| `POST /admin/api/kb/web/crawl` | ❌ | ✅ | Crawl ngay |
| `POST /admin/api/kb/web/runs/{id}/apply` | ❌ | ✅ | Áp dụng lần crawl bị chặn (>30% thay đổi) |
| `GET /admin/api/ops` | ❌ | ✅ | Op outbox `FAILED` |
| `GET /admin/api/llm/costs` | ❌ | ✅ | Chi phí LLM |
| `GET /admin/api/audit` (**mới**) | ❌ | ✅ †C6 | Xem `AuditLog` |
| `GET /admin/api/accounts` | ❌ | ✅ | Danh sách tài khoản |
| `POST /admin/api/accounts` | ❌ | ✅ | Tạo tài khoản, gửi mật khẩu tạm. Chỉ cho email có trong `Members`/`admins` †C2 |
| `POST /admin/api/accounts/action` | ❌ | ✅ | `reset` / `disable` / `enable` / `unlock` / `delete` †C2 |
| `GET /admin/api/templates` | ✅ †C5 | ✅ | Danh sách mẫu đơn |
| `GET /admin/api/templates/{id}/preview`, `/blank` | ✅ †C5 | ✅ | Xem bản điền thử / bản trắng |
| `POST /admin/api/templates/{id}/confirm`, `/cancel` | ✅ †C5 | ✅ | Kích hoạt / huỷ mẫu đơn mới |
| `POST /admin/api/templates/forms/{form_id}/deactivate` | ✅ †C5 | ✅ | Ngừng dùng mẫu đơn |

guest, member, thu_ki: ❌ toàn bộ.

### 2.5 Vận hành

| Route | Ai truy cập | Ghi chú |
|---|---|---|
| `GET /healthz` | Công khai | Chỉ trả "sống" |
| `GET /readyz` | **Nội bộ** (IP thuộc `metrics_allow_cidrs`) †C7 | Hiện đang công khai, trả tên lỗi nội bộ |
| `GET /metrics` | IP thuộc `security.metrics_allow_cidrs` | Kiểm tra theo **IP kết nối trực tiếp**, không theo `X-Forwarded-For`. Rủi ro khi chạy sau reverse proxy (G1). `/readyz` dùng chung cách kiểm tra này |

---

## 3. Ma trận tool của LLM

LLM chỉ được gửi danh sách tool của role hiện tại; registry kiểm tra role lần nữa khi chạy. `ctx` (role, email, session) do backend tiêm, không có trong schema.

| Tool | guest | member | thu_ki | leader | admin | Giới hạn bên trong |
|---|:-:|:-:|:-:|:-:|:-:|---|
| `search_kb` | ✅ | ✅ | ✅ | ✅ | ✅ | Guest chỉ thấy chunk `public`, không thấy biên bản họp; member+ thấy `public` + `member`. Bộ lọc dựng trong retriever, LLM không truyền được |
| `list_forms` | ✅ | ✅ | ✅ | ✅ | ✅ | Chỉ liệt kê mẫu đơn role đó được nộp |
| `get_application_status` | ✅ | ❌ | ❌ | ❌ | ❌ | Câu trả lời cố định, không xác nhận ai đã nộp |
| `query_equipment` | ❌ | ✅ | ✅ | ✅ | ✅ | |
| `my_borrows` | ❌ | ✅ | ✅ | ✅ | ✅ †C1 | Thiết bị mình đang mượn |
| `query_duty` | ❌ | ✅ | ✅ | ✅ | ✅ | |
| `register_duty` (**mới**) | ❌ | ✅ †C9 | ✅ †C9 | ✅ †C9 | ✅ †C9 | Điền / bỏ đăng ký buổi trực **của mình**, xem trước rồi Xác nhận |
| `query_members` | ❌ | ✅ | ✅ | ✅ | ✅ | SĐT chỉ trả cho `STAFF`; `Address` không bao giờ trả |
| `list_pending_applications` | ❌ | ❌ | ❌ | ✅ | ✅ | Leader chỉ xem |
| `get_form_template`, `start_form`, `update_form_fields`, `attach_file`, `render_preview`, `submit_filled_form` | ✅ | ✅ | ✅ | ✅ | ✅ | Tool mở cho mọi role, nhưng **từng mẫu đơn** giới hạn role (mục 5) |
| `export_data` | ❌ | ✅ | ✅ | ✅ | ✅ | Member không nhận cột MSSV/SĐT, không xuất cựu thành viên; lịch sử đã trả chỉ của mình. Thêm dữ liệu lịch trực †C9 |
| `export_form_files` | ❌ | ✅ | ✅ | ✅ | ✅ | Member chỉ xuất đơn của mình (biên bản họp: xuất được tất cả). Thư ký xuất được mọi đơn, kể cả đơn gia nhập †C3 |
| `return_equipment`, `extend_borrow` | ❌ | ✅ | ✅ | ✅ | ✅ †C1 | Tạo yêu cầu chờ xác nhận |
| `request_member_change` | ❌ | ❌ | ❌ | ❌ †C8 | ✅ | Duyệt / từ chối đơn, cho rời lab, đổi role. Hiện leader vẫn dùng được (trừ đổi role) |
| `propose_template`, `update_template_draft`, `cancel_template_draft` | ❌ | ❌ | ❌ | ✅ †C5 | ✅ | Không có tool kích hoạt; kích hoạt bằng nút trên UI |
| `publish_kb_document` (**mới**) | ❌ | ❌ | ✅ †C4 | ✅ †C4 | ✅ †C4 | Đẩy file vào `kb/public` hoặc `kb/internal`, xem trước rồi Xác nhận, không cần duyệt thêm |
| `request_data_edit` (**mới**) | ❌ | ❌ | ❌ | ✅ †C6 | ✅ †C6 | Sửa dữ liệu trong `data/`; sheet `Members` chỉ admin; `AuditLog` không ai sửa |

---

## 4. Thao tác chờ xác nhận (`pending_actions`)

Tool chỉ tạo yêu cầu; ghi dữ liệu thật khi người dùng bấm Xác nhận (`POST /actions/{id}/confirm`). Lúc tạo và lúc xác nhận đều kiểm tra `ACTION_ROLES`; yêu cầu phải thuộc đúng session.

| Loại action | Được tạo + xác nhận |
|---|---|
| `equipment_return` (trả thiết bị) | member, thu_ki, leader, admin †C1 |
| `equipment_extend` (gia hạn) | member, thu_ki, leader, admin †C1 |
| `member_approve` (duyệt đơn gia nhập, kèm tạo tài khoản †C2) | admin †C8 (hiện: leader, admin) |
| `member_reject` (từ chối đơn) | admin †C8 (hiện: leader, admin) |
| `member_remove` (cho rời lab) | admin †C8 (hiện: leader, admin) |
| `member_set_role` (đổi role) | admin |
| `duty_register` (**mới**, điền lịch trực của mình) | member, thu_ki, leader, admin †C9 |
| `kb_publish` (**mới**, đẩy tài liệu vào KB) | thu_ki, leader, admin †C4 |
| `data_edit` (**mới**, sửa dữ liệu trong `data/` trừ `Members`) | leader, admin †C6 |
| `member_edit` (**mới**, sửa thông tin trong `Members`) | admin †C6 |

---

## 5. Mẫu đơn theo role

Role được khai báo trong từng schema (`roles:`). Form engine chặn khi bắt đầu điền (`FormEngine.form`) và sẽ chặn cả lúc Xác nhận (G3).

| Mẫu đơn | Role được dùng |
|---|---|
| `don_xin_gia_nhap` (đơn gia nhập) | guest |
| `phieu_muon_thiet_bi` (phiếu mượn) | member, thu_ki, leader, admin †C1 |
| `bien_ban_hop` (biên bản họp) | thu_ki, leader, admin †C4 |
| Mẫu leader/admin thêm qua chat | Người thêm chọn `roles` khi tạo (`propose_template`); mặc định `forms.default_roles` = member, thu_ki, leader |

---

## 6. Dữ liệu được thấy theo role

### 6.1 Theo trường dữ liệu

| Dữ liệu | guest | member | thu_ki / leader / admin |
|---|:-:|:-:|:-:|
| KB `public` (web lab, nội quy, tài liệu công khai) | ✅ | ✅ | ✅ |
| KB `member` (tài liệu nội bộ, biên bản họp) | ❌ | ✅ | ✅ |
| Danh sách thành viên: họ tên, email, lớp, trường, role | ❌ | ✅ | ✅ |
| SĐT, MSSV | ❌ | ❌ | ✅ |
| Địa chỉ (`Address`), cột `No` | ❌ | ❌ | ❌ (`never_expose`) |
| Cựu thành viên | ❌ | ❌ | ✅ |
| Thiết bị, lịch trực | ❌ | ✅ | ✅ |
| Thiết bị đang được ai mượn | ❌ | ✅ | ✅ |
| Lịch sử mượn đã trả | ❌ | Của mình | ✅ |
| Đơn đã nộp | ❌ | Của mình | ✅ (thư ký †C3) |
| `AuditLog` | ❌ | ❌ | Chỉ admin †C6 |

### 6.2 Ma trận quyền trên Google Drive (khi dùng qua chatbot)

**Phạm vi.** Bảng này chỉ nói về thao tác **qua chatbot**. Chatbot dùng **một tài khoản Google của lab** (OAuth) cho mọi người dùng, nên Google không phân biệt ai đang hỏi: **mọi kiểm tra quyền nằm ở backend**. Việc chia sẻ thư mục Drive trực tiếp cho từng người là chuyện riêng (xem lưu ý cuối mục).

**Ký hiệu:**

| Quyền | Nghĩa |
|---|---|
| **Xem** | Chatbot đọc và trả lời / trích nội dung trong khung chat |
| **Xuất** | Nhận file tải về (file gốc trên Drive, hoặc bảng `.xlsx`/`.csv` tổng hợp) qua link ngắn hạn gắn với session |
| **Tạo** | Thêm mới (nộp đơn, thêm dòng, thêm file) |
| **Sửa** | Thay đổi nội dung đã có |
| (mình) | Chỉ phần của chính người hỏi (khớp họ tên trong `Members`, hoặc họ tên admin trong config) |
| — | Không truy cập được qua chatbot |

Mọi **Tạo / Sửa** đều qua bản xem trước + nút Xác nhận và ghi `AuditLog`.

**Cây thư mục** (tính từ `drive.root_folder_id`):

```
<root>/
├── data/
│   ├── Members            sheet: tab thành viên + tab "Cựu thành viên"
│   ├── Equipment          sheet: tab Thietbi + tab LichSuMuon
│   ├── DutySchedule       sheet: mỗi tuần một tab
│   ├── FormSubmissions    sheet: mỗi loại đơn một tab
│   └── AuditLog           sheet: nhật ký thao tác
├── kb/
│   ├── public/
│   └── internal/
│       └── meeting-minutes/
├── templates/             mẫu trắng (DonXinGiaNhap, PhieuMuonThietBi, BienBanHop, LichTrucTuan, …)
│   ├── _system/<form_id>/v<version>/    bản có placeholder + schema.yaml
│   └── _archive/                        mẫu cũ đã thay
├── submissions/
│   ├── applications/<HoTen>_<SDT>/      đơn gia nhập + file đính kèm
│   ├── equipment-requests/<YYYY-MM>/    phiếu mượn thiết bị
│   └── <form_id>/<YYYY-MM>/             đơn theo mẫu thêm mới
└── _archive/applications/               đơn gia nhập bị từ chối quá 6 tháng
```

#### a) `data/`: dữ liệu dạng sheet

| File / tab | guest | member | thu_ki | leader | admin | Hệ thống tự ghi |
|---|---|---|---|---|---|---|
| `Members` – tab thành viên | — | Xem · Xuất (không SĐT, MSSV) | Xem · Xuất | Xem · Xuất | Xem · Xuất · **Tạo** (duyệt đơn) · chuyển sang cựu (cho rời lab) · **Sửa** mọi cột trừ `Address`/`No` †C6 · đổi role | — |
| `Members` – tab Cựu thành viên | — | — | Xem · Xuất | Xem · Xuất | Xem · Xuất · Sửa †C6 | — |
| `Equipment` – Thietbi | — | Xem · Xuất | Xem · Xuất | Xem · Xuất · Tạo · Sửa (thêm thiết bị, sửa tình trạng, số lượng) †C6 | như leader †C6 | Cột "Người mượn", "Còn lại" khi mượn / trả |
| `Equipment` – LichSuMuon | — | Xem · Xuất (đang mượn: tất cả; đã trả: mình) · Tạo / Sửa (mình: mượn, trả, gia hạn) | Xem · Xuất tất cả · Tạo / Sửa (mình) | Xem · Xuất tất cả · Tạo / Sửa (mình) · Sửa mọi dòng †C6 | như leader; mượn / trả †C1 | — |
| `DutySchedule` | — | Xem · Xuất †C9 · Sửa (mình: đăng ký buổi trực) †C9 | như member | Xem · Xuất †C9 · Sửa mọi ô †C6 | như leader | Job thứ Sáu 18:00 tạo tab tuần mới và gửi mail kèm link tab; tự giao buổi trực cố định |
| `FormSubmissions` | — | Xem (mình) | Xem tất cả †C3 | Xem tất cả · Sửa (trừ trạng thái đơn gia nhập) †C6 | Xem tất cả · Sửa · đổi trạng thái đơn gia nhập (duyệt / từ chối) | Thêm dòng khi có đơn nộp |
| `AuditLog` | — | — | — | — | Xem (trang admin) †C6 | Ghi mọi thao tác. **Không ai sửa**, kể cả admin |

#### b) `kb/`: kho kiến thức

| Thư mục | guest | member | thu_ki | leader | admin | Hệ thống tự ghi |
|---|---|---|---|---|---|---|
| `kb/public/` | Xem | Xem | Xem · Tạo · Sửa (đẩy nội quy, tài liệu mới) †C4 | như thư ký †C4 | như thư ký †C4 | Job `kb_sync` index lại 15 phút / lần |
| `kb/internal/` (trừ biên bản) | — | Xem | Xem · Tạo · Sửa †C4 | như thư ký †C4 | như thư ký †C4 | như trên |
| `kb/internal/meeting-minutes/` | — | Xem · Xuất | Xem · Xuất · Tạo (biên bản qua mẫu `bien_ban_hop`) | như thư ký | như thư ký †C4 | Index ngay khi lưu biên bản |
| KB web (trang web lab đã crawl, **không nằm trên Drive**) | Xem | Xem | Xem | Xem | Xem · crawl ngay · áp dụng lần crawl bị chặn | Crawl nhanh hằng ngày, crawl đủ thứ Hai |

"Xem" ở `kb/` nghĩa là chatbot trích đoạn liên quan để trả lời, không gửi nguyên file (trừ biên bản họp qua `export_form_files`). Tài liệu thư ký đẩy lên `kb/public` có hiệu lực ngay, không cần leader/admin duyệt.

#### c) `templates/`: mẫu đơn

| Thư mục | guest | member | thu_ki | leader | admin |
|---|---|---|---|---|---|
| `templates/` (mẫu trắng) | Xuất (chỉ đơn gia nhập) | Xuất (mẫu mình được nộp) | Xuất (mẫu mình được nộp) | Xuất · Tạo · ngừng dùng mẫu †C5 | Xuất · Tạo · ngừng dùng mẫu |
| `templates/_system/`, `templates/_archive/` | — | — | — | Ghi gián tiếp khi kích hoạt mẫu †C5 | Ghi gián tiếp khi kích hoạt mẫu |

#### d) `submissions/`: đơn đã nộp

| Thư mục | guest | member | thu_ki | leader | admin |
|---|---|---|---|---|---|
| `submissions/applications/<HoTen>_<SDT>/` | Tạo (nộp đơn của mình; **không xem lại được**) | — | Xem · Xuất †C3 | Xem · Xuất | Xem · Xuất |
| `submissions/equipment-requests/<YYYY-MM>/` | — | Tạo (nộp phiếu) · Xem · Xuất (mình) | Tạo · Xem · Xuất tất cả | Tạo · Xem · Xuất tất cả | Tạo †C1 · Xem · Xuất tất cả |
| `submissions/<form_id>/<YYYY-MM>/` (mẫu mới) | Tạo nếu mẫu cho guest | Tạo nếu mẫu cho phép · Xem · Xuất (mình) | Tạo nếu mẫu cho phép · Xem · Xuất tất cả †C3 | như thư ký | như thư ký |
| `_archive/applications/` | — | — | — | — | — (cần thì mở Drive) |

File đã nộp **không ai sửa hoặc xoá qua chatbot**. Muốn sửa thì nộp lại đơn mới. Job `retention` chuyển đơn gia nhập bị từ chối quá 6 tháng sang `_archive/`.

> **Chia sẻ Drive ngoài chatbot:** nếu thành viên được chia sẻ quyền sửa thẳng thư mục `data/` trên Drive thì họ đi vòng qua toàn bộ ma trận này. Quy ước: chỉ tài khoản lab và admin có quyền sửa `data/`, `templates/`, `submissions/`. Thành viên chỉ được quyền sửa sheet `DutySchedule` (để tự đăng ký qua link trong mail) và quyền xem `kb/`.

---

## 7. Tài nguyên gắn với session

| Tài nguyên | Khoá sở hữu | Hết hạn |
|---|---|---|
| Hội thoại | `session_public_id` | 30 ngày |
| Upload | `session_public_id` | 24 giờ nếu chưa nộp |
| Bản nháp đơn | `owner_session` | 24 giờ (`drafts.ttl_hours`) |
| Yêu cầu chờ xác nhận | `owner_session` | 24 giờ (`drafts.pending_action_ttl_hours`) |
| File xuất | `owner_session` | Theo config |

Vì gắn theo session, không theo tài khoản: đăng nhập trên máy khác không thấy bản nháp của máy này; đăng nhập/đăng xuất làm mất bản nháp đang dở. Đây là chủ đích (đơn giản, không lộ chéo).

---

## 8. Chế độ test

| Chế độ | Bật khi | Tác dụng | Production |
|---|---|---|---|
| `dev_login` | `APP_ENV≠production` + `test_mode` + `test.dev_login` | Chọn "đăng nhập với tư cách <họ tên>" (mọi người dùng chung 1 email) | Khởi động bị từ chối |
| `auth_bypass` | `APP_ENV≠production` + `test_mode` + `test.auth_bypass` | Không cần đăng nhập, chọn role ngay trên UI (cookie `la_dev_role`), kể cả admin | Khởi động bị từ chối |

Lưu ý: khi `auth_bypass` bật, **ai truy cập được server đều tự chọn được admin**. Không để server dev/test lộ ra Internet khi bật chế độ này.

---

## 9. Việc cần làm

### 9.1 Lỗ hổng bảo mật

| # | Mức | Vấn đề | Hậu quả | Cách sửa |
|---|---|---|---|---|
| G1 | Cao | `/metrics` kiểm tra theo IP kết nối trực tiếp. Chạy sau reverse proxy trên cùng máy thì mọi request đều đến từ `127.0.0.1` (nằm trong `metrics_allow_cidrs`) | `/metrics` (và `/readyz` sau C7) lộ ra Internet qua proxy | Từ chối khi request có `X-Forwarded-For`, **và** chặn `/metrics`, `/readyz` ở cấu hình proxy |
| G2 | Cao | `must_change_password` chỉ chặn ở `/chat` | Người đăng nhập bằng mật khẩu tạm vẫn dùng được `/uploads`, `/drafts/*`, `/actions/*`, và **toàn bộ `/admin/api/*`** nếu là admin | Dependency `require_password_ok` cho mọi route ngoài `/auth/*`, trang tĩnh và health. Trả 403 kèm mã để UI chuyển sang màn đổi mật khẩu |
| G3 | Cao | `POST /drafts/{id}/confirm` không kiểm tra lại role của mẫu đơn | Thành viên vừa bị xoá khỏi `Members` (hoặc bị hạ role) vẫn nộp được phiếu mượn / biên bản đã điền dở | Trong `FormEngine.confirm`, gọi `role_allowed(schema, ctx.role)` sau khi nạp form; sai thì trả 403, giữ bản nháp |
| G4 | TB | Trang `GET /admin/kb/web` trả HTML cho mọi người (dữ liệu vẫn an toàn) | Lộ giao diện quản trị | Kiểm tra role trên server (leader, admin); không đủ quyền thì chuyển về `/login` |
| G5 | TB | `/uploads` chưa có rate limit | Guest không cần đăng nhập vẫn upload được nhiều file 10 MB | Giới hạn theo session + IP (ví dụ guest 10 file/giờ, member+ 60 file/giờ), cộng giới hạn tổng dung lượng/session |
| G6 | TB | `code_plan.md` §7.13 ghi `/actions/*` là "Leader/admin", khác code | Tài liệu sai | Sửa bảng, trỏ sang file này |
| G7 | TB | Logic xác định admin lặp ở 2 nơi: `deps.py` so sánh email phân biệt hoa thường, `roles.resolve_role` thì không | Dễ lệch khi sửa | Gom về `resolve_role`; chuẩn hoá email về chữ thường khi nạp config (làm cùng C1) |
| G8 | TB | `/readyz` công khai, trả tên lỗi nội bộ | Lộ thông tin hạ tầng | Gộp vào C7 |

### 9.2 Thay đổi theo chính sách đã chốt

| # | Thay đổi | Chỗ sửa chính |
|---|---|---|
| C1 | **Admin được mượn thiết bị; 2 tài khoản admin có họ tên riêng.** Đổi `auth.admins` thành danh sách `{email, ho_ten}` (thầy và dev). Thêm admin vào `BORROWERS` (tool + `ACTION_ROLES`) và `roles` của `phieu_muon_thiet_bi`. `UserContext.ho_ten` của admin lấy từ config; luồng trả / gia hạn / nhắc quá hạn khớp người mượn theo họ tên đó, gửi mail theo email admin. `AuditLog` ghi đúng người | `app/config.py`, `config*.yaml`, `app/auth/roles.py`, `app/auth/deps.py`, `app/tools/builtin.py`, `app/tools/actions.py`, `app/workflows/actions.py`, `app/workflows/equipment.py`, `app/jobs/equipment.py`, `schemas/phieu_muon_thiet_bi.yaml` |
| C2 | **Vòng đời tài khoản (mục 1.2).** Đăng nhập đúng mật khẩu nhưng email không thuộc `Members`/`admins` → 403 "Tài khoản không còn hiệu lực" (chỉ báo sau khi đúng mật khẩu nên không lộ email tồn tại). Session đang mở mà email bị xoá khỏi `Members` → huỷ session ở request kế tiếp; `Members` lỗi tạm thời thì không huỷ. Tạo tài khoản tay chỉ cho email có trong `Members`/`admins`. Duyệt đơn gia nhập tự tạo tài khoản và gửi mật khẩu tạm. Thêm thao tác `delete` | `app/api/auth.py`, `app/auth/deps.py`, `app/auth/accounts.py`, `app/api/admin.py`, `app/workflows/members.py`, `app/web/static/login.js`, `app/web/static/admin.js` |
| C3 | **Thư ký xem / xuất mọi file trong `submissions/`**, kể cả đơn gia nhập | `app/exports/forms.py` (`_sees_all`) |
| C4 | **STAFF đẩy tài liệu vào KB.** Tool mới `publish_kb_document`: thư ký / leader / admin gửi file trong chat → chọn `public` hoặc `internal` → xem trước → Xác nhận → lưu Drive + index ngay; không cần duyệt thêm. Thêm admin vào `roles` của `bien_ban_hop` | `app/tools/` (tool mới), `app/workflows/actions.py` (`kb_publish`), `app/rag/indexer.py`, `schemas/bien_ban_hop.yaml` |
| C5 | **Leader thêm / ngừng mẫu đơn.** Mở tool mẫu đơn và `/admin/api/templates/*` cho leader; trang admin chỉ hiện mục "Mẫu đơn" cho leader. Cần `require_role` (mục 9.4) | `app/tools/templates.py`, `app/api/templates.py`, `app/auth/deps.py`, `app/web/static/admin.js` |
| C6 | **Sửa dữ liệu trong `data/`.** Tool mới `request_data_edit` qua yêu cầu chờ xác nhận: leader / admin sửa thiết bị, dòng mượn, lịch trực, `FormSubmissions` (trừ trạng thái đơn gia nhập); chỉ admin sửa thông tin trong `Members`. `AuditLog` không sửa được. Thêm trang admin xem `AuditLog` | `app/tools/` (tool mới), `app/workflows/` (workflow mới), `app/domain/*`, `app/api/admin.py` |
| C7 | **`/readyz` chỉ nội bộ** (dùng chung kiểm tra IP với `/metrics`) | `app/api/health.py` |
| C8 | **Leader mất quyền duyệt / từ chối đơn gia nhập và cho rời lab**, chỉ còn xem danh sách đơn chờ. `member_approve`, `member_reject`, `member_remove` và tool `request_member_change` chỉ còn admin; sửa prompt member cho khớp | `app/workflows/actions.py` (`ACTION_ROLES`), `app/tools/actions.py`, `app/agents/prompts/member.md` |
| C9 | **Lịch trực qua chat.** Tool mới `register_duty`: thành viên nhờ chatbot đánh dấu / bỏ buổi trực **của mình** trên tab tuần đang mở đăng ký, xem trước rồi Xác nhận (action `duty_register`). Thêm dữ liệu `duty` vào `export_data` để xuất lịch trực. Cách cũ (điền thẳng qua link Drive trong mail) giữ nguyên | `app/tools/` (tool mới), `app/workflows/` (workflow mới), `app/domain/duty.py`, `app/exports/build.py` |

### 9.3 Thứ tự triển khai (đã chốt: tính năng trước, bảo mật sau)

| Đợt | Nội dung | Ghi chú |
|---|---|---|
| 1. Chỉnh quyền trên code có sẵn | C8, C3, C1 (+ G7), C5, C2 | Sửa nhỏ, chủ yếu đổi bảng role và thêm kiểm tra. C5 cần `require_role` |
| 2. Tool mới | C9 (lịch trực), C4 (đẩy tài liệu KB), C6 (sửa dữ liệu + trang `AuditLog`) | Mỗi tool đi theo mẫu có sẵn: tool tạo yêu cầu → nút Xác nhận → outbox ghi Drive |
| 3. Bảo mật, **bắt buộc xong trước khi lên production** | G2, G3, G1 + C7, G4, G5, G6 | Kèm test ma trận route tự động (`ROUTE_POLICY`) |

**Tiến độ (2026-10-31):** đợt 1 (C8, C3, C1 + G7, C5, C2) và đợt 2 (C9, C4, C6 + trang `AuditLog`) đã có code và test
(`tests/integration/test_auth_plan.py`); ký hiệu † ở các mục trên không còn đúng cho các mục này. Đợt 3 chưa làm.

Mỗi đợt đều kèm test của các mục trong đợt đó (mục 10). Trong lúc chưa xong đợt 3, chỉ chạy ở môi trường test, không mở ra Internet.

### 9.4 Cải tiến cấu trúc

- **Dependency khai báo quyền:** `require_role(*roles)` dùng chung cho route, thay cho việc mỗi route tự gọi `current_user` rồi tự kiểm tra.
- **Bảng chính sách route là nguồn sự thật cho test:** `ROUTE_POLICY = {("GET", "/chat/{id}/history"): "owner", ("POST", "/admin/api/accounts"): {ADMIN}, ...}`. Test duyệt mọi route của `app.routes`; **route mới mà chưa khai báo chính sách thì test fail**.
- **Gom quyền Drive vào một chỗ:** bảng mục 6.2 thành một module (ví dụ `app/auth/drive_policy.py`) mà tool và workflow cùng gọi, thay vì mỗi chỗ tự viết `if ctx.role in STAFF`.

### 9.5 Ghi nhận, không đổi

- Role có hiệu lực chậm tối đa 60 giây sau khi sửa `Members` (cache). Các thao tác xác nhận đã đọc lại dữ liệu sống.
- Link tải mẫu trắng không gắn session: mẫu trắng không chứa dữ liệu cá nhân.
- Thành viên không tự sửa thông tin cá nhân qua chat; nhờ admin sửa.

---

## 10. Kiểm thử cần có

| Nhóm | Test | Đợt |
|---|---|---|
| Thay đổi nhân sự (C8) | Leader gọi `request_member_change` / xác nhận `member_approve` → bị từ chối; leader vẫn xem được `list_pending_applications` | 1 |
| Thư ký xem đơn (C3) | Thư ký xuất được đơn gia nhập; member vẫn chỉ xuất đơn của mình | 1 |
| Admin mượn thiết bị (C1) | Mỗi admin (không có trong `Members`) mượn → trả → gia hạn được; sổ mượn và `AuditLog` ghi đúng họ tên của admin đó | 1 |
| Leader quản lý mẫu đơn (C5) | Leader dùng được tool và `/admin/api/templates/*`; leader gọi `/admin/api/accounts` → 403 | 1 |
| Tài khoản (C2) | Xoá email khỏi `Members` → đăng nhập bị 403 với thông báo đúng, session đang mở bị huỷ; `Members` lỗi tạm thời thì không huỷ; duyệt đơn tạo tài khoản và gửi mail; tạo tay cho email lạ bị từ chối; `delete` xoá cả session | 1 |
| Lịch trực (C9) | Member đăng ký buổi của mình được; sửa ô của người khác bị từ chối; xuất lịch trực ra file | 2 |
| Đẩy KB (C4) | Thư ký đẩy vào `public` → guest tìm thấy; member gọi tool → bị từ chối | 2 |
| Sửa dữ liệu (C6) | Leader sửa thiết bị được, sửa `Members` bị từ chối; không ai sửa được `AuditLog` | 2 |
| Ma trận Drive | Với mỗi ô ở mục 6.2: gọi tool bằng từng role trên `FakeDrive`/`FakeSheets`, kiểm tra được / bị từ chối, và "(mình)" không trả dữ liệu của người khác | 2 |
| Ma trận route | Mỗi route × 5 role (+ chưa đăng nhập, tài khoản bị khoá, `must_change_password`) đúng bảng mục 2; route chưa khai báo trong `ROUTE_POLICY` làm test fail | 3 |
| Chủ sở hữu | Session A không đọc / xác nhận / huỷ được bản nháp, action, file xuất, hội thoại của session B (trả 404/410) | 3 |
| Hạ quyền giữa chừng (G3) | Tạo bản nháp → đổi role → hết cache → xác nhận bị từ chối | 3 |
| Vận hành (G1, C7) | `/metrics`, `/readyz` từ IP ngoài → 404; qua proxy có `X-Forwarded-For` → 404 | 3 |
| Tool | Mỗi role chỉ nhận đúng danh sách tool mục 3; gọi tool ngoài quyền bị từ chối | 1–3 |
| Dữ liệu | Guest không bao giờ nhận chunk `member`; member không nhận SĐT/MSSV | có sẵn |
| Chế độ test | `APP_ENV=production` + bật `dev_login`/`auth_bypass`/`test_mode` → khởi động thất bại | có sẵn |
| CSRF | Mọi POST thiếu/sai token → 403 | 3 |

---

## 11. Các quyết định đã chốt

| # | Câu hỏi | Quyết định |
|---|---|---|
| 1 | Thư ký duyệt / từ chối đơn gia nhập? | Không |
| 2 | Leader tạo / khoá tài khoản? | Không, chỉ admin |
| 3 | Tài khoản không còn trong `Members`? | Chặn đăng nhập, báo "Tài khoản không còn hiệu lực". Guest không cần tài khoản → C2 |
| 4 | Admin có cần trong `Members`, có mượn thiết bị? | Không cần trong `Members`; có mượn thiết bị → C1 |
| 5 | `/readyz` công khai hay nội bộ? | Nội bộ → C7 |
| 6 | Thứ tự làm? | Tính năng trước, bảo mật sau (trước production) → mục 9.3 |
| 7 | Leader duyệt đơn, cho rời lab? | Không. Thêm nhân sự, cho rời lab, đổi role đều do admin. Leader chỉ xem đơn chờ → C8 |
| 8 | Tài khoản admin? | 2 email admin riêng (thầy và dev), mỗi email gắn họ tên trong config. Admin tạo / xoá / sửa tài khoản khác và gán role (qua cột Role trong `Members`) → C1, C2 |
| 9 | Thành viên tự sửa thông tin cá nhân qua chat? | Không, nhờ admin |
| 10 | `AuditLog`? | Không ai sửa; chỉ admin xem → C6 |
| 11 | Admin lập biên bản họp? | Có → C4 |
| 12 | `_archive/applications/` qua chatbot? | Không, cần thì mở Drive |
| 13 | Thư ký đẩy lên `kb/public` cần duyệt thêm? | Không |
| 14 | Lịch trực? | Thành viên nhờ chatbot điền buổi của mình (có xác nhận) hoặc điền thẳng qua link Drive trong mail. Cho xuất lịch trực qua chat → C9 |
| 15 | Tạo tài khoản cho thành viên mới? | Tự tạo khi admin duyệt đơn, gửi mật khẩu tạm qua mail → C2 |
