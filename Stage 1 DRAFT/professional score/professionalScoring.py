import logging
import statistics

logger = logging.getLogger(__name__)

# ============ CONFIG ============

# Sub-component weights (out of 100 total)
SUBCOMPONENT_WEIGHTS = {
    "analyst_consensus": 35,
    "rating_momentum": 25,
    "institutional_positioning": 25,
    "short_interest": 15,
}

# Minimum populated metrics required to compute a sub-score
MIN_METRICS_PER_SUBCOMPONENT = 1

# Minimum peers required for sector-relative scoring
MIN_PEERS_INDUSTRY = 10
MIN_PEERS_SECTOR = 3

# Drivers config
TOP_N_DRIVERS = 3

# Confidence multiplier when analyst count is low
LOW_ANALYST_THRESHOLD = 5
LOW_ANALYST_MULTIPLIER = 0.7


# ============ HELPERS ============

def _z_to_score(z: float) -> float:
    """Map z-score to 0-100, same shape as financial scoring."""
    if z is None:
        return None
    z = max(-2.5, min(2.5, z))
    if z <= -2:
        return 5.0 + (z + 2.5) * (0 - 5) / 0.5
    if z <= -1:
        return 5.0 + (z + 2) * (25 - 5)
    if z <= 0:
        return 25.0 + (z + 1) * (50 - 25)
    if z <= 1:
        return 50.0 + z * (75 - 50)
    if z <= 2:
        return 75.0 + (z - 1) * (95 - 75)
    return 95.0 + (z - 2) * (100 - 95) / 0.5


def _piecewise_linear_score(value, breakpoints: list) -> float:
    """Map a value to 0-100 via piecewise-linear interpolation. Same as financial scoring."""
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


def _z_score(value, mean: float, stdev: float) -> float:
    if value is None or mean is None or stdev is None or stdev == 0:
        return None
    return (value - mean) / stdev


def _weighted_average(weighted_pairs: list, min_count: int = MIN_METRICS_PER_SUBCOMPONENT) -> float:
    populated = [(v, w) for v, w in weighted_pairs if v is not None]
    if len(populated) < min_count:
        return None
    total_w = sum(w for _, w in populated)
    if total_w == 0:
        return None
    return sum(v * w for v, w in populated) / total_w


# ============ PEER STATISTICS ============

def compute_peer_stats(all_raw_metrics: dict) -> dict:
    """
    Compute peer statistics for the metrics that need sector-relative scoring.
    For professional analysis, sector-relative metrics are:
        - held_pct_institutions
        - short_pct_of_float
    """
    SECTOR_RELATIVE_METRICS = [
        ("institutional", "held_pct_institutions"),
        ("short_interest", "short_pct_of_float"),
    ]

    by_industry = {}
    by_sector = {}

    for ticker, raw in all_raw_metrics.items():
        if not raw:
            continue
        industry = raw.get("industry")
        sector = raw.get("sector")
        for category, metric_name in SECTOR_RELATIVE_METRICS:
            cat_block = raw.get(category, {})
            if not cat_block:
                continue
            value = cat_block.get(metric_name)
            if value is None:
                continue
            if industry:
                by_industry.setdefault(industry, {}).setdefault(metric_name, []).append(value)
            if sector:
                by_sector.setdefault(sector, {}).setdefault(metric_name, []).append(value)

    def _compute_distribution(values_list: list) -> dict:
        if len(values_list) < 2:
            return {"mean": None, "stdev": None, "n": len(values_list)}
        try:
            return {
                "mean": statistics.mean(values_list),
                "stdev": statistics.stdev(values_list),
                "n": len(values_list),
            }
        except statistics.StatisticsError:
            return {"mean": None, "stdev": None, "n": len(values_list)}

    industry_stats = {
        ind: {m: _compute_distribution(vs) for m, vs in metrics.items()}
        for ind, metrics in by_industry.items()
    }
    sector_stats = {
        sec: {m: _compute_distribution(vs) for m, vs in metrics.items()}
        for sec, metrics in by_sector.items()
    }

    return {"by_industry": industry_stats, "by_sector": sector_stats}


def _get_peer_distribution(peer_stats: dict, industry: str, sector: str, metric_name: str) -> dict:
    if industry:
        ind_dist = peer_stats.get("by_industry", {}).get(industry, {}).get(metric_name)
        if ind_dist and ind_dist.get("n", 0) >= MIN_PEERS_INDUSTRY:
            return {**ind_dist, "source": "industry"}
    if sector:
        sec_dist = peer_stats.get("by_sector", {}).get(sector, {}).get(metric_name)
        if sec_dist and sec_dist.get("n", 0) >= MIN_PEERS_SECTOR:
            return {**sec_dist, "source": "sector"}
    return None


# ============ SUB-COMPONENT SCORING ============

def score_analyst_consensus(raw: dict, peer_stats: dict = None) -> dict:
    """
    Analyst consensus sub-score (absolute).
    Components: recommendation mean, upside to target.
    Dampened by low analyst count.
    """
    block = raw.get("analyst", {}) or {}

    metrics_scored = {}

    # Recommendation mean (1=strong buy, 5=strong sell — lower is better)
    rec = block.get("recommendation_mean")
    if rec is not None:
        # 1.0 -> 100, 1.5 -> 90, 2.0 -> 75, 2.5 -> 55, 3.0 -> 35, 4.0 -> 10, 5.0 -> 0
        score = _piecewise_linear_score(
            rec,
            [(1.0, 100), (1.5, 90), (2.0, 75), (2.5, 55), (3.0, 35), (4.0, 10), (5.0, 0)]
        )
        metrics_scored["recommendation_mean"] = {"value": rec, "score": score}

    # Upside to target
    upside = block.get("upside_to_target")
    if upside is not None:
        # -10% -> 0, 0% -> 25, 5% -> 40, 15% -> 65, 30% -> 90, 50% -> 100
        score = _piecewise_linear_score(
            upside,
            [(-0.10, 0), (0, 25), (0.05, 40), (0.15, 65), (0.30, 90), (0.50, 100)]
        )
        metrics_scored["upside_to_target"] = {"value": upside, "score": score}

    # Combine: 60% recommendation + 40% upside
    weights = {"recommendation_mean": 0.60, "upside_to_target": 0.40}
    weighted_pairs = [(metrics_scored[m]["score"], weights[m]) for m in metrics_scored]
    base_score = _weighted_average(weighted_pairs)

    # Dampen if analyst count is low (signal is unreliable)
    num_analysts = block.get("num_analysts")
    confidence_multiplier = 1.0
    if base_score is not None and num_analysts is not None and num_analysts < LOW_ANALYST_THRESHOLD:
        confidence_multiplier = LOW_ANALYST_MULTIPLIER
        base_score = base_score * confidence_multiplier

    return {
        "score": base_score,
        "metrics": metrics_scored,
        "num_analysts": num_analysts,
        "confidence_multiplier": confidence_multiplier,
    }


def score_rating_momentum(raw: dict, peer_stats: dict = None) -> dict:
    """
    Rating momentum sub-score (absolute).
    Net upgrades vs downgrades over the last 12 months, normalized by total actions.
    """
    block = raw.get("rating_momentum", {}) or {}

    upgrades = block.get("upgrades_12m")
    downgrades = block.get("downgrades_12m")
    total = block.get("total_actions_12m")

    if upgrades is None or downgrades is None or total is None or total == 0:
        # No rating activity in last 12m — neutral score
        return {
            "score": 50.0 if total == 0 else None,
            "metrics": {
                "upgrades_12m": {"value": upgrades, "score": None},
                "downgrades_12m": {"value": downgrades, "score": None},
                "total_actions_12m": {"value": total, "score": None},
            },
            "net_momentum": None,
        }

    # Net momentum: -1 (all downgrades) to +1 (all upgrades)
    net = (upgrades - downgrades) / total

    # Map -1 -> 0, 0 -> 50, +1 -> 100, with extra credit for high activity
    score = _piecewise_linear_score(
        net,
        [(-1.0, 0), (-0.5, 20), (0, 50), (0.5, 80), (1.0, 100)]
    )

    # Small boost for higher overall activity (more ratings = more reliable signal)
    if total >= 10:
        score = min(100, score * 1.05)

    return {
        "score": score,
        "metrics": {
            "upgrades_12m": {"value": upgrades, "score": score},
            "downgrades_12m": {"value": downgrades, "score": score},
            "total_actions_12m": {"value": total, "score": score},
        },
        "net_momentum": net,
    }


def score_institutional_positioning(raw: dict, peer_stats: dict) -> dict:
    """
    Institutional positioning sub-score (sector-relative for institution %, absolute for insider %).
    """
    block = raw.get("institutional", {}) or {}
    industry = raw.get("industry")
    sector = raw.get("sector")

    metrics_scored = {}

    # Institutional ownership — sector-relative (some sectors naturally have higher institutional ownership)
    inst_pct = block.get("held_pct_institutions")
    if inst_pct is not None:
        dist = _get_peer_distribution(peer_stats, industry, sector, "held_pct_institutions")
        if dist is None:
            # No usable peer distribution — use neutral fallback
            score = 50.0
            src = "neutral_fallback"
        else:
            z = _z_score(inst_pct, dist["mean"], dist["stdev"])
            if z is None:
                score = 50.0
                src = "neutral_fallback"
            else:
                # Higher institutional ownership = better, but cap so we don't reward saturation
                # Use raw z up to ~+1.5, then dampen
                if z > 1.5:
                    z = 1.5 + (z - 1.5) * 0.3
                score = _z_to_score(z)
                src = dist["source"]
        metrics_scored["held_pct_institutions"] = {"value": inst_pct, "score": score, "source": src}

    # Insider ownership — absolute, modest insider stake is positive signal
    insider_pct = block.get("held_pct_insiders")
    if insider_pct is not None:
        # 0% -> 50, 1% -> 60, 5% -> 80, 10% -> 90, 25% -> 100
        # Very high insider ownership (>50%) can reduce float and isn't always positive,
        # but for scoring purposes we cap at 100.
        score = _piecewise_linear_score(
            insider_pct,
            [(0, 50), (0.01, 60), (0.05, 80), (0.10, 90), (0.25, 100), (0.75, 100)]
        )
        metrics_scored["held_pct_insiders"] = {"value": insider_pct, "score": score}

    # Combine: 70% institutional, 30% insider
    weights = {"held_pct_institutions": 0.70, "held_pct_insiders": 0.30}
    weighted_pairs = [(metrics_scored[m]["score"], weights[m]) for m in metrics_scored]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


def score_short_interest(raw: dict, peer_stats: dict) -> dict:
    """
    Short interest sub-score (sector-relative for short %, absolute for ratio and change).
    Lower short interest = better score.
    """
    block = raw.get("short_interest", {}) or {}
    industry = raw.get("industry")
    sector = raw.get("sector")

    metrics_scored = {}

    # Short % of float — sector-relative (some sectors are perpetually more shorted)
    short_pct = block.get("short_pct_of_float")
    if short_pct is not None:
        dist = _get_peer_distribution(peer_stats, industry, sector, "short_pct_of_float")
        if dist is None:
            # Absolute fallback when no peer data
            score = _piecewise_linear_score(
                short_pct,
                [(0, 100), (0.02, 90), (0.05, 70), (0.10, 40), (0.20, 10), (0.30, 0)]
            )
            src = "absolute_fallback"
        else:
            z = _z_score(short_pct, dist["mean"], dist["stdev"])
            if z is None:
                score = 50.0
                src = "neutral_fallback"
            else:
                # Invert: lower short % = better
                score = _z_to_score(-z)
                src = dist["source"]
        metrics_scored["short_pct_of_float"] = {"value": short_pct, "score": score, "source": src}

    # Short ratio (days to cover) — absolute, lower better
    short_ratio = block.get("short_ratio")
    if short_ratio is not None:
        score = _piecewise_linear_score(
            short_ratio,
            [(0, 100), (1, 90), (3, 70), (5, 50), (10, 20), (20, 0)]
        )
        metrics_scored["short_ratio"] = {"value": short_ratio, "score": score}

    # Short change month-over-month — negative change (declining short interest) = better
    short_change = block.get("short_change_pct")
    if short_change is not None:
        # Increasing short interest = worse
        # -50% (shorts covering) -> 100, 0% -> 50, +50% (shorts piling on) -> 0
        score = _piecewise_linear_score(
            short_change,
            [(-0.50, 100), (-0.20, 80), (0, 50), (0.20, 25), (0.50, 0)]
        )
        metrics_scored["short_change_pct"] = {"value": short_change, "score": score}

    # Combine
    weights = {
        "short_pct_of_float": 0.50,
        "short_ratio": 0.30,
        "short_change_pct": 0.20,
    }
    weighted_pairs = [(metrics_scored[m]["score"], weights[m]) for m in metrics_scored]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


# ============ DRIVERS EXTRACTION ============

def _flatten_metrics_for_drivers(subscore_results: dict) -> list:
    """Flatten all sub-component metrics into a single list for driver extraction."""
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
    """Top N strongest and weakest individual metrics across all sub-components."""
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

def score_company(raw: dict, peer_stats: dict) -> dict:
    """
    Top-level: score one company across all 4 sub-components and combine into a final
    weighted score with drivers.
    """
    if not raw:
        return {"score": None, "subscores": {}, "drivers": {"strongest": [], "weakest": []}, "subcomponent_details": {}}

    subscore_results = {
        "analyst_consensus": score_analyst_consensus(raw, peer_stats),
        "rating_momentum": score_rating_momentum(raw, peer_stats),
        "institutional_positioning": score_institutional_positioning(raw, peer_stats),
        "short_interest": score_short_interest(raw, peer_stats),
    }

    weighted_pairs = []
    for subcomp_name, weight in SUBCOMPONENT_WEIGHTS.items():
        sub_score = subscore_results[subcomp_name].get("score")
        if sub_score is not None:
            weighted_pairs.append((sub_score, weight))

    # Need at least 2 of 4 sub-components to produce a final score
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