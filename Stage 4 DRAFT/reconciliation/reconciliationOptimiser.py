"""
Reconciliation Quant Proposer.

Given the union of an incumbent portfolio and a freshly generated candidate
portfolio, propose the best 15-name portfolio to transition to. Mirrors Track A's
MIQP (`track A/quantOptimiser.py:optimize_portfolio`) — binary selection vars plus
continuous weights — and extends it with:

    - a turnover penalty, so weight/name churn is only taken when it pays for itself
    - a small inclusion bonus for names in the fresh candidate portfolio
      ("strongly favored, but still debatable")
    - must_include / must_exclude lock constraints, which carry the verdicts of
      already-settled debates

Objective (maximize):
    sum(w_i * expected_return_i)
      - LAMBDA   * sum(w_i^2)                       concentration penalty
      - GAMMA    * sum(|w_i - w_incumbent_i|)       turnover penalty
      + BETA     * sum(candidate_bonus_i * z_i)     fresh-candidate inclusion bonus

Subject to:
    sum(z) == 15
    sum(w) == 1
    0.03 * z_i <= w_i <= 0.20 * z_i
    per-sector weight sum <= 0.35
    z_i == 1 for must_include, z_i == 0 for must_exclude

There is deliberately no `base_return_12m > 0` pre-filter (Track A applies one):
an underperforming incumbent must still be *considered* — its weakness is argued
in the debate, not silently filtered out.
"""

import logging
import cvxpy as cp
import numpy as np

logger = logging.getLogger(__name__)

# ============ CONFIG ============

PORTFOLIO_SIZE = 15
MIN_POSITION_WEIGHT = 0.03
MAX_POSITION_WEIGHT = 0.20
SECTOR_CAP = 0.35

LAMBDA = 2.0           # concentration penalty — matches Track A and the allocator
TURNOVER_GAMMA = 0.05  # turnover penalty — anchors weights to the incumbent and
                       # suppresses trivial name swaps; a material return edge
                       # still gets proposed (and then debated). Higher = stickier.
CANDIDATE_BETA = 0.05  # inclusion bonus per fresh-candidate name — encodes
                       # "strongly favored, but still debatable".


def _constraints_dict() -> dict:
    return {
        "portfolio_size": PORTFOLIO_SIZE,
        "min_position_weight": MIN_POSITION_WEIGHT,
        "max_position_weight": MAX_POSITION_WEIGHT,
        "sector_cap": SECTOR_CAP,
        "lambda_concentration_penalty": LAMBDA,
        "turnover_gamma": TURNOVER_GAMMA,
        "candidate_beta": CANDIDATE_BETA,
    }


def propose(
    union: list,
    incumbent_weights: dict,
    candidate_tickers,
    must_include=None,
    must_exclude=None,
) -> dict:
    """
    Propose a reconciled 15-name portfolio.

    Args:
        union: list of dicts, each with ticker, expected_return_12m, sector.
        incumbent_weights: dict ticker -> weight (fraction of 1.0); names not held
            are absent (treated as weight 0).
        candidate_tickers: iterable of tickers in the fresh candidate portfolio.
        must_include: iterable of tickers forced into the portfolio (debate-locked).
        must_exclude: iterable of tickers forced out of the portfolio (debate-locked).

    Returns:
        {
            "status": "optimal" | "infeasible" | "error",
            "objective_value": float | None,
            "positions": [{"ticker", "allocation_pct", "sector",
                           "expected_return_12m"}, ...],   # 15 entries when optimal
            "infeasibility_reason": str | None,
            "constraints": {...},
        }
    """
    must_include = set(must_include or [])
    must_exclude = set(must_exclude or [])
    candidate_tickers = set(candidate_tickers or [])

    n = len(union)
    if n < PORTFOLIO_SIZE:
        return {
            "status": "infeasible",
            "objective_value": None,
            "positions": [],
            "infeasibility_reason": (
                f"Union has only {n} candidates; need at least {PORTFOLIO_SIZE}."
            ),
            "constraints": _constraints_dict(),
        }

    for i, c in enumerate(union):
        if not c.get("ticker"):
            return _error(f"Union entry {i} missing 'ticker'")
        if c.get("expected_return_12m") is None:
            return _error(f"{c['ticker']} missing expected_return_12m")
        if not c.get("sector"):
            return _error(f"{c['ticker']} missing sector")

    tickers = [c["ticker"] for c in union]
    ticker_index = {t: i for i, t in enumerate(tickers)}

    bad_locks = (must_include | must_exclude) - set(tickers)
    if bad_locks:
        logger.warning(f"Ignoring locks for tickers not in union: {sorted(bad_locks)}")
        must_include &= set(tickers)
        must_exclude &= set(tickers)

    if len(must_include) > PORTFOLIO_SIZE:
        return {
            "status": "infeasible",
            "objective_value": None,
            "positions": [],
            "infeasibility_reason": (
                f"{len(must_include)} names are debate-locked into the portfolio "
                f"but only {PORTFOLIO_SIZE} slots exist."
            ),
            "constraints": _constraints_dict(),
        }

    expected_returns = np.array([c["expected_return_12m"] for c in union], dtype=float)
    w_incumbent = np.array(
        [float(incumbent_weights.get(t, 0.0)) for t in tickers], dtype=float
    )
    candidate_bonus = np.array(
        [1.0 if t in candidate_tickers else 0.0 for t in tickers], dtype=float
    )

    sectors = [c["sector"] for c in union]
    sector_to_indices = {}
    for i, sec in enumerate(sectors):
        sector_to_indices.setdefault(sec, []).append(i)

    w = cp.Variable(n, nonneg=True)
    z = cp.Variable(n, boolean=True)

    objective = cp.Maximize(
        expected_returns @ w
        - LAMBDA * cp.sum_squares(w)
        - TURNOVER_GAMMA * cp.sum(cp.abs(w - w_incumbent))
        + CANDIDATE_BETA * (candidate_bonus @ z)
    )

    constraints = [
        cp.sum(z) == PORTFOLIO_SIZE,
        cp.sum(w) == 1.0,
        w >= MIN_POSITION_WEIGHT * z,
        w <= MAX_POSITION_WEIGHT * z,
    ]
    for sec, indices in sector_to_indices.items():
        constraints.append(cp.sum([w[i] for i in indices]) <= SECTOR_CAP)
    for t in must_include:
        constraints.append(z[ticker_index[t]] == 1)
    for t in must_exclude:
        constraints.append(z[ticker_index[t]] == 0)

    problem = cp.Problem(objective, constraints)

    # MIQP — same solver chain Track A relies on. CBC is excluded: it's MILP-only
    # and would just contribute a misleading "CBC not installed" message if it
    # were the last solver tried after SCIP/GUROBI also fall through.
    solvers_to_try = ["SCIPY", "SCIP", "GUROBI"]
    solve_error = None
    for solver in solvers_to_try:
        try:
            problem.solve(solver=solver)
            if problem.status in ("optimal", "optimal_inaccurate"):
                break
        except cp.error.SolverError as e:
            solve_error = e
            continue
        except Exception as e:
            solve_error = e
            continue

    if problem.status not in ("optimal", "optimal_inaccurate"):
        return {
            "status": "infeasible",
            "objective_value": None,
            "positions": [],
            "infeasibility_reason": (
                f"Proposer could not find an optimal solution: status={problem.status}, "
                f"last_solver_error={solve_error}"
            ),
            "constraints": _constraints_dict(),
        }

    weights = w.value
    selected = z.value
    if weights is None or selected is None:
        return _error("Solver returned None values")

    positions = []
    for i, c in enumerate(union):
        if selected[i] > 0.5:
            positions.append({
                "ticker": c["ticker"],
                "allocation_pct": round(float(weights[i]) * 100.0, 4),
                "sector": c["sector"],
                "expected_return_12m": round(float(c["expected_return_12m"]), 4),
            })
    positions.sort(key=lambda p: -p["allocation_pct"])

    return {
        "status": "optimal",
        "objective_value": round(float(problem.value), 6),
        "positions": positions,
        "infeasibility_reason": None,
        "constraints": _constraints_dict(),
    }


def _error(reason: str) -> dict:
    return {
        "status": "error",
        "objective_value": None,
        "positions": [],
        "infeasibility_reason": reason,
        "constraints": _constraints_dict(),
    }
