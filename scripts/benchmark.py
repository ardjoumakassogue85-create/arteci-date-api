"""Benchmark reproductible des approches de normalisation des colonnes de dates.

Le challenge demande de *comparer des outils de différents langages/librairies* et de
justifier celui qu'on garde. Ce script compare, sur les **vrais CSV fournis**, au moins
deux approches et reporte le temps mur et la RAM pic :

  * ``polars``          – notre approche de production : parsing vectorisé multi-passes,
                          traité par batchs à mémoire bornée.
  * ``pandas-rowwise``  – la baseline naïve : parse chaque cellule dans une boucle
                          Python (calque d'une implémentation ligne à ligne).
  * ``pandas-vectorized`` – ``to_datetime`` de pandas (C mono-thread), plus proche dans
                          l'esprit mais sans le moteur multi-threadé de Polars et sans la
                          règle métier de fallback par orientation.

Usage :
    python scripts/benchmark.py --file lst_of_users_anon_1.csv
    python scripts/benchmark.py --file lst_of_users_anon_2.csv --rows 500000
    python scripts/benchmark.py --approaches polars,pandas-vectorized

Les résultats alimentent docs/benchmark.md.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Rend `app` importable quand on lance depuis la racine du dépôt.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.date_normalizer import OUTPUT_FORMAT  # noqa: E402
from app.services.minio_client import (  # noqa: E402
    _normalize_csv_batched,
    detect_separator,
)

DEFAULT_DATE_COLUMNS = [
    "DATE_CREATION",
    "DATE_DESACTIVATION",
    "DATE_DERNIERE_CONNECTION_1",
]


class PeakMemory:
    """Échantillonne le RSS du process dans un thread de fond ; reporte le pic en Mo."""

    def __init__(self, interval: float = 0.05) -> None:
        self._interval = interval
        self._peak = 0
        self._stop = False
        self._thread: threading.Thread | None = None
        try:
            import psutil

            self._proc = psutil.Process()
        except ImportError:
            self._proc = None

    def __enter__(self) -> PeakMemory:
        if self._proc is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop:
            self._peak = max(self._peak, self._proc.memory_info().rss)
            time.sleep(self._interval)

    def __exit__(self, *exc) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join()

    @property
    def mb(self) -> float:
        return self._peak / 1e6


def _maybe_slice(src: Path, rows: int | None) -> tuple[Path, bool]:
    """Écrit éventuellement une tranche en-tête+N lignes dans un temp (comparaison équitable)."""
    if rows is None:
        return src, False
    with open(src, "rb") as fh:
        data = [fh.readline() for _ in range(rows + 1)]
    tmp = Path(tempfile.mktemp(suffix=".csv"))
    tmp.write_bytes(b"".join(d for d in data if d))
    return tmp, True


# --- approches --------------------------------------------------------------


def run_polars(src: Path, sep: str, date_cols, formats, batch_rows) -> int:
    out = tempfile.mktemp(suffix=".csv")
    try:
        rows, _ = _normalize_csv_batched(
            str(src), out, sep, date_cols, formats, batch_rows
        )
        return rows
    finally:
        if os.path.exists(out):
            os.remove(out)


def run_pandas_rowwise(src: Path, sep: str, date_cols, formats, _batch) -> int:
    from datetime import datetime

    import pandas as pd

    dmy = ["%d/%m/%Y", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"]
    mdy = ["%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"]
    iso = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]

    def parse_cell(v: object, hint: str) -> object:
        if not isinstance(v, str) or not v.strip():
            return v
        s = v.strip().replace("-", "/").replace(".", "/")
        order = (dmy + mdy) if hint == "DMY" else (mdy + dmy)
        for f in order:
            try:
                return datetime.strptime(s, f).strftime("%d-%m-%Y %H:%M:%S")
            except ValueError:
                continue
        for f in iso:
            try:
                return datetime.strptime(v.strip(), f).strftime("%d-%m-%Y %H:%M:%S")
            except ValueError:
                continue
        return v  # garde tel quel

    df = pd.read_csv(src, sep=sep, dtype=str, keep_default_na=False)
    for col, hint in zip(date_cols, formats, strict=True):
        df[col] = df[col].map(lambda x, h=hint: parse_cell(x, h))
    out = tempfile.mktemp(suffix=".csv")
    try:
        df.to_csv(out, sep=sep, index=False)
        return len(df)
    finally:
        if os.path.exists(out):
            os.remove(out)


def run_pandas_vectorized(src: Path, sep: str, date_cols, formats, _batch) -> int:
    import pandas as pd

    df = pd.read_csv(src, sep=sep, dtype=str, keep_default_na=False)
    for col, hint in zip(date_cols, formats, strict=True):
        s = df[col].replace("", pd.NA)
        parsed = pd.to_datetime(
            s, dayfirst=(hint == "DMY"), errors="coerce", format="mixed"
        )
        formatted = parsed.dt.strftime("%d-%m-%Y %H:%M:%S")
        df[col] = formatted.fillna(df[col])  # garde l'original là où ce n'est pas parsé
    out = tempfile.mktemp(suffix=".csv")
    try:
        df.to_csv(out, sep=sep, index=False)
        return len(df)
    finally:
        if os.path.exists(out):
            os.remove(out)


APPROACHES = {
    "polars": run_polars,
    "pandas-rowwise": run_pandas_rowwise,
    "pandas-vectorized": run_pandas_vectorized,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="lst_of_users_anon_1.csv")
    parser.add_argument(
        "--date-columns", default=",".join(DEFAULT_DATE_COLUMNS),
        help="colonnes de dates séparées par des virgules",
    )
    parser.add_argument(
        "--formats", default=None,
        help="DMY/MDY par colonne, séparés par des virgules (défaut : MDY partout)",
    )
    parser.add_argument("--rows", type=int, default=None, help="limite le nombre de lignes")
    parser.add_argument("--batch-rows", type=int, default=500_000)
    parser.add_argument(
        "--approaches", default="polars,pandas-vectorized,pandas-rowwise"
    )
    args = parser.parse_args()

    src = Path(args.file)
    if not src.is_file():
        # essaie la racine du projet
        root = Path(__file__).resolve().parents[1]
        src = root / args.file
    if not src.is_file():
        parser.error(f"file not found: {args.file}")

    date_cols = args.date_columns.split(",")
    formats = (
        args.formats.split(",") if args.formats else ["MDY"] * len(date_cols)
    )

    with open(src, "rb") as fh:
        sep = detect_separator(fh.read(8192))

    work, is_temp = _maybe_slice(src, args.rows)
    size_mb = work.stat().st_size / 1e6
    print(
        f"File: {src.name}  size={size_mb:.1f}MB  sep={sep!r}  "
        f"date_cols={date_cols}  formats={formats}  rows_limit={args.rows}"
    )
    print(f"Output format: {OUTPUT_FORMAT}\n")
    print(f"{'approach':22s} {'rows':>11s} {'time(s)':>9s} {'peak(MB)':>9s} {'rows/s':>12s}")
    print("-" * 68)

    try:
        for name in args.approaches.split(","):
            name = name.strip()
            fn = APPROACHES.get(name)
            if fn is None:
                print(f"{name:22s}  (unknown approach, skipped)")
                continue
            try:
                with PeakMemory() as mem:
                    t0 = time.perf_counter()
                    rows = fn(work, sep, date_cols, formats, args.batch_rows)
                    dt = time.perf_counter() - t0
                print(
                    f"{name:22s} {rows:>11,} {dt:>9.2f} {mem.mb:>9.0f} "
                    f"{rows / dt:>12,.0f}"
                )
            except Exception as exc:  # continue même si une approche échoue
                print(f"{name:22s}  FAILED: {type(exc).__name__}: {exc}")
    finally:
        if is_temp and work.exists():
            work.unlink()


if __name__ == "__main__":
    main()
