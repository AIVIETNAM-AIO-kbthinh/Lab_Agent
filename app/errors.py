"""Lỗi nghiệp vụ dùng chung. Thông điệp `user_message` an toàn để hiển thị cho người dùng."""

from __future__ import annotations


class LabAgentError(Exception):
    user_message = "Đã có lỗi xảy ra, vui lòng thử lại sau."

    def __init__(self, message: str = "", *, user_message: str | None = None) -> None:
        if user_message:
            self.user_message = user_message
        super().__init__(message or self.user_message)


class ConfigError(LabAgentError):
    """Config sai: dừng ngay lúc khởi động."""


class HeaderMissing(LabAgentError):
    """Sheet thiếu tiêu đề cột đã khai trong config."""


class NotFound(LabAgentError):
    user_message = "Không tìm thấy dữ liệu yêu cầu."


class PermissionDenied(LabAgentError):
    user_message = "Bạn không có quyền thực hiện thao tác này."


class DuplicateNames(LabAgentError):
    """Members có họ tên trùng nhau: job phải dừng và báo admin."""


class BudgetExceeded(LabAgentError):
    user_message = "Hệ thống đã đạt hạn mức sử dụng trong hôm nay, vui lòng thử lại sau."


class RateLimited(LabAgentError):
    user_message = "Bạn gửi quá nhiều yêu cầu, vui lòng chờ một lát rồi thử lại."


class LLMUnavailable(LabAgentError):
    user_message = "Trợ lý AI đang tạm thời không phản hồi, vui lòng thử lại sau ít phút."


class ExternalServiceError(LabAgentError):
    """Lỗi từ Google/LLM/web sau khi đã hết retry."""


class ValidationFailed(LabAgentError):
    user_message = "Dữ liệu không hợp lệ."


class DraftInvalid(LabAgentError):
    user_message = "Bản nháp không còn hiệu lực, vui lòng tạo lại."


class DraftChanged(LabAgentError):
    user_message = "Đơn đã thay đổi sau khi bạn xem, vui lòng xem lại bản mới nhất và xác nhận lại."


class RenderFailed(LabAgentError):
    user_message = "Không tạo được bản xem trước của đơn, vui lòng thử lại sau."
