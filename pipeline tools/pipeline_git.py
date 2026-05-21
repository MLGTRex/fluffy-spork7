"""Helper for per-company commits from inside the stage scripts.

Imported by Stage 2 and Stage 3 per-substage runners (deep research,
debate, scenarios, valuation metrics, consolidation, etc.) to push a
single company's *_research.json file to the dispatching branch as soon
as that company finishes a substage. No-ops when not running inside
GitHub Actions, so local runs are unaffected.

A single module-level asyncio.Lock serialises git operations so the
COMPANY_CONCURRENCY=5 in-flight companies don't race on the working
tree.
"""

import asyncio
import os
import subprocess

GIT_LOCK = asyncio.Lock()
_GIT_CONFIGURED = False
_REPO_ROOT = None


async def _ensure_repo_root():
    global _REPO_ROOT
    if _REPO_ROOT is None:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        _REPO_ROOT = result.stdout.strip()
    return _REPO_ROOT


async def _ensure_git_configured(repo_root: str):
    global _GIT_CONFIGURED
    if _GIT_CONFIGURED:
        return
    await asyncio.to_thread(
        subprocess.run,
        ["git", "config", "user.name", "github-actions[bot]"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    _GIT_CONFIGURED = True


async def commit_company_progress(file_name: str, stage_label: str, company: str):
    """Commit and push a single company's JSON file. No-op outside CI."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return

    run_id = os.getenv("GITHUB_RUN_ID", "local")
    branch = os.getenv("GITHUB_REF_NAME", "main")

    async with GIT_LOCK:
        try:
            repo_root = await _ensure_repo_root()
            await _ensure_git_configured(repo_root)

            add_result = await asyncio.to_thread(
                subprocess.run,
                ["git", "add", file_name],
                cwd=repo_root,
                capture_output=True,
            )
            if add_result.returncode != 0:
                print(f"[{company}] git add failed: {add_result.stderr.decode().strip()[:200]}")
                return

            diff_check = await asyncio.to_thread(
                subprocess.run,
                ["git", "diff", "--cached", "--quiet"],
                cwd=repo_root,
            )
            if diff_check.returncode == 0:
                return

            msg = f"Update {stage_label} for {company} (run {run_id})"
            commit_result = await asyncio.to_thread(
                subprocess.run,
                ["git", "commit", "-m", msg],
                cwd=repo_root,
                capture_output=True,
            )
            if commit_result.returncode != 0:
                print(f"[{company}] git commit failed: {commit_result.stderr.decode().strip()[:200]}")
                return

            for attempt, delay in [(1, 0), (2, 2), (3, 4), (4, 8), (5, 16)]:
                if delay:
                    await asyncio.sleep(delay)
                push_result = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "push", "-u", "origin", f"HEAD:{branch}"],
                    cwd=repo_root,
                    capture_output=True,
                )
                if push_result.returncode == 0:
                    print(f"[{company}] {stage_label} committed and pushed")
                    return
                err = push_result.stderr.decode().strip()[:200]
                print(f"[{company}] {stage_label} push attempt {attempt} failed: {err}")

            print(f"[{company}] {stage_label} push failed after retries; end-of-job commit step will pick it up")
        except Exception as e:
            print(f"[{company}] commit_company_progress error: {e}")
