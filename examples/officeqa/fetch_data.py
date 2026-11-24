"""Download the OfficeQA sample used by this example into ./data (not tracked in this repository).

The sample - 10 questions and the 9 Treasury Bulletin text files they refer to - is published in the
EvoSkill repository (https://github.com/sentient-agi/EvoSkill, Apache-2.0) under examples/officeqa.
Files are fetched at a pinned commit, so the dataset cannot change underneath a comparison.
"""

import csv
import io
import sys
import urllib.request
from pathlib import Path

COMMIT = "36f6f04952293d7054145550c2b9f0b0411bff1c"
BASE = f"https://raw.githubusercontent.com/sentient-agi/EvoSkill/{COMMIT}/examples/officeqa/data"
DATA = Path(__file__).resolve().parent / "data"


def fetch(rel: str) -> bytes:
    with urllib.request.urlopen(f"{BASE}/{rel}", timeout=120) as resp:  # noqa: S310 - fixed https URL
        return resp.read()


def main() -> int:
    (DATA / "treasury_bulletins").mkdir(parents=True, exist_ok=True)
    sample = fetch("officeqa_sample.csv")
    (DATA / "officeqa_sample.csv").write_bytes(sample)
    rows = list(csv.DictReader(io.StringIO(sample.decode("utf-8-sig"))))
    files = sorted({name.strip() for row in rows for name in row["source_files"].split(",") if name.strip()})
    for name in files:
        target = DATA / "treasury_bulletins" / name
        if target.exists() and target.stat().st_size > 0:
            print(f"  have   {name}")
            continue
        target.write_bytes(fetch(f"treasury_bulletins/{name}"))
        print(f"  fetched {name} ({target.stat().st_size // 1024} KB)")
    print(f"{len(rows)} questions, {len(files)} documents in {DATA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
