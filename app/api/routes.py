"""Endpoints HTTP : GET /columns et POST /processDate (+ health)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from opentelemetry import trace

from app.core.errors import UnsupportedFormatError
from app.models.schemas import ColumnsResponse, ProcessDateRequest, ProcessedRow
from app.services.date_normalizer import validate_hint
from app.services.minio_client import StorageService, get_storage

router = APIRouter()
logger = logging.getLogger("arteci.api")
tracer = trace.get_tracer("arteci.api")


@router.get("/health", tags=["ops"], summary="Sonde de disponibilité (liveness/readiness)")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/columns",
    response_model=ColumnsResponse,
    tags=["dates"],
    summary="Lister les colonnes d'un fichier dans MinIO sans le télécharger",
)
def get_columns(
    bucket: str = Query(..., min_length=1, description="Nom du bucket MinIO."),
    file: str = Query(..., min_length=1, description="Clé de l'objet dans le bucket."),
    storage: StorageService = Depends(get_storage),
) -> ColumnsResponse:
    """Lit uniquement l'en-tête (requête par plage) et renvoie le nom des colonnes."""
    with tracer.start_as_current_span("endpoint.columns") as span:
        span.set_attribute("arteci.bucket", bucket)
        span.set_attribute("arteci.file", file)
        columns, separator = storage.read_header_columns(bucket, file)
        logger.info(
            "Listed columns.",
            extra={"bucket": bucket, "file": file, "columns_count": len(columns)},
        )
        return ColumnsResponse(
            bucket=bucket, file=file, columns=columns, separator=separator
        )


def _validate_request(req: ProcessDateRequest) -> list[str]:
    """Valide les longueurs + les valeurs de hint, et renvoie les hints normalisés."""
    if len(req.date_columns) != len(req.date_formats):
        raise UnsupportedFormatError(
            "date_columns and date_formats must have the same length. "
            f"Got {len(req.date_columns)} column(s) and "
            f"{len(req.date_formats)} format(s).",
            date_columns_count=len(req.date_columns),
            date_formats_count=len(req.date_formats),
        )
    # validate_hint lève UnsupportedFormatError (400) sur tout ce qui n'est pas DMY/MDY.
    return [validate_hint(f) for f in req.date_formats]


@router.post(
    "/processDate",
    response_model=list[ProcessedRow],
    tags=["dates"],
    summary="Normaliser les colonnes de date, réécrire le fichier dans MinIO, renvoyer 100 lignes",
)
def process_date(
    req: ProcessDateRequest,
    storage: StorageService = Depends(get_storage),
) -> list[ProcessedRow]:
    """Traite les colonnes de date en place et renvoie les 100 premières lignes traitées.

    Les lignes renvoyées reflètent le fichier **après** la transformation complète
    (relues depuis l'objet réécrit), jamais un état intermédiaire.
    """
    with tracer.start_as_current_span("endpoint.processDate") as span:
        span.set_attribute("arteci.bucket", req.bucket)
        span.set_attribute("arteci.file", req.file)
        span.set_attribute("arteci.date_columns", ",".join(req.date_columns))

        hints = _validate_request(req)
        logger.info(
            "Processing date columns.",
            extra={
                "bucket": req.bucket,
                "file": req.file,
                "date_columns": req.date_columns,
                "date_formats": hints,
            },
        )
        result = storage.process_date_columns(
            bucket=req.bucket,
            file=req.file,
            date_columns=req.date_columns,
            date_formats=hints,
        )
        span.set_attribute("arteci.preview_rows", len(result.preview))
        logger.info(
            "Processing complete.",
            extra={
                "bucket": req.bucket,
                "file": req.file,
                "preview_rows": len(result.preview),
            },
        )
        return result.preview
