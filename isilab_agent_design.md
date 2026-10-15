# isilab Agent Platform — Tài liệu thiết kế

> Phiên bản: 2026-10-15 (lần 2) · Trạng thái: đã chốt, dùng làm đầu vào để bắt đầu code
> Thay đổi: xem mục 14. Schema 3 đơn: thư mục `schemas/`. Cấu hình đầy đủ: `config.yaml`.
> Phạm vi: chatbot hỏi đáp KB (guest/member) + tự động hoá giấy tờ + các job định kỳ, toàn bộ dữ liệu nằm trong folder Google Drive `isilab`.

## Mục lục

0. Tổng quan và nguyên tắc thiết kế
1. Vai trò và phân quyền
2. Cấu trúc Google Drive, đặt tên và versioning
3. Mô hình dữ liệu (Google Sheets)
4. Kiến trúc hệ thống
5. Tool layer (MCP server)
6. Chatbot RAG
7. Form engine
8. Các luồng nghiệp vụ
9. Job định kỳ
10. Cấu hình
11. Cấu trúc repo đề xuất
12. Lộ trình triển khai
13. Giả định và điểm đã chốt
14. Lịch sử thay đổi

---

## 0. Tổng quan và nguyên tắc thiết kế

Hệ thống có ba khối chức năng:

- **Chatbot hỏi đáp**: trả lời từ tài liệu trong `kb/` (RAG) và từ các sheet vận hành trong `data/` (truy vấn trực tiếp). Guest chỉ thấy nội dung public, member thấy toàn bộ.
- **Tự động hoá giấy tờ**: một form engine dùng chung cho mọi loại đơn. User có thể tự điền đơn rồi gửi lên, hoặc đưa thông tin qua chat để agent điền hộ. Đơn agent điền hộ luôn phải được user xác nhận trước khi lưu.
- **Job định kỳ**: tạo lịch trực tuần, nhắc thành viên chưa đủ buổi trực, nhắc trả thiết bị, gửi báo cáo tuần cho leader và thư kí, đồng bộ index KB.

Sáu nguyên tắc áp dụng cho toàn bộ code:

1. **LLM hiểu, code kiểm tra, code ghi.** LLM chỉ trích xuất thông tin và diễn đạt câu trả lời. Validate, tính toán và mọi thao tác ghi vào Drive/Sheets/Gmail đều do code thực hiện.
2. **Least privilege theo luồng.** Agent không có tool Drive tổng quát. Mỗi tool là một thao tác nghiệp vụ có phạm vi cố định. Role của người dùng do backend gắn vào request, LLM không tự khai.
3. **Chưa xác nhận thì chưa ghi.** Bản nháp chỉ nằm trong backend. Thứ được lưu phải giống hệt thứ user đã xem trước.
4. **Không xoá cứng.** "Xoá" nghĩa là chuyển vào `_archive/` hoặc chuyển sang nơi lưu trữ có trạng thái rõ ràng (tab `Cựu thành viên`, ghi `ngày trả`, schema `inactive`).
5. **Job định kỳ là code cố định và idempotent.** Chạy lại nhiều lần không tạo dữ liệu trùng. LLM không quyết định việc gì được làm trong job.
6. **Dữ liệu có cấu trúc không đi qua RAG.** Câu hỏi về thiết bị, lịch trực, thành viên được trả lời bằng tool đọc sheet trực tiếp.

---

## 1. Vai trò và phân quyền

### 1.1 Xác định vai trò

```
Request → có tài khoản đăng nhập?
            ├── không → guest
            └── có → lấy email
                  ├── email thuộc auth.admins → admin (thầy phụ trách)
                  └── tra email trong tab chính của data/Members
                        ├── Role = Leader    → leader
                        ├── Role = Secretary → thu_ki
                        ├── Role = Member    → member
                        └── không tìm thấy (hoặc đã chuyển sang tab "Cựu thành viên") → guest
```

- Tài khoản do admin cấp. Email tài khoản phải trùng với cột Email trong `Members`.
- Việc kiểm tra role nằm ở backend, chạy trước khi gọi LLM. Role đọc lại từ `Members` mỗi request (cache ngắn, ví dụ 60 giây).
- Lời tự xưng trong chat ("mình là leader") không có tác dụng gì.

**Hai loại khoá:**

| Khoá | Dùng để | Yêu cầu |
|---|---|---|
| Email | Đăng nhập, gửi mail | Duy nhất khi chạy thật |
| Họ tên (cột Name) | Liên kết giữa các sheet: `DutySchedule` và `LichSuMuon` ghi họ tên, muốn gửi mail cho ai thì tra họ tên trong `Members` để lấy email | Duy nhất. Nếu hai người trùng tên, thêm phần phân biệt vào tên, ví dụ "Nguyễn Văn A (UET)" |

So khớp họ tên: chuẩn hoá Unicode NFC, bỏ khoảng trắng thừa, không phân biệt hoa thường. Mỗi lần job chạy, code kiểm tra `Members` không có tên trùng; nếu có thì dừng job và mail admin.

**Chế độ test** (`test_mode: true`): mọi thành viên đang dùng chung một email nên không phân biệt được bằng email. Màn hình đăng nhập có thêm lựa chọn "đăng nhập với tư cách <họ tên>" (dev login). Mọi mail có tiền tố `[TEST]` và dòng đầu ghi "Người nhận dự kiến: <họ tên>", để kiểm tra được job gửi đúng người dù tất cả mail về cùng một hộp thư. Tắt `test_mode` khi chạy thật.

### 1.2 Ma trận quyền

| Hành động | Guest | Member | Thư kí | Leader | Admin |
|---|---|---|---|---|---|
| Hỏi KB public (nội quy, web lab) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Hỏi KB nội bộ, thiết bị, lịch trực | – | ✓ | ✓ | ✓ | ✓ |
| Nộp đơn gia nhập, hỏi trạng thái đơn | ✓ | – | – | – | – |
| Mượn / trả / gia hạn thiết bị | – | ✓ | ✓ | ✓ | – |
| Nộp các đơn khác có `roles` chứa member | – | ✓ | ✓ | ✓ | – |
| Tạo biên bản họp | – | – | ✓ | ✓ | – |
| Duyệt / từ chối đơn gia nhập | – | – | – | ✓ | ✓ |
| Cho thành viên rời lab | – | – | – | ✓ | ✓ |
| Đổi leader / thư kí | – | – | – | – | ✓ |
| Duyệt template đơn mới | – | – | – | – | ✓ |

### 1.3 Ghi chú bảo mật

- **Sheet lịch trực để "ai có link cũng sửa được"**: lab đã chấp nhận. Lưu ý là người sửa không đăng nhập Google sẽ hiện ẩn danh trong lịch sử phiên bản. Lab vẫn khôi phục được nội dung, nhưng không xác định được người sửa.
- **Prompt injection**: nội dung file user gửi lên (CV, đơn) là dữ liệu, không phải lệnh. Tác động tối đa của một prompt injection chỉ nằm trong phạm vi các tool của role hiện tại. Các thao tác quan trọng (xác nhận đơn, kích hoạt template) không phải tool của LLM mà là nút bấm gọi thẳng backend.
- **Thao tác thay đổi quyền** (duyệt đơn, cho rời lab, đổi role) luôn có bước xác nhận và luôn được ghi vào `AuditLog`.

---

## 2. Cấu trúc Google Drive, đặt tên và versioning

### 2.1 Cây thư mục

```
isilab/                                   # chủ sở hữu: tài khoản Gmail riêng của lab
├── kb/                                   # tài liệu phi cấu trúc → RAG
│   ├── public/                           # guest + member: nội quy, giới thiệu lab
│   └── internal/                         # chỉ member
│       └── meeting-minutes/              # biên bản họp
├── data/                                 # sheet vận hành → truy vấn bằng tool, KHÔNG index
│   ├── Members                           # thành viên = bảng phân quyền (+ tab "Cựu thành viên")
│   ├── Equipment                         # tab Thietbi + tab LichSuMuon
│   ├── DutySchedule                      # 1 tab / tuần, chia sẻ "ai có link cũng sửa được"
│   ├── FormSubmissions                   # 1 tab / loại đơn
│   └── AuditLog                          # nhật ký mọi thao tác ghi của hệ thống
├── templates/                            # admin chỉ thả đơn trống vào cấp này
│   ├── DonXinGiaNhap                     # Google Doc (mỗi đơn chỉ giữ MỘT file)
│   ├── PhieuMuonThietBi.docx
│   ├── BienBanHop                        # Google Doc
│   ├── LichTrucTuan                      # Google Sheet, form engine bỏ qua
│   └── _system/                          # agent quản lý
│       └── <form_id>/v<YYYY-MM-DD>/
│           ├── template.docx             # bản sao đã chèn {{placeholder}}
│           └── schema.yaml
├── submissions/
│   ├── applications/<HoTen>_<SDT>/
│   ├── equipment-requests/<YYYY-MM>/
│   └── <form_id>/<YYYY-MM>/              # đơn mới do agent tự sinh schema
└── _archive/                             # thay cho xoá
```

Quyền truy cập RAG suy ra từ đường dẫn: `kb/public/**` gắn `access=public`, `kb/internal/**` gắn `access=member`.

**Quy ước `_`:** file, folder và tab có tên bắt đầu bằng `_` (`_README.md`, `_system/`, `_archive/`, tab `_HuongDan`) được indexer, job sinh schema và code đọc dữ liệu bỏ qua.

**Khởi tạo:** cây thư mục và các sheet ban đầu nằm trong `isilab.zip`. Trước khi upload, bật cài đặt Drive *"Chuyển đổi tệp tải lên sang định dạng trình chỉnh sửa Google Tài liệu"* để các file xlsx thành Google Sheets (Sheets API không đọc/ghi ô trên file xlsx). Sau khi upload: chia sẻ `DutySchedule` ở chế độ "ai có link cũng sửa được", bật protected range cho `Members`, cột "Người mượn" của `Thietbi` và toàn bộ `AuditLog`.

### 2.2 Quy tắc đặt tên

- Ngày luôn ở định dạng ISO `YYYY-MM-DD` và đặt ở đầu tên file, để Drive tự sắp xếp đúng thứ tự thời gian.
- Tên người được bỏ dấu và viết liền theo kiểu PascalCase: "Nguyễn Văn Đức" thành `NguyenVanDuc`.
- SĐT luôn chuẩn hoá về dạng `0xxxxxxxxx`.

### 2.3 Versioning

| Loại file | Cách version |
|---|---|
| File nộp vào (đơn, CV, phiếu mượn) | Tiền tố `YYYY-MM-DD_` theo ngày nộp; nộp lại cùng ngày thêm `_v2`, `_v3` |
| Biên bản họp | Tiền tố theo **ngày họp**, không phải ngày đẩy lên |
| Sheet dùng liên tục (`data/*`) | Không đổi tên file. Dựa vào revision history của Drive và `AuditLog` |
| Tài liệu KB được thay bản mới | Bản cũ chuyển vào `_archive/` với tiền tố ngày; bản hiện hành giữ tên cố định; chỉ index bản hiện hành |
| Template đơn | Mỗi lần sửa sinh thư mục `_system/<form_id>/v<ngày>/` mới |

---

## 3. Mô hình dữ liệu (Google Sheets)

**Nguyên tắc chung: code đọc và ghi theo tiêu đề cột, không theo vị trí cột.** Ánh xạ "tên logic → tiêu đề trên sheet" nằm trong `config.yaml` (mục `sheets`). So khớp tiêu đề bỏ khoảng trắng thừa và không phân biệt hoa thường. Lab giữ được tiêu đề tiếng Việt quen dùng, có thể đổi thứ tự cột hoặc chèn thêm cột mà không phải sửa code. Đổi tên một tiêu đề thì sửa đúng một dòng trong config.

### 3.1 `data/Members`

Giữ nguyên bảng của lab. Dòng 1 là tiêu đề "INFORMATION LAB MEMBERS", dòng 2 trống, **header ở dòng 3**.

| Tiêu đề trên sheet | Tên logic | Ghi chú |
|---|---|---|
| No | – | Bỏ qua |
| Student ID | `mssv` | |
| Name | `ho_ten` | **Khoá liên kết** giữa các sheet, phải duy nhất |
| Lớp | `lop` | |
| Phone | `sdt` | Chỉ leader, thư kí, admin thấy qua chatbot |
| Address | – | Không bao giờ trả ra qua tool. Khi chạy thật nên bỏ cột này |
| University | `truong` | |
| Email | `email` | Đăng nhập và gửi mail |
| Role | `role` | `Leader` → leader, `Secretary` → thu_ki, `Member` → member |

- Lab có đúng 1 leader (kiêm người quản lý thiết bị) và có thể nhiều thư kí.
- Thầy phụ trách không nằm trong sheet này; quyền admin lấy từ `auth.admins` trong config.
- **Thành viên rời lab:** dòng được chuyển sang tab `Cựu thành viên` (cùng các cột, thêm cột "Ngày rời"). Tab chính vì vậy luôn chỉ chứa thành viên đang hoạt động, không cần cột trạng thái. Lịch sử mượn và biên bản cũ vẫn tra được tên trong tab cựu thành viên.
- Chỉ leader, thư kí và tài khoản hệ thống được sửa sheet này (protected range).

### 3.2 `data/Equipment`

**Tab `Thietbi`** (header dòng 1):

| Tiêu đề | Tên logic | Ghi chú |
|---|---|---|
| Mã thiết bị | `ma_tb` | `TB001`…, không bao giờ đổi |
| Thiết bị | `ten_tb` | |
| Trạng thái hoạt động | `tinh_trang` | `Tốt` / `Hỏng nhẹ` / `Hỏng` / `Mất`; `Hỏng`, `Mất` không cho mượn |
| Số lượng | `tong_so_luong` | Số nguyên |
| Ngày kiểm tra | – | Hệ thống không dùng |
| Ghi chú | `ghi_chu` | |
| **Người mượn** *(cột mới)* | `nguoi_muon` | Code ghi lại sau mỗi lần mượn/trả. Mỗi người đang mượn một dòng trong ô, dạng `Họ tên - SL - ngày mượn - hạn trả` |

Số đang mượn và số còn lại **không lưu trên sheet**, code tính khi đọc:

```
dang_muon(ma_tb) = tổng "Số lượng" các dòng LichSuMuon có Mã thiết bị = ma_tb và "ngày trả" trống
con_lai(ma_tb)   = tong_so_luong − dang_muon
```

Nếu lab muốn nhìn thấy số còn lại ngay trên sheet, có thể thêm cột "Còn lại" với công thức `=D2-SUMIFS(LichSuMuon!J:J,LichSuMuon!I:I,A2,LichSuMuon!F:F,"")` (chỉnh chữ cột theo vị trí thật); code không phụ thuộc cột này.

**Tab `LichSuMuon`** (header dòng 1). Giữ 8 cột hiện có, thêm 4 cột ở cuối:

| Tiêu đề | Tên logic | Ghi chú |
|---|---|---|
| Thời gian | `ngay_muon` | `dd/mm/yyyy`; đặt locale sheet là Việt Nam |
| Tên người mượn | `ho_ten` | Họ tên đầy đủ đúng như `Members` (code ghi) |
| thiết bị mượn | `ten_tb` | Tên chuẩn lấy từ `Thietbi` (code ghi) |
| trạng thái | `tinh_trang_khi_muon` | |
| Hạn trả | `han_tra` | |
| ngày trả | `ngay_tra` | **Trống = đang mượn** |
| trạng thái sau trả | `tinh_trang_khi_tra` | |
| Ghi chú | `ghi_chu` | Lịch sử gia hạn, ví dụ `gia hạn 20/10/2026 → 27/10/2026` |
| **Mã thiết bị** *(mới)* | `ma_tb` | |
| **Số lượng** *(mới)* | `so_luong` | Dòng cũ để trống được hiểu là 1 |
| **Mã phiếu** *(mới)* | `ma_phieu` | `P-YYYYMMDD-NN`, chung cho các thiết bị cùng một phiếu |
| **Link phiếu** *(mới)* | `link_phieu` | Link file phiếu trong `submissions/` |

- **Mỗi dòng là một thiết bị trong một phiếu.** Một dòng được xác định bởi cặp (`ma_phieu`, `ma_tb`).
- **Trạng thái suy ra từ dữ liệu, không lưu cột riêng:** đang mượn khi `ngày trả` trống; quá hạn khi đang mượn và `Hạn trả` < hôm nay. Có thể thêm conditional formatting tô đỏ dòng quá hạn.
- Chỉ member được mượn. Người ngoài lab tự liên hệ leader, không đi qua hệ thống.

### 3.3 `data/DutySchedule`

Giữ nguyên bố cục của lab, **không có cột email**: muốn gửi mail cho ai thì tra họ tên trong `Members`.

| Dòng | Nội dung |
|---|---|
| 1 | Tiêu đề `LỊCH TRỰC LAB TUẦN TỪ dd/mm - dd/mm` (job ghi) |
| 2 | Chú thích: tick `v` là đăng ký trực, `T` là lab giao trực |
| 3 | Hạn ghi nội dung công việc: trước 0h ngày chủ nhật |
| 4 | Thứ 2 … Chủ nhật (mỗi thứ merge 2 cột), "Nội dung công việc đang thực hiện", "Số buổi trực lab" |
| 5 | No, Name, Sáng, Chiều, … |
| 6 | Thầy phụ trách (tên ở cột A, không có số thứ tự) |
| 7 trở đi | Thành viên: cột A là số thứ tự, cột B là họ tên |

- **Dòng thành viên** = cột No là số và cột Name không trống. Dòng thầy phụ trách và các dòng đánh số nhưng chưa có tên đều tự bị bỏ qua.
- **Cột buổi trực** được code dò từ dòng 4–5 (dưới các tiêu đề thứ, có "Sáng"/"Chiều"). **Cột số buổi** và **cột nội dung công việc** dò theo tiêu đề ở dòng 4. Nhờ vậy bố cục template (công việc chiếm 2 cột, số buổi ở cột S) và các tab cũ (số buổi ở cột R) đều đọc được.
- Đếm buổi = số ô `V` hoặc `T`, không phân biệt hoa thường. Code tự đếm, không cần công thức trên sheet.
- **Tên tab** dạng `dd/mm-dd/mm` như lab đang dùng, ví dụ `12/10-18/10`. Năm được suy ra theo ngày gần hiện tại nhất. Tên tab phải khớp với tuần trong tiêu đề, vì job tìm tuần theo tên tab.
- Họ tên trong lịch trực phải viết giống hệt `Members`. Tên nào không khớp được đưa vào báo cáo tuần (mục 9.3) để leader sửa.

### 3.4 `data/FormSubmissions`

Mỗi loại đơn một tab, tên tab là `form_id`. Agent tự tạo tab và cột dựa trên schema.

| submission_id | thoi_gian_nop | nguoi_nop (họ tên hoặc "guest") | <các trường theo schema…> | link_file | trang_thai | ghi_chu |
|---|---|---|---|---|---|---|

- Trường kiểu `table` (danh sách thiết bị, phân công công việc) được ghi dạng JSON trong một ô.
- Riêng tab `don_xin_gia_nhap`: `trang_thai` thuộc `{CHO_DUYET, DA_DUYET, TU_CHOI}`.

### 3.5 `data/AuditLog`

| thoi_gian | actor (email / "system" / "guest:<session>") | role | action | target | ket_qua (OK / FAILED) | chi_tiet |
|---|---|---|---|---|---|---|

Mọi thao tác ghi của hệ thống (tool, job, xác nhận đơn) đều thêm đúng một dòng vào đây. Ở chế độ test, `actor` ghi thêm họ tên của người đăng nhập.

---

## 4. Kiến trúc hệ thống

### 4.1 Thành phần

```
┌──────────────┐    ┌────────────────────────────────────────────────────────────┐
│ Web chat UI  │───►│ Backend (FastAPI)                                          │
│ - đăng nhập  │    │  ├─ Auth & role resolver ── data/Members + bảng tài khoản  │
│ - upload     │    │  ├─ Agent router ─► Guest agent  / Member agent (LLM)      │
│ - nút Xác    │    │  │                     │ chỉ gọi tool được cấp theo role   │
│   nhận / Huỷ │    │  ├─ Confirm endpoints (KHÔNG phải tool của LLM)            │
└──────────────┘    │  ├─ Form engine (registry, drafts, render, validate)       │
                    │  ├─ Scheduler (cron jobs)                                  │
                    │  └─ Indexer (Drive changes → chunk → vector DB)            │
                    └──────────┬───────────────────────────────┬─────────────────┘
                               │                               │
                    ┌──────────▼──────────┐         ┌──────────▼──────────┐
                    │ MCP server nghiệp vụ│         │ Vector DB + BM25    │
                    │ (kiểm tra quyền)    │         │ (hybrid search)     │
                    └──────────┬──────────┘         └─────────────────────┘
                               │
                    ┌──────────▼────────────────────────────────┐
                    │ Google APIs: Drive · Sheets · Docs · Gmail │
                    └───────────────────────────────────────────┘
```

Backend có thêm một DB riêng (SQLite khi phát triển, Postgres khi triển khai) để lưu: tài khoản, bản nháp đơn, trạng thái đồng bộ index (page token, hash từng file), outbox cho các thao tác ghi nhiều bước, và cache form registry.

### 4.2 Stack đề xuất (thay được)

| Phần | Đề xuất |
|---|---|
| Ngôn ngữ / web | Python 3.11+, FastAPI |
| MCP server | FastMCP |
| Scheduler | APScheduler, timezone `Asia/Ho_Chi_Minh` |
| Vector DB | Qdrant (hỗ trợ dense + sparse cho hybrid search) hoặc pgvector + BM25 |
| Render đơn | `docxtpl` (điền placeholder) + LibreOffice headless (docx sang PDF) |
| Đọc file | `python-docx`, `pypdf` / `pdfplumber` |
| Validate | Pydantic v2 |

### 4.3 Cấu hình Google API

- Bật 4 API trong Google Cloud Console: **Drive, Sheets, Docs, Gmail**.
- **Xác thực bằng OAuth của một tài khoản Gmail riêng của lab** (ví dụ `isilab.agent@gmail.com`), tài khoản này là chủ folder `isilab`. Lưu refresh token trong `.env` hoặc secret manager. Không dùng service account, vì service account không gửi được Gmail thay cho tài khoản @gmail.com thường và hay gặp lỗi dung lượng khi tạo file trong Drive của người khác.
- **OAuth consent screen phải ở trạng thái "In production".** Ở trạng thái "Testing", refresh token hết hạn sau 7 ngày và các cron sẽ chết im lặng. Cảnh báo "ứng dụng chưa xác minh" có thể bỏ qua vì chỉ tài khoản lab đăng nhập.
- Scope: `https://www.googleapis.com/auth/drive`, `.../auth/spreadsheets`, `.../auth/documents`, `.../auth/gmail.send`.

| Việc | API / method |
|---|---|
| Tạo tab lịch trực | Sheets `spreadsheets.sheets.copyTo` (tab `Template` của `templates/LichTrucTuan` sang `DutySchedule`), rồi `batchUpdate` → `updateSheetProperties` đổi tên |
| Đọc / ghi ô | Sheets `values.get`, `values.append`, `values.update`, `values.batchUpdate` |
| Dropdown, khoá cột | Sheets `batchUpdate` → `setDataValidation`, `addProtectedRange` |
| Xuất template sang docx | Drive `files.export` (Google Doc) hoặc `files.get?alt=media` (docx) |
| Upload đơn, tạo folder, chuyển file | Drive `files.create`, `files.update` (addParents / removeParents) |
| Lưu biên bản dạng Google Doc | Drive `files.create` với `mimeType=application/vnd.google-apps.document` (convert khi upload) |
| Theo dõi thay đổi | Drive `changes.getStartPageToken`, `changes.list` |
| Link chia sẻ | Drive `permissions.create` + trường `webViewLink` |
| Gửi mail | Gmail `users.messages.send` |

### 4.4 Ghi nhiều bước và xử lý lỗi

Một thao tác nghiệp vụ thường gồm nhiều bước ghi, ví dụ: upload file, thêm dòng vào sheet, gửi mail. Để tránh dữ liệu lệch nhau khi API lỗi giữa chừng:

1. Ghi ý định vào bảng `outbox` trong DB backend (gồm `operation_id` và danh sách các bước).
2. Worker thực hiện lần lượt từng bước. Mỗi bước idempotent theo `operation_id`: trước khi ghi, kiểm tra xem bước đó đã được thực hiện chưa.
3. Bước nào lỗi thì retry với backoff. Quá số lần retry thì ghi `FAILED` vào `AuditLog` và gửi mail báo admin.

Mọi thao tác ghi đi qua một hàng đợi đơn luồng, để tránh tranh chấp (ví dụ hai người cùng mượn món thiết bị cuối cùng).

---

## 5. Tool layer (MCP server)

Server tự gắn role vào mỗi lời gọi tool và kiểm tra quyền trong code. Tool không nhận tham số đường dẫn Drive tuỳ ý.

| Tool | Role | Mô tả |
|---|---|---|
| `search_kb(query, filters?)` | tất cả | Hybrid search, tự lọc `access` theo role |
| `list_forms()` | tất cả | Danh sách đơn đang `active` mà role được dùng, kèm `description` |
| `get_form_template(form_id)` | theo `roles` của form | Trả file đơn trống để gửi cho user |
| `start_form(form_id)` | theo `roles` | Tạo bản nháp, điền sẵn trường `source: account` |
| `update_form_fields(draft_id, fields)` | chủ bản nháp | Cập nhật trường (LLM trích xuất từ chat), trả về danh sách trường còn thiếu hoặc sai |
| `attach_file(draft_id, key, upload_id)` | chủ bản nháp | Gắn file đính kèm (CV) |
| `submit_filled_form(form_id, upload_id)` | theo `roles` | User tự điền: trích xuất, validate, trả về lỗi hoặc yêu cầu xác nhận lưu |
| `render_preview(draft_id)` | chủ bản nháp | Render PDF xem trước, trả về `shown_hash` để UI gắn vào nút Xác nhận |
| `cancel_form(draft_id)` | chủ bản nháp | Huỷ bản nháp |
| `get_application_status()` | guest | Luôn trả câu trả lời cố định (mục 8.1) |
| `query_equipment(name?)` | member+ | Đọc `Thietbi` và `LichSuMuon`, trả số lượng còn lại, tình trạng, người đang mượn |
| `my_borrows()` | member+ | Các dòng đang mượn (`ngày trả` trống) có `Tên người mượn` = họ tên người hỏi |
| `return_equipment(ma_phieu, ma_tb, so_luong, tinh_trang)` | member+ (người mượn) | Luồng trả (mục 8.3) |
| `extend_borrow(ma_phieu, han_tra_moi, ma_tb?)` | member+ (người mượn) | Gia hạn cả phiếu hoặc một thiết bị |
| `query_duty(member?, week?)` | member+ | Đọc tab lịch trực |
| `query_members(name?)` | member+ | Tên, role, email (không trả SĐT cho member thường nếu lab muốn) |
| `list_pending_applications()` | leader, admin | Đơn gia nhập `CHO_DUYET` |
| `request_member_change(action, target, …)` | leader / admin | Tạo yêu cầu duyệt đơn, cho rời lab, đổi role; trả về bản tóm tắt chờ xác nhận |

**Không có tool cho:** xác nhận đơn, kích hoạt template, xác nhận thay đổi thành viên, xoá file, chia sẻ file tuỳ ý. Các thao tác xác nhận là endpoint backend gắn với nút bấm trên UI.

---

## 6. Chatbot RAG

### 6.1 Định tuyến câu hỏi

| Ví dụ câu hỏi | Đường xử lý |
|---|---|
| "Nội quy về giờ ra vào lab?" | `search_kb` |
| "Cuộc họp tuần trước bàn gì?" | `search_kb` với filter `doc_date` trong khoảng tuần trước, chỉ trong `meeting-minutes` |
| "Thành viên X trực những ngày nào tuần này?" | `query_duty(member=X, week=current)` |
| "Máy chiếu còn mấy cái, ai đang mượn?" | `query_equipment("máy chiếu")` |
| "Mình đang mượn gì, hạn khi nào?" | `my_borrows()` |
| "Lab có đơn xin nghỉ trực không?" | `list_forms()` |

Biểu thức thời gian ("tuần trước", "tháng 9") được code chuyển thành khoảng ngày trước khi gọi tool. Mọi phép đếm, cộng trừ số lượng do code thực hiện.

### 6.2 Indexing

- **Nguồn:** `kb/public/**`, `kb/internal/**`, và nội dung crawl từ website lab (lưu với `source=web`, `access=public`).
- **Chunking:** theo cấu trúc tiêu đề của tài liệu, khoảng 500–800 token, overlap khoảng 10–15%. Mỗi chunk mang tiêu đề tài liệu và tiêu đề mục để giữ ngữ cảnh.
- **Metadata mỗi chunk:** `file_id`, `path`, `title`, `access`, `doc_type` (`rule` / `minutes` / `web` / `other`), `doc_date`, `content_hash`, `chunk_index`.
- **`doc_date` của biên bản** lấy từ trường "Ngày họp" trong format biên bản, không lấy ngày upload.
- **Retrieval:** dense + BM25, gộp bằng RRF, sau đó rerank top-k. Filter `access` được áp dụng **tại retriever**, nên chunk nội bộ không bao giờ được đưa vào context của guest.

### 6.3 Re-index

| Nguồn thay đổi | Phát hiện | Hành động |
|---|---|---|
| Hệ thống tự ghi vào `kb/` (biên bản) | Biết ngay | Re-index file đó ngay sau khi ghi |
| Người sửa trực tiếp trên Drive | Poll `changes.list` mỗi 15 phút | Re-index các file đã thay đổi |
| Website lab | Crawl định kỳ | Hàng tuần (chỉnh trong config) |
| Lưới an toàn | Đối chiếu toàn bộ `kb/` với vector DB | Chủ nhật 23:00 |

Cách re-index một file: khoá theo `file_id`. Hash nội dung không đổi thì bỏ qua. Hash đổi thì xoá toàn bộ chunk của `file_id` rồi chèn chunk mới. File bị chuyển ra khỏi `kb/` (ví dụ vào `_archive/`) thì xoá chunk. Sheet trong `data/` không được index.

---

## 7. Form engine

### 7.1 Schema đơn

Mỗi đơn có một schema nằm trong `templates/_system/<form_id>/v<ngày>/schema.yaml`. Ở P3, schema của 3 đơn ban đầu được viết tay theo đúng mẫu đơn thật của lab, nằm trong thư mục `schemas/`:

| File | Mẫu đơn | Ai dùng | Hook |
|---|---|---|---|
| `schemas/don_xin_gia_nhap.yaml` | `templates/DonXinGiaNhap` | guest | `application` |
| `schemas/phieu_muon_thiet_bi.yaml` | `templates/PhieuMuonThietBi.docx` | member, thu_ki, leader | `equipment_borrow` |
| `schemas/bien_ban_hop.yaml` | `templates/BienBanHop` | thu_ki, leader | `meeting_minutes` |

Ở P4, schema do agent tự sinh (mục 7.7) phải cho ra kết quả tương đương ba file này; chúng đồng thời là dữ liệu kiểm thử cho `schema_gen`.

**Thuộc tính của một trường:**

| Thuộc tính | Ý nghĩa |
|---|---|
| `label` | Tên hiển thị khi hỏi user và trong bản xem trước |
| `anchor` | Đoạn text trong mẫu đơn đứng ngay trước ô trống; dùng để chèn placeholder |
| `blank: next_line` | Ô trống nằm ở dòng dưới `anchor` (các mục đánh số 1., 2., III., IV.) |
| `required`, `default` | Bắt buộc / giá trị mặc định (`today`, `leader`, `account.<trường>`) |
| `source: account.<trường>` + `locked` | Lấy từ dòng `Members` của người đăng nhập, user không sửa được |
| `filled_by: system` | Hệ thống tự điền (ngày làm đơn = ngày nộp) |
| `filled_by: lab` | Để trống, lab ghi tay (ý kiến trưởng lab, xác nhận trả) |
| `derived_from` | Cột trong bảng tự điền từ cột khác (mã và tình trạng thiết bị từ tên thiết bị) |
| `before`, `after`, `not_before`, `not_after` | Ràng buộc ngày/giờ so với `today` hoặc trường khác |

Phần `signatures` mô tả các ô "Ký và ghi rõ họ tên": ô của người nộp in họ tên (thay chữ ký khi nộp online), ô của lab để trống. Phần `output.format` quy định lưu dạng `pdf` (đúng bản user đã xem trước) hay `gdoc` (biên bản, để sửa được và để index).

**Các kiểu trường:**

| type | Validate |
|---|---|
| `text` | Không rỗng nếu `required` |
| `longtext` | Như `text`, không giới hạn độ dài dòng |
| `phone` | Chuẩn hoá `+84` → `0`, khớp `^0\d{9}$` |
| `email` | Đúng định dạng, domain thuộc `email.allowed_domains` |
| `date` | Ngày hợp lệ, định dạng `dd/mm/yyyy`; hỗ trợ các ràng buộc ngày |
| `time` | Giờ `HH:MM` |
| `int` | Số nguyên; hỗ trợ `min`, `max` |
| `choice` | Thuộc `options` |
| `member_ref` | Khớp một họ tên trong `Members` (khớp mờ, nhiều kết quả thì hỏi lại). `multiple: true` cho danh sách; `allow_admin` cho phép tên thầy phụ trách; `allow_unknown` cho phép tên ngoài lab (bản xem trước đánh dấu) |
| `equipment_ref` | Khớp một thiết bị trong `Thietbi` theo tên hoặc mã (khớp mờ, nhiều kết quả thì hỏi lại); trạng thái không phải `Hỏng` / `Mất` |
| `table` | Danh sách nhiều dòng, mỗi cột có kiểu riêng; hỗ trợ `min_rows` và `rules` cấp bảng (ví dụ `so_luong_khong_vuot_con_lai`, cộng dồn theo `ma_tb`). Render bằng vòng lặp dòng của `docxtpl` (`{%tr for %}`) |
| `row_number` | Số thứ tự dòng trong bảng, tự sinh |

### 7.2 Hai cách nộp đơn

```
                ┌──────────── user gửi file đơn đã điền ────────────┐
                │                                                   ▼
user hỏi → [GỬI FORMAT + LỜI MỜI]                         [TRÍCH XUẤT + VALIDATE]
                │                                                   │
                └── user đưa thông tin qua chat ──► [THU THẬP]      │ hợp lệ, file giữ nguyên
                                                       │            │
                                  thiếu/sai → hỏi lại ◄┤            │
                                                       ▼            │
                                                  [XEM TRƯỚC] ◄─ cần sửa
                                                   │      │         │
                                     user muốn sửa ┘      │ bấm     │
                                     → THU THẬP lại        │ Xác nhận│
                                                          ▼         ▼
                                              [KIỂM TRA LẠI] → [LƯU + XỬ LÝ TIẾP]

Ngoài luồng: HUỶ (user huỷ) · HẾT HẠN (bản nháp quá 24h không xác nhận → xoá khỏi DB)
```

**Quy tắc xác nhận:** nếu file sẽ lưu khác với file user đã gửi lên thì bắt buộc xác nhận.

- User tự điền, file hợp lệ, lưu nguyên file: không cần xem trước. Vẫn hỏi một câu xác nhận nộp bằng nút bấm.
- User gửi đơn sai rồi nhờ agent sửa: coi như agent điền, phải xem trước và xác nhận.
- User đưa thông tin qua chat: phải xem trước và xác nhận.

Lời mời mẫu khi user muốn nộp đơn:

> Bạn có thể tải mẫu đơn này về, điền rồi gửi lại cho mình 📎 DonXinGiaNhap.docx
> Hoặc bạn cho mình các thông tin: họ tên, trường, mã sinh viên, số điện thoại, email, lý do muốn tham gia (không bắt buộc), mình sẽ điền đơn giúp bạn. Đừng quên gửi kèm CV nhé.

Danh sách thông tin trong lời mời được sinh từ `fields` của schema, nên đúng với mọi loại đơn.

### 7.3 Thu thập thông tin

- LLM chỉ trích xuất. Trường nào user chưa nói thì để trống và hỏi lại. Không suy đoán, không tự chuẩn hoá ngầm (ví dụ không tự đổi "UET" thành tên đầy đủ). Nếu muốn chuẩn hoá, giá trị đã chuẩn hoá phải hiện trong bản xem trước.
- Thông tin có thể đến qua nhiều tin nhắn; bản nháp được cập nhật dần.
- Hỏi gộp tất cả trường còn thiếu hoặc sai trong một tin nhắn.
- Trường `locked` không sửa được qua chat; agent giải thích lý do nếu user yêu cầu.
- File đính kèm bắt buộc (CV) phải có trước khi chuyển sang bước xem trước.

### 7.4 Xem trước

- Render ở backend: tải template từ `_system/` (đã có placeholder), điền bằng `docxtpl`, chuyển sang PDF. **Không tạo file nào trên Drive trước khi xác nhận.**
- Gửi PDF trong chat, kèm bản tóm tắt các trường dạng text. Không gửi link Drive cho guest.
- Mỗi lần sửa tạo phiên bản xem trước mới (v1, v2…). UI hiện nút **[Xác nhận] [Huỷ]** gắn với `draft_id` và `shown_hash` của phiên bản đang hiển thị.

### 7.5 Xác nhận

Endpoint backend, gọi từ nút bấm. LLM không có quyền gọi.

```python
def confirm(draft_id: str, shown_hash: str, user) -> SaveResult:
    d = drafts.get(draft_id)
    if d is None or d.owner != user.session_id or d.expired():
        raise DraftInvalid("Bản nháp không còn hiệu lực, vui lòng tạo lại.")
    if sha256(d.rendered_bytes) != shown_hash:   # nội dung đổi sau khi xem trước
        raise DraftChanged("Đơn đã thay đổi, vui lòng xem và xác nhận lại.")
    errors = validate(d.form, d.fields, recheck_live=True)
    if errors:                                   # vd: thiết bị vừa bị người khác mượn hết
        return SaveResult.needs_edit(errors)
    return save_and_process(d)                   # chỉ từ đây mới ghi Drive / Sheets
```

`recheck_live` kiểm tra lại những ràng buộc phụ thuộc dữ liệu thay đổi theo thời gian (số lượng thiết bị còn lại, trạng thái thành viên).

### 7.6 Xử lý sau khi lưu

**Xử lý chung** (mọi đơn):

1. Upload file đơn (và đính kèm) vào folder theo `output`.
2. Thêm một dòng vào tab `<form_id>` của `data/FormSubmissions`.
3. Ghi `AuditLog`.
4. Đơn sẽ xuất hiện trong báo cáo chủ nhật.

**Hook đặc thù** (code viết tay, gắn với `form_id` qua trường `hook`):

| Hook | Đơn | Việc làm thêm |
|---|---|---|
| `application` | Đơn xin gia nhập | Folder `applications/<HoTen>_<SDT>/`, version theo SĐT, `trang_thai = CHO_DUYET` |
| `equipment_borrow` | Phiếu mượn thiết bị | Mỗi thiết bị một dòng `LichSuMuon` chung `ma_phieu`, ghi lại cột "Người mượn", trả `ma_phieu` cho user |
| `meeting_minutes` | Biên bản họp | Lưu dạng Google Doc vào `kb/internal/meeting-minutes/`, tên theo ngày họp, re-index ngay |

Đơn mới do agent tự sinh schema chỉ có xử lý chung. Muốn có xử lý đặc thù (ví dụ đơn xin nghỉ trực tự xoá ô `V` trong lịch trực) thì người phát triển viết thêm hook. Agent không tự suy ra thao tác ghi đặc thù.

### 7.7 Tự sinh schema từ đơn trống

Admin chỉ cần thả file đơn trống (docx hoặc Google Doc) vào `templates/`. Agent tự sinh schema và gửi admin duyệt một lần.

```
Admin thả DonXinNghiTruc.docx vào templates/
        │  (Drive changes, mục 6.3)
        ▼
[SINH SCHEMA] ① tìm ô trống bằng luật  ② LLM rà soát thêm  ③ suy luận kiểu dữ liệu
        │     ④ chèn placeholder vào bản sao  ⑤ tự kiểm tra bằng dữ liệu mẫu
        ├── lỗi (PDF, ảnh scan, không tìm thấy ô trống…) → mail báo admin kèm lý do
        ▼
[pending_review] → mail admin: danh sách trường + PDF điền thử + 2 câu hỏi
        ├── admin yêu cầu sửa bằng lời → cập nhật → gửi lại
        ▼
[active] → chatbot bắt đầu giới thiệu đơn
```

**① Tìm ô trống bằng luật**

```python
import re
from docx import Document

BLANK = re.compile(r"(?P<label>[^:.…_\n]{2,60}?)\s*:\s*(?P<blank>[.…_]{3,})")

def find_blanks(path: str) -> list[dict]:
    doc, out = Document(path), []
    for i, p in enumerate(doc.paragraphs):
        for m in BLANK.finditer(p.text):          # 1 dòng có thể chứa nhiều ô
            out.append({"where": f"p{i}", "label": m["label"].strip(), "anchor": m[0]})
    for t, table in enumerate(doc.tables):
        for r, row in enumerate(table.rows):
            cells = row.cells
            for c in range(len(cells) - 1):        # ô có chữ, ô bên phải trống
                if cells[c].text.strip() and not cells[c + 1].text.strip():
                    out.append({"where": f"t{t}r{r}c{c+1}", "label": cells[c].text.strip()})
    return out
```

Các mẫu cần bắt: `Nhãn: ......`, `Nhãn: ____`, ô bảng trống cạnh ô nhãn, `....../....../......`, `☐ A ☐ B`.

Bổ sung sau khi kiểm tra 3 mẫu đơn thật của lab:

```python
DOTS_ONLY = re.compile(r"^[\s.…_]{3,}$")
DATE_LINE = re.compile(r"ngày\s*…+\s*tháng\s*…+\s*năm\s*…+")

# (a) dòng chỉ có dấu chấm → ô trống, nhãn = đoạn ngay phía trên ("1. Hướng nghiên cứu quan tâm")
if DOTS_ONLY.match(p.text) and i > 0:
    out.append({"where": f"p{i}", "label": doc.paragraphs[i - 1].text.strip(), "kind": "block"})
# (b) "Hà Nội, ngày … tháng … năm …" → trường ngày, mặc định là ngày nộp
if DATE_LINE.search(p.text):
    out.append({"where": f"p{i}", "label": "ngay_lap", "kind": "date"})
# (c) bảng có hàng đầu bắt đầu bằng "STT" và các hàng còn lại trống → trường kiểu table
rows = table.rows
if rows[0].cells[0].text.strip().upper() == "STT" and all(
        not c.text.strip() for r in rows[1:] for c in r.cells):
    out.append({"where": f"t{t}", "kind": "table",
                "columns": [c.text.strip() for c in rows[0].cells]})
# (d) bảng chữ ký: ô chứa "Ký và ghi rõ họ tên" → một trường chữ ký cho mỗi ô
for c in (c for r in rows for c in r.cells):
    if "ký và ghi rõ họ tên" in c.text.lower():
        out.append({"where": f"t{t}", "kind": "signature",
                    "label": c.text.split("(")[0].strip()})
```

Luật (c) kiểm tra ô đầu là "STT" để không nhầm với bảng chữ ký và bảng quốc hiệu. Luật (d): ô chữ ký của người nộp ("NGƯỜI LÀM ĐƠN", "NGƯỜI MƯỢN", "THƯ KÝ", "CHỦ TRÌ") được in họ tên tương ứng; các ô còn lại là `filled_by: lab`. Các ô trống chỉ có một dấu "…" trong câu (ví dụ "kết thúc vào lúc … cùng ngày") do bước ② (LLM) bắt.

**② LLM rà soát thêm.** LLM nhận toàn văn đơn cùng danh sách ô đã tìm được, đề xuất các ô còn sót (ví dụ "Tôi tên là ............, sinh ngày ......"). Mỗi đề xuất bắt buộc kèm `anchor` là đoạn text xuất hiện nguyên văn trong đơn; code kiểm tra lại và loại bỏ đề xuất có anchor không tồn tại.

**③ Suy luận kiểu dữ liệu** (luật trước, LLM xử lý phần còn lại)

| Dấu hiệu | Kiểu |
|---|---|
| "điện thoại", "SĐT" | `phone` |
| "email", "thư điện tử" | `email` |
| "ngày", hoặc mẫu `../../....` | `date` |
| "số lượng", "SL" | `int` |
| "thiết bị", "mã thiết bị" | `equipment_ref` |
| `☐ A ☐ B` | `choice` với `options` |
| "chữ ký", "xác nhận của…", "ý kiến của…" | `filled_by: lab` |
| "lý do", "nội dung", "mô tả", "ghi chú" | `longtext`, `required: false` với "ghi chú" và "lý do" |
| Còn lại | `text`, `required: true` |

`form_id` sinh từ tên file (bỏ dấu, snake_case). `description` do LLM tóm tắt. Nếu admin chọn đơn chỉ dành cho member, trường họ tên và email được tự đặt `source: account, locked: true`.

**④ Chèn placeholder** vào bản sao trong `_system/<form_id>/v<ngày>/template.docx`, không sửa file gốc. Trước khi thay thế phải gộp các run của đoạn văn, vì một dòng thường bị chia thành nhiều run định dạng.

**⑤ Tự kiểm tra:** điền dữ liệu mẫu, render PDF, rồi kiểm tra: không còn placeholder `{{...}}` sót lại; không còn chuỗi dấu chấm hoặc gạch dưới tại các vị trí đã nhận diện; số trường trong schema khớp số ô tìm được ở ① và ②. Không qua thì không gửi duyệt mà báo lỗi cho admin.

**Mail duyệt gửi admin**

> **Đơn mới cần duyệt: Đơn xin nghỉ trực lab**
> 📎 DonXinNghiTruc_dien_thu.pdf (đã điền dữ liệu mẫu, các ô nhận diện được tô màu)
> Agent nhận diện được 6 trường: Họ và tên · Email · Ngày xin nghỉ (ngày) · Buổi (Sáng/Chiều) · Lý do (không bắt buộc) · Xác nhận của Leader (để trống, lab điền)
> 1. Ai được dùng đơn này? Đề xuất: **Member**
> 2. Lưu đơn vào: `submissions/don_xin_nghi_truc/`
> Ghi chú: đơn này có vẻ liên quan đến lịch trực, cần bổ sung hook nếu muốn tự cập nhật lịch.
> **[Kích hoạt] [Yêu cầu sửa] [Bỏ qua]**

**Vòng đời template**

| Admin làm gì trên Drive | Hệ thống xử lý |
|---|---|
| Thả file mới | Sinh schema, gửi duyệt |
| Sửa hoặc thay file đang dùng | Sinh phiên bản mới và gửi duyệt. Phiên bản cũ vẫn `active` đến khi bản mới được kích hoạt. Bản nháp đang dở render bằng phiên bản đã tạo ra nó |
| Chuyển file vào `_archive/` | Đơn chuyển `inactive`, chatbot ngừng giới thiệu. Đơn đã nộp không ảnh hưởng |
| Thả PDF hoặc ảnh scan | Không xử lý, mail admin yêu cầu bản Word hoặc Google Doc |
| Thả Google Sheet | Bỏ qua (template của cron, không phải đơn) |
| Hai file cho ra cùng `form_id` (ví dụ `BienBanHop` và `BienBanHop.docx`) | Không sinh schema cho đơn đó, mail admin yêu cầu giữ lại một file |

Ba đơn ban đầu (gia nhập, mượn thiết bị, biên bản họp) cũng đi qua pipeline này. Sau khi admin duyệt, người phát triển gán `hook` cho chúng.

---

## 8. Các luồng nghiệp vụ

### 8.1 Guest đăng ký gia nhập lab

1. Guest hỏi về việc tham gia lab. Agent gửi đơn trống kèm lời mời điền hộ (mục 7.2).
2. Guest tự điền và gửi lên, hoặc đưa thông tin qua chat. Thu thập, validate, xem trước, xác nhận theo mục 7.
3. Validate: họ tên không rỗng; trường không rỗng; MSSV không rỗng (không ép định dạng vì sinh viên đến từ nhiều trường); SĐT và email theo hàm dưới; có CV (PDF/DOCX, tối đa 10 MB).
4. Sau khi xác nhận, hook `application`:
   - Lưu vào `submissions/applications/<HoTen>_<SDT>/` với tên `YYYY-MM-DD_DonXinGiaNhap.<ext>` và `YYYY-MM-DD_CV.<ext>`.
   - Cùng SĐT nộp lại thì lưu version mới trong cùng folder.
   - Thêm dòng vào `FormSubmissions/don_xin_gia_nhap` với `trang_thai = CHO_DUYET`.
5. Leader nhận danh sách đơn mới trong báo cáo chủ nhật.
6. Guest hỏi trạng thái đơn: luôn trả lời cố định *"Đơn của bạn đang trong quá trình xử lý. Nếu muốn biết thêm chi tiết, vui lòng liên hệ {email thư kí}."* Email thư kí đọc từ `Members` lúc trả lời. Câu trả lời này không xác nhận hay phủ nhận việc một người cụ thể đã nộp đơn.
7. Đơn mẫu cần có một câu đồng ý cho lab xử lý dữ liệu cá nhân. Đơn bị từ chối được chuyển vào `_archive/` sau 6 tháng.

```python
import re, unicodedata

ALLOWED_DOMAINS = {"gmail.com", "vnu.edu.vn"}  # đọc từ config trong code thật

def normalize_phone(p: str) -> str:
    p = re.sub(r"[\s.\-()]", "", p)
    return "0" + p[3:] if p.startswith("+84") else p

def valid_phone(p: str) -> bool:
    return re.fullmatch(r"0\d{9}", normalize_phone(p)) is not None

def valid_email(e: str) -> bool:
    m = re.fullmatch(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)", e.strip())
    return bool(m) and m.group(1).lower() in ALLOWED_DOMAINS

def folder_name(ho_ten: str, sdt: str) -> str:  # "Nguyễn Văn Đức" → NguyenVanDuc_0912345678
    s = ho_ten.replace("đ", "d").replace("Đ", "D")    # NFD không tách được chữ đ
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return "".join(w.capitalize() for w in s.split()) + "_" + normalize_phone(sdt)
```

### 8.2 Mượn thiết bị

Member luôn liên hệ leader (người quản lý thiết bị) và được đồng ý trước khi gửi phiếu, nên hệ thống **mặc định chấp thuận**. Schema: `schemas/phieu_muon_thiet_bi.yaml`.

1. Member gửi phiếu tự điền, hoặc đưa thông tin qua chat để agent điền hộ. Toàn bộ mục I (họ tên, vai trò, MSSV, SĐT, email) lấy từ dòng `Members` của người đăng nhập, không lấy từ nội dung phiếu.
2. Mục III là bảng danh sách thiết bị, một phiếu mượn được nhiều thiết bị. Với mỗi dòng: tên thiết bị khớp với `Thietbi`; cột "Mã / Số serial" và "Tình trạng khi mượn" tự điền từ `Thietbi`; số lượng không vượt số còn lại (cộng dồn nếu một thiết bị xuất hiện nhiều dòng); thiết bị không ở trạng thái `Hỏng` / `Mất`. Ràng buộc chung: `Mượn từ ngày` ≥ hôm nay, `Dự kiến trả ngày` sau ngày mượn.
3. Xem trước, xác nhận. Lúc xác nhận kiểm tra lại số còn lại của mọi dòng.
4. Hook `equipment_borrow`:
   - Sinh `ma_phieu` dạng `P-YYYYMMDD-NN`, lưu PDF phiếu vào `submissions/equipment-requests/YYYY-MM/YYYY-MM-DD_<HoTen>_<ma_phieu>.pdf`.
   - Thêm **mỗi thiết bị một dòng** vào `LichSuMuon`:

     | Thời gian | Tên người mượn | thiết bị mượn | trạng thái | Hạn trả | ngày trả | trạng thái sau trả | Ghi chú | Mã thiết bị | Số lượng | Mã phiếu | Link phiếu |
     |---|---|---|---|---|---|---|---|---|---|---|---|
     | ngày mượn | họ tên trong `Members` | tên trong `Thietbi` | tình trạng khi mượn | hạn trả | *(trống)* | *(trống)* | ghi chú dòng | `ma_tb` | SL | `ma_phieu` | link PDF |

   - Ghi lại cột "Người mượn" trong `Thietbi` cho các thiết bị liên quan.
   - Trả `ma_phieu` cho member.
5. Mục V "Xác nhận trả thiết bị" và các ô ký của người quản lý thiết bị, trưởng lab là `filled_by: lab`.

### 8.3 Trả, trả một phần, gia hạn

- **Trả:** member báo trả, agent gọi `my_borrows()` và liệt kê các thiết bị đang mượn, nhóm theo `ma_phieu`. Member chọn trả cả phiếu hoặc từng thiết bị. Agent hỏi tình trạng khi trả, sau đó ghi `ngày trả` và `trạng thái sau trả`. Nếu hỏng hoặc mất thì cập nhật "Trạng thái hoạt động" trong `Thietbi`. Cuối cùng ghi lại cột "Người mượn".
- **Trả một phần:** dòng cũ được ghi số lượng đã trả cùng ngày trả; thêm một dòng mới cùng `ma_phieu`, `ma_tb`, `Hạn trả` với số lượng còn lại và `ngày trả` trống.
- **Gia hạn:** cập nhật `Hạn trả` của cả phiếu hoặc một thiết bị, ghi lịch sử vào `Ghi chú`. Hiện trong báo cáo tuần.
- Dòng cũ của lab (không có `Mã phiếu`) vẫn trả được: agent nhận diện theo cặp (họ tên, tên thiết bị).

### 8.4 Biên bản họp

Schema: `schemas/bien_ban_hop.yaml`.

1. Thư ký (hoặc leader) gửi nội dung họp: ghi chú thô, transcript, hoặc gạch đầu dòng.
2. Agent điền theo mẫu: ngày họp, giờ bắt đầu và kết thúc, hình thức, địa điểm, chủ trì (mặc định leader, có thể là thầy phụ trách), thư ký (mặc định người đang đăng nhập), thành viên tham dự, nội dung, kết luận, bảng phân công công việc.
3. **Ngày họp** là ngày ở dòng "Hà Nội, ngày … tháng … năm …" đầu biên bản (mẫu không có dòng "Ngày họp" riêng). Mặc định hôm nay; nếu ghi chú cho biết họp hôm khác thì agent hỏi lại.
4. "Vắng mặt" được agent đề xuất sẵn = thành viên trong `Members` trừ người tham dự; thư ký sửa nếu sai. Tên trong "Người phụ trách" phải khớp `Members`.
5. Xem trước, thư ký xác nhận.
6. Hook `meeting_minutes`: lưu dạng Google Doc vào `kb/internal/meeting-minutes/` với tên `<ngày họp>_BienBanHop`, re-index ngay với `doc_date = ngày họp`.

### 8.5 Duyệt đơn gia nhập (leader)

1. Leader gọi `list_pending_applications()`, chọn đơn, chọn duyệt hoặc từ chối.
2. Hệ thống hiện bản tóm tắt, leader bấm xác nhận.
3. **Duyệt:**
   - Kiểm tra họ tên chưa có trong `Members`; nếu trùng, leader nhập tên có phần phân biệt.
   - Thêm một dòng vào tab chính `Members`: No = số tiếp theo, Student ID ← `mssv`, Name ← `ho_ten`, Lớp ← `lop_khoa`, Phone ← `sdt`, University ← `khoa_truong`, Email ← `email`, Role = `Member`.
   - Mail admin yêu cầu cấp tài khoản với đúng email đó; mail chào mừng ứng viên; `trang_thai = DA_DUYET`.
   - Từ tuần kế tiếp, người này có tên trong lịch trực.
4. **Từ chối:** `trang_thai = TU_CHOI`; mail báo ứng viên.

### 8.6 Thành viên rời lab (leader)

1. Leader yêu cầu cho thành viên X rời lab. Nếu tên gần giống nhiều người, agent liệt kê để leader chọn.
2. **Chặn nếu X còn thiết bị đang mượn** (dòng `LichSuMuon` có tên X và `ngày trả` trống); báo leader xử lý trả thiết bị trước.
3. Leader xác nhận "Chuyển X sang danh sách cựu thành viên?".
4. Chuyển dòng của X từ tab chính sang tab `Cựu thành viên`, ghi thêm "Ngày rời". Mail admin khoá tài khoản. Từ tuần sau X không còn trong lịch trực.

### 8.7 Đổi leader hoặc thư kí (admin)

1. Admin yêu cầu đổi role. Agent hiện tóm tắt thay đổi:
   - Đổi leader: người mới thành `leader`, leader cũ trở về `member` (luôn đúng 1 leader).
   - Thư kí: thêm hoặc bỏ từng người (`member` ↔ `thu_ki`), không tự động hạ người khác, vì lab có thể có nhiều thư kí.
2. Admin xác nhận. Cập nhật cột Role trong `Members`, ghi `AuditLog`.
3. Có hiệu lực ngay, vì mọi job đọc `Members` mỗi lần chạy.

---

## 9. Job định kỳ

Tất cả chạy theo giờ `Asia/Ho_Chi_Minh`, idempotent theo khoá ghi trong bảng.

| Thời điểm | Job | Việc làm | Khoá idempotent |
|---|---|---|---|
| Mỗi 15 phút | `kb_sync` | Poll Drive changes; re-index `kb/`; phát hiện template mới trong `templates/` | page token |
| Hằng ngày 08:00 | `equipment_reminder` | Nhắc người có hạn trả vào ngày mai; nhắc quá hạn mỗi 3 ngày (gộp theo người mượn, email tra từ `Members`) | họ tên + `ma_phieu` + ngày |
| Hằng ngày 03:00 | `draft_cleanup` | Xoá bản nháp quá 24h | – |
| Thứ 6 18:00 | `duty_create` | Tạo tab tuần sau | tên tab tuần |
| Chủ nhật 22:00 | `duty_remind` | Nhắc **một lần** thành viên chưa đủ 3 buổi ở tab tuần sau | tuần + email |
| Chủ nhật 20:00 | `weekly_report` | Mail báo cáo cho leader và thư kí | tuần |
| Chủ nhật 23:00 | `kb_reconcile` | Đối chiếu toàn bộ `kb/` với vector DB | tuần |
| Theo config | `web_crawl` | Crawl lại website lab | ngày chạy |
| Hằng tháng | `retention` | Chuyển đơn gia nhập bị từ chối quá 6 tháng vào `_archive/` | tháng |

### 9.1 `duty_create` (thứ 6, 18:00)

1. Tính tuần sau, tên tab dạng `dd/mm-dd/mm`. Nếu tab đã tồn tại thì dừng.
2. Copy tab đầu của `templates/LichTrucTuan` sang `DutySchedule` bằng `sheets.copyTo`, đổi tên tab.
3. Ghi tiêu đề `LỊCH TRỰC LAB TUẦN TỪ dd/mm - dd/mm` vào ô A1. Dòng 6 (thầy phụ trách) giữ nguyên từ template.
4. Điền họ tên các thành viên trong tab chính `Members` vào cột B từ dòng 7, theo thứ tự trong `Members`. Template có sẵn số thứ tự 1–17; nếu nhiều thành viên hơn thì chèn thêm dòng và đánh số tiếp.
5. **Chép các ô `T` từ tab tuần hiện tại** (lịch cố định của leader), ghép theo họ tên. Người không còn trong `Members` thì bỏ qua.
6. Khoá cột A, B của tab mới.

### 9.2 `duty_remind` (chủ nhật, 22:00)

1. Đọc tab tuần sau, lấy các dòng thành viên (cột No là số, cột Name không trống).
2. Với mỗi người có số ô `V`/`T` nhỏ hơn 3: tra họ tên trong `Members` để lấy email, gửi **một** mail kèm link tab và số buổi hiện có. Nội dung mail dùng template cố định.
3. Họ tên không tìm thấy trong `Members` thì không gửi được: ghi `AuditLog` (`FAILED`, lý do "không khớp Members").

Thứ tự trong ngày chủ nhật: báo cáo cho leader và thư ký lúc 20:00 (danh sách "chưa đủ 3 buổi" tính đến 20:00), sau đó nhắc thành viên lúc 22:00.

### 9.3 `weekly_report` (chủ nhật 20:00)

Gửi đến email của leader và **tất cả** thư ký, đọc từ `Members` lúc chạy. Toàn bộ dùng template, không cần LLM. Mục không có dữ liệu ghi "Không có" thay vì bỏ trống.

```
Chủ đề: [isilab] Báo cáo tuần 21/09 – 27/09/2026

1. LỊCH TRỰC TUẦN TỚI (28/09 – 04/10): <link tab>
   Chưa đủ 3 buổi (2 người):
   - Nguyễn Văn A — 1/3
   - Trần Thị B — 0/3

2. THIẾT BỊ QUÁ HẠN TRẢ (1)
   - Lê Văn C — Máy chiếu (TB003) ×1 — hạn 20/09 — trễ 7 ngày

3. MƯỢN / TRẢ TRONG TUẦN
   - Mượn mới: 4 lượt | Đã trả: 3 lượt | Gia hạn: 1 lượt
   - Trả kèm hư hỏng: Arduino Uno (TB010) — Phạm Văn D — "gãy chân cắm"

4. ĐƠN GIA NHẬP MỚI (2)
   - Hoàng Văn E — 0912345678 — e@gmail.com — <link folder>
   - Đỗ Thị F — 0987654321 — f@vnu.edu.vn — <link folder>

5. ĐƠN KHÁC TRONG TUẦN
   - Đơn xin nghỉ trực: 1 — <link tab FormSubmissions>

6. DỮ LIỆU CẦN SỬA
   - Lịch trực tuần tới có tên không khớp Members: "Trung Đào"
   - LichSuMuon có tên không khớp Members: "Trung Đào" (3 dòng, đã trả)
```

Nguồn dữ liệu: tab lịch trực tuần sau (mục 1, 6), `LichSuMuon` (mục 2, 3, 6), `FormSubmissions` (mục 4, 5). Mục 6 chỉ liệt kê dòng đang mượn hoặc thuộc tuần tới; dòng lịch sử đã trả có tên không khớp chỉ cảnh báo một lần.

---

## 10. Cấu hình

Toàn bộ cấu hình nằm trong `config.yaml` (bí mật như OAuth client, refresh token, LLM API key để trong `.env`). Phần quan trọng nhất là `sheets`: ánh xạ tên logic sang tiêu đề cột thật của lab, và bố cục lịch trực. Nội dung file:

```yaml
timezone: Asia/Ho_Chi_Minh

# Giai đoạn test: mọi thành viên dùng chung 1 email, dữ liệu là dữ liệu giả.
test_mode: true
test:
  dev_login: true                  # màn hình đăng nhập cho chọn "đăng nhập với tư cách <tên thành viên>"
  subject_prefix: "[TEST]"         # mọi mail có tiền tố này
  show_intended_recipient: true    # dòng đầu mail ghi "Người nhận dự kiến: <họ tên>"

drive:
  root_folder_id: "1qTHFS17ZSoF534-8c3b54YskDPZW1lb0"   # các file con tra theo đường dẫn khi khởi động, cache ID

auth:
  admins: ["<email thầy phụ trách>"]  # thầy Nguyễn Đăng Khoa; không nằm trong Members

email:
  sender: "<email tài khoản lab>"
  allowed_domains: [gmail.com, vnu.edu.vn]

upload:
  max_mb: 10
  allowed_types: [pdf, docx]

# ---------------------------------------------------------------
# Ánh xạ sheet: code đọc/ghi theo TIÊU ĐỀ CỘT, không theo vị trí cột.
# So khớp tiêu đề bỏ khoảng trắng thừa và không phân biệt hoa thường.
# Lab có thể đổi thứ tự cột, chèn cột mới; chỉ cần giữ nguyên tiêu đề.
# ---------------------------------------------------------------
sheets:
  members:
    path: data/Members
    tab: null                      # null = tab đầu tiên
    header_row: 3
    columns:
      mssv: "Student ID"
      ho_ten: "Name"               # KHOÁ liên kết với DutySchedule và LichSuMuon
      lop: "Lớp"
      sdt: "Phone"
      truong: "University"
      email: "Email"               # dùng để đăng nhập và gửi mail
      role: "Role"
    never_expose: ["Address", "No"]
    role_values: {Leader: leader, Secretary: thu_ki, Member: member}
    role_labels: {leader: Leader, thu_ki: "Thư ký", member: "Thành viên"}
    former_tab: "Cựu thành viên"   # thành viên rời lab được chuyển dòng sang tab này

  equipment:
    path: data/Equipment
    devices:
      tab: Thietbi
      header_row: 1
      columns:
        ma_tb: "Mã thiết bị"
        ten_tb: "Thiết bị"
        tinh_trang: "Trạng thái hoạt động"
        tong_so_luong: "Số lượng"
        ghi_chu: "Ghi chú"
        nguoi_muon: "Người mượn"   # CỘT MỚI, code ghi lại sau mỗi lần mượn/trả
      tinh_trang_values: ["Tốt", "Hỏng nhẹ", "Hỏng", "Mất"]
      not_borrowable: ["Hỏng", "Mất"]
    borrows:
      tab: LichSuMuon
      header_row: 1
      columns:
        ngay_muon: "Thời gian"
        ho_ten: "Tên người mượn"
        ten_tb: "thiết bị mượn"
        tinh_trang_khi_muon: "trạng thái"
        han_tra: "Hạn trả"
        ngay_tra: "ngày trả"       # TRỐNG = đang mượn
        tinh_trang_khi_tra: "trạng thái sau trả"
        ghi_chu: "Ghi chú"
        ma_tb: "Mã thiết bị"       # CỘT MỚI
        so_luong: "Số lượng"       # CỘT MỚI
        ma_phieu: "Mã phiếu"       # CỘT MỚI
        link_phieu: "Link phiếu"   # CỘT MỚI
      date_format: "%d/%m/%Y"
      default_so_luong: 1          # dòng cũ để trống số lượng

  duty:
    path: data/DutySchedule
    template_path: templates/LichTrucTuan
    template_tab: null
    tab_name_format: "{start:%d/%m}-{end:%d/%m}"          # vd 12/10-18/10
    title_cell: A1
    title_format: "LỊCH TRỰC LAB TUẦN TỪ {start:%d/%m} - {end:%d/%m}"
    day_header_row: 4              # Thứ 2 … Chủ nhật (mỗi thứ merge 2 cột)
    slot_header_row: 5             # No | Name | Sáng | Chiều | …
    first_member_row: 7            # dòng 6 là thầy phụ trách
    no_col: A
    name_col: B
    worklog_header: "Nội dung công việc đang thực hiện"
    count_header: "Số buổi trực lab"
    member_row_rule: "cột No là số và cột Name không trống"
    marks_counted: [V, T]          # không phân biệt hoa thường
    fixed_mark: T                  # được chép sang tuần sau
    min_sessions_per_week: 3

  form_submissions: {path: data/FormSubmissions}
  audit_log: {path: data/AuditLog}

drafts:
  ttl_hours: 24

applications:
  rejected_retention_months: 6

index:
  drive_poll_minutes: 15
  chunk_tokens: 700
  chunk_overlap: 0.12
  web_urls: ["<URL website lab>"]
  web_crawl_cron: "0 2 * * MON"

schedule:
  duty_create:        "0 18 * * FRI"
  weekly_report:      "0 20 * * SUN"
  duty_remind:        "0 22 * * SUN"
  equipment_reminder: "0 8 * * *"
  kb_reconcile:       "0 23 * * SUN"
  draft_cleanup:      "0 3 * * *"
  retention:          "0 4 1 * *"

equipment:
  overdue_reminder_every_days: 3
```

---

## 11. Cấu trúc repo đề xuất

```
isilab-agent/
├── app/
│   ├── main.py                  # FastAPI: chat API, upload, confirm endpoints, admin review
│   ├── config.py                # đọc config.yaml + .env
│   ├── auth/                    # đăng nhập, resolve role (mục 1.1)
│   ├── google/                  # client mỏng: drive.py, sheets.py, docs.py, gmail.py, oauth.py
│   ├── domain/                  # đọc/ghi sheet có kiểu: members.py, equipment.py, duty.py,
│   │                            #   submissions.py, audit.py
│   ├── mcp_server/              # tools.py: tool nghiệp vụ + kiểm tra quyền (mục 5)
│   ├── agents/                  # guest_agent.py, member_agent.py, router.py, prompts/
│   ├── forms/
│   │   ├── registry.py          # nạp schema từ _system/, cache
│   │   ├── validators.py        # các type ở mục 7.1
│   │   ├── drafts.py            # lưu bản nháp, TTL
│   │   ├── render.py            # docxtpl + chuyển PDF
│   │   ├── engine.py            # thu thập, xem trước, confirm
│   │   └── schema_gen/          # blanks.py, llm_sweep.py, types.py, placeholder.py, selftest.py
│   ├── hooks/                   # application.py, equipment_borrow.py, meeting_minutes.py
│   ├── rag/                     # indexer.py, chunker.py, retriever.py, crawler.py
│   ├── jobs/                    # scheduler.py, duty.py, equipment.py, weekly_report.py,
│   │                            #   kb_sync.py, cleanup.py
│   └── outbox.py                # ghi nhiều bước, retry (mục 4.4)
├── schemas/                     # don_xin_gia_nhap.yaml, phieu_muon_thiet_bi.yaml, bien_ban_hop.yaml
├── email_templates/             # weekly_report.txt.j2, duty_remind.txt.j2, ...
├── tests/
│   ├── unit/                    # validators, folder_name, công thức tính quá hạn
│   ├── integration/             # chạy với một folder Drive test riêng
│   └── security/                # prompt injection trong CV, giả mạo role
├── config.yaml
├── .env.example
└── README.md
```

---

## 12. Lộ trình triển khai

Thứ tự này ưu tiên phần không cần LLM trước, để có nền tảng chắc chắn và kiểm thử được bằng code thông thường.

| Giai đoạn | Nội dung | Xong khi |
|---|---|---|
| **P0 — Nền móng** | OAuth tài khoản lab, 4 Google client, config, tạo cây folder và các sheet `data/`, `domain/members.py`, resolve role, `AuditLog` | Script đọc được `Members`, xác định đúng role cho 3 email test, gửi được 1 mail |
| **P1 — Job không LLM** | `domain/equipment.py`, `domain/duty.py`, `duty_create`, `duty_remind`, `equipment_reminder`, `weekly_report`, outbox | Chạy tay từng job trên folder test cho kết quả đúng; chạy 2 lần không tạo trùng |
| **P2 — Chatbot** | Indexer + Drive changes, hybrid retriever, tool `query_*`, guest/member agent, filter `access` | Guest không bao giờ nhận được chunk `internal`; 20 câu hỏi mẫu trả lời đúng |
| **P3 — Form engine** | Schema viết tay cho 3 đơn ban đầu, drafts, render, xem trước, endpoint confirm, 3 hook, luồng trả / gia hạn, luồng quản lý thành viên | Nộp được cả 3 loại đơn theo 2 cách; sửa sau xem trước bắt buộc xác nhận lại |
| **P4 — Tự sinh schema** | `schema_gen/`, trang duyệt admin, vòng đời template | Thả 5 đơn trống mẫu, schema sinh ra đúng trường; 3 đơn ban đầu chuyển sang schema tự sinh |
| **P5 — Gia cố** | Bộ test prompt injection (CV chứa lệnh ẩn), test giả mạo role, xử lý lỗi API, giám sát refresh token | Toàn bộ test bảo mật pass; lỗi API không để lại dữ liệu lệch |

---

## 13. Giả định và điểm đã chốt

1. Buổi `T` do leader gán **có tính** vào tối thiểu 3 buổi/tuần.
2. Lab có đúng **1 leader** (kiêm quản lý thiết bị) và có thể **nhiều thư ký**. *(đã chốt)*
3. Lịch trực có **14 buổi/tuần** (sáng/chiều từ T2 đến CN). *(đã chốt)*
4. Hạn ghi nội dung công việc: trước 0h chủ nhật. Agent không nhắc việc ghi nội dung công việc, chỉ nhắc đăng ký buổi trực, **một lần lúc 22:00 chủ nhật**. *(đã chốt)*
5. Chỉ member được mượn thiết bị. *(đã chốt)*
6. Họ tên là khoá liên kết giữa các sheet và phải duy nhất trong `Members`. *(đã chốt: lịch trực không có cột email)*
7. MSSV không ép định dạng vì sinh viên đến từ nhiều trường. Email nhận domain `gmail.com` và `vnu.edu.vn`.
8. Mẫu đơn gia nhập cần thêm một câu đồng ý cho lab xử lý dữ liệu cá nhân; đơn bị từ chối được archive sau 6 tháng.
9. Thầy phụ trách (admin) khai báo trong `auth.admins`, không nằm trong `Members`.
10. Member thường không thấy SĐT của thành viên khác qua chatbot; chỉ leader, thư ký, admin thấy. Cột Address không bao giờ được trả ra.
11. Dữ liệu hiện tại là dữ liệu test (một email chung, thông tin giả); `test_mode` phải tắt khi chạy thật.
12. Nội dung `kb/` sẽ được crawl từ website lab.

---

## 14. Lịch sử thay đổi

### Thay đổi lần 1 (2026-10-15, sau khi kiểm tra Drive thật)

| Mục | Thay đổi |
|---|---|
| 3.1 Members | Thêm `mssv`, `lop`; header ở dòng 1; nhiều thư kí; leader kiêm quản lý thiết bị; thầy phụ trách không nằm trong sheet |
| 3.2 Equipment | `ThietBi` giữ thêm `ngay_kiem_tra`, `hinh_anh`; `LichSuMuon` thêm `ma_phieu`, mỗi thiết bị một dòng; chỉ member được mượn |
| 3.3 DutySchedule | Theo bố cục thật của lab: header dòng 4–5, dòng 6 là thầy phụ trách, thêm cột email ẩn; hạn ghi công việc trước 0h chủ nhật |
| 7.1, 7.7 | Thêm kiểu trường `table`; `find_blanks` bắt thêm dòng chỉ có dấu chấm, dòng "ngày … tháng … năm …", bảng chỉ có hàng tiêu đề |
| 8.2 | Phiếu mượn nhiều thiết bị |
| 8.7 | Đổi role hỗ trợ nhiều thư kí |
| 9, 10 | Nhắc lịch trực một lần lúc 22:00 chủ nhật (bỏ lượt 09:00); thêm `duty.layout` |

### Thay đổi lần 2 (2026-10-15, sau khi lab chỉnh dữ liệu)

| Mục | Thay đổi |
|---|---|
| 1.1 | Hai loại khoá: email (đăng nhập, gửi mail) và họ tên (liên kết giữa các sheet); chế độ test với dev login và mail `[TEST]` |
| 3 | Code đọc/ghi theo **tiêu đề cột**, giữ nguyên bảng và tiêu đề tiếng Việt của lab; ánh xạ trong `config.yaml` |
| 3.1 | Dùng đúng cột của lab (header dòng 3); bỏ cột trạng thái, thay bằng tab `Cựu thành viên` |
| 3.2 | `Thietbi` giữ cột của lab, thêm cột "Người mượn"; số còn lại do code tính. `LichSuMuon` giữ 8 cột, thêm 4 cột ở cuối; bỏ `ma_muon` và cột trạng thái, đang mượn = "ngày trả" trống |
| 3.3 | Không có cột email; dò cột theo tiêu đề; tên tab `dd/mm-dd/mm`; dòng thành viên = No là số và Name không trống |
| 7.1 | Schema 3 đơn viết theo mẫu thật (`schemas/`); thêm kiểu `time`, `member_ref`, `row_number`; thêm `anchor`, `blank: next_line`, `signatures`, `output.format` |
| 7.7 | Luật (c) nhận bảng có hàng trống sau tiêu đề; thêm luật (d) cho bảng chữ ký; cảnh báo khi hai file cùng `form_id` |
| 8.2–8.6 | Ghi `LichSuMuon` theo cột thật; biên bản lấy ngày họp từ dòng ngày đầu biên bản; duyệt đơn ghi vào cột thật của `Members`; rời lab = chuyển tab |
| 9 | Job lịch trực ghép theo họ tên; báo cáo tuần gửi tất cả thư ký và thêm mục "Dữ liệu cần sửa" |
