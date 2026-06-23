"""Point d'entrée de l'application FastAPI : télémétrie, routes, handlers d'erreurs."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.telemetry import setup_telemetry


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ARTECI – API de standardisation des dates",
        version="1.0.0",
        description=(
            "Standardisation haute performance des formats de date pour ARTECI. "
            "Normalise les dates DMY/MDY/mixtes/ISO/timestamp au format "
            "`DD-MM-YYYY HH:mm:ss`, réécrit le fichier en place dans MinIO, "
            "et renvoie un aperçu de 100 lignes."
        ),
    )

    # La télémétrie doit être en place avant de servir des requêtes (instrumente aussi l'app).
    setup_telemetry(app, settings)
    register_exception_handlers(app)
    app.include_router(router)
    return app


app = create_app()
