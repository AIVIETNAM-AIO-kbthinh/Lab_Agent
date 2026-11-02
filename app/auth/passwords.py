"""Mật khẩu: argon2id (tham số mặc định argon2-cffi), tự băm lại khi tham số đổi; chính sách tối thiểu."""

from __future__ import annotations

import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()

# danh sách rút gọn mật khẩu phổ biến (so khớp sau khi lowercase)
COMMON = {
    "123456",
    "123456789",
    "12345678",
    "1234567890",
    "password",
    "password1",
    "password123",
    "qwerty",
    "qwerty123",
    "abc123",
    "111111",
    "123123",
    "admin",
    "admin123",
    "iloveyou",
    "letmein",
    "welcome",
    "matkhau",
    "matkhau123",
    "1q2w3e4r",
    "1qaz2wsx",
    "zaq12wsx",
    "000000",
    "dragon",
    "monkey",
    "football",
    "baseball",
    "sunshine",
    "princess",
    "trustno1",
    "passw0rd",
    "p@ssw0rd",
    "p@ssword",
    "abcd1234",
    "12341234",
    "11111111",
    "88888888",
    "66666666",
    "99999999",
    "anhyeuem",
    "iloveyou123",
    "123qwe",
    "qwe123",
    "asdfghjkl",
    "zxcvbnm",
    "isilab123",
}


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(stored_hash: str, pw: str) -> tuple[bool, bool]:
    """Trả (đúng, cần_băm_lại)."""
    try:
        _ph.verify(stored_hash, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    return True, _ph.check_needs_rehash(stored_hash)


def dummy_verify() -> None:
    """Chạy một phép verify giả để thời gian phản hồi không lộ việc email có tồn tại hay không."""
    verify_password(_DUMMY, "x" * 12)


_DUMMY = _ph.hash("dummy-password-for-timing")


def policy_errors(pw: str, *, min_length: int = 10, email: str | None = None) -> list[str]:
    errs = []
    if len(pw) < min_length:
        errs.append(f"Mật khẩu phải dài ít nhất {min_length} ký tự.")
    if len(pw) > 256:
        errs.append("Mật khẩu quá dài.")
    low = pw.lower()
    if low in COMMON or low.rstrip("!@#.") in COMMON:
        errs.append("Mật khẩu quá phổ biến, hãy chọn mật khẩu khác.")
    if len(set(pw)) < 4:
        errs.append("Mật khẩu quá đơn giản.")
    if email and email.split("@")[0].lower() in low and len(email.split("@")[0]) >= 4:
        errs.append("Mật khẩu không được chứa tên email.")
    return errs


def generate_temp_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw
