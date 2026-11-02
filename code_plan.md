# Lab Agent Platform — Kế hoạch triển khai code (bản 2)

> Ngày lập: 2026-10-16 · Đầu vào: `isilab_agent_design.md` (bản 2026-10-15 lần 2) + góp ý ngày 2026-10-16
> Tài liệu thiết kế trả lời câu hỏi **làm gì**. File này trả lời câu hỏi **code thế nào, theo thứ tự nào, xong khi nào, vận hành production ra sao**. Số mục dạng "TK §x" là mục tương ứng trong tài liệu thiết kế.

**Thay đổi so với bản 1:**
- Chốt model LLM, embedding và rerank qua gateway shopaikey (đã chạy thử với key thật).
- Đăng nhập: tài khoản do admin cấp, không dùng Google.
- Thêm thiết kế crawl website lab và cách xử lý khi web thay đổi.
- Nâng toàn bộ kế hoạch lên chuẩn production, dùng được cho nhiều lab trong trường.
- Đã tạo `.gitignore`, `pyproject.toml`, `uv.lock`, `.env.example` và cài thư viện vào `.venv`.

## Mục lục

1. Hiện trạng repo
2. Nguyên tắc production
3. Các quyết định kỹ thuật
4. LLM, embedding và rerank: lựa chọn và chi phí
5. Knowledge base từ website lab
6. Chỗ lệch hoặc chưa rõ trong tài liệu thiết kế
7. Thiết kế code chi tiết
8. Lộ trình theo giai đoạn (P0–P6)
9. Chiến lược kiểm thử
10. Vận hành production
11. Rủi ro và cách giảm

---

## 1. Hiện trạng repo

| Mục | Trạng thái |
|---|---|
| `.gitignore` | ✅ Đã tạo; chặn `.env`, `secrets/`, `credentials.json`, `token.json`, dữ liệu runtime |
| `pyproject.toml` + `uv.lock` | ✅ Đã tạo; resolve 152 gói, đã `uv sync` vào `.venv` (Python 3.12) |
| `.env.example` | ✅ Đã tạo |
| `requirements.txt` cũ | Đã xoá (thay bằng `pyproject.toml` + `uv.lock`) |
| `credentials.json`, `token.json` | Chưa từng bị commit (đã kiểm tra `git log`). Nên chuyển vào `secrets/` |
| `.env` | Đang dùng `LLM_PROVIDER` / `LLM_API_URL`. Cần đổi theo `.env.example` (mục 4.4) |
| `config.yaml`, `schemas/`, `app/` | Chưa có, sẽ tạo ở P0/P3 |

Cài môi trường trên máy mới: `uv sync` (thêm `--group dev` để có công cụ test/lint; `uv sync` mặc định đã cài nhóm `dev`).

---

## 2. Nguyên tắc production

Sáu nguyên tắc của TK §0 vẫn giữ nguyên. Bổ sung thêm các nguyên tắc dưới đây, áp dụng cho mọi dòng code:

1. **Không gắn cứng thông tin của một lab.** Tên lab, website, Drive root, tiêu đề cột, lịch job, model đều nằm trong `config.yaml`. Thêm một lab mới = thêm một bộ config + secrets, không sửa code.
2. **Config sai thì dừng ngay lúc khởi động.** Có lệnh `labagent preflight` kiểm tra config, quyền Google, đường dẫn Drive, tiêu đề cột, kết nối LLM, DB và Qdrant trước khi chạy thật.
3. **Mọi lời gọi ra ngoài (Google, LLM, web) đều có timeout, retry có giới hạn và log.** Không chờ vô hạn, không retry vô hạn.
4. **Mọi thao tác ghi đều idempotent** và đi qua outbox. Không có thao tác ghi nào "chạy một lần là xong, lỗi thì thôi".
5. **Không tin dữ liệu đầu vào:** chat, file upload, nội dung web crawl và output của LLM đều là dữ liệu không tin cậy.
6. **Chi phí có trần.** Mỗi lượt chat, mỗi user, mỗi ngày và mỗi lab đều có hạn mức token. Vượt hạn mức thì từ chối mềm và cảnh báo admin.
7. **Quan sát được:** log có cấu trúc kèm `request_id` / `op_id`, có metrics và alert. Lỗi phải được biết trước khi user báo.
8. **Dữ liệu cá nhân tối thiểu:** chỉ gửi cho LLM những gì cần cho tác vụ, có thời hạn lưu trữ rõ ràng, không log nội dung CV.
9. **Mọi thay đổi schema DB qua Alembic.** Mọi thay đổi code qua CI (lint, type check, test).

---

## 3. Các quyết định kỹ thuật

| Chủ đề | Quyết định | Lý do |
|---|---|---|
| **Mô hình nhiều lab** | **Mỗi lab một instance** (một bộ container + DB + collection Qdrant + tài khoản Google riêng), dùng chung một Docker image | Dữ liệu các lab tách biệt ngay từ hạ tầng, không có lỗi rò dữ liệu chéo lab. Mỗi lab vốn có tài khoản Google, Drive và bố cục sheet riêng. Nâng cấp hay sự cố của lab này không ảnh hưởng lab khác. Code vẫn không gắn cứng lab nào (nguyên tắc 1), nên sau này chuyển sang multi-tenant được |
| Đóng gói | Docker image + Docker Compose cho mỗi lab; Caddy làm reverse proxy (HTTPS tự động) | Đơn giản, đủ cho quy mô trường |
| Tiến trình | `api` (FastAPI, stateless) và `worker` (outbox + scheduler + crawler + indexer) là **hai tiến trình riêng** | API scale được; job và hàng đợi ghi chỉ chạy đúng một bản |
| Đảm bảo một worker | Postgres advisory lock khi worker khởi động | Lỡ chạy 2 worker thì bản thứ hai chờ, không chạy job trùng |
| DB | **Postgres ở mọi môi trường** (dev chạy bằng Docker); SQLAlchemy 2.1 + psycopg 3 + Alembic | Outbox cần `SELECT ... FOR UPDATE SKIP LOCKED` và advisory lock. Dev giống prod. (Tài liệu thiết kế ghi SQLite cho dev, nay đổi) |
| Vector DB | Qdrant server (container) ở mọi môi trường | |
| Quản lý gói | `uv` + `pyproject.toml` + `uv.lock` | Build tái lập được |
| Web | FastAPI + Uvicorn, async | |
| Config | `pydantic-settings` (`.env`, secrets) + Pydantic model validate `config.yaml` | |
| Google API | `google-api-python-client`, gọi qua `anyio.to_thread`, retry bằng `tenacity` | |
| LLM | Hai adapter sau một interface: Claude qua **SDK `anthropic`, endpoint native `/v1/messages`**; Gemini qua **SDK `openai`, endpoint OpenAI-compatible** (mục 4) | Claude giữ đủ strict tool schema và prompt caching. Gemini cho guest giá rẻ. Hai hãng làm fallback cho nhau. Chuyển sang API chính hãng chỉ cần đổi `base_url` + key |
| Embedding / rerank | SDK `openai` (embedding) và `httpx` (rerank) qua endpoint OpenAI-compatible của gateway | |
| Sparse/BM25 | Tự sinh sparse vector (tách âm tiết tiếng Việt + bigram, hash) + Qdrant modifier IDF | Không phụ thuộc stemmer tiếng Anh, không cần mô hình nặng |
| Render | `docxtpl` + LibreOffice headless **trong container Linux của worker** | Tránh các vấn đề của LibreOffice trên Windows. Khi dev trên Windows thì khai `render.soffice_path` |
| Scheduler | APScheduler 3.11 trong tiến trình `worker`, múi giờ `Asia/Ho_Chi_Minh` | |
| **Đăng nhập** | **Email + mật khẩu do admin cấp** (đã chốt). Băm argon2id; session lưu trong DB (cookie ngẫu nhiên, HttpOnly, Secure, SameSite=Lax) + CSRF token | Không dùng Google Sign-In |
| UI | HTML + JS thuần (hoặc HTMX) do FastAPI phục vụ; stream câu trả lời qua SSE | Ít phụ thuộc, dễ bảo trì |
| Log / metrics / lỗi | `structlog` (JSON), `prometheus-client` (`/metrics`), `sentry-sdk` (tuỳ chọn) | |
| Lint / test | `ruff`, `mypy --strict`, `pytest`, `testcontainers` (Postgres), `respx` (mock HTTP), `time-machine` | |
| CI | GitHub Actions: ruff → mypy → pytest (có Postgres) → `pip-audit` → build image | |

**Cây thư mục hạ tầng bổ sung:**

```
deploy/
├── Dockerfile                 # multi-stage; image worker có LibreOffice + font tiếng Việt
├── compose.yaml               # api, worker, postgres, qdrant, caddy
├── compose.dev.yaml           # chỉ postgres + qdrant cho dev
├── Caddyfile
└── labs/<lab_id>/             # config.yaml + .env của từng lab (KHÔNG commit secrets)
.github/workflows/ci.yml
.pre-commit-config.yaml
docs/runbooks/                 # onboarding lab mới, xoay vòng token, khôi phục backup, sự cố
```

---

## 4. LLM, embedding và rerank: lựa chọn và chi phí

### 4.1 Gateway shopaikey: kết quả kiểm tra (2026-10-16)

| Kiểm tra | Kết quả |
|---|---|
| `GET /v1/models` với key của bạn | 182 model, có các dòng Claude 4.5 → 5.5, GPT, Gemini, Qwen, DeepSeek... **Không có `claude-haiku-5-5`** |
| Claude qua `/v1/messages` (native) + tool `strict` | ✅ `claude-sonnet-5-5` và `claude-haiku-4-5-20251001` đều gọi tool đúng, tham số tiếng Việt chuẩn |
| `text-embedding-3-large` (`dimensions=1024`) | ✅ |
| `gemini-embedding-001` | ❌ Lỗi 400 khi gọi qua endpoint OpenAI-compatible của gateway |
| `qwen3-rerank` qua `/v1/rerank` | ✅ Điểm liên quan hợp lý cho câu tiếng Việt |
| Giá | Phụ thuộc **nhóm (group)** của key: cùng một model có giá ×0,8 đến ×2. Các bảng bên dưới dùng mức giá thấp nhất được niêm yết |

**So sánh 6 model** (2026-10-16). Cùng system prompt, 5 tool, 5 câu hỏi nghiệp vụ tiếng Việt; chấm việc chọn đúng tool và đúng tham số:

| Model | Giá vào/ra (USD/1M) | Đúng | Trễ TB | Trễ max | Ghi chú |
|---|---|---|---|---|---|
| `claude-sonnet-5-5` | 2 / 10 | 5/5 | 17,6 s | 49 s | Đo riêng bằng stream: thường 3–7 s, nhưng có đỉnh 25–49 s **kể cả khi tắt thinking**, nên chậm là do kênh gateway |
| `claude-haiku-4-5-20251001` | 1 / 5 | 5/5 | **1,6 s** | 1,8 s | Token đầu tiên sau ~0,5 s, ổn định |
| `gemini-3.1-flash-lite` | **0,4 / 2,4** | 5/5 | 1,9 s | 2,6 s | Rẻ nhất trong nhóm đạt 5/5 |
| `gemini-2.5-flash` | 0,64 / 5,34 | 5/5 | 2,2 s | 2,8 s | Thế hệ cũ |
| `gemini-3-flash-preview` | 1,07 / 6,41 | 4/5 | 3,9 s | 5,2 s | Câu "muốn mượn Arduino" thì tra thiết bị trước thay vì mở phiếu (không hẳn sai) |
| `gemini-3.8-flash` | 2,4 / 14,4 | 5/5 | 7,1 s | 14 s | **Đắt hơn Sonnet 5.5** trên gateway này; các bản 3.5–3.7 Flash cũng vậy |

**Cách hiểu kết quả:**
- 5 câu này chỉ kiểm tra việc *chọn tool*, phần dễ nhất. Chưa kiểm tra điền đơn nhiều lượt, trích nhiều trường cùng lúc, hỏi gộp các trường thiếu, hay chống prompt injection. Model nhỏ thường kém ở chính những phần đó.
- Code validate được *định dạng*, nhưng không phát hiện được lỗi *ngữ nghĩa* (ví dụ điền nhầm ngày mượn vào ngày trả). Phần này dựa vào bước xem trước + xác nhận và bộ eval.
- Vì vậy lựa chọn dưới đây là **mặc định ban đầu**. Bộ eval ở P2 (hỏi đáp) và P3 (điền đơn) sẽ quyết định cuối cùng; đổi model chỉ cần sửa config.

### 4.2 Model đã chọn

| Vai trò | Model mặc định | Giá vào/ra | Lý do |
|---|---|---|---|
| `member_agent`: chat member, điền đơn, nghiệp vụ | `claude-haiku-4-5-20251001` | 1 / 5 | Nhanh (~1,6 s) và ổn định; tool `strict` chạy native; 5/5. Sonnet 5.5 hiện quá chậm qua gateway cho chat tương tác |
| `guest_agent`: chat guest, đơn gia nhập | `gemini-3.1-flash-lite` | 0,4 / 2,4 | Rẻ nhất mà vẫn 5/5 và nhanh. Guest không đăng nhập nên chi phí bị lạm dụng là rủi ro chính; guest chỉ hỏi KB public và một loại đơn |
| `extract`: trích trường từ file đơn user tự điền | `claude-sonnet-5-5`, effort `low` | 2 / 10 | Cần chính xác nhất; tần suất thấp nên chấp nhận chậm hơn |
| `schema_gen`: sinh schema từ đơn trống (P4) | `claude-opus-5-5`, effort `high` | 4 / 20 | Vài lần mỗi năm; chất lượng là ưu tiên |
| Fallback **khác hãng** | Claude lỗi → `gemini-2.5-flash`; Gemini lỗi → `claude-haiku-4-5-20251001` | | Kênh Claude hoặc Gemini trên gateway sập thì vẫn phục vụ được |
| Nâng cấp khi eval yêu cầu | `member_agent` → `claude-sonnet-5-5` | 2 / 10 | Nếu eval P3 cho thấy Haiku điền đơn chưa đạt ngưỡng |
| Embedding | `text-embedding-3-large`, 1024 chiều | 0,156 | Xem mục 4.2.1 |
| Rerank | `qwen3-rerank` | 0,6 | Đa ngôn ngữ, đã chạy thử được |

Hệ quả kỹ thuật: lớp `app/llm/` có **hai adapter** sau một interface chung: `AnthropicAdapter` (native `/v1/messages`) và `OpenAICompatAdapter` (`/v1/chat/completions`, cho Gemini). Interface chuẩn hoá tool call, kết quả tool, usage và stop reason. Với Gemini không có chế độ `strict`, nên **tham số tool luôn được validate lại bằng Pydantic** trước khi chạy; sai thì trả `tool_result` lỗi để model gọi lại. Bộ eval chạy cho cả hai adapter.

#### 4.2.1 Có nên dùng Gemini cho embedding không

| Model | Giá / 1M token | Trạng thái qua gateway |
|---|---|---|
| `text-embedding-3-small` | 0,024 | ✅ chạy được, yếu hơn với tiếng Việt |
| `text-embedding-3-large` (chọn) | 0,156 | ✅ chạy được |
| `gemini-embedding-001` | 0,36 | ❌ lỗi 400 |
| `gemini-embedding-2-preview` | 0,48 | Chưa thử, đang preview |

Không nên. Gemini embedding **đắt hơn** trên gateway này và đang lỗi. Hơn nữa toàn bộ KB của một lab chỉ cỡ dưới 1M token, nên index lại toàn bộ bằng `text-embedding-3-large` tốn chưa tới 0,2 USD. Chất lượng quan trọng hơn vài cent. Bộ eval P2 có thể so thêm `text-embedding-3-small` nếu muốn.

**Không chọn cho agent:**
- Gemini 3.5–3.8 Flash: đắt hơn Sonnet 5.5 trên gateway này và chậm hơn Haiku.
- DeepSeek/Qwen chat: gọi tool kém ổn định hơn với tiếng Việt.

> **Đổi model embedding = phải index lại toàn bộ.** Vì vậy collection Qdrant được đặt tên theo model và số chiều (`kb_te3l_1024`), và có lệnh `labagent reindex --all` để chuyển đổi không gây gián đoạn (index vào collection mới rồi mới đổi alias).

### 4.3 Ước tính chi phí

Giả định một lượt chat gồm ~2 lời gọi LLM, mỗi lời gọi ~6k token vào (system + tool + kết quả tool/RAG) và ~400 token ra. Chưa tính giảm giá nhờ cache:

| Model cho agent | 1 lượt | 1.000 lượt/tháng |
|---|---|---|
| `claude-sonnet-5-5` | ~0,032 USD | ~32 USD |
| `claude-haiku-4-5-20251001` (member) | ~0,016 USD | ~16 USD |
| `gemini-2.5-flash` | ~0,012 USD | ~12 USD |
| `gemini-3.1-flash-lite` (guest) | ~0,007 USD | ~7 USD |

Với cấu hình mặc định, một lab 20 người (~800 lượt member + ~200 lượt guest mỗi tháng) tốn khoảng **12–15 USD/tháng**. Chênh lệch giữa các phương án chỉ khoảng 10–20 USD/lab/tháng. Vì vậy **độ trễ và độ tin cậy quan trọng hơn giá** khi chọn model. Job định kỳ không dùng LLM nên không tốn tiền LLM. Phải xác minh gateway có chuyển tiếp prompt caching không (`usage.cache_read_input_tokens > 0`) bằng script `scripts/llm_smoke.py` ở P0. Chi phí thật được ghi vào bảng `llm_usage` và hiện trên metrics.

### 4.4 Cấu hình

**Cấu hình có hai lớp, thứ tự ưu tiên: biến môi trường (`.env`) > `config.yaml` > giá trị mặc định trong code.**

| Đặt ở đâu | Nội dung | Lý do |
|---|---|---|
| `.env` | Bí mật (key, mật khẩu DB, secret key) + **các giá trị hay đổi theo từng lab hoặc từng môi trường**: URL gateway, tên model từng vai trò, model embedding, URL website lab | Đổi nhanh khi triển khai, không cần sửa file YAML |
| `config.yaml` | Cấu hình có cấu trúc: effort và max_tokens từng vai trò, khả năng từng model, budget, include/exclude của crawler, ánh xạ cột sheet, lịch job | Nhiều tầng, nhiều trường, cần comment. File này nằm trong git nên mọi thay đổi đều được review và có lịch sử |

Khi khởi động, `labagent preflight` in ra **giá trị cuối cùng** của từng mục kèm nguồn (`env` / `yaml` / `default`), để luôn biết cấu hình nào đang có hiệu lực.

> Lưu ý production: đổi model qua `.env` thì thay đổi không nằm trong git. Mỗi lần đổi model phải chạy lại bộ eval và ghi vào changelog vận hành của lab.

`.env` (xem `.env.example`):

```
LLM_API_KEY=<key shopaikey>
LLM_ANTHROPIC_BASE_URL=https://api.shopaikey.com
LLM_OPENAI_BASE_URL=https://api.shopaikey.com/v1
LLM_MODEL_MEMBER=claude-haiku-4-5-20251001
LLM_MODEL_GUEST=gemini-3.1-flash-lite
LLM_MODEL_EXTRACT=claude-sonnet-5-5
LLM_MODEL_SCHEMA_GEN=claude-opus-5-5
LLM_FALLBACK_CLAUDE=gemini-2.5-flash
LLM_FALLBACK_OPENAI_COMPAT=claude-haiku-4-5-20251001
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_DIMENSIONS=1024
RERANK_MODEL=qwen3-rerank
LAB_WEBSITE_URL=http://isilab.click/
```

`config.yaml`:

```yaml
llm:
  request_timeout_s: 60
  first_token_timeout_s: 15        # quá thời gian chưa có token đầu tiên → huỷ, chuyển fallback
  max_tool_iterations: 8
  profiles:                        # model lấy từ env nếu có (LLM_MODEL_*)
    member_agent: {model: claude-haiku-4-5-20251001, max_tokens: 4000}
    guest_agent:  {model: gemini-3.1-flash-lite, max_tokens: 2000}
    extract:      {model: claude-sonnet-5-5, effort: low, max_tokens: 4000}
    schema_gen:   {model: claude-opus-5-5, effort: high, max_tokens: 16000}
  model_capabilities:              # chọn adapter + tham số được phép gửi
    claude-sonnet-5-5:         {adapter: anthropic, effort: true,  strict_tools: true}
    claude-opus-5-5:           {adapter: anthropic, effort: true,  strict_tools: true}
    claude-haiku-4-5-20251001: {adapter: anthropic, effort: false, strict_tools: true}
    gemini-3.1-flash-lite:     {adapter: openai_compat, strict_tools: false}
    gemini-2.5-flash:          {adapter: openai_compat, strict_tools: false}
  budgets:
    max_input_chars_per_message: 4000
    guest_tokens_per_session_per_day: 60000
    member_tokens_per_user_per_day: 400000
    lab_usd_per_month_alert: 30
    lab_usd_per_month_hard_stop: 60
embedding: {model: text-embedding-3-large, dimensions: 1024, batch_size: 64}
rerank:    {enabled: true, model: qwen3-rerank, candidates: 30, top_n: 6}
```

**Cách lấy các giá trị bí mật trong `.env`:**

| Biến | Lấy từ đâu |
|---|---|
| `APP_SECRET_KEY` | **Tự sinh**, không ai cấp: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Dùng để ký CSRF token và link tải mẫu đơn. Mỗi môi trường và mỗi lab một key riêng; đổi key thì mọi session và link đã phát bị vô hiệu |
| `QDRANT_API_KEY` | **Tự sinh** như trên (`token_urlsafe(32)`). Docker Compose truyền cùng giá trị vào container Qdrant (`QDRANT__SERVICE__API_KEY`), app dùng nó để gọi Qdrant. Nếu dùng Qdrant Cloud thì lấy key trong dashboard của cluster |
| Mật khẩu trong `DATABASE_URL` | Tự đặt. Compose truyền vào container Postgres (`POSTGRES_PASSWORD`) |
| `LLM_API_KEY` | Dashboard shopaikey |
| `SENTRY_DSN` | Tuỳ chọn; tạo project trên sentry.io (hoặc Sentry tự host) rồi lấy DSN |
| `GOOGLE_*_FILE` | `credentials.json` tải từ Google Cloud Console; `token.json` sinh bằng `scripts/get_token.py` |

Định dạng `.env`: mỗi dòng `KEY=value`, **không có dấu cách quanh dấu `=`** và **không có chú thích cùng dòng** (chú thích đặt ở dòng riêng). Các parser `.env` khác nhau xử lý những trường hợp này không giống nhau. `preflight` sẽ cảnh báo nếu gặp.

### 4.5 Lưu ý về gateway trung gian (nên đọc trước khi chạy thật)

shopaikey là bên bán lại API, không phải Anthropic/OpenAI. Với sản phẩm production dùng cho nhiều lab, cần cân nhắc:

- **Dữ liệu cá nhân đi qua bên thứ ba.** CV, SĐT, email, nội dung biên bản đều đi qua máy chủ của gateway. Câu đồng ý xử lý dữ liệu trong đơn (TK §8.1 bước 7) cần ghi rõ dữ liệu được xử lý bởi dịch vụ AI bên ngoài.
- **Chọn nhóm (group) chính thống.** Nên dùng nhóm "Claude & Claude Code API" (×1). Tránh các nhóm "Reverse" hoặc nhóm dành riêng cho Claude Code: các kênh này thường kém ổn định và có thể bị ngắt bất cứ lúc nào.
- **Thiết kế không phụ thuộc gateway.** Lớp `app/llm/` chỉ dùng chuẩn native Anthropic và chuẩn OpenAI embeddings. Chuyển sang API chính hãng của Anthropic chỉ cần đổi `LLM_ANTHROPIC_BASE_URL` và key. Riêng embedding phải giữ cùng model, nếu đổi model thì index lại.
- **Tính năng có thể không được chuyển tiếp:** prompt caching, structured outputs, tham số beta. Script smoke test ở P0 kiểm tra từng tính năng; code phải chạy được (chậm hơn hoặc đắt hơn) khi tính năng đó không có.

---

## 5. Knowledge base từ website lab

### 5.1 Đặc điểm `http://isilab.click/` (khảo sát 2026-10-16)

| Đặc điểm | Hệ quả cho crawler |
|---|---|
| Web tự viết, chạy IIS/Plesk, HTML render phía server | Đọc HTML trực tiếp, không cần trình duyệt headless |
| **Chỉ có HTTP.** HTTPS trả chứng chỉ của `tenten.vn` (sai tên miền) | Crawl bằng `http://`. Không tắt kiểm tra TLS toàn cục. Báo lab cấu hình HTTPS |
| Không có `robots.txt`, `sitemap.xml` | Phải tự khám phá link (BFS) |
| `Cache-Control: no-store`, không có `ETag` hay `Last-Modified` | Không dùng conditional GET được. **Phát hiện thay đổi bằng hash nội dung chính đã chuẩn hoá** |
| Song ngữ: `?lang=vi` / `?lang=en`, ngôn ngữ lưu vào cookie `isilab-lang` | Mỗi ngôn ngữ dùng một cookie jar riêng (hoặc không lưu cookie) và luôn gắn `?lang=` vào URL, để không lẫn ngôn ngữ |
| Các mục: `/research`, `/members`, `/projects/*`, `/publications`, `/news/*`, `/contact`, `/gallery`, `/admin/login` | Include các mục nội dung; exclude `/admin`, `/public/` (asset), `/gallery` (chủ yếu ảnh) |
| Tin tức có ngày đăng ("Oct 15, 2026") | Lấy làm `doc_date` cho trang tin |
| Một số số liệu do JS đếm (hiện "0 Members") | Bỏ các khối số liệu động khi trích nội dung |

### 5.2 Lưu ở đâu: **lưu trong backend, không đưa vào Google Drive**

| Tiêu chí | Đưa vào Drive (`kb/public/web/`) | **Lưu trong backend (chọn)** |
|---|---|---|
| Nguồn sự thật | Có hai bản (web + Drive). Ai đó sửa bản trên Drive thì lần crawl sau ghi đè mất, hoặc hai bản mâu thuẫn nhau | **Một nguồn duy nhất là website.** Muốn sửa nội dung thì sửa web |
| Phát hiện thay đổi | Crawl → ghi Drive → Drive changes → reindex: hai vòng, chậm, dễ lỗi | Crawl → so hash → reindex trực tiếp |
| Metadata (URL, ngôn ngữ, lần thấy cuối, số lần mất) | Không có chỗ lưu tự nhiên | Bảng `web_pages` trong Postgres |
| Quota và nhiễu trên Drive | Hàng trăm file, revision history nhiễu | Không ảnh hưởng Drive |
| Nhìn thấy nội dung đã crawl | Mở Drive xem được | Trang admin `/admin/kb/web` (danh sách trang, lần crawl cuối, trạng thái) + lệnh CLI |

Drive vẫn là nơi cho **tài liệu do người viết mà web không có** (nội quy chi tiết, biên bản họp, tài liệu nội bộ), đúng như `kb/public/` và `kb/internal/` trong TK §2.1.

**Khi web và Drive mâu thuẫn:** cả hai đều được index, mỗi chunk có `source` (`web` / `drive`) và `updated_at`. Prompt yêu cầu trích nguồn và ưu tiên tài liệu mới hơn. Nếu lab muốn một tài liệu Drive luôn thắng, đặt `priority: high` trong metadata thư mục (cấu hình được).

### 5.3 Cách crawl và xử lý khi web cập nhật

**Lưu trữ:**
- Bảng `web_pages`: url_canonical, lang, title, content_hash, doc_date, status, first_seen, last_fetched, last_changed, missing_count, chunk_count.
- Bảng `web_crawl_runs`: thống kê mỗi lần chạy.
- HTML gốc của **5 phiên bản gần nhất** mỗi trang lưu trong `var/web_snapshots/` (gzip) để gỡ lỗi và khôi phục.

**Quy trình mỗi lần chạy** (`app/rag/crawler.py`, job `web_crawl`):

1. **Khám phá:** BFS từ các URL gốc trong config (`/`, mỗi mục chính, cho cả `vi` và `en`). Chỉ cùng host, lọc theo include/exclude regex. Chuẩn hoá URL (bỏ fragment, sắp xếp query, chỉ giữ tham số `lang`). Giới hạn `max_pages` và `max_depth`.
2. **Tải trang:** 1 request/giây, timeout 20 giây, retry 3 lần cho lỗi mạng/5xx. User-Agent ghi rõ tên bot + email admin.
3. **Trích nội dung chính:** `trafilatura` (bỏ menu, footer, khối số liệu động), chuẩn hoá Unicode NFC và khoảng trắng. Lấy `title`, `doc_date` (trang tin), `lang`.
4. **Phát hiện "soft 404":** trang trả 200 nhưng nội dung quá ngắn hoặc chứa mẫu "not found" thì coi như không tồn tại.
5. **So sánh hash nội dung đã chuẩn hoá** với `web_pages.content_hash`:
   - **Trang mới** → index.
   - **Hash đổi** → xoá chunk cũ theo `doc_id` (hash của URL + lang) rồi chèn chunk mới. Lưu snapshot.
   - **Hash không đổi** → chỉ cập nhật `last_fetched`.
   - **404/410, soft 404, hoặc không còn được khám phá thấy** → tăng `missing_count`. **Chỉ xoá chunk khi `missing_count >= 2`** (hai lần chạy liên tiếp), tránh xoá nhầm vì lỗi tạm thời.
   - **Lỗi mạng / 5xx / timeout** → **giữ nguyên index**, không tăng `missing_count`.
6. **Chốt an toàn:**
   - Nếu một lần chạy định xoá hoặc thay **> 30%** số trang (web đổi giao diện, web sập trả trang trống) thì **dừng, không áp dụng**, và mail admin kèm danh sách. Admin xác nhận bằng `labagent crawl --apply-run <id>`.
   - Nếu không tải được trang chủ thì huỷ cả lần chạy và giữ index cũ. Web lỗi quá 3 ngày liên tiếp thì mail admin.
7. **Mọi thay đổi index ghi vào AuditLog** (số trang mới / đổi / xoá).

**Tần suất:**

| Lịch | Việc làm |
|---|---|
| Hằng ngày 02:00 | **Crawl nhanh:** tải lại các trang danh sách (`/news`, `/projects`, `/publications`, `/members`) để phát hiện mục mới, cộng các trang đã biết có `last_changed` trong 30 ngày gần nhất |
| Thứ 2 03:00 | **Crawl đầy đủ:** toàn bộ trang, bao gồm việc phát hiện trang bị xoá |
| Khi cần | Admin bấm "Cập nhật ngay" trên trang admin hoặc chạy `labagent crawl --now` (ví dụ ngay sau khi lab đăng tin mới) |

**Cải tiến nên đề xuất với người phụ trách website** (web tự viết nên sửa được), để crawler nhanh và chính xác hơn:
1. Thêm `sitemap.xml` có `<lastmod>` cho mỗi trang.
2. Bật HTTPS đúng chứng chỉ.
3. Thêm `<meta property="article:published_time">` cho tin tức.

Khi có sitemap, crawler dùng nó trước và chỉ tải trang có `lastmod` mới (code hỗ trợ sẵn, bật trong config).

**Cấu hình:**

```yaml
lab:
  id: isilab
  name: "ISI Lab - Intelligent System and IoT Laboratory"
  website: "http://isilab.click/"

web_sources:
  - id: isilab_site
    base_url: "http://isilab.click/"
    langs: {param: lang, values: [vi, en]}
    seeds: ["/", "/research", "/members", "/projects", "/publications", "/news", "/contact"]
    include: ['^/$', '^/(research|members|projects|publications|news|contact)(/.*)?$']
    exclude: ['^/admin', '^/public/', '^/gallery']
    max_pages: 400
    max_depth: 4
    rate_per_sec: 1
    sitemap: null                      # đặt URL khi web có sitemap
    min_content_chars: 200
    delete_after_missing_runs: 2
    max_change_ratio: 0.30
    access: public
    schedule: {quick: "0 2 * * *", full: "0 3 * * MON"}
```

---

## 6. Chỗ lệch hoặc chưa rõ trong tài liệu thiết kế

| # | Vấn đề | Đề xuất (sẽ làm nếu không có chỉ đạo khác) |
|---|---|---|
| 1 | Khoá idempotent của `duty_remind` là "tuần + email". Ở `test_mode` mọi người dùng chung email, nên chỉ người đầu tiên được nhắc | Đổi khoá thành **tuần + họ tên chuẩn hoá** |
| 2 | Đơn gia nhập dùng `lop_khoa`, `khoa_truong` (TK §8.5), còn `Members` dùng `lop`, `truong` | Bảng ánh xạ tường minh trong hook `application` |
| 3 | ~~Cơ chế đăng nhập~~ | **Đã chốt:** tài khoản + mật khẩu do admin cấp (mục 3, 7.12) |
| 4 | `shown_hash` = hash file PDF, mà LibreOffice ghi thời điểm tạo vào PDF nên render lại ra hash khác | Lưu bytes đã render trong draft; xác nhận và upload đúng bytes đó, không render lại |
| 5 | Endpoint xác nhận phải trả `ma_phieu` ngay, nhưng việc ghi đi qua hàng đợi | Sinh `ma_phieu` trong DB trước khi xếp hàng; endpoint chờ outbox tối đa 30 giây |
| 6 | Số thứ tự `NN` trong `ma_phieu` | `max(sequence DB, mã lớn nhất trong ngày trên LichSuMuon) + 1` |
| 7 | Tên tab lịch trực chứa `/` | Luôn bọc tên tab trong dấu nháy đơn trong A1: `'12/10-18/10'!A1` |
| 8 | Ô ngày trên Sheet có thể là chuỗi hoặc số serial | Đọc `UNFORMATTED_VALUE` + `SERIAL_NUMBER`, parse cả hai; ghi `USER_ENTERED` dạng `dd/mm/yyyy` |
| 9 | Lịch sử chat lưu bao lâu | Append-only, xoá sau **30 ngày**; không log nội dung file upload |
| 10 | Trang duyệt template (P4) | `/admin/templates/...` yêu cầu đăng nhập admin |
| 11 | TK §6.2/§13.12: "nội dung `kb/` sẽ được crawl từ website" | **Đổi:** nội dung web lưu trong backend, không vào `kb/` (mục 5.2) |
| 12 | TK §4.1: SQLite khi dev | **Đổi:** Postgres ở mọi môi trường (mục 3) |
| 13 | Tên folder gốc `isilab/` gắn cứng trong tài liệu | Code chỉ dùng `drive.root_folder_id`; tên folder tuỳ lab |

---

## 7. Thiết kế code chi tiết

### 7.1 Cây thư mục

```
Lab_Agent/
├── app/
│   ├── main.py                     # FastAPI app factory (tiến trình api)
│   ├── worker.py                   # tiến trình worker: advisory lock → outbox + scheduler
│   ├── cli.py                      # `labagent ...`: preflight, account, jobs, crawl, reindex, smoke
│   ├── config.py                   # Settings (.env) + AppConfig (config.yaml), validate khi khởi động
│   ├── clock.py                    # now()/today() theo Asia/Ho_Chi_Minh, tiêm được khi test
│   ├── logging.py  metrics.py  errors.py
│   ├── db/                         # models.py, session.py, migrations/ (Alembic)
│   ├── google/                     # oauth.py, retry.py, drive.py, sheets.py, docs.py, gmail.py, paths.py
│   ├── sheets/                     # table.py (đọc/ghi theo tiêu đề), a1.py
│   ├── domain/                     # names.py, dates.py, members.py, equipment.py, duty.py, submissions.py, audit.py
│   ├── mail/                       # mailer.py (test_mode), templates.py
│   ├── auth/                       # passwords.py, sessions.py, accounts.py, roles.py, csrf.py, ratelimit.py, deps.py
│   ├── llm/
│   │   ├── client.py               # bọc anthropic SDK: profile → tham số theo khả năng model, retry, fallback
│   │   ├── usage.py                # ghi llm_usage, kiểm tra budget
│   │   ├── embeddings.py  rerank.py
│   ├── tools/                      # registry.py + kb.py, equipment.py, duty.py, members.py, forms.py, admin.py, mcp_adapter.py
│   ├── agents/                     # loop.py, router.py, conversation.py, prompts/{guest,member}.md
│   ├── forms/                      # schema.py, registry.py, validators.py, drafts.py, render.py, engine.py, extract.py, schema_gen/
│   ├── hooks/                      # base.py, application.py, equipment_borrow.py, meeting_minutes.py
│   ├── workflows/                  # return_equipment.py, extend_borrow.py, member_changes.py
│   ├── rag/                        # extract_text.py, chunker.py, sparse.py, store.py, indexer.py, retriever.py, crawler.py
│   ├── outbox/                     # models.py, worker.py, steps.py
│   ├── jobs/                       # scheduler.py, idempotency.py, duty.py, equipment.py, weekly_report.py,
│   │                               #   kb_sync.py, web_crawl.py, cleanup.py, retention.py, health.py
│   ├── api/                        # auth.py, chat.py, uploads.py, drafts.py, actions.py, admin.py, health.py
│   └── web/                        # static/ (index.html, app.js, style.css), admin/
├── schemas/  email_templates/  scripts/  docs/  deploy/  .github/
├── tests/
│   ├── fakes/                      # FakeSheets, FakeDrive, FakeGmail, FakeLLM, FakeWeb
│   ├── fixtures/                   # sheet JSON (ẩn danh), mẫu đơn docx, HTML trang web mẫu
│   ├── unit/  integration/  security/  eval/
├── config.example.yaml  .env.example  pyproject.toml  uv.lock  README.md
```

### 7.2 Lớp Google client

- **`oauth.py`**: đọc token từ `GOOGLE_TOKEN_FILE`, refresh rồi ghi lại (ghi qua file tạm + rename để không hỏng file). Lỗi `invalid_grant` → health đỏ + alert. Job `health` chạy hằng ngày.
- **`retry.py`**: `tenacity` retry cho HttpError 429/500/502/503/504 và lỗi mạng, backoff mũ có jitter, tối đa 5 lần. 4xx khác ném ra ngay. Mọi request đặt timeout.
- **Client chỉ có các method mà TK §4.3 cần.** Domain không gọi `googleapiclient` trực tiếp, nhờ đó thay được bằng bản giả khi test.
- **`paths.py`**: `resolve("data/Members") -> file_id` đi từ `drive.root_folder_id`, cache trong DB, gặp 404 thì tra lại. Có thêm `ensure_folder(path)`.

### 7.3 `SheetTable`: đọc và ghi theo tiêu đề cột

```python
class SheetTable:
    async def load(self) -> None                      # đọc tab, dựng header_map; thiếu tiêu đề → HeaderMissing
    def rows(self) -> list[Row]                       # dict theo tên logic + row_number
    async def append(self, values: dict[str, Any]) -> int
    async def update(self, row_number: int, values: dict[str, Any]) -> None   # ghi từng ô, không ghi đè dòng
```

- So khớp tiêu đề sau khi chuẩn hoá NFC, gộp khoảng trắng và `casefold`.
- Cột không có trong config (ví dụ `Address`) không bao giờ đọc vào `Row`.
- Trước khi ghi, tìm lại dòng theo khoá nghiệp vụ, vì có thể có người vừa sửa tay trên sheet.

### 7.4 Domain

| Module | Hàm chính |
|---|---|
| `names.py` | `norm_name`, `to_pascal_ascii` (xử lý `đ`), `normalize_phone`, `valid_phone`, `valid_email`, `fuzzy_match` (rapidfuzz trên chuỗi bỏ dấu) |
| `dates.py` | `parse_sheet_date`, `fmt_date`, `week_of`, `tab_name`, `parse_tab_name` (suy năm), `resolve_time_expr("tuần trước", today)` |
| `members.py` | `list_active`, `by_email`, `by_name`, `leader`, `secretaries`, `assert_unique_names`, `append_member`, `move_to_former`, `set_role` |
| `equipment.py` | `availability(ma_tb)`, `active_borrows(ho_ten?)`, `overdue(today)`, `render_nguoi_muon_cell`, `append_borrow_rows`, `mark_returned`, `split_partial_return`, `extend` |
| `duty.py` | `DutyTab.load` (dò cột theo dòng 4–5), `member_rows`, `count_sessions`, `fixed_marks`, `create_week_tab` |
| `submissions.py` | `ensure_tab`, `append`, `list`, `set_status` |
| `audit.py` | `log(actor, role, action, target, ok, detail)` |

### 7.5 Bảng trong DB

| Bảng | Dùng cho |
|---|---|
| `accounts` (email, password_hash, must_change_password, failed_logins, locked_until, disabled) | Đăng nhập |
| `sessions` (id hash, account_email?, guest, dev_login_name?, ip, user_agent, expires_at) | Session, kể cả guest ẩn danh |
| `conversations`, `messages` (content_json nguyên block, append-only) | Lịch sử chat, xoá sau 30 ngày |
| `uploads` (sha256, mime đã kiểm tra, path, expires_at) | File upload |
| `drafts`, `pending_actions` | Form engine, thay đổi thành viên |
| `outbox_ops`, `outbox_steps` | Ghi nhiều bước |
| `idempotency_keys`, `sequences`, `drive_state`, `drive_path_cache` | Job, mã phiếu, đồng bộ Drive |
| `index_docs` (doc_id, source drive/web, path/url, access, doc_type, doc_date, content_hash, chunk_count) | Đồng bộ index |
| `web_pages`, `web_crawl_runs` | Crawl web (mục 5.3) |
| `form_versions` | Form registry |
| `llm_usage` (ts, session/account, profile, model, input/output/cache tokens, cost_usd, latency_ms, request_id) | Chi phí, budget, metrics |
| `rate_limits` (key, window_start, count) | Giới hạn tần suất dùng chung giữa các bản api |

### 7.6 Outbox và hàng đợi ghi

- Mỗi thao tác nghiệp vụ là một op gồm nhiều step có tên. Mỗi step idempotent theo `(op_id, step)`; step xong ghi `result_json`.
- Worker duy nhất (được bảo đảm bằng advisory lock) lấy op bằng `FOR UPDATE SKIP LOCKED` và chạy tuần tự.
- **Kiểm tra lại dữ liệu sống (ví dụ số thiết bị còn lại) chạy ngay trong worker**, trước step ghi đầu tiên.
- Step append vào sheet tự kiểm tra dòng đã tồn tại chưa (theo `ma_phieu` + `ma_tb` hoặc `submission_id`) trước khi ghi.
- Quá số lần retry: op → `FAILED`, ghi AuditLog `FAILED`, alert admin. Khi khởi động, worker nhận lại các op còn dở.
- Endpoint API chờ kết quả bằng cách poll trạng thái op (hoặc Postgres `LISTEN/NOTIFY`), timeout 30 giây.

### 7.7 Tool registry và phân quyền

- `@tool(roles=..., strict=True)`. Tham số `ctx: UserContext` do backend tiêm vào, **không có trong schema gửi cho LLM**.
- LLM chỉ thấy tool của role mình; khi thực thi vẫn kiểm tra quyền lần nữa. Tool chủ bản nháp kiểm tra `owner_session`.
- `ToolResult` gồm `for_llm` (JSON gọn) và `ui_actions` (nút, PDF, link do backend sinh; **LLM không tự tạo được nút bấm**).
- Nội dung do user hoặc web cung cấp được bọc trong `{"untrusted_content": ...}`.
- Danh sách tool đúng TK §5. Không có tool nào cho xác nhận, kích hoạt template, xoá, chia sẻ file.

### 7.8 Lớp LLM và vòng lặp agent

**`llm/client.py`**: điểm duy nhất gọi LLM.
- Nhận `profile` → tra model → chọn adapter (`anthropic` / `openai_compat`) theo `model_capabilities`. Chỉ gửi `effort` / `thinking` khi model hỗ trợ (ví dụ Haiku 4.5 không nhận `effort`).
- **Interface chung:** `chat(messages, tools, profile) -> LLMResponse(text, tool_calls, stop, usage)`. Lịch sử hội thoại lưu ở dạng trung lập; mỗi adapter tự chuyển sang định dạng của mình. Hội thoại đã chuyển sang model fallback thì **ở lại model đó đến hết hội thoại**, để lịch sử luôn append-only.
- `anthropic.AsyncAnthropic(base_url=LLM_ANTHROPIC_BASE_URL, ...)` và `openai.AsyncOpenAI(base_url=LLM_OPENAI_BASE_URL, ...)`.
- **Bắt lỗi theo từng loại:** 429/5xx/timeout → retry (SDK); 400 → không retry, log `request_id`.
- **Chuyển sang fallback khác hãng một lần**, gắn cờ `degraded`, khi: hết retry, `stop_reason == "refusal"`, hoặc quá `first_token_timeout_s` mà chưa có token đầu tiên. Điều kiện cuối nhằm xử lý các đỉnh trễ 25–49 giây đã đo trên gateway.
- **Trước mỗi lời gọi kiểm tra budget** (`llm/usage.py`); sau lời gọi ghi `llm_usage` (token, cache, chi phí, độ trễ).

**`agents/loop.py`**: tự viết vòng lặp tool-use, stream qua SSE:

```
messages = lịch sử (append-only) + tin nhắn user (cắt theo max_input_chars, kèm ngày hôm nay)
lặp tối đa max_tool_iterations:
    resp = stream(profile, system=[prompt theo role, cache_control], tools=tools_for(role) (thứ tự cố định),
                  tool_choice={"type": "auto"}, messages)
    lưu resp.content NGUYÊN VẸN
    refusal → câu trả lời cố định + log ; max_tokens → báo lỗi mềm
    không phải tool_use → kết thúc
    chạy song song các tool_use (timeout từng tool) → TẤT CẢ tool_result gửi trong MỘT tin nhắn user
    (tool lỗi → is_error=true kèm thông điệp an toàn, không lộ stack trace)
hết số lượt → câu trả lời "mình chưa xử lý xong yêu cầu này" + log
```

**Các ràng buộc khi dùng model Claude 5.x:**
- Không ép gọi tool (`tool_choice` `any`/`tool` bị lỗi 400 trên Sonnet 5.5 / Opus 5.5). Dùng `auto` + `strict: true` + chỉ dẫn trong prompt.
- Lịch sử **chỉ append, không sửa hay xoá lượt cũ** (thinking block gắn với hội thoại). Hội thoại quá dài thì mở hội thoại mới.
- Không prefill câu trả lời assistant.
- **Prompt caching:** system prompt + tool cố định theo role, đặt `cache_control`. Không chèn thời gian hay ID vào system; ngày hôm nay đi trong tin nhắn user.
- Parse `tool_use.input` bằng `json`, không so khớp chuỗi.

**Structured extraction** (`forms/extract.py`, `schema_gen/llm_sweep.py`): dùng `messages.parse()` với Pydantic model. Nếu gateway không hỗ trợ structured output (smoke test ở P0 sẽ cho biết) thì chuyển sang tool `strict` có một tham số object, rồi validate lại bằng Pydantic.

### 7.9 Form engine

Giữ như bản 1: `schema.py` (Pydantic), `registry.py` (bảng `form_versions`), `validators.py` (mỗi type một hàm thuần, `live=True` đọc lại sheet), `drafts.py`, `render.py`, `engine.py` (start / update / attach / submit_filled / preview / cancel / confirm), `hooks/`.

Các điểm production:
- **Upload:** kiểm tra magic bytes (`filetype`), đuôi file, kích thước ≤ 10 MB. Với docx: kiểm tra zip-bomb (tổng kích thước sau giải nén, số entry) và từ chối file có macro hoặc đối tượng nhúng.
- **Render:** profile LibreOffice tạm riêng mỗi lần, semaphore 1–2 tiến trình, timeout 60 giây, kill nếu treo.
- **Confirm:** khoá trạng thái draft (`CONFIRMING`) để chống bấm đúp; so `shown_hash` với bytes đã lưu; sinh `ma_phieu`; enqueue outbox.
- Bản nháp hết hạn sau 24 giờ; file PDF tạm bị xoá cùng.

### 7.10 RAG

- **`extract_text.py`**: Google Doc → export docx → `python-docx` (giữ heading); PDF → `pdfplumber`; web → `trafilatura`.
- **`chunker.py`**: tách theo heading, ~700 token (đếm bằng `tiktoken`), overlap 12%. Mỗi chunk có tiền tố `"<tiêu đề tài liệu> › <tiêu đề mục>"`.
- **`sparse.py`**: chuẩn hoá NFC + lowercase, tách âm tiết, sinh unigram + bigram âm tiết (bắt được từ ghép tiếng Việt như "máy chiếu"), hash về chỉ số. Qdrant tính IDF.
- **`store.py`**: collection `kb_<model>_<dim>` gồm vector `dense` và sparse `bm25`; có payload index cho `access`, `source`, `doc_type`, `doc_date`, `doc_id`, `lang`. Truy cập qua alias để đổi collection không gián đoạn.
- **`retriever.py`**:
  - **Bộ lọc `access` dựng từ role ngay trong retriever**; LLM không truyền được.
  - Prefetch dense 30 + sparse 30, gộp RRF, rerank `qwen3-rerank` lấy top 6.
  - Rerank lỗi thì dùng kết quả RRF (giảm chất lượng, không báo lỗi).
- **`indexer.py`**: `upsert_doc(doc)` (so hash → xoá theo `doc_id` → chèn), `delete_doc`, `reconcile()`. Dùng chung cho nguồn Drive và web.
- **`crawler.py`**: theo mục 5.3.

### 7.11 Job định kỳ

Như bản 1 (TK §9), cộng thêm:
- `web_crawl` (quick/full) thay cho lịch crawl cũ.
- `health` (hằng ngày): refresh token Google, kết nối gateway LLM, số op `FAILED`, chi phí LLM trong tháng so với ngưỡng.
- `data_retention` (hằng ngày): xoá hội thoại > 30 ngày, upload mồ côi, snapshot web cũ.

Mỗi job có `dry_run`, tiêm `today`, dùng khoá idempotent, và chạy `assert_unique_names()` đầu tiên khi cần đọc `Members`. Chạy tay: `labagent job run <tên> --today YYYY-MM-DD [--dry-run]`.

### 7.12 Xác thực và bảo mật web

- **Tài khoản do admin cấp:** `labagent account create --email ... --role-check` sinh mật khẩu tạm và gửi mail. Lần đăng nhập đầu bắt buộc đổi mật khẩu. Admin reset hoặc khoá tài khoản qua CLI hoặc trang admin. Chính sách mật khẩu: tối thiểu 10 ký tự, kiểm tra danh sách mật khẩu phổ biến.
- **Mật khẩu:** argon2id (tham số mặc định của `argon2-cffi`), tự băm lại khi tham số thay đổi.
- **Chống dò mật khẩu:** giới hạn theo IP và theo tài khoản (5 lần sai / 15 phút → khoá tạm, tăng dần). Thông báo lỗi chung chung, không cho biết email có tồn tại hay không.
- **Session:** token ngẫu nhiên 256 bit, DB chỉ lưu hash; hết hạn khi không hoạt động 7 ngày, tối đa 30 ngày; đổi mật khẩu thì huỷ mọi session.
- **CSRF:** token đôi (cookie + header) cho mọi POST. Security headers: CSP, HSTS (khi có HTTPS), X-Content-Type-Options, frame-ancestors none.
- **Role vẫn resolve từ `Members`** mỗi request (TK §1.1). Tài khoản chỉ chứng minh danh tính (email). Email không còn trong `Members` → guest.
- **Dev-login** chỉ bật khi `APP_ENV != production` **và** `test_mode: true`. Khởi động production với dev-login bật → từ chối chạy.
- **Giới hạn chat:** guest 20 tin/giờ/session và 60 tin/giờ/IP; member 120 tin/giờ; cộng thêm budget token (mục 4.4).

### 7.13 API

| Method | Đường dẫn | Ghi chú |
|---|---|---|
| POST | `/auth/login`, `/auth/logout`, `/auth/change-password`; GET `/auth/me` | |
| GET/POST | `/auth/dev-login` | Chỉ dev/test |
| POST | `/chat` (SSE) | Stream text + sự kiện `ui_action` |
| POST | `/uploads` | |
| GET | `/drafts/{id}/preview.pdf`; POST `/drafts/{id}/confirm`, `/drafts/{id}/cancel` | Không phải tool |
| POST | `/actions/{id}/confirm`, `/actions/{id}/cancel` | Leader/admin |
| GET | `/forms/{form_id}/blank` | Link có chữ ký + hạn dùng |
| GET/POST | `/admin/...` | Tài khoản, template, KB web (danh sách trang, crawl ngay), op `FAILED`, chi phí LLM |
| GET | `/healthz` (sống), `/readyz` (DB, Qdrant, token Google), `/metrics` (chỉ mạng nội bộ) | |

---

## 8. Lộ trình theo giai đoạn

### P0 — Nền móng và hạ tầng

- [x] `.gitignore`, `pyproject.toml`, `uv.lock`, `.env.example`, cài thư viện.
- [ ] Chuyển `credentials.json`, `token.json` vào `secrets/` (**cần làm tay**: `.env` đã trỏ sẵn tới `secrets/`; `scripts/get_token.py` giờ ghi thẳng vào đó).
- [x] `.env` đã theo mục 4.4.
- [x] `deploy/compose.dev.yaml` (Postgres 16 + Qdrant); `.pre-commit-config.yaml`; `.github/workflows/ci.yml`.
- [x] `config.py` + `config.example.yaml` (đủ các mục TK §10 + `lab`, `llm`, `embedding`, `rerank`, `web_sources`, `security`).
- [x] `logging.py` (structlog JSON, `request_id`), `metrics.py`, `errors.py`, `clock.py`.
- [x] `db/` + Alembic migration đầu tiên (`0001`, 19 bảng).
- [x] `google/` (oauth, retry, 4 client, paths); `sheets/` + `FakeSheets` + test.
- [x] `domain/names.py`, `dates.py`, `members.py`, `audit.py`; `auth/roles.py`; `mail/mailer.py`.
- [x] `llm/client.py` (bản đầu) + **`labagent smoke llm`**: kiểm tra trên gateway tool use, strict, streaming, prompt cache (`cache_read_input_tokens`), structured output, embedding, rerank. Ghi kết quả vào `docs/gateway-compat.md`.
- [x] **`labagent preflight`**: config, quyền Google, đường dẫn Drive, tiêu đề cột mọi sheet, consent screen "In production", LLM, DB, Qdrant.

**Xong khi:**
- `labagent preflight` toàn xanh trên Drive thật.
- Xác định đúng role cho leader / thư ký / member / admin / guest.
- Gửi được 1 mail `[TEST]`.
- `labagent smoke llm` đã ghi rõ tính năng nào gateway hỗ trợ.
- CI chạy xanh.

### P1 — Job không dùng LLM

- [x] `domain/equipment.py`, `domain/duty.py` + test với fixture ẩn danh dựng theo bố cục thật (`tests/factories.py`).
- [x] `outbox/` + worker + advisory lock; test lỗi giữa chừng rồi chạy lại.
- [x] `jobs/` (scheduler, idempotency, CLI) + `duty_create`, `duty_remind`, `equipment_reminder`, `weekly_report`, `draft_cleanup`, `health` (+ `data_retention`).
- [x] `email_templates/*.j2`. Không dùng `config.test.yaml` riêng: `config.yaml` hiện trỏ thẳng tới folder Drive dữ liệu test của lab.

**Xong khi:** chạy tay từng job trên folder test ra đúng kết quả; chạy lại lần 2 không tạo gì trùng; test tích hợp pass.

### P2 — Chatbot hỏi đáp + KB web

- [x] `rag/` (extract, chunker, sparse, store, indexer, retriever + rerank).
- [x] `rag/crawler.py` + job `web_crawl` + bảng `web_pages`; fixture HTML của isilab.click để test (trang mới / đổi / mất / soft 404 / chốt an toàn 30%).
- [x] Job `kb_sync` (Drive changes), `kb_reconcile`.
- [x] `tools/` (registry + tool `query_*`, `search_kb`, `list_forms`, `get_application_status`, `list_pending_applications`).
- [x] `llm/usage.py` (budget), `agents/` (loop, router, conversation, prompts).
- [x] `auth/` đầy đủ (mục 7.12), `labagent account ...`, `api/auth.py`, `api/chat.py` (SSE), `api/uploads.py`.
- [x] `web/` (chat, đăng nhập, đổi mật khẩu, dev-login) + trang admin KB web.
- [x] `tests/eval/`: 30 câu hỏi (20 câu theo TK + 10 câu về nội dung web), mỗi câu ghi role, tool mong đợi và ý chính; script chấm điểm + ghi chi phí/độ trễ.

**Xong khi:**
- Guest không bao giờ nhận chunk `internal` (test đơn vị + tích hợp).
- ≥ 28/30 câu eval đúng.
- p95 thời gian ra token đầu tiên < 8 giây.
- Chi phí trung bình mỗi lượt khớp ước tính ở mục 4.3 (sai lệch ±50%).
- Crawl lại web không đổi nội dung thì không reindex gì.

### P3 — Form engine

- [x] 3 schema (`schemas/`) viết theo mẫu thật trên Drive + chèn placeholder vào bản sao (`forms/placeholder.py`, dùng lại ở P4); `labagent forms publish` (tự kiểm tra bằng dữ liệu mẫu rồi mới kích hoạt).
- [x] validators; drafts / render (docxtpl + LibreOffice, không có LibreOffice thì xem trước bằng DOCX) / engine / extract (luật theo anchor trước, LLM bổ sung phần thiếu).
- [x] Tool form; endpoint confirm (`/drafts/{id}/confirm`, khoá CONFIRMING chống bấm đúp, so hash, recheck_live); 3 hook qua outbox (upload idempotent theo `appProperties`).
- [x] Luồng trả, trả một phần (idempotent), gia hạn; quản lý thành viên (duyệt/từ chối/rời lab/đổi role) — tất cả qua yêu cầu chờ bấm Xác nhận (`pending_actions`); job `retention`.
- [x] UI: nút 📎 tải file, thẻ xem trước + Xác nhận/Huỷ, nút tải mẫu đơn. Migration `0002`.
- [ ] Chạy `labagent forms publish` thật trên Drive (đã dry-run OK cả 3 đơn) và thử tay trên giao diện.
- [ ] Mẫu đơn gia nhập chưa có câu đồng ý xử lý dữ liệu cá nhân (TK §8.1 bước 7) — lab cần thêm vào Google Doc rồi publish lại.
- [ ] Drive đang có cả `BienBanHop` + `BienBanHop.docx`, `DonXinGiaNhap` + `DonXinGiaNhap.docx`: publish dùng bản Google Doc; nên chuyển bản `.docx` vào `_archive/` (TK §7.7: mỗi đơn một file).

**Xong khi:**
- Nộp được 3 loại đơn theo 2 cách.
- Sửa sau khi xem trước thì nút xác nhận cũ bị từ chối.
- Hai người cùng mượn món thiết bị cuối: đúng một người thành công.
- Bấm xác nhận 2 lần chỉ tạo 1 bản ghi.
- Trường `locked` không sửa được qua chat; draft quá hạn bị từ chối.

### P4 — Tự sinh schema

Luồng (chốt 2026-10-26): admin gửi file mẫu đơn (.docx, đơn trắng **hoặc đơn đã điền thông tin mẫu**) trong chat và nhờ "thêm mẫu này vào hệ thống" → agent tẩy trắng, sinh schema, điền thử → **thẻ xem trước + nút Kích hoạt/Huỷ** (giống thẻ xác nhận đơn P3) → admin sửa bằng lời nếu cần → bấm Kích hoạt → lưu Drive + form `active` → member dùng ngay qua form engine (xử lý chung: lưu file + dòng `FormSubmissions`). Thay cho mail duyệt của bản 1.

- [x] `forms/schema_gen/`: `blanks.py` (luật ① tìm ô trống: `Nhãn: …`, nhiều ô/dòng, dòng chấm dưới nhãn, `…` giữa câu, `ngày … tháng … năm …`, `…/…/……`, ô tick ☐, bảng STT/bảng trống, ô bảng cạnh nhãn, ô ký), `llm_sweep.py` (② profile `schema_gen`: thông tin mẫu cần tẩy, ô sót, kiểu, ai điền, người ký, đối tượng dùng), `whiteout.py` (tẩy trắng, kiểm tra lại từng đoạn LLM chỉ ra), `types.py` (③ luật kiểu + key chuẩn `ho_ten/email/sdt/mssv`), `build.py` (gộp luật + LLM; LLM lỗi → chỉ luật), `pipeline.py` (④ placeholder, bỏ trường không đặt được, ⑤ tự kiểm tra, phát hiện chỗ trống còn sót), `review.py` (đề xuất/sửa/huỷ/kích hoạt, hash chống nút cũ, khoá chống bấm đúp), `activate.py` (outbox `template_activate`: mẫu trắng → `templates/`, bản cũ cùng form → `templates/_archive/`, `_system/<form_id>/v<ngày>/`, `form_versions`), `compare.py`.
- [x] Mở rộng P3 dùng chung: kiểu ô `cell`, `checkbox` (render ☒/☐, trích xuất ngược), nhãn lặp lại, chuỗi "……" dài; `template.file_id` để nút tải mẫu luôn đúng file.
- [x] Tool admin `propose_template`, `update_template_draft`, `cancel_template_draft` (không có tool kích hoạt); endpoint `/admin/api/templates/{id}/preview|blank|confirm|cancel`; trang admin mục "Mẫu đơn" (danh sách, ngừng dùng); prompt; job `draft_cleanup` xoá bản duyệt quá hạn; migration `0003`; `labagent forms generate --template x.docx [--no-llm] [--compare <form_id>]`.
- [x] Test: 18 unit (3 đơn thật + 2 đơn tổng hợp + đơn đã điền mẫu), 5 tích hợp (admin thêm → sửa → kích hoạt → member nộp; thay phiên bản; trùng mã đơn có hook bị chặn; phân quyền; endpoint).
- [ ] Thử tay trên giao diện với Drive thật.
- [ ] Chưa làm: tự phát hiện file admin thả thẳng vào `templates/` trên Drive (bản 1) — hiện chỉ thêm qua chat.

**Kết quả chạy thật (Opus 5.5, ~40 s/đơn):** đơn gia nhập tương đương 100% `schemas/`; phiếu mượn chỉ khác "Mục đích" text/longtext (coi là tương đương); biên bản họp khác 2 điểm ngữ nghĩa ("Hình thức" không in sẵn lựa chọn nên AI để text; ngày đầu biên bản AI coi là ngày lập) — admin sửa bằng một câu chat. Đơn đã điền mẫu: tẩy đúng 12/12 thông tin, ra cùng các ô như bản trắng. Chỉ dùng luật (không AI): 2/3 đơn gốc tương đương.

**Xong khi:** 5 đơn trống mẫu cho ra đúng trường; schema sinh từ 3 đơn ban đầu tương đương `schemas/`.

### P5 — Gia cố bảo mật và độ tin cậy

- [ ] `tests/security/`:
  - prompt injection trong CV/đơn và **trong nội dung web crawl**;
  - tự xưng role trong chat;
  - truy cập draft/preview của người khác;
  - CSRF, brute-force đăng nhập;
  - upload độc (zip-bomb, sai magic bytes);
  - vượt budget.
- [ ] Tiêm lỗi (Google 500/timeout ở từng step, gateway LLM sập, Qdrant sập, web lab sập) → hệ thống giảm chất lượng có kiểm soát, không lệch dữ liệu.
- [ ] Kiểm thử tải nhẹ (k6 hoặc locust): 20 user chat đồng thời.
- [ ] `pip-audit` sạch; quét image (Trivy).

**Xong khi:** toàn bộ test bảo mật và tiêm lỗi pass.

### P6 — Đưa vào production và nhân rộng cho các lab

- [ ] `deploy/Dockerfile` (image worker có LibreOffice + font tiếng Việt), `compose.yaml`, Caddy (HTTPS).
- [ ] Backup: `pg_dump` hằng ngày, giữ 14 bản, **diễn tập khôi phục**. Qdrant không cần backup vì dựng lại được từ nguồn bằng `labagent reindex --all`.
- [ ] Alert: op `FAILED`, job lỗi, token Google, chi phí LLM vượt ngưỡng, web lab lỗi nhiều ngày, đĩa đầy.
- [ ] Runbook trong `docs/runbooks/`:
  - **onboarding lab mới** (tạo tài khoản Google, OAuth, upload cây thư mục mẫu, config, preflight, tạo admin);
  - xoay vòng secrets; khôi phục backup; xử lý sự cố.
- [ ] Checklist tắt `test_mode`: email thật, tắt dev-login, bỏ cột Address, đồng ý xử lý dữ liệu.
- [ ] Phát hành theo phiên bản (tag + changelog), migration chạy tự động khi deploy, có cách rollback.

**Xong khi:** isilab chạy production ổn định 2 tuần; một lab thứ hai được onboard chỉ bằng runbook, không sửa code.

---

## 9. Chiến lược kiểm thử

| Tầng | Công cụ | Phạm vi |
|---|---|---|
| Unit | pytest, `time-machine` | names, dates, SheetTable, validators, equipment, duty, chunker, sparse, crawler diff, blanks, password/session |
| Tích hợp (giả) | `FakeSheets` / `FakeDrive` / `FakeGmail` / `FakeLLM` / `FakeWeb`, Postgres qua `testcontainers` | Job chạy 2 lần, outbox lỗi giữa chừng, luồng đơn, confirm, hook, crawl |
| Tích hợp (thật) | `@pytest.mark.live`, folder Drive test, gateway thật | copyTo, protected range, upload, Google Doc, smoke LLM |
| Agent / eval | `FakeLLM` (vòng lặp, phân quyền); bộ eval với model thật (chất lượng, chi phí, độ trễ) | Chạy eval trước mỗi lần đổi model hoặc prompt |
| Bảo mật | `tests/security/` | Mục P5 |

Fixture từ dữ liệu thật phải được ẩn danh trước khi commit; bản chưa ẩn danh để trong `tests/fixtures/private/` (đã gitignore).

---

## 10. Vận hành production

| Hạng mục | Cách làm |
|---|---|
| Triển khai | Mỗi lab một compose project (`deploy/labs/<lab_id>/`); một server nhỏ (2 vCPU, 4 GB RAM) chạy được vài lab |
| Secrets | `.env` quyền 600 hoặc Docker secrets; không bao giờ log key; xoay vòng key LLM và OAuth theo runbook |
| Logs | JSON ra stdout; giữ 30 ngày; **không log nội dung chat, CV hay SĐT** (chỉ log ID và độ dài) |
| Metrics | Request/độ trễ, lượt chat, token và chi phí LLM theo profile, job thành công/thất bại, outbox tồn đọng, kết quả crawl |
| Alert | Gửi mail admin (và Sentry nếu bật) |
| Dữ liệu cá nhân | Hội thoại 30 ngày; upload chưa nộp 24 giờ; đơn bị từ chối archive sau 6 tháng (TK §8.1); trang admin xuất và xoá dữ liệu của một người khi được yêu cầu |
| Nâng cấp | Tag phiên bản → CI build image → từng lab `docker compose pull && up -d` (migration tự chạy) |

---

## 11. Rủi ro và cách giảm

| Rủi ro | Cách giảm |
|---|---|
| Gateway LLM trung gian ngừng, đổi giá, hoặc chặn tính năng | Lớp `llm/` chỉ dùng chuẩn native Anthropic/OpenAI; đổi `base_url` là xong; có model fallback và chế độ giảm chất lượng; alert chi phí |
| Dữ liệu cá nhân qua bên thứ ba | Câu đồng ý rõ ràng; chỉ gửi trường cần thiết; khuyến nghị dùng API chính hãng khi nhân rộng |
| Độ trễ kênh Claude trên gateway không ổn định (Sonnet 5.5 có đỉnh 25–49 s) | Mặc định dùng Haiku 4.5 / Gemini Flash Lite cho chat; stream; `first_token_timeout_s` → fallback khác hãng; đo p95 ở P2 |
| Model nhỏ điền đơn sai về ngữ nghĩa | Xem trước + xác nhận bắt buộc; eval P3 làm ngưỡng; nâng `member_agent` lên Sonnet 5.5 bằng một dòng config |
| Lab đổi bố cục sheet | Đọc theo tiêu đề; thiếu tiêu đề thì dừng và báo; `preflight` phát hiện sớm |
| Web lab đổi giao diện hoặc sập | Chốt an toàn 30%, chỉ xoá sau 2 lần mất, giữ index khi lỗi mạng, lưu snapshot |
| Refresh token Google chết | Consent screen "In production"; job `health` hằng ngày; alert |
| Lạm dụng chatbot guest gây tốn tiền | Rate limit theo session/IP, budget token, hard stop theo tháng, model rẻ cho guest |
| LibreOffice treo | Container Linux, timeout + kill, semaphore |
| Ghi đồng thời với người sửa tay trên Sheet | Ghi từng ô; tìm lại dòng theo khoá ngay trước khi ghi |
