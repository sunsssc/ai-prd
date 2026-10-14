"""Recompute published counts; this does not rerun models or regrade evidence."""
import json
from pathlib import Path
from statistics import median


def check():
    data = json.loads(Path(__file__).with_name("results.json").read_text())
    cases = data["pr_cases"]
    assert len(cases) == len({row["case"] for row in cases}) == 20
    assert len({row["family"] for row in cases}) == 9
    assert sum(row["source_type"] == "real_historical_commit" for row in cases) == 10
    assert sum(row["source_type"] == "reverse_historical_fix_reconstruction" for row in cases) == 10
    known_total = sum(row["known_total"] for row in cases)
    for arm, expected in data["pr_summary"].items():
        findings = {kind: sum(row[arm]["findings"][kind] for row in cases)
                    for kind in ("valid", "invalid", "unresolved")}
        hits = sum(row[arm]["known_hits"] for row in cases)
        assert findings == expected["findings"], (arm, findings)
        assert hits == expected["known_hits"]
        assert known_total == expected["known_total"] == 13
        assert all(0 <= row[arm]["known_hits"] <= row["known_total"] for row in cases)
        print(f"{arm}: valid/all={findings['valid']}/{sum(findings.values())}; recall={hits}/{known_total}")
    revised = data["pr_summary"]["two_stage"]
    assert sum(row["two_stage"]["total_tokens"] for row in cases) == revised["total_tokens"]
    assert abs(median(row["two_stage"]["seconds"] for row in cases) - revised["median_seconds"]) < 1e-9
    for arm, result in data["qa"].items():
        assert result["denominator"] == 20
        assert result["rate"] == result["numerator"] / result["denominator"]
        print(f"QA {arm}: {result['numerator']}/{result['denominator']}")
    print("All published per-case PR totals match. QA counts are aggregate-only.")


if __name__ == "__main__":
    check()
