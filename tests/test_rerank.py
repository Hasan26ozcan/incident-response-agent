"""Tests for the Stage 11 reranking layer."""

from __future__ import annotations

import json
import math

import pytest

from incident_agent.eval import harness, retrieval_eval
from incident_agent.knowledge import (
    CachedReranker,
    HashingEmbedder,
    HybridSearcher,
    KBDoc,
    OverlapReranker,
    RerankerError,
    RerankingSearcher,
    build_reranker,
)
from incident_agent.knowledge.rerank import RERANK_MODE, _minmax

PAD = " filler words padding this test document beyond the minimum length requirement for documents."


def kb(doc_id: str, text: str, cls: str | None = "x") -> KBDoc:
    return KBDoc(id=doc_id, doc_type="runbook", title=f"Title {doc_id}", failure_class=cls, text=text + PAD)  # type: ignore[arg-type]


DOCS = [
    kb("cpu", "cpu throttling saturation on the service raises latency"),
    kb("mem", "memory leak heap growth causes container oom kills"),
    kb("dns", "dns lookup failures nxdomain for internal hostnames"),
    kb("tls", "expired certificate handshake failures for clients"),
]


class ScriptedReranker:
    name = "scripted"

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.fitted: dict[str, str] = {}
        self.calls = 0

    def fit(self, docs):
        self.fitted = dict(docs)

    def score(self, query, texts):
        self.calls += 1
        return [next((v for k, v in self.scores.items() if k in t), 0.0) for t in texts]


def searcher(reranker, **kw) -> RerankingSearcher:
    s = RerankingSearcher(HybridSearcher(HashingEmbedder()), reranker, **kw)
    s.index(DOCS)
    return s


class TestOverlapReranker:
    def fitted(self) -> OverlapReranker:
        r = OverlapReranker()
        r.fit({d.id: d.search_text for d in DOCS})
        return r

    def test_full_coverage_beats_partial_and_none(self):
        r = self.fitted()
        full, partial, none = r.score(
            "memory leak heap", [DOCS[1].search_text, DOCS[0].search_text, DOCS[2].search_text]
        )
        assert full > partial >= none and none == 0.0

    def test_rare_terms_outweigh_common_ones(self):
        r = OverlapReranker()
        r.fit({"a": "common common rare", "b": "common", "c": "common", "d": "common"})
        s_rare, s_common = r.score("rare common", ["rare", "common"])
        assert s_rare > s_common

    def test_word_order_bigrams_add_score(self):
        r = OverlapReranker()
        r.fit({"a": "restart loop crash", "b": "crash loop restart"})
        in_order, shuffled = r.score("restart loop", ["restart loop crash", "crash loop restart"])
        assert in_order == pytest.approx(shuffled + r.bigram_weight)

    def test_exact_values(self):
        r = OverlapReranker(bigram_weight=0.5)
        r.fit({"a": "alpha beta", "b": "gamma"})
        # query terms alpha, beta; both present -> coverage 1; one bigram present -> 1; total 1 + 0.5
        assert r.score("alpha beta", ["alpha beta"]) == [pytest.approx(1.5)]

    def test_idf_values(self):
        r = OverlapReranker(bigram_weight=0.0)
        r.fit({"a": "xxx yyy", "b": "xxx", "c": "xxx"})
        idf_x, idf_y = math.log(1 + 0.5 / 3.5), math.log(1 + 2.5 / 1.5)
        assert r.score("xxx yyy", ["xxx"]) == [pytest.approx(idf_x / (idf_x + idf_y))]

    def test_unknown_query_term_counts_as_rare_and_missing(self):
        r = self.fitted()
        with_unknown, without = r.score("cpu zzzunseen", [DOCS[0].search_text]), r.score("cpu", [DOCS[0].search_text])
        assert with_unknown[0] < without[0]

    def test_empty_query_and_empty_corpus(self):
        r = OverlapReranker()
        assert r.score("", ["anything"]) == [0.0]
        r.fit({})
        assert r.score("cpu", ["cpu"]) == [pytest.approx(1.0)]
        assert r.score("cpu", []) == []

    def test_negative_bigram_weight_rejected(self):
        with pytest.raises(ValueError):
            OverlapReranker(-0.1)

    def test_deterministic(self):
        assert self.fitted().score("dns lookup", [DOCS[2].search_text]) == self.fitted().score(
            "dns lookup", [DOCS[2].search_text]
        )


class TestCachedReranker:
    def test_hits_misses_and_persistence(self, tmp_path):
        inner = ScriptedReranker({"cpu": 1.0})
        c = CachedReranker(inner, tmp_path)
        assert c.score("q", ["cpu doc", "other"]) == [1.0, 0.0] and (c.hits, c.misses) == (0, 2)
        assert c.score("q", ["cpu doc", "other"]) == [1.0, 0.0] and (c.hits, c.misses) == (2, 2) and inner.calls == 1
        c2 = CachedReranker(ScriptedReranker({}), tmp_path)
        assert c2.score("q", ["cpu doc"]) == [1.0]  # served from disk, inner would have said 0

    def test_key_depends_on_query_text_and_name(self, tmp_path):
        c = CachedReranker(ScriptedReranker({"x": 1.0}), tmp_path)
        c.score("q1", ["x"])
        c.score("q2", ["x"])
        c.score("q1", ["xy"])
        assert c.misses == 3
        other = CachedReranker(ScriptedReranker({"x": 5.0}), tmp_path)
        other.name = "different"
        assert other.score("q1", ["x"]) == [5.0]

    def test_partial_hit_and_corrupt_entry(self, tmp_path):
        inner = ScriptedReranker({"a": 2.0, "b": 3.0})
        c = CachedReranker(inner, tmp_path)
        c.score("q", ["a"])
        c._path("q", "b").write_text("not json")
        c._path("q", "a").with_suffix(".json")
        assert c.score("q", ["a", "b"]) == [2.0, 3.0]
        assert c.hits == 1 and c.misses == 2

    def test_empty_and_fit_passthrough(self, tmp_path):
        inner = ScriptedReranker({})
        c = CachedReranker(inner, tmp_path)
        assert c.score("q", []) == []
        c.fit({"a": "b"})
        assert inner.fitted == {"a": "b"}


class TestBuildReranker:
    def test_default_and_overlap(self, monkeypatch):
        monkeypatch.delenv("RERANKER", raising=False)
        monkeypatch.delenv("RERANK_CACHE_DIR", raising=False)
        assert isinstance(build_reranker(), OverlapReranker)
        assert isinstance(build_reranker("overlap"), OverlapReranker)

    def test_env_and_cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RERANKER", "overlap")
        monkeypatch.setenv("RERANK_CACHE_DIR", str(tmp_path))
        assert isinstance(build_reranker(), CachedReranker)
        assert isinstance(build_reranker(cache_dir=tmp_path), CachedReranker)

    def test_unknown(self):
        with pytest.raises(RerankerError, match="unknown reranker"):
            build_reranker("magic")

    def test_ce_without_dependency_or_model_is_a_clear_error(self):
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            with pytest.raises(RerankerError, match="sentence-transformers"):
                build_reranker("ce:some/model")
        else:
            pytest.skip("sentence-transformers installed; loading a model needs a download")


class TestMinMax:
    def test_values(self):
        assert _minmax([1.0, 3.0, 2.0]) == [0.0, 1.0, 0.5]
        assert _minmax([2.0, 2.0]) == [0.0, 0.0]
        assert _minmax([]) == []


class TestRerankingSearcher:
    def test_non_rerank_modes_pass_through(self):
        s = searcher(ScriptedReranker({}))
        for mode in ("bm25", "dense", "hybrid"):
            assert s.search("memory leak", 3, mode) == s.base.search("memory leak", 3, mode)

    def test_index_fits_the_reranker(self):
        r = ScriptedReranker({})
        searcher(r)
        assert set(r.fitted) == {"cpu", "mem", "dns", "tls"}

    def test_reranker_order_wins_when_alpha_zero(self):
        s = searcher(ScriptedReranker({"certificate": 9.0, "cpu": 5.0, "oom": 1.0}), candidates=4)
        hits = s.search("cpu memory dns certificate", 4, RERANK_MODE)
        assert [h.doc_id for h in hits][:2] == ["tls", "cpu"]
        assert [h.rank for h in hits] == [1, 2, 3, 4]
        top = hits[0]
        assert top.ranks[RERANK_MODE] == 1 and "hybrid" in top.ranks and top.score == pytest.approx(1.0)

    def test_alpha_one_reproduces_hybrid_order(self):
        s = searcher(ScriptedReranker({"certificate": 9.0}), alpha=1.0, candidates=4)
        q = "cpu memory dns certificate"
        assert [h.doc_id for h in s.search(q, 4, RERANK_MODE)] == [h.doc_id for h in s.base.search(q, 4, "hybrid")]

    def test_blend_formula(self):
        q = "cpu memory dns certificate"
        s = searcher(ScriptedReranker({"certificate": 9.0}), alpha=0.5, candidates=4)
        pool = s.base.search(q, 4, "hybrid")
        lo, hi = min(h.score for h in pool), max(h.score for h in pool)
        fused = {h.doc_id: (h.score - lo) / (hi - lo) for h in pool}
        hits = {h.doc_id: h for h in s.search(q, 4, RERANK_MODE)}
        assert hits["tls"].score == pytest.approx(0.5 * 1.0 + 0.5 * fused["tls"], abs=1e-6)
        assert hits["cpu"].score == pytest.approx(0.5 * 0.0 + 0.5 * fused["cpu"], abs=1e-6)

    def test_ties_keep_the_hybrid_order(self):
        s = searcher(ScriptedReranker({}), candidates=4)
        q = "cpu memory dns certificate"
        assert [h.doc_id for h in s.search(q, 4, RERANK_MODE)] == [h.doc_id for h in s.base.search(q, 4, "hybrid")]

    def test_candidates_limit_what_can_be_promoted(self):
        s = searcher(ScriptedReranker({"certificate": 9.0}), candidates=1)
        hits = s.search("cpu throttling latency", 1, RERANK_MODE)
        assert len(hits) == 1 and hits[0].doc_id != "tls"

    def test_candidates_never_below_k(self):
        s = searcher(ScriptedReranker({}), candidates=1)
        assert len(s.search("cpu memory dns certificate", 3, RERANK_MODE)) == 3

    def test_degenerate_inputs(self):
        s = searcher(ScriptedReranker({}))
        assert s.search("  ", 3, RERANK_MODE) == [] and s.search("cpu", 0, RERANK_MODE) == []
        assert s.search("zzzz qqqq", 3, RERANK_MODE) == []
        assert len(s) == 4 and s.store is s.base.store

    def test_wrong_score_count_is_an_error(self):
        class Bad(ScriptedReranker):
            def score(self, query, texts):
                return [1.0]

        with pytest.raises(RerankerError, match="different number"):
            searcher(Bad({})).search("cpu memory dns", 3, RERANK_MODE)

    @pytest.mark.parametrize("kw", [{"candidates": 0}, {"alpha": -0.1}, {"alpha": 1.1}])
    def test_invalid_config(self, kw):
        with pytest.raises(ValueError):
            RerankingSearcher(HybridSearcher(HashingEmbedder()), ScriptedReranker({}), **kw)

    def test_scores_come_from_the_texts_of_the_candidates(self):
        r = ScriptedReranker({})
        s = searcher(r)
        s.search("cpu memory", 2, RERANK_MODE)
        assert r.calls == 1


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval_eval, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(harness, "OPENINGS_LOG", tmp_path / "openings.log")
    monkeypatch.delenv("RERANKER", raising=False)
    return tmp_path


class TestEvalCli:
    def test_rerank_run_adds_mode_and_deltas(self, sandbox, capsys):
        args = [
            "--split",
            "dev",
            "--style",
            "paraphrase",
            "--reranker",
            "overlap",
            "--candidates",
            "10",
            "--alpha",
            "0.2",
        ]
        assert retrieval_eval.main([*args, "--tag", "rr"]) == 0
        report = json.loads((sandbox / "results" / "retrieval__rr.json").read_text())
        assert set(report["summary"]["paraphrase"]) == {"bm25", "dense", "hybrid", "rerank"}
        assert (report["meta"]["reranker"], report["meta"]["candidates"], report["meta"]["alpha"]) == (
            "overlap",
            10,
            0.2,
        )
        assert {"rerank_vs_hybrid_mrr", "rerank_vs_hybrid_hit1", "rerank_vs_hybrid_ndcg10"} <= set(
            report["deltas"]["paraphrase"]
        )
        assert "reranker=overlap" in capsys.readouterr().out

    def test_without_reranker_nothing_changes(self, sandbox):
        assert retrieval_eval.main(["--split", "dev", "--style", "log", "--tag", "plain"]) == 0
        report = json.loads((sandbox / "results" / "retrieval__plain.json").read_text())
        assert set(report["summary"]["log"]) == {"bm25", "dense", "hybrid"}
        assert report["meta"]["reranker"] is None and report["meta"]["candidates"] is None
        assert set(report["deltas"]["log"]) == {"hybrid_vs_bm25_mrr", "hybrid_vs_dense_mrr"}

    def test_env_enables_reranker(self, sandbox, monkeypatch):
        monkeypatch.setenv("RERANKER", "overlap")
        assert retrieval_eval.main(["--ids", "INC-001", "--style", "log", "--tag", "env"]) == 0
        assert "rerank" in json.loads((sandbox / "results" / "retrieval__env.json").read_text())["summary"]["log"]

    @pytest.mark.parametrize(
        "argv",
        [
            ["--reranker", "magic"],
            ["--reranker", "overlap", "--alpha", "2"],
            ["--reranker", "overlap", "--candidates", "0"],
        ],
    )
    def test_bad_reranker_config_returns_2(self, sandbox, capsys, argv):
        assert retrieval_eval.main([*argv, "--style", "log"]) == 2
        assert capsys.readouterr().out.startswith("ERROR")

    def test_overlap_rerank_quality_floor_on_dev_paraphrase(self, sandbox):
        retrieval_eval.main(["--split", "dev", "--style", "paraphrase", "--reranker", "overlap", "--tag", "floor"])
        s = json.loads((sandbox / "results" / "retrieval__floor.json").read_text())["summary"]["paraphrase"]
        assert s["rerank"]["hit@3"] >= 0.85
