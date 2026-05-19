import logging

logger = logging.getLogger(__name__)

# ============ CONFIG ============

# Sub-component weights (out of 100 total)
SUBCOMPONENT_WEIGHTS = {
    "earnings_surprise_revisions": 40,
    "price_momentum": 40,
    "insider_activity": 20,
}

# Minimum populated metrics required to compute a sub-score
MIN_METRICS_PER_SUBCOMPONENT = 1

# Drivers config
TOP_N_DRIVERS = 3


# ============ HELPERS ============

def _piecewise_linear_score(value, breakpoints: list) -> float:
    """Map a value to 0-100 via piecewise-linear interpolation."""
    if value is None:
        return None
    if value <= breakpoints[0][0]:
        return float(breakpoints[0][1])
    if value >= breakpoints[-1][0]:
        return float(breakpoints[-1][1])
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= value <= x1:
            if x1 == x0:
                return float(y0)
            return float(y0 + (value - x0) * (y1 - y0) / (x1 - x0))
    return None


def _weighted_average(weighted_pairs: list, min_count: int = MIN_METRICS_PER_SUBCOMPONENT) -> float:
    populated = [(v, w) for v, w in weighted_pairs if v is not None]
    if len(populated) < min_count:
        return None
    total_w = sum(w for _, w in populated)
    if total_w == 0:
        return None
    return sum(v * w for v, w in populated) / total_w


# ============ PEER STATISTICS (NO-OP) ============

def compute_peer_stats(all_raw_metrics: dict) -> dict:
    """
    News sentiment uses absolute scoring only — no sector-relative metrics.
    Returns an empty structure for architectural consistency with other modules.
    """
    return {"by_industry": {}, "by_sector": {}}


# ============ SUB-COMPONENT SCORING ============

def score_earnings_surprise_revisions(raw: dict, peer_stats: dict = None) -> dict:
    """
    Earnings surprise & revisions sub-score (absolute).
    Components:
        - Beat rate (last 4 quarters)
        - Average surprise magnitude
        - 90-day EPS estimate revision trend
    """
    block = raw.get("earnings", {}) or {}

    metrics_scored = {}

    # Beat rate
    beats = block.get("beat_count_4q")
    total = block.get("total_quarters_4q")
    if beats is not None and total is not None and total > 0:
        beat_rate = beats / total
        # 0/4 -> 0 pts, 2/4 -> 50 pts, 4/4 -> 100 pts
        score = beat_rate * 100
        metrics_scored["beat_rate_4q"] = {"value": beat_rate, "score": score}

    # Average surprise magnitude
    surprise = block.get("avg_surprise_magnitude_4q")
    if surprise is not None:
        # -10% -> 0, 0% -> 50, +5% -> 75, +10% -> 90, +20% -> 100
        score = _piecewise_linear_score(
            surprise,
            [(-0.20, 0), (-0.10, 10), (-0.05, 30), (0, 50), (0.05, 75), (0.10, 90), (0.20, 100)]
        )
        metrics_scored["avg_surprise_magnitude_4q"] = {"value": surprise, "score": score}

    # EPS revision trend (90-day)
    revision = block.get("eps_estimate_revision_90d")
    if revision is not None:
        # -10% revision -> 0, 0 -> 50, +10% -> 100
        score = _piecewise_linear_score(
            revision,
            [(-0.20, 0), (-0.10, 15), (-0.05, 35), (0, 50), (0.05, 70), (0.10, 90), (0.20, 100)]
        )
        metrics_scored["eps_estimate_revision_90d"] = {"value": revision, "score": score}

    # Combine with weights
    weights = {
        "beat_rate_4q": 0.35,
        "avg_surprise_magnitude_4q": 0.30,
        "eps_estimate_revision_90d": 0.35,
    }
    weighted_pairs = [(metrics_scored[m]["score"], weights[m]) for m in metrics_scored]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


def score_price_momentum(raw: dict, peer_stats: dict = None) -> dict:
    """
    Price momentum sub-score (absolute).
    Components: 1m, 3m, 6m returns.
    Longer horizons weighted more heavily (filter out short-term noise).
    """
    block = raw.get("price_momentum", {}) or {}

    metrics_scored = {}

    # Each horizon: -25% -> 0, 0% -> 50, +25% -> 100
    breakpoints = [(-0.30, 0), (-0.15, 25), (0, 50), (0.15, 75), (0.30, 100)]

    r1m = block.get("return_1m")
    if r1m is not None:
        metrics_scored["return_1m"] = {
            "value": r1m,
            "score": _piecewise_linear_score(r1m, breakpoints),
        }

    r3m = block.get("return_3m")
    if r3m is not None:
        metrics_scored["return_3m"] = {
            "value": r3m,
            "score": _piecewise_linear_score(r3m, breakpoints),
        }

    r6m = block.get("return_6m")
    if r6m is not None:
        metrics_scored["return_6m"] = {
            "value": r6m,
            "score": _piecewise_linear_score(r6m, breakpoints),
        }

    # Weight: 25% / 35% / 40% (longer horizons more meaningful)
    weights = {
        "return_1m": 0.25,
        "return_3m": 0.35,
        "return_6m": 0.40,
    }
    weighted_pairs = [(metrics_scored[m]["score"], weights[m]) for m in metrics_scored]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


def score_insider_activity(raw: dict, peer_stats: dict = None) -> dict:
    """
    Insider activity sub-score (absolute).
    Net insider activity (purchases minus sales by dollar value) over the last 90 days.
    Normalized by market cap to make companies comparable.

    Returns 50 (neutral) if no insider activity in the window — absence of
    activity is not a positive or negative signal.
    """
    block = raw.get("insider_activity", {}) or {}

    metrics_scored = {}

    purchases = block.get("insider_purchases_count_90d")
    sales = block.get("insider_sales_count_90d")
    net_value = block.get("insider_net_value_90d_usd")
    market_cap = raw.get("market_cap_usd")

    # If no activity at all: return neutral
    if purchases is not None and sales is not None and (purchases + sales) == 0:
        return {
            "score": 50.0,
            "metrics": {
                "insider_activity_summary": {"value": "no activity in 90d", "score": 50.0},
            },
        }

    # Net value normalized by market cap
    if net_value is not None and market_cap is not None and market_cap > 0:
        net_pct_of_mcap = net_value / market_cap
        # -0.1% (insiders selling 0.1% of mcap) is a meaningful negative signal
        # +0.1% is a meaningful positive signal
        # Most insider activity is small relative to market cap, so the signal scale is tight
        score = _piecewise_linear_score(
            net_pct_of_mcap,
            [(-0.005, 0), (-0.001, 25), (0, 50), (0.001, 75), (0.005, 100)]
        )
        metrics_scored["net_insider_value_pct_mcap_90d"] = {
            "value": net_pct_of_mcap,
            "score": score,
        }

    # Purchase count vs sales count as secondary signal
    if purchases is not None and sales is not None and (purchases + sales) > 0:
        net_count_ratio = (purchases - sales) / (purchases + sales)
        # -1 -> 0, 0 -> 50, +1 -> 100
        score = _piecewise_linear_score(
            net_count_ratio,
            [(-1.0, 0), (-0.5, 25), (0, 50), (0.5, 75), (1.0, 100)]
        )
        metrics_scored["insider_count_ratio_90d"] = {
            "value": net_count_ratio,
            "score": score,
        }

    # Weight: net dollar value matters more than count
    weights = {
        "net_insider_value_pct_mcap_90d": 0.70,
        "insider_count_ratio_90d": 0.30,
    }
    weighted_pairs = [(metrics_scored[m]["score"], weights[m]) for m in metrics_scored]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


# ============ DRIVERS EXTRACTION ============

def _flatten_metrics_for_drivers(subscore_results: dict) -> list:
    out = []
    for subcomp_name, result in subscore_results.items():
        metrics = result.get("metrics", {})
        for metric_name, info in metrics.items():
            score = info.get("score")
            value = info.get("value")
            if score is None:
                continue
            label = f"{metric_name} ({subcomp_name})"
            out.append((label, score, value, subcomp_name))
    return out


def extract_drivers(subscore_results: dict, top_n: int = TOP_N_DRIVERS) -> dict:
    flat = _flatten_metrics_for_drivers(subscore_results)
    if not flat:
        return {"strongest": [], "weakest": []}

    sorted_by_score = sorted(flat, key=lambda x: x[1])
    weakest = sorted_by_score[:top_n]
    strongest = sorted_by_score[-top_n:][::-1]

    def _format(item):
        label, score, value, _ = item
        if value is None:
            return f"{label}: score={score:.0f}"
        if isinstance(value, (int, float)):
            if abs(value) < 1 and value != 0:
                value_str = f"{value*100:.1f}%"
            else:
                value_str = f"{value:.2f}"
        else:
            value_str = str(value)
        return f"{label}: score={score:.0f}, value={value_str}"

    return {
        "strongest": [_format(item) for item in strongest],
        "weakest": [_format(item) for item in weakest],
    }


# ============ TOP-LEVEL WRAPPER ============

def score_company(raw: dict, peer_stats: dict = None) -> dict:
    """
    Top-level: score one company across all 3 sub-components and combine into a final
    weighted score with drivers.
    """
    if not raw:
        return {"score": None, "subscores": {}, "drivers": {"strongest": [], "weakest": []}, "subcomponent_details": {}}

    subscore_results = {
        "earnings_surprise_revisions": score_earnings_surprise_revisions(raw, peer_stats),
        "price_momentum": score_price_momentum(raw, peer_stats),
        "insider_activity": score_insider_activity(raw, peer_stats),
    }

    weighted_pairs = []
    for subcomp_name, weight in SUBCOMPONENT_WEIGHTS.items():
        sub_score = subscore_results[subcomp_name].get("score")
        if sub_score is not None:
            weighted_pairs.append((sub_score, weight))

    # Need at least 2 of 3 sub-components to produce a final score
    final_score = _weighted_average(weighted_pairs, min_count=2)

    drivers = extract_drivers(subscore_results)

    subscores = {
        name: result.get("score")
        for name, result in subscore_results.items()
    }

    return {
        "score": final_score,
        "subscores": subscores,
        "drivers": drivers,
        "subcomponent_details": subscore_results,
    }