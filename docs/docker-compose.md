# Guide — Stack locale avec Docker Compose

L'idée ici : un seul `docker compose up` qui démarre **l'API + MinIO (avec les buckets
`raw`/`processeddata`) + SigNoz**, le tout déjà câblé ensemble. Plus bas, un
`docker-compose.yml` de référence commenté, et les raisons des choix.

## Ce qui tourne, et comment c'est connecté
```
            ┌──────────────┐      OTLP 4317       ┌────────────────────┐
 client ──▶ │  arteci-api  │ ───────────────────▶ │ SigNoz (collector  │
            │   :8000      │                       │  + UI :3301)       │
            └──────┬───────┘                       └────────────────────┘
                   │ API S3 :9000
                   ▼
            ┌──────────────┐   console :9001
            │    MinIO     │
            │ raw,         │
            │ processeddata│
            └──────────────┘
```
- L'API parle à MinIO via le réseau Docker interne (`minio:9000`).
- L'API envoie sa télémétrie à SigNoz en OTLP gRPC (`signoz-otel-collector:4317`).
- Un conteneur one-shot `mc` (MinIO Client) crée les buckets au démarrage.

## À propos de SigNoz
SigNoz est lui-même une stack multi-conteneurs (ClickHouse, query-service,
otel-collector, frontend). Le plus simple est de passer par leur compose officiel :
```bash
git clone -b main https://github.com/SigNoz/signoz.git
cd signoz/deploy/docker            # le compose officiel est ici
docker compose up -d               # UI SigNoz sur http://localhost:3301
```
Deux options propres se présentent :
1. Celle qu'on recommande : lancer SigNoz depuis son propre compose (ci-dessus) et
   mettre notre API sur le **même réseau Docker** (`networks: [signoz-net]` +
   `external: true`). Ça garde notre fichier petit et ça réutilise la topologie déjà
   testée de SigNoz.
2. Inliner soi-même un sous-ensemble minimal de SigNoz — plus fragile, à éviter.

## `docker-compose.yml` de référence (API + MinIO + init des buckets)
```yaml
services:
  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ["9000:9000", "9001:9001"]
    volumes: ["minio-data:/data"]
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks: [arteci]

  # One-shot : crée les buckets raw/processeddata, puis se termine.
  minio-init:
    image: minio/mc:latest
    depends_on:
      minio: {condition: service_healthy}
    entrypoint: >
      /bin/sh -c "
      mc alias set local http://minio:9000 minioadmin minioadmin &&
      mc mb --ignore-existing local/raw &&
      mc mb --ignore-existing local/processeddata &&
      echo 'buckets ready'"
    networks: [arteci]

  api:
    build: .
    image: arteci-date-api:local
    depends_on:
      minio: {condition: service_healthy}
    environment:
      MINIO_ENDPOINT: "minio:9000"
      MINIO_ACCESS_KEY: "minioadmin"
      MINIO_SECRET_KEY: "minioadmin"
      MINIO_SECURE: "false"
      OTEL_ENABLED: "true"
      OTEL_SERVICE_NAME: "arteci-date-api"
      # Si SigNoz tourne dans son propre compose, pointez vers ce collector et joignez son réseau.
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://signoz-otel-collector:4317"
      OTEL_EXPORTER_OTLP_PROTOCOL: "grpc"
    ports: ["8000:8000"]
    networks: [arteci, signoz-net]

volumes:
  minio-data:

networks:
  arteci:
  # Fourni par le compose SigNoz ; marqué external pour s'y rattacher.
  signoz-net:
    external: true
    name: signoz_default     # vérifiez le vrai nom du réseau : `docker network ls`
```

## Démarrer / vérifier
```bash
# 1. (optionnel) démarrer SigNoz depuis son dépôt d'abord (crée le réseau signoz_default)
# 2. puis :
docker compose up -d --build
# uploadez un fichier de test dans le bucket raw ou processeddata via la console http://localhost:9001
# 3. testez l'API
curl "http://localhost:8000/columns?bucket=processeddata&file=lst_of_users_anon_1.csv"
```

## Quelques astuces
- Pas besoin de SigNoz en local ? Mettez `OTEL_ENABLED=false` : l'API continue de
  logger du JSON structuré sur stdout (`docker compose logs -f api`).
- Le volume garde les données MinIO entre deux runs. `docker compose down -v` les
  efface — à n'utiliser que si on veut repartir de zéro.
- Les noms de buckets `raw` et `processeddata` collent à l'architecture ARTECI (brut
  d'un côté, traité de l'autre). Comme `POST /processDate` réécrit **en place**, un
  fichier traité reste dans `processeddata` et y est écrasé.
