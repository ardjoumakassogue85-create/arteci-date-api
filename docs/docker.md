# Guide — Conteneurisation de l'API (Docker)

>  Ce document explique le *pourquoi*

## Objectifs
Une image petite, sécurisée, reproductible et cloud-native, publiée sur DockerHub :
- build **multi-stage** (image finale petite, sans la toolchain de compilation),
- exécution en utilisateur **non-root**,
- dépendances installées depuis le `requirements.txt` épinglé,
- un `HEALTHCHECK` qui interroge `/health`,
- un `.dockerignore` strict (déjà fourni).

## Image de base : laquelle, et le piège Polars
On part sur **`python:3.12-slim`** (Debian), *pas* Alpine.

- Polars fournit des wheels **manylinux** (glibc). Alpine est en **musl**, donc sur
  Alpine pip ne trouve pas de wheel ou en tire un build musl, et dans le pire des cas on
  finit par compiler du Rust depuis les sources — builds lents et images fragiles.
- `slim` est assez petite et marche directement avec les wheels Polars/pydantic/grpc.
- Côté CPU : le wheel Polars par défaut a besoin d'AVX2. Si le CPU runtime est plus
  ancien (ou si on voit `Illegal instruction` à l'import), il faut utiliser
  `polars-lts-cpu` à la place de `polars` dans `requirements.txt`. Pour la machine cible
  ARTECI (Alpine 3.22, 4 CPU) : s'il faut vraiment tourner sur Alpine au runtime, mieux
  vaut quand même une image runtime Debian-slim.

## Build multi-stage (`Dockerfile` de référence)
```dockerfile
# ---------- Stage 1 : builder ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Installe les deps dans un préfixe isolé, copiable tel quel dans le runtime.
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---------- Stage 2 : runtime ----------
FROM python:3.12-slim AS runtime
# Utilisateur non-root
RUN groupadd -r app && useradd -r -g app -d /app -s /usr/sbin/nologin app
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
# Copie les deps dans /usr/local (préfixe par défaut de Python : déjà sur le PATH
# ET sur sys.path) -> les binaires (gunicorn...) ET leurs packages sont trouvés.
COPY --from=builder /install /usr/local
COPY app/ ./app/
# Dossier scratch pour les CSV temporaires pendant le traitement (writable par non-root)
RUN mkdir -p /tmp/arteci && chown -R app:app /app /tmp/arteci
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
# gunicorn avec workers uvicorn : serveur ASGI de production
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", "-b", "0.0.0.0:8000", "--timeout", "300"]
```

### Pourquoi ces choix
- **Deux stages** : le compilateur et le cache pip restent dans `builder` ; le runtime
  ne reçoit que les site-packages + le code, donc plus petit et moins de CVE.
- **`--prefix=/install`** dans le builder, puis `COPY … /usr/local` au runtime : les
  packages atterrissent dans le `site-packages` par défaut (sur `sys.path`), donc
  binaires + modules sont trouvés sans `pip` ni `PYTHONPATH` custom au runtime.
- **Utilisateur non-root `app`** : défense en profondeur, et beaucoup de clusters
  l'exigent.
- **`--timeout 300`** sur gunicorn : le fichier de 931 Mo prend ~65 s, on ne veut pas
  tuer le worker en plein milieu de la requête. À ajuster selon le plus gros fichier
  (ou passer à des workers async, voir `docs/kubernetes.md`).
- **`-w 4`** workers : correspond à la cible 4 CPU, chaque worker est stateless.
- **HEALTHCHECK** : les orchestrateurs peuvent ainsi redémarrer un conteneur unhealthy.

## Build, run, push
```bash
# Build (tag avec votre user/repo DockerHub)
docker build -t <user-dockerhub>/arteci-date-api:1.0.0 -t <user-dockerhub>/arteci-date-api:latest .

# Run en local (pointez l'env vers votre MinIO/SigNoz)
docker run --rm -p 8000:8000 --env-file .env <user-dockerhub>/arteci-date-api:1.0.0

# Login + push vers DockerHub
docker login -u <user-dockerhub>
docker push <user-dockerhub>/arteci-date-api:1.0.0
docker push <user-dockerhub>/arteci-date-api:latest
```

## Checklist taille / hygiène
- [ ] `.dockerignore` exclut `.venv`, `tests/data/*.csv`, `.git`, `docs/` (fait).
- [ ] Aucun secret en dur — passés via `--env-file`/Secret K8s au runtime.
- [ ] Épingler l'image de base par digest en production (`python:3.12-slim@sha256:...`).
- [ ] Scan optionnel : `docker scout cves <image>` ou Trivy (voir `docs/cicd-github-actions.md`).
- [ ] Vérifier l'exécution non-root : `docker run --rm <image> id` → `uid=…(app)`.
