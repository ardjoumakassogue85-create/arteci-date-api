# ARTECI — API de standardisation des dates

Cette API règle un problème précis du pipeline d'ingestion ARTECI : les colonnes de
dates n'arrivent jamais dans un format unique. Elle prend un fichier déjà déposé dans
MinIO, normalise les colonnes de dates (anglais MDY, français DMY, les deux mélangés,
ISO, timestamps, mois écrits en toutes lettres), réécrit le fichier **au même endroit
dans MinIO**, et renvoie les 100 premières lignes pour une dernière vérification à
l'œil. Le tout doit tenir sur des fichiers de plusieurs Go avec seulement 4 CPU et
8 Go de RAM.

Le format de sortie est imposé par ARTECI : `JJ-MM-AAAA HH:mm:ss`
(soit `DD-MM-YYYY HH:mm:ss`, par exemple `17-07-2019 00:00:00`).

---

## 1. Le problème

Au moment de l'ingestion, chaque colonne est validée. Les colonnes de dates sont la
partie pénible : d'un fichier à l'autre, et parfois d'une ligne à l'autre dans la
même colonne, le format change. On peut avoir du `MM/DD/YYYY` (US) sur une ligne et
du `DD/MM/YYYY` (FR) sur la suivante.

Au moment où l'API entre en jeu, l'utilisateur a déjà indiqué côté frontend quelles
colonnes sont des dates et quelle est leur orientation *dominante* (`DMY` ou `MDY`).
L'API arrive donc tout à la fin de la validation : elle reçoit ces indications,
normalise, réécrit le fichier dans MinIO, et renvoie les 100 premières lignes traitées.

### Où l'API s'insère dans le flux

```
frontend ──upload──▶ API ──▶ MinIO (raw)
frontend ──valide colonnes/formats──▶ [CETTE API : POST /processDate]
                                        │ lecture du fichier MinIO (streamé sur disque)
                                        │ normalisation des colonnes dates (Polars, par batch)
                                        │ réécriture sur le MÊME bucket/clé (en place)
                                        ▼
                                100 lignes traitées ──▶ frontend
```

---

## 2. Choix techniques

Voici ce qu'on a retenu et pourquoi.

| Préoccupation | Choix | Pourquoi |
|---|---|---|
| Langage | **Python 3.12** | Écosystème data riche ; avec FastAPI + Polars on a de la vitesse native là où ça compte tout en gardant le code de liaison lisible. |
| Moteur de données | **Polars** (Rust, multi-threadé) | `str.to_datetime` est vectorisé et exploite les 4 CPU, et le lecteur par batch garde la mémoire sous contrôle sur les gros fichiers. On a mesuré 3,4 à 4,3× plus rapide et ~2× moins de mémoire que pandas (voir §6 et `docs/benchmark.md`). |
| Framework API | **FastAPI** | Async, validation Pydantic, OpenAPI/Swagger générés tout seuls, et l'instrumentation OpenTelemetry s'y branche proprement. |
| Stockage objet | **SDK MinIO** | `fget_object`/`fput_object` streament vers/depuis le disque (RAM bornée), et les requêtes HTTP range permettent de ne lire que l'en-tête pour `GET /columns`. |
| Observabilité | **OpenTelemetry → OTLP → SigNoz** | Des traces sur tout le cycle de la requête, plus des logs JSON corrélés par `trace_id`/`span_id`. |
| Serveur | **uvicorn/gunicorn** | Les endpoints synchrones tournent dans un threadpool, donc le gros travail Polars ne bloque pas la boucle d'événements ; l'API est stateless, on peut scaler horizontalement. |

### L'algorithme de normalisation (le cœur du projet)

Pour chaque colonne de dates, on enchaîne plusieurs passes de `coalesce` vectorisé.
Chaque passe tente un format strict sur toute la colonne (en Rust, multi-threadé) et
renvoie `null` quand ça ne colle pas ; la première valeur non-nulle l'emporte. C'est
exactement ce qui permet de gérer les formats mixtes dans une même colonne :

1. **Passe 1 — l'orientation indiquée par l'utilisateur.** On parse les valeurs
   ambiguës (`01/02/2023`) comme demandé, plus les valeurs non ambiguës de cette
   orientation.
2. **Passe 2 — l'orientation opposée.** `25/12/2023` échoue en MDY (mois 25) donc on
   le rattrape en DMY ; `12/25/2023` échoue en DMY donc on le rattrape en MDY. À
   retenir : une date non ambiguë tombe juste quelle que soit l'orientation indiquée ;
   le hint ne sert qu'à départager les cas vraiment ambigus.
3. **Passes suivantes** — ISO 8601 (avec `T`, `Z`, fuseaux), mois en toutes lettres
   FR/EN (et jour de semaine en préfixe), 12h AM/PM, timestamps Unix (s/ms).
4. Tout ce qui n'est parsé par aucune passe est **gardé tel quel** : c'est la règle
   métier, une cellule mal formée ne bloque ni la colonne ni le fichier, on la renvoie
   à l'identique.
5. Les valeurs parsées sont reformatées en `DD-MM-YYYY HH:mm:ss` (heure à `00:00:00`
   quand la source n'en a pas).

Les colonnes qui ne sont pas des dates sont lues et réécrites en texte : elles font un
aller-retour à l'octet près, seules les colonnes de dates bougent. Pour les années à
2 chiffres on applique le pivot de siècle POSIX (00–68 → 2000s, 69–99 → 1900s), traité
au niveau chaîne pour aller vite.

### Pourquoi la mémoire reste bornée

`POST /processDate` streame l'objet dans un fichier temporaire, puis le traite avec
`read_csv_batched` (`PROCESSING_BATCH_ROWS=500000` par défaut). Il n'y a qu'un seul
batch en mémoire à la fois, donc la RAM pic reste à peu près constante quelle que soit
la taille du fichier, avant de ré-uploader le résultat sur la même clé. On a pris le
batching explicite parce que le `sink_csv` streaming de Polars 1.18 ne gère pas encore
ces expressions de dates — et de toute façon c'est plus simple à raisonner pour la
contrainte mémoire (voir Limites connues).

---

## 3. Démarrage rapide (local, sans Docker)

> Pour la stack complète en local (API + MinIO + SigNoz) via Docker Compose, suis
> [`docs/docker-compose.md`](docs/docker-compose.md). Ci-dessous, on lance juste l'API
> avec un venv Python.

```bash
# 1. Créer un venv et installer les deps (Python 3.12 recommandé ; 3.11–3.13 OK)
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash ;  ./.venv/bin/activate sous Linux/mac
pip install -r requirements.txt      # ou requirements-dev.txt pour tests + benchmark

# 2. Configurer
cp .env.example .env                 # puis éditer les paramètres MinIO + OTLP

# 3. Lancer l'API
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# Swagger UI :  http://localhost:8000/docs
```

Pour les CSV de test, dépose `lst_of_users_anon_*.csv` dans `tests/data/` (ou à la
racine du dépôt, ou pointe `TEST_DATA_DIR` dessus). Détails dans
[`tests/data/README.md`](tests/data/README.md).

Lancer les tests :
```bash
pytest                  # 56 tests ; ceux sur données réelles sont skippés si les fichiers sont absents
```

Lancer le benchmark :
```bash
python scripts/benchmark.py --file lst_of_users_anon_1.csv
```

Toute la configuration passe par des variables d'environnement, listées dans
[`.env.example`](.env.example).

---

## 4. Endpoints

### `GET /columns`
Liste les colonnes d'un fichier sans le télécharger (requête HTTP range sur l'en-tête).

```bash
curl "http://localhost:8000/columns?bucket=processeddata&file=lst_of_users_anon_1.csv"
```
```json
{
  "bucket": "processeddata",
  "file": "lst_of_users_anon_1.csv",
  "columns": ["CODE_LOGIN","LOGIN","NOM_PRENOM","PROFIL","DATE_CREATION",
              "ETAT_USER","DATE_DESACTIVATION","DATE_DERNIERE_CONNECTION_1","SITE_AGENCE"],
  "separator": ";"
}
```
Renvoie `404` si le fichier ou le bucket est introuvable.

### `POST /processDate`
Normalise les colonnes de dates, réécrit le fichier en place dans MinIO, et renvoie
les 100 premières lignes **après** le traitement complet.

```bash
curl -X POST "http://localhost:8000/processDate" \
  -H "Content-Type: application/json" \
  -d '{
        "date_columns": ["DATE_CREATION","DATE_DESACTIVATION","DATE_DERNIERE_CONNECTION_1"],
        "date_formats": ["MDY","MDY","MDY"],
        "bucket": "processeddata",
        "file": "lst_of_users_anon_1.csv"
      }'
```
```json
[
  {"CODE_LOGIN":"10000","LOGIN":"user00000001","NOM_PRENOM":"Sam Leroy","PROFIL":"PROFIL_B",
   "DATE_CREATION":"17-07-2019 00:00:00","ETAT_USER":"DESACTIVE",
   "DATE_DESACTIVATION":"19-12-2023 00:00:00","DATE_DERNIERE_CONNECTION_1":"25-08-2023 00:00:00",
   "SITE_AGENCE":"Agence Beta"}
]
```

Les erreurs sont volontairement explicites et actionnables :

| Situation | Statut | `error.code` du body |
|---|---|---|
| Fichier introuvable dans le bucket | `404` | `file_not_found` |
| Bucket introuvable | `404` | `bucket_not_found` |
| Colonne de date absente (on la nomme + on liste les colonnes dispo) | `400` | `column_not_found` |
| Format non supporté / longueurs de listes différentes | `400` | `unsupported_format` |
| Échec d'accès/réseau MinIO | `502` | `storage_access_error` |

### Ce qui s'écarte du contrat brut (et pourquoi)
Le contrat de référence parle d'« une liste de colonnes » et d'« une liste de lignes ».
Pour `GET /columns`, on enveloppe la liste dans un petit objet (on ajoute
`bucket`/`file`/`separator`) parce que c'est plus pratique en interne. En revanche
`POST /processDate` renvoie bien le tableau JSON nu de lignes, exactement comme
spécifié. Les `bucket`/`file` de `/columns` passent en query params.

---

## 5. Observabilité (SigNoz)

- **Traces :** l'auto-instrumentation FastAPI crée le span serveur, et on ajoute des
  spans enfants `storage.read_header` → `storage.download` → `normalize.transform` →
  `storage.upload`, avec des attributs utiles (`arteci.bucket`, `arteci.file`,
  `arteci.row_count`, `arteci.date_columns`, `arteci.unparsed_cells`, nb d'octets,
  taille de batch).
- **Logs :** du JSON structuré sur stdout, chaque ligne avec `severity`, `timestamp`,
  le contexte de la requête et `trace_id`/`span_id`. Quand `OTEL_ENABLED=true`, les
  logs partent aussi vers SigNoz via l'exporteur OTLP logs.
- **Config :** `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`,
  `OTEL_EXPORTER_OTLP_PROTOCOL` (`grpc` ou `http/protobuf`), voir `.env.example`. Côté
  SigNoz : *Services* → `arteci-date-api` pour les traces, et *Logs* filtrés par
  service.

---

## 6. Performance (mesurée)

Sur une machine locale (indicatif ; la cible officielle est Alpine 3.22, 4 CPU / 8 Go) :

| Fichier | Taille | Lignes | Temps | RAM pic | Cible |
|---|---|---|---|---|---|
| anon_1 | 28 Mo | 320 399 | ~3,4 s | 142 Mo | ≲ 20 s ✓ |
| anon_2 | 182 Mo | 2 119 517 | ~17 s | 343 Mo | ≲ 50 s ✓ |
| anon_3 | 931 Mo | 10 799 773 | ~65 s | ~1,3 Go | ≲ 2 min ✓ |

Comparaison des moteurs sur anon_1 (320 399 lignes) :

| Approche | Temps | RAM pic | Lignes/s |
|---|---|---|---|
| **Polars (cette API)** | **3,35 s** | **142 Mo** | **95 564** |
| pandas vectorisé | 11,43 s | 286 Mo | 28 025 |
| pandas ligne-à-ligne | 14,50 s | 281 Mo | 22 100 |

Grâce au batching, la RAM pic reste bornée (très en dessous de la taille du fichier
pour anon_3). Méthodologie et détails dans [`docs/benchmark.md`](docs/benchmark.md).

---

## 7. Déploiement & DevOps (guides à réaliser)

Ces parties sont documentées, pas encore implémentées — les guides expliquent comment
les mettre en place :

- [`docs/docker.md`](docs/docker.md) — image multi-stage optimisée, non-root, DockerHub.
- [`docs/docker-compose.md`](docs/docker-compose.md) — stack locale : API + MinIO (`raw`/`processeddata`) + SigNoz.
- [`docs/kubernetes.md`](docs/kubernetes.md) — Deployment/Service/Config/Secret/HPA/Ingress, stratégie de scaling.
- [`docs/cicd-github-actions.md`](docs/cicd-github-actions.md) — pipeline CI/CD, chaque étape justifiée.
- [`docs/benchmark.md`](docs/benchmark.md) — méthodologie de benchmark, résultats, justification langage/outil.

---

## 8. Structure du projet

```
app/
  main.py                 # app FastAPI, câblage télémétrie, handlers d'erreurs
  api/routes.py           # GET /columns, POST /processDate, /health
  core/
    config.py             # settings par variables d'env (pydantic-settings)
    telemetry.py          # traces OpenTelemetry + logs JSON (OTLP/SigNoz)
    errors.py             # exceptions métier + handlers
  services/
    date_normalizer.py    # logique vectorisée DMY/MDY/mixte/ISO/timestamp/mois
    minio_client.py       # I/O MinIO + traitement par batch à mémoire bornée
  models/schemas.py       # modèles Pydantic requête/réponse
tests/                    # 56 tests (unitaires + API + pipeline + échantillon réel)
scripts/benchmark.py      # Polars vs pandas, temps + mémoire
docs/                     # guides Docker / Compose / K8s / CI-CD / benchmark
```

---

## 9. Limites connues & pistes d'amélioration

- **Sink streaming :** le `sink_csv` streaming de Polars 1.18 ne gère pas les
  expressions de dates, d'où le batching explicite via `read_csv_batched` (mémoire
  bornée, mais en passant par un fichier temporaire sur disque). Une version plus
  récente de Polars, ou un sink Rust maison, supprimerait cet aller-retour disque.
- **Sémantique « en place » :** « en place » veut dire même bucket/clé, implémenté en
  download → transform → ré-upload. Ce n'est pas atomique : un crash en plein upload
  pourrait laisser un objet partiel. Le pattern sûr serait d'écrire sur une clé
  temporaire puis de faire une copie côté serveur (noté comme durcissement futur dans
  `docs/kubernetes.md`).
- **Fuseaux horaires :** les entrées ISO avec fuseau sont normalisées sur leur heure
  murale (on supprime l'offset, on ne le convertit pas en UTC). C'est un choix délibéré
  et documenté ; convertir vers un fuseau fixe ne serait qu'une ligne si besoin.
- **Mois en toutes lettres :** on couvre les formes FR/EN courantes (complètes et en
  3 lettres) ainsi que le jour de semaine en préfixe ; les abréviations exotiques ou
  d'autres locales ne le sont pas. De toute façon les fichiers fournis n'ont aucune date
  écrite en toutes lettres.
- **Très gros fichiers / async :** pour des fichiers bien au-delà de 1 Go ou beaucoup de
  requêtes concurrentes, un pattern de file de workers (soumettre un job → poller son
  statut) découplerait les timeouts HTTP du traitement. C'est esquissé dans
  `docs/kubernetes.md`. Le design synchrone actuel est plus simple et atteint déjà les
  cibles annoncées.
- **Fidélité CSV :** la sortie utilise des fins de ligne `\n` et supprime un BOM UTF-8
  s'il y en a un (Polars le retire à la lecture). Le quoting suit le style « necessary »
  de Polars.
