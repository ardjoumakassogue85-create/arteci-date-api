"""Tests des endpoints avec une couche de stockage factice (pas besoin de vrai MinIO).

La dépendance de stockage est surchargée pour exercer le routage, la validation des
requêtes et le mapping d'erreurs de façon isolée. Les I/O lourdes et le moteur de dates
sont couverts par les tests du normalizer et du pipeline.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ColumnNotFoundError, FileNotFoundInBucketError
from app.main import create_app
from app.services.minio_client import ProcessResult, get_storage

KNOWN_COLUMNS = ["CODE_LOGIN", "LOGIN", "DATE_CREATION", "DATE_DESACTIVATION"]


class FakeStorage:
    """Remplaçant de StorageService, au comportement déterministe et sans réseau."""

    def read_header_columns(self, bucket: str, file: str):
        if file == "missing.csv":
            raise FileNotFoundInBucketError(bucket, file)
        return KNOWN_COLUMNS, ";"

    def process_date_columns(self, bucket, file, date_columns, date_formats):
        if file == "missing.csv":
            raise FileNotFoundInBucketError(bucket, file)
        missing = [c for c in date_columns if c not in KNOWN_COLUMNS]
        if missing:
            raise ColumnNotFoundError(missing, KNOWN_COLUMNS)
        # Renvoie deux fausses lignes traitées.
        rows = [
            {"CODE_LOGIN": "10000", "DATE_CREATION": "17-07-2019 00:00:00"},
            {"CODE_LOGIN": "10001", "DATE_CREATION": "30-07-2020 00:00:00"},
        ]
        return ProcessResult(
            preview=rows, row_count=2, columns=list(rows[0]), separator=";"
        )


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    return TestClient(app)


# --- /health ----------------------------------------------------------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- GET /columns -----------------------------------------------------------


def test_columns_ok(client):
    r = client.get("/columns", params={"bucket": "raw", "file": "f.csv"})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == KNOWN_COLUMNS
    assert body["separator"] == ";"


def test_columns_file_not_found(client):
    r = client.get("/columns", params={"bucket": "raw", "file": "missing.csv"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "file_not_found"


def test_columns_missing_query_param(client):
    r = client.get("/columns", params={"bucket": "raw"})
    assert r.status_code == 422  # validation FastAPI : 'file' est requis


# --- POST /processDate ------------------------------------------------------


def _body(**over):
    base = {
        "date_columns": ["DATE_CREATION"],
        "date_formats": ["MDY"],
        "bucket": "processeddata",
        "file": "f.csv",
    }
    base.update(over)
    return base


def test_process_ok_returns_rows(client):
    r = client.post("/processDate", json=_body())
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert rows[0]["DATE_CREATION"] == "17-07-2019 00:00:00"


def test_process_length_mismatch_400(client):
    r = client.post(
        "/processDate",
        json=_body(date_columns=["DATE_CREATION", "DATE_DESACTIVATION"], date_formats=["MDY"]),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_format"


def test_process_bad_format_400(client):
    r = client.post("/processDate", json=_body(date_formats=["YMD"]))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "unsupported_format"
    assert "DMY" in str(err)  # actionnable : liste les valeurs supportées


def test_process_missing_column_400(client):
    r = client.post("/processDate", json=_body(date_columns=["NOPE"]))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "column_not_found"
    assert "NOPE" in err["missing_columns"]
    assert "CODE_LOGIN" in err["available_columns"]  # actionnable : liste les colonnes dispo


def test_process_file_not_found_404(client):
    r = client.post("/processDate", json=_body(file="missing.csv"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "file_not_found"


def test_process_empty_lists_422(client):
    # Le min_length=1 de Pydantic rejette les listes vides avant notre handler.
    r = client.post("/processDate", json=_body(date_columns=[], date_formats=[]))
    assert r.status_code == 422
