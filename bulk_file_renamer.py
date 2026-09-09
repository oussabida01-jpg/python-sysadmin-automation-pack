#!/usr/bin/env python3
"""
bulk_file_renamer.py
====================
Batch-rename files with prefixes, suffixes, regex substitution, sequential
numbering, case conversion, date stamps, slugification and EXIF/mtime-based
naming - dry-run by default, with collision detection and an undo script.

  * --dry-run is the DEFAULT; add --apply to actually rename
  * detects target collisions before touching anything
  * writes an undo CSV/shell script so any run can be reverted
  * transformations are applied in a documented, predictable order

Examples
--------
    python bulk_file_renamer.py -p ./photos --prefix "holiday_" --apply
    python bulk_file_renamer.py -p ./docs --regex "\\s+" --replace "_" --apply
    python bulk_file_renamer.py -p ./scans --number --number-start 1 --number-digits 3 \\
        --prefix "invoice_" --apply
    python bulk_file_renamer.py -p ./media --lowercase --slugify --pattern "*.MP4"
    python bulk_file_renamer.py -p ./logs --date-prefix --date-format "%Y-%m-%d" --apply
    python bulk_file_renamer.py -p ./files --undo-file undo.csv --undo
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

LOG = logging.getLogger("bulk_renamer")

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS = {
    "con", "prn", "aux", "nul",
    *("com%d" % i for i in range(1, 10)),
    *("lpt%d" % i for i in range(1, 10)),
}


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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    """'Rapport Financier (2024).pdf' -> 'rapport-financier-2024'."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^\w\s-]", "", ascii_text).strip().lower()
    return re.sub(r"[\s_]+", "-", ascii_text).strip("-") or "file"


def sanitize(name: str) -> str:
    """Strip characters no filesystem will accept, and dodge Windows reserved names."""
    cleaned = INVALID_CHARS.sub("_", name).strip(" .")
    if cleaned.split(".")[0].lower() in RESERVED_WINDOWS:
        cleaned = "_" + cleaned
    return cleaned or "unnamed"


def collect_files(root: Path, pattern: str, recursive: bool,
                  extensions: Sequence[str] | None,
                  exclude: Sequence[str] | None,
                  include_dirs: bool) -> List[Path]:
    globber = root.rglob("*") if recursive else root.glob("*")
    wanted = {("." + e.lstrip(".")).lower() for e in (extensions or [])}

    files: List[Path] = []
    for path in globber:
        try:
            if path.is_dir() and not include_dirs:
                continue
            if not path.is_dir() and not path.is_file():
                continue
            if not fnmatch.fnmatch(path.name, pattern):
                continue
            if wanted and path.suffix.lower() not in wanted:
                continue
            if exclude and any(fnmatch.fnmatch(path.name, e) for e in exclude):
                continue
            files.append(path)
        except OSError as exc:
            LOG.warning("Skipping %s (%s)", path, exc)

    return files


def sort_files(files: List[Path], sort_by: str) -> List[Path]:
    try:
        if sort_by == "name":
            return sorted(files, key=lambda p: p.name.lower())
        if sort_by == "mtime":
            return sorted(files, key=lambda p: p.stat().st_mtime)
        if sort_by == "size":
            return sorted(files, key=lambda p: p.stat().st_size)
        if sort_by == "natural":
            def natural_key(path: Path):
                return [int(part) if part.isdigit() else part.lower()
                        for part in re.split(r"(\d+)", path.name)]
            return sorted(files, key=natural_key)
    except OSError as exc:
        LOG.warning("Sorting fell back to unsorted order (%s)", exc)
    return files


def exif_datetime(path: Path) -> datetime | None:
    """Read the EXIF capture time when Pillow is available."""
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            lookup = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            for key in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
                raw = lookup.get(key)
                if raw:
                    return datetime.strptime(str(raw)[:19], "%Y:%m:%d %H:%M:%S")
    except Exception as exc:
        LOG.debug("No EXIF date in %s (%s)", path.name, exc)
    return None


# --------------------------------------------------------------------------- #
# Name transformation
# --------------------------------------------------------------------------- #
def build_new_name(path: Path, index: int, args: argparse.Namespace) -> str:
    """Apply every requested transformation in a fixed, predictable order."""
    stem = path.stem if path.is_file() else path.name
    suffix = path.suffix if path.is_file() else ""

    # 1. regex substitution
    if args.regex:
        try:
            stem = re.sub(args.regex, args.replace or "", stem,
                          flags=0 if args.case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ValueError("invalid --regex: %s" % exc) from exc

    # 2. plain find/replace
    if args.find:
        stem = stem.replace(args.find, args.replace_text or "")

    # 3. strip characters / normalise separators
    if args.strip_chars:
        for char in args.strip_chars:
            stem = stem.replace(char, "")
    if args.spaces_to:
        stem = re.sub(r"\s+", args.spaces_to, stem)
    if args.collapse:
        stem = re.sub(r"[_\-\s]{2,}", args.spaces_to or "_", stem).strip("_- ")

    # 4. case
    if args.lowercase:
        stem = stem.lower()
    elif args.uppercase:
        stem = stem.upper()
    elif args.titlecase:
        stem = stem.title()
    elif args.capitalize:
        stem = stem.capitalize()

    # 5. slugify
    if args.slugify:
        stem = slugify(stem)

    # 6. truncate
    if args.max_length and len(stem) > args.max_length:
        stem = stem[:args.max_length].rstrip("_- ")

    # 7. date stamp
    if args.date_prefix or args.date_suffix:
        stamp_source = None
        if args.date_source == "exif":
            stamp_source = exif_datetime(path)
        if stamp_source is None:
            try:
                stat = path.stat()
                stamp_source = datetime.fromtimestamp(
                    stat.st_ctime if args.date_source == "created" else stat.st_mtime)
            except OSError:
                stamp_source = datetime.now()
        stamp = stamp_source.strftime(args.date_format)
        if args.date_prefix:
            stem = "%s%s%s" % (stamp, args.separator, stem)
        if args.date_suffix:
            stem = "%s%s%s" % (stem, args.separator, stamp)

    # 8. sequential number
    if args.number:
        number = args.number_start + index * args.number_step
        formatted = str(number).zfill(args.number_digits)
        if args.number_position == "prefix":
            stem = "%s%s%s" % (formatted, args.separator, stem)
        else:
            stem = "%s%s%s" % (stem, args.separator, formatted)

    # 9. prefix / suffix
    if args.prefix:
        stem = args.prefix + stem
    if args.suffix:
        stem = stem + args.suffix

    # 10. extension handling
    if args.new_extension:
        suffix = "." + args.new_extension.lstrip(".")
    if args.lowercase_extension:
        suffix = suffix.lower()

    return sanitize(stem + suffix)


def plan_renames(files: List[Path], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Compute every rename and detect collisions before doing anything."""
    plan: List[Dict[str, Any]] = []
    problems: List[str] = []
    claimed: Dict[str, Path] = {}

    for index, path in enumerate(files):
        try:
            new_name = build_new_name(path, index, args)
        except ValueError as exc:
            problems.append("%s: %s" % (path.name, exc))
            continue

        destination = (Path(args.move_to).expanduser() / new_name
                       if args.move_to else path.with_name(new_name))

        if destination == path:
            LOG.debug("No change for %s", path.name)
            continue

        key = str(destination).lower() if os.name == "nt" else str(destination)
        if key in claimed:
            problems.append("collision: %s and %s both map to %s"
                            % (path.name, claimed[key].name, new_name))
            continue
        if destination.exists() and not args.overwrite:
            problems.append("target already exists: %s" % destination)
            continue

        claimed[key] = path
        plan.append({
            "source": str(path),
            "destination": str(destination),
            "old_name": path.name,
            "new_name": new_name,
        })

    return plan, problems


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def execute(plan: List[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, int]:
    summary = {"renamed": 0, "failed": 0, "skipped": 0}

    if args.move_to and args.apply:
        Path(args.move_to).expanduser().mkdir(parents=True, exist_ok=True)

    for entry in plan:
        source = Path(entry["source"])
        destination = Path(entry["destination"])

        if not args.apply:
            LOG.info("[dry-run] %-46s -> %s", entry["old_name"][:46], entry["new_name"])
            summary["skipped"] += 1
            continue

        try:
            if destination.exists() and args.overwrite:
                LOG.warning("Overwriting %s", destination.name)
            if args.overwrite:
                source.replace(destination)
            else:
                source.rename(destination)
            summary["renamed"] += 1
            LOG.info("RENAMED  %-46s -> %s", entry["old_name"][:46], entry["new_name"])
        except FileExistsError:
            summary["failed"] += 1
            LOG.error("%s already exists - skipped", destination)
        except PermissionError as exc:
            summary["failed"] += 1
            LOG.error("Permission denied renaming %s (%s)", source.name, exc)
        except OSError as exc:
            summary["failed"] += 1
            LOG.error("Cannot rename %s: %s", source.name, exc)

    return summary


def write_undo(plan: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["current_path", "restore_to"])
        for entry in plan:
            writer.writerow([entry["destination"], entry["source"]])
    LOG.info("Undo file written to %s (run with --undo to revert)", path)


def run_undo(path: Path, apply_changes: bool) -> int:
    """Revert a previous run using its undo CSV."""
    if not path.is_file():
        LOG.error("Undo file not found: %s", path)
        return 2

    restored = failed = 0
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    LOG.info("Reverting %d rename(s) from %s", len(rows), path.name)
    for row in reversed(rows):
        current = Path(row["current_path"])
        original = Path(row["restore_to"])
        if not current.exists():
            LOG.warning("%s no longer exists - skipped", current)
            continue
        if not apply_changes:
            LOG.info("[dry-run] would restore %s -> %s", current.name, original.name)
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            current.rename(original)
            restored += 1
            LOG.info("RESTORED %-46s -> %s", current.name[:46], original.name)
        except OSError as exc:
            failed += 1
            LOG.error("Cannot restore %s: %s", current, exc)

    LOG.info("Undo complete - %d restored, %d failed", restored, failed)
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch rename files with regex, prefix/suffix and numbering rules.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Transformations run in this order: regex, find/replace, strip, case, "
               "slugify, truncate, date, number, prefix/suffix, extension.",
    )
    parser.add_argument("-p", "--path", default=".", help="Folder containing the files")
    parser.add_argument("--pattern", default="*", help="Filename glob to select files")
    parser.add_argument("--extensions", nargs="*", help="Only these extensions, e.g. jpg png")
    parser.add_argument("--exclude", action="append", help="Glob to skip (repeatable)")
    parser.add_argument("-r", "--recursive", action="store_true", help="Recurse into sub-folders")
    parser.add_argument("--include-dirs", action="store_true", help="Also rename directories")
    parser.add_argument("--sort", dest="sort_by",
                        choices=["name", "natural", "mtime", "size", "none"], default="natural",
                        help="Order used for sequential numbering")

    parser.add_argument("--prefix", help="Text inserted before the name")
    parser.add_argument("--suffix", help="Text appended after the name")
    parser.add_argument("--regex", help="Regex searched in the stem")
    parser.add_argument("--replace", help="Replacement for --regex (supports \\1 groups)")
    parser.add_argument("--find", help="Literal substring to replace")
    parser.add_argument("--replace-text", help="Replacement for --find")
    parser.add_argument("--strip-chars", help="Characters removed from the name")
    parser.add_argument("--spaces-to", help="Replace whitespace runs with this string")
    parser.add_argument("--collapse", action="store_true",
                        help="Collapse repeated separators")
    parser.add_argument("--case-sensitive", action="store_true",
                        help="Make --regex case sensitive")

    parser.add_argument("--lowercase", action="store_true", help="lower case the name")
    parser.add_argument("--uppercase", action="store_true", help="UPPER CASE the name")
    parser.add_argument("--titlecase", action="store_true", help="Title Case The Name")
    parser.add_argument("--capitalize", action="store_true", help="Capitalize the name")
    parser.add_argument("--slugify", action="store_true", help="ASCII kebab-case the name")
    parser.add_argument("--max-length", type=int, help="Truncate the stem to N characters")

    parser.add_argument("--number", action="store_true", help="Add a sequential number")
    parser.add_argument("--number-start", type=int, default=1, help="First number")
    parser.add_argument("--number-step", type=int, default=1, help="Increment")
    parser.add_argument("--number-digits", type=int, default=3, help="Zero padding width")
    parser.add_argument("--number-position", choices=["prefix", "suffix"], default="prefix",
                        help="Where the number goes")

    parser.add_argument("--date-prefix", action="store_true", help="Prepend a date stamp")
    parser.add_argument("--date-suffix", action="store_true", help="Append a date stamp")
    parser.add_argument("--date-format", default="%Y%m%d", help="strftime pattern")
    parser.add_argument("--date-source", choices=["modified", "created", "exif"],
                        default="modified", help="Where the date comes from")

    parser.add_argument("--new-extension", help="Force this extension")
    parser.add_argument("--lowercase-extension", action="store_true",
                        help="Lower-case the extension")
    parser.add_argument("--separator", default="_", help="Separator inserted by date/number")
    parser.add_argument("--move-to", help="Also move the renamed files into this folder")

    parser.add_argument("--apply", action="store_true",
                        help="Actually rename (dry-run without it)")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting targets")
    parser.add_argument("--undo-file", default="rename_undo.csv",
                        help="Where the undo mapping is written/read")
    parser.add_argument("--undo", action="store_true", help="Revert a previous run")
    parser.add_argument("--log-file", help="Write logs to this file as well")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose, args.log_file)

    try:
        if args.undo:
            return run_undo(Path(args.undo_file).expanduser(), args.apply)

        root = Path(args.path).expanduser().resolve()
        if not root.is_dir():
            LOG.error("Not a directory: %s", root)
            return 2

        transformations = any([
            args.prefix, args.suffix, args.regex, args.find, args.strip_chars,
            args.spaces_to, args.collapse, args.lowercase, args.uppercase,
            args.titlecase, args.capitalize, args.slugify, args.max_length,
            args.number, args.date_prefix, args.date_suffix, args.new_extension,
            args.lowercase_extension,
        ])
        if not transformations:
            LOG.error("No transformation requested - see --help for the available rules")
            return 2

        if not args.apply:
            LOG.warning("DRY-RUN mode - nothing will be renamed. Add --apply to act.")

        files = collect_files(root, args.pattern, args.recursive,
                              args.extensions, args.exclude, args.include_dirs)
        if not files:
            LOG.error("No file matching %r found in %s", args.pattern, root)
            return 1

        files = sort_files(files, args.sort_by)
        LOG.info("Found %d file(s) in %s (order: %s)", len(files), root, args.sort_by)

        plan, problems = plan_renames(files, args)

        for problem in problems:
            LOG.error("%s", problem)
        if problems and not args.overwrite:
            LOG.error("%d problem(s) detected - resolve them or pass --overwrite. "
                      "Nothing was renamed.", len(problems))
            return 1

        if not plan:
            LOG.info("Every file already has the desired name - nothing to do")
            return 0

        LOG.info("-" * 66)
        summary = execute(plan, args)
        LOG.info("-" * 66)

        if args.apply and summary["renamed"]:
            write_undo([e for e in plan if Path(e["destination"]).exists()],
                       Path(args.undo_file).expanduser())
            LOG.info("Rename complete - %d renamed, %d failed",
                     summary["renamed"], summary["failed"])
        else:
            LOG.info("DRY-RUN summary - %d file(s) would be renamed", summary["skipped"])
            LOG.info("Re-run with --apply to perform the renames.")

        return 0 if summary["failed"] == 0 else 1

    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        return 130
    except Exception as exc:
        LOG.error("Rename run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
