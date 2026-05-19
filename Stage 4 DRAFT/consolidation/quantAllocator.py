"""
Consolidation Quant Allocator (functional).

Given a fixed set of 15 ticker names with their expected returns and sectors,
solve a continuous QP to assign optimal allocations:

    maximize:  sum(w_i * expected_return_12m_i) - LAMBDA * sum(w_i^2)
    subject to:
        sum(w_i) = 1
        0.03 <= w_i <= 0.20  for each i
        per-sector weight sums <= 0.35

If the selection makes the problem infeasible (e.g., too many names in one sector),
returns a structured infeasibility result with a human-readable reason for the
LLM to act on in the next iteration.
"""

import logging
import cvxpy as cp
import numpy as np

logger = logging.getLogger(__name__)

# ============ CONFIG (matches Track A) ============

MIN_POSITION_WEIGHT = 0.03
MAX_POSITION_WEIGHT = 0.20
SECTOR_CAP = 0.35
LAMBDA = 2.0  # same quadratic concentration penalty as Track A
PORTFOLIO_SIZE = 15


# ============ FEASIBILITY PRE-CHECK ============

def _check_sector_feasibility(picks: list) -> tuple:
    """
    Quick pre-check before solving: identify sector overloads that make the
    problem trivially infeasible.

    For sector S with N picks, the minimum possible sector weight is N * 0.03.
    If that exceeds 0.35, the cap is unsatisfiable.

    Returns (is_feasible: bool, reason: str or None).
    """
    sector_counts = {}
    for p in picks:
        sec = p.get("sector") or "Unknown"
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    max_per_sector = int(SECTOR_CAP / MIN_POSITION_WEIGHT)  # 35 / 3 = 11.67 -> 11

    overloads = []
    for sec, count in sector_counts.items():
        if count > max_per_sector:
            overloads.append(
                f"{sec} has {count} picks; max allowed is {max_per_sector} "
                f"(since {max_per_sector+1} * {MIN_POSITION_WEIGHT*100:.0f}% = "
                f"{(max_per_sector+1) * MIN_POSITION_WEIGHT * 100:.0f}% which exceeds "
                f"the {SECTOR_CAP*100:.0f}% sector cap)"
            )

    if overloads:
        reason = (
            "Sector cap infeasible — too many picks in one or more sectors:\n  - "
            + "\n  - ".join(overloads)
            + "\n\nFix: drop names from the over-concentrated sector(s) and replace "
              "with names from less-represented sectors in the union pool."
        )
        return False, reason

    return True, None


# ============ ALLOCATION ============

def _constraints_dict() -> dict:
    return {
        "portfolio_size": PORTFOLIO_SIZE,
        "min_position_weight": MIN_POSITION_WEIGHT,
        "max_position_weight": MAX_POSITION_WEIGHT,
        "sector_cap": SECTOR_CAP,
        "lambda_concentration_penalty": LAMBDA,
    }


def allocate(picks: list) -> dict:
    """
    Solve the QP for fixed-name allocation.

    Args:
        picks: list of dicts, each containing:
            - ticker (str)
            - expected_return_12m (float)
            - sector (str)

    Returns:
        {
            "status": "optimal" | "infeasible" | "error",
            "objective_value": float | None,
            "allocations": [
                {"ticker": ..., "allocation_pct": ..., "sector": ..., "expected_return_12m": ...},
                ...
            ],
            "infeasibility_reason": str | None,
            "constraints": {...},
        }
    """
    n = len(picks)

    if n != PORTFOLIO_SIZE:
        return {
            "status": "error",
            "objective_value": None,
            "allocations": [],
            "infeasibility_reason": (
                f"Allocator received {n} picks but PORTFOLIO_SIZE is {PORTFOLIO_SIZE}. "
                f"The selector must produce exactly {PORTFOLIO_SIZE} picks."
            ),
            "constraints": _constraints_dict(),
        }

    # Validate inputs
    for i, p in enumerate(picks):
        if "ticker" not in p:
            return {
                "status": "error", "objective_value": None, "allocations": [],
                "infeasibility_reason": f"Pick {i+1} missing 'ticker'",
                "constraints": _constraints_dict(),
            }
        if p.get("expected_return_12m") is None:
            return {
                "status": "error", "objective_value": None, "allocations": [],
                "infeasibility_reason": f"{p['ticker']} missing expected_return_12m",
                "constraints": _constraints_dict(),
            }
        if not p.get("sector"):
            return {
                "status": "error", "objective_value": None, "allocations": [],
                "infeasibility_reason": f"{p['ticker']} missing sector",
                "constraints": _constraints_dict(),
            }

    # Pre-check sector feasibility before invoking solver
    feasible, reason = _check_sector_feasibility(picks)
    if not feasible:
        return {
            "status": "infeasible",
            "objective_value": None,
            "allocations": [],
            "infeasibility_reason": reason,
            "constraints": _constraints_dict(),
        }

    # Build the QP
    expected_returns = np.array([p["expected_return_12m"] for p in picks], dtype=float)
    sectors = [p["sector"] for p in picks]
    unique_sectors = sorted(set(sectors))
    sector_to_indices = {s: [i for i, sec in enumerate(sectors) if sec == s] for s in unique_sectors}

    w = cp.Variable(n, nonneg=True)

    objective = cp.Maximize(expected_returns @ w - LAMBDA * cp.sum_squares(w))

    constraints = [
        cp.sum(w) == 1.0,
        w >= MIN_POSITION_WEIGHT,
        w <= MAX_POSITION_WEIGHT,
    ]
    for sec, indices in sector_to_indices.items():
        constraints.append(cp.sum([w[i] for i in indices]) <= SECTOR_CAP)

    problem = cp.Problem(objective, constraints)

    # Pure QP (no integer variables), so the solver chain is simpler than Track A's
    solvers_to_try = ["SCIPY", "CLARABEL", "OSQP", "SCS"]
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
        # Solver-level infeasibility — shouldn't normally happen given our pre-check
        return {
            "status": "infeasible",
            "objective_value": None,
            "allocations": [],
            "infeasibility_reason": (
                f"Allocator could not find an optimal solution: status={problem.status}, "
                f"last_solver_error={solve_error}"
            ),
            "constraints": _constraints_dict(),
        }

    weights = w.value
    if weights is None:
        return {
            "status": "error",
            "objective_value": None,
            "allocations": [],
            "infeasibility_reason": "Solver returned None weights",
            "constraints": _constraints_dict(),
        }

    allocations = []
    for i, p in enumerate(picks):
        allocations.append({
            "ticker": p["ticker"],
            "allocation_pct": round(float(weights[i]) * 100.0, 4),
            "sector": p["sector"],
            "expected_return_12m": round(float(p["expected_return_12m"]), 4),
        })
    allocations.sort(key=lambda a: -a["allocation_pct"])

    return {
        "status": "optimal",
        "objective_value": round(float(problem.value), 6),
        "allocations": allocations,
        "infeasibility_reason": None,
        "constraints": _constraints_dict(),
    }