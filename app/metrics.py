"""Prometheus metrics (endpoint /metrics, chỉ mạng nội bộ)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("labagent_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("labagent_http_request_seconds", "HTTP latency", ["route"])

CHAT_TURNS = Counter("labagent_chat_turns_total", "Lượt chat", ["role", "outcome"])
CHAT_FIRST_TOKEN = Histogram(
    "labagent_chat_first_token_seconds",
    "Thời gian tới token đầu tiên",
    ["profile"],
    buckets=(0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60),
)
LLM_TOKENS = Counter("labagent_llm_tokens_total", "Token LLM", ["profile", "model", "kind"])
LLM_COST = Counter("labagent_llm_cost_usd_total", "Chi phí LLM (USD)", ["profile", "model"])
LLM_ERRORS = Counter("labagent_llm_errors_total", "Lỗi LLM", ["model", "kind"])
LLM_FALLBACKS = Counter(
    "labagent_llm_fallbacks_total", "Chuyển sang model fallback", ["from_model", "to_model"]
)

TOOL_CALLS = Counter("labagent_tool_calls_total", "Lời gọi tool", ["tool", "outcome"])

JOB_RUNS = Counter("labagent_job_runs_total", "Lần chạy job", ["job", "outcome"])
OUTBOX_BACKLOG = Gauge("labagent_outbox_backlog", "Số op đang chờ trong outbox")
OUTBOX_FAILED = Gauge("labagent_outbox_failed", "Số op FAILED")

CRAWL_PAGES = Counter("labagent_crawl_pages_total", "Kết quả crawl theo trang", ["source", "result"])
INDEX_CHUNKS = Counter("labagent_index_chunks_total", "Chunk được index", ["source"])
GOOGLE_CALLS = Counter("labagent_google_calls_total", "Lời gọi Google API", ["api", "outcome"])
