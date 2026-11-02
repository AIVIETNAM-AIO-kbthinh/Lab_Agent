"""Validate + chuẩn hoá giá trị trường (TK §7.1). Hàm thuần: dữ liệu sống (Members, thiết bị) truyền vào qua VContext.

`live=True` ở bước xác nhận nghĩa là VContext được dựng từ dữ liệu vừa đọc lại (số thiết bị còn lại, thành viên).
"""

from __future__ import annotations

import operator
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.domain.dates import fmt_date, parse_sheet_date, resolve_time_expr
from app.domain.equipment import Device, EquipmentSnapshot
from app.domain.members import MembersSnapshot
from app.domain.names import ascii_key, fuzzy_match, norm_name, normalize_phone, valid_email, valid_phone
from app.forms.schema import ColumnSpec, FieldSpec, FormSchema

DATE_FMT = "%d/%m/%Y"


@dataclass
class Issue:
    key: str  # "ho_ten" hoặc "thiet_bi[2].so_luong"
    label: str
    message: str
    options: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"field": self.key, "label": self.label, "error": self.message}
        if self.options:
            d["options"] = self.options
        return d


@dataclass
class VContext:
    today: date
    allowed_domains: list[str]
    members: MembersSnapshot | None = None
    equipment: EquipmentSnapshot | None = None
    advisor_name: str | None = None


@dataclass
class Normalized:
    value: Any = None
    error: str | None = None
    options: list[str] = field(default_factory=list)
    note: str | None = None  # giá trị đã được chuẩn hoá khác với lời người dùng (để LLM nói lại)
    unknown: list[str] = field(default_factory=list)  # tên ngoài lab (allow_unknown) — bản xem trước đánh dấu
    device: Device | None = None


def empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


# ---------------------------------------------------------------- từng kiểu


def parse_date_input(raw: Any, today: date) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"^(ngày|ngay)\s+", "", s, flags=re.I)
    m = re.fullmatch(r"(\d{1,2})\s*(?:tháng|thang)\s*(\d{1,2})(?:\s*(?:năm|nam)\s*(\d{4}))?", s, flags=re.I)
    if m:
        s = f"{m.group(1)}/{m.group(2)}/{m.group(3) or today.year}"
    if re.fullmatch(r"\d{1,2}[/.\-]\d{1,2}", s):
        s = f"{s}/{today.year}"
    d = parse_sheet_date(s) if re.search(r"\d", s) else None
    if d is None:
        rng = resolve_time_expr(s, today)
        if rng and rng[0] == rng[1]:
            d = rng[0]
    return d


_TIME = re.compile(
    r"^(\d{1,2})\s*(?:[:hg]|giờ|gio)?\s*(\d{1,2})?\s*(?:phút|phut|p)?\s*(sáng|sang|chiều|chieu|tối|toi|am|pm)?$",
    re.I,
)


def parse_time_input(raw: Any) -> str | None:
    s = str(raw).strip().lower()
    m = _TIME.match(s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    suffix = ascii_key(m.group(3) or "")
    if suffix in {"chieu", "toi", "pm"} and h < 12:
        h += 12
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


def _split_names(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,;\n]|\s+và\s+|\s+&\s+", str(raw))
    return [re.sub(r"\s+", " ", x).strip(" .-•\t") for x in items if x and x.strip(" .-•\t")]


def resolve_member(name: str, vc: VContext, *, allow_admin: bool) -> tuple[str | None, list[str]]:
    """(tên chuẩn, các lựa chọn nếu mơ hồ). Không thấy → (None, [])."""
    cands = [m.ho_ten for m in (vc.members.active if vc.members else [])]
    if allow_admin and vc.advisor_name:
        cands.append(vc.advisor_name)
        # "thầy Khoa", "Thầy Nguyễn Đăng Khoa"
        bare = re.sub(r"^(thầy|thay|cô|co|ts\.?|pgs\.?|gs\.?)\s+", "", name.strip(), flags=re.I)
        words = set(norm_name(vc.advisor_name).split())
        if bare != name.strip() and bare and set(norm_name(bare).split()) <= words:
            return vc.advisor_name, []
    # khớp NGUYÊN TỪ trước ("An" → "Lê Văn An", không lẫn với "Trần"); nhiều người cùng chứa các từ đó → mơ hồ
    qwords = ascii_key(name).split()
    whole = [c for c in cands if qwords and set(qwords) <= set(ascii_key(c).split())]
    exact = [c for c in whole if norm_name(c) == norm_name(name)]
    if exact:
        return exact[0], []
    if len(whole) == 1:
        return whole[0], []
    if len(whole) > 1:
        return None, whole[:5]
    hits = fuzzy_match(name, cands, limit=5, cutoff=72)
    if not hits:
        return None, []
    top_name, top = hits[0]
    if top >= 100:
        return top_name, []
    close = [h for h, s in hits if s >= top - 5]
    if len(close) > 1:
        return None, close
    if top >= 85:
        return top_name, []
    return None, [h for h, _ in hits[:3]]


def resolve_device(raw: str, vc: VContext) -> tuple[Device | None, list[str]]:
    snap = vc.equipment
    if snap is None:
        return None, []
    d = snap.device(raw)
    if d:
        return d, []
    hits = snap.search(raw, limit=5)
    if len(hits) == 1:
        return hits[0], []
    exact = [x for x in hits if norm_name(x.ten_tb) == norm_name(raw)]
    if len(exact) == 1:
        return exact[0], []
    return None, [f"{x.ten_tb} ({x.ma_tb})" for x in hits]


def normalize_value(spec: ColumnSpec, raw: Any, vc: VContext) -> Normalized:
    """Chuẩn hoá MỘT giá trị (chưa xét ràng buộc giữa các trường)."""
    if empty(raw):
        return Normalized(None)
    t = spec.type
    if t in {"text", "longtext"}:
        s = str(raw).strip() if t == "longtext" else re.sub(r"\s+", " ", str(raw)).strip()
        if spec.max_length and len(s) > spec.max_length:
            return Normalized(error=f"Dài quá {spec.max_length} ký tự.")
        return Normalized(s)
    if t == "phone":
        p = normalize_phone(str(raw))
        if not valid_phone(p):
            return Normalized(error="Số điện thoại phải gồm 10 chữ số, bắt đầu bằng 0 (hoặc +84).")
        return Normalized(p, note=f"chuẩn hoá thành {p}" if p != str(raw).strip() else None)
    if t == "email":
        e = str(raw).strip()
        if not valid_email(e):
            return Normalized(error="Email không đúng định dạng.")
        if not valid_email(e, vc.allowed_domains):
            return Normalized(error=f"Email phải thuộc tên miền: {', '.join(vc.allowed_domains)}.")
        return Normalized(e)
    if t == "date":
        d = parse_date_input(raw, vc.today)
        if d is None:
            return Normalized(error="Ngày không hợp lệ, dùng dạng dd/mm/yyyy.")
        ds = fmt_date(d, DATE_FMT)
        return Normalized(ds, note=f"hiểu là {ds}" if ds != str(raw).strip() else None)
    if t == "time":
        tm = parse_time_input(raw)
        if tm is None:
            return Normalized(error="Giờ không hợp lệ, dùng dạng HH:MM (vd 14:30).")
        return Normalized(tm)
    if t == "int":
        try:
            num = int(str(raw).strip())
        except ValueError:
            return Normalized(error="Phải là số nguyên.")
        if spec.min is not None and num < spec.min:
            return Normalized(error=f"Tối thiểu là {spec.min}.")
        if spec.max is not None and num > spec.max:
            return Normalized(error=f"Tối đa là {spec.max}.")
        return Normalized(num)
    if t == "choice":
        s = str(raw).strip()
        for o in spec.options:
            if ascii_key(o) == ascii_key(s):
                return Normalized(o)
        hits = fuzzy_match(s, spec.options, limit=3, cutoff=80)
        if len(hits) == 1:
            return Normalized(hits[0][0], note=f"hiểu là '{hits[0][0]}'")
        return Normalized(error="Chọn một trong các giá trị cho phép.", options=list(spec.options))
    if t == "member_ref":
        names = _split_names(raw) if spec.multiple else [str(raw).strip()]
        out: list[str] = []
        unknown: list[str] = []
        notes: list[str] = []
        for n in names:
            canon, options = resolve_member(n, vc, allow_admin=spec.allow_admin)
            if canon:
                if norm_name(canon) != norm_name(n):
                    notes.append(f"'{n}' → {canon}")
                if canon not in out:
                    out.append(canon)
            elif options:
                return Normalized(error=f"'{n}' khớp nhiều người, hãy chọn một.", options=options)
            elif spec.allow_unknown:
                out.append(n)
                unknown.append(n)
            else:
                return Normalized(error=f"Không tìm thấy '{n}' trong danh sách thành viên.")
        value: Any = out if spec.multiple else (out[0] if out else None)
        return Normalized(value, note="; ".join(notes) or None, unknown=unknown)
    if t == "equipment_ref":
        s = str(raw).strip()
        dev, options = resolve_device(s, vc)
        if dev is None:
            if options:
                return Normalized(error=f"'{s}' khớp nhiều thiết bị, hãy chọn một.", options=options)
            return Normalized(error=f"Không tìm thấy thiết bị '{s}'.")
        snap = vc.equipment
        if snap is not None and dev.tinh_trang in snap.not_borrowable:
            return Normalized(
                error=f"{dev.ten_tb} ({dev.ma_tb}) đang ở trạng thái '{dev.tinh_trang}', không cho mượn."
            )
        note = f"'{s}' → {dev.ten_tb} ({dev.ma_tb})" if norm_name(s) != norm_name(dev.ten_tb) else None
        return Normalized(dev.ten_tb, note=note, device=dev)
    if t == "row_number":
        return Normalized(None)
    return Normalized(error=f"Kiểu trường không hỗ trợ: {t}")


# ---------------------------------------------------------------- bảng


def normalize_table(
    f: FieldSpec, raw_rows: Any, vc: VContext
) -> tuple[list[dict[str, Any]], list[Issue], list[str]]:
    """Chuẩn hoá cả bảng; tự điền cột derived và STT. Trả (rows, issues, notes)."""
    rows_out: list[dict[str, Any]] = []
    issues: list[Issue] = []
    notes: list[str] = []
    if not isinstance(raw_rows, list):
        return [], [Issue(f.key, f.label, "Bảng phải là danh sách các dòng.")], []
    if len(raw_rows) > f.max_rows:
        return [], [Issue(f.key, f.label, f"Tối đa {f.max_rows} dòng.")], []
    for i, raw in enumerate(raw_rows, start=1):
        raw = raw if isinstance(raw, dict) else {}
        row: dict[str, Any] = {}
        devices: dict[str, Device] = {}
        for c in f.columns:
            if c.derived:
                continue
            v = raw.get(c.key)
            if empty(v) and c.default is not None:
                v = c.default
            n = normalize_value(c, v, vc)
            label = f"{f.label} – dòng {i} – {c.label}"
            if n.error:
                issues.append(Issue(f"{f.key}[{i}].{c.key}", label, n.error, n.options))
                row[c.key] = None
                continue
            row[c.key] = n.value
            if n.device:
                devices[c.key] = n.device
            if n.note:
                notes.append(f"{label}: {n.note}")
        for c in f.columns:
            if c.type == "row_number":
                row[c.key] = i
            elif c.derived_from:
                base, _, attr = c.derived_from.partition(".")
                d = devices.get(base)
                row[c.key] = getattr(d, attr) if d else None
        rows_out.append(row)
    return rows_out, issues, notes


# ---------------------------------------------------------------- kiểm tra toàn bộ bản nháp


def _as_date(v: Any) -> date | None:
    return parse_sheet_date(v) if isinstance(v, str) and v else None


def _relation_issues(
    key: str,
    label: str,
    spec: ColumnSpec,
    value: Any,
    fields: dict[str, Any],
    schema: FormSchema,
    today: date,
) -> list[Issue]:
    out: list[Issue] = []

    def ref(name: str) -> tuple[Any, str]:
        if name == "today":
            return (fmt_date(today, DATE_FMT) if spec.type == "date" else None), "hôm nay"
        other = schema.field(name)
        return fields.get(name), (other.label.lower() if other else name)

    checks: tuple[tuple[str | None, Callable[[Any, Any], bool], str], ...] = (
        (spec.not_before, operator.ge, "không được trước"),
        (spec.not_after, operator.le, "không được sau"),
        (spec.after, operator.gt, "phải sau"),
        (spec.before, operator.lt, "phải trước"),
    )
    for target, ok, phrase in checks:
        if not target or empty(value):
            continue
        other, other_label = ref(target)
        if empty(other):
            continue
        a: Any
        b: Any
        if spec.type == "date":
            a, b = _as_date(value), _as_date(other)
        else:  # time "HH:MM" so sánh chuỗi được
            a, b = str(value), str(other)
        if a is None or b is None:
            continue
        if not ok(a, b):
            out.append(Issue(key, label, f"{label} {phrase} {other_label} ({other})."))
    return out


def check_draft(
    schema: FormSchema, fields: dict[str, Any], attachments: dict[str, Any], vc: VContext
) -> list[Issue]:
    """Thiếu trường bắt buộc, ràng buộc ngày/giờ, quy tắc bảng, đính kèm. Trả danh sách lỗi (rỗng = hợp lệ)."""
    issues: list[Issue] = []
    for f in schema.fields:
        if f.filled_by == "lab":
            continue
        if f.type == "attachment":
            if f.required and f.key not in attachments:
                issues.append(Issue(f.key, f.label, "Chưa có file đính kèm."))
            continue
        v = fields.get(f.key)
        if f.type == "table":
            rows = v or []
            if len(rows) < f.min_rows:
                issues.append(Issue(f.key, f.label, f"Cần ít nhất {f.min_rows} dòng."))
            for i, row in enumerate(rows, start=1):
                for c in f.columns:
                    lbl = f"{f.label} – dòng {i} – {c.label}"
                    cv = row.get(c.key)
                    if c.required and not c.derived and empty(cv):
                        issues.append(Issue(f"{f.key}[{i}].{c.key}", lbl, "Còn trống."))
                    issues.extend(
                        _relation_issues(f"{f.key}[{i}].{c.key}", lbl, c, cv, fields, schema, vc.today)
                    )
            if "so_luong_khong_vuot_con_lai" in f.rules:
                issues.extend(_rule_availability(f, rows, vc))
            continue
        if empty(v):
            if f.required:
                issues.append(Issue(f.key, f.label, "Còn thiếu."))
            continue
        issues.extend(_relation_issues(f.key, f.label, f, v, fields, schema, vc.today))
        if f.suggest and f.suggest.startswith("members_except:") and isinstance(v, list):
            # vd Vắng mặt ∩ Tham dự phải rỗng
            ref = f.suggest.split(":", 1)[1]
            other = fields.get(ref) or []
            both = [n for n in v if norm_name(n) in {norm_name(x) for x in other}]
            if both:
                ref_label = schema.field(ref).label if schema.field(ref) else ref  # type: ignore[union-attr]
                issues.append(
                    Issue(
                        f.key,
                        f.label,
                        f"{', '.join(both)} vừa có trong '{ref_label}' vừa có trong '{f.label}'.",
                    )
                )
    return issues


def _rule_availability(f: FieldSpec, rows: list[dict[str, Any]], vc: VContext) -> list[Issue]:
    """Số lượng không vượt số còn lại, cộng dồn theo ma_tb (một thiết bị có thể xuất hiện nhiều dòng)."""
    snap = vc.equipment
    if snap is None:
        return []
    need: dict[str, int] = defaultdict(int)
    first_row: dict[str, int] = {}
    for i, row in enumerate(rows, start=1):
        ma = str(row.get("ma_tb") or "")
        if not ma or not isinstance(row.get("so_luong"), int):
            continue
        need[ma.upper()] += row["so_luong"]
        first_row.setdefault(ma.upper(), i)
    out: list[Issue] = []
    for ma, qty in need.items():
        av = snap.availability(ma)
        if av is None:
            out.append(
                Issue(
                    f"{f.key}[{first_row[ma]}].ten_tb", f.label, f"Thiết bị {ma} không còn trong danh sách."
                )
            )
            continue
        if av.device.tinh_trang in snap.not_borrowable:
            out.append(
                Issue(
                    f"{f.key}[{first_row[ma]}].ten_tb",
                    f.label,
                    f"{av.device.ten_tb} ({ma}) đang '{av.device.tinh_trang}', không cho mượn.",
                )
            )
        elif qty > av.con_lai:
            out.append(
                Issue(
                    f"{f.key}[{first_row[ma]}].so_luong",
                    f"{f.label} – {av.device.ten_tb}",
                    f"{av.device.ten_tb} ({ma}) chỉ còn {av.con_lai} (đang yêu cầu {qty}).",
                )
            )
    return out


def revalidate_refs(schema: FormSchema, fields: dict[str, Any], vc: VContext) -> list[Issue]:
    """Kiểm tra lại các tham chiếu thay đổi theo thời gian (thành viên, thiết bị) với dữ liệu vừa đọc."""
    issues: list[Issue] = []
    for f in schema.data_fields():
        v = fields.get(f.key)
        if empty(v):
            continue
        if f.type == "member_ref" and f.filled_by == "user":
            names = v if isinstance(v, list) else [v]
            for n in names:
                canon, _ = resolve_member(str(n), vc, allow_admin=f.allow_admin)
                if canon is None and not f.allow_unknown:
                    issues.append(Issue(f.key, f.label, f"'{n}' không còn trong danh sách thành viên."))
        if f.type == "table":
            for i, row in enumerate(v or [], start=1):
                for c in f.columns:
                    cv = row.get(c.key)
                    if empty(cv):
                        continue
                    if c.type == "member_ref":
                        canon, _ = resolve_member(str(cv), vc, allow_admin=c.allow_admin)
                        if canon is None and not c.allow_unknown:
                            issues.append(
                                Issue(f"{f.key}[{i}].{c.key}", f.label, f"'{cv}' không còn là thành viên.")
                            )
                    if c.type == "equipment_ref":
                        n = normalize_value(c, row.get("ma_tb") or cv, vc)
                        if n.error:
                            issues.append(Issue(f"{f.key}[{i}].{c.key}", f.label, n.error))
    return issues
