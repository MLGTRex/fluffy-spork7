import logging
import numpy as np

logger = logging.getLogger(__name__)

# Score field names in per-company JSONs
SCORE_FIELDS = ["financial", "professional", "news_sentiment"]


def _get_score(company_data: dict, score_name: str):
    """Pull a score from per-company data (e.g., 'financial' -> 'financial_score' field)."""
    if not company_data:
        return None
    val = company_data.get(f"{score_name}_score")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_all_scores(company_data: dict) -> dict:
    """Return {score_name: value_or_None} for all SCORE_FIELDS."""
    return {name: _get_score(company_data, name) for name in SCORE_FIELDS}


def _count_populated(scores: dict) -> int:
    return sum(1 for v in scores.values() if v is not None)


# ============ REGRESSION IMPUTATION ============

def _fit_one_regression(targets: list, predictors_matrix: list) -> dict:
    """
    Fit a single OLS regression: target = beta_0 + beta_1 * x1 + beta_2 * x2.

    Inputs:
        targets: list of y values (the score being predicted)
        predictors_matrix: list of [x1, x2] pairs

    Returns dict with: intercept, coefs (list of floats), r_squared, n.
    """
    if len(targets) != len(predictors_matrix) or len(targets) < 3:
        return None

    y = np.array(targets, dtype=float)
    X = np.array(predictors_matrix, dtype=float)

    # Add intercept column
    X_with_intercept = np.column_stack([np.ones(len(X)), X])

    try:
        # np.linalg.lstsq returns: solution, residuals, rank, singular_values
        coefs_full, _, _, _ = np.linalg.lstsq(X_with_intercept, y, rcond=None)
    except np.linalg.LinAlgError:
        return None

    intercept = float(coefs_full[0])
    coefs = [float(c) for c in coefs_full[1:]]

    # Compute R^2
    y_pred = X_with_intercept @ coefs_full
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return {
        "intercept": intercept,
        "coefs": coefs,
        "r_squared": r_squared,
        "n": len(targets),
    }


def fit_imputation_models(all_company_data: dict, min_training_size: int = 50) -> dict:
    """
    Fit three regressions, one for each possible "missing one" case.

    Args:
        all_company_data: dict of {ticker: company_data}
        min_training_size: minimum number of fully-scored companies required to fit

    Returns dict like:
        {
            "predict_news_sentiment": {"predictors": ["financial", "professional"], "model": {...}},
            "predict_professional": {"predictors": ["financial", "news_sentiment"], "model": {...}},
            "predict_financial": {"predictors": ["professional", "news_sentiment"], "model": {...}}
        }

    Returns None values for any regression that couldn't be fit (insufficient data).
    Raises RuntimeError if training set is too small overall.
    """
    # Build training set: companies with all 3 scores
    training_rows = []
    for ticker, data in all_company_data.items():
        if not data:
            continue
        scores = _get_all_scores(data)
        if _count_populated(scores) == 3:
            training_rows.append(scores)

    n_training = len(training_rows)
    logger.info(f"Imputation training set size: {n_training} companies (with all 3 scores)")

    if n_training < min_training_size:
        raise RuntimeError(
            f"Training set too small for regression imputation: {n_training} < {min_training_size}. "
            f"Cannot proceed with imputation."
        )

    # For each "missing one" case, fit a regression
    models = {}
    for target in SCORE_FIELDS:
        predictors = [s for s in SCORE_FIELDS if s != target]

        targets_y = [row[target] for row in training_rows]
        predictors_X = [[row[p] for p in predictors] for row in training_rows]

        model = _fit_one_regression(targets_y, predictors_X)

        models[f"predict_{target}"] = {
            "predictors": predictors,
            "model": model,
        }

        if model:
            logger.info(
                f"Fitted regression to predict {target}: "
                f"intercept={model['intercept']:.2f}, "
                f"coefs={[f'{c:.3f}' for c in model['coefs']]}, "
                f"R^2={model['r_squared']:.3f}, n={model['n']}"
            )
        else:
            logger.warning(f"Could not fit regression to predict {target}")

    return models


def _predict_with_model(model_info: dict, predictor_values: list) -> float:
    """Apply a fitted regression to predict a value. Returns the raw prediction (uncapped)."""
    model = model_info["model"]
    if model is None:
        return None
    intercept = model["intercept"]
    coefs = model["coefs"]
    if len(coefs) != len(predictor_values):
        return None
    pred = intercept + sum(c * v for c, v in zip(coefs, predictor_values))
    return pred


def impute_missing_score(scores: dict, models: dict) -> dict:
    """
    For a company missing exactly one score, impute it using the appropriate regression.

    Args:
        scores: {"financial": float_or_None, "professional": ..., "news_sentiment": ...}
        models: output of fit_imputation_models()

    Returns:
        {
            "imputed_scores": {full set of 3 scores, with missing one filled in},
            "imputed_score_name": "news_sentiment" (or whichever),
            "imputed_value": 67.2,
            "imputation_r_squared": 0.42,
        }
        OR None if imputation isn't applicable (not exactly 1 missing, or no model available).
    """
    populated_count = _count_populated(scores)
    if populated_count != 2:
        return None

    # Find the missing score
    missing = [name for name, val in scores.items() if val is None]
    if len(missing) != 1:
        return None
    target = missing[0]

    model_key = f"predict_{target}"
    if model_key not in models:
        return None
    model_info = models[model_key]
    model = model_info.get("model")
    if model is None:
        return None

    # Pull predictor values in the order expected by the model
    predictors = model_info["predictors"]
    predictor_values = [scores[p] for p in predictors]
    if any(v is None for v in predictor_values):
        return None

    raw_prediction = _predict_with_model(model_info, predictor_values)
    if raw_prediction is None:
        return None

    # Cap to 0-100
    capped = max(0.0, min(100.0, raw_prediction))

    imputed_scores = dict(scores)
    imputed_scores[target] = capped

    return {
        "imputed_scores": imputed_scores,
        "imputed_score_name": target,
        "imputed_value": capped,
        "imputation_r_squared": model["r_squared"],
    }


# ============ COMPOSITE SCORING ============

def compute_composite_score(scores: dict, weights: dict) -> float:
    """
    Compute weighted composite from a dict of {score_name: value} and {score_name: weight}.
    All scores must be populated (None values skipped — should be imputed before calling this).
    Returns None if there's not enough data.
    """
    populated_pairs = [
        (scores[name], weights[name])
        for name in SCORE_FIELDS
        if scores.get(name) is not None and weights.get(name) is not None
    ]
    if not populated_pairs:
        return None
    total_weight = sum(w for _, w in populated_pairs)
    if total_weight == 0:
        return None
    return sum(v * w for v, w in populated_pairs) / total_weight


def score_company(company_data: dict, models: dict, weights: dict) -> dict:
    """
    Full pipeline for one company: get scores, impute if needed, compute composite, return result.

    Returns a dict with composite_score, scores_used, scores_imputed, imputation_details.
    Returns None composite if company can't be scored (2+ missing scores or no model).
    """
    scores = _get_all_scores(company_data)
    populated_count = _count_populated(scores)

    if populated_count == 3:
        composite = compute_composite_score(scores, weights)
        return {
            "composite_score": composite,
            "scores_used": list(SCORE_FIELDS),
            "scores_imputed": [],
            "imputed_values": {},
            "imputation_r_squared": None,
        }

    if populated_count == 2:
        imputation_result = impute_missing_score(scores, models)
        if imputation_result is None:
            return {
                "composite_score": None,
                "scores_used": [name for name, v in scores.items() if v is not None],
                "scores_imputed": [],
                "imputed_values": {},
                "imputation_r_squared": None,
                "skip_reason": "Imputation model not available for missing score",
            }
        composite = compute_composite_score(imputation_result["imputed_scores"], weights)
        imputed_name = imputation_result["imputed_score_name"]
        return {
            "composite_score": composite,
            "scores_used": [name for name, v in scores.items() if v is not None],
            "scores_imputed": [imputed_name],
            "imputed_values": {imputed_name: imputation_result["imputed_value"]},
            "imputation_r_squared": imputation_result["imputation_r_squared"],
        }

    # 0 or 1 populated scores: skip
    return {
        "composite_score": None,
        "scores_used": [name for name, v in scores.items() if v is not None],
        "scores_imputed": [],
        "imputed_values": {},
        "imputation_r_squared": None,
        "skip_reason": f"Only {populated_count} of 3 scores populated; need at least 2",
    }