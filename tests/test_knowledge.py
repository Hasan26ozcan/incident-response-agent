"""Stage 10 knowledge layer: tokenizer, BM25, RRF, embedders, vector stores, corpus, hybrid searcher."""

from __future__ import annotations

import math
import sys

import numpy as np
import pytest

from incident_agent.eval.retrieval_eval import load_gold
from incident_agent.knowledge import (
    BM25Index,
    CachedEmbedder,
    CorpusError,
    EmbedderError,
    HashingEmbedder,
    HybridSearcher,
    KBDoc,
    NumpyVectorStore,
    QdrantVectorStore,
    VectorStoreError,
    build_embedder,
    build_store,
    load_corpus,
    parse_doc,
    rrf_fuse,
)
from incident_agent.knowledge.text import normalise_message, stem, tokenize

BODY = "This body is long enough to satisfy the minimum length rule for knowledge base documents in tests."


def doc(
    doc_id: str, text: str = BODY, *, kind: str = "runbook", cls: str | None = "x_class", title: str = "A title"
) -> KBDoc:
    if len(text) < 80:
        text = f"{text} " + "filler words padding this test document beyond the minimum length requirement."
    return KBDoc(id=doc_id, doc_type=kind, title=title, failure_class=cls, text=text)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- text
class TestText:
    def test_inflections_collapse_to_one_stem(self):
        forms = ["restart", "restarts", "restarted", "restarting"]
        assert len({stem(f) for f in forms}) == 1
        assert stem("queries") == "query"
        assert stem("pools") == "pool"
        assert stem("running") == stem("runs") == "run"

    def test_stemmer_leaves_short_words_and_special_endings_alone(self):
        for word in ("status", "less", "is", "bus", "ssl", "ing", "red"):
            assert stem(word) == word

    def test_identifiers_are_kept_whole_and_split(self):
        toks = tokenize("cpu_percent latency_p99_ms")
        assert {"cpu", "percent", "cpu_percent", "latency", "p99", "latency_p99_ms"} <= set(toks)
        camel = tokenize("OOMKilled")
        assert {"oom", "kill", "oomkilled"} <= set(camel)

    def test_numbers_are_dropped_except_http_error_codes(self):
        assert tokenize("503 errors at 12345 and 3 times, 200 ok") == ["503", "error", "time", "ok"]

    def test_stopwords_and_single_letters_are_removed(self):
        assert tokenize("the a of and to x") == []

    def test_term_frequency_is_preserved(self):
        assert tokenize("cpu cpu cpu").count("cpu") == 3

    def test_empty_and_punctuation_only(self):
        assert tokenize("") == [] and tokenize("!!! --- ...") == []

    def test_normalise_message_collapses_numbers(self):
        assert normalise_message("took 123ms (limit 250.5ms), depth=40") == "took #ms (limit #ms), depth=#"


# --------------------------------------------------------------------------- BM25
class TestBM25:
    def test_term_frequency_matters_regression(self):
        """The legacy index recorded each document once per term, so tf was always 1."""
        idx = BM25Index()
        idx.build({"many": "cpu cpu cpu cpu cpu saturated", "once": "cpu disk network memory cache queue timeout"})
        hits = idx.search("cpu")
        assert [h.doc_id for h in hits] == ["many", "once"]
        assert hits[0].score > hits[1].score

    def test_exact_score_matches_the_okapi_definition(self):
        k1, b = 1.2, 0.75
        idx = BM25Index(k1, b)
        docs = {"d1": "alpha alpha beta", "d2": "beta gamma", "d3": "delta delta delta delta"}
        idx.build(docs)
        n, avg = 3, (3 + 2 + 4) / 3
        idf = math.log(1 + (n - 1 + 0.5) / (1 + 0.5))  # "alpha" appears in one document
        expected = idf * 2 * (k1 + 1) / (2 + k1 * (1 - b + b * 3 / avg))
        (hit,) = idx.search("alpha")
        assert hit.doc_id == "d1" and hit.score == pytest.approx(expected, abs=1e-5) and hit.rank == 1

    def test_rarer_terms_weigh_more(self):
        idx = BM25Index()
        idx.build({"a": "common rare", "b": "common", "c": "common", "d": "common"})
        assert idx.idf("rare") > idx.idf("common") > 0
        assert idx.search("common rare")[0].doc_id == "a"

    def test_length_normalisation_prefers_the_shorter_document(self):
        idx = BM25Index()
        idx.build({"short": "disk full", "long": "disk full " + "filler " * 40})
        assert [h.doc_id for h in idx.search("disk full")] == ["short", "long"]

    def test_b_zero_disables_length_normalisation(self):
        idx = BM25Index(b=0.0)
        idx.build({"short": "disk full", "long": "disk full " + "filler " * 40})
        a, b = idx.search("disk")
        assert a.score == b.score

    def test_ties_are_broken_by_document_id(self):
        idx = BM25Index()
        idx.build({"b": "same words here", "a": "same words here"})
        assert [h.doc_id for h in idx.search("words")] == ["a", "b"]

    def test_ranks_are_one_based_and_k_limits(self):
        idx = BM25Index()
        idx.build({f"d{i}": "memory " * (i + 1) + "x" * 3 for i in range(5)})
        hits = idx.search("memory", k=3)
        assert [h.rank for h in hits] == [1, 2, 3]

    def test_query_term_repetition_does_not_change_the_score(self):
        idx = BM25Index()
        idx.build({"a": "disk full error", "b": "network error"})
        assert idx.search("disk")[0].score == idx.search("disk disk disk")[0].score

    @pytest.mark.parametrize("query", ["", "   ", "zzzzqq", "the of"])
    def test_no_match_returns_nothing(self, query):
        idx = BM25Index()
        idx.build({"a": "disk full"})
        assert idx.search(query) == []

    def test_k_zero_unbuilt_and_empty_collection(self):
        idx = BM25Index()
        assert idx.search("x") == [] and len(idx) == 0
        idx.build({})
        assert idx.search("disk") == []
        idx.build({"a": "disk"})
        assert idx.search("disk", k=0) == []

    def test_rebuild_replaces_the_old_collection(self):
        idx = BM25Index()
        idx.build({"a": "disk"})
        idx.build({"b": "network"})
        assert idx.search("disk") == [] and [h.doc_id for h in idx.search("network")] == ["b"] and len(idx) == 1

    @pytest.mark.parametrize("kwargs", [{"k1": -1.0}, {"b": 1.5}, {"b": -0.1}])
    def test_invalid_parameters_are_rejected(self, kwargs):
        with pytest.raises(ValueError):
            BM25Index(**kwargs)

    def test_scores_are_deterministic(self):
        docs = {"a": "memory heap gc pause", "b": "heap dump leak", "c": "network"}
        one, two = BM25Index(), BM25Index()
        one.build(docs)
        two.build(dict(reversed(list(docs.items()))))
        assert one.search("heap gc leak") == two.search("heap gc leak")


# --------------------------------------------------------------------------- RRF
class TestRRF:
    def test_exact_scores(self):
        fused = {f.doc_id: f for f in rrf_fuse({"a": ["x", "y"], "b": ["y", "z"]}, k=60)}
        assert fused["y"].score == pytest.approx(1 / 62 + 1 / 61, abs=1e-7)
        assert fused["x"].score == pytest.approx(1 / 61, abs=1e-7)
        assert fused["z"].score == pytest.approx(1 / 62, abs=1e-7)
        assert fused["y"].ranks == {"a": 2, "b": 1}

    def test_ranker_that_did_not_return_a_document_contributes_nothing(self):
        """The legacy fusion gave absent documents a made-up rank and still added a term."""
        fused = rrf_fuse({"a": ["only-a"], "b": ["only-b", "shared"], "c": ["shared"]})
        by = {f.doc_id: f for f in fused}
        assert by["only-a"].score == pytest.approx(1 / 61, abs=1e-7)
        assert by["only-a"].ranks == {"a": 1}
        assert fused[0].doc_id == "shared"  # found by two rankers beats found first by one

    def test_agreement_beats_a_single_top_rank(self):
        fused = rrf_fuse({"a": ["p", "q", "r"], "b": ["q", "r", "p"]})
        assert fused[0].doc_id == "q"

    def test_weights_scale_each_ranker(self):
        fused = rrf_fuse({"a": ["x"], "b": ["y"]}, weights={"a": 2.0, "b": 1.0})
        assert [f.doc_id for f in fused] == ["x", "y"]
        assert fused[0].score == pytest.approx(2 / 61, abs=1e-7)

    def test_zero_weight_removes_a_ranker(self):
        fused = rrf_fuse({"a": ["x", "y"], "b": ["y", "x"]}, weights={"b": 0.0})
        assert [f.doc_id for f in fused] == ["x", "y"]

    def test_unlisted_ranker_defaults_to_weight_one(self):
        fused = rrf_fuse({"a": ["x"], "b": ["y"]}, weights={"a": 1.0})
        assert fused[0].score == fused[1].score

    def test_larger_k_flattens_rank_differences(self):
        small = rrf_fuse({"a": ["x", "y"]}, k=1)
        large = rrf_fuse({"a": ["x", "y"]}, k=1000)
        assert small[0].score / small[1].score > large[0].score / large[1].score

    def test_duplicates_inside_one_ranker_count_once_at_the_best_position(self):
        fused = rrf_fuse({"a": ["x", "y", "x"]})
        by = {f.doc_id: f for f in fused}
        assert by["x"].score == pytest.approx(1 / 61, abs=1e-7) and by["x"].ranks == {"a": 1}

    def test_ties_are_ordered_by_id_for_determinism(self):
        fused = rrf_fuse({"a": ["b"], "b": ["a"]})
        assert [f.doc_id for f in fused] == ["a", "b"]

    def test_empty_inputs(self):
        assert rrf_fuse({}) == [] and rrf_fuse({"a": []}) == []

    @pytest.mark.parametrize("kwargs", [{"k": 0}, {"k": -5}, {"weights": {"a": -1.0}}])
    def test_invalid_arguments(self, kwargs):
        with pytest.raises(ValueError):
            rrf_fuse({"a": ["x"]}, **kwargs)


# --------------------------------------------------------------------------- embedders
class TestHashingEmbedder:
    def test_deterministic_across_instances_and_pinned(self):
        a = HashingEmbedder().embed_documents(["cpu throttling on the checkout service"])
        b = HashingEmbedder().embed_documents(["cpu throttling on the checkout service"])
        assert np.array_equal(a, b)
        # pins the hash function: Python's salted hash() would change this between processes
        assert int(np.argmax(np.abs(HashingEmbedder(64).embed_documents(["cpu"])[0]))) == 56

    def test_rows_are_unit_length_and_float32(self):
        m = HashingEmbedder().embed_documents(["disk full", "network partition between zones"])
        assert m.dtype == np.float32 and m.shape == (2, 512)
        assert np.linalg.norm(m, axis=1) == pytest.approx([1.0, 1.0], abs=1e-5)

    def test_text_without_tokens_gives_a_zero_vector(self):
        m = HashingEmbedder().embed_documents(["the of and"])
        assert float(np.linalg.norm(m)) == 0.0

    def test_related_text_is_closer_than_unrelated_text(self):
        e = HashingEmbedder()
        q, near, far = e.embed_documents(["heap memory leak gc pause", "memory leak heap growth", "dns lookup failure"])
        assert float(q @ near) > float(q @ far)

    def test_queries_and_documents_use_the_same_space(self):
        e = HashingEmbedder()
        assert np.array_equal(e.embed_queries(["disk full"]), e.embed_documents(["disk full"]))

    def test_empty_list(self):
        assert HashingEmbedder(32).embed_documents([]).shape == (0, 32)

    def test_bigrams_change_the_vector(self):
        with_bi = HashingEmbedder(bigrams=True).embed_documents(["connection pool exhausted"])
        without = HashingEmbedder(bigrams=False).embed_documents(["connection pool exhausted"])
        assert not np.array_equal(with_bi, without) and HashingEmbedder(bigrams=False).name.endswith("512")

    def test_dimension_must_be_reasonable(self):
        with pytest.raises(ValueError):
            HashingEmbedder(4)


class _Counting:
    name = "counting"
    dim = 4

    def __init__(self) -> None:
        self.doc_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts):
        self.doc_calls += len(texts)
        return np.tile(np.array([1, 0, 0, 0], dtype=np.float32), (len(texts), 1))

    def embed_queries(self, texts):
        self.query_calls += len(texts)
        return np.tile(np.array([0, 1, 0, 0], dtype=np.float32), (len(texts), 1))


class TestCachedEmbedder:
    def test_second_call_is_served_from_disk(self, tmp_path):
        inner = _Counting()
        c = CachedEmbedder(inner, tmp_path)
        first = c.embed_documents(["a", "b"])
        second = CachedEmbedder(inner, tmp_path).embed_documents(["a", "b"])
        assert inner.doc_calls == 2 and np.array_equal(first, second)
        assert c.misses == 2

    def test_documents_and_queries_are_cached_separately(self, tmp_path):
        inner = _Counting()
        c = CachedEmbedder(inner, tmp_path)
        d = c.embed_documents(["same"])
        q = c.embed_queries(["same"])
        assert not np.array_equal(d, q) and inner.doc_calls == 1 and inner.query_calls == 1

    def test_only_missing_texts_reach_the_model_and_order_is_kept(self, tmp_path):
        inner = _Counting()
        c = CachedEmbedder(inner, tmp_path)
        c.embed_documents(["a"])
        out = c.embed_documents(["a", "b", "a"])
        assert inner.doc_calls == 2 and out.shape == (3, 4)
        assert c.hits == 2

    def test_corrupt_or_wrong_shape_cache_files_are_recomputed(self, tmp_path):
        inner = _Counting()
        c = CachedEmbedder(inner, tmp_path)
        c.embed_documents(["a"])
        (path,) = list(tmp_path.glob("*.npy"))
        path.write_bytes(b"not a numpy file")
        c.embed_documents(["a"])
        assert inner.doc_calls == 2
        np.save(path, np.zeros(9, dtype=np.float32))  # wrong dimension
        c.embed_documents(["a"])
        assert inner.doc_calls == 3

    def test_different_embedder_names_never_share_entries(self, tmp_path):
        class Other(_Counting):
            name = "other"

        a, b = _Counting(), Other()
        CachedEmbedder(a, tmp_path).embed_documents(["t"])
        CachedEmbedder(b, tmp_path).embed_documents(["t"])
        assert b.doc_calls == 1

    def test_no_temporary_files_are_left_behind_and_empty_input_is_fine(self, tmp_path):
        c = CachedEmbedder(_Counting(), tmp_path / "nested")
        c.embed_documents(["a"])
        assert not list((tmp_path / "nested").glob("*.tmp*"))
        assert c.embed_documents([]).shape == (0, 4)


class TestBuildEmbedder:
    def test_default_is_hashing(self, monkeypatch):
        monkeypatch.delenv("EMBEDDER", raising=False)
        monkeypatch.delenv("EMBED_CACHE_DIR", raising=False)
        assert build_embedder().name == "hashing-512-bi"

    def test_spec_and_environment(self, monkeypatch):
        assert build_embedder("hashing:64").dim == 64
        monkeypatch.setenv("EMBEDDER", "hashing:128")
        assert build_embedder().dim == 128

    def test_cache_directory_wraps_the_embedder(self, tmp_path):
        assert isinstance(build_embedder("hashing", tmp_path), CachedEmbedder)

    @pytest.mark.parametrize("spec", ["nope", "hashing:abc", "hashing:2"])
    def test_bad_specs(self, spec):
        with pytest.raises(EmbedderError):
            build_embedder(spec)

    def test_sentence_transformers_missing_gives_an_actionable_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        with pytest.raises(EmbedderError, match="pip install"):
            build_embedder("st:some/model")


# --------------------------------------------------------------------------- vector stores
def _stores():
    out = [pytest.param(NumpyVectorStore, id="numpy")]
    try:
        import qdrant_client  # noqa: F401

        out.append(pytest.param(QdrantVectorStore, id="qdrant"))
    except ImportError:  # pragma: no cover
        out.append(pytest.param(None, id="qdrant", marks=pytest.mark.skip(reason="qdrant-client not installed")))
    return out


@pytest.mark.parametrize("factory", _stores())
class TestVectorStores:
    def make(self, factory, dim=3):
        store = factory()
        store.reset(dim)
        return store

    def test_ranking_by_cosine(self, factory):
        s = self.make(factory)
        s.add(["a", "b", "c"], np.array([[1, 0, 0], [0, 1, 0], [0.9, 0.1, 0]], dtype=np.float32))
        hits = s.search(np.array([1, 0, 0], dtype=np.float32), 3)
        assert [h.doc_id for h in hits] == ["a", "c", "b"] and [h.rank for h in hits] == [1, 2, 3]
        assert hits[0].score == pytest.approx(1.0, abs=1e-4)

    def test_ties_are_ordered_by_id(self, factory):
        s = self.make(factory)
        s.add(["z", "a"], np.array([[1, 0, 0], [1, 0, 0]], dtype=np.float32))
        assert [h.doc_id for h in s.search(np.array([1, 0, 0], dtype=np.float32), 2)] == ["a", "z"]

    def test_k_limits_and_k_larger_than_count(self, factory):
        s = self.make(factory)
        s.add(["a", "b"], np.eye(3, dtype=np.float32)[:2])
        v = np.array([1, 1, 0], dtype=np.float32)
        assert len(s.search(v, 1)) == 1 and len(s.search(v, 50)) == 2 and s.search(v, 0) == []

    def test_count_and_empty_store(self, factory):
        s = self.make(factory)
        assert s.count() == 0 and s.search(np.array([1, 0, 0], dtype=np.float32), 3) == []
        s.add(["a"], np.array([[1, 0, 0]], dtype=np.float32))
        assert s.count() == 1

    def test_reset_clears_everything(self, factory):
        s = self.make(factory)
        s.add(["a"], np.array([[1, 0, 0]], dtype=np.float32))
        s.reset(3)
        assert s.count() == 0 and s.search(np.array([1, 0, 0], dtype=np.float32), 3) == []

    def test_validation_errors(self, factory):
        s = self.make(factory)
        with pytest.raises(VectorStoreError):
            s.add(["a", "b"], np.zeros((1, 3), dtype=np.float32))
        with pytest.raises(VectorStoreError):
            s.add(["a", "a"], np.zeros((2, 3), dtype=np.float32))
        with pytest.raises(VectorStoreError):
            s.add(["a"], np.zeros((1, 5), dtype=np.float32))
        s.add(["a"], np.array([[1, 0, 0]], dtype=np.float32))
        with pytest.raises(VectorStoreError):
            s.add(["a"], np.array([[0, 1, 0]], dtype=np.float32))

    def test_adding_nothing_is_a_no_op(self, factory):
        s = self.make(factory)
        s.add([], np.zeros((0, 3), dtype=np.float32))
        assert s.count() == 0


def test_numpy_store_adds_in_batches_and_auto_sizes():
    s = NumpyVectorStore()
    s.add(["a"], np.array([[1, 0]], dtype=np.float32))  # no reset(): dimension inferred
    s.add(["b"], np.array([[0, 1]], dtype=np.float32))
    assert s.count() == 2 and [h.doc_id for h in s.search(np.array([0, 1], dtype=np.float32), 1)] == ["b"]


def test_numpy_and_qdrant_agree_on_random_data():
    pytest.importorskip("qdrant_client")
    rng = np.random.default_rng(7)
    vectors = rng.normal(size=(40, 16)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    ids = [f"d{i:02d}" for i in range(40)]
    a, b = NumpyVectorStore(), QdrantVectorStore()
    for s in (a, b):
        s.reset(16)
        s.add(ids, vectors)
    for _ in range(10):
        q = rng.normal(size=16).astype(np.float32)
        q /= np.linalg.norm(q)
        ha, hb = a.search(q, 10), b.search(q, 10)
        assert [h.doc_id for h in ha] == [h.doc_id for h in hb]
        assert [h.score for h in ha] == pytest.approx([h.score for h in hb], abs=1e-4)


def test_qdrant_persists_to_a_local_path(tmp_path):
    pytest.importorskip("qdrant_client")
    store = QdrantVectorStore(str(tmp_path / "q"), collection="c")
    store.reset(3)
    store.add(["a"], np.array([[1, 0, 0]], dtype=np.float32))
    assert [h.doc_id for h in store.search(np.array([1, 0, 0], dtype=np.float32), 1)] == ["a"]


class TestBuildStore:
    def test_default_and_explicit(self, monkeypatch):
        monkeypatch.delenv("VECTOR_STORE", raising=False)
        assert isinstance(build_store(), NumpyVectorStore)
        assert isinstance(build_store("numpy"), NumpyVectorStore)

    def test_qdrant_from_environment(self, monkeypatch):
        pytest.importorskip("qdrant_client")
        monkeypatch.setenv("VECTOR_STORE", "qdrant")
        monkeypatch.delenv("QDRANT_URL", raising=False)
        assert isinstance(build_store(), QdrantVectorStore)

    def test_unknown_store(self):
        with pytest.raises(VectorStoreError, match="unknown vector store"):
            build_store("faiss")


# --------------------------------------------------------------------------- corpus
GOOD = (
    "---\nid: rb-x\ntype: runbook\ntitle: Runbook: something broke\nclass: x_class   # the class\n---\n" + BODY + "\n"
)


class TestCorpus:
    def test_parse_valid_document(self):
        d = parse_doc(GOOD)
        assert (d.id, d.doc_type, d.failure_class) == ("rb-x", "runbook", "x_class")
        assert d.title == "Runbook: something broke" and d.text == BODY
        assert d.search_text.startswith("Runbook: something broke\n")

    def test_class_is_optional_for_references(self):
        d = parse_doc("---\nid: ref-a\ntype: reference\ntitle: Some reference\n---\n" + BODY)
        assert d.failure_class is None

    @pytest.mark.parametrize(
        "raw,message",
        [
            ("no front matter\n" + BODY, "missing front matter"),
            ("---\nid: a\ntype: runbook\n", "not closed"),
            ("---\nid: a\ntype: runbook\ntitle: A title here\nowner: me\n---\n" + BODY, "bad front matter line"),
            ("---\nid a\n---\n" + BODY, "bad front matter line"),
            ("---\nid: a\nid: b\ntype: runbook\ntitle: A title here\n---\n" + BODY, "duplicate"),
            ("---\nid: a\ntype: runbook\n---\n" + BODY, "missing front matter keys"),
            ("---\nid: Bad ID\ntype: runbook\ntitle: A title here\n---\n" + BODY, "slug"),
            ("---\nid: a\ntype: memo\ntitle: A title here\n---\n" + BODY, "doc_type"),
            ("---\nid: a\ntype: runbook\ntitle: A title here\n---\ntoo short", "text"),
        ],
    )
    def test_malformed_documents_are_rejected_with_the_reason(self, raw, message):
        with pytest.raises(CorpusError, match=message):
            parse_doc(raw, "f.md")

    def test_blank_front_matter_lines_and_bom_are_tolerated(self):
        d = parse_doc("﻿---\nid: rb-x\n\ntype: runbook\ntitle: A title here\n---\n" + BODY)
        assert d.id == "rb-x"

    def test_load_checks_file_name_matches_id(self, tmp_path):
        (tmp_path / "other.md").write_text(GOOD)
        with pytest.raises(CorpusError, match="must equal the file name"):
            load_corpus(tmp_path)

    def test_load_rejects_an_empty_directory(self, tmp_path):
        with pytest.raises(CorpusError, match="no knowledge-base documents"):
            load_corpus(tmp_path)

    def test_load_reads_documents_in_sorted_order(self, tmp_path):
        for name in ("rb-b", "rb-a"):
            (tmp_path / f"{name}.md").write_text(GOOD.replace("rb-x", name))
        assert [d.id for d in load_corpus(tmp_path)] == ["rb-a", "rb-b"]

    def test_real_corpus_has_expected_composition(self):
        docs = load_corpus()
        kinds = {k: sum(d.doc_type == k for d in docs) for k in ("runbook", "postmortem", "reference")}
        assert kinds == {"runbook": 27, "postmortem": 27, "reference": 12}
        assert len({d.id for d in docs}) == len(docs)

    def test_every_gold_failure_class_has_a_runbook_and_a_postmortem(self):
        docs = load_corpus()
        classes = {c for e in load_gold().values() for c in [e["primary"], *e["secondary"]]}
        for cls in classes:
            assert {d.doc_type for d in docs if d.failure_class == cls} >= {"runbook", "postmortem"}, cls


# --------------------------------------------------------------------------- hybrid searcher
def small_corpus() -> list[KBDoc]:
    return [
        doc(
            "rb-mem",
            "heap memory leak garbage collection pause out of memory killed container restart",
            title="Memory leak",
        ),
        doc(
            "rb-disk",
            "disk full no space left on device log rotation partition usage cleanup",
            title="Disk full",
            cls="disk",
        ),
        doc(
            "rb-dns",
            "dns resolution failure hostname not found resolver coredns record",
            title="DNS failure",
            cls="dns",
        ),
        doc(
            "rb-tls",
            "tls certificate expired handshake failed renew reload clients reject",
            title="Certificate",
            cls="tls",
        ),
    ]


def searcher(**kw) -> HybridSearcher:
    s = HybridSearcher(HashingEmbedder(), NumpyVectorStore(), **kw)
    s.index(small_corpus())
    return s


class TestHybridSearcher:
    def test_every_mode_finds_the_right_document(self):
        s = searcher()
        for mode in ("bm25", "dense", "hybrid"):
            assert s.search("memory leak and gc pauses", 3, mode)[0].doc_id == "rb-mem", mode

    def test_hit_carries_metadata_and_per_retriever_ranks(self):
        (top, *_) = searcher().search("expired certificate handshake", 3)
        assert (top.doc_id, top.title, top.doc_type, top.failure_class, top.rank) == (
            "rb-tls",
            "Certificate",
            "runbook",
            "tls",
            1,
        )
        assert set(top.ranks) == {"bm25", "dense"}

    def test_single_retriever_modes_report_only_their_own_rank(self):
        s = searcher()
        assert set(s.search("disk space", 2, "bm25")[0].ranks) == {"bm25"}
        assert set(s.search("disk space", 2, "dense")[0].ranks) == {"dense"}

    def test_k_limits_and_ranks_are_consecutive(self):
        out = searcher().search("memory disk dns certificate", 2)
        assert len(out) == 2 and [h.rank for h in out] == [1, 2]

    def test_blank_query_empty_index_and_k_zero(self):
        s = searcher()
        assert s.search("   ", 3) == [] and s.search("memory", 0) == []
        assert HybridSearcher(HashingEmbedder()).search("memory", 3) == []

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError, match="mode"):
            searcher().search("x", 3, "magic")

    def test_zero_dense_weight_reproduces_the_bm25_order(self):
        weighted = searcher(weights={"bm25": 1.0, "dense": 0.0})
        q = "memory disk dns certificate failure"
        assert [h.doc_id for h in weighted.search(q, 4)] == [h.doc_id for h in weighted.search(q, 4, "bm25")]

    def test_depth_limits_candidates_per_retriever(self):
        hits = searcher(depth=2).search("memory disk dns certificate", 2)
        assert hits and all(rank <= 2 for h in hits for rank in h.ranks.values())

    def test_depth_is_never_below_k(self):
        assert len(searcher(depth=1).search("memory disk dns certificate failure", 4, "bm25")) == 4

    def test_duplicate_ids_are_rejected(self):
        s = HybridSearcher(HashingEmbedder())
        with pytest.raises(ValueError, match="duplicate"):
            s.index([doc("a"), doc("a")])

    def test_reindexing_replaces_the_corpus(self):
        s = searcher()
        s.index([doc("only-one", "kernel panic reboot loop on node hardware fault memory board replaced")])
        assert len(s) == 1 and [h.doc_id for h in s.search("memory", 3)] == ["only-one"]

    def test_invalid_depth(self):
        with pytest.raises(ValueError):
            HybridSearcher(HashingEmbedder(), depth=0)

    def test_results_are_deterministic(self):
        a, b = searcher(), searcher()
        assert a.search("memory leak dns", 4) == b.search("memory leak dns", 4)

    def test_works_over_the_qdrant_store_too(self):
        pytest.importorskip("qdrant_client")
        s = HybridSearcher(HashingEmbedder(), QdrantVectorStore())
        s.index(small_corpus())
        ref = searcher()
        for q in ("memory leak and gc pauses", "expired certificate", "dns hostname not found"):
            assert [h.doc_id for h in s.search(q, 4)] == [h.doc_id for h in ref.search(q, 4)]

    def test_orthogonal_dense_scores_are_not_treated_as_matches(self):
        class Orthogonal(HashingEmbedder):
            def embed_queries(self, texts):
                return np.zeros((len(texts), self.dim), dtype=np.float32)

        s = HybridSearcher(Orthogonal(), NumpyVectorStore())
        s.index(small_corpus())
        assert s.search("memory", 3, "dense") == []
        assert [h.doc_id for h in s.search("memory", 3)] == ["rb-mem"]  # bm25 alone carries the fusion
