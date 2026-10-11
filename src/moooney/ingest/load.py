"""Carga de ficheros Excel en SQLite: CSV limpio, huellas e imports."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from moooney.config import Settings
from moooney.db import repo
from moooney.ingest.detect import list_excel_files, sha256_file
from moooney.ingest.parse_santander import inspect_file

CSV_COLUMNS = ["date", "value_date", "concept", "amount", "balance", "account"]


def fingerprint(account: str, iso_date: str, amount: float, concept: str, balance: float | None) -> str:
    """sha1(account|date|amount|concept|balance). El saldo desambigua iguales del mismo dia."""
    bal = "" if balance is None else f"{balance:.2f}"
    payload = f"{account}|{iso_date}|{amount:.2f}|{concept}|{bal}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class IngestOutcome:
    filename: str
    sha256: str
    rows_total: int
    rows_new: int
    date_min: str | None
    date_max: str | None
    csv_path: Path | None = None
    skipped: bool = False


def _write_clean_csv(df: pd.DataFrame, clean_dir: Path, sha256: str) -> Path:
    clean_dir.mkdir(parents=True, exist_ok=True)
    out = clean_dir / f"{date.today():%Y%m%d}_{sha256[:8]}.csv"
    out_csv = df.copy()
    out_csv["amount"] = out_csv["amount"].map(lambda v: f"{v:.2f}")
    out_csv["balance"] = out_csv["balance"].map(
        lambda v: "" if pd.isna(v) else f"{v:.2f}"
    )
    out_csv["value_date"] = out_csv["value_date"].fillna("")
    out_csv.to_csv(out, index=False, encoding="utf-8", lineterminator="\n")
    return out


def ingest_file(path: Path, conn, settings: Settings) -> IngestOutcome:
    """Parsea un Excel, escribe el CSV limpio e inserta las huellas nuevas."""
    sha256 = sha256_file(path)
    if repo.get_import_by_hash(conn, sha256) is not None:
        return IngestOutcome(path.name, sha256, 0, 0, None, None, skipped=True)

    result = inspect_file(path, settings.profiles)
    df = result.df
    assert df is not None
    rows_total = len(df)
    date_min = None if df.empty else str(df["date"].min())
    date_max = None if df.empty else str(df["date"].max())

    csv_path = _write_clean_csv(df, settings.paths.clean_dir, sha256)

    now = datetime.now(UTC).isoformat(timespec="seconds")
    import_id = repo.insert_import(
        conn,
        sha256=sha256,
        filename=path.name,
        imported_at=now,
        rows_total=rows_total,
        rows_new=0,
        date_min=date_min,
        date_max=date_max,
    )
    tx_rows = [
        {
            "fingerprint": fingerprint(
                r["account"], r["date"], r["amount"], r["concept"],
                None if pd.isna(r["balance"]) else r["balance"],
            ),
            "date": r["date"],
            "value_date": None if pd.isna(r["value_date"]) else r["value_date"],
            "concept": r["concept"],
            "amount": r["amount"],
            "balance": None if pd.isna(r["balance"]) else r["balance"],
            "account": r["account"],
            "created_at": now,
        }
        for r in df.to_dict("records")
    ]
    rows_new = repo.insert_transactions(conn, tx_rows, import_id)
    conn.execute("UPDATE imports SET rows_new = ? WHERE id = ?", (rows_new, import_id))
    conn.commit()
    return IngestOutcome(path.name, sha256, rows_total, rows_new, date_min, date_max, csv_path)


def ingest_directory(directory: Path, conn, settings: Settings) -> list[IngestOutcome]:
    """Ingiere todos los Excel nuevos del directorio. Idempotente por SHA-256."""
    outcomes = []
    for path in list_excel_files(directory):
        outcomes.append(ingest_file(path, conn, settings))
    return outcomes
