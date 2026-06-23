"""Schémas Pydantic requête/réponse (validation + documentation OpenAPI)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProcessDateRequest(BaseModel):
    """Corps de ``POST /processDate``.

    ``date_formats[i]`` est l'orientation dominante indiquée (``DMY`` ou ``MDY``)
    pour ``date_columns[i]`` ; les deux listes doivent avoir la même longueur. La
    validation sémantique (longueurs identiques, valeurs d'indice autorisées) se
    fait dans l'endpoint afin de renvoyer un ``400`` actionnable plutôt qu'un
    ``422`` générique.
    """

    date_columns: list[str] = Field(
        min_length=1,
        description="Colonnes de date à normaliser.",
        examples=[["DATE_CREATION", "DATE_DESACTIVATION", "DATE_DERNIERE_CONNECTION_1"]],
    )
    date_formats: list[str] = Field(
        min_length=1,
        description="Indice d'orientation dominant par colonne ('DMY' ou 'MDY'), même ordre.",
        examples=[["MDY", "MDY", "MDY"]],
    )
    bucket: str = Field(
        min_length=1, description="Bucket MinIO contenant le fichier.", examples=["processeddata"]
    )
    file: str = Field(
        min_length=1,
        description="Clé complète de l'objet dans le bucket.",
        examples=["lst_of_users_anon_1.csv"],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "date_columns": [
                        "DATE_CREATION",
                        "DATE_DESACTIVATION",
                        "DATE_DERNIERE_CONNECTION_1",
                    ],
                    "date_formats": ["MDY", "MDY", "MDY"],
                    "bucket": "processeddata",
                    "file": "lst_of_users_anon_1.csv",
                }
            ]
        }
    }


# La réponse de POST /processDate est, par contrat, un tableau JSON des 100 premières
# lignes traitées. Chaque ligne est un objet {nom_colonne: valeur}.
ProcessedRow = dict[str, Any]


class ColumnsResponse(BaseModel):
    """Réponse de ``GET /columns``."""

    bucket: str
    file: str
    columns: list[str]
    separator: str = Field(description="Délimiteur détecté dans le fichier.")
