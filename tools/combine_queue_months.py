"""Merge the monthly Queue_All_Calls exports into one chronological CSV.

Rows are kept byte-for-byte (UTF-8 with BOM, CRLF) and sorted by call date. The
monthly originals are zipped next to the combined file and, with --delete-originals,
removed once the combined file is verified to hold exactly the same rows.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "testdata" / "New_year_data"
DATE_FORMAT = "%d/%b/%y %H:%M:%S"


def read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        return header, [row for row in reader if row]


def row_digest(rows: list[list[str]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows):
        digest.update("\x1f".join(row).encode("utf-8") + b"\n")
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--pattern", default="Queue_All_Calls_????-??.csv")
    parser.add_argument("--delete-originals", action="store_true")
    args = parser.parse_args()

    monthly = sorted(args.data_dir.glob(args.pattern))
    if not monthly:
        raise SystemExit(f"No monthly files matching {args.pattern} in {args.data_dir}")

    header: list[str] | None = None
    rows: list[list[str]] = []
    for path in monthly:
        file_header, file_rows = read_rows(path)
        if header is None:
            header = file_header
        elif file_header != header:
            raise SystemExit(f"{path.name} has a different header: {file_header}")
        rows.extend(file_rows)
        print(f"{path.name}: {len(file_rows)} rows")

    call_ids = [row[0] for row in rows]
    if len(set(call_ids)) != len(call_ids):
        raise SystemExit(f"{len(call_ids) - len(set(call_ids))} duplicate Call IDs across monthly files")

    date_col = header.index("Date")
    rows.sort(key=lambda row: (datetime.strptime(row[date_col], DATE_FORMAT), row[0]))
    first = rows[0][date_col][:9].replace("/", "-")
    last = rows[-1][date_col][:9].replace("/", "-")
    start = datetime.strptime(rows[0][date_col], DATE_FORMAT).strftime("%Y-%m")
    end = datetime.strptime(rows[-1][date_col], DATE_FORMAT).strftime("%Y-%m")
    combined = args.data_dir / f"Queue_All_Calls_{start}_to_{end}.csv"

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    combined.write_bytes(b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))

    combined_header, combined_rows = read_rows(combined)
    if combined_header != header or row_digest(combined_rows) != row_digest(rows) or len(combined_rows) != len(rows):
        raise SystemExit("Combined file verification failed; originals left in place.")
    print(f"Wrote {combined.name}: {len(combined_rows)} rows ({first} to {last}), digest verified")

    archive = args.data_dir / "monthly_originals.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in monthly:
            bundle.write(path, arcname=path.name)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None or sorted(bundle.namelist()) != sorted(p.name for p in monthly):
            raise SystemExit("Archive verification failed; originals left in place.")
    print(f"Archived {len(monthly)} originals to {archive.name}")

    if args.delete_originals:
        for path in monthly:
            path.unlink()
        print(f"Deleted {len(monthly)} monthly originals")


if __name__ == "__main__":
    main()
