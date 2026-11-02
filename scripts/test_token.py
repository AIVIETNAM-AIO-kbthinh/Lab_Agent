"""Thử token Google: liệt kê file trong thư mục gốc của lab. Kiểm tra đầy đủ hơn: `labagent preflight`."""

from googleapiclient.discovery import build

from app.config import get_loaded
from app.google.oauth import load_credentials

loaded = get_loaded()
creds = load_credentials(loaded.settings.google_token_file)
drive = build("drive", "v3", credentials=creds)
res = (
    drive.files()
    .list(
        q=f"'{loaded.config.drive.root_folder_id}' in parents and trashed = false",
        fields="files(name, mimeType)",
    )
    .execute()
)
print([f["name"] for f in res["files"]])
