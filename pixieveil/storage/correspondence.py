"""
Patient ↔ PixieVeil ↔ RHYTHM correspondence table.

Built entirely from the permanent ``<NNNN>.state.json`` sidecars, which
survive the retention purge, so a study whose files are long gone can still
be traced: original PatientID / StudyInstanceUID → our study number and
pseudonyms → every RHYTHM upload attempt (batch, job id) and RHYTHM's own
Repository Study ID — the ID a data-erasure request to RHYTHM must name.

Shared by the admin-only ``/lookup`` page (``GET /api/lookup``) and the CLI:

    python -m pixieveil.storage.correspondence <PatientID or UID or ID>
    python -m pixieveil.storage.correspondence --all --csv > table.csv

The output contains the original (identifying) PatientID: treat it like the
anontrail it mirrors.
"""

import argparse
import csv
import io
import logging
import sys
from pathlib import Path

from pixieveil.storage.study_sidecar import StudySidecar

logger = logging.getLogger(__name__)

CSV_COLUMNS = (
    "study_number", "original_patient_id", "original_study_uid",
    "anonymized_patient_id", "anonymized_study_uid", "received_at", "status",
    "archived_via", "exported_at", "exported_by", "purged_at",
    "rhythm_repository_study_ids", "rhythm_job_ids", "rhythm_batches",
)


def _uploads(sc: StudySidecar) -> list[dict]:
    """Every RHYTHM attempt; a pre-history sidecar yields its one latest."""
    if sc.rhythm_uploads:
        return [dict(u) for u in sc.rhythm_uploads]
    if sc.rhythm_batch or sc.rhythm_job_id:
        return [{
            "started_at": None, "batch": sc.rhythm_batch,
            "session_id": sc.rhythm_session_id, "job_id": sc.rhythm_job_id,
            "status": sc.rhythm_status, "zip_sha256": sc.rhythm_zip_sha256,
            "zip_size": sc.rhythm_zip_size,
            "repository_study_id": sc.rhythm_repository_study_id,
            "orthanc_study_id": sc.rhythm_orthanc_study_id,
        }]
    return []


def row_for(sc: StudySidecar) -> dict:
    uploads = _uploads(sc)
    repo_ids = [u["repository_study_id"] for u in uploads if u.get("repository_study_id")]
    if sc.rhythm_repository_study_id and sc.rhythm_repository_study_id not in repo_ids:
        repo_ids.append(sc.rhythm_repository_study_id)
    return {
        "study_number": sc.study_number,
        "original_patient_id": sc.original_patient_id,
        "original_study_uid": sc.original_study_uid,
        "anonymized_patient_id": sc.anonymized_patient_id,
        "anonymized_study_uid": sc.anonymized_study_uid,
        "received_at": sc.received_at,
        "status": sc.status,
        "archived_via": sc.archived_via,
        "exported_at": sc.exported_at,
        "exported_by": sc.exported_by,
        "purged_at": sc.purged_at,
        "rhythm_repository_study_ids": list(dict.fromkeys(repo_ids)),
        "rhythm_uploads": uploads,
    }


def all_rows(base_path: Path) -> list[dict]:
    rows = [row_for(sc) for sc in StudySidecar.load_all(base_path).values()]
    return sorted(rows, key=lambda r: r["study_number"])


def _keys(row: dict) -> set[str]:
    keys = {row["original_patient_id"], row["original_study_uid"],
            row["anonymized_patient_id"], row["anonymized_study_uid"],
            *row["rhythm_repository_study_ids"]}
    for u in row["rhythm_uploads"]:
        keys.update((u.get("job_id"), u.get("batch"), u.get("session_id"),
                     u.get("orthanc_study_id")))
    return {str(k).strip().casefold() for k in keys if k}


def search(base_path: Path, query: str) -> list[dict]:
    """Rows whose any identifier equals *query* (case-insensitive, exact —
    no substring match, so a short query can't sweep in other patients).
    ``#N`` matches study number N only. A bare number matches the study
    number only when it matches no identifier: numeric PatientIDs are
    common, and "1042" must not return both patient 1042 and the
    unrelated study 1042."""
    q = (query or "").strip()
    if not q:
        return []
    rows = all_rows(base_path)
    if q.startswith("#"):
        n = q[1:].strip()
        if not (n.isascii() and n.isdigit()):
            return []
        return [r for r in rows if r["study_number"] == int(n)]
    qf = q.casefold()
    hits = [r for r in rows if qf in _keys(r)]
    if hits or not (q.isascii() and q.isdigit()):
        return hits
    return [r for r in rows if r["study_number"] == int(q)]


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: object) -> object:
    """Neutralise spreadsheet formula injection: PatientIDs and UIDs come
    from DICOM any C-STORE peer can send, and a cell starting with = + - @
    (or tab/CR) is evaluated when the CSV is opened in Excel/LibreOffice.
    Prefixing a quote keeps the text readable but inert."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        flat = dict(r)
        flat["rhythm_repository_study_ids"] = " ".join(r["rhythm_repository_study_ids"])
        flat["rhythm_job_ids"] = " ".join(
            u["job_id"] for u in r["rhythm_uploads"] if u.get("job_id"))
        flat["rhythm_batches"] = " ".join(
            u["batch"] for u in r["rhythm_uploads"] if u.get("batch"))
        w.writerow(["" if flat.get(c) is None else _csv_cell(flat[c]) for c in CSV_COLUMNS])
    return buf.getvalue()


def _format(row: dict) -> str:
    lines = [
        f"Study {row['study_number']:04d}  status={row['status']}"
        f"  exported_via={row['archived_via'] or '-'}",
        f"  original:   PatientID={row['original_patient_id']}"
        f"  StudyUID={row['original_study_uid']}",
        f"  anonymized: PatientID={row['anonymized_patient_id']}"
        f"  StudyUID={row['anonymized_study_uid']}",
        f"  received={row['received_at']}  exported={row['exported_at'] or '-'}"
        f" by {row['exported_by'] or '-'}  purged={row['purged_at'] or '-'}",
    ]
    if not row["rhythm_uploads"]:
        lines.append("  RHYTHM: never uploaded")
    for u in row["rhythm_uploads"]:
        lines.append(
            f"  RHYTHM upload {u.get('started_at') or '?'}: status={u.get('status')}"
            f"  repository_study_id={u.get('repository_study_id') or '-'}"
            f"  job={u.get('job_id') or '-'}  batch={u.get('batch') or '-'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m pixieveil.storage.correspondence",
        description="Find which studies (and RHYTHM IDs) belong to a patient.")
    p.add_argument("query", nargs="?",
                   help="original or pseudonymized PatientID / StudyInstanceUID, "
                        "study number (#N forces it), RHYTHM Repository Study ID, job id or batch")
    p.add_argument("--all", action="store_true", help="every study")
    p.add_argument("--csv", action="store_true", help="CSV output")
    p.add_argument("--base-path", help="study sidecar directory (default: from settings)")
    args = p.parse_args(argv)
    if not args.all and not args.query:
        p.error("give a query or --all")

    if args.base_path:
        base = Path(args.base_path)
    else:
        from pixieveil.config import Settings
        base = Path(Settings.load().storage["base_path"])

    rows = all_rows(base) if args.all else search(base, args.query)
    if args.csv:
        sys.stdout.write(to_csv(rows))
    elif not rows:
        print("no matching study", file=sys.stderr)
        return 1
    else:
        print("\n\n".join(_format(r) for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
