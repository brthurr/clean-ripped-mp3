#!/usr/bin/env python3
"""
Detect corrupted MP3 files ripped from CD.

Uses multiple heuristics:
  - mutagen parse failure
  - zero or suspiciously short duration
  - MP3 frame-level scanning (sync byte ratio, invalid headers)
  - file truncation detection
  - frame count vs reported duration mismatch

Usage:
    python detect_corrupted.py /path/to/music [options]
"""

import argparse
import os
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from mutagen.mp3 import MP3
    from mutagen import MutagenError
except ImportError:
    print("Error: mutagen is required.  Run: pip install mutagen")
    sys.exit(1)


# ---------------------------------------------------------------------------
# MP3 frame parsing tables
# ---------------------------------------------------------------------------

# Bitrates in kbps indexed by [mpeg_version][layer][bitrate_index]
# mpeg_version: 1=MPEG1, 2=MPEG2, 3=MPEG2.5
# layer: 1=LayerI, 2=LayerII, 3=LayerIII (MP3)
_BITRATE = {
    (1, 1): [0,32,64,96,128,160,192,224,256,288,320,352,384,416,448],
    (1, 2): [0,32,48,56, 64, 80, 96,112,128,160,192,224,256,320,384],
    (1, 3): [0,32,40,48, 56, 64, 80, 96,112,128,160,192,224,256,320],
    (2, 1): [0,32,48,56, 64, 80, 96,112,128,144,160,176,192,224,256],
    (2, 2): [0, 8,16,24, 32, 40, 48, 56, 64, 80, 96,112,128,144,160],
    (2, 3): [0, 8,16,24, 32, 40, 48, 56, 64, 80, 96,112,128,144,160],
    (3, 1): [0,32,48,56, 64, 80, 96,112,128,144,160,176,192,224,256],
    (3, 2): [0, 8,16,24, 32, 40, 48, 56, 64, 80, 96,112,128,144,160],
    (3, 3): [0, 8,16,24, 32, 40, 48, 56, 64, 80, 96,112,128,144,160],
}

_SAMPLE_RATE = {
    1: [44100, 48000, 32000],
    2: [22050, 24000, 16000],
    3: [11025, 12000,  8000],
}

_MPEG_VER_BITS = {0b00: 3, 0b10: 2, 0b11: 1}   # bit pattern -> version id
_LAYER_BITS    = {0b01: 3, 0b10: 2, 0b11: 1}   # bit pattern -> layer id


def _parse_frame_header(data: bytes, offset: int) -> Optional[int]:
    """
    Try to parse an MP3 frame at *offset*.  Returns frame size in bytes, or
    None if the header is invalid.
    """
    if offset + 4 > len(data):
        return None

    b0, b1, b2, _ = data[offset:offset + 4]

    # Sync word: first 11 bits must be set
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None

    ver_bits  = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    br_idx    = (b2 >> 4) & 0x0F
    sr_idx    = (b2 >> 2) & 0x03
    padding   = (b2 >> 1) & 0x01

    mpeg_ver = _MPEG_VER_BITS.get(ver_bits)
    layer    = _LAYER_BITS.get(layer_bits)

    if mpeg_ver is None or layer is None:
        return None
    if br_idx == 0 or br_idx == 15:   # free / bad bitrate
        return None
    if sr_idx == 3:                    # reserved sample rate
        return None

    bitrates = _BITRATE.get((mpeg_ver, layer))
    if bitrates is None:
        return None

    bitrate    = bitrates[br_idx] * 1000   # bps
    sample_rate = _SAMPLE_RATE[mpeg_ver][sr_idx]

    if layer == 1:
        frame_size = (12 * bitrate // sample_rate + padding) * 4
    elif layer == 3 and mpeg_ver != 1:
        # MPEG2/2.5 Layer III: 576 samples/frame  → coefficient 72
        frame_size = 72 * bitrate // sample_rate + padding
    else:
        # MPEG1 Layer II/III and MPEG2 Layer II: 1152 samples/frame → coefficient 144
        frame_size = 144 * bitrate // sample_rate + padding

    if frame_size < 21:   # sanity: no real MP3 frame is this small
        return None

    return frame_size


def _skip_id3v2(data: bytes) -> int:
    """Return the byte offset after any ID3v2 tag at the start of the file."""
    if not data.startswith(b"ID3"):
        return 0
    if len(data) < 10:
        return 0
    flags = data[5]
    # Size is a synchsafe integer (4 x 7-bit bytes)
    raw = struct.unpack(">I", data[6:10])[0]
    size = ((raw & 0x7F000000) >> 3 |
            (raw & 0x007F0000) >> 2 |
            (raw & 0x00007F00) >> 1 |
            (raw & 0x0000007F))
    footer = 10 if (flags & 0x10) else 0
    return 10 + size + footer


# ---------------------------------------------------------------------------
# Corruption checks
# ---------------------------------------------------------------------------

MIN_FILE_BYTES  = 8_192    # 8 KB – anything smaller is almost certainly junk
MIN_DURATION_S  = 2.0      # seconds – shorter than this is suspicious
FRAME_SCAN_BYTES = 512_000 # how many bytes to scan for frame analysis (512 KB)
MAX_SEEK_BYTES   = 2048    # max bytes to scan while hunting for next sync


@dataclass
class CheckResult:
    flags: list[str] = field(default_factory=list)
    score: float = 0.0   # 0.0 = clean, 1.0 = definitely corrupt
    details: dict = field(default_factory=dict)

    def add(self, flag: str, weight: float, detail: str = ""):
        self.flags.append(flag)
        self.score = min(1.0, self.score + weight)
        if detail:
            self.details[flag] = detail


def check_file(path: Path) -> CheckResult:
    result = CheckResult()

    # --- 1. Basic file size ---
    size = path.stat().st_size
    result.details["file_size"] = size
    if size < MIN_FILE_BYTES:
        result.add("tiny_file", 0.5,
                   f"{size} bytes (threshold {MIN_FILE_BYTES})")

    # --- 2. Mutagen parse ---
    reported_duration = 0.0
    try:
        audio = MP3(path)
        reported_duration = audio.info.length
        result.details["reported_duration"] = round(reported_duration, 2)
        result.details["bitrate"] = getattr(audio.info, "bitrate", "?")
        if reported_duration <= 0:
            result.add("zero_duration", 0.5, "mutagen reports 0 s duration")
        elif reported_duration < MIN_DURATION_S:
            result.add("very_short", 0.2,
                       f"{reported_duration:.1f} s (threshold {MIN_DURATION_S} s)")
    except MutagenError as exc:
        result.add("mutagen_error", 0.6, str(exc))

    # --- 3. Frame-level scan ---
    try:
        with open(path, "rb") as fh:
            raw = fh.read(FRAME_SCAN_BYTES)

        offset = _skip_id3v2(raw)
        data_len = len(raw)

        valid_frames   = 0
        invalid_bytes  = 0
        frame_samples  = 0
        first_sr       = None
        inconsistent_sr = False
        consecutive_bad = 0
        max_consecutive_bad = 0

        while offset < data_len - 4:
            frame_size = _parse_frame_header(raw, offset)

            if frame_size is not None and (offset + frame_size) <= data_len:
                # Optionally verify next-frame sync (lookahead)
                next_off = offset + frame_size
                if next_off + 4 <= data_len:
                    next_ok = (_parse_frame_header(raw, next_off) is not None
                               or next_off >= data_len - 4)
                else:
                    next_ok = True

                if next_ok:
                    valid_frames += 1
                    consecutive_bad = 0
                    # Collect sample rate for consistency
                    sr_idx  = (raw[offset + 2] >> 2) & 0x03
                    ver_bits = (raw[offset + 1] >> 3) & 0x03
                    mpeg_ver = _MPEG_VER_BITS.get(ver_bits)
                    if mpeg_ver and sr_idx < 3:
                        sr = _SAMPLE_RATE[mpeg_ver][sr_idx]
                        if first_sr is None:
                            first_sr = sr
                        elif sr != first_sr:
                            inconsistent_sr = True
                    # Estimate samples per frame (Layer III / MPEG1 = 1152)
                    frame_samples += 1152
                    offset += frame_size
                    continue

            # Invalid byte – advance one byte and count as junk
            invalid_bytes += 1
            consecutive_bad += 1
            max_consecutive_bad = max(max_consecutive_bad, consecutive_bad)
            offset += 1

        result.details["valid_frames"] = valid_frames
        result.details["invalid_bytes"] = invalid_bytes
        result.details["max_consecutive_bad_bytes"] = max_consecutive_bad

        total_scanned = data_len - _skip_id3v2(raw)
        if total_scanned > 0:
            junk_ratio = invalid_bytes / total_scanned
            result.details["junk_ratio"] = round(junk_ratio, 4)
            if valid_frames == 0:
                result.add("no_valid_frames", 0.7,
                           "no valid MP3 frames found in scanned region")
            elif junk_ratio > 0.30:
                result.add("high_junk_ratio", 0.4,
                           f"{junk_ratio:.1%} of scanned bytes are not in valid frames")
            elif junk_ratio > 0.10:
                result.add("moderate_junk_ratio", 0.2,
                           f"{junk_ratio:.1%} of scanned bytes are not in valid frames")

        if inconsistent_sr:
            result.add("inconsistent_sample_rate", 0.15,
                       "sample rate changes mid-file (unusual)")

        # --- 4. Duration consistency ---
        if reported_duration > 0 and frame_samples > 0:
            # frame_samples counted over FRAME_SCAN_BYTES; estimate full count
            scan_fraction = min(1.0, FRAME_SCAN_BYTES / size)
            estimated_frames_total = valid_frames / scan_fraction if scan_fraction > 0 else valid_frames
            estimated_duration = (estimated_frames_total * 1152) / 44100
            ratio = abs(estimated_duration - reported_duration) / max(reported_duration, 1)
            result.details["estimated_duration"] = round(estimated_duration, 2)
            if ratio > 0.60:
                result.add("duration_mismatch", 0.25,
                           f"frame-estimated {estimated_duration:.1f} s vs "
                           f"reported {reported_duration:.1f} s ({ratio:.0%} off)")

        # --- 5. Truncation check ---
        # Read the last few bytes to see if file ends with a valid frame or garbage
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 256))
            tail = fh.read(256)
        # Look for a sync byte in the tail; absence suggests truncation
        has_tail_sync = any(
            tail[i] == 0xFF and (tail[i + 1] & 0xE0) == 0xE0
            for i in range(len(tail) - 1)
        )
        if not has_tail_sync and size >= MIN_FILE_BYTES:
            result.add("truncated", 0.25,
                       "no valid sync bytes in last 256 bytes of file")

    except OSError as exc:
        result.add("read_error", 0.8, str(exc))

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_mp3s(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return sorted(root.rglob("*.mp3")) + sorted(root.rglob("*.MP3"))
    return sorted(root.glob("*.mp3")) + sorted(root.glob("*.MP3"))


def format_score(score: float) -> str:
    if score >= 0.6:
        return f"\033[91m{score:.2f}\033[0m"   # red
    if score >= 0.3:
        return f"\033[93m{score:.2f}\033[0m"   # yellow
    return f"\033[92m{score:.2f}\033[0m"        # green


def main():
    parser = argparse.ArgumentParser(
        description="Detect corrupted MP3 files ripped from CD.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Actions (choose one, or omit for report-only):
  --move DIR    Move suspected-corrupt files into DIR
  --delete      Delete suspected-corrupt files (IRREVERSIBLE)

Examples:
  # Just report
  python detect_corrupted.py /media/usb/music

  # Report and move bad files to a quarantine folder
  python detect_corrupted.py /media/usb/music --move /tmp/corrupted

  # Recursive scan, higher sensitivity
  python detect_corrupted.py /media/usb/music -r --threshold 0.3
""")
    parser.add_argument("directory",
                        help="Directory to scan for MP3 files")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Scan subdirectories recursively")
    parser.add_argument("--threshold", type=float, default=0.40,
                        help="Corruption score threshold (0.0–1.0, default 0.40). "
                             "Lower = more sensitive.")
    parser.add_argument("--move", metavar="DEST_DIR",
                        help="Move files above threshold to DEST_DIR")
    parser.add_argument("--delete", action="store_true",
                        help="Delete files above threshold")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show details for every file, not just flagged ones")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color output")
    args = parser.parse_args()

    if args.move and args.delete:
        parser.error("--move and --delete are mutually exclusive")

    if args.no_color:
        global format_score
        format_score = lambda s: f"{s:.2f}"   # noqa: E731

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        print(f"Error: '{root}' is not a directory", file=sys.stderr)
        sys.exit(1)

    dest_dir: Optional[Path] = None
    if args.move:
        dest_dir = Path(args.move).expanduser().resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)

    mp3s = find_mp3s(root, args.recursive)
    if not mp3s:
        print(f"No MP3 files found in '{root}'")
        sys.exit(0)

    print(f"Scanning {len(mp3s)} MP3 file(s) in '{root}' …\n")

    corrupted: list[tuple[Path, CheckResult]] = []
    clean_count = 0

    for i, mp3 in enumerate(mp3s, 1):
        result = check_file(mp3)
        rel = mp3.relative_to(root)

        if result.score >= args.threshold:
            corrupted.append((mp3, result))
            score_str = format_score(result.score)
            print(f"[{i:>4}/{len(mp3s)}] SUSPECT  {score_str}  {rel}")
            for flag, detail in result.details.items():
                if flag not in ("file_size", "bitrate", "reported_duration",
                                "valid_frames", "invalid_bytes",
                                "max_consecutive_bad_bytes", "junk_ratio",
                                "estimated_duration"):
                    continue
            for flag in result.flags:
                detail = result.details.get(flag, "")
                detail_str = f" – {detail}" if detail else ""
                print(f"            • {flag}{detail_str}")
        else:
            clean_count += 1
            if args.verbose:
                score_str = format_score(result.score)
                print(f"[{i:>4}/{len(mp3s)}] ok       {score_str}  {rel}")

    # Summary
    print(f"\n{'─'*60}")
    print(f"Results: {len(corrupted)} suspect / {clean_count} clean / {len(mp3s)} total")
    print(f"Threshold: {args.threshold:.2f}")

    if not corrupted:
        print("No corrupted files found.")
        return

    # Action
    if args.delete:
        print(f"\nDeleting {len(corrupted)} file(s) …")
        for path, _ in corrupted:
            try:
                path.unlink()
                print(f"  deleted: {path.name}")
            except OSError as exc:
                print(f"  ERROR deleting {path.name}: {exc}", file=sys.stderr)
    elif dest_dir:
        print(f"\nMoving {len(corrupted)} file(s) to '{dest_dir}' …")
        for path, _ in corrupted:
            dest = dest_dir / path.name
            # Avoid overwriting if two files share a name
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                counter = 1
                while dest.exists():
                    dest = dest_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
            try:
                shutil.move(str(path), str(dest))
                print(f"  moved: {path.name}  →  {dest}")
            except OSError as exc:
                print(f"  ERROR moving {path.name}: {exc}", file=sys.stderr)
    else:
        print("\nRe-run with --move <dir> to quarantine, or --delete to remove.")

    # Write a log file regardless
    log_path = root / "corrupted_files.log"
    with open(log_path, "w") as fh:
        fh.write("# Suspected corrupted MP3 files\n")
        fh.write(f"# Scanned: {root}\n")
        fh.write(f"# Threshold: {args.threshold:.2f}\n\n")
        for path, result in corrupted:
            fh.write(f"{path}\n")
            fh.write(f"  score: {result.score:.2f}\n")
            fh.write(f"  flags: {', '.join(result.flags)}\n")
            for k, v in result.details.items():
                fh.write(f"  {k}: {v}\n")
            fh.write("\n")
    print(f"\nLog written to: {log_path}")


if __name__ == "__main__":
    main()
