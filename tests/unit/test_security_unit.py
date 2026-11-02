import io
import zipfile

import pytest

from app.api.uploads import UploadRejected, inspect_file
from app.auth import csrf
from app.auth.passwords import generate_temp_password, hash_password, policy_errors, verify_password
from app.config import ConfigError, Settings, dev_login_enabled, lint_env_file, load_config, validate_runtime
from app.mail.mailer import Mailer, OutgoingMail, Recipient
from tests.conftest import ROOT, make_loaded


def _docx(extra: dict[str, bytes] | None = None, size: int = 100) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "x" * size)
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


def test_passwords() -> None:
    h = hash_password("Một-mật-khẩu-dài-123")
    assert verify_password(h, "Một-mật-khẩu-dài-123") == (True, False)
    assert verify_password(h, "sai")[0] is False
    assert policy_errors("short")
    assert policy_errors("password123")  # quá phổ biến
    assert policy_errors("an.nguyen2026!!", email="an.nguyen@gmail.com")
    assert policy_errors("Kh0ng-Ph0-Bien-2026") == []
    pw = generate_temp_password()
    assert len(pw) == 14 and not policy_errors(pw)


def test_csrf_bound_to_session() -> None:
    tok = csrf.make_token("secret", "sess1")
    assert csrf.check("secret", "sess1", tok, tok)
    assert not csrf.check("secret", "sess2", tok, tok)  # token của session khác
    assert not csrf.check("secret", "sess1", tok, None)
    assert not csrf.check("secret", "sess1", "a", "a")


def test_upload_inspection() -> None:
    assert inspect_file(b"%PDF-1.7 ...", "cv.pdf", ["pdf", "docx"], 10)[0] == "pdf"
    assert inspect_file(_docx(), "don.docx", ["pdf", "docx"], 10)[0] == "docx"
    with pytest.raises(UploadRejected):
        inspect_file(b"MZ\x90\x00", "cv.pdf", ["pdf"], 10)  # sai magic bytes
    with pytest.raises(UploadRejected):
        inspect_file(b"%PDF-1.7", "cv.exe", ["pdf"], 10)
    with pytest.raises(UploadRejected, match="macro"):
        inspect_file(_docx({"word/vbaProject.bin": b"x"}), "d.docx", ["docx"], 10)
    with pytest.raises(UploadRejected, match="nhúng"):
        inspect_file(_docx({"word/embeddings/oleObject1.bin": b"x"}), "d.docx", ["docx"], 10)
    with pytest.raises(UploadRejected, match="giải nén"):
        inspect_file(_docx(size=70 * 1024 * 1024), "bomb.docx", ["docx"], 10)  # zip-bomb
    with pytest.raises(UploadRejected, match="vượt"):
        inspect_file(b"%PDF-" + b"0" * (2 * 1024 * 1024), "big.pdf", ["pdf"], 1)


def test_production_refuses_dev_login() -> None:
    loaded = make_loaded()
    assert dev_login_enabled(loaded.settings, loaded.config)
    prod = Settings(_env_file=None, app_env="production", app_secret_key="x" * 48)  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        validate_runtime(prod, loaded.config)
    assert not dev_login_enabled(prod, loaded.config)


def test_env_overrides_and_sources() -> None:
    s = Settings(_env_file=None, llm_model_member="claude-sonnet-5-5", embedding_dimensions=1024)  # type: ignore[call-arg]
    loaded = load_config(s, ROOT / "config.example.yaml")
    assert loaded.config.llm.profiles["member_agent"].model == "claude-sonnet-5-5"
    assert loaded.sources["llm.profiles.member_agent.model"] == "env"
    assert loaded.sources["llm.profiles.guest_agent.model"] == "yaml"
    assert loaded.sources["security.cookie_secure"] in {"yaml", "default"}
    assert loaded.config.qdrant_collection == "kb_isilab_te3l_1024"
    bad = Settings(_env_file=None, llm_model_member="model-khong-co")  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        load_config(bad, ROOT / "config.example.yaml")


def test_lint_env(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / ".env"
    p.write_text("A=1\nB = 2\nC=3 # chú thích\n# ok\n", encoding="utf-8")
    w = lint_env_file(p)
    assert any("B" in x and "dấu cách" in x for x in w) and any("C" in x and "chú thích" in x for x in w)


async def test_mailer_test_mode() -> None:
    cfg = make_loaded().config

    class G:
        sent: list = []  # type: ignore[type-arg]  # noqa: RUF012

        async def send(self, m):  # type: ignore[no-untyped-def]
            self.sent.append(m)
            return "id"

    g = G()
    out = await Mailer(g, cfg).send(
        OutgoingMail([Recipient("an@gmail.com", "Lê Văn An")], "Nhắc lịch", "Nội dung")
    )
    assert out.subject == "[TEST] Nhắc lịch"
    assert g.sent[0].get_content().startswith("Người nhận dự kiến: Lê Văn An")
    with pytest.raises(ValueError):
        await Mailer(g, cfg).send(OutgoingMail([Recipient("không-hợp-lệ")], "x", "y"))
