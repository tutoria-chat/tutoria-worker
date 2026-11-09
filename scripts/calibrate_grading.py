"""
Calibration for score mode (corporate training only): run answers that people
already graded by hand through the same scoring the worker uses, and measure how
often the AI agrees. Use it in a pilot before turning score mode on for a client,
and again whenever the prompt or the model changes.

Input CSV (UTF-8, header row; extra columns ignored):
  question, answer, max_mark, human_mark       required
  rubric, reference_answer                     optional (rubric = the question's
                                               "Information for graders" in Moodle)
  question_id                                  optional, groups rows per question

Usage (needs the worker's env: DATABASE_URL for the model chain + provider keys):
  python -m scripts.calibrate_grading answers.csv --pass-share 0.7 --out report.csv
  python -m scripts.calibrate_grading answers.csv --criteria "Seja rigoroso com termos técnicos"

Prints a summary and writes one row per answer (AI mark, difference, flags).
Nothing is stored in Tutoria; the answers go only to the configured AI providers,
so use anonymised answers.
"""

import argparse
import asyncio
import csv
import json
import statistics
import sys
from decimal import Decimal
from typing import Optional


def _num(value) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def summarize(rows: list, pass_share: float, tolerance_share: float) -> dict:
    """Agreement numbers for rows that have both a human and an AI mark."""
    scored = [r for r in rows if r["ai_mark"] is not None and r["human_mark"] is not None]
    if not scored:
        return {"answers": len(rows), "scored": 0}

    diffs = [abs(r["ai_mark"] - r["human_mark"]) for r in scored]
    within = [abs(r["ai_mark"] - r["human_mark"]) <= tolerance_share * r["max_mark"] for r in scored]
    passfail = [
        (r["ai_mark"] >= pass_share * r["max_mark"]) == (r["human_mark"] >= pass_share * r["max_mark"])
        for r in scored
    ]
    clean = [r for r in scored if not r["review"]]
    clean_passfail = [
        (r["ai_mark"] >= pass_share * r["max_mark"]) == (r["human_mark"] >= pass_share * r["max_mark"])
        for r in clean
    ]
    ai_too_high = sum(1 for r in scored if r["ai_mark"] >= pass_share * r["max_mark"] > r["human_mark"])
    ai_too_low = sum(1 for r in scored if r["human_mark"] >= pass_share * r["max_mark"] > r["ai_mark"])

    return {
        "answers": len(rows),
        "scored": len(scored),
        "mean_abs_diff": round(statistics.mean(diffs), 3),
        "mean_abs_diff_share_of_max": round(statistics.mean(
            abs(r["ai_mark"] - r["human_mark"]) / r["max_mark"] for r in scored), 3),
        f"within_{int(tolerance_share * 100)}pct_of_max": round(sum(within) / len(scored), 3),
        "pass_fail_agreement": round(sum(passfail) / len(scored), 3),
        "ai_passed_human_failed": ai_too_high,
        "ai_failed_human_passed": ai_too_low,
        "flagged_for_review_share": round(1 - len(clean) / len(scored), 3),
        "pass_fail_agreement_unflagged": round(sum(clean_passfail) / len(clean), 3) if clean else None,
    }


async def run(path: str, pass_share: float, tolerance_share: float, criteria: Optional[str],
              out: Optional[str]) -> dict:
    from app.core.database import SessionLocal
    from app.services import grading_scoring as scoring
    from app.services.grading_service import _call_ai_with_model

    with open(path, encoding="utf-8-sig", newline="") as fh:
        source = list(csv.DictReader(fh))

    db = SessionLocal()
    rows = []
    models = set()
    try:
        for i, src in enumerate(source, start=1):
            max_mark = _num(src.get("max_mark"))
            human = _num(src.get("human_mark"))
            answer = (src.get("answer") or "").strip()
            if not max_mark or max_mark <= 0:
                print(f"row {i}: skipped (no max_mark)", file=sys.stderr)
                continue
            question = {
                "slot": 1,
                "question_text": (src.get("question") or "").strip(),
                "answer": answer,
                "max_mark": str(max_mark),
                "rubric": (src.get("rubric") or "").strip(),
                "gabarito": (src.get("reference_answer") or "").strip(),
            }
            if not answer:
                result = {"nota": "0", "confianca": "alta", "revisar": [scoring.REVIEW_EMPTY], "justificativa": ""}
            else:
                prompt = scoring.build_score_user_prompt([question], "", criteria, 0)
                text, model = await _call_ai_with_model(scoring.SCORE_SYSTEM_PROMPT, prompt, db)
                models.add(model)
                result = scoring.parse_score_response(text, [question])[0]
            ai = _num(result.get("nota")) if result.get("nota") != "" else None
            rows.append({
                "row": i,
                "question_id": src.get("question_id") or "",
                "max_mark": max_mark,
                "human_mark": human,
                "ai_mark": ai,
                "diff": None if ai is None or human is None else round(ai - human, 2),
                "confidence": result.get("confianca"),
                "review": ";".join(result.get("revisar") or []),
                "rationale": result.get("justificativa", ""),
            })
            print(f"row {i}: human={human} ai={ai} review={rows[-1]['review'] or '-'}", file=sys.stderr)
    finally:
        db.close()

    if out:
        with open(out, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["row"])
            writer.writeheader()
            writer.writerows(rows)

    summary = summarize(rows, pass_share, tolerance_share)
    summary["prompt_version"] = scoring.SCORE_PROMPT_VERSION
    summary["models"] = sorted(models)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="Calibrate score mode against human grades.")
    p.add_argument("csv", help="answers already graded by people")
    p.add_argument("--pass-share", type=float, default=0.7,
                   help="pass threshold as a share of each question's max mark (default 0.7)")
    p.add_argument("--tolerance-share", type=float, default=0.1,
                   help="'close enough' as a share of the max mark (default 0.1)")
    p.add_argument("--criteria", default=None, help="reviewer's criteria applied to every question")
    p.add_argument("--out", default=None, help="write per-answer results here")
    args = p.parse_args()
    summary = asyncio.run(run(args.csv, args.pass_share, args.tolerance_share, args.criteria, args.out))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
