"""Configuration de l'application.

Tous les réglages viennent de variables d'environnement (12-factor). Ça garde
l'image Docker immuable et l'API stateless, ce qui permet de scaler horizontalement
(plusieurs réplicas K8s derrière un HPA) sans aucun état local partagé.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Réglages typés et validés, chargés une seule fois au démarrage depuis l'env / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- API server ----
    app_name: str = "arteci-date-api"
    app_env: Literal["local", "production"] = "local"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ---- MinIO ----
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_region: str | None = None

    # ---- Traitement ----
    preview_rows: int = 100
    # Chaîne vide => on auto-détecte le délimiteur depuis l'en-tête du fichier.
    csv_separator: str = ""
    # Nombre de lignes par batch pour les gros fichiers. Borne la RAM pic : un seul
    # batch est en mémoire à la fois, quelle que soit la taille totale du fichier.
    processing_batch_rows: int = 500_000

    # ---- Test data ----
    test_data_dir: str = "./tests/data"

    # ---- OpenTelemetry ----
    otel_enabled: bool = True
    otel_service_name: str = "arteci-date-api"
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_exporter_otlp_protocol: Literal["grpc", "http/protobuf"] = "grpc"
    otel_exporter_otlp_insecure: bool = True
    otel_resource_attributes: str = Field(default="")

    @property
    def csv_separator_or_none(self) -> str | None:
        """Renvoie le séparateur configuré, ou ``None`` pour déclencher l'auto-détection."""
        return self.csv_separator if self.csv_separator else None


@lru_cache
def get_settings() -> Settings:
    """Accès en singleton mis en cache : les réglages ne sont parsés qu'une seule fois."""
    return Settings()
