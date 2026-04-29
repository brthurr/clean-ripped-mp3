#!/usr/bin/env python3
"""
Restore quarantined MP3 files back to their original locations.

Reads the corrupted_files.log produced by detect_corrupted.py and moves
files from the quarantine directory back to where they came from.

Usage:
    # Restore everything in the quarantine
    python restore_quarantine.py --log /mnt/media/music/corrupted_files.log \
                                 --quarantine /home/shawn/quarantine

    # Restore only files that scored below 0.55 (likely false positives)
    python restore_quarantine.py --log /mnt/media/music/corrupted_files.log \
                                 --quarantine /home/shawn/quarantine \
                                 --below 0.55
"""

import argparse
import shutil
import sys
from pathlib import Path


def parse_log(log_path: Path) -> list[tuple[Path, float]]:
    """
    Parse corrupted_files.log and return (original_path, score) pairs.
    """
    entries = []
    current_path = None
    current_score = None

    with open(log_path) as fh:
        for line in fh:
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("  score:"):
                current_score = float(line.split(":", 1)[1].strip())
                if current_path is not None and current_score is not None:
                    entries.append((current_path, current_score))
                    current_path = None
                    current_score = None
            elif not line.startswith(" "):
                # Unindented non-comment line is a file path
                current_path = Path(line.strip())

    return entries


def main():
    parser = argparse.ArgumentParser(
        description="Restore quarantined MP3 files to their original locations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Restore all quarantined files
  python restore_quarantine.py --log /mnt/media/music/corrupted_files.log \\
                               --quarantine /home/shawn/quarantine

  # Restore only files that scored below 0.55 (likely false positives)
  python restore_quarantine.py --log /mnt/media/music/corrupted_files.log \\
                               --quarantine /home/shawn/quarantine \\
                               --below 0.55
""")
    parser.add_argument("--log", required=True, metavar="LOG_FILE",
                        help="Path to corrupted_files.log from detect_corrupted.py")
    parser.add_argument("--quarantine", required=True, metavar="DIR",
                        help="Quarantine directory where files were moved")
    parser.add_argument("--below", type=float, metavar="SCORE",
                        help="Only restore files with a score below this value "
                             "(e.g. 0.55 restores likely false positives)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be restored without moving anything")
    args = parser.parse_args()

    log_path = Path(args.log).expanduser().resolve()
    quarantine_dir = Path(args.quarantine).expanduser().resolve()

    if not log_path.exists():
        print(f"Error: log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    if not quarantine_dir.is_dir():
        print(f"Error: quarantine directory not found: {quarantine_dir}", file=sys.stderr)
        sys.exit(1)

    entries = parse_log(log_path)
    if not entries:
        print("No entries found in log file.")
        sys.exit(0)

    # Filter by score threshold if requested
    if args.below is not None:
        candidates = [(p, s) for p, s in entries if s < args.below]
        print(f"Found {len(entries)} total log entries, "
              f"{len(candidates)} below score {args.below:.2f}")
    else:
        candidates = entries
        print(f"Found {len(entries)} entries in log")

    if not candidates:
        print("Nothing to restore.")
        return

    if args.dry_run:
        print("\n--- DRY RUN (no files will be moved) ---\n")

    restored = 0
    skipped  = 0
    missing  = 0

    for original_path, score in candidates:
        filename = original_path.name
        quarantine_file = quarantine_dir / filename

        if not quarantine_file.exists():
            print(f"  NOT IN QUARANTINE ({score:.2f})  {filename}")
            missing += 1
            continue

        if original_path.exists():
            print(f"  ALREADY EXISTS    ({score:.2f})  {filename} — skipping")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  would restore  ({score:.2f})  {filename}")
            print(f"                 → {original_path}")
            restored += 1
            continue

        # Recreate parent directory if needed
        original_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.move(str(quarantine_file), str(original_path))
            print(f"  restored  ({score:.2f})  {filename}")
            print(f"            → {original_path}")
            restored += 1
        except OSError as exc:
            print(f"  ERROR moving {filename}: {exc}", file=sys.stderr)
            skipped += 1

    print(f"\n{'─'*60}")
    if args.dry_run:
        print(f"Dry run: {restored} would be restored, "
              f"{skipped} skipped, {missing} not found in quarantine")
    else:
        print(f"Restored: {restored}  |  Skipped: {skipped}  |  "
              f"Not in quarantine: {missing}")


if __name__ == "__main__":
    main()
