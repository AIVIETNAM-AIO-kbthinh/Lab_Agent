from pathlib import Path

import yaml

from tests.eval.run_eval import grade

Q = yaml.safe_load((Path(__file__).parents[1] / "eval" / "questions.yaml").read_text(encoding="utf-8"))


def test_question_bank_shape() -> None:
    assert len(Q) == 30
    assert sum(1 for q in Q if q["id"].startswith("web")) == 10
    assert len({q["id"] for q in Q}) == 30
    assert {q["role"] for q in Q} <= {"guest", "member", "leader"}


def test_grader() -> None:
    q = next(x for x in Q if x["id"] == "web03")
    assert grade(q, "Lab ở 1 Phan Tây Nhạc, Hà Nội.", [("search_kb", {"query": "địa chỉ"})])[0]
    assert not grade(q, "Lab ở Hà Nội.", [("search_kb", {})])[0]
    assert not grade(q, "1 Phan Tây Nhạc", [])[0]  # không tra cứu
    leak = next(x for x in Q if x["id"] == "tk04")
    assert not grade(leak, "SĐT: 0911111111", [])[0]
    assert grade(leak, "Mình không thể cung cấp thông tin này.", [])[0]
    args = next(x for x in Q if x["id"] == "tk06")
    assert grade(
        args, "Tuần trước họp về UAV.", [("search_kb", {"doc_type": "minutes", "time_expr": "tuần trước"})]
    )[0]
    assert not grade(args, "x", [("search_kb", {"doc_type": None, "time_expr": None})])[0]
