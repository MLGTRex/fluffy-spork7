import json
import os
import logging
from datetime import datetime
from compositeRanking import (
    fit_imputation_models,
    score_company,
    SCORE_FIELDS,
)

# ============ CONFIG ============

# Composite weights (must sum to 100 for clean interpretation, but math will work either way)
COMPOSITE_WEIGHTS = {
    "financial": 50,
    "professional": 30,
    "news_sentiment": 20,
}

# Minimum number of fully-scored companies required to fit imputation regressions
MIN_TRAINING_SET_SIZE = 50

# Top N candidates to write out as a buffer for downstream disqualifier filtering
TOP_N_BUFFER = 75

# ============ LOGGING ============

log_filename = f"composite_ranking_log_{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============ JSON FIELD MANAGEMENT ============

COMPOSITE_FIELDS = {
    "composite_score": None,
    "composite_score_date": "",
    "composite_rank": None,
    "composite_metadata": None,
}


def ensure_composite_fields(company_data: dict) -> dict:
    """Add composite fields to JSON if they don't exist. Idempotent."""
    for key, default in COMPOSITE_FIELDS.items():
        if key not in company_data:
            company_data[key] = default
    return company_data


def needs_composite_recompute(company_data: dict) -> bool:
    """
    Decide whether to recompute composite for a company.
    Recompute if:
        - Composite score is missing, OR
        - Any input score date is newer than composite_score_date
    """
    composite = company_data.get("composite_score")
    composite_date = company_data.get("composite_score_date", "")
    if composite is None or not composite_date:
        return True
    for score_name in SCORE_FIELDS:
        score_date = company_data.get(f"{score_name}_score_date", "")
        if score_date and score_date > composite_date:
            return True
    return False


# ============ HELPERS ============

def load_all_companies(company_data_dir: str) -> dict:
    """Load all per-company JSONs from the directory. Returns {ticker: data}."""
    out = {}
    if not os.path.isdir(company_data_dir):
        logger.error(f"Company data directory not found: {company_data_dir}")
        return out

    for fname in sorted(os.listdir(company_data_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(company_data_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ticker = data.get("ticker") or fname.removesuffix(".json")
            out[ticker] = data
        except Exception as e:
            logger.warning(f"Could not read {fname}: {e}")
    return out


def write_company_back(company_data: dict, company_data_dir: str):
    """Write a per-company JSON back to disk."""
    ticker = company_data.get("ticker")
    if not ticker:
        return
    safe_ticker = ticker.replace("/", "-").replace(" ", "_")
    path = os.path.join(company_data_dir, f"{safe_ticker}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(company_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[{ticker}] could not write {path}: {e}")


# ============ MAIN ============

def main():
    today_str = datetime.now().date().isoformat()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    stage1_root = os.path.join(script_dir, "..")
    output_dir = os.path.join(stage1_root, "output")
    company_data_dir = os.path.join(output_dir, "company_data")

    print(f"Stage 1 composite ranking starting...")
    print(f"Loading per-company data from {company_data_dir}...")

    all_companies = load_all_companies(company_data_dir)
    if not all_companies:
        print("Error: no company data loaded.")
        return

    print(f"Loaded {len(all_companies)} companies.")

    # Fit imputation models
    print(f"Fitting imputation regression models...")
    try:
        models = fit_imputation_models(all_companies, min_training_size=MIN_TRAINING_SET_SIZE)
    except RuntimeError as e:
        print(f"Error: {e}")
        return

    # Score each company
    print(f"Computing composite scores...")
    scored = []
    skipped = 0
    skipped_reasons = {}
    recomputed = 0
    skipped_already_current = 0

    for ticker, company_data in all_companies.items():
        company_data = ensure_composite_fields(company_data)

        if not needs_composite_recompute(company_data):
            # Already current — keep existing score for ranking
            skipped_already_current += 1
            existing = company_data.get("composite_score")
            if existing is not None:
                scored.append((ticker, existing, company_data))
            continue

        result = score_company(company_data, models, COMPOSITE_WEIGHTS)
        composite = result["composite_score"]

        if composite is None:
            skipped += 1
            reason = result.get("skip_reason", "unknown")
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            company_data["composite_score"] = None
            company_data["composite_score_date"] = today_str
            company_data["composite_rank"] = None
            company_data["composite_metadata"] = {
                "scores_used": result["scores_used"],
                "scores_imputed": result["scores_imputed"],
                "imputed_values": result["imputed_values"],
                "imputation_r_squared": result["imputation_r_squared"],
                "weights_applied": COMPOSITE_WEIGHTS,
                "skip_reason": reason,
            }
            write_company_back(company_data, company_data_dir)
            continue

        # Successfully scored
        company_data["composite_score"] = composite
        company_data["composite_score_date"] = today_str
        # Rank assigned later after sorting
        company_data["composite_metadata"] = {
            "scores_used": result["scores_used"],
            "scores_imputed": result["scores_imputed"],
            "imputed_values": result["imputed_values"],
            "imputation_r_squared": result["imputation_r_squared"],
            "weights_applied": COMPOSITE_WEIGHTS,
        }
        scored.append((ticker, composite, company_data))
        recomputed += 1

    print(f"Composite scoring: {recomputed} recomputed, {skipped_already_current} already current, {skipped} skipped (no composite).")
    if skipped_reasons:
        print(f"Skip reasons:")
        for reason, count in sorted(skipped_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")

    if not scored:
        print("Error: no companies scored.")
        return

    # Sort descending by composite score, assign ranks
    scored.sort(key=lambda x: -x[1])

    # Update each company's rank and write back
    for rank, (ticker, composite, company_data) in enumerate(scored, start=1):
        company_data["composite_rank"] = rank
        write_company_back(company_data, company_data_dir)

    # Build the full ranking JSON
    full_ranking = []
    for rank, (ticker, composite, company_data) in enumerate(scored, start=1):
        full_ranking.append({
            "rank": rank,
            "ticker": ticker,
            "company_name": company_data.get("company_name", ""),
            "composite_score": round(composite, 2),
            "subscores": {
                "financial": company_data.get("financial_score"),
                "professional": company_data.get("professional_score"),
                "news_sentiment": company_data.get("news_sentiment_score"),
            },
            "imputed_scores": company_data.get("composite_metadata", {}).get("scores_imputed", []),
        })

    # Write full ranking
    ranking_path = os.path.join(output_dir, "composite_ranking.json")
    with open(ranking_path, "w", encoding="utf-8") as f:
        json.dump({
            "ranking_date": today_str,
            "weights_applied": COMPOSITE_WEIGHTS,
            "total_ranked": len(full_ranking),
            "ranking": full_ranking,
        }, f, indent=4, ensure_ascii=False)
    print(f"Wrote full ranking to {ranking_path} ({len(full_ranking)} companies).")

    # Write top-N buffer for disqualifier processing
    top_n = full_ranking[:TOP_N_BUFFER]
    top_n_path = os.path.join(output_dir, f"top_{TOP_N_BUFFER}_candidates.json")
    with open(top_n_path, "w", encoding="utf-8") as f:
        json.dump({
            "ranking_date": today_str,
            "buffer_size": TOP_N_BUFFER,
            "candidates": top_n,
        }, f, indent=4, ensure_ascii=False)
    print(f"Wrote top {TOP_N_BUFFER} candidates to {top_n_path}.")

    # Summary
    n_imputed = sum(1 for r in full_ranking if r["imputed_scores"])
    print(f"\nSummary:")
    print(f"  Total ranked: {len(full_ranking)}")
    print(f"  Imputed (1 score missing, regression-filled): {n_imputed}")
    print(f"  Skipped (insufficient data): {skipped}")
    print(f"  Top score: {full_ranking[0]['composite_score']:.2f} ({full_ranking[0]['ticker']})")
    print(f"  Median score: {full_ranking[len(full_ranking)//2]['composite_score']:.2f}")
    print(f"  Bottom score: {full_ranking[-1]['composite_score']:.2f}")
    print(f"\nComposite ranking complete.")


if __name__ == "__main__":
    main()