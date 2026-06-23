"""Exceptions métier et leur correspondance HTTP.

L'API est *interne* : les messages d'erreur doivent dire **quoi** a échoué et
**pourquoi**, et être actionnables (par ex. lister les colonnes qui existent
vraiment). Chaque exception métier porte un statut HTTP, un ``code`` machine stable
et un message lisible par un humain.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ArteciError(Exception):
    """Classe de base de toutes les erreurs métier renvoyées au client."""

    status_code: int = 400
    code: str = "arteci_error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        # Champs additionnels fusionnés dans le body JSON pour aider au debug.
        self.context: dict[str, object] = context


class FileNotFoundInBucketError(ArteciError):
    """La clé d'objet n'existe pas dans le bucket donné."""

    status_code = 404
    code = "file_not_found"

    def __init__(self, bucket: str, file: str) -> None:
        super().__init__(
            f"File '{file}' was not found in bucket '{bucket}'. "
            f"Check the bucket name and the full object key (including any prefix).",
            bucket=bucket,
            file=file,
        )


class BucketNotFoundError(ArteciError):
    """Le bucket lui-même n'existe pas."""

    status_code = 404
    code = "bucket_not_found"

    def __init__(self, bucket: str) -> None:
        super().__init__(
            f"Bucket '{bucket}' does not exist on the configured MinIO endpoint.",
            bucket=bucket,
        )


class ColumnNotFoundError(ArteciError):
    """Une ou plusieurs colonnes de dates demandées sont absentes du fichier."""

    status_code = 400
    code = "column_not_found"

    def __init__(self, missing: list[str], available: list[str]) -> None:
        super().__init__(
            f"Column(s) {missing} do not exist in the file. "
            f"Available columns are: {available}.",
            missing_columns=missing,
            available_columns=available,
        )


class UnsupportedFormatError(ArteciError):
    """Une valeur de date_formats est invalide, ou les listes sont incohérentes."""

    status_code = 400
    code = "unsupported_format"


class StorageAccessError(ArteciError):
    """Échec MinIO générique (accès refusé, réseau, mauvaise config…)."""

    status_code = 502
    code = "storage_access_error"


def _error_body(exc: ArteciError) -> dict[str, object]:
    return {"error": {"code": exc.code, "message": exc.message, **exc.context}}


def register_exception_handlers(app: FastAPI) -> None:
    """Branche les exceptions métier sur des réponses JSON propres."""

    @app.exception_handler(ArteciError)
    async def _handle_arteci_error(_: Request, exc: ArteciError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_error_body(exc))
