"""Gộp ô tìm bằng luật + kết quả LLM thành FormSchema (cùng định dạng schema viết tay trong `schemas/`).

- Vị trí ô (anchor, blank) luôn lấy từ luật hoặc được kiểm tra lại trên tài liệu; LLM chỉ quyết định nhãn, key,
  kiểu, bắt buộc, ai điền, lựa chọn.
- Ô luật tìm được mà LLM không nhắc tới (và không bảo bỏ) vẫn được giữ, kèm cảnh báo.
- Không có LLM (lỗi/tắt): schema chỉ dựa trên luật.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.domain.names import ascii_key, to_pascal_ascii
from app.forms.placeholder import anchor_regex
from app.forms.schema import ACCOUNT_SOURCES, FormSchema
from app.forms.schema_gen.blanks import Candidate, Detection, canonical
from app.forms.schema_gen.llm_sweep import FieldOut, FormAnalysis
from app.forms.schema_gen.types import (
    ACCOUNT_KEYS,
    RESERVED,
    infer_column,
    infer_field,
    key_from_label,
    std_key,
)

ROLES_ORDER = ["guest", "member", "thu_ki", "leader", "admin"]
DEFAULT_ROLES = ["member", "thu_ki", "leader"]
SIGNER_LABELS = (
    "nguoi lam don",
    "nguoi viet don",
    "nguoi de nghi",
    "nguoi muon",
    "nguoi nop",
    "nguoi khai",
    "nguoi dang ky",
    "nguoi xin",
    "sinh vien",
    "nguoi bao cao",
)
SCALAR_OK = {
    "text",
    "longtext",
    "phone",
    "email",
    "date",
    "time",
    "int",
    "choice",
    "member_ref",
    "equipment_ref",
    "attachment",
}
UNLOCKED_TYPE = {"email": "email", "sdt": "phone"}


@dataclass
class BuildNotes:
    warnings: list[str] = field(default_factory=list)
    llm_notes: list[str] = field(default_factory=list)


def slug(s: str, max_len: int = 60) -> str:
    k = "_".join(re.findall(r"[a-z0-9]+", ascii_key(s)))[:max_len].strip("_")
    if not k or not k[0].isalpha():
        k = "don_" + k
    return k


def file_base(title: str) -> str:
    return to_pascal_ascii(title)[:60] or "MauDon"


def sanitize_key(k: str) -> str:
    k = "_".join(re.findall(r"[a-z0-9]+", ascii_key(k or "")))[:40].strip("_")
    if not k:
        return ""
    if not k[0].isalpha():
        k = "f_" + k
    return k + "_" if k in RESERVED else k


def _unique(k: str, used: set[str]) -> str:
    base, n = k, 2
    while k in used:
        k = f"{base}_{n}"
        n += 1
    used.add(k)
    return k


# ---------------------------------------------------------------- ghép LLM ↔ ô luật


def _match_new(field_out: FieldOut, det: Detection, claimed: set[str]) -> Candidate | None:
    """Ô mới LLM đề xuất (có anchor) → ô luật trên bản đã tẩy trắng cùng vị trí, hoặc ô tự tạo theo anchor."""
    if not field_out.anchor or not field_out.anchor.strip():
        return None
    rx = anchor_regex(field_out.anchor)
    fa = ascii_key(canonical(field_out.anchor))
    for pa in det.paras:
        m = rx.search(pa.text)
        if not m:
            continue
        for c in det.fields():
            if c.cid in claimed or c.para != pa.idx:
                continue
            ca = ascii_key(c.anchor)
            if fa in ca or ca in fa or c.pos >= m.end():
                return c
        if field_out.blank and field_out.type != "table":
            return Candidate(
                f"x{pa.idx}_{m.start()}",
                field_out.blank,
                pa.idx,
                m.end(),
                field_out.label,
                canonical(field_out.anchor),
            )
    return None


def _pair(
    det: Detection, analysis: FormAnalysis | None, orig: Detection | None, notes: BuildNotes
) -> list[tuple[Candidate, FieldOut | None]]:
    cands = det.fields()
    if analysis is None:
        return [(c, None) for c in cands]
    orig = orig or det
    by_key = {(c.para, ascii_key(c.anchor)): c for c in cands}
    orig_by_id = {c.cid: c for c in orig.fields()}
    claimed: set[str] = set()
    pairs: list[tuple[Candidate, FieldOut | None]] = []
    ignored: set[str] = set()
    for cid in analysis.ignore:
        oc = orig_by_id.get(cid)
        if oc is not None:
            bc = by_key.get((oc.para, ascii_key(oc.anchor)))
            if bc is not None:
                ignored.add(bc.cid)
    for fo in analysis.fields:
        c: Candidate | None = None
        if fo.candidate:
            oc = orig_by_id.get(fo.candidate.strip())
            if oc is not None:
                c = by_key.get((oc.para, ascii_key(oc.anchor)))
                if c is None:  # anchor đổi sau khi tẩy trắng: ô duy nhất còn trống trong cùng đoạn
                    same = [x for x in cands if x.para == oc.para and x.cid not in claimed]
                    c = same[0] if len(same) == 1 else None
        if c is None:
            c = _match_new(fo, det, claimed)
        if c is None or c.cid in claimed:
            if fo.filled_by != "lab":
                notes.warnings.append(
                    f"Bỏ trường '{fo.label}': không xác định được vị trí trong đơn"
                    + (f" (anchor '{fo.anchor}')" if fo.anchor else "")
                    + "."
                )
            continue
        claimed.add(c.cid)
        pairs.append((c, fo))
    for c in cands:
        if c.cid in claimed or c.cid in ignored:
            continue
        notes.warnings.append(f"Thêm trường '{c.label}' theo luật (AI không liệt kê), kiểm tra lại.")
        pairs.append((c, None))
    pairs.sort(key=lambda x: (x[0].para, x[0].pos))
    return pairs


# ---------------------------------------------------------------- trường


def _field(c: Candidate, fo: FieldOut | None, used: set[str]) -> dict[str, Any]:
    f = infer_field(
        c.label,
        c.kind,
        type_hint=c.type_hint,
        options=c.options,
        lab=c.lab,
        multiline=c.multiline,
        system_date=c.system_date,
    )
    if fo is not None:
        if fo.label.strip():
            f["label"] = fo.label.strip()
        t = fo.type
        if c.kind == "table":
            t = "table"
        elif c.kind == "checkbox":
            t = "choice"
        elif c.kind == "date_parts":
            t = "date"
        elif t not in SCALAR_OK:
            t = f["type"]
        f["type"] = t
        f["required"] = bool(fo.required)
        f["filled_by"] = "lab" if fo.filled_by == "lab" else "user"
        if c.system_date and fo.filled_by == "system":
            f.update(filled_by="system", default="today")  # ngày làm đơn = ngày nộp
        elif c.system_date and fo.filled_by == "user":
            f["default"] = "today"  # vd ngày họp: mặc định hôm nay, người dùng sửa được
        elif f.get("default") == "today":
            f.pop("default")
        if t == "choice" and c.kind != "checkbox":
            opts = [o.strip() for o in fo.options if o.strip()]
            if len(opts) >= 2:
                f["options"] = list(dict.fromkeys(opts))
            else:
                f["type"] = "text"
        if t != "choice":
            f.pop("options", None)
        if t == "member_ref":
            f["multiple"] = bool(fo.multiple)
        if fo.hint:
            f["hint"] = fo.hint.strip()[:300]
        if t == "attachment":
            f["accept"] = ["pdf", "docx"]
        if f["filled_by"] == "lab":
            f["required"] = False
            f.pop("default", None)
    if f["type"] != "member_ref":
        for k in ("multiple", "allow_unknown", "allow_admin"):
            f.pop(k, None)
        if isinstance(f.get("default"), str) and f["default"] in ("leader", "account.ho_ten"):
            f.pop("default")
    if f["type"] == "attachment":
        f["accept"] = f.get("accept") or ["pdf", "docx"]

    key = std_key(f["label"]) if f["filled_by"] != "lab" else None
    if c.system_date and f["filled_by"] == "system":
        key = "ngay_lap"
    if key is None or key in used:
        key = sanitize_key(fo.key) if fo is not None else ""
        key = key or key_from_label(f["label"])
    f["key"] = _unique(key, used)
    if fo is None and f["key"] == "ho_ten" and "ten la" in ascii_key(f["label"]):
        f["label"] = "Họ và tên"  # "Tôi tên là ……" → nhãn dễ hiểu khi hỏi người dùng
    if c.kind != "table":
        f["anchor"] = c.anchor
        f["blank"] = c.kind
    else:
        f["anchor"] = c.anchor
        f.update(min_rows=1 if f["required"] else 0, max_rows=30)
        f["columns"] = _columns(c, fo)
    return f


def _columns(c: Candidate, fo: FieldOut | None) -> list[dict[str, Any]]:
    cols: list[dict[str, Any]] = []
    used: set[str] = set()
    llm_cols = fo.columns if fo is not None and len(fo.columns) == len(c.columns) else []
    for i, label in enumerate(c.columns):
        col = infer_column(label, c.columns)
        if llm_cols and col["type"] not in ("row_number",) and not col.get("derived_from"):
            lc = llm_cols[i]
            t = lc.type if lc.type in SCALAR_OK - {"attachment", "choice"} else col["type"]
            col.update(type=t, required=bool(lc.required))
            if t in ("text", "longtext", "member_ref", "date"):
                col.pop("min", None)
                col.pop("default", None)
            k = sanitize_key(lc.key)
            if k and col["key"] not in ("ten_tb", "so_luong"):
                col["key"] = k
        col["label"] = label
        col["key"] = _unique(col["key"], used)
        cols.append(col)
    return cols


def apply_roles(fields: list[dict[str, Any]], roles: list[str]) -> None:
    """Đơn không dành cho guest ⇒ họ tên/email/SĐT/MSSV lấy từ tài khoản thành viên (khoá); có guest ⇒ mở khoá."""
    member_only = "guest" not in roles
    for f in fields:
        key = f.get("key")
        src = ACCOUNT_KEYS.get(key or "")
        if src is None or src not in ACCOUNT_SOURCES or f.get("filled_by", "user") == "lab":
            continue
        if f.get("type") in ("table", "attachment"):
            continue
        if member_only:
            f.update(source=f"account.{src}", locked=True, type="text", required=key == "ho_ten")
            f.pop("default", None)
        elif f.get("locked"):
            f.pop("source", None)
            f["locked"] = False
            f["type"] = UNLOCKED_TYPE.get(key or "", "text")
            f["required"] = True


def _signatures(
    det: Detection, analysis: FormAnalysis | None, fields: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    keys = {f["key"]: f for f in fields if f.get("filled_by", "user") != "lab"}
    by_label = {ascii_key(f["label"]): f["key"] for f in keys.values()}
    llm = {ascii_key(s.label): s.signer_key for s in (analysis.signatures if analysis else [])}
    out: list[dict[str, Any]] = []
    for c in det.signatures():
        k = ascii_key(c.label)
        signer = llm.get(k)
        if signer not in keys:
            signer = None
            if any(k.startswith(w) for w in SIGNER_LABELS) and "ho_ten" in keys:
                signer = "ho_ten"
            elif k in by_label and keys[by_label[k]].get("type") in ("text", "member_ref"):
                signer = by_label[k]
        if signer and keys[signer].get("multiple"):
            signer = None
        out.append({"label": c.label, "field": signer})
    return out


def build_schema(
    det: Detection,
    analysis: FormAnalysis | None,
    *,
    orig: Detection | None = None,
    filename: str = "",
    roles: list[str] | None = None,
    title: str | None = None,
    form_id: str | None = None,
    today: date,
    templates_folder: str = "templates",
    submissions_folder: str = "submissions",
    default_roles: list[str] | None = None,
) -> tuple[FormSchema, BuildNotes]:
    notes = BuildNotes()
    if analysis is not None:
        notes.llm_notes = [n for n in analysis.notes if n.strip()][:10]
    pairs = _pair(det, analysis, orig, notes)
    used: set[str] = set()
    fields = [_field(c, fo, used) for c, fo in pairs]
    # nhãn trùng (vd "Họ và tên:" ở mục người đề nghị và mục người nhận) → thêm tên mục để phân biệt
    seen_labels: dict[str, int] = {}
    for f in fields:
        seen_labels[ascii_key(f["label"])] = seen_labels.get(ascii_key(f["label"]), 0) + 1
    for (c, _), f in zip(pairs, fields, strict=True):
        if (
            seen_labels[ascii_key(f["label"])] > 1
            and c.section
            and ascii_key(c.section) not in ascii_key(f["label"])
        ):
            f["label"] = f"{f['label']} ({c.section[:1].lower() + c.section[1:]})"

    # vắng mặt = thành viên trừ người tham dự (đề xuất, như biên bản họp viết tay)
    present = next(
        (f["key"] for f in fields if f.get("multiple") and "tham du" in ascii_key(f["label"])), None
    )
    for f in fields:
        if present and f.get("type") == "member_ref" and "vang mat" in ascii_key(f["label"]):
            f["suggest"] = f"members_except:{present}"

    title = (
        title or (analysis.title if analysis else None) or det.title or filename.rsplit(".", 1)[0]
    ).strip()
    title = title[:1].upper() + title[1:]
    chosen_roles = roles or (
        list(analysis.suggested_roles) if analysis and analysis.suggested_roles else None
    )
    chosen_roles = [r for r in ROLES_ORDER if r in set(chosen_roles or default_roles or DEFAULT_ROLES)]
    apply_roles(fields, chosen_roles)
    fid = slug(form_id or title)
    base = file_base(title)
    description = (analysis.description.strip() if analysis else "") or f"{title} (mẫu đơn do admin thêm)."
    raw: dict[str, Any] = {
        "form_id": fid,
        "version": today.isoformat(),
        "title": title,
        "description": description[:500],
        "roles": chosen_roles,
        "hook": None,
        "template": {"drive_path": f"{templates_folder.strip('/')}/{base}.docx"},
        "fields": fields,
        "signatures": _signatures(det, analysis, fields),
        "output": {
            "format": "pdf",
            "folder": f"{submissions_folder.strip('/')}/{fid}/{{today:%Y-%m}}",
            "filename": "",
            "versioning": True,
        },
    }
    raw["output"]["filename"] = output_filename(raw)
    return FormSchema.model_validate(raw), notes


def output_filename(raw: dict[str, Any]) -> str:
    """Tên file đơn nộp: <ngày>_<HoTen hoặc mã nộp>_<TenMau> (tên mẫu theo file mẫu trắng)."""
    base = raw["template"]["drive_path"].rsplit("/", 1)[-1].removesuffix(".docx")
    who = "{ho_ten_ascii}" if any(f["key"] == "ho_ten" for f in raw["fields"]) else "{submission_id}"
    return f"{{today:%Y-%m-%d}}_{who}_{base}"


def schema_without(schema: FormSchema, drop_keys: set[str], unsign: set[str]) -> FormSchema:
    """Bỏ các trường không đặt được vào mẫu (và mọi tham chiếu tới chúng); chữ ký không đặt được → lab ký tay."""
    raw = schema.dump()
    raw["fields"] = [f for f in raw["fields"] if f["key"] not in drop_keys]
    for f in raw["fields"]:
        for rel in ("not_before", "not_after", "after", "before"):
            if f.get(rel) in drop_keys:
                f[rel] = None
        if f.get("suggest") and f["suggest"].partition(":")[2] in drop_keys:
            f["suggest"] = None
    for s in raw["signatures"]:
        if s["field"] in drop_keys or s["label"] in unsign:
            s["field"] = None
    raw["output"]["attachment_names"] = {
        k: v for k, v in raw["output"].get("attachment_names", {}).items() if k not in drop_keys
    }
    return FormSchema.model_validate(raw)
