# clean-ripped-mp3

Scans a directory of MP3 files and identifies corrupted ones — particularly useful for CD rips where bad sectors, read errors, or interrupted rips leave garbled or truncated audio files.

## How it works

Each file is scored from 0.0 (clean) to 1.0 (definitely corrupt) using multiple checks:

| Check | What it catches |
|---|---|
| mutagen parse failure | completely unreadable files |
| no valid MP3 frames | random/garbled byte streams |
| high junk byte ratio (>30%) | partially garbled rips |
| moderate junk byte ratio (>10%) | lightly corrupted sections |
| zero duration | empty or headerless files |
| tiny file (<8 KB) | files truncated during rip |
| very short duration (<2 s) | obviously incomplete tracks |
| no sync bytes near end of file | file cut off mid-stream |
| frame count vs reported duration mismatch | misreported length |

Files scoring **0.40 or above** are flagged as suspect (adjustable with `--threshold`).

## Installation

### Requirements

- Python 3.11+
- [mutagen](https://mutagen.readthedocs.io/)

### Setup

```bash
git clone https://github.com/brthurr/clean-ripped-mp3.git
cd clean-ripped-mp3

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> **Debian/Ubuntu:** If `python3 -m venv` fails, install the missing package first:
> ```bash
> sudo apt install python3.11-venv -y
> ```

## Usage

### Report only (safe, no files are changed)

```bash
python detect_corrupted.py /path/to/music
```

### Recursive scan

```bash
python detect_corrupted.py /path/to/music -r
```

### Move suspect files to a quarantine folder

```bash
python detect_corrupted.py /path/to/music -r --move ~/quarantine
```

Review the quarantine folder before deleting — some borderline files may still be listenable.

### Delete suspect files immediately

```bash
python detect_corrupted.py /path/to/music -r --delete
```

### Adjust sensitivity

Lower threshold = more sensitive (catches marginal files):

```bash
python detect_corrupted.py /path/to/music -r --threshold 0.30
```

### Show all files including clean ones

```bash
python detect_corrupted.py /path/to/music -r --verbose
```

## Output

Results are printed to the terminal with a corruption score and the specific flags that triggered it. A `corrupted_files.log` is always written to the scanned directory with full details for every suspect file.

```
[   1/412] SUSPECT  0.85  Rock/track07.mp3
            • high_junk_ratio – 34.2% of scanned bytes are not in valid frames
            • truncated – no valid sync bytes in last 256 bytes of file
[   2/412] ok       0.00  Rock/track08.mp3
...
────────────────────────────────────────────────────────────
Results: 23 suspect / 389 clean / 412 total
```

## Options

```
positional arguments:
  directory             Directory to scan for MP3 files

options:
  -r, --recursive       Scan subdirectories recursively
  --threshold FLOAT     Corruption score threshold 0.0–1.0 (default: 0.40)
  --move DEST_DIR       Move suspect files to DEST_DIR
  --delete              Delete suspect files (irreversible)
  --verbose, -v         Show all files, not just flagged ones
  --no-color            Disable ANSI color output
```
