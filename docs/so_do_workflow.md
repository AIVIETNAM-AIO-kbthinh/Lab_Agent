# Sơ đồ các workflow đã hoàn thành (P0 – P3)

Quy ước màu trong mọi sơ đồ:

- 🟩 **Xanh lá** = đầu vào (input)
- 🟦 **Xanh dương** = đầu ra (output)
- 🟥 **Đỏ** = bị từ chối / dừng
- Hình thoi = điểm rẽ nhánh (kiểm tra điều kiện)

Danh sách:

1. Tổng quan hệ thống
2. Đăng nhập và xác định vai trò
3. Chatbot hỏi đáp (vòng lặp agent)
4. Tra cứu tài liệu KB (RAG)
5. Cập nhật KB từ Google Drive
6. Cập nhật KB từ website lab (crawl)
7. Nộp đơn: thu thập thông tin → bản xem trước
8. Xác nhận và lưu đơn (3 loại đơn)
9. Thao tác cần xác nhận: trả / gia hạn thiết bị, quản lý thành viên
10. Các job chạy định kỳ
11. Publish mẫu đơn lên Drive

---

## 1. Tổng quan hệ thống

```mermaid
flowchart LR
    U(["👤 Người dùng<br/>khách, thành viên, thư ký,<br/>leader, admin"])

    subgraph API["Tiến trình API"]
        WEB["Giao diện chat web"]
        AGENT["Agent<br/>LLM + tools"]
    end

    subgraph WORKER["Tiến trình Worker"]
        OUTBOX["Hàng đợi ghi<br/>outbox"]
        JOBS["Job định kỳ"]
        CRAWL["Crawl web + index KB"]
    end

    LLM["LLM gateway<br/>Claude / Gemini"]
    QD[("Qdrant<br/>tài liệu KB")]
    PG[("Postgres<br/>hội thoại, bản nháp,<br/>outbox, log")]
    GG["Google Workspace<br/>Drive, Sheets, Gmail"]
    SITE["Website lab"]

    U -->|"câu hỏi, file đơn"| WEB
    WEB --> AGENT
    AGENT <-->|"gọi model"| LLM
    AGENT -->|"tìm tài liệu"| QD
    AGENT -->|"đọc Members, thiết bị,<br/>lịch trực"| GG
    AGENT -->|"lưu nháp, yêu cầu ghi"| PG
    PG --> OUTBOX
    OUTBOX -->|"ghi file, sheet, gửi mail"| GG
    JOBS -->|"đọc / ghi sheet, gửi mail"| GG
    CRAWL -->|"đọc kb/ trên Drive"| GG
    CRAWL -->|"tải trang"| SITE
    CRAWL -->|"cập nhật vector"| QD
    WEB -->|"câu trả lời + nút bấm"| U

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    class U,SITE input
```

---

## 2. Đăng nhập và xác định vai trò

```mermaid
flowchart TD
    IN(["Email + mật khẩu<br/>do admin cấp"]) --> RL{"Quá số lần<br/>đăng nhập sai?"}
    RL -- "Có" --> LOCK["Khoá tạm tài khoản"]
    RL -- "Không" --> PW{"Mật khẩu đúng?<br/>băm argon2id"}
    PW -- "Sai" --> FAIL["Báo sai, tăng bộ đếm"]
    PW -- "Đúng" --> SES["Tạo session<br/>cookie HttpOnly + CSRF"]
    NOLOGIN(["Không đăng nhập"]) --> GUEST

    SES --> REQ["Mỗi request:<br/>tra email trong sheet Members"]
    REQ --> A{"Email nằm trong<br/>danh sách admin?"}
    A -- "Có" --> ADMIN["Vai trò: Admin"]
    A -- "Không" --> M{"Có trong Members?"}
    M -- "Không" --> GUEST["Vai trò: Khách"]
    M -- "Có" --> ROLE["Vai trò theo cột role:<br/>Leader / Thư ký / Thành viên"]

    ADMIN --> OUT(["Vai trò quyết định:<br/>model, tool được dùng,<br/>tài liệu được đọc"])
    GUEST --> OUT
    ROLE --> OUT

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class IN,NOLOGIN input
    class OUT output
    class LOCK,FAIL stop
```

---

## 3. Chatbot hỏi đáp (vòng lặp agent)

```mermaid
flowchart TD
    IN(["Câu hỏi của người dùng<br/>+ vai trò"]) --> BUD{"Còn hạn mức<br/>token trong ngày?"}
    BUD -- "Hết" --> DENY["Từ chối nhẹ nhàng<br/>+ cảnh báo admin"]
    BUD -- "Còn" --> SAVE["Lưu tin nhắn vào hội thoại"]
    SAVE --> PICK["Chọn model + prompt theo vai trò<br/>Khách: Gemini Flash Lite<br/>Thành viên trở lên: Claude Haiku"]
    PICK --> CALL["Gửi LLM: lịch sử chat<br/>+ danh sách tool vai trò được dùng"]
    CALL --> ERR{"LLM lỗi?"}
    ERR -- "Có" --> FB["Chuyển sang model<br/>hãng khác, 1 lần"]
    FB --> CALL
    ERR -- "Không" --> NEED{"LLM muốn<br/>gọi tool?"}
    NEED -- "Có" --> TOOL["Chạy tool<br/>kiểm tra quyền lần 2,<br/>kiểm tra lại tham số"]
    TOOL --> BACK["Gửi kết quả tool lại cho LLM<br/>tối đa 8 vòng"]
    BACK --> CALL
    NEED -- "Không" --> OUT(["Câu trả lời stream từng chữ<br/>+ nút bấm do backend sinh<br/>tải mẫu, Xác nhận / Huỷ"])

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class IN input
    class OUT output
    class DENY stop
```

**Tool theo vai trò** (LLM chỉ thấy tool của vai trò mình):

| Nhóm | Tool | Ai dùng được |
|---|---|---|
| Hỏi đáp | `search_kb`, `list_forms` | Tất cả |
| Hỏi đáp | `get_application_status` | Khách |
| Tra cứu lab | `query_equipment`, `query_duty`, `query_members`, `my_borrows` | Thành viên trở lên |
| Duyệt đơn | `list_pending_applications` | Leader, Admin |
| Đơn từ | `get_form_template`, `start_form`, `update_form_fields`, `attach_file`, `submit_filled_form`, `render_preview`, `cancel_form` | Theo quyền từng đơn |
| Thao tác ghi | `return_equipment`, `extend_borrow` | Thành viên, thư ký, leader |
| Thao tác ghi | `request_member_change` | Leader, Admin (đổi vai trò: chỉ Admin) |

---

## 4. Tra cứu tài liệu KB (tool `search_kb`)

```mermaid
flowchart LR
    IN(["Câu hỏi + vai trò"]) --> F["Dựng bộ lọc quyền từ vai trò<br/>Khách: chỉ public<br/>Thành viên: public + member"]
    F --> D["Vector ngữ nghĩa<br/>embedding"]
    F --> S["Vector từ khoá<br/>BM25 tiếng Việt"]
    D --> H["Qdrant: tìm kết hợp<br/>gộp RRF, lấy 30 ứng viên"]
    S --> H
    H --> F2["Lọc quyền lần 2<br/>phòng thủ nhiều lớp"]
    F2 --> RR{"Rerank<br/>chạy được?"}
    RR -- "Có" --> TOP["Rerank, giữ 6 đoạn tốt nhất"]
    RR -- "Không" --> RRF["Giữ thứ tự RRF"]
    TOP --> OUT(["6 đoạn tài liệu<br/>+ tiêu đề, nguồn, ngày"])
    RRF --> OUT

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    class IN input
    class OUT output
```

---

## 5. Cập nhật KB từ Google Drive (job `kb_sync`, `kb_reconcile`)

```mermaid
flowchart TD
    IN1(["Các thay đổi trên Drive<br/>mỗi 15 phút"]) --> FILE
    IN2(["Toàn bộ thư mục kb/<br/>Chủ nhật 23:00"]) --> FILE
    FILE["Xét từng file"] --> DEL{"Bị xoá hoặc<br/>chuyển ra khỏi kb/?"}
    DEL -- "Có" --> RM["Xoá các đoạn cũ<br/>khỏi Qdrant"]
    DEL -- "Không" --> ACC["Gán quyền theo thư mục<br/>kb/public → public<br/>kb/internal → member"]
    ACC --> EXT["Trích văn bản<br/>Google Doc, PDF, DOCX"]
    EXT --> HASH{"Nội dung<br/>có thay đổi?"}
    HASH -- "Không" --> SKIP["Bỏ qua"]
    HASH -- "Có" --> CH["Chia đoạn theo heading<br/>khoảng 700 token<br/>mục có nhãn Nội bộ → member"]
    CH --> EMB["Tạo vector ngữ nghĩa<br/>+ vector từ khoá"]
    EMB --> UP["Ghi đoạn mới vào Qdrant<br/>rồi xoá đoạn cũ"]
    UP --> OUT(["KB đã cập nhật<br/>+ ghi AuditLog"])
    RM --> OUT

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    class IN1,IN2 input
    class OUT output
```

---

## 6. Cập nhật KB từ website lab (job `web_crawl`)

```mermaid
flowchart TD
    IN(["Website lab<br/>nhanh: 02:00 hằng ngày<br/>đầy đủ: 03:00 thứ Hai"]) --> HOME{"Tải được<br/>trang chủ?"}
    HOME -- "Không" --> ABORT["Huỷ lần chạy<br/>ABORTED"]
    HOME -- "Có" --> DISC["Duyệt link cùng tên miền<br/>theo robots.txt, đa ngôn ngữ"]
    DISC --> CMP["So từng trang với lần trước<br/>mới / thay đổi / không đổi / mất"]
    CMP --> PLAN["Lập kế hoạch cập nhật"]
    PLAN --> RATIO{"Thay hoặc xoá<br/>quá 30% số trang?"}
    RATIO -- "Có" --> BLOCK["Chặn lại BLOCKED<br/>chờ admin duyệt<br/>trên trang /admin/kb/web"]
    BLOCK -- "Admin bấm áp dụng" --> APPLY
    RATIO -- "Không" --> APPLY["Áp dụng:<br/>index trang mới và trang đổi<br/>trang mất nhiều lần liên tiếp → xoá"]
    APPLY --> OUT(["KB web đã cập nhật<br/>+ báo cáo lần crawl"])

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class IN input
    class OUT output
    class ABORT,BLOCK stop
```

---

## 7. Nộp đơn: thu thập thông tin → bản xem trước

Áp dụng cho 3 đơn: **đơn xin gia nhập**, **phiếu mượn thiết bị**, **biên bản họp**.

```mermaid
flowchart TD
    INA(["Cách 1: file DOCX/PDF<br/>người dùng tự điền<br/>từ mẫu tải về"]) --> EXA["Trích dữ liệu từ file<br/>1. theo nhãn trong mẫu<br/>2. thiếu thì dùng LLM"]
    INB(["Cách 2: thông tin<br/>gửi qua chat"]) --> EXB["Trợ lý điền hộ<br/>tool start_form, update_form_fields"]
    EXA --> DRAFT["Bản nháp đơn<br/>lưu trong DB"]
    EXB --> DRAFT
    DRAFT --> VAL{"Kiểm tra hợp lệ<br/>trường bắt buộc, định dạng,<br/>ngày, thiết bị còn đủ"}
    VAL -- "Thiếu hoặc sai" --> ASK["Trợ lý hỏi lại<br/>trường còn thiếu"]
    ASK --> EXB
    VAL -- "Hợp lệ" --> REN["Tạo bản xem trước<br/>điền mẫu, xuất PDF<br/>cách 1: giữ nguyên file gốc"]
    REN --> HASH["Gắn mã hash<br/>của đúng bản đang hiển thị"]
    HASH --> OUT(["Thẻ xem trước<br/>+ nút Xác nhận / Huỷ"])

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    class INA,INB input
    class OUT output
```

Thông tin người nộp (họ tên, email…) của thành viên lấy theo tài khoản, không sửa được qua chat.

---

## 8. Xác nhận và lưu đơn

```mermaid
flowchart TD
    IN(["Người dùng bấm Xác nhận<br/>mã bản nháp + hash"]) --> OWN{"Đúng chủ bản nháp<br/>và hash khớp<br/>bản đã xem?"}
    OWN -- "Không, đã sửa sau khi xem" --> REJ["Từ chối nút cũ"]
    OWN -- "Có" --> LOCK{"Đang xử lý<br/>hoặc đã lưu?"}
    LOCK -- "Có, bấm đúp" --> SAME["Trả lại kết quả lần trước<br/>không tạo bản ghi mới"]
    LOCK -- "Không" --> LIVE{"Kiểm tra lại dữ liệu mới nhất<br/>vẫn hợp lệ?"}
    LIVE -- "Không" --> EDIT["Yêu cầu sửa đơn"]
    LIVE -- "Có" --> ID["Sinh mã đơn<br/>phiếu mượn: mã P-YYYYMMDD-NN"]
    ID --> Q["Xếp vào hàng đợi ghi outbox"]

    subgraph W["Worker xử lý tuần tự, từng bước làm lại được"]
        S1["1. Upload file đơn<br/>+ file đính kèm lên Drive"]
        S2["2. Thêm dòng vào sheet<br/>FormSubmissions"]
        S3["3. Bước riêng của từng đơn<br/>xem sơ đồ dưới"]
        S4["4. Ghi AuditLog"]
        S1 --> S2 --> S3 --> S4
    end

    Q --> S1
    S4 --> OUT(["Thông báo đã lưu<br/>+ link file<br/>+ mã phiếu nếu có"])

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class IN input
    class OUT output
    class REJ,EDIT stop
```

**Bước riêng của từng loại đơn:**

```mermaid
flowchart TB
    subgraph A["Đơn xin gia nhập"]
        direction LR
        A1(["Đơn đã xác nhận"]) --> A2["Lưu vào folder<br/>applications/HoTen_SDT"]
        A2 --> A3(["Trạng thái CHO_DUYET<br/>leader thấy trong báo cáo CN"])
    end
    subgraph B["Phiếu mượn thiết bị"]
        direction LR
        B1(["Phiếu đã xác nhận"]) --> B2{"Thiết bị<br/>vẫn còn đủ?"}
        B2 -- "Không" --> B5["Dừng, báo người dùng<br/>sửa phiếu"]
        B2 -- "Có" --> B3["Ghi sheet LichSuMuon<br/>mỗi thiết bị 1 dòng<br/>+ cập nhật cột Người mượn"]
        B3 --> B4(["Mã phiếu + hạn trả"])
    end
    subgraph C["Biên bản họp"]
        direction LR
        C1(["Biên bản đã xác nhận"]) --> C2["Lưu Google Doc vào<br/>kb/internal/meeting-minutes"]
        C2 --> C3(["Index ngay vào KB<br/>thành viên hỏi được luôn"])
    end
    A ~~~ B ~~~ C

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class A1,B1,C1 input
    class A3,B4,C3 output
    class B5 stop
```

Hai người cùng mượn món thiết bị cuối cùng: worker chạy lần lượt nên người sau bị dừng ở bước kiểm tra, chỉ đúng một người thành công.

**Cách worker đảm bảo không mất / không trùng dữ liệu:**

```mermaid
flowchart LR
    IN(["Thao tác trong hàng đợi"]) --> RUN["Chạy từng bước<br/>bỏ qua bước đã xong"]
    RUN --> OK{"Kết quả"}
    OK -- "Xong hết" --> DONE(["DONE"])
    OK -- "Điều kiện nghiệp vụ<br/>không còn đúng" --> PF(["FAILED ngay<br/>không thử lại"])
    OK -- "Lỗi tạm thời<br/>Google, mạng" --> RT["Chờ lâu dần rồi thử lại"]
    RT --> MAX{"Quá số lần<br/>cho phép?"}
    MAX -- "Chưa" --> RUN
    MAX -- "Rồi" --> FL(["FAILED<br/>+ mail báo admin"])

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class IN input
    class DONE output
    class PF,FL stop
```

---

## 9. Thao tác cần xác nhận: trả / gia hạn thiết bị, quản lý thành viên

```mermaid
flowchart TD
    IN(["Yêu cầu qua chat<br/>vd: mình trả máy chiếu,<br/>duyệt đơn của bạn A"]) --> TOOL["Tool tạo yêu cầu<br/>return_equipment, extend_borrow,<br/>request_member_change"]
    TOOL --> PERM{"Vai trò có<br/>quyền không?"}
    PERM -- "Không" --> NO["Từ chối"]
    PERM -- "Có" --> PEND["Tạo yêu cầu chờ xác nhận<br/>có hạn dùng"]
    PEND --> SUM(["Bản tóm tắt<br/>+ nút Xác nhận / Huỷ"])
    SUM -- "Bấm Xác nhận" --> RECHK{"Kiểm tra lại<br/>quyền và hạn dùng"}
    RECHK -- "Không đạt" --> NO
    RECHK -- "Đạt" --> Q["Xếp vào hàng đợi ghi outbox"]
    Q --> KIND{"Loại yêu cầu"}

    KIND --> R["Trả thiết bị<br/>ghi ngày trả + tình trạng<br/>cập nhật thiết bị, người mượn"]
    KIND --> E["Gia hạn<br/>ghi hạn trả mới"]
    KIND --> AP["Duyệt đơn gia nhập<br/>thêm vào Members<br/>trạng thái DA_DUYET<br/>mail chào mừng + mail admin cấp tài khoản"]
    KIND --> RJ["Từ chối đơn<br/>trạng thái TU_CHOI<br/>mail báo ứng viên"]
    KIND --> RM["Cho rời lab<br/>phải trả hết thiết bị trước<br/>chuyển sang cựu thành viên<br/>mail báo admin"]
    KIND --> SR["Đổi vai trò, chỉ Admin<br/>ghi role mới vào Members"]

    R --> OUT(["Kết quả + ghi AuditLog"])
    E --> OUT
    AP --> OUT
    RJ --> OUT
    RM --> OUT
    SR --> OUT

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class IN input
    class SUM,OUT output
    class NO stop
```

| Thao tác | Ai được làm |
|---|---|
| Trả / gia hạn thiết bị | Thành viên, thư ký, leader |
| Duyệt / từ chối đơn, cho rời lab | Leader, Admin |
| Đổi vai trò (leader, thư ký) | Chỉ Admin |

---

## 10. Các job chạy định kỳ

```mermaid
flowchart LR
    subgraph T["Lịch chạy"]
        direction TB
        t1(["Thứ 6, 18:00"])
        t2(["Chủ nhật, 22:00"])
        t3(["Hằng ngày, 08:00"])
        t4(["Chủ nhật, 20:00"])
        t5(["Hằng ngày, 07:30"])
        t6(["Hằng ngày, 03:00"])
        t7(["Ngày 1 hằng tháng"])
    end

    subgraph J["Job + dữ liệu đọc vào"]
        direction TB
        j1["duty_create<br/>đọc Members + lịch trực tuần này"]
        j2["duty_remind<br/>đọc lịch trực tuần sau"]
        j3["equipment_reminder<br/>đọc LichSuMuon"]
        j4["weekly_report<br/>đọc lịch trực, thiết bị, đơn"]
        j5["health<br/>kiểm tra token Google, LLM,<br/>outbox, chi phí, website"]
        j6["draft_cleanup + data_retention<br/>DB: bản nháp, upload, hội thoại cũ"]
        j7["retention<br/>đơn gia nhập bị từ chối quá hạn"]
    end

    subgraph O["Đầu ra"]
        direction TB
        o1(["Tab lịch trực tuần sau<br/>trên Google Sheets"])
        o2(["Mail nhắc người<br/>chưa đủ số buổi trực"])
        o3(["Mail nhắc hạn trả ngày mai<br/>và thiết bị quá hạn"])
        o4(["Mail báo cáo tuần<br/>cho leader + thư ký"])
        o5(["Mail cảnh báo admin<br/>nếu có vấn đề"])
        o6(["Xoá dữ liệu quá hạn"])
        o7(["Chuyển hồ sơ vào _archive/<br/>không xoá cứng"])
    end

    t1 --> j1 --> o1
    t2 --> j2 --> o2
    t3 --> j3 --> o3
    t4 --> j4 --> o4
    t5 --> j5 --> o5
    t6 --> j6 --> o6
    t7 --> j7 --> o7

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    class t1,t2,t3,t4,t5,t6,t7 input
    class o1,o2,o3,o4,o5,o6,o7 output
```

Mọi job đều chạy lại an toàn: mỗi lần gửi mail / tạo tab có khoá chống trùng, chạy lần 2 không gửi lặp. Có thể chạy tay bằng `labagent job run <tên> --dry-run`. Hai job KB (`kb_sync`, `kb_reconcile`) và crawl web nằm ở sơ đồ 5 và 6.

---

## 11. Publish mẫu đơn lên Drive (`labagent forms publish`)

```mermaid
flowchart LR
    IN1(["Mẫu đơn trống gốc<br/>trên Drive"]) --> PH
    IN2(["Schema đơn<br/>schemas/*.yaml"]) --> PH
    PH["Chèn placeholder<br/>vào bản sao của mẫu"] --> TEST{"Tự kiểm tra:<br/>điền dữ liệu mẫu<br/>có đúng không?"}
    TEST -- "Sai" --> STOP["Dừng, không kích hoạt"]
    TEST -- "Đúng" --> UP["Upload template + schema<br/>templates/_system/form/version"]
    UP --> OUT(["Phiên bản mẫu mới<br/>được kích hoạt"])

    classDef input fill:#d4f7d4,stroke:#2e7d32,color:#000
    classDef output fill:#d6e8ff,stroke:#1565c0,color:#000
    classDef stop fill:#ffd6d6,stroke:#c62828,color:#000
    class IN1,IN2 input
    class OUT output
    class STOP stop
```
