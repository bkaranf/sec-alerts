"""Add the five approved, source-linked comparison charts to review artifacts."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CHARTS = {
    "TD": {
        "title": "Groupwide residential mortgage loans 31 to 89 days past due, not impaired",
        "unit": "CAD millions",
        "sources": ["1"],
        "points": [
            {"period": "October 31, 2025", "value": "536", "display": "CAD $536m"},
            {"period": "July 31, 2026", "value": "631", "display": "CAD $631m"},
        ],
    },
    "RY": {
        "title": "Canadian residential mortgage lending",
        "unit": "CAD billions",
        "reader_note": "Reported lending balances.",
        "sources": ["1"],
        "points": [
            {"period": "April 30, 2026", "value": "461.384", "display": "C$461.384bn"},
            {"period": "July 31, 2026", "value": "473.303", "display": "C$473.303bn"},
        ],
    },
    "CM": {
        "title": "Canadian residential mortgage 90-plus-day delinquency",
        "unit": "percent",
        "sources": ["5"],
        "points": [
            {"period": "Q2 2026", "value": "0.47", "display": "0.47%"},
            {"period": "Q3 2026", "value": "0.51", "display": "0.51%"},
        ],
    },
    "BNS": {
        "title": "Canadian residential mortgage lending",
        "unit": "CAD billions",
        "reader_note": "Reported lending balances.",
        "sources": ["1"],
        "points": [
            {"period": "Q2 2026", "value": "315.063", "display": "C$315.063bn"},
            {"period": "Q3 2026", "value": "311.209", "display": "C$311.209bn"},
        ],
    },
    "BMO": {
        "title": "Groupwide gross impaired residential mortgages",
        "unit": "CAD millions",
        "sources": ["2"],
        "points": [
            {"period": "October 31, 2025", "value": "903", "display": "CAD $903m"},
            {"period": "July 31, 2026", "value": "1135", "display": "CAD $1,135m"},
        ],
    },
}


def main() -> None:
    for ticker, chart in CHARTS.items():
        path = ROOT / ticker / "review.json"
        review = json.loads(path.read_text(encoding="utf-8"))
        if review.get("ticker") != ticker:
            raise ValueError(f"{path}: unexpected ticker {review.get('ticker')!r}")
        review["chart"] = chart
        path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"updated {path}")


if __name__ == "__main__":
    main()
