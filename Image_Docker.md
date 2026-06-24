# Image Docker — ARTECI API de standardisation des dates

[![Docker Image](https://img.shields.io/docker/v/ardjouma/arteci-date-api?sort=semver&logo=docker&label=docker)](https://hub.docker.com/r/ardjouma/arteci-date-api)
[![Image Size](https://img.shields.io/docker/image-size/ardjouma/arteci-date-api/latest?logo=docker&label=taille)](https://hub.docker.com/r/ardjouma/arteci-date-api)

L'image est construite et publiée **automatiquement sur DockerHub** par la CI
(GitHub Actions) à chaque push sur `main` :

**[`ardjouma/arteci-date-api`](https://hub.docker.com/r/ardjouma/arteci-date-api)**

## Récupérer l'image

```bash
docker pull ardjouma/arteci-date-api:latest
```

## Lancer le conteneur

Toute la configuration passe par des variables d'environnement (voir
[`.env.example`](.env.example)) :

```bash
# Avec un fichier .env (copié depuis .env.example puis ajusté)
docker run --rm -p 8000:8000 --env-file .env ardjouma/arteci-date-api:latest
```

Ou en passant les variables directement (pointez vers votre MinIO / SigNoz) :

```bash
docker run --rm -p 8000:8000 \
  -e MINIO_ENDPOINT=mon-minio:9000 \
  -e MINIO_ACCESS_KEY=xxx \
  -e MINIO_SECRET_KEY=yyy \
  -e OTEL_ENABLED=true \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://mon-signoz:4317 \
  ardjouma/arteci-date-api:latest
```

Une fois lancée :
- API : http://localhost:8000
- Swagger : http://localhost:8000/docs
- Santé : http://localhost:8000/health

## Tags disponibles

| Tag | Description |
|---|---|
| `latest` | Dernier build de la branche `main`. |
| `sha-<commit>` | Image traçable à un commit précis (utile pour debug en prod). |
| `1.0.0` (semver) | Publié quand on pose un tag git `v1.0.0` (release immuable). |

## Variables d'environnement principales

| Variable | Rôle | Défaut |
|---|---|---|
| `MINIO_ENDPOINT` | host:port de MinIO (sans `http://`) | `localhost:9000` |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | Identifiants MinIO | `minioadmin` |
| `MINIO_SECURE` | `true` => HTTPS/TLS | `false` |
| `PROCESSING_BATCH_ROWS` | Lignes par batch (borne la RAM) | `500000` |
| `OTEL_ENABLED` | Active l'export des traces/logs | `true` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector OTLP de SigNoz | `http://localhost:4317` |

> Liste complète et commentée dans [`.env.example`](.env.example).

## Image

- Base **`python:3.12-slim`**, build **multi-stage**, exécution en utilisateur **non-root**.
- `HEALTHCHECK` intégré sur `/health`.
- Détails de construction : [`docs/docker.md`](docs/docker.md).
