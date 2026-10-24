"""Tests for the episodic-memory evaluation protocol and the recurrence dataset (Stage 12)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from incident_agent.eval import harness, memory_eval
from incident_agent.eval.memory_eval import (
    MemoryEvalError,
    evaluate,
    format_report,
    kb_predict,
    result_path,
    seed_memory,
    summarise,
)
from incident_agent.eval.retrieval_eval import load_gold
from incident_agent.knowledge import HashingEmbedder, HybridSearcher, load_corpus
from incident_agent.knowledge.query import INCIDENTS_DIR
from incident_agent.memory.episodic import EpisodicMemory

ROOT = Path(__file__).resolve().parents[1]
GOLD = load_gold()
DEV = harness.load_split("dev")


@pytest.fixture(scope="module")
def kb() -> HybridSearcher:
    s = HybridSearcher(HashingEmbedder())
    s.index(load_corpus())
    return s


def fresh_memory() -> EpisodicMemory:
    return EpisodicMemory(HashingEmbedder())


class TestKbPredict:
    def test_returns_class_of_best_document(self, kb):
        assert kb_predict(kb, "expired certificate handshake failures tls clients refused") == "tls_cert_expiry"

    def test_only_reference_documents_or_blank_query_gives_none(self):
        refs = [d for d in load_corpus() if d.doc_type == "reference"]
        assert refs
        only_refs = HybridSearcher(HashingEmbedder())
        only_refs.index(refs)
        assert kb_predict(only_refs, "cpu memory dns certificate latency restart") is None

    def test_a_reference_document_ranked_first_is_skipped_for_the_next_class_bearing_one(self):
        from incident_agent.knowledge import KBDoc

        pad = " filler words padding this test document beyond the minimum length requirement for documents."
        docs = [
            KBDoc(
                id="ref-a",
                doc_type="reference",
                title="Reference note",
                text="restart loop crash backoff restart loop" + pad,
            ),
            KBDoc(id="rb-b", doc_type="runbook", title="Runbook b", failure_class="b_class", text="restart" + pad),
        ]
        s = HybridSearcher(HashingEmbedder())
        s.index(docs)
        assert s.search("restart loop crash backoff", 2, "hybrid")[0].doc_id == "ref-a"
        assert kb_predict(s, "restart loop crash backoff") == "b_class"

    def test_blank_query(self, kb):
        assert kb_predict(kb, "   ") is None


class TestSeedMemory:
    def test_predicts_before_writing_and_records_outcomes(self, kb):
        mem = fresh_memory()
        log = seed_memory(mem, kb, DEV[:3], GOLD, INCIDENTS_DIR)
        assert len(mem) == 3 and [e["incident_id"] for e in log] == DEV[:3]
        assert log[0]["recalled"] is None  # cold start: nothing existed when the first incident was diagnosed
        for entry, ep in zip(log, mem.episodes, strict=True):
            assert ep.resolved_class == entry["resolved"] == GOLD[ep.incident_id]["primary"]
            assert ep.diagnosed_class == entry["diagnosed"]
            assert ep.reflection is not None

    def test_a_similar_earlier_incident_is_recalled_by_the_next_one(self, kb):
        log = seed_memory(fresh_memory(), kb, ["INC-002", "INC-020"], GOLD, INCIDENTS_DIR)
        assert log[0]["recalled"] is None and log[1]["recalled"] == "memory_leak"

    def test_the_resolved_class_is_never_used_for_the_prediction(self, kb, monkeypatch):
        seen: list[str] = []
        real = memory_eval.kb_predict
        monkeypatch.setattr(memory_eval, "kb_predict", lambda k, q, n=5: seen.append(q) or real(k, q, n))
        seed_memory(fresh_memory(), kb, DEV[:2], GOLD, INCIDENTS_DIR)
        assert seen and all(GOLD[i]["primary"] not in q for q in seen for i in DEV[:2])

    def test_false_alarm_flag(self, kb):
        mem = fresh_memory()
        seed_memory(mem, kb, ["INC-019", "INC-001"], GOLD, INCIDENTS_DIR)
        flags = {e.incident_id: e.is_false_alarm for e in mem.episodes}
        assert flags == {"INC-019": True, "INC-001": False}

    def test_unknown_incident(self, kb):
        with pytest.raises(MemoryEvalError, match="INC-999"):
            seed_memory(fresh_memory(), kb, ["INC-999"], GOLD, INCIDENTS_DIR)


def row(**kw):
    base = {
        "kb_correct": False,
        "memory_answered": False,
        "memory_correct": False,
        "combined_correct": False,
        "first_encounter_kb_wrong": False,
        "novel": False,
    }
    return {**base, **kw}


class TestSummarise:
    def test_exact_numbers(self):
        rows = [
            row(kb_correct=True, combined_correct=True, memory_answered=True, memory_correct=True),
            row(combined_correct=True, memory_answered=True, memory_correct=True, first_encounter_kb_wrong=True),
            row(kb_correct=True, memory_answered=True),  # memory misled: kb right, combined wrong
            row(first_encounter_kb_wrong=True),
            row(kb_correct=True, combined_correct=True, novel=True),
            row(novel=True, memory_answered=True),
        ]
        s = summarise(rows)
        assert s["n"] == 6
        assert s["kb_accuracy"] == pytest.approx(0.5) and s["combined_accuracy"] == pytest.approx(0.5)
        assert s["memory_coverage"] == pytest.approx(4 / 6, abs=1e-4)
        assert s["memory_precision"] == pytest.approx(0.5)
        assert (s["rescued"], s["misled"]) == (1, 1)
        assert s["delta_combined_minus_kb"]["mean"] == 0.0
        assert s["first_encounter_kb_wrong"] == {"n": 2, "kb_accuracy": 0.0, "combined_accuracy": 0.5}
        assert s["novel_classes"] == {"n": 2, "memory_answered": 1, "kb_accuracy": 0.5, "combined_accuracy": 0.5}

    def test_empty_and_unanswered(self):
        s = summarise([])
        assert s["n"] == 0 and s["kb_accuracy"] == 0.0 and s["memory_precision"] == 0.0
        assert summarise([row()])["memory_precision"] == 0.0

    def test_delta_ci_brackets_the_mean(self):
        s = summarise([row(combined_correct=True)] * 3 + [row()] * 3)
        d = s["delta_combined_minus_kb"]
        assert d["mean"] == 0.5 and d["ci"][0] <= d["mean"] <= d["ci"][1]

    def test_format_report_mentions_every_number(self):
        text = format_report({"summary": summarise([row(kb_correct=True, combined_correct=True)])})
        for part in ("queries: 1", "kb accuracy 1.000", "memory coverage", "rescued 0  misled 0", "novel classes"):
            assert part in text

    def test_result_path(self, tmp_path):
        assert result_path("t", tmp_path) == tmp_path / "memory__t.json"
        assert result_path(None, tmp_path) == tmp_path / "memory.json"


class TestEvaluate:
    def gold_rows(self):
        return json.loads(memory_eval.RECURRENCE_GOLD.read_text())["incidents"]

    def test_recurrences_are_recognised_without_misleading(self, kb):
        mem = fresh_memory()
        seed_memory(mem, kb, DEV, GOLD, INCIDENTS_DIR)
        rep = evaluate(mem, kb, self.gold_rows(), memory_eval.RECURRENCE_DIR)
        s = rep["summary"]
        assert s["n"] == 14 and s["misled"] == 0
        assert (
            s["combined_accuracy"] >= s["kb_accuracy"] and s["memory_precision"] == 1.0 and s["memory_coverage"] >= 0.85
        )
        for r in rep["incidents"]:
            assert r["recurrence_of"] in DEV and r["novel"] is False
            if r["memory_answered"]:
                assert r["recalled"][0] == r["recurrence_of"]

    def test_empty_memory_falls_back_to_the_knowledge_base(self, kb):
        rep = evaluate(fresh_memory(), kb, self.gold_rows(), memory_eval.RECURRENCE_DIR)
        assert rep["summary"]["memory_coverage"] == 0.0
        assert all(r["combined"] == r["kb"] for r in rep["incidents"])

    def test_threshold_controls_coverage(self, kb):
        mem = fresh_memory()
        seed_memory(mem, kb, DEV, GOLD, INCIDENTS_DIR)
        assert (
            evaluate(mem, kb, self.gold_rows(), memory_eval.RECURRENCE_DIR, min_similarity=0.0)["summary"][
                "memory_coverage"
            ]
            == 1.0
        )
        assert (
            evaluate(mem, kb, self.gold_rows(), memory_eval.RECURRENCE_DIR, min_similarity=1.01)["summary"][
                "memory_coverage"
            ]
            == 0.0
        )

    def test_memory_correct_requires_that_memory_actually_answered(self, kb):
        mem = fresh_memory()
        seed_memory(mem, kb, DEV, GOLD, INCIDENTS_DIR)
        rep = evaluate(mem, kb, self.gold_rows(), memory_eval.RECURRENCE_DIR, min_similarity=1.01)
        assert not any(r["memory_correct"] or r["memory_answered"] for r in rep["incidents"])
        assert any(r["memory"] == r["truth"] for r in rep["incidents"])  # it would have been right, but stayed quiet

    def test_novel_incidents_are_not_hijacked_by_memory_on_dev(self, kb):
        """Dev-only control: with the incident's own episode left out, memory must stay quiet."""
        answered = 0
        for held_out in DEV:
            mem = fresh_memory()
            seed_memory(mem, kb, [i for i in DEV if i != held_out], GOLD, INCIDENTS_DIR)
            rep = evaluate(mem, kb, {held_out: GOLD[held_out]}, INCIDENTS_DIR)
            answered += rep["incidents"][0]["memory_answered"]
        assert answered == 0

    def test_first_encounter_wrong_flag_comes_from_the_episode(self, kb):
        mem = fresh_memory()
        seed_memory(mem, kb, ["INC-001", "INC-002"], GOLD, INCIDENTS_DIR)
        mem.record("INC-001", mem.episodes[0].observation, "cpu_exhaustion", diagnosed_class="memory_leak")
        mem.record("INC-002", mem.episodes[1].observation, "memory_leak", diagnosed_class="memory_leak")
        rows = {k: v for k, v in self.gold_rows().items() if k in ("R-001", "R-002")}
        flags = {
            r["incident_id"]: r["first_encounter_kb_wrong"]
            for r in evaluate(mem, kb, rows, memory_eval.RECURRENCE_DIR)["incidents"]
        }
        assert flags == {"R-001": True, "R-002": False}


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_eval, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(harness, "OPENINGS_LOG", tmp_path / "openings.log")
    monkeypatch.delenv("EMBEDDER", raising=False)
    monkeypatch.delenv("EPISODE_MEMORY_DIR", raising=False)
    return tmp_path


class TestMain:
    def test_recurrence_run(self, sandbox, capsys):
        assert memory_eval.main(["--tag", "r"]) == 0
        out = capsys.readouterr().out
        assert "episodes=14" in out and "rescued" in out and "TEST SET" not in out
        rep = json.loads((sandbox / "results" / "memory__r.json").read_text())
        assert rep["meta"]["queries"] == "recurrence" and rep["meta"]["n_episodes"] == 14
        assert rep["meta"]["min_similarity"] == memory_eval.DEFAULT_MIN_SIMILARITY and len(rep["seeding"]) == 14
        assert rep["summary"]["n"] == 14 and not (sandbox / "openings.log").exists()

    def test_test_control_is_logged_and_stays_quiet(self, sandbox, capsys):
        assert memory_eval.main(["--queries", "test", "--tag", "ctl"]) == 0
        assert "TEST SET OPENED" in capsys.readouterr().out
        assert "agent=memory" in (sandbox / "openings.log").read_text()
        s = json.loads((sandbox / "results" / "memory__ctl.json").read_text())["summary"]
        assert s["n"] == 14 and s["novel_classes"]["n"] == 13  # INC-020 is the only test incident with a dev class
        assert s["novel_classes"]["memory_answered"] == 0

    def test_persists_episodes_and_refuses_a_non_empty_directory(self, sandbox, capsys):
        d = sandbox / "mem"
        assert memory_eval.main(["--memory-dir", str(d), "--tag", "p"]) == 0
        assert len((d / "episodes.jsonl").read_text().splitlines()) == 14
        assert memory_eval.main(["--memory-dir", str(d), "--tag", "q"]) == 2
        assert "already holds 14 episodes" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "argv", [["--min-similarity", "1.5"], ["--min-similarity", "-0.1"], ["--embedder", "bogus"]]
    )
    def test_configuration_errors(self, sandbox, capsys, argv):
        assert memory_eval.main(argv) == 2
        assert capsys.readouterr().out.startswith("ERROR")

    def test_deterministic(self, sandbox):
        for tag in ("a", "b"):
            memory_eval.main(["--tag", tag])
        a = json.loads((sandbox / "results" / "memory__a.json").read_text())
        b = json.loads((sandbox / "results" / "memory__b.json").read_text())
        assert a["summary"] == b["summary"] and a["incidents"] == b["incidents"] and a["seeding"] == b["seeding"]


# --- the recurrence dataset ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def make_rec():
    sys.path.insert(0, str(ROOT / "data"))
    try:
        return importlib.import_module("make_recurrences")
    finally:
        sys.path.remove(str(ROOT / "data"))


class TestRecurrenceData:
    gold = json.loads((ROOT / "eval" / "recurrence_gold.json").read_text())["incidents"]

    def test_one_recurrence_per_dev_incident(self):
        assert sorted(self.gold) == [f"R-{n:03d}" for n in range(1, 15)]
        assert [g["recurrence_of"] for _, g in sorted(self.gold.items())] == DEV
        for g in self.gold.values():
            assert g["primary"] == GOLD[g["recurrence_of"]]["primary"]

    def test_files_exist_and_are_agent_safe(self):
        for rid in self.gold:
            d = memory_eval.RECURRENCE_DIR / rid
            assert {p.name for p in d.iterdir()} == {"meta.json", "logs.log", "metrics.json", "deploys.json"}
            text = "".join(p.read_text() for p in d.iterdir())
            assert "INC-" not in text and self.gold[rid]["primary"] not in text
            assert json.loads((d / "meta.json").read_text())["incident_id"] == rid

    def test_services_differ_from_the_original_and_data_is_regenerated_not_copied(self):
        for rid, g in self.gold.items():
            new = json.loads((memory_eval.RECURRENCE_DIR / rid / "meta.json").read_text())
            old = json.loads((INCIDENTS_DIR / g["recurrence_of"] / "meta.json").read_text())
            assert new["services"][0] != old["services"][0]
            assert (memory_eval.RECURRENCE_DIR / rid / "logs.log").read_text() != (
                INCIDENTS_DIR / g["recurrence_of"] / "logs.log"
            ).read_text()

    def test_generator_reproduces_the_committed_files(self, make_rec, tmp_path, monkeypatch):
        monkeypatch.setattr(make_rec, "OUT_DIR", tmp_path / "incidents")
        monkeypatch.setattr(make_rec, "GOLD_OUT", tmp_path / "gold.json")
        assert make_rec.main() == 0
        assert (tmp_path / "gold.json").read_text() == (ROOT / "eval" / "recurrence_gold.json").read_text()
        for rid in self.gold:
            for name in ("meta.json", "logs.log", "metrics.json", "deploys.json"):
                assert (tmp_path / "incidents" / rid / name).read_bytes() == (
                    memory_eval.RECURRENCE_DIR / rid / name
                ).read_bytes()

    def test_rename_handles_prefix_overlapping_service_names(self, make_rec):
        spec = {
            "id": "INC-1",
            "slug": "s",
            "services": ["orders-service", "orders-api"],
            "text": "orders-api calls orders-service",
        }
        out = make_rec.renamed(spec, "R-1")
        assert (
            out["services"] == ["fulfilment-service", "purchase-api"]
            and out["text"] == "purchase-api calls fulfilment-service"
        )
        assert (out["id"], out["slug"], out["recurrence_of"]) == ("R-1", "s-again", "INC-1")

    def test_pinned_main_dataset_is_not_touched(self):
        pinned = json.loads((ROOT / "tests" / "dataset_v1_hashes.json").read_text())
        assert (
            pinned
        )  # the v1 pin test itself lives in tests/test_dataset_v2.py; this only guards that the file is present
        assert not (INCIDENTS_DIR / "R-001").exists()
