"""Carga de ficheros Excel en SQLite: CSV limpio, huellas e imports.

Deduplicación entre ficheros que se solapan (decisión 2026-11-08):

- Clave de un movimiento con saldo (cuentas): (cuenta, fecha, importe, saldo). El saldo tras
  el movimiento lo identifica aunque el concepto cambie de una exportación a otra (el
  Santander no escribe igual el concepto en todos sus informes).
- Sin saldo (tarjeta): (cuenta, fecha, importe, concepto).
- Una clave puede repetirse de verdad: dos cafés iguales el mismo día en la tarjeta, o en la
  cuenta un cargo, su devolución y otra vez el cargo, que deja el mismo saldo (visto en un
  extracto real: los saldos encadenan fila a fila). Por eso se cuentan: si un fichero trae la
  clave n veces y la base ya tiene m, entran n - m. Reimportar no duplica y no se pierde nada.
  La primera aparición conserva la huella de siempre; la k-ésima lleva «#k».
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from moooney.config import Settings
from moooney.db import repo
from moooney.ingest.detect import list_excel_files, sha256_file
from moooney.ingest.parse_santander import ParseError, inspect_file

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
    error: str | None = None  # no se pudo leer (formato no reconocido); el resto sigue


def dedup_key(account: str, iso_date: str, amount: float, concept: str, balance: float | None) -> tuple:
    """Identidad de un movimiento para comparar ficheros que se solapan (ver docstring)."""
    if balance is None:
        return ("sin_saldo", account, iso_date, round(amount, 2), concept)
    # Importe 0 no mueve el saldo: ahí el concepto sí hace falta para distinguir.
    return ("saldo", account, iso_date, round(amount, 2), round(balance, 2), concept if amount == 0 else "")


def _existing_count(conn, key: tuple) -> int:
    if key[0] == "saldo":
        _, account, d, amount, balance, concept = key
        sql = ("SELECT COUNT(*) FROM transactions WHERE account = ? AND date = ? AND ROUND(amount, 2) = ?"
               " AND ROUND(balance, 2) = ?")
        args: list = [account, d, amount, balance]
        if amount == 0:
            sql += " AND concept = ?"
            args.append(concept)
    else:
        _, account, d, amount, concept = key
        sql = ("SELECT COUNT(*) FROM transactions WHERE account = ? AND date = ? AND ROUND(amount, 2) = ?"
               " AND balance IS NULL AND concept = ?")
        args = [account, d, amount, concept]
    return conn.execute(sql, args).fetchone()[0]


def select_new_rows(conn, rows: list[dict]) -> list[dict]:
    """Filas del fichero que aún no están en la base, con huellas únicas para las repetidas."""
    by_key: dict[tuple, list[dict]] = {}
    for r in rows:
        by_key.setdefault(dedup_key(r["account"], r["date"], r["amount"], r["concept"], r["balance"]), []).append(r)
    nuevas = []
    for key, group in by_key.items():
        ya = _existing_count(conn, key)
        for k, r in enumerate(group, start=1):
            if k <= ya:
                continue
            # La primera conserva la huella de siempre (bases ya cargadas); las repeticiones
            # legítimas de una tarjeta llevan su número de aparición.
            nuevas.append(r if k == 1 else {**r, "fingerprint": r["fingerprint"] + f"#{k}"})
    return nuevas

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


def _tx_row(r: dict, created_at: str) -> dict:
    balance = None if pd.isna(r["balance"]) else r["balance"]
    return {
        "fingerprint": fingerprint(r["account"], r["date"], r["amount"], r["concept"], balance),
        "date": r["date"],
        "value_date": None if pd.isna(r["value_date"]) else r["value_date"],
        "concept": r["concept"],
        "amount": r["amount"],
        "balance": balance,
        "account": r["account"],
        "created_at": created_at,
    }


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
    tx_rows = [_tx_row(r, now) for r in df.to_dict("records")]
    rows_new = repo.insert_transactions(conn, select_new_rows(conn, tx_rows), import_id)
    conn.execute("UPDATE imports SET rows_new = ? WHERE id = ?", (rows_new, import_id))
    conn.commit()
    return IngestOutcome(path.name, sha256, rows_total, rows_new, date_min, date_max, csv_path)


def ingest_directory(directory: Path, conn, settings: Settings) -> list[IngestOutcome]:
    """Ingiere todos los Excel nuevos del directorio. Idempotente por SHA-256."""
    outcomes = []
    for path in list_excel_files(directory):
        try:
            outcomes.append(ingest_file(path, conn, settings))
        except ParseError as e:
            # Un fichero ilegible no bloquea a los demás: se informa y se sigue.
            outcomes.append(IngestOutcome(path.name, sha256_file(path), 0, 0, None, None, error=str(e)))
    repo.mark_card_settlements(conn)
    return outcomes


def find_overlap_duplicates(conn) -> list[int]:
    """Movimientos con saldo que entraron dos veces porque dos extractos solapados escribían
    distinto el concepto (cargas anteriores a 1.5.1). Devuelve los id sobrantes.

    Solo se consideran las filas sin «#k» (las repeticiones contadas son legítimas). De cada
    clave se queda todo lo de la primera importación que la trajo; de cada importación
    posterior sobran tantas como ya había: un fichero no puede traer repetido lo que otro ya
    tenía, pero sí repeticiones propias de más.
    """
    rows = conn.execute(
        "SELECT id, account, date, amount, balance, concept, import_id FROM transactions"
        " WHERE balance IS NOT NULL AND fingerprint NOT LIKE '%#%' ORDER BY id"
    ).fetchall()
    grupos: dict[tuple, dict] = {}
    for r in rows:
        key = dedup_key(r["account"], r["date"], r["amount"], r["concept"], r["balance"])
        grupos.setdefault(key, {}).setdefault(r["import_id"], []).append(r["id"])
    sobran = []
    for por_import in grupos.values():
        if len(por_import) < 2:
            continue
        imports = list(por_import.values())  # en orden de carga: los id crecen
        ya = len(imports[0])
        for ids in imports[1:]:
            repetidas = min(len(ids), ya)
            sobran.extend(ids[:repetidas])
            ya = max(ya, len(ids))
    return sobran


def recover_missing(conn, settings: Settings) -> int:
    """Relee todos los Excel de la carpeta (también los ya importados) y añade los movimientos
    que falten. Recupera las repeticiones legítimas que la huella antigua juntaba en una."""
    total = 0
    for path in list_excel_files(settings.paths.excel_dir):
        try:
            df = inspect_file(path, settings.profiles).df
        except ParseError:
            continue
        sha256 = sha256_file(path)
        imp = repo.get_import_by_hash(conn, sha256)
        if imp is None:
            continue  # aún sin importar: lo hará la ingesta normal
        rows = [_tx_row(r, imp["imported_at"]) for r in df.to_dict("records")]
        n = repo.insert_transactions(conn, select_new_rows(conn, rows), imp["id"])
        if n:
            conn.execute("UPDATE imports SET rows_new = rows_new + ? WHERE id = ?", (n, imp["id"]))
            conn.commit()
        total += n
    return total


def remove_overlap_duplicates(conn, ids: list[int]) -> int:
    with conn:
        conn.executemany("DELETE FROM transactions WHERE id = ?", [(i,) for i in ids])
    return len(ids)
