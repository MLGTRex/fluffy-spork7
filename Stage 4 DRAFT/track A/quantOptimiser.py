"""
Track A — Pure Quant Portfolio Construction.

Solves a Mixed Integer Linear Program (MILP) via cvxpy:
    Maximize:   sum(w_i * expected_return_12m_i)
    Subject to:
        - exactly 15 positions (sum of binary selection variables = 15)
        - 3% min, 20% max per selected position
        - 35% sector cap (sum of weights in any sector <= 0.35)
        - Universe pre-filter: drop companies with base_return_12m <= 0
        - Weights sum to 1.0

This is pure quant — no LLM, no judgment. Outputs the math-optimal portfolio
under the constraints.
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

# Concentration penalty (quadratic regularization on weights).
#   LAMBDA = 0.0   -> pure linear objective; weights pin at min/max corners
#   LAMBDA = 0.5   -> mild spreading; top names still dominant but others get >3%
#   LAMBDA = 1.0   -> clear proportional sizing across the portfolio
#   LAMBDA = 2.0   -> stronger spreading; weights cluster more around average
#   LAMBDA = 5.0+  -> approaches equal-weight; expected return barely matters
LAMBDA = 2.0


# ============ HELPERS ============

def _filter_eligible_candidates(candidates: list) -> tuple:
    """
    Apply the pre-filter: only candidates with base_return_12m > 0 are eligible.

    Args:
        candidates: list of dicts, each containing at minimum:
            - ticker
            - expected_return_12m
            - base_return_12m
            - sector

    Returns:
        (eligible, filtered_out, filter_reasons) where:
            eligible: list of eligible candidate dicts
            filtered_out: list of (ticker, reason) tuples
    """
    eligible = []
    filtered_out = []

    for c in candidates:
        ticker = c.get("ticker", "?")
        exp_ret = c.get("expected_return_12m")
        base_ret = c.get("base_return_12m")
        sector = c.get("sector")

        if exp_ret is None:
            filtered_out.append((ticker, "missing expected_return_12m"))
            continue
        if base_ret is None:
            filtered_out.append((ticker, "missing base_return_12m"))
            continue
        if base_ret <= 0:
            filtered_out.append((ticker, f"base_return_12m={base_ret:.4f} <= 0"))
            continue
        if not sector:
            filtered_out.append((ticker, "missing sector"))
            continue

        eligible.append(c)

    return eligible, filtered_out


# ============ CORE OPTIMIZATION ============

def optimize_portfolio(candidates: list) -> dict:
    """
    Solve the MILP for portfolio construction.

    Args:
        candidates: list of dicts with at minimum: ticker, expected_return_12m,
                    base_return_12m, sector

    Returns:
        {
            "status": "optimal" | "infeasible" | "error",
            "objective_value": float | None,
            "positions": [
                {"ticker": ..., "allocation_pct": ..., "sector": ...,
                 "expected_return_12m": ..., "base_return_12m": ...},
                ... 15 entries
            ],
            "filtered_out": [(ticker, reason), ...],
            "n_eligible": int,
            "error_message": str | None
        }
    """
    eligible, filtered_out = _filter_eligible_candidates(candidates)
    n_eligible = len(eligible)

    if n_eligible < PORTFOLIO_SIZE:
        return {
            "status": "infeasible",
            "objective_value": None,
            "positions": [],
            "filtered_out": filtered_out,
            "n_eligible": n_eligible,
            "error_message": (
                f"Only {n_eligible} eligible candidates after pre-filter; "
                f"need at least {PORTFOLIO_SIZE}"
            ),
        }

    # Build sector index: ticker_index -> sector, and sector -> list of ticker_indices
    n = n_eligible
    tickers = [c["ticker"] for c in eligible]
    expected_returns = np.array([c["expected_return_12m"] for c in eligible], dtype=float)
    sectors = [c["sector"] for c in eligible]
    unique_sectors = sorted(set(sectors))
    sector_to_indices = {s: [] for s in unique_sectors}
    for i, sec in enumerate(sectors):
        sector_to_indices[sec].append(i)

    # Decision variables
    # w[i] = weight allocated to candidate i (continuous in [0, 1])
    # z[i] = 1 if candidate i is selected, 0 otherwise (binary)
    w = cp.Variable(n, nonneg=True)
    z = cp.Variable(n, boolean=True)

    # Objective: maximize sum(w_i * expected_return_12m_i) - LAMBDA * sum(w_i^2)
    # The quadratic penalty breaks the corner-sitting behavior of pure linear objectives,
    # producing portfolios where weights reflect relative return rather than pinning at min/max.
    objective = cp.Maximize(expected_returns @ w - LAMBDA * cp.sum_squares(w))

    constraints = []

    # Exactly 15 positions
    constraints.append(cp.sum(z) == PORTFOLIO_SIZE)

    # Weights sum to 1
    constraints.append(cp.sum(w) == 1.0)

    # Linking: weight is zero unless selected; within [min, max] when selected
    # 0.03 * z[i] <= w[i] <= 0.20 * z[i]
    constraints.append(w >= MIN_POSITION_WEIGHT * z)
    constraints.append(w <= MAX_POSITION_WEIGHT * z)

    # Sector cap: sum of weights in any sector <= 0.35
    for sec, indices in sector_to_indices.items():
        if not indices:
            continue
        constraints.append(cp.sum([w[i] for i in indices]) <= SECTOR_CAP)

    problem = cp.Problem(objective, constraints)

    # Solve. The quadratic objective makes this a Mixed Integer Quadratic Program (MIQP).
    # We try solvers that support MIQP: SCIP (via SCIPY/SCIPOpt), GUROBI (if licensed), CBC.
    # ECOS_BB and GLPK_MI can't handle quadratic objectives and are excluded.
    solvers_to_try = ["SCIPY", "SCIP", "GUROBI", "CBC"]
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
            "status": "error" if problem.status is None else problem.status,
            "objective_value": None,
            "positions": [],
            "filtered_out": filtered_out,
            "n_eligible": n_eligible,
            "error_message": (
                f"cvxpy could not find an optimal solution: "
                f"status={problem.status}, last_error={solve_error}"
            ),
        }

    # Extract solution
    weights = w.value
    selected = z.value
    if weights is None or selected is None:
        return {
            "status": "error",
            "objective_value": None,
            "positions": [],
            "filtered_out": filtered_out,
            "n_eligible": n_eligible,
            "error_message": "Solver returned None values",
        }

    # Build positions list
    positions = []
    for i, c in enumerate(eligible):
        # Selected if z[i] is approximately 1
        if selected[i] > 0.5:
            allocation_pct = round(float(weights[i]) * 100.0, 4)
            positions.append({
                "ticker": c["ticker"],
                "allocation_pct": allocation_pct,
                "sector": c["sector"],
                "expected_return_12m": round(c["expected_return_12m"], 4),
                "base_return_12m": round(c["base_return_12m"], 4),
            })

    # Sort by allocation descending for readability
    positions.sort(key=lambda p: -p["allocation_pct"])

    return {
        "status": "optimal",
        "objective_value": round(float(problem.value), 6),
        "positions": positions,
        "filtered_out": filtered_out,
        "n_eligible": n_eligible,
        "error_message": None,
    }


# ============ PUBLIC ENTRY POINT ============

def construct_track_a_portfolio(candidates: list) -> dict:
    """
    Public entry point for Track A portfolio construction.

    Args:
        candidates: list of candidate dicts (50 from Stage 3)

    Returns:
        Full Track A result dict suitable for writing to JSON.
    """
    result = optimize_portfolio(candidates)

    # Build a clean output dict
    output = {
        "track": "A",
        "method": "Quant MILP with quadratic concentration penalty",
        "constraints": {
            "portfolio_size": PORTFOLIO_SIZE,
            "min_position_weight": MIN_POSITION_WEIGHT,
            "max_position_weight": MAX_POSITION_WEIGHT,
            "sector_cap": SECTOR_CAP,
            "base_return_filter": "base_return_12m > 0",
            "lambda_concentration_penalty": LAMBDA,
        },
        "n_input_candidates": len(candidates),
        "n_eligible": result["n_eligible"],
        "n_filtered_out": len(result["filtered_out"]),
        "filtered_out": [
            {"ticker": t, "reason": r} for t, r in result["filtered_out"]
        ],
        "status": result["status"],
        "objective_value": result["objective_value"],
        "positions": result["positions"],
        "error_message": result["error_message"],
    }

    return output