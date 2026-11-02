"""P4 — tự sinh schema từ mẫu đơn admin gửi lên (TK §7.7).

blanks (luật tìm ô trống) → llm_sweep (LLM rà soát + nhận diện thông tin mẫu) → whiteout (tẩy trắng)
→ build (gộp luật + LLM thành FormSchema) → pipeline (chèn placeholder, tự kiểm tra) → review (admin duyệt
qua thẻ xem trước trên chat) → activate (lưu Drive + kích hoạt qua outbox).
"""
