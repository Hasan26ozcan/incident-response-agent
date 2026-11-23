# Checkpoints

After every step a patch zip is delivered and an entry is added here, so any step can be resumed or rewound.

**How to save in your repo after applying a patch** (PowerShell, project root):
```
git add -A
git commit -m "checkpoint NN: <title>"
git tag checkpoint-NN
```
**Rewind:** `git checkout checkpoint-NN` (look around) or `git reset --hard checkpoint-NN` (discard later work).

| # | Date | Content | Patch | Verified |
|---|---|---|---|---|
| 20 | 2026-11-20 | Stage 20 adversarial security | stage20-patch.zip | 2329 tests |
| 21 | 2026-11-21 | Stage 21 observability and cost | stage21-patch.zip | 71 new tests, gate PASS |
| 22 | 2026-11-22 | Stage 21b fault lab (real processes, Docker Compose, LAB ids) | stage21b-patch.zip | 28 lab tests |
| 23 | 2026-11-22 | Stage 21c end-to-end pipeline, shadow mode, INJ-09 closed in the pipeline | stage21c-patch.zip | 35 pipeline tests, security 0 vulnerable |
