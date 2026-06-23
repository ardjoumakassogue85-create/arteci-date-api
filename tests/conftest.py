"""Fixtures pytest partagées.

La télémétrie et le réseau sont désactivés pour que les tests soient hermétiques et
rapides. Les vrais CSV sont localisés via ``TEST_DATA_DIR`` (par défaut
``./tests/data``), avec un repli sur la racine du projet, et les tests qui en ont besoin
sont skippés quand ils sont absents.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Doit être défini avant l'import de l'app (et de ses Settings mis en cache).
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("APP_ENV", "local")
os.environ.setdefault("LOG_LEVEL", "WARNING")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_FILES = {
    "anon_1": "lst_of_users_anon_1.csv",
    "anon_2": "lst_of_users_anon_2.csv",
    "anon_3": "lst_of_users_anon_3.csv",
}


def find_real_csv(key: str) -> Path | None:
    """Localise un CSV fourni dans TEST_DATA_DIR ou à la racine du projet."""
    name = REAL_FILES[key]
    candidates = [
        Path(os.environ.get("TEST_DATA_DIR", PROJECT_ROOT / "tests" / "data")) / name,
        PROJECT_ROOT / "tests" / "data" / name,
        PROJECT_ROOT / name,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


@pytest.fixture
def anon_1_path() -> Path:
    path = find_real_csv("anon_1")
    if path is None:
        pytest.skip(
            "CSV réel lst_of_users_anon_1.csv introuvable. "
            "Placez-le dans tests/data/ (ou définissez TEST_DATA_DIR)."
        )
    return path
