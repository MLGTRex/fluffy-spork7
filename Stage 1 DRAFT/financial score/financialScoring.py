import logging
import statistics

logger = logging.getLogger(__name__)

# ============ CONFIG ============

# Sub-component weights (out of 100 total)
SUBCOMPONENT_WEIGHTS = {
    "profitability": 20,
    "returns_on_capital": 20,
    "cash_flow_quality": 15,
    "balance_sheet": 15,
    "growth": 15,
    "valuation": 15,
}

# Minimum populated metrics required to compute a sub-score (otherwise return None)
MIN_METRICS_PER_SUBCOMPONENT = 2

# Minimum peers required for sector-relative scoring
MIN_PEERS_INDUSTRY = 10
MIN_PEERS_SECTOR = 3

# Drivers config
TOP_N_DRIVERS = 3


# ============ HELPERS ============

def _z_to_score(z: float) -> float:
    """
    Map a z-score to a 0-100 sub-score using a piecewise-linear sigmoid-ish mapping.
        z = -2  -> 5 pts
        z = -1  -> 25 pts
        z =  0  -> 50 pts
        z = +1  -> 75 pts
        z = +2  -> 95 pts
    Caps at 0 and 100 outside [-2, +2].
    """
    if z is None:
        return None
    z = max(-2.5, min(2.5, z))  # cap extremes
    if z <= -2:
        return 5.0 + (z + 2.5) * (0 - 5) / 0.5  # tiny gradient below -2
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
    """
    Map a value to a 0-100 score using piecewise-linear interpolation.
    breakpoints: list of (threshold, score) tuples in ascending order of threshold.
    Below the lowest threshold, returns the lowest score; above the highest, returns the highest.

    Example: breakpoints=[(0, 0), (5, 30), (15, 70), (25, 100)]
        value = 10 -> linearly interpolate between (5, 30) and (15, 70) -> 50
    """
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
    """Compute z-score with safety against zero stdev."""
    if value is None or mean is None or stdev is None or stdev == 0:
        return None
    return (value - mean) / stdev


def _average_subscores(subscores: list, min_count: int = MIN_METRICS_PER_SUBCOMPONENT) -> float:
    """
    Average a list of sub-scores, skipping None values.
    Returns None if fewer than min_count are populated.
    """
    populated = [s for s in subscores if s is not None]
    if len(populated) < min_count:
        return None
    return sum(populated) / len(populated)


def _weighted_average(weighted_pairs: list, min_count: int = MIN_METRICS_PER_SUBCOMPONENT) -> float:
    """
    Weighted average of (value, weight) tuples, skipping None values.
    Returns None if fewer than min_count are populated.
    """
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
    Given a dict of {ticker: raw_metrics}, compute peer statistics by industry and sector
    for the metrics that need sector-relative scoring.

    Returns:
    {
        "by_industry": {
            "Software—Application": {
                "gross_margin_ttm": {"mean": 0.62, "stdev": 0.18, "n": 35},
                "operating_margin_ttm": { ... },
                ...
            },
            ...
        },
        "by_sector": { ... same structure ... }
    }
    """
    # Metrics that use sector-relative scoring
    SECTOR_RELATIVE_METRICS = [
        ("profitability", "gross_margin_ttm"),
        ("profitability", "operating_margin_ttm"),
        ("profitability", "net_margin_ttm"),
        ("returns_on_capital", "roe_ttm"),
        ("returns_on_capital", "roa_ttm"),
        ("returns_on_capital", "roic_ttm"),
        ("valuation", "ev_ebitda"),
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
            mean = statistics.mean(values_list)
            stdev = statistics.stdev(values_list)
            return {"mean": mean, "stdev": stdev, "n": len(values_list)}
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
    """
    Resolve the peer distribution to use for a metric, falling back through:
        1. Industry (if >=10 peers)
        2. Sector (if >=3 peers)
        3. None (caller treats as z=0)
    Returns dict with keys {mean, stdev, n, source} or None if no usable distribution.
    """
    if industry:
        ind_dist = peer_stats.get("by_industry", {}).get(industry, {}).get(metric_name)
        if ind_dist and ind_dist.get("n", 0) >= MIN_PEERS_INDUSTRY:
            return {**ind_dist, "source": "industry"}

    if sector:
        sec_dist = peer_stats.get("by_sector", {}).get(sector, {}).get(metric_name)
        if sec_dist and sec_dist.get("n", 0) >= MIN_PEERS_SECTOR:
            return {**sec_dist, "source": "sector"}

    return None


def _sector_relative_score(value, peer_stats: dict, industry: str, sector: str, metric_name: str):
    """
    Compute a sector-relative score for a metric, returning (score, source).
    Falls back to z=0 (50 pts) if no usable peer distribution exists.
    """
    if value is None:
        return None, None
    dist = _get_peer_distribution(peer_stats, industry, sector, metric_name)
    if dist is None:
        return 50.0, "neutral_fallback"
    z = _z_score(value, dist["mean"], dist["stdev"])
    if z is None:
        return 50.0, "neutral_fallback"
    return _z_to_score(z), dist["source"]


# ============ SUB-COMPONENT SCORING ============

def score_profitability(raw: dict, peer_stats: dict) -> dict:
    """
    Profitability sub-score (sector-relative for margins, +/- adjustment for trend).
    """
    block = raw.get("profitability", {}) or {}
    industry = raw.get("industry")
    sector = raw.get("sector")

    metrics_scored = {}
    for metric_name in ("gross_margin_ttm", "operating_margin_ttm", "net_margin_ttm"):
        value = block.get(metric_name)
        score, src = _sector_relative_score(value, peer_stats, industry, sector, metric_name)
        if score is not None:
            metrics_scored[metric_name] = {"value": value, "score": score, "source": src}

    base_score = _average_subscores([m["score"] for m in metrics_scored.values()])

    # Trend adjustment: +5 if improving by >=2 percentage points, -5 if deteriorating by same amount
    trend = block.get("operating_margin_trend_3yr")
    trend_adj = 0
    if trend is not None:
        if trend >= 0.02:
            trend_adj = 5
        elif trend <= -0.02:
            trend_adj = -5

    final_score = None
    if base_score is not None:
        final_score = max(0, min(100, base_score + trend_adj))

    return {
        "score": final_score,
        "metrics": metrics_scored,
        "trend_adjustment": trend_adj,
    }


def score_returns_on_capital(raw: dict, peer_stats: dict) -> dict:
    """
    Returns on capital sub-score (sector-relative).
    Weights: ROIC > ROE > ROA. Since ROIC is unavailable in yfinance, falls back to ROE/ROA.
    """
    block = raw.get("returns_on_capital", {}) or {}
    industry = raw.get("industry")
    sector = raw.get("sector")

    weights = {"roic_ttm": 0.5, "roe_ttm": 0.3, "roa_ttm": 0.2}
    metrics_scored = {}
    weighted_pairs = []
    for metric_name, weight in weights.items():
        value = block.get(metric_name)
        score, src = _sector_relative_score(value, peer_stats, industry, sector, metric_name)
        if score is not None:
            metrics_scored[metric_name] = {"value": value, "score": score, "source": src}
            weighted_pairs.append((score, weight))

    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


def score_cash_flow_quality(raw: dict, peer_stats: dict = None) -> dict:
    """
    Cash flow quality sub-score (absolute thresholds).
    Components: FCF margin, FCF/Net Income (cash conversion), OCF 3yr CAGR.
    """
    block = raw.get("cash_flow", {}) or {}

    metrics_scored = {}

    # FCF margin — higher is better
    fcf_margin = block.get("fcf_margin_ttm")
    if fcf_margin is not None:
        score = _piecewise_linear_score(
            fcf_margin,
            [(-0.10, 0), (0, 10), (0.05, 35), (0.15, 70), (0.25, 95), (0.40, 100)]
        )
        metrics_scored["fcf_margin_ttm"] = {"value": fcf_margin, "score": score}

    # FCF / Net Income — sweet spot around 1.0
    fcf_to_ni = block.get("fcf_to_net_income_ttm")
    if fcf_to_ni is not None:
        # Custom mapping: <0.5 = bad, 0.8-1.2 = best, >1.5 = caution
        if fcf_to_ni < 0:
            score = 0.0
        elif fcf_to_ni < 0.5:
            score = _piecewise_linear_score(fcf_to_ni, [(0, 10), (0.5, 40)])
        elif fcf_to_ni < 0.8:
            score = _piecewise_linear_score(fcf_to_ni, [(0.5, 40), (0.8, 75)])
        elif fcf_to_ni <= 1.2:
            score = _piecewise_linear_score(fcf_to_ni, [(0.8, 75), (1.0, 95), (1.2, 95)])
        elif fcf_to_ni <= 1.5:
            score = _piecewise_linear_score(fcf_to_ni, [(1.2, 95), (1.5, 80)])
        else:
            score = _piecewise_linear_score(fcf_to_ni, [(1.5, 80), (3.0, 60)])
        metrics_scored["fcf_to_net_income_ttm"] = {"value": fcf_to_ni, "score": score}

    # OCF 3yr CAGR — higher is better
    ocf_cagr = block.get("ocf_cagr_3yr")
    if ocf_cagr is not None:
        score = _piecewise_linear_score(
            ocf_cagr,
            [(-0.20, 0), (0, 30), (0.05, 50), (0.15, 80), (0.30, 100)]
        )
        metrics_scored["ocf_cagr_3yr"] = {"value": ocf_cagr, "score": score}

    # Weighted average: FCF margin 50%, cash conversion 35%, OCF growth 15%
    weights = {"fcf_margin_ttm": 0.50, "fcf_to_net_income_ttm": 0.35, "ocf_cagr_3yr": 0.15}
    weighted_pairs = [
        (metrics_scored[m]["score"], weights[m]) for m in metrics_scored
    ]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


def score_balance_sheet(raw: dict, peer_stats: dict = None) -> dict:
    """
    Balance sheet sub-score (absolute thresholds).
    Components: Net debt/EBITDA, debt/equity, current ratio, interest coverage.
    """
    block = raw.get("balance_sheet", {}) or {}

    metrics_scored = {}

    # Net debt / EBITDA — lower is better. Negative = net cash = great.
    nd_ebitda = block.get("net_debt_to_ebitda")
    if nd_ebitda is not None:
        if nd_ebitda < 0:
            score = 100.0
        else:
            score = _piecewise_linear_score(
                nd_ebitda,
                [(0, 100), (1.5, 80), (3.0, 50), (4.5, 20), (6.0, 0)]
            )
        metrics_scored["net_debt_to_ebitda"] = {"value": nd_ebitda, "score": score}

    # Debt / Equity — lower is generally better, but very low can suggest under-leverage
    de = block.get("debt_to_equity")
    if de is not None:
        if de < 0:
            # Negative D/E from negative book equity = red flag
            score = 0.0
        else:
            score = _piecewise_linear_score(
                de,
                [(0, 95), (0.5, 90), (1.0, 75), (2.0, 50), (3.0, 25), (5.0, 0)]
            )
        metrics_scored["debt_to_equity"] = {"value": de, "score": score}

    # Current ratio — higher is better up to a point
    cr = block.get("current_ratio")
    if cr is not None:
        score = _piecewise_linear_score(
            cr,
            [(0.5, 0), (1.0, 30), (1.5, 70), (2.5, 95), (4.0, 100)]
        )
        metrics_scored["current_ratio"] = {"value": cr, "score": score}

    # Interest coverage — higher is better
    ic = block.get("interest_coverage")
    if ic is not None:
        score = _piecewise_linear_score(
            ic,
            [(1.0, 0), (2.0, 30), (5.0, 70), (10.0, 95), (20.0, 100)]
        )
        metrics_scored["interest_coverage"] = {"value": ic, "score": score}

    # Weights
    weights = {
        "net_debt_to_ebitda": 0.40,
        "debt_to_equity": 0.25,
        "current_ratio": 0.20,
        "interest_coverage": 0.15,
    }
    weighted_pairs = [
        (metrics_scored[m]["score"], weights[m]) for m in metrics_scored
    ]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


def score_growth(raw: dict, peer_stats: dict = None) -> dict:
    """
    Growth sub-score (absolute thresholds).
    Components: revenue growth (1yr + 3yr CAGR), EPS growth (1yr + 3yr CAGR), FCF 3yr CAGR.
    """
    block = raw.get("growth", {}) or {}

    metrics_scored = {}

    # Revenue 3yr CAGR — primary growth signal
    rev_cagr = block.get("revenue_cagr_3yr")
    if rev_cagr is not None:
        score = _piecewise_linear_score(
            rev_cagr,
            [(-0.10, 0), (0, 20), (0.05, 50), (0.12, 75), (0.25, 95), (0.50, 100)]
        )
        metrics_scored["revenue_cagr_3yr"] = {"value": rev_cagr, "score": score}

    # Revenue 1yr — recent growth
    rev_1yr = block.get("revenue_growth_1yr")
    if rev_1yr is not None:
        score = _piecewise_linear_score(
            rev_1yr,
            [(-0.15, 0), (0, 25), (0.05, 50), (0.15, 80), (0.30, 100)]
        )
        metrics_scored["revenue_growth_1yr"] = {"value": rev_1yr, "score": score}

    # EPS 3yr CAGR (proxied by net income CAGR)
    eps_cagr = block.get("eps_cagr_3yr")
    if eps_cagr is not None:
        score = _piecewise_linear_score(
            eps_cagr,
            [(-0.20, 0), (0, 20), (0.10, 60), (0.20, 85), (0.40, 100)]
        )
        metrics_scored["eps_cagr_3yr"] = {"value": eps_cagr, "score": score}

    # EPS 1yr
    eps_1yr = block.get("eps_growth_1yr")
    if eps_1yr is not None:
        score = _piecewise_linear_score(
            eps_1yr,
            [(-0.30, 0), (0, 30), (0.10, 60), (0.25, 90), (0.50, 100)]
        )
        metrics_scored["eps_growth_1yr"] = {"value": eps_1yr, "score": score}

    # FCF 3yr CAGR — quality of growth
    fcf_cagr = block.get("fcf_cagr_3yr")
    if fcf_cagr is not None:
        score = _piecewise_linear_score(
            fcf_cagr,
            [(-0.25, 0), (0, 25), (0.10, 60), (0.20, 85), (0.40, 100)]
        )
        metrics_scored["fcf_cagr_3yr"] = {"value": fcf_cagr, "score": score}

    # Weights — favor 3yr CAGRs over 1yr
    weights = {
        "revenue_cagr_3yr": 0.30,
        "revenue_growth_1yr": 0.15,
        "eps_cagr_3yr": 0.25,
        "eps_growth_1yr": 0.10,
        "fcf_cagr_3yr": 0.20,
    }
    weighted_pairs = [
        (metrics_scored[m]["score"], weights[m]) for m in metrics_scored
    ]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


def score_valuation(raw: dict, peer_stats: dict) -> dict:
    """
    Valuation sub-score (mixed: absolute thresholds + sector-relative for EV/EBITDA).
    Components: P/E trailing, P/E forward, EV/EBITDA (sector-relative), FCF yield, PEG.
    Lower multiples = better; FCF yield higher = better.
    """
    block = raw.get("valuation", {}) or {}
    industry = raw.get("industry")
    sector = raw.get("sector")

    metrics_scored = {}

    # P/E trailing — absolute, lower better
    pe_t = block.get("pe_trailing")
    if pe_t is not None and pe_t > 0:
        score = _piecewise_linear_score(
            pe_t,
            [(5, 100), (10, 90), (15, 75), (20, 60), (30, 35), (50, 10), (80, 0)]
        )
        metrics_scored["pe_trailing"] = {"value": pe_t, "score": score}

    # P/E forward — absolute, lower better
    pe_f = block.get("pe_forward")
    if pe_f is not None and pe_f > 0:
        score = _piecewise_linear_score(
            pe_f,
            [(5, 100), (10, 90), (15, 75), (20, 60), (30, 35), (50, 10), (80, 0)]
        )
        metrics_scored["pe_forward"] = {"value": pe_f, "score": score}

    # EV/EBITDA — sector-relative (cheaper than peers = better, so invert the z)
    ev_ebitda = block.get("ev_ebitda")
    if ev_ebitda is not None and ev_ebitda > 0:
        dist = _get_peer_distribution(peer_stats, industry, sector, "ev_ebitda")
        if dist is None:
            score = 50.0
            src = "neutral_fallback"
        else:
            z = _z_score(ev_ebitda, dist["mean"], dist["stdev"])
            if z is None:
                score = 50.0
                src = "neutral_fallback"
            else:
                # Invert: cheaper than peers = good
                score = _z_to_score(-z)
                src = dist["source"]
        metrics_scored["ev_ebitda"] = {"value": ev_ebitda, "score": score, "source": src}

    # FCF yield — higher better
    fcf_y = block.get("fcf_yield")
    if fcf_y is not None:
        score = _piecewise_linear_score(
            fcf_y,
            [(-0.05, 0), (0, 10), (0.02, 30), (0.04, 60), (0.06, 85), (0.10, 100)]
        )
        metrics_scored["fcf_yield"] = {"value": fcf_y, "score": score}

    # PEG ratio — lower better (~1 = fair)
    peg = block.get("peg_ratio")
    if peg is not None and peg > 0:
        score = _piecewise_linear_score(
            peg,
            [(0.3, 100), (0.7, 90), (1.0, 75), (1.5, 50), (2.5, 20), (4.0, 0)]
        )
        metrics_scored["peg_ratio"] = {"value": peg, "score": score}

    # Weights
    weights = {
        "pe_trailing": 0.20,
        "pe_forward": 0.20,
        "ev_ebitda": 0.25,
        "fcf_yield": 0.20,
        "peg_ratio": 0.15,
    }
    weighted_pairs = [
        (metrics_scored[m]["score"], weights[m]) for m in metrics_scored
    ]
    final_score = _weighted_average(weighted_pairs)

    return {
        "score": final_score,
        "metrics": metrics_scored,
    }


# ============ DRIVERS EXTRACTION ============

def _flatten_metrics_for_drivers(subscore_results: dict) -> list:
    """
    Flatten all sub-component metrics into a single list for driver extraction.
    Returns: [(label, score, value, source_subcomponent), ...]
    """
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
    """
    Identify the top N strongest and weakest individual metrics across all sub-components.
    Returns:
    {
        "strongest": ["label (score=92, value=...)", ...],
        "weakest": ["label (score=18, value=...)", ...]
    }
    """
    flat = _flatten_metrics_for_drivers(subscore_results)
    if not flat:
        return {"strongest": [], "weakest": []}

    # Sort by score
    sorted_by_score = sorted(flat, key=lambda x: x[1])

    weakest = sorted_by_score[:top_n]
    strongest = sorted_by_score[-top_n:][::-1]  # highest first

    def _format(item):
        label, score, value, _ = item
        if value is None:
            return f"{label}: score={score:.0f}"
        # Format value as percentage if abs <1, else as number with 2 decimals
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
    Top-level: score one company across all 6 sub-components, combine into final
    weighted score, and extract drivers.

    Returns:
    {
        "score": float (0-100, weighted across sub-components),
        "subscores": {"profitability": 82, "returns_on_capital": 85, ...},
        "drivers": {"strongest": [...], "weakest": [...]},
        "subcomponent_details": { ...full details for debugging... }
    }
    """
    if not raw:
        return {"score": None, "subscores": {}, "drivers": {"strongest": [], "weakest": []}, "subcomponent_details": {}}

    subscore_results = {
        "profitability": score_profitability(raw, peer_stats),
        "returns_on_capital": score_returns_on_capital(raw, peer_stats),
        "cash_flow_quality": score_cash_flow_quality(raw, peer_stats),
        "balance_sheet": score_balance_sheet(raw, peer_stats),
        "growth": score_growth(raw, peer_stats),
        "valuation": score_valuation(raw, peer_stats),
    }

    # Compute weighted final score
    weighted_pairs = []
    for subcomp_name, weight in SUBCOMPONENT_WEIGHTS.items():
        sub_score = subscore_results[subcomp_name].get("score")
        if sub_score is not None:
            weighted_pairs.append((sub_score, weight))

    final_score = _weighted_average(weighted_pairs, min_count=3)

    # Extract drivers
    drivers = extract_drivers(subscore_results)

    # Subscores summary
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