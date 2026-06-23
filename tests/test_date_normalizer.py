"""Tests unitaires des règles métier de normalisation des dates.

Ils utilisent de petits jeux de chaînes en dur (pas de fichiers), pour que chaque règle
soit isolée et déterministe. Contrat de sortie : ``DD-MM-YYYY HH:mm:ss``.
"""

from __future__ import annotations

import pytest

from app.core.errors import UnsupportedFormatError
from app.services.date_normalizer import normalize_values, validate_hint

OUT_RE = r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}$"


# --- orientation & ambiguïté ------------------------------------------------


def test_mdy_unambiguous():
    assert normalize_values(["07/17/2019"], "MDY") == ["17-07-2019 00:00:00"]


def test_dmy_unambiguous():
    assert normalize_values(["25/12/2023"], "DMY") == ["25-12-2023 00:00:00"]


def test_ambiguous_resolved_by_hint():
    # 01/02/2023 est ambigu : le hint tranche.
    assert normalize_values(["01/02/2023"], "MDY") == ["02-01-2023 00:00:00"]
    assert normalize_values(["01/02/2023"], "DMY") == ["01-02-2023 00:00:00"]


def test_mixed_column_unambiguous_other_orientation():
    """Dans une colonne hint MDY, une valeur clairement DMY est quand même bien parsée."""
    # 12/25/2023 -> MDY (25 déc). 25/12/2023 -> impossible en MDY, parsé en DMY.
    out = normalize_values(["12/25/2023", "25/12/2023", "01/02/2023"], "MDY")
    assert out == ["25-12-2023 00:00:00", "25-12-2023 00:00:00", "02-01-2023 00:00:00"]


def test_mixed_column_in_dmy_hint():
    out = normalize_values(["25/12/2023", "12/25/2023", "01/02/2023"], "DMY")
    # 25/12 -> 25 déc ; 12/25 -> impossible en DMY -> MDY -> 25 déc ; 01/02 -> hint DMY
    assert out == ["25-12-2023 00:00:00", "25-12-2023 00:00:00", "01-02-2023 00:00:00"]


# --- séparateurs & années ---------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("17/07/2019", "17-07-2019 00:00:00"),
        ("17-07-2019", "17-07-2019 00:00:00"),
        ("17.07.2019", "17-07-2019 00:00:00"),
    ],
)
def test_separators(value, expected):
    assert normalize_values([value], "DMY") == [expected]


def test_two_digit_year_century_pivot():
    # 00-68 -> années 2000, 69-99 -> années 1900
    assert normalize_values(["17/07/19"], "DMY") == ["17-07-2019 00:00:00"]
    assert normalize_values(["01/02/75"], "DMY") == ["01-02-1975 00:00:00"]


def test_four_digit_year_not_touched_by_two_digit_logic():
    assert normalize_values(["31/12/2099"], "DMY") == ["31-12-2099 00:00:00"]


# --- composantes horaires ---------------------------------------------------


def test_with_time_hms():
    assert normalize_values(["07/17/2019 14:30:45"], "MDY") == ["17-07-2019 14:30:45"]


def test_with_time_hm():
    assert normalize_values(["17/07/2019 09:05"], "DMY") == ["17-07-2019 09:05:00"]


def test_am_pm_english():
    assert normalize_values(["07/17/2019 02:30:00 PM"], "MDY") == [
        "17-07-2019 14:30:00"
    ]


def test_missing_time_defaults_to_midnight():
    out = normalize_values(["17/07/2019"], "DMY")[0]
    assert out.endswith(" 00:00:00")


# --- ISO, timestamps, noms de mois ------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2019-07-17", "17-07-2019 00:00:00"),
        ("2019-07-17 14:30:00", "17-07-2019 14:30:00"),
        ("2019-07-17T14:30:00", "17-07-2019 14:30:00"),
        ("2019-07-17T14:30:00Z", "17-07-2019 14:30:00"),
        ("2019-07-17T14:30:00+02:00", "17-07-2019 14:30:00"),
    ],
)
def test_iso_family(value, expected):
    assert normalize_values([value], "DMY") == [expected]


def test_unix_timestamps():
    assert normalize_values(["1563321600"], "MDY") == ["17-07-2019 00:00:00"]
    assert normalize_values(["1563321600000"], "MDY") == ["17-07-2019 00:00:00"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("15 janvier 2023", "15-01-2023 00:00:00"),
        ("1 décembre 2020", "01-12-2020 00:00:00"),
        ("15 janv 2023", "15-01-2023 00:00:00"),
        ("January 15, 2023", "15-01-2023 00:00:00"),
        ("15 Jan 2023", "15-01-2023 00:00:00"),
    ],
)
def test_month_names_fr_en(value, expected):
    assert normalize_values([value], "DMY") == [expected]


@pytest.mark.parametrize(
    "value,hint,expected",
    [
        # Formats anglais/français supplémentaires de la liste de référence Qlik/Talend
        ("07/17/2019 02:30 PM", "MDY", "17-07-2019 14:30:00"),       # 12h, sans secondes
        ("January 15, 2023 02:30:00 PM", "MDY", "15-01-2023 14:30:00"),  # mois + 12h
        ("15 janvier 2023 02:30:00 PM", "DMY", "15-01-2023 14:30:00"),
        ("2019-07-17 02:30:00 PM", "DMY", "17-07-2019 14:30:00"),    # date ISO + 12h
        ("2019/07/17", "DMY", "17-07-2019 00:00:00"),               # yyyy/M/d
        ("2019-07-17T14:30:00.123Z", "DMY", "17-07-2019 14:30:00"),  # secondes fractionnaires
        ("2019-07-17 14:30:00.500", "DMY", "17-07-2019 14:30:00"),
        ("Monday, January 15, 2023", "MDY", "15-01-2023 00:00:00"),  # préfixe jour EN
        ("lundi 15 janvier 2023", "DMY", "15-01-2023 00:00:00"),     # préfixe jour FR
    ],
)
def test_qlik_talend_extra_formats(value, hint, expected):
    assert normalize_values([value], hint) == [expected]


def test_weekday_does_not_eat_month_name():
    # 'mars' (le mois) ne doit pas être confondu avec l'abréviation de jour 'mar' (mardi).
    assert normalize_values(["15 mars 2023"], "DMY") == ["15-03-2023 00:00:00"]


# --- règles de robustesse ---------------------------------------------------


def test_malformed_kept_verbatim():
    vals = ["not a date", "32/13/2023", "??", "12/2023"]
    assert normalize_values(vals, "MDY") == vals


def test_empty_and_null_kept():
    assert normalize_values(["", None], "MDY") == ["", None]


def test_malformed_does_not_block_neighbours():
    out = normalize_values(["07/17/2019", "garbage", "12/25/2023"], "MDY")
    assert out == ["17-07-2019 00:00:00", "garbage", "25-12-2023 00:00:00"]


def test_output_format_shape():
    import re

    out = normalize_values(["07/17/2019", "2019-07-17T14:30:00"], "MDY")
    assert all(re.match(OUT_RE, v) for v in out)


# --- validation du hint -----------------------------------------------------


def test_validate_hint_accepts_case_insensitive():
    assert validate_hint("dmy") == "DMY"
    assert validate_hint(" MDY ") == "MDY"


def test_validate_hint_rejects_unknown():
    with pytest.raises(UnsupportedFormatError):
        validate_hint("YMD")
