"""Regenerates dataset.jsonl for the custom-adapter example."""

import json
from pathlib import Path

TERMS = {
    "sla": "Service Level Agreement",
    "rca": "Root Cause Analysis",
    "mttr": "Mean Time To Recovery",
    "slo": "Service Level Objective",
    "poc": "Proof Of Concept",
    "okr": "Objectives And Key Results",
}
QUESTIONS = [
    "What does our team mean by {t}?",
    "Expand the abbreviation {t}",
    "In our docs, what is {t}?",
    "Spell out {t}",
]

with (Path(__file__).parent / "dataset.jsonl").open("w", encoding="utf-8") as f:
    for term, meaning in TERMS.items():
        for i, q in enumerate(QUESTIONS, 1):
            case = {
                "id": f"{term}-{i}",
                "input": q.format(t=term.upper()),
                "expected": meaning,
                "tags": [term],
                # consumed by the simulated analyst only; a real analyst reads the requirement and the traces
                "meta": {"hints": {term: meaning}},
            }
            f.write(json.dumps(case) + "\n")
print(len(TERMS) * len(QUESTIONS), "cases written")
