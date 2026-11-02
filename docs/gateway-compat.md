# Tương thích gateway LLM

> Sinh tự động bởi `labagent smoke llm` lúc 2026-10-16 22:20 (Asia/Ho_Chi_Minh).
> Anthropic base URL: `https://api.shopaikey.com` · OpenAI-compatible base URL: `https://api.shopaikey.com/v1`.
> SDK: anthropic 1.15.0.

| Model | Tính năng | Kết quả | Chi tiết |
|---|---|---|---|
| `claude-haiku-4-5-20251001` | messages.create | ✅ | 2405 ms, stop=end_turn |
| `claude-haiku-4-5-20251001` | streaming | ✅ | token đầu 1.72 s, tổng 2.34 s |
| `claude-haiku-4-5-20251001` | tool use strict | ✅ | input={'name': 'máy chiếu'} |
| `claude-haiku-4-5-20251001` | prompt caching | ✅ | lần 1: write=1461 read=4948; lần 2: write=1461 read=4948 |
| `claude-haiku-4-5-20251001` | structured output (messages.parse) | ✅ | ho_ten='Nguyễn Văn A' email='a@gmail.com' so_luong=2 |
| `claude-haiku-4-5-20251001` | output_config.effort | — | model không hỗ trợ (không gửi) |
| `claude-opus-5-5` | messages.create | ✅ | 1765 ms, stop=end_turn |
| `claude-opus-5-5` | streaming | ✅ | token đầu 2.16 s, tổng 2.58 s |
| `claude-opus-5-5` | tool use strict | ✅ | input={'name': 'máy chiếu'} |
| `claude-opus-5-5` | prompt caching | ✅ | lần 1: write=1461 read=4948; lần 2: write=1461 read=4948 |
| `claude-opus-5-5` | structured output (messages.parse) | ✅ | ho_ten='Nguyễn Văn A' email='a@gmail.com' so_luong=2 |
| `claude-opus-5-5` | output_config.effort | ✅ | chấp nhận effort=low |
| `claude-sonnet-5-5` | messages.create | ✅ | 2359 ms, stop=end_turn |
| `claude-sonnet-5-5` | streaming | ✅ | token đầu 2.78 s, tổng 3.47 s |
| `claude-sonnet-5-5` | tool use strict | ✅ | input={'name': 'máy chiếu'} |
| `claude-sonnet-5-5` | prompt caching | ✅ | lần 1: write=15132 read=0; lần 2: write=1211 read=13921 |
| `claude-sonnet-5-5` | structured output (messages.parse) | ✅ | ho_ten='Nguyễn Văn A' email='a@gmail.com' so_luong=2 |
| `claude-sonnet-5-5` | output_config.effort | ✅ | chấp nhận effort=low |
| `gemini-2.5-flash` | chat.completions (stream) | ✅ | 32125 ms, token đầu 31531 ms |
| `gemini-2.5-flash` | tool use (không strict) | ✅ | input={'name': 'Máy chiếu'} |
| `gemini-3.1-flash-lite` | chat.completions (stream) | ✅ | 2250 ms, token đầu 1984 ms |
| `gemini-3.1-flash-lite` | tool use (không strict) | ✅ | input={'name': 'máy chiếu'} |
| `text-embedding-3-large` | embedding | ✅ | 2 vector × 1024 chiều |
| `qwen3-rerank` | rerank | ✅ | [(0, 0.6043342821690618), (2, 0.3831167180970927), (1, 0.3202723860285871)] |

## Hệ quả cho code

- Prompt cache không được chuyển tiếp ⇒ code vẫn chạy, chỉ đắt hơn; theo dõi `cache_read_tokens` trong `llm_usage`.
- Structured output (`messages.parse`) lỗi ⇒ `LLMClient.structured()` đã dùng tool strict một tham số object.
- Tool strict không có trên kênh OpenAI-compatible ⇒ registry luôn validate lại tham số bằng Pydantic.
- Kênh Gemini (OpenAI-compatible) **âm thầm trả phản hồi rỗng** khi schema tool có `anyOf [..., {"type": "null"}]` (phát hiện 2026-10-16) ⇒ `openai_adapter.loose_schema()` đổi về schema lỏng; phản hồi rỗng hoàn toàn được coi là lỗi để chuyển fallback.
- `gemini-2.5-flash` (fallback của Claude) đo được token đầu ~31 s ở lần chạy 2026-10-16: cân nhắc đặt `LLM_FALLBACK_CLAUDE=gemini-3.1-flash-lite` nếu lặp lại.
