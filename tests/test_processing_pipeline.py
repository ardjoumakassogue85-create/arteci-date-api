"""Tests du pipeline de traitement CSV par batch (lecture -> normalisation -> écriture).

Un tout petit CSV fabriqué exerce la mécanique d'aller-retour de façon déterministe (les
vrais fichiers fournis sont uniformément en MDY et ne peuvent pas exercer les cellules
mixtes/malformées). Un test séparé traite un *échantillon* du vrai fichier anon_1 quand
il est disponible.
"""

from __future__ import annotations

import polars as pl
import pytest

from app.services.minio_client import _normalize_csv_batched, detect_separator

# Entrée fabriquée : séparateur ';' + BOM UTF-8, une colonne de date aux formats mixtes,
# une cellule malformée, une cellule vide, une valeur ISO/fuseau, plus des colonnes non-dates.
CRAFTED = (
    "﻿id;NOM;DATE_X;FLAG\n"
    "1;Alice;07/17/2019;A\n"
    "2;Bob;25/12/2023;B\n"            # clairement DMY alors que le hint est MDY
    "3;Carol;01/02/2023;C\n"         # ambigu -> tranché par le hint MDY
    "4;Dave;not a date;D\n"          # malformé -> gardé tel quel
    "5;Eve;;E\n"                      # vide -> gardé vide
    "6;Frank;2019-07-17T14:30:00Z;F\n"  # ISO + fuseau
)

EXPECTED_DATE_X = [
    "17-07-2019 00:00:00",
    "25-12-2023 00:00:00",
    "02-01-2023 00:00:00",
    "not a date",
    None,  # la cellule vide est relue comme null
    "17-07-2019 14:30:00",
]


@pytest.fixture
def crafted_csv(tmp_path):
    src = tmp_path / "in.csv"
    src.write_bytes(CRAFTED.encode("utf-8"))
    return src


def test_separator_autodetect():
    assert detect_separator(CRAFTED.encode("utf-8")) == ";"


def test_batched_normalizes_date_column(crafted_csv, tmp_path):
    out = tmp_path / "out.csv"
    # batch_rows=2 force plusieurs batchs -> vérifie aussi l'en-tête écrit une seule fois.
    rows, unparsed = _normalize_csv_batched(
        str(crafted_csv), str(out), ";", ["DATE_X"], ["MDY"], batch_rows=2
    )
    assert rows == 6
    assert unparsed == 1  # seul "not a date" est non vide et non parsable

    df = pl.read_csv(out, separator=";", infer_schema_length=0)
    assert df.columns == ["id", "NOM", "DATE_X", "FLAG"]
    assert df["DATE_X"].to_list() == EXPECTED_DATE_X


def test_batched_preserves_non_date_columns(crafted_csv, tmp_path):
    out = tmp_path / "out.csv"
    _normalize_csv_batched(str(crafted_csv), str(out), ";", ["DATE_X"], ["MDY"], 2)
    df = pl.read_csv(out, separator=";", infer_schema_length=0)
    assert df["id"].to_list() == ["1", "2", "3", "4", "5", "6"]
    assert df["NOM"].to_list() == ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]
    assert df["FLAG"].to_list() == ["A", "B", "C", "D", "E", "F"]


def test_batched_header_written_once(crafted_csv, tmp_path):
    out = tmp_path / "out.csv"
    _normalize_csv_batched(str(crafted_csv), str(out), ";", ["DATE_X"], ["MDY"], 2)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "id;NOM;DATE_X;FLAG"
    assert len(lines) == 7  # 1 en-tête + 6 lignes de données, pas d'en-tête dupliqué
    # cellule vide conservée vide dans le fichier écrit
    assert lines[5] == "5;Eve;;E"


# --- échantillon de données réelles -----------------------------------------

REAL_DATE_COLS = ["DATE_CREATION", "DATE_DESACTIVATION", "DATE_DERNIERE_CONNECTION_1"]
OUT_RE = r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}$"


def test_real_anon1_sample(anon_1_path, tmp_path):
    """Traite un échantillon du vrai fichier et valide les règles métier."""
    # Prend l'en-tête + les 3000 premières lignes de données pour garder le test rapide.
    with open(anon_1_path, "rb") as fh:
        sample_lines = [fh.readline() for _ in range(3001)]
    src = tmp_path / "sample.csv"
    src.write_bytes(b"".join(sample_lines))

    sep = detect_separator(sample_lines[0])
    out = tmp_path / "out.csv"
    rows, _ = _normalize_csv_batched(
        str(src), str(out), sep, REAL_DATE_COLS, ["MDY", "MDY", "MDY"], 1000
    )
    assert rows == 3000

    df = pl.read_csv(out, separator=sep, infer_schema_length=0)
    df.columns = [c.lstrip("﻿") for c in df.columns]
    # Chaque cellule de date non vide est au format de sortie normalisé.
    for col in REAL_DATE_COLS:
        s = df[col]
        non_empty = s.filter(s.is_not_null() & (s.str.strip_chars() != ""))
        assert non_empty.str.contains(OUT_RE).all(), f"format incorrect dans {col}"
    # Première ligne connue (07/17/2019 -> 17-07-2019).
    assert df["DATE_CREATION"][0] == "17-07-2019 00:00:00"
    # Colonne non-date inchangée.
    assert df["CODE_LOGIN"][0] == "10000"
