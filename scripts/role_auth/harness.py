"""Bộ chạy kịch bản chat theo role (auth_bypass): mỗi role 1 session, mỗi prompt 1 hội thoại mới."""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.environ.get("ROLE_AUTH_BASE", "http://127.0.0.1:8000")


class RoleClient:
    def __init__(self, role: str) -> None:
        self.role = role
        self.c = httpx.Client(base_url=BASE, timeout=httpx.Timeout(300, connect=10))
        me = self.c.get("/auth/me").json()
        self.csrf = me["csrf"]
        r = self.c.post("/auth/dev-role", json={"role": role}, headers=self.h())
        r.raise_for_status()
        me = self.c.get("/auth/me").json()
        self.csrf = me["csrf"]
        self.me = me
        assert me["role"] == role, me

    def h(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    def set_role(self, role: str) -> None:
        self.c.post("/auth/dev-role", json={"role": role}, headers=self.h()).raise_for_status()
        self.role = role

    def chat(self, message: str, conversation_id: str | None = None) -> dict:
        tools: list[tuple[str, str]] = []
        text: list[str] = []
        ui: list[dict] = []
        errors: list[str] = []
        conv = conversation_id
        with self.c.stream(
            "POST", "/chat", json={"message": message, "conversation_id": conversation_id}, headers=self.h()
        ) as r:
            if r.status_code != 200:
                r.read()
                return {"http": r.status_code, "body": r.text, "tools": [], "text": "", "ui": []}
            ev_type = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    ev_type = line[6:].strip()
                elif line.startswith("data:"):
                    d = json.loads(line[5:].strip())
                    t = d.get("type", ev_type)
                    if t == "text":
                        text.append(d["text"])
                    elif t == "reset":
                        text.clear()
                    elif t == "tool" and d["status"] != "start":
                        tools.append((d["name"], d["status"]))
                    elif t == "ui_action":
                        ui.append(d["action"])
                    elif t == "error":
                        errors.append(d["message"])
                    elif t in ("conversation", "done"):
                        conv = d.get("id") or d.get("conversation_id") or conv
        return {"http": 200, "tools": tools, "text": "".join(text), "ui": ui, "errors": errors, "conv": conv}


sys.stdout.reconfigure(encoding="utf-8")

if __name__ == "__main__":
    rc = RoleClient(sys.argv[1])
    print(json.dumps(rc.me, ensure_ascii=False))
    print(json.dumps(rc.chat(sys.argv[2]), ensure_ascii=False, indent=1))
