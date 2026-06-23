"""Cœur métier : normalisation vectorisée des dates avec Polars.

Contrat de sortie (imposé par ARTECI) : toute date parsable devient la chaîne
``DD-MM-YYYY HH:mm:ss``. L'heure vaut ``00:00:00`` par défaut quand elle est absente.
Une valeur qu'aucun format candidat ne sait parser est **gardée exactement telle quelle**.

Pourquoi un *coalesce multi-passes* plutôt qu'un parsing ligne à ligne
----------------------------------------------------------------------
Pour chaque colonne de dates, on construit, par ordre de priorité, une liste
d'expressions de parsing « candidates ». Chaque candidate tente un format strict sur
**toute** la colonne (vectorisé, multi-threadé, en Rust) et renvoie ``null`` là où ça
ne colle pas (``strict=False``). ``pl.coalesce`` garde ensuite le premier résultat
non-null par ligne.

Cette structure *est* la règle métier des colonnes mixtes DMY/MDY :

* **Passe 1 = l'orientation du hint.** Elle parse les valeurs ambiguës (``01/02/2023``)
  comme l'utilisateur l'a demandé, plus les valeurs non ambiguës de cette orientation.
* **Passe 2 = l'orientation opposée.** Une valeur comme ``25/12/2023`` échoue à la
  passe MDY (mois 25 invalide -> null) et est rattrapée ici en DMY. De même,
  ``12/25/2023`` échoue à la passe DMY et est rattrapée en MDY. Les dates non ambiguës
  sont donc toujours parsées correctement *quel que soit le hint*, le hint ne tranchant
  que les cas réellement ambigus.
* **Passes 3 et suivantes** couvrent l'ISO, les noms de mois et les timestamps Unix.

Tout reste sous forme d'expressions Polars, donc exactement la même logique tourne sans
changement sur chaque batch à mémoire bornée produit par ``read_csv_batched`` (voir
``minio_client._normalize_csv_batched``).
"""

from __future__ import annotations

import polars as pl

from app.core.errors import UnsupportedFormatError

# Format de sortie canonique et un dtype datetime unique, pour que toutes les
# candidates soient compatibles coalesce (même dtype, sans fuseau).
OUTPUT_FORMAT = "%d-%m-%Y %H:%M:%S"
DT = pl.Datetime("us")
SUPPORTED_HINTS = ("DMY", "MDY")

# Noms de mois français + anglais (en minuscules) -> nom anglais complet canonique.
# Utilisé par la branche noms de mois après passage en minuscules et normalisation
# des séparateurs. Le remplacement Aho-Corasick est leftmost-longest, donc "janvier"
# l'emporte sur "jan".
_MONTH_MAP: dict[str, str] = {
    # Janvier
    "janvier": "January", "janv": "January", "jan": "January", "january": "January",
    # Février
    "fevrier": "February", "février": "February", "fevr": "February", "févr": "February",
    "fev": "February", "fév": "February", "feb": "February", "february": "February",
    # Mars
    "mars": "March", "mar": "March", "march": "March",
    # Avril
    "avril": "April", "avr": "April", "apr": "April", "april": "April",
    # Mai
    "mai": "May", "may": "May",
    # Juin
    "juin": "June", "jun": "June", "june": "June",
    # Juillet
    "juillet": "July", "juil": "July", "jul": "July", "july": "July",
    # Août
    "aout": "August", "août": "August", "aug": "August", "august": "August",
    # Septembre
    "septembre": "September", "sept": "September", "sep": "September",
    "september": "September",
    # Octobre
    "octobre": "October", "oct": "October", "october": "October",
    # Novembre
    "novembre": "November", "nov": "November", "november": "November",
    # Décembre
    "decembre": "December", "décembre": "December", "dec": "December",
    "déc": "December", "december": "December",
}


def validate_hint(hint: str) -> str:
    """Renvoie le hint normalisé, ou lève une erreur actionnable."""
    h = hint.strip().upper()
    if h not in SUPPORTED_HINTS:
        raise UnsupportedFormatError(
            f"Unsupported date format hint '{hint}'. "
            f"Supported values are {list(SUPPORTED_HINTS)} (DMY = Day/Month/Year, "
            f"MDY = Month/Day/Year).",
            invalid_format=hint,
            supported=list(SUPPORTED_HINTS),
        )
    return h


def _numeric_formats(order: str) -> list[str]:
    """Formats de date numériques pour une orientation, séparateurs déjà unifiés en '/'.

    ``order`` vaut 'DMY' ou 'MDY' et décide si le jour ou le mois vient en premier.
    """
    if order == "MDY":
        a, b = "%m", "%d"
    else:
        a, b = "%d", "%m"
    # Uniquement des formats à année sur 4 chiffres : les années à 2 chiffres sont
    # étendues à 4 chiffres au niveau chaîne (voir `_expand_two_digit_year`), donc on
    # n'a jamais besoin de `%y`. Ça évite une correction de siècle coûteuse après parse.
    base = f"{a}/{b}/%Y"
    return [
        base,                       # 17/07/2019  (le plus courant : essayé en premier)
        f"{base} %H:%M:%S",        # 17/07/2019 14:30:00
        f"{base} %H:%M",           # 17/07/2019 14:30
        f"{base} %I:%M:%S %p",     # 07/17/2019 02:30:00 PM (12h anglais)
        f"{base} %I:%M %p",        # 07/17/2019 02:30 PM   (12h, sans secondes)
    ]


# Famille ISO 8601 (sans fuseau). Les suffixes de fuseau et les secondes
# fractionnaires sont retirés au préalable, donc on ne produit que des datetimes
# naïfs (pas de conflit de dtype dans coalesce).
# Les formats ISO contiennent tous un '-', donc la branche est conditionnée à la
# présence de '-'. (yyyy/M/d est géré par la branche numérique, qui unifie les
# séparateurs en '/'.)
_ISO_FORMATS: list[str] = [
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %I:%M:%S %p",        # date ISO + heure 12h anglais
]

# Dispositions avec nom de mois, appliquées après passage en minuscules, conversion
# séparateur->espace et suppression du nom de jour (donc "EEEE, MMMM d, yyyy" marche aussi).
_MONTH_FORMATS: list[str] = [
    "%d %B %Y %H:%M:%S",
    "%d %B %Y",
    "%B %d %Y %H:%M:%S",
    "%B %d %Y",
    "%d %B %Y %I:%M:%S %p",        # 15 janvier 2023 02:30:00 PM
    "%B %d %Y %I:%M:%S %p",        # January 15 2023 02:30:00 PM
]

# Noms de jours complets (FR + EN) retirés dans la branche noms de mois. On utilise
# uniquement les noms complets (pas les abréviations à 3 lettres) pour éviter de
# télescoper des tokens de mois comme "mars" (March) vs "mar" (mardi).
_WEEKDAYS = (
    "lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
)


def _parse(expr: pl.Expr, fmt: str) -> pl.Expr:
    """Une passe de parse stricte, exacte et non-levante -> Datetime[us] naïf."""
    return expr.str.strptime(DT, format=fmt, strict=False, exact=True).cast(DT)


def _expand_two_digit_year(s_num: pl.Expr) -> pl.Expr:
    """Étend une année finale à 2 chiffres en 4 chiffres avec le pivot de siècle POSIX.

    Fait entièrement au niveau chaîne (forme numérique unifiée en '/'), pour que le
    parser numérique n'ait jamais besoin que de ``%Y``. Pivot : 00-68 -> 2000-2068,
    69-99 -> 1969-1999.

    Le ``^`` initial ancre le jour/mois au *début*, donc une date avec l'année en
    premier comme "2019/07/17" n'est jamais mal détectée (sa fin "19/07/17" ne doit pas
    être étendue). Le ``\\b`` final laisse une année à 4 chiffres intacte (pas de
    frontière à l'intérieur de "2019") et autorise une partie heure optionnelle après
    l'année.
    """
    return (
        s_num
        # 00-68 -> 20xx
        .str.replace_all(r"^(\d{1,2}/\d{1,2})/([0-5]\d|6[0-8])\b", r"${1}/20${2}")
        # 69-99 -> 19xx
        .str.replace_all(r"^(\d{1,2}/\d{1,2})/(69|[7-9]\d)\b", r"${1}/19${2}")
    )


def _candidate_datetimes(raw: pl.Expr, hint: str) -> pl.Expr:
    """Construit le coalesce complet et ordonné des candidates datetime pour une colonne."""
    s = raw.str.strip_chars()

    # Drapeaux de routage peu coûteux (une passe chacun). Ils permettent de forcer à
    # null les branches coûteuses (alphabétique / ISO) pour les cellules qui ne peuvent
    # pas matcher, afin que les kernels chaîne de Polars les sautent. Sur des données
    # purement numériques (le cas courant), la prépa mois/ISO ne coûte alors presque rien.
    has_alpha = s.str.contains(r"[A-Za-z]")
    has_dash = s.str.contains("-", literal=True)

    # Branche numérique : unifie '.' et '-' en '/' pour qu'un seul jeu de formats '/'
    # couvre dd/MM, dd-MM et dd.MM (plus yyyy/M/d). Les années à 2 chiffres sont étendues
    # à 4 chiffres, donc on n'a besoin que des formats `%Y`. Cette branche tourne sur
    # chaque cellule.
    s_num = _expand_two_digit_year(s.str.replace_all(r"[.\-]", "/"))

    # Branche ISO (conditionnée à '-') : retire les secondes fractionnaires
    # (":SS.sss" -> ":SS") puis un 'Z' final ou un offset '+HH:MM' / '-HHMM', puis parse
    # en naïf (on garde l'heure murale ; documenté dans le README).
    s_iso = (
        pl.when(has_dash)
        .then(s)
        .otherwise(None)
        .str.replace(r"(:\d{2})\.\d+", r"${1}")
        .str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "")
    )

    # Branche noms de mois (conditionnée à une lettre) : minuscules, séparateurs ->
    # espaces, suppression d'un nom de jour (ex. "Monday,"/"lundi"), espaces compressés.
    # On ENTOURE ensuite chaque token d'espaces et on mappe " janvier " -> " January ".
    # Entourer d'espaces rend le match borné au token, donc l'abréviation "jan" ne peut
    # jamais être remplacée *à l'intérieur* du nom complet "janvier" (collision de préfixe).
    s_month_in = pl.when(has_alpha).then(s.str.to_lowercase()).otherwise(None)
    s_month_norm = (
        s_month_in.str.replace_all(r"[,\./\-]", " ")
        .str.replace_all(rf"\b({_WEEKDAYS})\b", " ")
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )
    keys = [f" {k} " for k in _MONTH_MAP]
    vals = [f" {v} " for v in _MONTH_MAP.values()]
    s_month = (
        pl.concat_str([pl.lit(" "), s_month_norm, pl.lit(" ")])
        .str.replace_many(keys, vals)
        .str.strip_chars()
    )

    # Timestamps Unix : chaînes tout en chiffres de longueur 10 (secondes) ou 13 (millis).
    as_int = s.cast(pl.Int64, strict=False)
    ts = (
        pl.when(s.str.contains(r"^\d{13}$"))
        .then(pl.from_epoch(as_int, time_unit="ms"))
        .when(s.str.contains(r"^\d{10}$"))
        .then(pl.from_epoch(as_int, time_unit="s"))
        .otherwise(None)
        .cast(DT)
    )

    alt = "MDY" if hint == "DMY" else "DMY"
    candidates: list[pl.Expr] = []
    candidates += [_parse(s_num, f) for f in _numeric_formats(hint)]   # 1. hint
    candidates += [_parse(s_num, f) for f in _numeric_formats(alt)]    # 2. opposée
    candidates.append(_parse(s_num, "%Y/%m/%d"))                       #    yyyy/M/d
    candidates += [_parse(s_iso, f) for f in _ISO_FORMATS]            # 3. ISO
    candidates += [_parse(s_month, f) for f in _MONTH_FORMATS]        # 4. noms de mois
    candidates.append(ts)                                             # 5. timestamps

    return pl.coalesce(candidates)


def normalize_expr(column: str, hint: str) -> pl.Expr:
    """Expression Polars qui normalise ``column`` en ``DD-MM-YYYY HH:mm:ss``.

    Les valeurs parsables sont reformatées ; tout le reste (vide, malformé, null) est
    renvoyé exactement tel qu'il est entré. L'expression est ré-aliasée vers ``column``
    pour écraser la colonne source en place.
    """
    hint = validate_hint(hint)
    raw = pl.col(column)
    # `formatted` est null partout où aucune candidate n'a matché. Référencer la valeur
    # parsée une seule fois (dans `formatted`) permet à Polars d'évaluer le gros coalesce
    # une seule fois ; `coalesce([formatted, raw])` garde ensuite la valeur d'origine
    # telle quelle pour tout ce qui n'a pas été parsé.
    formatted = _candidate_datetimes(raw, hint).dt.strftime(OUTPUT_FORMAT)
    return pl.coalesce([formatted, raw]).alias(column)


def normalize_columns(date_columns: list[str], date_formats: list[str]) -> list[pl.Expr]:
    """Construit une expression de normalisation par paire (colonne, hint)."""
    return [
        normalize_expr(col, fmt)
        for col, fmt in zip(date_columns, date_formats, strict=True)
    ]


def normalize_values(values: list[str | None], hint: str) -> list[str | None]:
    """Helper pratique pour les tests unitaires : normalise une petite liste de chaînes.

    Construit un DataFrame à une colonne et applique la même expression qu'en
    production, pour que les tests unitaires exercent la vraie logique.
    """
    df = pl.DataFrame({"v": pl.Series("v", values, dtype=pl.String)})
    out = df.select(normalize_expr("v", hint))
    return out["v"].to_list()
