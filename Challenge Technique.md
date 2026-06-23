Challenge Technique

Optimisation du Traitement de Données :

Projet ARTECI

Artefact Côte d'Ivoire . Stage DevOps / Data Platform



1\. Contexte et Problématique

Comprendre le système existant et le défi à résoudre

ARTECI est une application interne dediee au traitement de fichiers de revues provenant de

sources heterogenes (logs, CSV, Excel). Lors de l'ingestion d'un fichier, chaque colonne

passe par une etape de validation de format avant tout traitement. Cette etape est

obligatoire, elle garantit la coherence et la fiabilite des donnees en aval.



Cependant, la verification et la standardisation des colonnes de dates representent

aujourd'hui un goulot d'etranglement significatif sur les fichiers volumineux. Les fichiers

recus ne respectent pas un format de date unique : certains arrivent au format anglais (MDY

: Month/Day/Year), d'autres au format francais (DMY : Day/Month/Year). Il est possible de

rencontrer plusieurs formats differents coexistant dans une meme colonne du fichier.



2\. Objectif

Ce que le candidat doit concevoir et livrer

L'objectif est de concevoir et de developper une API haute performance dediee a la

standardisation des formats de date. Quel que soit le format recu en entree (DMY, MDY, ou

un melange des deux, selon leurs variantes), l'API doit produire une sortie normalisee et

exploitable par les systemes en aval. Le succes de la solution sera juge sur trois axes

principaux :



* Résolution du probleme metier : Pouvoir gerer toutes les variantes de date en entree puis retourner le format standard de sortie "JJ-MM-AAAA HH:mm:ss".
* Performance : Optimiser le temps de traitement des colonnes dates, meme sur des fichiers de grande taille. Benchmarkez les outils de differents langages afin de trouver ceux adaptes pour fournir de meilleures performances.
* &#x20;Scalabilite : Avoir la capacite a absorber des volumes croissants de donnees sans degradation de service ni augmentation lineaire des ressources.





3\. Description du Système

Architecture et flux de données du projet ARTECI

Le systeme ARTECI est organise autour d'un frontend et d'une API backend. Les fichiers

transitent par un stockage objet MinlO, qui joue le role de source de verite pour les donnees

brutes et traitées. Voici le flux complet tel qu'il est implémenté :



1. &#x20;Le fichier est charge depuis le frontend par l'utilisateur.
2. &#x20;L'API le reçoit et le stocke dans un bucket MinlO dédié aux données brutes (raw).
3. &#x20;Le frontend affiche une interface de validation permettant a l'utilisateur d'identifier les colonnes de dates et de specifier leur format (DMY ou MDY).
4. &#x20;L'API recoit ces informations, traite le fichier (normalisation des information incluant la standardisation des dates), puis le stocke dans un autre bucket dedie aux fichiers traites (processeddata). **C'est a cette etape que vous devez intervenir.**
5. &#x20;Une fois le traitement termine, les 100 premieres lignes du fichier traite sont renvoyées au frontend pour une dernière validation visuelle par l'utilisateur.





Cl-dessous des exemples de fichiers traités et leur temps de traitement :



|Nom|Taille|Nombre de ligne|Temps de traitement|
|-|-|-|-|
|Ist\_of\_users\_anon\_1.csv|\~28 MB|320 399|\~20s|
|Ist\_of\_users\_anon\_2.csv|\~182 MB|2 119 517|\~50s|
|Ist\_of\_users\_anon\_3.csv|\~931 MB|10 799 773|\~2min|





Caractéristiques du système utilisé pour les tests :

* &#x09;OS Linux Alpine 3.22
* &#x09;8 GB RAM
* &#x20;	4 CPU



Ces fichiers vous seront partages par mail lors de l'envoi du challenge. Vous devez les utiliser pour valider l'ensemble des regles metiers et tester les performances de votre solution.







4\. Ce qu'on attend de vous

Spécifications techniques et fonctionnelles à implémenter

La phase de validation d'un fichier regroupe plusieurs types de traitements. Dans ce challenge, vous n'avez a traiter qu'un seul cas : la normalisation des colonnes de dates.

Votre API intervient exclusivement a la fin de cette phase pour detecter, standardiser et retourner les donnees de dates traitees.



4.1 Endpoints requis

Deux endpoints doivent être implémentés. Leurs contrats d'interface sont définis ci-dessous.



|**Endpoint**|**Description**|**Donnees en entrée**|**Retour**|
|-|-|-|-|
|POST /processDate|Endpoint principal. Traite les colonnes de dates du fichier, met à jour le fichier dans MinIO et retourne un aperçu des données normalisées.|**date\_columns** : Liste des colonnes de dates du fichier.<br /><br />**date\_formats** : Liste des formats de chaque colonne (DMY ou MDY), dans le même ordre que date\_columns.<br /><br />**bucket** : Nom du bucket MinIO contenant le fichier.<br /><br />**file** : Chemin complet du fichier dans le bucket.|Une liste des 100 premières lignes du fichier après traitement et normalisation des dates.<br />|
|GET /columns|Retourne la liste des colonnes presentes dans un fichier stocke dans MinIO|**bucket** : Nom du bucket MinIO.<br /><br />**file** : Chemin du fichier dans le bucket.|La liste des noms de colonnes présentes dans le fichier.|





4.2 Règles métier

Contraintes fonctionnelles à respecter dans l'implémentation

* L'API est utilisee en interne : les messages d'erreur doivent etre explicites et actionnables. Exemples attendus : fichier introuvable dans le bucket, colonne spécifiée inexistante dans le fichier, format non supporté.
* L'API doit modifier le fichier directement dans MinlO (ecriture en place dans le bucket) et non en creer un autre.
* Le traitement doit gerer les formats mixtes au sein d'une meme colonne : une ligne peut contenir une date en DMY et la suivante en MDY. L'API doit detecter et normaliser les deux. Ici ([https://help.qlik.com/talend/fr-FR/data-preparation-user-guide/8.0/list-of-date-and-date-time-formats](https://help.qlik.com/talend/fr-FR/data-preparation-user-guide/8.0/list-of-date-and-date-time-formats) ) une liste de formats qui doivent etre supportes en entree (privilégiez les formats anglais et français ainsi que les timestamp).
* Une cellule mal formatee ne doit pas empecher le traitement de la colonne ou du fichier, elle doit etre retournee telle qu'elle est.
* Le retour des 100 premieres lignes doit refleter l'etat du fichier apres traitement complet, pas un aperçu intermédiaire.



4.3 Exigences techniques

Standards et pratiques d'ingénierie attendus pour la solution



**Conteneurisation**

L'ensemble du systeme doit etre cloud-native et base sur les conteneurs. L'application et toutes ces dépendances doivent etre packagees dans une image Docker et publier sur DockerHub. Veuillez suivre les meilleures pratiques afin de produire une image optimisée.





**Instrumentation**

Tous les endpoints et les fonctions principales de l'application doivent etre instrumentalises selon les standards Open Telemetry. L'objectif est de pouvoir suivre les performances de chaque traitement et de faciliter le debugging en production. Sont attendus :

* Des traces couvrant le cycle de vie complet d'une requete (reception, lecture MinIO, traitement, écriture, réponse).
* Des logs structures avec niveau de severite, timestamp et contexte de la requête.
* Le tout doit etre expose pour visualisation dans Signoz.



**Automatisation (CI/CD)**

Un pipeline d'integration et de livraison continue doit etre mis en place via GitHub Actions.

La conception entiere du pipeline vous revient, le but etant d'automatiser la publication de l'image de l'application sur DockerHub. Chaque etape incluse devra être justifiée.



**Deploiement dans un environnement distribue**

L'application doit pouvoir etre installee dans un cluster Kubernetes. Les manifests Kubernetes fournis doivent couvrir le deploiement de l'application et de tous les stacks nécessaires pour son fonctionnement sur un environnement de production.



**5. Livrables**



Ce qui doit etre soumis a l'issue des deux semaines de challenge

Un repository GitHub public (ou partage avec l'equipe) contenant les elements suivants :

* **Code source** : Le code source de l'API, structuré et documenté.
* **README** : Un README clair et concis expliquant le projet, les choix techniques, et les instructions de demarrage selon l'environnement cible (local ou Kubernetes).
* **Docker Compose** : Un fichier docker-compose.yml embarquant toutes les stacks necessaires au demarrage en local.
* **Manifests Kubernetes** : Les manifests Kubernetes (fichiers YAML) permettant le deploiement de l'application dans un cluster K8s.
* **Pipeline CI/CD** : Le workflow GitHub Actions (.github/workflows/) pour la CI/CD.
* **Image Docker** : Une image Docker publiee sur DockerHub.





**6. Criteres d'evaluation**

Comment votre travail sera jugé lors de l'entretien technique





|Critère|Ce qu'on regarde|
|-|-|
|Respect du cahier des charges|Les deux endpoints sont implementes et documentes, les regles metier respectees, les livrables fournis.|
|Qualite du code|Structure claire, separation des responsabilites, gestion d'erreurs robustes, lisibilite et maintenabilite. Le choix du langage/outil doit etre justifie.|
|Performance|Temps de traitement optimise meme sur des fichiers de grande taille.|
|Observabilite|Instrumentation complete et pertinente. Les traces couvrent le flux entier, les logs sont structures et exploitables. Le tout est expose sur Signoz.|
|DevOps \& deployment|Docker Compose fonctionnel, pipeline CI/CD operationnel, manifests K8s cohérents avec l'architecture.|
|Documentation|README complet, justification des choix, transparence sur les limites et ameliorations.|



