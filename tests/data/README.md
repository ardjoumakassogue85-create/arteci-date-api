# Données de test

On dépose ici les **fichiers CSV réels d'ARTECI**. Ils sont volontairement git-ignorés
(voir `.gitignore`) parce qu'ils sont volumineux et n'ont pas vocation à être commités :

```
tests/data/lst_of_users_anon_1.csv   (~28 Mo,  320 399 lignes)
tests/data/lst_of_users_anon_2.csv   (~182 Mo, 2 119 517 lignes)
tests/data/lst_of_users_anon_3.csv   (~931 Mo, 10 799 773 lignes)
```

Les tests et `scripts/benchmark.py` cherchent ces fichiers dans cet ordre :

1. `$TEST_DATA_DIR/<nom>` (variable d'env, par défaut `./tests/data`)
2. `./tests/data/<nom>`
3. la **racine du projet** `./<nom>`

On peut donc aussi simplement déposer les fichiers à la racine du dépôt. Les tests qui
ont besoin d'un fichier réel sont **skippés** (pas en échec) quand il est absent, donc la
suite tourne quand même en CI sans les données.

> Ne renommez pas les fichiers : les colonnes de dates utilisées par défaut par les
> tests et le benchmark sont `DATE_CREATION`, `DATE_DESACTIVATION` et
> `DATE_DERNIERE_CONNECTION_1` (toutes en `MDY`).
