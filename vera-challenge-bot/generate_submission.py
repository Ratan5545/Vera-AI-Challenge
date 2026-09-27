#!/usr/bin/env python3
"""
Generate submission.jsonl (30 lines, one per canonical test pair) by calling
composer.compose() directly against the expanded dataset — no HTTP involved,
so this is a pure sanity-check that the composer itself is solid before it's
wrapped in the HTTP server for judging.

Usage:
    python3 dataset/generate_dataset.py --seed-dir dataset --out expanded
    python3 generate_submission.py --expanded-dir expanded --out submission.jsonl
"""
import argparse
import json
from pathlib import Path

from composer import compose


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expanded-dir", default="expanded")
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    base = Path(args.expanded_dir)
    categories = {load_json(f)["slug"]: load_json(f) for f in (base / "categories").glob("*.json")}
    merchants = {load_json(f)["merchant_id"]: load_json(f) for f in (base / "merchants").glob("*.json")}
    customers = {load_json(f)["customer_id"]: load_json(f) for f in (base / "customers").glob("*.json")}
    triggers = {load_json(f)["id"]: load_json(f) for f in (base / "triggers").glob("*.json")}
    pairs = load_json(base / "test_pairs.json")["pairs"]

    lines = []
    for pair in pairs:
        trigger = triggers.get(pair["trigger_id"])
        merchant = merchants.get(pair["merchant_id"])
        customer_id = pair.get("customer_id")
        customer = customers.get(customer_id) if customer_id else None
        if not (trigger and merchant):
            print(f"SKIP {pair['test_id']}: missing trigger or merchant")
            continue
        category = categories.get(merchant["category_slug"])
        if not category:
            print(f"SKIP {pair['test_id']}: missing category")
            continue

        composed = compose(category, merchant, trigger, customer)
        lines.append({
            "test_id": pair["test_id"],
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    with open(args.out, "w") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(lines)} lines to {args.out}")


if __name__ == "__main__":
    main()
