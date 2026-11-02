"""Cấu hình: Settings (.env, bí mật) + AppConfig (config.yaml, có cấu trúc).

Thứ tự ưu tiên: biến môi trường > config.yaml > mặc định trong code.
`load_config()` trả về cả nguồn của từng giá trị để `labagent preflight` in ra.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigError

# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_config_path: str = "config.yaml"
    app_secret_key: SecretStr = SecretStr("")

    lab_website_url: str | None = None

    database_url: str = "postgresql+psycopg://labagent:labagent@localhost:5432/labagent"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None

    llm_api_key: SecretStr = SecretStr("")
    llm_anthropic_base_url: str = "https://api.anthropic.com"
    llm_openai_base_url: str = "https://api.openai.com/v1"
    llm_model_member: str | None = None
    llm_model_guest: str | None = None
    llm_model_extract: str | None = None
    llm_model_schema_gen: str | None = None
    llm_fallback_claude: str | None = None
    llm_fallback_openai_compat: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    rerank_model: str | None = None

    google_client_secrets_file: str = "secrets/credentials.json"
    google_token_file: str = "secrets/token.json"  # noqa: S105

    sentry_dsn: str | None = None
    log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


# Biến môi trường → đường dẫn trong config.yaml
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "llm_model_member": ("llm", "profiles", "member_agent", "model"),
    "llm_model_guest": ("llm", "profiles", "guest_agent", "model"),
    "llm_model_extract": ("llm", "profiles", "extract", "model"),
    "llm_model_schema_gen": ("llm", "profiles", "schema_gen", "model"),
    "llm_fallback_claude": ("llm", "fallback", "anthropic"),
    "llm_fallback_openai_compat": ("llm", "fallback", "openai_compat"),
    "embedding_model": ("embedding", "model"),
    "embedding_dimensions": ("embedding", "dimensions"),
    "rerank_model": ("rerank", "model"),
    "lab_website_url": ("lab", "website"),
}


# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabCfg(_Model):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    website: str | None = None
    # thầy phụ trách (admin) không nằm trong Members; tên này được phép ở trường member_ref có allow_admin
    advisor_name: str | None = None


class TestCfg(_Model):
    dev_login: bool = False
    # Cô lập phần xác thực khi phát triển tính năng: không cần đăng nhập, vai trò chọn ngay trên giao diện.
    # Chỉ có hiệu lực khi test_mode=true và APP_ENV != production (production từ chối khởi động nếu bật).
    auth_bypass: bool = False
    bypass_role: Literal["guest", "member", "thu_ki", "leader", "admin"] = "member"
    subject_prefix: str = "[TEST]"
    show_intended_recipient: bool = True


class DriveCfg(_Model):
    root_folder_id: str


class AdminCfg(_Model):
    """Một tài khoản admin (thầy hướng dẫn, dev): không cần có trong Members; họ tên dùng khi mượn thiết bị,
    ghi AuditLog, khớp sổ mượn."""

    email: str
    ho_ten: str = ""

    @field_validator("email")
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.strip().lower()


class AuthCfg(_Model):
    admins: list[AdminCfg] = []
    session_idle_days: int = 7
    session_max_days: int = 30
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    password_min_length: int = 10
    role_cache_seconds: int = 60
    # Địa chỉ trang đăng nhập ghi trong mail mật khẩu tạm khi hệ thống TỰ tạo tài khoản (duyệt đơn gia nhập)
    login_url: str | None = None

    @field_validator("admins", mode="before")
    @classmethod
    def _admins(cls, v: Any) -> Any:
        # chấp nhận cả dạng cũ: danh sách email
        if not isinstance(v, list):
            return v
        out = []
        for x in v:
            if isinstance(x, str):
                if x.strip():
                    out.append({"email": x})
            else:
                out.append(x)
        return out

    def admin_emails(self) -> list[str]:
        return [a.email for a in self.admins if a.email]

    def admin(self, email: str | None) -> AdminCfg | None:
        e = (email or "").strip().lower()
        return next((a for a in self.admins if e and a.email == e), None)

    def admin_by_name(self, ho_ten: str) -> AdminCfg | None:
        from app.domain.names import norm_name

        k = norm_name(ho_ten or "")
        return next((a for a in self.admins if a.ho_ten and norm_name(a.ho_ten) == k), None)


class EmailCfg(_Model):
    sender: str | None = None
    allowed_domains: list[str] = ["gmail.com"]
    alert_recipients: list[str] = []


class UploadCfg(_Model):
    max_mb: int = 10
    allowed_types: list[str] = ["pdf", "docx"]


class MembersSheetCfg(_Model):
    path: str
    tab: str | None = None
    header_row: int = 1
    columns: dict[str, str]
    never_expose: list[str] = []
    role_values: dict[str, str]
    role_labels: dict[str, str] = {}
    former_tab: str = "Cựu thành viên"


class TableCfg(_Model):
    tab: str
    header_row: int = 1
    columns: dict[str, str]


class DevicesCfg(TableCfg):
    tinh_trang_values: list[str] = []
    not_borrowable: list[str] = []


class BorrowsCfg(TableCfg):
    date_format: str = "%d/%m/%Y"
    default_so_luong: int = 1


class EquipmentSheetCfg(_Model):
    path: str
    devices: DevicesCfg
    borrows: BorrowsCfg


class DutySheetCfg(_Model):
    path: str
    template_path: str
    template_tab: str | None = None
    tab_name_format: str = "{start:%d/%m}-{end:%d/%m}"
    title_cell: str = "A1"
    title_format: str = "LỊCH TRỰC LAB TUẦN TỪ {start:%d/%m} - {end:%d/%m}"
    day_header_row: int = 4
    slot_header_row: int = 5
    first_member_row: int = 7
    no_col: str = "A"
    name_col: str = "B"
    worklog_header: str = "Nội dung công việc đang thực hiện"
    count_header: str = "Số buổi trực lab"
    slot_labels: list[str] = ["Sáng", "Chiều"]
    marks_counted: list[str] = ["V", "T"]
    fixed_mark: str = "T"
    # Chép ô T từ tuần hiện tại sang tab mới. Tắt mặc định: T do agent tự giao mỗi tuần (duty_assign).
    copy_fixed_from_prev: bool = False
    # Chỉ tiêu T khi tự giao: cứ `registrations_per_t` buổi đăng ký thì 1 T (làm tròn, tối thiểu 1)
    registrations_per_t: int = Field(default=3, ge=1)
    min_sessions_per_week: int = 3


class PathOnlyCfg(_Model):
    path: str
    tab: str | None = None


class SheetsCfg(_Model):
    members: MembersSheetCfg
    equipment: EquipmentSheetCfg
    duty: DutySheetCfg
    form_submissions: PathOnlyCfg
    audit_log: PathOnlyCfg


class DraftsCfg(_Model):
    ttl_hours: int = 24
    confirm_wait_s: float = 30  # endpoint xác nhận chờ outbox tối đa (giây)
    keep_after_submit_days: int = 7  # giữ bản nháp đã nộp (bytes PDF) để worker xử lý / tra lại
    pending_action_ttl_hours: int = 24
    blank_link_ttl_minutes: int = 120  # link tải đơn trống có chữ ký


class ExportsCfg(_Model):
    ttl_minutes: int = 60  # link tải file xuất hết hạn sau N phút (chỉ đúng phiên đã yêu cầu tải được)
    max_rows: int = 5000  # quá số dòng này thì báo người dùng lọc hẹp hơn
    max_files: int = 30  # xuất đơn đã nộp (docx/zip): tối đa số đơn trong một lần xuất


class FormsCfg(_Model):
    schemas_dir: str = "schemas"
    system_folder: str = "templates/_system"
    # P4: mẫu đơn admin thêm qua chat
    templates_folder: str = "templates"  # mẫu trắng (member tải về) lưu ở đây
    submissions_folder: str = "submissions"  # đơn nộp theo mẫu mới: <folder>/<form_id>/<YYYY-MM>/
    review_ttl_hours: int = 72  # bản duyệt chưa kích hoạt sau thời gian này bị xoá
    default_roles: list[str] = ["member", "thu_ki", "leader"]


class ApplicationsCfg(_Model):
    rejected_retention_months: int = 6


class IndexCfg(_Model):
    drive_poll_minutes: int = 15
    chunk_tokens: int = 700
    chunk_overlap: float = 0.12
    kb_public_path: str = "kb/public"
    kb_internal_path: str = "kb/internal"


class EquipmentCfg(_Model):
    overdue_reminder_every_days: int = 3


class NotificationsCfg(_Model):
    # Hạn đăng ký lịch trực ghi trong mail thông báo/nhắc; {sunday} = chủ nhật trước tuần trực
    duty_deadline: str = "hết Chủ nhật {sunday:%d/%m}"
    # Người nhận báo cáo tuần (email tra trong Members lúc chạy)
    weekly_report_to: list[Literal["leader", "thu_ki"]] = ["leader", "thu_ki"]


class ProfileCfg(_Model):
    model: str
    max_tokens: int = 4000
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class ModelCap(_Model):
    adapter: Literal["anthropic", "openai_compat"]
    effort: bool = False
    strict_tools: bool = False
    price_in: float = 0.0
    price_out: float = 0.0
    price_cache_read: float | None = None  # mặc định 10% price_in
    price_cache_write: float | None = None  # mặc định 125% price_in


class FallbackCfg(_Model):
    anthropic: str | None = None
    openai_compat: str | None = None


class BudgetsCfg(_Model):
    max_input_chars_per_message: int = 4000
    guest_tokens_per_session_per_day: int = 200000
    member_tokens_per_user_per_day: int = 400000
    lab_usd_per_month_alert: float = 30
    lab_usd_per_month_hard_stop: float = 60


class LLMCfg(_Model):
    request_timeout_s: float = 60
    first_token_timeout_s: float = 15
    max_tool_iterations: int = 8
    tool_timeout_s: float = 20
    profiles: dict[str, ProfileCfg]
    fallback: FallbackCfg = FallbackCfg()
    model_capabilities: dict[str, ModelCap]
    budgets: BudgetsCfg = BudgetsCfg()

    def capability(self, model: str) -> ModelCap:
        try:
            return self.model_capabilities[model]
        except KeyError:
            raise ConfigError(f"Model '{model}' chưa khai trong llm.model_capabilities") from None

    def fallback_for(self, model: str) -> str | None:
        adapter = self.capability(model).adapter
        return self.fallback.anthropic if adapter == "anthropic" else self.fallback.openai_compat


class EmbeddingCfg(_Model):
    model: str = "text-embedding-3-large"
    dimensions: int = 1024
    batch_size: int = 64
    price_per_m: float = 0.156


class RerankCfg(_Model):
    enabled: bool = True
    model: str = "qwen3-rerank"
    candidates: int = 30
    top_n: int = 6
    timeout_s: float = 10
    price_per_m: float = 0.6


class QdrantCfg(_Model):
    collection_prefix: str = "kb"


class LangsCfg(_Model):
    param: str = "lang"
    values: list[str] = ["vi"]


class WebSourceCfg(_Model):
    id: str
    base_url: str | None = None
    langs: LangsCfg = LangsCfg()
    seeds: list[str] = ["/"]
    listing_pages: list[str] = []
    include: list[str] = [".*"]
    exclude: list[str] = []
    max_pages: int = 400
    max_depth: int = 4
    rate_per_sec: float = 1.0
    timeout_s: float = 20
    retries: int = 3
    sitemap: str | None = None
    min_content_chars: int = 40
    soft_404_patterns: list[str] = ["page not found", "không tìm thấy"]
    drop_tags: list[str] = ["nav", "footer", "script", "style", "form"]
    drop_class_patterns: list[str] = []
    delete_after_missing_runs: int = 2
    max_change_ratio: float = 0.30
    recent_days: int = 30
    access: Literal["public", "member"] = "public"
    bot_contact: str | None = None
    # theo ngôn ngữ trang: {tiêu đề/heading gốc: tên gọi thêm khi index}, vd {vi: {"Contact Us": "Liên hệ"}}
    heading_aliases: dict[str, dict[str, str]] = {}

    @field_validator("include", "exclude")
    @classmethod
    def _regex_ok(cls, v: list[str]) -> list[str]:
        for p in v:
            re.compile(p)
        return v


class SecurityCfg(_Model):
    cookie_secure: bool | None = None
    guest_msgs_per_session_hour: int = 20
    guest_msgs_per_ip_hour: int = 60
    member_msgs_per_hour: int = 120
    login_attempts_per_ip_15min: int = 30
    trusted_proxies: list[str] = ["127.0.0.1"]
    metrics_allow_cidrs: list[str] = ["127.0.0.0/8"]


class RenderCfg(_Model):
    soffice_path: str | None = None  # null = tự tìm (PATH, thư mục cài mặc định)
    max_concurrency: int = 1
    timeout_s: float = 60


class RetentionCfg(_Model):
    conversation_days: int = 30
    upload_hours: int = 24
    web_snapshots_keep: int = 5


class WorkerCfg(_Model):
    poll_interval_s: float = 2
    outbox_max_attempts: int = 6
    advisory_lock_key: int = 42001


class PathsCfg(_Model):
    var_dir: str = "var"


class AppConfig(_Model):
    timezone: str = "Asia/Ho_Chi_Minh"
    lab: LabCfg
    test_mode: bool = False
    test: TestCfg = TestCfg()
    drive: DriveCfg
    auth: AuthCfg = AuthCfg()
    email: EmailCfg = EmailCfg()
    upload: UploadCfg = UploadCfg()
    sheets: SheetsCfg
    drafts: DraftsCfg = DraftsCfg()
    exports: ExportsCfg = ExportsCfg()
    forms: FormsCfg = FormsCfg()
    applications: ApplicationsCfg = ApplicationsCfg()
    index: IndexCfg = IndexCfg()
    schedule: dict[str, str] = {}
    equipment: EquipmentCfg = EquipmentCfg()
    notifications: NotificationsCfg = NotificationsCfg()
    llm: LLMCfg
    embedding: EmbeddingCfg = EmbeddingCfg()
    rerank: RerankCfg = RerankCfg()
    qdrant: QdrantCfg = QdrantCfg()
    web_sources: list[WebSourceCfg] = []
    security: SecurityCfg = SecurityCfg()
    render: RenderCfg = RenderCfg()
    retention: RetentionCfg = RetentionCfg()
    worker: WorkerCfg = WorkerCfg()
    paths: PathsCfg = PathsCfg()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        caps = self.llm.model_capabilities
        for name, prof in self.llm.profiles.items():
            if prof.model not in caps:
                raise ValueError(
                    f"llm.profiles.{name}.model = '{prof.model}' chưa khai trong model_capabilities"
                )
        for fb in (self.llm.fallback.anthropic, self.llm.fallback.openai_compat):
            if fb and fb not in caps:
                raise ValueError(f"Model fallback '{fb}' chưa khai trong model_capabilities")
        for required in ("member_agent", "guest_agent"):
            if required not in self.llm.profiles:
                raise ValueError(f"Thiếu llm.profiles.{required}")
        for src in self.web_sources:
            if not (src.base_url or self.lab.website):
                raise ValueError(f"web_sources.{src.id}: thiếu base_url và lab.website")
        return self

    # tiện ích
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def var_dir(self) -> Path:
        return Path(self.paths.var_dir)

    def web_base_url(self, src: WebSourceCfg) -> str:
        url = src.base_url or self.lab.website or ""
        return url if url.endswith("/") else url + "/"

    def alert_recipients(self) -> list[str]:
        return self.email.alert_recipients or self.auth.admin_emails()

    @property
    def qdrant_alias(self) -> str:
        return f"{self.qdrant.collection_prefix}_{self.lab.id}"

    @property
    def qdrant_collection(self) -> str:
        # text-embedding-3-large → te3l (đổi model/số chiều ⇒ collection mới, chuyển alias khi reindex)
        parts = [p for p in re.split(r"[^a-z0-9]+", self.embedding.model.lower()) if p]
        short = "".join(p if p.isdigit() else p[0] for p in parts)
        return f"{self.qdrant.collection_prefix}_{self.lab.id}_{short}_{self.embedding.dimensions}"


# ---------------------------------------------------------------------------
# Nạp + nguồn giá trị
# ---------------------------------------------------------------------------


class LoadedConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings
    config: AppConfig
    sources: dict[str, str]  # "llm.profiles.member_agent.model" -> env|yaml|default


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out.update(_flatten(v, key))
            else:
                out[key] = v
    return out


def _set_path(d: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = d
    for p in path[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[path[-1]] = value


def load_config(settings: Settings | None = None, path: str | Path | None = None) -> LoadedConfig:
    settings = settings or Settings()
    cfg_path = Path(path or settings.app_config_path)
    if not cfg_path.exists():
        raise ConfigError(f"Không tìm thấy file cấu hình {cfg_path} (sao chép từ config.example.yaml)")
    with cfg_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: nội dung phải là mapping YAML")

    yaml_keys = set(_flatten(raw))
    env_keys: set[str] = set()
    for attr, cpath in ENV_OVERRIDES.items():
        val = getattr(settings, attr)
        if val is not None and val != "":
            _set_path(raw, cpath, val)
            env_keys.add(".".join(cpath))

    try:
        config = AppConfig.model_validate(raw)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(f"{cfg_path} không hợp lệ:\n{e}") from e

    validate_runtime(settings, config)

    sources: dict[str, str] = {}
    for key in _flatten(config.model_dump(mode="json")):
        if key in env_keys:
            sources[key] = "env"
        elif key in yaml_keys:
            sources[key] = "yaml"
        else:
            sources[key] = "default"
    return LoadedConfig(settings=settings, config=config, sources=sources)


def validate_runtime(settings: Settings, config: AppConfig) -> None:
    """Các điều kiện bắt buộc dừng khởi động (nguyên tắc production 2)."""
    if settings.is_production:
        if config.test.dev_login or config.test_mode or config.test.auth_bypass:
            raise ConfigError(
                "APP_ENV=production nhưng test_mode/dev_login/auth_bypass đang bật: từ chối chạy"
            )
        if len(settings.app_secret_key.get_secret_value()) < 32:
            raise ConfigError("APP_SECRET_KEY phải dài ít nhất 32 ký tự ở production")
        if not config.auth.admins:
            raise ConfigError("auth.admins rỗng ở production")


def dev_login_enabled(settings: Settings, config: AppConfig) -> bool:
    return (not settings.is_production) and config.test_mode and config.test.dev_login


def auth_bypass_enabled(settings: Settings, config: AppConfig) -> bool:
    return (not settings.is_production) and config.test_mode and config.test.auth_bypass


def lint_env_file(path: str | Path = ".env") -> list[str]:
    """Cảnh báo các dòng .env dễ bị parser hiểu khác nhau (dấu cách quanh '=', chú thích cùng dòng)."""
    p = Path(path)
    if not p.exists():
        return [f"{p}: không tồn tại"]
    warnings: list[str] = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            warnings.append(f"{p}:{i}: dòng không có '='")
            continue
        key, _, value = s.partition("=")
        if key != key.rstrip() or value != value.lstrip():
            warnings.append(f"{p}:{i}: có dấu cách quanh '=' ({key.strip()})")
        if re.search(r"\s#", value) and not (value.startswith('"') or value.startswith("'")):
            warnings.append(f"{p}:{i}: có chú thích cùng dòng ({key.strip()})")
    return warnings


# ---------------------------------------------------------------------------
# Singleton cho tiến trình (test có thể ghi đè)
# ---------------------------------------------------------------------------

_override: LoadedConfig | None = None


@lru_cache(maxsize=1)
def _cached() -> LoadedConfig:
    return load_config()


def get_loaded() -> LoadedConfig:
    return _override or _cached()


def get_config() -> AppConfig:
    return get_loaded().config


def get_settings() -> Settings:
    return get_loaded().settings


def set_loaded(loaded: LoadedConfig | None) -> None:
    global _override
    _override = loaded
    _cached.cache_clear()


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}
