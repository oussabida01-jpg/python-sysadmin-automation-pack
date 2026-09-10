#!/usr/bin/env python3
"""
stale_files_purger.py
=====================
Delete (or archive) log / backup / temporary files older than N days, with the
safety rails you want before pointing a delete loop at a production disk:

  * --dry-run is the DEFAULT: nothing is removed unless --apply is given
  * refuses to operate on filesystem roots and on obviously dangerous paths
  * pattern include/exclude filters, minimum-size and age selectors
  * --keep-min N always preserves the newest N matching files per folder
  * --move-to archives instead of deleting
  * optional empty-directory cleanup, per-run JSON/CSV report
  * uses modification, access or creation time as the age reference

Examples
--------
    python stale_files_purger.py -p /var/log --days 30 --pattern "*.log"
    python stale_files_purger.py -p /var/log --days 30 --pattern "*.log" --apply
    python stale_files_purger.py -p ./backups --days 14 --pattern "*.zip" --keep-min 3 --apply
    python stale_files_purger.py -p ./tmp --days 7 --move-to ./quarantine --apply
    python stale_files_purger.py -p ./logs --days 90 --remove-empty-dirs --report purge.json --apply
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence

LOG = logging.getLogger("stale_purger")

# Paths we refuse to purge, whatever the user asks.
PROTECTED = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/proc",
    "/root", "/sbin", "/srv", "/sys", "/usr", "/var", "/opt",
    "c:\\", "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\users", "c:\\programdata",
}

DEFAULT_PATTERNS = ["*.log", "*.log.*", "*.bak", "*.old", "*.tmp", "*.temp",
                    "*.gz", "*.zip", "*.dump", "*.sql", "*.sql.gz"]


def configure_logging(verbose: bool = False, log_file: str | None = None) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            print("WARNING: cannot open log file %s: %s" % (log_file, exc), file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return "%.1f %s" % (num_bytes, unit)
        num_bytes /= 1024.0
    return "%.1f PB" % num_bytes


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
def assert_safe_target(path: Path, force: bool) -> None:
    """Refuse roots, system folders and (unless --force) very shallow paths."""
    resolved = path.resolve()
    normalized = str(resolved).rstrip("\\/").lower() or str(resolved).lower()

    if resolved == Path(resolved.anchor):
        raise PermissionError("refusing to purge the filesystem root %s" % resolved)

    if normalized in PROTECTED or (normalized + os.sep) in PROTECTED:
        raise PermissionError("refusing to purge the protected system path %s" % resolved)

    depth = len([part for part in resolved.parts if part not in ("/", resolved.anchor)])
    if depth < 2 and not force:
        raise PermissionError(
            "%s is only %d level(s) deep - pass --force if you really mean it"
            % (resolved, depth)
        )


def age_reference(stat_result: os.stat_result, mode: str) -> float:
    if mode == "atime":
        return stat_result.st_atime
    if mode == "ctime":
        return stat_result.st_ctime
    return stat_result.st_mtime


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def matches_any(name: str, relative: str, patterns: Sequence[str]) -> bool:
    posix = relative.replace(os.sep, "/")
    for pattern in patterns:
        normalized = pattern.replace(os.sep, "/")
        if fnmatch.fnmatch(name, normalized) or fnmatch.fnmatch(posix, normalized):
            return True
    return False


def find_stale_files(root: Path, args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Return the metadata of every file eligible for purging."""
    cutoff = time.time() - args.days * 86400
    min_bytes = (args.min_size_mb or 0) * 1024 * 1024
    patterns = args.pattern or DEFAULT_PATTERNS
    excludes = args.exclude or []

    candidates: List[Dict[str, Any]] = []
    scanned = 0

    walker = os.walk(root) if args.recursive else [
        (str(root), [], [p.name for p in root.iterdir() if p.is_file()])
    ]

    for current_root, directories, filenames in walker:
        current_path = Path(current_root)

        if args.recursive and excludes:
            directories[:] = [
                d for d in directories
                if not matches_any(d, str((current_path / d).relative_to(root)), excludes)
            ]

        for filename in filenames:
            file_path = current_path / filename
            scanned += 1
            try:
                relative = str(file_path.relative_to(root))
            except ValueError:
                relative = filename

            if not matches_any(filename, relative, patterns):
                continue
            if excludes and matches_any(filename, relative, excludes):
                LOG.debug("Excluded %s", relative)
                continue

            try:
                if file_path.is_symlink() and not args.follow_symlinks:
                    continue
                stat_result = file_path.stat()
            except OSError as exc:
                LOG.warning("Cannot stat %s (%s) - skipped", file_path, exc)
                continue

            reference = age_reference(stat_result, args.time_field)
            if reference >= cutoff:
                continue
            if stat_result.st_size < min_bytes:
                continue
            if args.max_size_mb and stat_result.st_size > args.max_size_mb * 1024 * 1024:
                continue

            candidates.append({
                "path": str(file_path),
                "relative": relative,
                "folder": str(current_path),
                "name": filename,
                "size_bytes": stat_result.st_size,
                "modified": datetime.fromtimestamp(stat_result.st_mtime)
                            .isoformat(timespec="seconds"),
                "age_days": round((time.time() - reference) / 86400, 1),
                "_sort_time": reference,
            })

    LOG.info("Scanned %d file(s) under %s - %d match the purge criteria",
             scanned, root, len(candidates))
    return candidates


def apply_keep_min(candidates: List[Dict[str, Any]], keep_min: int) -> List[Dict[str, Any]]:
    """Always preserve the newest *keep_min* matching files in each folder."""
    if keep_min <= 0:
        return candidates

    by_folder: Dict[str, List[Dict[str, Any]]] = {}
    for record in candidates:
        by_folder.setdefault(record["folder"], []).append(record)

    kept_out: List[Dict[str, Any]] = []
    for folder, records in by_folder.items():
        records.sort(key=lambda r: r["_sort_time"], reverse=True)
        preserved = records[:keep_min]
        if preserved:
            LOG.info("Keeping the %d newest file(s) in %s", len(preserved), folder)
        kept_out.extend(records[keep_min:])
    return kept_out


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def purge(candidates: List[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    summary = {"deleted": 0, "moved": 0, "failed": 0, "bytes_freed": 0, "skipped": 0}
    move_target = Path(args.move_to).expanduser() if args.move_to else None
    if move_target and args.apply:
        move_target.mkdir(parents=True, exist_ok=True)

    for record in candidates:
        path = Path(record["path"])
        size = record["size_bytes"]

        if not args.apply:
            action = "move" if move_target else "delete"
            LOG.info("[dry-run] would %s %-58s %9s  %5.1f days old",
                     action, record["relative"][:58], human_size(size), record["age_days"])
            summary["skipped"] += 1
            summary["bytes_freed"] += size
            continue

        try:
            if move_target:
                destination = move_target / record["name"]
                if destination.exists():
                    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    destination = move_target / ("%s_%s%s" % (path.stem, stamp, path.suffix))
                shutil.move(str(path), str(destination))
                summary["moved"] += 1
                summary["bytes_freed"] += size
                record["action"] = "moved"
                record["destination"] = str(destination)
                LOG.info("MOVED   %-58s -> %s", record["relative"][:58], destination.name)
            else:
                path.unlink()
                summary["deleted"] += 1
                summary["bytes_freed"] += size
                record["action"] = "deleted"
                LOG.info("DELETED %-58s %9s  %5.1f days old",
                         record["relative"][:58], human_size(size), record["age_days"])
        except PermissionError as exc:
            summary["failed"] += 1
            record["action"] = "error"
            record["error"] = str(exc)
            LOG.error("Permission denied on %s: %s", path, exc)
        except OSError as exc:
            summary["failed"] += 1
            record["action"] = "error"
            record["error"] = str(exc)
            LOG.error("Cannot remove %s: %s", path, exc)

    return summary


def remove_empty_dirs(root: Path, apply_changes: bool) -> int:
    """Bottom-up removal of directories left empty by the purge."""
    removed = 0
    for current_root, directories, filenames in os.walk(root, topdown=False):
        current_path = Path(current_root)
        if current_path == root:
            continue
        try:
            if any(current_path.iterdir()):
                continue
            if apply_changes:
                current_path.rmdir()
                LOG.info("Removed the empty directory %s", current_path)
            else:
                LOG.info("[dry-run] would remove the empty directory %s", current_path)
            removed += 1
        except OSError as exc:
            LOG.debug("Cannot remove %s: %s", current_path, exc)
    return removed


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def write_report(records: List[Dict[str, Any]], summary: Dict[str, Any],
                 output: Path, args: argparse.Namespace) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]

    if output.suffix.lower() == ".csv":
        fieldnames: List[str] = []
        for record in payload:
            for key in record:
                if key not in fieldnames:
                    fieldnames.append(key)
        with output.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(payload)
    else:
        with output.open("w", encoding="utf-8") as fh:
            json.dump({
                "run_at": datetime.now().isoformat(timespec="seconds"),
                "applied": args.apply,
                "paths": args.path,
                "older_than_days": args.days,
                "patterns": args.pattern or DEFAULT_PATTERNS,
                "summary": summary,
                "files": payload,
            }, fh, indent=2, default=str)
    LOG.info("Report written to %s", output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete or archive log/backup files older than N days (dry-run by default).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--path", action="append", required=True,
                        help="Folder to clean (repeatable)")
    parser.add_argument("--days", type=int, required=True,
                        help="Delete files older than this many days")
    parser.add_argument("--pattern", action="append",
                        help="Glob of files to consider (repeatable; defaults to log/backup types)")
    parser.add_argument("--exclude", action="append", help="Glob to never touch (repeatable)")
    parser.add_argument("--min-size-mb", type=float, help="Ignore files smaller than this")
    parser.add_argument("--max-size-mb", type=float, help="Ignore files larger than this")
    parser.add_argument("--keep-min", type=int, default=0,
                        help="Always keep the N newest matching files per folder")
    parser.add_argument("--time-field", choices=["mtime", "atime", "ctime"], default="mtime",
                        help="Timestamp used to compute the age")
    parser.add_argument("--move-to", help="Move files here instead of deleting them")
    parser.add_argument("--remove-empty-dirs", action="store_true",
                        help="Also remove directories left empty")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", default=True,
                        help="Only look at the top level of each path")
    parser.add_argument("--follow-symlinks", action="store_true", help="Consider symlinks")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete/move (without it nothing is changed)")
    parser.add_argument("--force", action="store_true",
                        help="Allow shallow paths that are normally refused")
    parser.add_argument("--report", help="Write a JSON/CSV report to this path")
    parser.add_argument("--log-file", help="Write logs to this file as well")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose, args.log_file)

    if args.days < 0:
        LOG.error("--days must be zero or positive")
        return 2

    if not args.apply:
        LOG.warning("DRY-RUN mode - nothing will be deleted. Add --apply to act.")

    try:
        roots: List[Path] = []
        for raw in args.path:
            path = Path(raw).expanduser()
            if not path.is_dir():
                LOG.error("Not a directory: %s", path)
                continue
            try:
                assert_safe_target(path, args.force)
            except PermissionError as exc:
                LOG.error("%s", exc)
                continue
            roots.append(path.resolve())

        if not roots:
            LOG.error("No usable path to clean")
            return 2

        cutoff_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M")
        LOG.info("Purging files older than %d day(s) (before %s) in %d path(s)",
                 args.days, cutoff_date, len(roots))
        LOG.info("Patterns: %s", ", ".join(args.pattern or DEFAULT_PATTERNS))

        all_records: List[Dict[str, Any]] = []
        totals = {"deleted": 0, "moved": 0, "failed": 0, "bytes_freed": 0, "skipped": 0}

        for root in roots:
            LOG.info("-" * 66)
            LOG.info("Scanning %s", root)
            candidates = find_stale_files(root, args)
            candidates = apply_keep_min(candidates, args.keep_min)

            if not candidates:
                LOG.info("Nothing to purge in %s", root)
                continue

            total_bytes = sum(record["size_bytes"] for record in candidates)
            LOG.info("%d file(s) selected, %s reclaimable",
                     len(candidates), human_size(total_bytes))

            summary = purge(candidates, args)
            for key in totals:
                totals[key] += summary.get(key, 0)
            all_records.extend(candidates)

            if args.remove_empty_dirs:
                removed = remove_empty_dirs(root, args.apply)
                LOG.info("%d empty director%s %s",
                         removed, "y" if removed == 1 else "ies",
                         "removed" if args.apply else "would be removed")

        LOG.info("=" * 66)
        if args.apply:
            LOG.info("Purge complete - %d deleted, %d moved, %d failed, %s freed",
                     totals["deleted"], totals["moved"], totals["failed"],
                     human_size(totals["bytes_freed"]))
        else:
            LOG.info("DRY-RUN summary - %d file(s) would be removed, %s would be freed",
                     totals["skipped"], human_size(totals["bytes_freed"]))
            LOG.info("Re-run with --apply to perform the purge.")
        LOG.info("=" * 66)

        if args.report:
            write_report(all_records, totals, Path(args.report).expanduser(), args)

        return 0 if totals["failed"] == 0 else 1

    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        return 130
    except Exception as exc:
        LOG.error("Purge failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
