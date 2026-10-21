"""Tests for the retrieval evaluation, the log-query builder and the knowledge-base validator (Stage 10)."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from incident_agent.eval import harness, retrieval_eval, validate_kb
from incident_agent.eval.retrieval_eval import (
    GoldError,
    deltas,
    evaluate,
    format_table,
    grade,
    kb_fingerprint,
    load_gold,
    parse_weights,
    query_for,
    result_path,
    score_ranking,
)
from incident_agent.knowledge.corpus import KB_DIR, KBDoc, load_corpus
from incident_agent.knowledge.embedders import HashingEmbedder
from incident_agent.knowledge.hybrid import HybridSearcher
from incident_agent.knowledge.query import INCIDENTS_DIR, QueryError, build_log_query

ROOT = Path(__file__).resolve().parents[1]
BODY = "filler words padding this test document beyond the minimum length requirement for documents."


def kb(doc_id: str, cls: str | None) -> KBDoc:
    kind = "runbook" if cls else "reference"
    return KBDoc(id=doc_id, doc_type=kind, title=f"Title {doc_id}", failure_class=cls, text=BODY)  # type: ignore[arg-type]


DOCS = {
    "a": kb("a", "alpha"),
    "b": kb("b", "beta"),
    "c": kb("c", "gamma"),
    "d": kb("d", "alpha"),
    "r": kb("r", None),
}
ENTRY = {"primary": "alpha", "secondary": ["beta"]}


# --- grading -------------------------------------------------------------------------------------------------


class TestGrade:
    def test_primary_secondary_other_and_reference(self):
        assert grade(DOCS["a"], ENTRY) == 2
        assert grade(DOCS["b"], ENTRY) == 1
        assert grade(DOCS["c"], ENTRY) == 0
        assert grade(DOCS["r"], ENTRY) == 0

    def test_missing_secondary_key_is_fine(self):
        assert grade(DOCS["b"], {"primary": "alpha"}) == 0


class TestScoreRanking:
    def test_perfect_ranking(self):
        s = score_ranking(["a", "d", "b", "c", "r"], DOCS, ENTRY)
        assert (s["hit@1"], s["hit@3"], s["hit@5"], s["mrr"], s["first_relevant_rank"]) == (1.0, 1.0, 1.0, 1.0, 1.0)
        assert s["ndcg@10"] == pytest.approx(1.0)

    def test_exact_values_when_relevant_doc_is_third(self):
        s = score_ranking(["c", "b", "a"], DOCS, ENTRY)
        assert (s["hit@1"], s["hit@3"], s["hit@5"]) == (0.0, 1.0, 1.0)
        assert s["mrr"] == pytest.approx(1 / 3)
        assert s["first_relevant_rank"] == 3.0
        dcg = 0 + 1 / math.log2(3) + 3 / math.log2(4)
        ideal = 3 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4)
        assert s["ndcg@10"] == pytest.approx(dcg / ideal)

    def test_hit_cutoffs(self):
        s = score_ranking(["c", "r", "c", "r", "a"], DOCS, ENTRY)
        assert (s["hit@1"], s["hit@3"], s["hit@5"]) == (0.0, 0.0, 1.0)

    def test_relevant_document_at_rank_four_is_not_a_hit_at_three(self):
        s = score_ranking(["c", "r", "c", "a"], DOCS, ENTRY)
        assert (s["hit@3"], s["hit@5"]) == (0.0, 1.0)

    def test_ideal_dcg_uses_ten_documents(self):
        docs = {f"p{i}": kb(f"p{i}", "alpha") for i in range(8)} | {"z": kb("z", "zzz")}
        s = score_ranking([f"p{i}" for i in range(8)], docs, ENTRY)
        assert s["ndcg@10"] == pytest.approx(1.0)
        # ten ideal slots: eight perfect documents followed by nothing; a five-slot ideal would give > 1 for 8 hits
        assert s["ndcg@10"] <= 1.0 + 1e-9

    def test_no_relevant_document_found(self):
        s = score_ranking(["c", "r"], DOCS, ENTRY)
        assert s["mrr"] == 0.0 and s["first_relevant_rank"] == 0.0 and s["hit@5"] == 0.0

    def test_empty_ranking_and_no_relevant_doc_in_corpus(self):
        assert score_ranking([], DOCS, ENTRY)["ndcg@10"] == 0.0
        assert score_ranking(["a"], DOCS, {"primary": "nothing"})["ndcg@10"] == 0.0

    def test_ndcg_only_looks_at_top_ten(self):
        docs = {f"x{i}": kb(f"x{i}", "other") for i in range(10)} | {"a": kb("a", "alpha")}
        s = score_ranking([*(f"x{i}" for i in range(10)), "a"], docs, ENTRY)
        assert s["ndcg@10"] == 0.0 and s["mrr"] == pytest.approx(1 / 11)


# --- gold ----------------------------------------------------------------------------------------------------


class TestGold:
    def test_real_gold_covers_every_incident_with_all_fields(self):
        gold = load_gold()
        assert sorted(gold) == [f"INC-{i:03d}" for i in range(1, 29)]
        assert all(e["primary"] and e["paraphrase"] and e["symptom"] for e in gold.values())

    def write(self, tmp_path: Path, entry: dict) -> Path:
        p = tmp_path / "g.json"
        p.write_text(json.dumps({"incidents": {"INC-001": entry}}))
        return p

    def test_missing_field(self, tmp_path):
        with pytest.raises(GoldError, match="needs"):
            load_gold(self.write(tmp_path, {"primary": "x", "paraphrase": "p"}))

    def test_primary_listed_as_secondary(self, tmp_path):
        with pytest.raises(GoldError, match="also listed"):
            load_gold(self.write(tmp_path, {"primary": "x", "secondary": ["x"], "paraphrase": "p", "symptom": "s"}))

    def test_valid_entry_loads(self, tmp_path):
        gold = load_gold(self.write(tmp_path, {"primary": "x", "paraphrase": "p", "symptom": "s"}))
        assert gold["INC-001"]["primary"] == "x"

    def test_every_primary_class_matches_the_gold_answer_class_set(self):
        """Each primary class is used by at least one KB runbook and post-mortem."""
        gold = load_gold()
        docs = load_corpus()
        for iid, e in gold.items():
            kinds = {d.doc_type for d in docs if d.failure_class == e["primary"]}
            assert {"runbook", "postmortem"} <= kinds, iid


# --- queries -------------------------------------------------------------------------------------------------


def make_incident(root: Path, iid: str, logs: list[str], meta: dict | None = None, deploys: list | None = None) -> Path:
    d = root / iid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(meta or {"incident_id": iid, "services": ["svc-a"], "available_metrics": ["cpu_percent"]})
    )
    (d / "logs.log").write_text("\n".join(logs) + "\n")
    if deploys is not None:
        (d / "deploys.json").write_text(json.dumps(deploys))
    return root


def line(level: str, msg: str, svc: str = "svc-a") -> str:
    return f'2026-03-01T00:00:00+00:00 service={svc} level={level} msg="{msg}"'


class TestBuildLogQuery:
    def test_contains_services_metrics_errors_rare_info_and_deploys(self, tmp_path):
        logs = [line("INFO", "request ok")] * 10 + [
            line("ERROR", "connection refused by upstream"),
            line("ERROR", "connection refused by upstream"),
            line("WARN", "slow response observed"),
            line("INFO", "maintenance window opened"),
        ]
        root = make_incident(tmp_path, "INC-900", logs, deploys=[{"description": "bump pool size"}])
        q = build_log_query("INC-900", root)
        for part in ("svc-a", "cpu_percent", "connection refused", "slow response", "maintenance window", "bump pool"):
            assert part in q
        assert "request ok" not in q  # common INFO noise is excluded

    def test_most_frequent_error_comes_before_rarer_ones(self, tmp_path):
        logs = [line("ERROR", "rare failure")] + [line("ERROR", "common failure")] * 3
        q = build_log_query("INC-900", make_incident(tmp_path, "INC-900", logs))
        assert q.index("common failure") < q.index("rare failure")

    def test_max_severe_limits_templates(self, tmp_path):
        logs = [line("ERROR", f"failure kind {w}") for w in ("alpha", "bravo", "charlie")]
        q = build_log_query("INC-900", make_incident(tmp_path, "INC-900", logs), max_severe=1)
        assert sum(w in q for w in ("alpha", "bravo", "charlie")) == 1

    def test_max_rare_info_limits_and_orders_by_first_appearance(self, tmp_path):
        logs = [
            line("INFO", "first event happened"),
            line("INFO", "second event happened"),
            line("INFO", "third event"),
        ]
        q = build_log_query("INC-900", make_incident(tmp_path, "INC-900", logs), max_rare_info=2)
        assert "first event" in q and "second event" in q and "third event" not in q

    def test_rare_threshold(self, tmp_path):
        logs = [line("INFO", "seen twice")] * 2
        root = make_incident(tmp_path, "INC-900", logs)
        assert "seen twice" in build_log_query("INC-900", root, rare_threshold=2)
        assert "seen twice" not in build_log_query("INC-900", root, rare_threshold=1)

    def test_unparseable_lines_and_missing_optional_files(self, tmp_path):
        root = make_incident(tmp_path, "INC-900", ["garbage line", line("ERROR", "real failure")])
        (root / "INC-900" / "logs.log").unlink()
        assert build_log_query("INC-900", root) == "svc-a; cpu_percent"

    def test_deploys_limited_to_three(self, tmp_path):
        deploys = [{"description": f"deploy number {w}"} for w in ("one", "two", "three", "four")]
        q = build_log_query("INC-900", make_incident(tmp_path, "INC-900", [], deploys=deploys))
        assert "three" in q and "four" not in q

    def test_missing_incident(self, tmp_path):
        with pytest.raises(QueryError, match="INC-404"):
            build_log_query("INC-404", tmp_path)

    def test_real_incidents(self):
        q1 = build_log_query("INC-001", INCIDENTS_DIR)
        assert "checkout-api" in q1 and "cpu" in q1.lower()
        q21 = build_log_query("INC-021", INCIDENTS_DIR)
        assert "inventory-service" in q21

    def test_never_reads_gold(self, tmp_path, monkeypatch):
        seen: list[str] = []
        real = Path.read_text

        def spy(self, *a, **k):
            seen.append(str(self))
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", spy)
        build_log_query("INC-001", INCIDENTS_DIR)
        assert seen and not any("gold" in s for s in seen)


class TestQueryFor:
    gold = {"INC-001": {"paraphrase": "para text", "symptom": "sym text"}}

    def test_styles(self):
        assert query_for("INC-001", "paraphrase", self.gold, None) == "para text"
        assert query_for("INC-001", "symptom", self.gold, None) == "sym text"
        assert "checkout-api" in query_for("INC-001", "log", self.gold, INCIDENTS_DIR)

    def test_unknown_style(self):
        with pytest.raises(ValueError, match="style"):
            query_for("INC-001", "nope", self.gold, None)


# --- evaluate / deltas ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_searcher():
    docs = load_corpus()
    s = HybridSearcher(HashingEmbedder())
    s.index(docs)
    return s, docs, load_gold()


class TestEvaluate:
    def test_summary_shape_and_averages(self, real_searcher):
        s, docs, gold = real_searcher
        ids = ["INC-001", "INC-002"]
        rep = evaluate(
            s, docs, gold, ids, styles=("paraphrase",), modes=("bm25", "hybrid"), incidents_dir=INCIDENTS_DIR
        )
        assert set(rep["summary"]) == {"paraphrase"} and set(rep["summary"]["paraphrase"]) == {"bm25", "hybrid"}
        rows = [r for r in rep["incidents"] if r["mode"] == "bm25"]
        assert len(rows) == 2 and rep["summary"]["paraphrase"]["bm25"]["n"] == 2
        mean = sum(r["mrr"] for r in rows) / 2
        assert rep["summary"]["paraphrase"]["bm25"]["mrr"] == pytest.approx(mean, abs=1e-4)
        assert all(len(r["top"]) <= 5 and r["primary"] for r in rows)

    def test_unknown_incident(self, real_searcher):
        s, docs, gold = real_searcher
        with pytest.raises(GoldError, match="INC-999"):
            evaluate(s, docs, gold, ["INC-999"])

    def test_dev_quality_floor_regression(self, real_searcher):
        """Guards the hashing/numpy baseline so a retrieval regression fails the build."""
        s, docs, gold = real_searcher
        ids = harness.load_split("dev")
        rep = evaluate(s, docs, gold, ids, incidents_dir=INCIDENTS_DIR)["summary"]
        assert rep["log"]["hybrid"]["hit@3"] >= 0.9
        assert rep["paraphrase"]["hybrid"]["hit@3"] >= 0.85
        assert rep["symptom"]["hybrid"]["hit@3"] >= 0.7
        assert rep["paraphrase"]["hybrid"]["mrr"] >= rep["paraphrase"]["dense"]["mrr"] - 0.05

    def test_numpy_and_qdrant_stores_agree_on_dev(self):
        pytest.importorskip("qdrant_client")
        from incident_agent.knowledge.vector_store import build_store

        docs, gold = load_corpus(), load_gold()
        ids = harness.load_split("dev")
        out = []
        for store in ("numpy", "qdrant"):
            s = HybridSearcher(HashingEmbedder(), build_store(store))
            s.index(docs)
            rep = evaluate(s, docs, gold, ids, styles=("paraphrase", "symptom"), incidents_dir=INCIDENTS_DIR)
            out.append([(r["incident_id"], r["style"], r["mode"], r["top"]) for r in rep["incidents"]])
        assert out[0] == out[1]


class TestDeltas:
    rows = [
        {"incident_id": "I1", "style": "s", "mode": "hybrid", "mrr": 1.0},
        {"incident_id": "I1", "style": "s", "mode": "bm25", "mrr": 0.5},
        {"incident_id": "I2", "style": "s", "mode": "hybrid", "mrr": 0.5},
        {"incident_id": "I2", "style": "s", "mode": "bm25", "mrr": 0.5},
        {"incident_id": "I3", "style": "s", "mode": "hybrid", "mrr": 0.2},
        {"incident_id": "I3", "style": "s", "mode": "bm25", "mrr": 0.4},
        {"incident_id": "I1", "style": "other", "mode": "hybrid", "mrr": 0.0},
    ]

    def test_paired_difference(self):
        d = deltas(self.rows, "s", "hybrid", "bm25")
        assert d["n"] == 3 and d["wins"] == 1 and d["losses"] == 1
        assert d["mean"] == pytest.approx(0.1, abs=1e-4)
        lo, hi = d["ci"]
        assert lo <= d["mean"] <= hi

    def test_other_metric_and_empty(self):
        rows = [{"incident_id": "I1", "style": "s", "mode": m, "hit@1": v} for m, v in (("a", 1.0), ("b", 0.0))]
        assert deltas(rows, "s", "a", "b", metric="hit@1")["mean"] == 1.0
        assert deltas([], "s", "a", "b") == {"n": 0, "mean": 0.0, "ci": (0.0, 0.0), "wins": 0, "losses": 0}


# --- small helpers ------------------------------------------------------------------------------------------


class TestHelpers:
    def test_parse_weights(self):
        assert parse_weights(None) is None and parse_weights("") is None
        assert parse_weights("bm25=1,dense=2.5") == {"bm25": 1.0, "dense": 2.5}
        assert parse_weights(" bm25 = 1 ,dense=0") == {"bm25": 1.0, "dense": 0.0}

    @pytest.mark.parametrize("bad", ["bm25", "sparse=1", "bm25=x", "bm25=1,foo=2"])
    def test_parse_weights_rejects(self, bad):
        with pytest.raises(ValueError):
            parse_weights(bad)

    def test_result_path(self, tmp_path):
        assert result_path("t", tmp_path) == tmp_path / "retrieval__t.json"
        assert result_path(None, tmp_path) == tmp_path / "retrieval.json"
        assert result_path("x").parent == harness.RESULTS_DIR

    def test_kb_fingerprint_changes_with_kb_and_gold(self, tmp_path):
        kb_dir = tmp_path / "kb"
        kb_dir.mkdir()
        (kb_dir / "a.md").write_text("one")
        gold = tmp_path / "g.json"
        gold.write_text("{}")
        base = kb_fingerprint(kb_dir, gold)
        assert base == kb_fingerprint(kb_dir, gold) and len(base) == 16
        (kb_dir / "a.md").write_text("two")
        changed = kb_fingerprint(kb_dir, gold)
        assert changed != base
        gold.write_text("{ }")
        assert kb_fingerprint(kb_dir, gold) != changed
        (kb_dir / "b.md").write_text("x")
        assert kb_fingerprint(kb_dir, gold) != changed

    def test_format_table(self):
        rep = {
            "summary": {
                "log": {"hybrid": {"n": 3, "hit@1": 0.5, "hit@3": 1.0, "hit@5": 1.0, "mrr": 0.75, "ndcg@10": 0.9}}
            }
        }
        out = format_table(rep)
        assert "query style: log" in out and "hybrid" in out and "0.750" in out and "ndcg@10" in out


# --- CLI ----------------------------------------------------------------------------------------------------


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    results = tmp_path / "results"
    monkeypatch.setattr(retrieval_eval, "RESULTS_DIR", results)
    monkeypatch.setattr(harness, "OPENINGS_LOG", tmp_path / "openings.log")
    return tmp_path


class TestMain:
    def test_dev_run_writes_report(self, sandbox, capsys):
        assert retrieval_eval.main(["--split", "dev", "--style", "paraphrase", "--tag", "t"]) == 0
        out = capsys.readouterr().out
        assert "embedder=hashing" in out and "hybrid_vs_bm25_mrr" in out
        report = json.loads((sandbox / "results" / "retrieval__t.json").read_text())
        assert report["meta"]["split"] == "dev" and report["meta"]["n_docs"] == len(load_corpus())
        assert report["meta"]["kb_fingerprint"] == kb_fingerprint() and report["meta"]["store"] == "NumpyVectorStore"
        assert set(report["summary"]) == {"paraphrase"} and set(report["deltas"]) == {"paraphrase"}
        assert not (sandbox / "openings.log").exists()

    def test_test_split_is_logged(self, sandbox, capsys):
        assert retrieval_eval.main(["--split", "test", "--style", "symptom", "--tag", "opened"]) == 0
        assert "TEST SET OPENED" in capsys.readouterr().out
        entry = (sandbox / "openings.log").read_text()
        assert "agent=retrieval" in entry and "tag=opened" in entry and "n=14" in entry

    def test_explicit_ids_do_not_open_test(self, sandbox):
        assert retrieval_eval.main(["--split", "test", "--ids", "INC-004", "--style", "symptom", "--tag", "one"]) == 0
        assert not (sandbox / "openings.log").exists()
        report = json.loads((sandbox / "results" / "retrieval__one.json").read_text())
        assert {r["incident_id"] for r in report["incidents"]} == {"INC-004"} and report["meta"]["split"] == "ids"

    def test_weights_and_depth_are_recorded(self, sandbox):
        assert (
            retrieval_eval.main(
                [
                    "--ids",
                    "INC-001",
                    "--style",
                    "log",
                    "--weights",
                    "bm25=1,dense=0",
                    "--rrf-k",
                    "10",
                    "--depth",
                    "5",
                    "--tag",
                    "w",
                ]
            )
            == 0
        )
        meta = json.loads((sandbox / "results" / "retrieval__w.json").read_text())["meta"]
        assert (meta["weights"], meta["rrf_k"], meta["depth"]) == ({"bm25": 1.0, "dense": 0.0}, 10, 5)

    def test_all_split_covers_all_gold(self, sandbox):
        assert retrieval_eval.main(["--split", "all", "--style", "log", "--tag", "all"]) == 0
        report = json.loads((sandbox / "results" / "retrieval__all.json").read_text())
        assert len({r["incident_id"] for r in report["incidents"]}) == 28

    @pytest.mark.parametrize(
        "argv",
        [["--weights", "nonsense"], ["--embedder", "bogus"], ["--ids", "INC-999"], ["--embedder", "hashing:0"]],
    )
    def test_configuration_errors_return_2(self, sandbox, capsys, argv):
        assert retrieval_eval.main([*argv, "--style", "log"]) == 2
        assert capsys.readouterr().out.startswith("ERROR")

    def test_missing_incident_data_returns_2(self, sandbox, monkeypatch, capsys):
        monkeypatch.setattr(retrieval_eval, "INCIDENTS_DIR", sandbox / "nowhere")
        assert retrieval_eval.main(["--ids", "INC-001", "--style", "log"]) == 2
        assert "no incident data" in capsys.readouterr().out

    def test_runs_are_deterministic(self, sandbox):
        for tag in ("x", "y"):
            retrieval_eval.main(["--split", "dev", "--tag", tag])
        a = json.loads((sandbox / "results" / "retrieval__x.json").read_text())
        b = json.loads((sandbox / "results" / "retrieval__y.json").read_text())
        assert a["summary"] == b["summary"] and a["incidents"] == b["incidents"]


# --- validator ----------------------------------------------------------------------------------------------


@pytest.fixture()
def repo_copy(tmp_path) -> dict[str, Path]:
    kb_dir = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb_dir)
    gold_dir = tmp_path / "gold"
    shutil.copytree(ROOT / "eval" / "gold", gold_dir)
    gold_file = tmp_path / "retrieval_gold.json"
    shutil.copy(retrieval_eval.GOLD_FILE, gold_file)
    splits = tmp_path / "splits.json"
    shutil.copy(harness.SPLITS_FILE, splits)
    return {"kb_dir": kb_dir, "gold_dir": gold_dir, "gold_file": gold_file, "splits_file": splits}


def run_validate(paths: dict[str, Path], **override) -> list[str]:
    return validate_kb.validate(**{**paths, "incidents_dir": INCIDENTS_DIR, **override})


class TestValidator:
    def test_shipped_kb_is_clean(self):
        assert validate_kb.validate() == []

    def test_shingles(self):
        assert validate_kb.shingles("a b c", 4) == set()
        assert validate_kb.shingles("One two, THREE four five", 3) == {
            ("one", "two", "three"),
            ("two", "three", "four"),
            ("three", "four", "five"),
        }

    def test_copy_is_clean(self, repo_copy):
        assert run_validate(repo_copy) == []

    def test_doc_copying_gold_answer_is_a_leak(self, repo_copy):
        gold = json.loads((repo_copy["gold_dir"] / "INC-001.json").read_text())
        stolen = " ".join(gold["root_cause"].split()[:8])
        doc = next(repo_copy["kb_dir"].glob("rb-*.md"))
        doc.write_text(doc.read_text() + f"\n{stolen}\n")
        problems = run_validate(repo_copy)
        assert any(p.startswith("leak:") and "INC-001" in p for p in problems)

    def test_incident_id_mention_is_a_leak(self, repo_copy):
        doc = next(repo_copy["kb_dir"].glob("pm-*.md"))
        doc.write_text(doc.read_text() + "\nSee also inc-007 for details.\n")
        assert any("names an incident id" in p for p in run_validate(repo_copy))

    @pytest.mark.parametrize("style", ["paraphrase", "symptom"])
    def test_query_copying_gold_answer_is_a_leak(self, repo_copy, style):
        gold = json.loads((repo_copy["gold_dir"] / "INC-002.json").read_text())
        data = json.loads(repo_copy["gold_file"].read_text())
        data["incidents"]["INC-002"][style] = " ".join(gold["root_cause"].split()[:8])
        repo_copy["gold_file"].write_text(json.dumps(data))
        assert any(p.startswith("leak:") and f"INC-002:{style}" in p for p in run_validate(repo_copy))

    def test_missing_runbook_or_postmortem(self, repo_copy):
        victim = repo_copy["kb_dir"] / "rb-cpu-exhaustion.md"
        assert victim.exists()
        victim.unlink()
        assert any("'cpu_exhaustion' has no runbook" in p for p in run_validate(repo_copy))
        (repo_copy["kb_dir"] / "pm-cpu-exhaustion.md").unlink()
        assert any("'cpu_exhaustion' has no postmortem" in p for p in run_validate(repo_copy))

    def test_reference_with_class_and_runbook_without_class(self, repo_copy):
        ref = next(repo_copy["kb_dir"].glob("ref-*.md"))
        ref.write_text(ref.read_text().replace("type: reference", "type: reference\nclass: cpu_exhaustion"))
        assert any("reference notes must not carry a failure class" in p for p in run_validate(repo_copy))
        rb = repo_copy["kb_dir"] / "rb-cpu-exhaustion.md"
        rb.write_text("\n".join(ln for ln in rb.read_text().splitlines() if not ln.startswith("class:")))
        assert any("needs a 'class'" in p for p in run_validate(repo_copy))

    def test_missing_gold_answer_incident_and_split(self, repo_copy):
        (repo_copy["gold_dir"] / "INC-003.json").unlink()
        splits = json.loads(repo_copy["splits_file"].read_text())
        splits["dev"] = [i for i in splits["dev"] if i != "INC-001"]
        repo_copy["splits_file"].write_text(json.dumps(splits))
        problems = run_validate(repo_copy, incidents_dir=repo_copy["kb_dir"])
        assert "INC-003: no eval/gold answer file" in problems
        assert "INC-001: not in eval/splits.json" in problems
        assert "INC-002: no incident data" in problems

    def test_class_without_document(self, repo_copy):
        data = json.loads(repo_copy["gold_file"].read_text())
        data["incidents"]["INC-001"]["secondary"] = ["no_such_class"]
        repo_copy["gold_file"].write_text(json.dumps(data))
        problems = run_validate(repo_copy)
        assert any("class 'no_such_class' has no document" in p for p in problems)
        assert any("failure class 'no_such_class' has no runbook" in p for p in problems)

    def test_broken_inputs_are_reported_not_raised(self, repo_copy, tmp_path):
        assert run_validate(repo_copy, kb_dir=tmp_path / "empty")[0].startswith("corpus:")
        repo_copy["gold_file"].write_text('{"incidents": {"INC-001": {"primary": "x"}}}')
        assert run_validate(repo_copy)[0].startswith("retrieval gold:")
        repo_copy["gold_file"].write_text("not json")
        assert run_validate(repo_copy)[0].startswith("retrieval gold:")

    def test_main_exit_codes(self, monkeypatch, capsys):
        assert validate_kb.main() == 0 and "OK" in capsys.readouterr().out
        monkeypatch.setattr(validate_kb, "validate", lambda: ["boom"])
        assert validate_kb.main() == 1
        out = capsys.readouterr().out
        assert "1 problem(s)" in out and "ERROR boom" in out
