# Benchmark — méthodologie, résultats & justification de l'outil

Le cahier des charges demande de comparer des outils de différents langages/librairies
pour la normalisation des colonnes de dates, et de justifier celui qu'on garde. Ce
document décrit la méthode, donne les chiffres de `scripts/benchmark.py`, et explique
pourquoi le choix s'est porté sur **Polars**.

## Ce qu'on mesure
`scripts/benchmark.py` fait tourner le *même travail* (normaliser les colonnes de dates
d'un vrai CSV ARTECI en `DD-MM-YYYY HH:mm:ss`) avec plusieurs implémentations, et
reporte :
- le **temps mur** (`time.perf_counter`),
- la **RAM pic** (échantillonnée dans un thread de fond via `psutil`),
- le **débit** (lignes/s).

```bash
python scripts/benchmark.py --file lst_of_users_anon_1.csv
python scripts/benchmark.py --file lst_of_users_anon_2.csv --rows 1000000
python scripts/benchmark.py --approaches polars,pandas-vectorized
```

## Approches comparées
| Nom | Description |
|---|---|
| `polars` | **L'approche retenue.** Parsing vectorisé multi-passes (Rust, multi-threadé), traité par batchs à mémoire bornée via `read_csv_batched` — c'est exactement le chemin de production de l'API. |
| `pandas-vectorized` | `pandas.to_datetime(..., format="mixed", dayfirst=...)` puis `strftime`. C mono-thread ; c'est la baseline « équitable » la plus proche. |
| `pandas-rowwise` | Parse chaque cellule dans une boucle Python en essayant les formats par priorité — représente l'implémentation naïve ligne-à-ligne. |

## Résultats — `lst_of_users_anon_1.csv` (28 Mo, 320 399 lignes, 3 colonnes de dates)
Sur une machine de dev locale (vos chiffres seront différents ; c'est l'ordre
*relatif* qui compte) :

| Approche | Temps | RAM pic | Lignes/s | vs Polars |
|---|---:|---:|---:|---:|
| **polars** | **3,35 s** | **142 Mo** | **95 564** | 1,0× |
| pandas-vectorized | 11,43 s | 286 Mo | 28 025 | 3,4× plus lent |
| pandas-rowwise | 14,50 s | 281 Mo | 22 100 | 4,3× plus lent |

## Résultats fichier complet avec le pipeline de production (Polars)
Mesuré de bout en bout avec le moteur par batch (`PROCESSING_BATCH_ROWS=500000`) :

| Fichier | Taille | Lignes | Temps | RAM pic |
|---|---|---:|---:|---:|
| anon_1 | 28 Mo | 320 399 | ~3,4 s | 142 Mo |
| anon_2 | 182 Mo | 2 119 517 | ~17 s | 343 Mo |
| anon_3 | 931 Mo | 10 799 773 | ~65 s | ~1,3 Go |

Deux choses ressortent :
- Le temps évolue à peu près **linéairement** avec le nombre de lignes — pas
  d'explosion pathologique quand le fichier grossit.
- La RAM pic reste **bornée** : le fichier de 931 Mo plafonne à ~1,3 Go, ce qui n'est
  pas proportionnel à sa taille, parce qu'un seul batch est en mémoire à la fois. Un
  fichier de 5 Go plafonnerait à peu près au même niveau (baisser
  `PROCESSING_BATCH_ROWS` échange de la vitesse contre de la RAM).

## Pourquoi Polars plutôt que les alternatives
- **vs pandas** — 3 à 4× plus rapide et ~2× moins de mémoire ici. pandas est
  mono-thread pour ce travail et copie davantage. Mais le point décisif, c'est que
  `to_datetime(format="mixed")` **n'implémente pas la règle métier ARTECI** : il ne sait
  pas appliquer un *fallback par orientation* à l'intérieur d'une colonne mixte, comme
  le fait notre coalesce hint→opposé→ISO. Polars gagne donc sur **la vitesse ET la
  correction** pour l'exigence des formats mixtes.
- **vs PyArrow** — excellent pour l'I/O colonnaire, mais le parsing de dates avec
  fallback par orientation obligerait à écrire à la main toute la logique multi-passes ;
  Polars la fournit nativement, avec une API d'expressions de haut niveau et un moteur
  multi-threadé par-dessus Arrow.
- **vs DuckDB** — très bon moteur SQL ; `strptime` + `try_cast` pourraient exprimer la
  chose, mais le fallback multi-format devient du SQL verbeux, et glisser un hint
  dynamique par colonne reste plus propre en expressions Polars. DuckDB reste une
  alternative solide à explorer.
- **vs un microservice Go/Rust** — la boucle de parsing serait rapide, mais il faudrait
  ré-implémenter à la main le parsing multi-format vectorisé, le streaming CSV et les
  règles métier. Or Polars *est* déjà du Rust sous le capot : on récupère la vitesse
  native **et** on garde l'API, la validation et l'observabilité dans l'écosystème mûr
  de Python. Si un jour le profilage montrait que c'est la glue Python le goulot (ce
  n'est pas le cas aujourd'hui, le moteur domine), un sink Rust ou un binaire Rust basé
  sur `polars` serait l'étape suivante.

## Conclusion
Polars donne la performance demandée (les trois fichiers passent sous leurs cibles)
avec une **mémoire bornée** et une expression propre de la **règle métier mixte
DMY/MDY**, tout en gardant l'API autour dans une stack Python productive. C'est cette
combinaison — vitesse + sûreté mémoire + correction + maintenabilité — qui a tranché.

## Reproduire
```bash
pip install -r requirements-dev.txt          # ajoute pandas + psutil pour les baselines
python scripts/benchmark.py --file <un des CSV fournis>
```
Ajoutez `--rows N` pour limiter le nombre de lignes (conseillé avant de lancer
`pandas-rowwise` sur anon_2/3).
