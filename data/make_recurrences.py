"""Generate the Stage 12 recurrence set: a second occurrence of each dev incident's failure on a *different service*.

Each R-0xx is a dev scenario re-generated with a new seed (new noise and timing) and with its service names swapped.
It exists because the main set has no same-class recurrence inside a split, so "better on the second encounter"
could not be measured. Output never touches the pinned v1/v2 files:

    data/recurrence/incidents/R-0xx/        agent-visible data
    eval/recurrence_gold.json               {R-0xx: {recurrence_of, primary, is_false_alarm}}

Run: python data/make_recurrences.py      (deterministic; safe to re-run)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import generate_dataset as gd  # noqa: E402

OUT_DIR = HERE / "recurrence" / "incidents"
GOLD_OUT = ROOT / "eval" / "recurrence_gold.json"
SWAP = {
    "checkout-api": "cart-api",
    "notifications-service": "alerts-service",
    "inventory-service": "stock-service",
    "payment-service": "billing-service",
    "orders-service": "fulfilment-service",
    "cart-service": "basket-service",
    "search-service": "discovery-service",
    "search-indexer": "catalog-indexer",
    "ledger-service": "accounts-service",
    "catalog-api": "listing-api",
    "reporting-service": "analytics-service",
    "orders-api": "purchase-api",
    "auth-proxy": "gateway-proxy",
    "email-worker": "mail-worker",
}


def renamed(spec: dict, new_id: str) -> dict:
    text = json.dumps(spec)
    for old in sorted(SWAP, key=len, reverse=True):  # longest first: "orders-service" must not eat "orders-api"
        text = text.replace(old, SWAP[old])
    out = json.loads(text)
    out["id"] = new_id
    out["slug"] = spec["slug"] + "-again"
    out["recurrence_of"] = spec["id"]
    return out


def main() -> int:
    splits = json.loads((ROOT / "eval" / "splits.json").read_text())
    retrieval_gold = json.loads((ROOT / "eval" / "retrieval_gold.json").read_text())["incidents"]
    by_id = {s["id"]: s for s in gd.SCENARIOS}
    gold: dict[str, dict] = {}
    for n, original in enumerate(splits["dev"], start=1):
        rid = f"R-{n:03d}"
        spec = renamed(by_id[original], rid)
        rng = gd.random.Random(f"{rid}-{spec['slug']}")
        meta, logs, metrics, deploys, _ = gd.build_scenario(rng, spec)
        d = OUT_DIR / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta, indent=2), newline="\n")
        (d / "logs.log").write_text("\n".join(logs) + "\n", newline="\n")
        (d / "metrics.json").write_text(json.dumps(metrics, indent=2), newline="\n")
        (d / "deploys.json").write_text(json.dumps(deploys, indent=2), newline="\n")
        gold[rid] = {
            "recurrence_of": original,
            "primary": retrieval_gold[original]["primary"],
            "is_false_alarm": bool(by_id[original].get("is_false_alarm", False)),
        }
    gold_data = {"_note": "Stage 12 recurrence set; see data/make_recurrences.py", "incidents": gold}
    GOLD_OUT.write_text(json.dumps(gold_data, indent=2) + "\n", newline="\n")
    print(f"wrote {len(gold)} recurrence incidents under {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
