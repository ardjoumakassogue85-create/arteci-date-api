# Guide — CI/CD avec GitHub Actions

>  Objectif : automatiser **test → build → publication sur DockerHub** à chaque push,
> avec chaque étape justifiée. Créez `.github/workflows/ci.yml`.

## Déclencheurs
```yaml
on:
  push:
    branches: [main]
    tags: ["v*"]          # les tags de version pilotent les images de release
  pull_request:
    branches: [main]
```
- Sur une **PR** : lint + tests seulement (feedback rapide, pas de publication).
- Sur un **push sur `main`** : en plus, build & push d'une image `:latest` + `:sha`.
- Sur un **tag `v*`** : publication de l'image de release semver (`:1.0.0`).

## Secrets de dépôt à configurer
| Secret | Usage |
|---|---|
| `DOCKERHUB_USERNAME` | Login DockerHub |
| `DOCKERHUB_TOKEN` | **Token d'accès** DockerHub (pas le mot de passe) pour `docker/login-action` |

## Les étapes du pipeline

### 1. Lint + tests (le garde-fou de tout)
```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: "pip" }
      - run: pip install -r requirements-dev.txt
      - run: ruff check app tests           # style/lint — échoue vite, peu coûteux
      - run: pytest -q                       # tests des règles métier (tests données réelles auto-skippés)
```
Le but : ne jamais publier une image dont les tests échouent. La suite tourne **sans les
gros CSV** (ces tests-là sont skippés), donc la CI reste rapide et autonome, et le lint
garde le code cohérent.

### 2. Build (et scan) de l'image
```yaml
  build:
    needs: test
    runs-on: ubuntu-latest
    permissions: { contents: read }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3        # BuildKit : cache + multi-arch
      - uses: docker/build-push-action@v6
        with:
          context: .
          load: true                                # build local d'abord (pour le scan)
          tags: arteci-date-api:ci
          cache-from: type=gha
          cache-to: type=gha,mode=max
      - name: Scan image (Trivy)
        uses: aquasecurity/trivy-action@0.24.0
        with: { image-ref: arteci-date-api:ci, severity: "HIGH,CRITICAL", exit-code: "1" }
```
Le cache de layers BuildKit (`type=gha`) accélère les rebuilds, et **Trivy** bloque la
release si l'image traîne des CVE HIGH/CRITICAL — ce qui compte d'autant plus que
l'image est publique sur DockerHub.

### 3. Login + tag + push (uniquement sur main/tags)
```yaml
  publish:
    needs: build
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
      - name: Dériver les tags
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ secrets.DOCKERHUB_USERNAME }}/arteci-date-api
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=sha,prefix=sha-                 # traçabilité : image == commit
            type=semver,pattern={{version}}      # v1.0.0 -> :1.0.0
      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```
La logique des tags :
- `sha-<commit>` permet de retrouver l'image exacte d'un commit (debug en prod).
- `latest` est pratique pour le dev/compose (uniquement depuis `main`).
- `:1.0.0` (semver), posé depuis les tags git, donne des artefacts de release immuables
  et promouvables.

## Ce qu'on pourrait ajouter
- **Concurrency** pour annuler les runs obsolètes : `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }`.
- **Multi-arch** (`platforms: linux/amd64,linux/arm64`) si on déploie sur ARM — les
  wheels Polars existent pour les deux, mais le build est plus lent.
- **SBOM/provenance** : `provenance: true` + `sbom: true` sur `build-push-action` pour
  l'attestation de chaîne d'approvisionnement.
- Un job **deploy** séparé (`workflow_dispatch` manuel ou sur tag) qui lance
  `kubectl set image` / upgrade Helm sur le cluster, via un secret `KUBE_CONFIG`.

## Modèle mental
```
PR :        checkout → lint → pytest
push main : …test… → build → trivy → login → tag(sha,latest) → push
tag v* :    …test… → build → trivy → login → tag(semver)       → push → (deploy)
```
