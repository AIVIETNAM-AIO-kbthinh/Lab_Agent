"""So sánh schema tự sinh với schema viết tay (tiêu chí P4: "schema sinh từ 3 đơn ban đầu tương đương schemas/").

Ghép trường theo vị trí trong đơn (anchor + kiểu ô), không theo key: key do người viết tay đặt tuỳ ý.
text và longtext coi là cùng kiểu (chỉ khác cỡ ô nhập; giá trị đều là chuỗi).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.names import ascii_key
from app.forms.schema import FieldSpec, FormSchema
from app.forms.schema_gen.blanks import canonical


def _pos(f: FieldSpec) -> str:
    return ascii_key(canonical(f.anchor or "")) + "|" + ("table" if f.type == "table" else f.blank)


@dataclass
class Diff:
    matched: int = 0
    missing: list[str] = field(default_factory=list)  # có trong bản viết tay, bản sinh không có
    extra: list[str] = field(default_factory=list)  # bản sinh có thêm
    mismatches: list[str] = field(default_factory=list)  # cùng vị trí nhưng khác kiểu / ai điền / số cột

    @property
    def equivalent(self) -> bool:
        return not self.missing and not self.extra and not self.mismatches

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "equivalent": self.equivalent}


def compare(gen: FormSchema, ref: FormSchema, *, check_types: bool = True) -> Diff:
    d = Diff()
    ref_fields = [f for f in ref.fields if f.anchor]
    gen_by = {_pos(f): f for f in gen.fields if f.anchor}
    for rf in ref_fields:
        gf = gen_by.pop(_pos(rf), None)
        if gf is None:
            d.missing.append(f"{rf.key} ('{rf.anchor}', {rf.blank})")
            continue
        d.matched += 1
        probs = []
        if check_types and gf.type != rf.type and {gf.type, rf.type} != {"text", "longtext"}:
            probs.append(f"kiểu {gf.type} ≠ {rf.type}")
        if gf.filled_by != rf.filled_by and not (rf.locked and gf.locked):
            probs.append(f"ai điền {gf.filled_by} ≠ {rf.filled_by}")
        if rf.type == "table" and len(gf.columns) != len(rf.columns):
            probs.append(f"{len(gf.columns)} cột ≠ {len(rf.columns)}")
        if probs:
            d.mismatches.append(f"{rf.key}: " + ", ".join(probs))
    # trường lab ghi tay trong bản viết tay thường không có anchor: không tính là thừa
    lab_labels = {ascii_key(f.label) for f in ref.fields if not f.anchor}
    d.extra = [
        f"{f.key} ('{f.anchor}')"
        for f in gen_by.values()
        if not (f.filled_by == "lab" and ascii_key(f.label) in lab_labels)
    ]
    sig_ref = {ascii_key(s.label): bool(s.field) for s in ref.signatures}
    sig_gen = {ascii_key(s.label): bool(s.field) for s in gen.signatures}
    if sig_ref != sig_gen:
        d.mismatches.append(f"chữ ký {sig_gen} ≠ {sig_ref}")
    return d
