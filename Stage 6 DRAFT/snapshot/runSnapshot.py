"""
Snapshot pass.

Walks every upstream file in SNAPSHOT_SOURCES, hashes it, compares against the
prior snapshot's manifest, and either copies a fresh full snapshot into
backups/snapshots/{ISO_TS}/ or records that nothing changed.

Also archives rolling *_log_YYYY-MM-DD.log files into backups/logs/, dedup by
sha256, never deleting the originals.

Pure file I/O — no Alpaca, no UI building. Other Stage 6 steps consume the
snapshot id this returns.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ============ Snapshot id ============

def _snapshot_id_now() -> str:
    """Filesystem-safe ISO-8601 UTC: 2026-05-20T14-30-00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


# ============ Hashing ============

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ============ Source discovery ============

def _discover_upstream_files() -> list[dict]:
    """
    Returns a list of {section, abs_path, rel_path} for every file currently
    matched by SNAPSHOT_SOURCES. rel_path is what gets written under the
    snapshot folder (e.g. 'stage4/consolidation_portfolio.json').
    """
    found: list[dict] = []
    for section, sources in config.SNAPSHOT_SOURCES.items():
        for source_dir, pattern in sources:
            if not os.path.isdir(source_dir):
                continue
            matches = sorted(glob.glob(os.path.join(source_dir, pattern)))
            for abs_path in matches:
                if not os.path.isfile(abs_path):
                    continue
                filename = os.path.basename(abs_path)
                # The directory portion under each section: keep portfolio
                # history files under stage4/portfolio_history/ instead of the
                # space-version, so the snapshot tree is shell-friendly.
                parent_dir = os.path.basename(os.path.dirname(abs_path))
                if parent_dir == "company_data":
                    rel_path = os.path.join(section, "company_data", filename)
                elif parent_dir == "portfolio history":
                    rel_path = os.path.join(section, "portfolio_history", filename)
                else:
                    rel_path = os.path.join(section, filename)
                found.append({
                    "section": section,
                    "abs_path": abs_path,
                    "rel_path": rel_path,
                })
    return found


# ============ Prior-manifest lookup ============

def _list_existing_snapshots() -> list[str]:
    if not os.path.isdir(config.SNAPSHOTS_DIR):
        return []
    return sorted(
        d for d in os.listdir(config.SNAPSHOTS_DIR)
        if os.path.isdir(os.path.join(config.SNAPSHOTS_DIR, d))
    )


def _load_prior_manifest() -> Optional[dict]:
    snapshots = _list_existing_snapshots()
    if not snapshots:
        return None
    newest = snapshots[-1]
    manifest_path = os.path.join(config.SNAPSHOTS_DIR, newest, "manifest.json")
    if not os.path.isfile(manifest_path):
        logger.warning(
            "Prior snapshot %s has no manifest.json — treating as no prior", newest
        )
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not parse prior manifest %s: %s", manifest_path, e)
        return None


# ============ Upstream anchors ============

def _compute_upstream_anchors() -> dict:
    """
    Extracts a few timestamp-like fields from the upstream files so the UI
    can answer 'which pipeline run is this snapshot from' without diffing.

    All accesses defensive — missing files / missing fields → null.
    """
    anchors: dict = {
        "stage1_universe_fetched_date": None,
        "stage2_max_synthesis_date": None,
        "stage3_max_consolidation_date": None,
        "stage4_consolidation_date": None,
        "stage4_reconciled_date": None,
        "latest_execution_timestamp": None,
    }

    def _load(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    universe = _load(os.path.join(config.STAGE1_OUTPUT, "universe.json"))
    if universe:
        anchors["stage1_universe_fetched_date"] = universe.get("fetched_date")

    if os.path.isdir(config.STAGE2_OUTPUT):
        max_synth = None
        for p in glob.glob(os.path.join(config.STAGE2_OUTPUT, "*_research.json")):
            d = _load(p) or {}
            sd = d.get("synthesis_date")
            if sd and (max_synth is None or sd > max_synth):
                max_synth = sd
        anchors["stage2_max_synthesis_date"] = max_synth

    if os.path.isdir(config.STAGE3_OUTPUT):
        max_cons = None
        for p in glob.glob(os.path.join(config.STAGE3_OUTPUT, "*_research.json")):
            d = _load(p) or {}
            cd = d.get("consolidation_date")
            if cd and (max_cons is None or cd > max_cons):
                max_cons = cd
        anchors["stage3_max_consolidation_date"] = max_cons

    consol = _load(os.path.join(config.STAGE4_OUTPUT, "consolidation_portfolio.json"))
    if consol:
        anchors["stage4_consolidation_date"] = consol.get("consolidation_date")
        anchors["stage4_reconciled_date"] = consol.get("reconciled_date")

    if os.path.isdir(config.STAGE4_EXECUTION_OUTPUT):
        execs = sorted(glob.glob(
            os.path.join(config.STAGE4_EXECUTION_OUTPUT, "execution_*.json")
        ))
        if execs:
            last = _load(execs[-1])
            if last:
                anchors["latest_execution_timestamp"] = last.get("execution_timestamp")

    return anchors


# ============ Snapshot creation ============

def _build_files_block(files: list[dict]) -> list[dict]:
    """Hash + size + source mtime per discovered file."""
    block = []
    for entry in files:
        abs_path = entry["abs_path"]
        try:
            sha = _sha256_file(abs_path)
            size = os.path.getsize(abs_path)
            mtime = datetime.fromtimestamp(
                os.path.getmtime(abs_path), tz=timezone.utc
            ).isoformat()
        except OSError as e:
            logger.warning("Could not stat/hash %s: %s", abs_path, e)
            continue
        block.append({
            "path": entry["rel_path"].replace(os.sep, "/"),
            "sha256": sha,
            "size": size,
            "source_mtime": mtime,
            "_abs_path": abs_path,
        })
    return block


def _diff_against_prior(current: list[dict], prior_manifest: Optional[dict]) -> dict:
    """Compute added/modified/removed paths vs the prior manifest."""
    if not prior_manifest:
        return {"added": [e["path"] for e in current], "modified": [], "removed": []}
    prior = {e["path"]: e["sha256"] for e in prior_manifest.get("files", [])}
    current_paths = {e["path"]: e["sha256"] for e in current}
    added = sorted(p for p in current_paths if p not in prior)
    modified = sorted(
        p for p in current_paths if p in prior and current_paths[p] != prior[p]
    )
    removed = sorted(p for p in prior if p not in current_paths)
    return {"added": added, "modified": modified, "removed": removed}


def _copy_into_snapshot(files: list[dict], snapshot_dir: str) -> None:
    for entry in files:
        target = os.path.join(snapshot_dir, entry["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(entry["_abs_path"], target)


def _write_manifest(
    snapshot_dir: str,
    snapshot_id: str,
    prior_snapshot_id: Optional[str],
    files: list[dict],
    changes: dict,
    anchors: dict,
) -> str:
    public_files = [
        {k: v for k, v in e.items() if k != "_abs_path"} for e in files
    ]
    manifest = {
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prior_snapshot_id": prior_snapshot_id,
        "tracked_file_count": len(public_files),
        "files": public_files,
        "changes_since_prior": changes,
        "upstream_anchors": anchors,
    }
    manifest_path = os.path.join(snapshot_dir, "manifest.json")
    _atomic_write_json(manifest_path, manifest)
    return manifest_path


def _atomic_write_json(path: str, data) -> None:
    """Write JSON to path atomically: temp + rename."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ============ Log archive pass ============

def archive_logs() -> dict:
    """
    Copy every *_log_*.log file under UPSTREAM_LOG_ROOTS into backups/logs/,
    grouped by the date in the filename. Dedup by sha256 — never delete
    originals.
    """
    archived = 0
    skipped = 0
    failed = 0
    for root in config.UPSTREAM_LOG_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if "_log_" not in name or not name.endswith(".log"):
                    continue
                src = os.path.join(dirpath, name)
                date_tag = _date_tag_from_log_name(name)
                target_dir = os.path.join(config.LOGS_ARCHIVE_DIR, date_tag or "undated")
                target = os.path.join(target_dir, name)
                try:
                    if os.path.exists(target) and _sha256_file(src) == _sha256_file(target):
                        skipped += 1
                        continue
                    os.makedirs(target_dir, exist_ok=True)
                    shutil.copy2(src, target)
                    archived += 1
                except Exception as e:
                    logger.warning("Failed to archive log %s: %s", src, e)
                    failed += 1
    return {"archived": archived, "skipped_unchanged": skipped, "failed": failed}


def _date_tag_from_log_name(name: str) -> Optional[str]:
    """*_log_2026-05-20.log -> 2026-05-20."""
    import re
    m = re.search(r"_log_(\d{4}-\d{2}-\d{2})\.log$", name)
    return m.group(1) if m else None


# ============ Entry point ============

def run_snapshot() -> dict:
    """
    Returns:
        {
            "snapshot_id": "<id>" or None if skipped,
            "snapshot_dir": absolute path or None,
            "snapshot_skipped": bool,
            "prior_snapshot_id": "<id>" or None,
            "tracked_file_count": int,
            "changes": {"added": [], "modified": [], "removed": []},
            "upstream_anchors": {...},
            "log_archive": {...},
        }
    """
    config.ensure_stage6_dirs()

    discovered = _discover_upstream_files()
    files = _build_files_block(discovered)
    logger.info("Discovered %d upstream files for snapshot tracking", len(files))

    prior = _load_prior_manifest()
    prior_id = prior.get("snapshot_id") if prior else None
    changes = _diff_against_prior(files, prior)
    anchors = _compute_upstream_anchors()

    log_archive = archive_logs()
    logger.info(
        "Log archive: %d copied, %d unchanged, %d failed",
        log_archive["archived"], log_archive["skipped_unchanged"], log_archive["failed"],
    )

    no_changes = (
        prior is not None
        and not changes["added"]
        and not changes["modified"]
        and not changes["removed"]
    )

    if no_changes:
        logger.info(
            "No upstream changes since snapshot %s — skipping new snapshot", prior_id
        )
        return {
            "snapshot_id": None,
            "snapshot_dir": None,
            "snapshot_skipped": True,
            "prior_snapshot_id": prior_id,
            "tracked_file_count": len(files),
            "changes": changes,
            "upstream_anchors": anchors,
            "log_archive": log_archive,
        }

    snapshot_id = _snapshot_id_now()
    snapshot_dir = os.path.join(config.SNAPSHOTS_DIR, snapshot_id)
    os.makedirs(snapshot_dir, exist_ok=True)

    _copy_into_snapshot(files, snapshot_dir)
    _write_manifest(snapshot_dir, snapshot_id, prior_id, files, changes, anchors)
    logger.info(
        "Wrote snapshot %s with %d files (added=%d modified=%d removed=%d)",
        snapshot_id, len(files),
        len(changes["added"]), len(changes["modified"]), len(changes["removed"]),
    )

    return {
        "snapshot_id": snapshot_id,
        "snapshot_dir": snapshot_dir,
        "snapshot_skipped": False,
        "prior_snapshot_id": prior_id,
        "tracked_file_count": len(files),
        "changes": changes,
        "upstream_anchors": anchors,
        "log_archive": log_archive,
    }


def newest_snapshot_dir() -> Optional[str]:
    """Returns the absolute path of the most recent snapshot dir, or None."""
    snapshots = _list_existing_snapshots()
    if not snapshots:
        return None
    return os.path.join(config.SNAPSHOTS_DIR, snapshots[-1])


def list_snapshot_ids() -> list[str]:
    """All snapshot ids in chronological order."""
    return _list_existing_snapshots()


def load_snapshot_manifest(snapshot_id: str) -> Optional[dict]:
    path = os.path.join(config.SNAPSHOTS_DIR, snapshot_id, "manifest.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not parse manifest for %s: %s", snapshot_id, e)
        return None
