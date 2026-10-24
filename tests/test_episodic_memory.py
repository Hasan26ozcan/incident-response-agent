"""Tests for episodic memory (Stage 12): episodes, reflection, persistence, recall and prediction."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from incident_agent.knowledge import HashingEmbedder
from incident_agent.memory.episodic import (
    Episode,
    EpisodeMemoryError,
    EpisodicMemory,
    Recollection,
    Reflection,
    reflect,
)

FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
CPU = "checkout-api cpu_percent latency_p99_ms; worker pool saturated; request queue growing; cpu throttling"
MEM = "reporting-service heap_used_mb; container killed out of memory; heap growth garbage collection long pauses"
DNS = "billing-api resolver failure; hostname lookup fails nxdomain; upstream host unknown"


def memory(path=None, **kw) -> EpisodicMemory:
    return EpisodicMemory(HashingEmbedder(), path, clock=lambda: FIXED, **kw)


def filled(path=None) -> EpisodicMemory:
    m = memory(path)
    m.record("INC-001", CPU, "cpu_exhaustion", diagnosed_class="cpu_exhaustion")
    m.record("INC-002", MEM, "memory_leak", diagnosed_class="gc_pause_storm")
    m.record("INC-009", DNS, "dns_misconfig", diagnosed_class=None)
    return m


class TestReflect:
    def test_confirmed(self):
        r = reflect("a", "a")
        assert r == Reflection(outcome="confirmed", lesson="Diagnosis a was confirmed.")

    def test_confirmed_with_agreeing_memory(self):
        assert reflect("a", "a", "a").lesson.endswith("Memory had pointed the same way.")
        assert "Memory" not in reflect("a", "a", "b").lesson

    def test_misdiagnosed(self):
        r = reflect("a", "b")
        assert r.outcome == "misdiagnosed" and "Believed a but it was b" in r.lesson and "check b signals" in r.lesson
        assert "Memory had suggested c." in reflect("a", "b", "c").lesson

    def test_unclassified(self):
        r = reflect(None, "b")
        assert r.outcome == "unclassified" and "resolved as b" in r.lesson

    def test_classes_compare_exactly_not_as_substrings(self):
        assert reflect("memory_leak", "slow_memory_leak").outcome == "misdiagnosed"


class TestEpisode:
    def base(self, **kw):
        d = {"incident_id": "INC-1", "created_at": "t", "observation": "some obs", "resolved_class": "c"}
        return Episode(**{**d, **kw})

    def test_diagnosis_correct(self):
        assert self.base().diagnosis_correct is None
        assert self.base(diagnosed_class="c").diagnosis_correct is True
        assert self.base(diagnosed_class="x").diagnosis_correct is False

    @pytest.mark.parametrize(
        "bad",
        [
            {"incident_id": ""},
            {"incident_id": "has space"},
            {"incident_id": "a_b"},
            {"observation": "ab"},
            {"resolved_class": ""},
        ],
    )
    def test_validation(self, bad):
        with pytest.raises(ValueError):
            self.base(**bad)

    def test_frozen(self):
        with pytest.raises(ValueError):
            self.base().resolved_class = "z"  # type: ignore[misc]


class TestRecordAndRecall:
    def test_record_uses_clock_and_writes_reflection(self):
        m = memory()
        e = m.record("INC-1", CPU, "cpu_exhaustion", diagnosed_class="memory_leak", recalled_class="cpu_exhaustion")
        assert e.created_at == "2026-01-02T03:04:05+00:00" == m.now()
        assert e.reflection is not None and e.reflection.outcome == "misdiagnosed"
        assert "Memory had suggested cpu_exhaustion." in e.reflection.lesson
        assert e.is_false_alarm is False and len(m) == 1

    def test_same_incident_replaces_earlier_episode(self):
        m = memory()
        m.record("INC-1", CPU, "cpu_exhaustion")
        m.record("INC-1", CPU, "memory_leak")
        assert len(m) == 1 and m.episodes[0].resolved_class == "memory_leak"

    def test_episodes_sorted_by_incident_id(self):
        m = filled()
        assert [e.incident_id for e in m.episodes] == ["INC-001", "INC-002", "INC-009"]

    def test_recall_finds_the_matching_episode_first(self):
        m = filled()
        r = m.recall("pods killed out of memory, heap keeps growing, long garbage collection pauses", 3)
        assert r[0].episode.incident_id == "INC-002" and [x.rank for x in r] == [1, 2, 3]
        assert r[0].similarity > r[1].similarity > 0

    def test_recall_limits_and_empty_cases(self):
        m = filled()
        assert len(m.recall(CPU, 1)) == 1 and m.recall(CPU, 0) == [] and memory().recall(CPU, 3) == []

    def test_recall_sees_episodes_added_later(self):
        m = filled()
        assert m.recall("disk full no space left", 3) == []  # nothing in memory shares a word with it
        m.record("INC-010", "disk usage 100 percent no space left on device write failed", "disk_full")
        assert m.recall("disk full no space left on device", 1)[0].episode.incident_id == "INC-010"

    def test_forget(self):
        m = filled()
        assert m.recall(CPU, 1)[0].episode.incident_id == "INC-001"  # builds the index
        assert m.forget("INC-001") is True and m.forget("INC-001") is False and len(m) == 2
        assert all(r.episode.incident_id != "INC-001" for r in m.recall(CPU, 3))

    def test_lessons_for(self):
        m = filled()
        lessons = m.lessons_for("heap growth long garbage collection pauses out of memory", 1)
        assert lessons == ["Believed gc_pause_storm but it was memory_leak; check memory_leak signals first."]

    def test_short_observation_is_indexable(self):
        m = memory()
        m.record("INC-1", "cpu high", "false_alarm")
        assert m.recall("cpu high", 1)[0].episode.incident_id == "INC-1"


class TestPredict:
    def test_abstains_on_empty_memory(self):
        p = memory().predict(CPU)
        assert (p.label, p.similarity, p.share, p.recollections) == (None, 0.0, 0.0, ())

    def test_predicts_the_resolved_class_not_the_diagnosed_one(self):
        p = filled().predict("container killed out of memory heap growth garbage collection pauses", 3)
        assert p.label == "memory_leak" and p.similarity > 0.5 and 0 < p.share <= 1
        assert p.recollections[0].episode.incident_id == "INC-002"

    def test_score_weighted_vote_across_episodes_of_one_class(self):
        m = memory()
        m.record("INC-1", "alpha beta gamma delta epsilon zeta eta theta", "class_a")
        m.record("INC-2", "alpha beta gamma delta epsilon zeta eta iota", "class_a")
        m.record("INC-3", "alpha beta gamma delta epsilon zeta kappa lambda", "class_b")
        p = m.predict("alpha beta gamma delta epsilon zeta eta", 3)
        assert p.label == "class_a" and p.share > 0.5

    def test_identical_observations_the_earlier_id_ranks_first_and_wins(self):
        m = memory()
        m.record("INC-1", "identical observation text for both episodes here", "zeta_class")
        m.record("INC-2", "identical observation text for both episodes here", "alpha_class")
        p = m.predict("identical observation text for both episodes here", 2)
        assert [r.episode.incident_id for r in p.recollections] == ["INC-1", "INC-2"] and p.label == "zeta_class"

    def test_unrelated_query_has_low_similarity(self):
        assert filled().predict("completely different words zebra quantum").similarity < 0.3


class TestPersistence:
    def test_roundtrip_and_replacement(self, tmp_path):
        filled(tmp_path)
        again = memory(tmp_path)
        assert [e.incident_id for e in again.episodes] == ["INC-001", "INC-002", "INC-009"]
        assert again.episodes[1].reflection is not None and again.episodes[1].diagnosis_correct is False
        again.record("INC-001", CPU, "memory_leak")
        assert memory(tmp_path).episodes[0].resolved_class == "memory_leak"
        assert len((tmp_path / "episodes.jsonl").read_text().splitlines()) == 3

    def test_forget_persists(self, tmp_path):
        m = filled(tmp_path)
        m.forget("INC-009")
        assert len(memory(tmp_path)) == 2

    def test_directory_is_created_and_no_temp_files_remain(self, tmp_path):
        target = tmp_path / "deep" / "dir"
        filled(target)
        assert [p.name for p in target.iterdir()] == ["episodes.jsonl"]

    def test_env_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPISODE_MEMORY_DIR", str(tmp_path))
        filled()
        assert (tmp_path / "episodes.jsonl").is_file()

    def test_default_is_in_memory_only(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EPISODE_MEMORY_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        filled()
        assert list(tmp_path.iterdir()) == []

    def test_corrupt_file_reports_line(self, tmp_path):
        filled(tmp_path)
        f = tmp_path / "episodes.jsonl"
        f.write_text(f.read_text() + "not json\n")
        with pytest.raises(EpisodeMemoryError, match="line 4"):
            memory(tmp_path)

    def test_blank_lines_are_ignored_and_unreadable_path_is_an_error(self, tmp_path):
        filled(tmp_path)
        f = tmp_path / "episodes.jsonl"
        f.write_text("\n" + f.read_text() + "\n\n")
        assert len(memory(tmp_path)) == 3
        f.unlink()
        f.mkdir()
        with pytest.raises(EpisodeMemoryError, match="cannot read"):
            memory(tmp_path)

    def test_never_writes_into_the_source_tree(self, tmp_path):
        from pathlib import Path

        import incident_agent.memory.episodic as mod

        before = {p for p in Path(mod.__file__).parent.rglob("*") if p.suffix in (".json", ".jsonl")}
        filled(tmp_path)
        after = {p for p in Path(mod.__file__).parent.rglob("*") if p.suffix in (".json", ".jsonl")}
        assert before == after


class TestVoteArithmetic:
    """The vote is exercised with hand-made recollections so scores and ties are exact."""

    def vote(self, monkeypatch, scores):
        m = memory()
        recs = [
            Recollection(
                Episode(incident_id=f"INC-{i}", created_at="t", observation="obs obs", resolved_class=c), s, i, 0.9
            )
            for i, (c, s) in enumerate(scores, start=1)
        ]
        monkeypatch.setattr(m, "recall", lambda query, k=3: recs)
        return m.predict("q")

    def test_share_is_the_winning_fraction_of_the_score_mass(self, monkeypatch):
        p = self.vote(monkeypatch, [("a", 0.03), ("b", 0.01), ("a", 0.0)])
        assert p.label == "a" and p.share == pytest.approx(0.75) and p.similarity == 0.9

    def test_exact_tie_goes_to_the_alphabetically_first_class(self, monkeypatch):
        assert self.vote(monkeypatch, [("zeta", 0.02), ("alpha", 0.02)]).label == "alpha"

    def test_zero_score_mass_gives_zero_share(self, monkeypatch):
        p = self.vote(monkeypatch, [("a", 0.0), ("b", 0.0)])
        assert p.label == "a" and p.share == 0.0

    def test_lessons_follow_k(self):
        m = filled()
        q = "container killed out of memory heap growth worker pool saturated cpu throttling queue growing"
        assert len(m.lessons_for(q, 2)) == 2 and len(m.lessons_for(q, 1)) == 1
