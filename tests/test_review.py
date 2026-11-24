import csv

import pytest

from skillloop.core.models import Case, CaseResult, EvalReport, Judgment, Trajectory
from skillloop.core.project import Project
from skillloop.eval import review


@pytest.fixture
def project(tmp_path):
    cases = [Case(id=f"c{i}", input=f"问题 {i}", expected=f"a{i}") for i in range(4)]
    p = Project.init(tmp_path / "proj", "demo", cases=cases)
    yield p
    p.close()


def make_report():
    rep = EvalReport(skill_version="v001")
    spec = [("c0", True, 1.0, False), ("c1", False, 0.0, False), ("c2", True, 0.8, True), ("c3", False, 0.3, False)]
    for cid, passed, score, nr in spec:
        rep.results.append(
            CaseResult(
                case_id=cid,
                trajectory=Trajectory(case_id=cid, output=f"out {cid}"),
                judgment=Judgment(
                    score=score, passed=passed, reason=f"r {cid}", judge="llm", needs_review=nr, labels=["l1", "l2"]
                ),
            )
        )
    return rep


def test_enqueue_modes(project):
    rep = make_report()
    assert review.enqueue_reviews(project, rep) == 1
    assert [r["case_id"] for r in project.store.reviews(eval_id=rep.id)] == ["c2"]
    assert review.enqueue_reviews(project, rep, mode="failures") == 3
    assert review.enqueue_reviews(project, rep, mode="all") == 4
    assert len(project.store.reviews(eval_id=rep.id, status="pending")) == 4
    rep2 = make_report()
    assert review.enqueue_reviews(project, rep2, sample_rate=1.0) == 4
    n = review.enqueue_reviews(project, make_report(), sample_rate=0.5, seed=3)
    assert 1 <= n <= 4
    with pytest.raises(ValueError):
        review.enqueue_reviews(project, rep, mode="nope")


def test_sheet_roundtrip_apply_and_agreement(project):
    rep = make_report()
    path = review.export_review_sheet(project, rep, "outputs/review.csv")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == review.COLUMNS and len(rows) == 4
    assert rows[0]["input"] == "问题 0" and rows[0]["expected"] == "a0" and rows[0]["labels"] == "l1;l2"
    assert rows[1]["auto_passed"] == "false" and rows[0]["human_score"] == ""

    rows[0].update(human_passed="yes", reviewer="amy")  # agrees (pass)
    rows[1].update(human_score="0.9", comment="actually fine")  # overturns to pass
    rows[2].update(human_score="0.1", human_passed="否")  # overturns to fail
    rows[3].update(human_score="7")  # invalid
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=review.COLUMNS)
        w.writeheader()
        w.writerows(rows)
    res = review.import_review_sheet(project, path)
    assert res["imported"] == 3 and res["unfilled"] == 0 and res["eval_ids"] == [rep.id]
    assert len(res["skipped"]) == 1 and res["skipped"][0]["row"] == 5

    applied = review.apply_reviews(project, rep)
    by = {r.case_id: r.judgment for r in applied.results}
    assert by["c0"].passed and by["c0"].score == 1.0 and by["c0"].source == "human"
    assert by["c1"].passed and by["c1"].reason == "actually fine" and by["c1"].details["auto"]["passed"] is False
    assert not by["c2"].passed and not by["c2"].needs_review
    assert by["c3"].source == "auto"
    assert rep.results[1].judgment.source == "auto" and not rep.results[1].judgment.passed  # original untouched
    assert applied.pass_rate == 0.5

    ag = review.agreement(project, rep.id)
    assert ag["n"] == 3 and ag["pass_agreement"] == pytest.approx(1 / 3, abs=1e-3)
    assert {d["case_id"] for d in ag["disagreements"]} == {"c1", "c2"}
    assert ag["kappa"] < 0 and ag["mean_abs_score_diff"] == pytest.approx((0 + 0.9 + 0.7) / 3, abs=1e-3)
    assert review.agreement(project, "nope")["n"] == 0

    # re-export is prefilled with the stored human reviews
    path2 = review.export_review_sheet(project, rep, project.outputs_dir / "review2.csv")
    with path2.open(newline="", encoding="utf-8-sig") as f:
        rows2 = {r["case_id"]: r for r in csv.DictReader(f)}
    assert rows2["c0"]["reviewer"] == "amy" and rows2["c0"]["human_passed"] == "true"
    assert rows2["c1"]["human_score"] == "0.9" and rows2["c3"]["human_score"] == ""


def test_perfect_agreement_kappa(project):
    rep = make_report()
    review.enqueue_reviews(project, rep, mode="all")
    for r in rep.results:
        project.store.submit_review(rep.id, r.case_id, 0, score=r.judgment.score, passed=r.judgment.passed)
    ag = review.agreement(project, rep.id)
    assert ag["pass_agreement"] == 1.0 and ag["kappa"] == 1.0 and ag["disagreements"] == []
