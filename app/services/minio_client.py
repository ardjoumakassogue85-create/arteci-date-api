"""Accès au stockage objet MinIO et orchestration du traitement de fichier.

Objectifs de conception :
* **Mémoire bornée** sur les fichiers de plusieurs Go : l'objet est streamé vers un
  fichier temporaire local, transformé par batchs (``read_csv_batched``), puis
  ré-uploadé. La RAM reste à peu près constante quelle que soit la taille du
  fichier ; seul le scratch disque grossit.
* **Écriture en place** : le fichier traité est ré-uploadé sur le *même*
  bucket/clé (aucun second objet n'est créé).
* **Lecture de l'en-tête seul** pour ``/columns`` via une requête HTTP range, pour ne
  jamais télécharger un fichier de 931 Mo juste pour lister ses colonnes.
* **Erreurs actionnables** : les échecs S3 sont mappés vers des exceptions métier
  explicites.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache

import polars as pl
from minio import Minio
from minio.error import S3Error
from opentelemetry import trace
from urllib3.exceptions import MaxRetryError

from app.core.config import Settings, get_settings
from app.core.errors import (
    BucketNotFoundError,
    ColumnNotFoundError,
    FileNotFoundInBucketError,
    StorageAccessError,
)
from app.services.date_normalizer import normalize_columns

tracer = trace.get_tracer("arteci.storage")

# Délimiteurs CSV candidats, du plus au moins révélateur d'un vrai CSV.
_DELIMITER_CANDIDATES = [";", ",", "\t", "|"]
_BOM = "﻿"


@dataclass
class ProcessResult:
    """Résultat d'une exécution de /processDate."""

    preview: list[dict[str, object]]
    row_count: int | None = None
    columns: list[str] = field(default_factory=list)
    separator: str = ","


def _strip_bom(name: str) -> str:
    return name.lstrip(_BOM)


def detect_separator(sample: bytes, override: str | None = None) -> str:
    """Choisit le délimiteur en comptant les candidats sur la ligne d'en-tête."""
    if override:
        return override
    text = sample.decode("utf-8", errors="replace").lstrip(_BOM)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if not first_line:
        return ","
    counts = {d: first_line.count(d) for d in _DELIMITER_CANDIDATES}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


class StorageService:
    """Fine couche autour du SDK MinIO, avec un mapping d'erreurs lisible."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
            region=settings.minio_region or None,
        )

    # ---- helpers bas niveau ------------------------------------------------

    def _ensure_exists(self, bucket: str, file: str) -> None:
        """Lève un 404 précis si le bucket ou l'objet est absent."""
        try:
            self._client.stat_object(bucket, file)
        except S3Error as exc:
            self._map_s3_error(exc, bucket, file)
        except MaxRetryError as exc:  # endpoint injoignable
            raise StorageAccessError(
                f"Cannot reach MinIO at '{self._settings.minio_endpoint}'. "
                f"Check MINIO_ENDPOINT / network / TLS settings.",
                endpoint=self._settings.minio_endpoint,
            ) from exc

    @staticmethod
    def _map_s3_error(exc: S3Error, bucket: str, file: str) -> None:
        code = getattr(exc, "code", "") or ""
        if code in ("NoSuchKey", "NoSuchObject"):
            raise FileNotFoundInBucketError(bucket, file) from exc
        if code == "NoSuchBucket":
            raise BucketNotFoundError(bucket) from exc
        if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            raise StorageAccessError(
                f"Access denied to '{bucket}/{file}' ({code}). "
                f"Check MINIO_ACCESS_KEY / MINIO_SECRET_KEY and bucket policy.",
                bucket=bucket,
                file=file,
                s3_code=code,
            ) from exc
        raise StorageAccessError(
            f"MinIO error '{code}' on '{bucket}/{file}': {exc}.",
            bucket=bucket,
            file=file,
            s3_code=code,
        ) from exc

    # ---- API publique ------------------------------------------------------

    def read_header_columns(self, bucket: str, file: str) -> tuple[list[str], str]:
        """Renvoie (noms de colonnes, séparateur) en ne lisant que l'en-tête du fichier.

        Utilise une requête HTTP range (les 256 premiers Ko), donc l'objet complet
        n'est jamais téléchargé — c'est ce qui garde ``GET /columns`` rapide sur les
        gros fichiers.
        """
        with tracer.start_as_current_span("storage.read_header") as span:
            span.set_attribute("arteci.bucket", bucket)
            span.set_attribute("arteci.file", file)
            self._ensure_exists(bucket, file)
            response = None
            try:
                response = self._client.get_object(
                    bucket, file, offset=0, length=256 * 1024
                )
                sample = response.read()
            except S3Error as exc:
                self._map_s3_error(exc, bucket, file)
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()

            separator = detect_separator(sample, self._settings.csv_separator_or_none)
            text = sample.decode("utf-8", errors="replace").lstrip(_BOM)
            lines = text.splitlines()
            if not lines:
                return [], separator
            # Parse uniquement la ligne d'en-tête avec Polars pour respecter le quoting CSV.
            header_df = pl.read_csv(
                lines[0].encode("utf-8"),
                separator=separator,
                has_header=True,
                infer_schema_length=0,
            )
            columns = [_strip_bom(c) for c in header_df.columns]
            span.set_attribute("arteci.columns_count", len(columns))
            return columns, separator

    def process_date_columns(
        self,
        bucket: str,
        file: str,
        date_columns: list[str],
        date_formats: list[str],
    ) -> ProcessResult:
        """Download -> normalisation (par batch) -> upload en place -> aperçu.

        Renvoie les ``PREVIEW_ROWS`` premières lignes du fichier **après** la
        transformation complète, comme l'exige la règle métier.
        """
        settings = self._settings
        self._ensure_exists(bucket, file)

        tmp_in = _new_tempfile(suffix=".in.csv")
        tmp_out = _new_tempfile(suffix=".out.csv")
        try:
            # 1. Streame l'objet vers le disque local (RAM bornée).
            with tracer.start_as_current_span("storage.download") as span:
                span.set_attribute("arteci.bucket", bucket)
                span.set_attribute("arteci.file", file)
                try:
                    self._client.fget_object(bucket, file, tmp_in)
                except S3Error as exc:
                    self._map_s3_error(exc, bucket, file)
                span.set_attribute("arteci.bytes", os.path.getsize(tmp_in))

            separator = _sniff_file_separator(tmp_in, settings.csv_separator_or_none)

            # 2. Vérifie que les colonnes demandées existent vraiment.
            available = [
                _strip_bom(c)
                for c in pl.read_csv(
                    tmp_in, separator=separator, n_rows=0, infer_schema_length=0
                ).columns
            ]
            _check_columns(date_columns, available)

            # 3. Transforme par batchs à mémoire bornée. Toutes les colonnes sont
            #    lues en Utf8, donc les colonnes non-dates font un aller-retour à
            #    l'octet près et seules les colonnes de dates sont réécrites. Un seul
            #    batch est en mémoire à la fois => RAM ~constante quelle que soit la
            #    taille du fichier.
            rename = _bom_aware_rename(date_columns, available)
            with tracer.start_as_current_span("normalize.transform") as span:
                span.set_attribute("arteci.date_columns", ",".join(date_columns))
                span.set_attribute("arteci.date_formats", ",".join(date_formats))
                span.set_attribute("arteci.batch_rows", settings.processing_batch_rows)
                row_count, unparsed = _normalize_csv_batched(
                    tmp_in,
                    tmp_out,
                    separator,
                    rename,
                    date_formats,
                    batch_rows=settings.processing_batch_rows,
                )
                span.set_attribute("arteci.row_count", row_count)
                span.set_attribute("arteci.unparsed_cells", unparsed)

            # 4. Ré-upload le fichier traité sur la MÊME clé (en place).
            with tracer.start_as_current_span("storage.upload") as span:
                span.set_attribute("arteci.bucket", bucket)
                span.set_attribute("arteci.file", file)
                content_type = "text/csv"
                try:
                    self._client.fput_object(
                        bucket, file, tmp_out, content_type=content_type
                    )
                except S3Error as exc:
                    self._map_s3_error(exc, bucket, file)
                span.set_attribute("arteci.bytes", os.path.getsize(tmp_out))

            # 5. Lit l'aperçu post-traitement (peu coûteux : seulement N lignes).
            preview_df = pl.read_csv(
                tmp_out,
                separator=separator,
                n_rows=settings.preview_rows,
                infer_schema_length=0,
            )
            preview_df.columns = [_strip_bom(c) for c in preview_df.columns]
            return ProcessResult(
                preview=preview_df.to_dicts(),
                row_count=row_count,
                columns=preview_df.columns,
                separator=separator,
            )
        finally:
            _safe_remove(tmp_in)
            _safe_remove(tmp_out)


# --------------------------------------------------------------------------
# helpers au niveau module
# --------------------------------------------------------------------------


def _new_tempfile(suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="arteci-", suffix=suffix)
    os.close(fd)  # Polars/MinIO rouvrent par chemin ; on ferme notre handle (sûr sous Windows).
    return path


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _sniff_file_separator(path: str, override: str | None) -> str:
    if override:
        return override
    with open(path, "rb") as fh:
        return detect_separator(fh.read(256 * 1024), override)


def _check_columns(requested: list[str], available: list[str]) -> None:
    missing = [c for c in requested if c not in available]
    if missing:
        raise ColumnNotFoundError(missing, available)


def _bom_aware_rename(date_columns: list[str], actual_names: list[str]) -> list[str]:
    """Fait correspondre les noms demandés (BOM retiré) aux vrais noms de l'en-tête.

    Polars peut garder un BOM sur le nom de la première colonne ; l'utilisateur, lui,
    passe des noms propres.
    """
    lookup = {_strip_bom(n): n for n in actual_names}
    return [lookup.get(c, c) for c in date_columns]


_OUTPUT_RE = r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}$"


def _normalize_csv_batched(
    in_path: str,
    out_path: str,
    separator: str,
    date_columns: list[str],
    date_formats: list[str],
    batch_rows: int,
) -> tuple[int, int]:
    """Lit -> normalise -> écrit le CSV en append, un batch à la fois.

    Renvoie ``(row_count, unparsed_cell_count)``. La RAM pic est bornée par un seul
    batch parce qu'on ne matérialise jamais tout le fichier : ``read_csv_batched``
    fournit des chunks de taille fixe, et chaque chunk transformé est écrit en append
    dans la sortie avant de lire le suivant. Les colonnes non-dates sont lues/écrites
    en Utf8, donc elles ressortent inchangées.
    """
    reader = pl.read_csv_batched(
        in_path,
        separator=separator,
        infer_schema_length=0,
        has_header=True,
        batch_size=batch_rows,
    )
    exprs: list[pl.Expr] | None = None
    actual_cols: list[str] = date_columns
    row_count = 0
    unparsed = 0
    first = True

    with open(out_path, "wb") as out_fh:
        while True:
            batches = reader.next_batches(4)
            if not batches:
                break
            for df in batches:
                if exprs is None:
                    actual_cols = _bom_aware_rename(date_columns, df.columns)
                    exprs = normalize_columns(actual_cols, date_formats)
                df = df.with_columns(exprs)
                row_count += df.height
                unparsed += _count_unparsed(df, actual_cols)
                df.write_csv(out_fh, separator=separator, include_header=first)
                first = False
    return row_count, unparsed


def _count_unparsed(df: pl.DataFrame, date_columns: list[str]) -> int:
    """Compte les cellules de date non vides qui NE sont PAS au format de sortie normalisé.

    Vérification chaîne peu coûteuse sur le batch déjà transformé (une métrique
    d'observabilité utile : combien de cellules ont été gardées telles quelles parce
    qu'elles n'ont pas pu être parsées).
    """
    total = 0
    for col in date_columns:
        s = df.get_column(col)
        non_empty = s.is_not_null() & (s.str.strip_chars() != "")
        matched = s.str.contains(_OUTPUT_RE)
        total += int((non_empty & ~matched.fill_null(False)).sum())
    return total


@lru_cache
def get_storage() -> StorageService:
    """Service de stockage mis en cache (un seul client MinIO par process)."""
    return StorageService(get_settings())
