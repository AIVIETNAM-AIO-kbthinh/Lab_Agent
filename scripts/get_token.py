"""Sinh refresh token OAuth cho tài khoản Gmail của lab.

Đọc GOOGLE_CLIENT_SECRETS_FILE, ghi GOOGLE_TOKEN_FILE (mặc định secrets/). Chạy: uv run python scripts/get_token.py
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import Settings
from app.google.oauth import SCOPES

s = Settings()
flow = InstalledAppFlow.from_client_secrets_file(s.google_client_secrets_file, SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
out = Path(s.google_token_file)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(creds.to_json(), encoding="utf-8")
print(f"Đã lưu {out}. Có refresh token:", bool(creds.refresh_token))
