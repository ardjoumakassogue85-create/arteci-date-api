# Guide — Déploiement Kubernetes

>  Quels manifests créer, pourquoi, et comment ils répondent aux contraintes ARTECI
> (4 CPU / 8 Go, scaling horizontal, MinIO + SigNoz).

## Prérequis

- Un **cluster Kubernetes** et `kubectl` configuré dessus (`kubectl get nodes` répond).
- L'**image publiée** sur un registre que le cluster peut tirer (la nôtre :
  `ardjouma/arteci-date-api`, déjà poussée par le CI sur DockerHub).
- **MinIO** et **SigNoz** déjà déployés (voir plus bas), ou des instances accessibles
  depuis le cluster. L'API a juste besoin de leurs endpoints + credentials.

### Cluster local pour tester (optionnel)
Un cluster local fait l'affaire pour la démo :
```bash
# minikube
minikube start --cpus=4 --memory=8192
# metrics-server : nécessaire pour que le HPA voie le CPU (sinon il reste <unknown>)
minikube addons enable metrics-server
# ou kind
kind create cluster
```

## Les fichiers à créer

On range tout dans un dossier `k8s/`, **un fichier par manifest** :
```
k8s/configmap.yaml      # config non secrète
k8s/secret.yaml         # credentials MinIO
k8s/deployment.yaml     # les réplicas de l'API
k8s/service.yaml        # adresse stable in-cluster
k8s/hpa.yaml            # autoscaling
k8s/ingress.yaml        # (optionnel) entrée HTTPS externe
```

## Les manifests à créer

| Manifest | Rôle |
|---|---|
| `Deployment` | Lance N réplicas d'API stateless. Stateless = on peut scaler et redémarrer sans risque. |
| `Service` (ClusterIP) | Adresse stable in-cluster ; load-balance sur les réplicas. |
| `ConfigMap` | Config non secrète (endpoint MinIO, endpoint OTLP, taille de batch, niveau de log). |
| `Secret` | Clés d'accès MinIO (jamais dans l'image ni dans le ConfigMap). |
| `HorizontalPodAutoscaler` | Scale les réplicas sur CPU/RAM pour absorber des volumes croissants. |
| `Ingress` (optionnel) | Entrée HTTPS externe si le frontend est hors du cluster. |

MinIO et SigNoz sont des services de plateforme **stateful** : on les déploie via leurs
charts Helm officiels (`minio/minio`, `signoz/signoz`) dans leurs propres namespaces, ou
on se branche sur des instances managées existantes. L'API n'a besoin que de leurs
endpoints et de leurs credentials.

## Deployment + ressources (cohérent avec 4 CPU / 8 Go)
```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: arteci-date-api, labels: { app: arteci-date-api } }
spec:
  replicas: 2
  selector: { matchLabels: { app: arteci-date-api } }
  template:
    metadata: { labels: { app: arteci-date-api } }
    spec:
      securityContext: { runAsNonRoot: true, runAsUser: 1000, fsGroup: 1000 }
      containers:
        - name: api
          image: ardjouma/arteci-date-api:latest   # épinglez une release en prod (ex. :1.0.0 ou :sha-xxxx)
          ports: [{ containerPort: 8000 }]
          envFrom:
            - configMapRef: { name: arteci-config }
            - secretRef: { name: arteci-minio }
          resources:
            requests: { cpu: "1",   memory: "1Gi" }   # base
            limits:   { cpu: "4",   memory: "4Gi" }   # marge pour le fichier de 931 Mo
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 20
            periodSeconds: 20
          volumeMounts: [{ name: scratch, mountPath: /tmp/arteci }]
      volumes:
        - name: scratch
          emptyDir: { sizeLimit: 4Gi }   # scratch CSV temp ; dimensionné pour le plus gros fichier
```

### Pourquoi ce dimensionnement
- Le traitement est borné par le CPU et la mémoire transitoire, sans état persistant. La
  RAM pic est plafonnée par le batch (mesuré ~1,3 Go sur le fichier de 931 Mo avec un
  batch de 500k), donc `limits.memory: 4Gi` est confortable ; baisser
  `PROCESSING_BATCH_ROWS` réduit encore.
- Le scratch `emptyDir` doit contenir les fichiers temp d'entrée + sortie du plus gros
  fichier (~2 Go pour anon_3), à dimensionner en conséquence, sur un disque rapide.
- La limite `cpu` à 4 fait correspondre le moteur multi-threadé de Polars à la machine
  cible.

## ConfigMap + Secret
```yaml
apiVersion: v1
kind: ConfigMap
metadata: { name: arteci-config }
data:
  MINIO_ENDPOINT: "minio.minio.svc.cluster.local:9000"
  MINIO_SECURE: "false"
  PROCESSING_BATCH_ROWS: "500000"
  # Route les fichiers temporaires vers l'emptyDir monté sur /tmp/arteci
  # (sinon tempfile écrit dans /tmp et le sizeLimit du volume ne s'applique pas).
  TMPDIR: "/tmp/arteci"
  OTEL_ENABLED: "true"
  OTEL_EXPORTER_OTLP_ENDPOINT: "http://signoz-otel-collector.signoz.svc.cluster.local:4317"
  OTEL_SERVICE_NAME: "arteci-date-api"
---
apiVersion: v1
kind: Secret
metadata: { name: arteci-minio }
type: Opaque
stringData:
  MINIO_ACCESS_KEY: "CHANGE_ME"
  MINIO_SECRET_KEY: "CHANGE_ME"
```

## Service + HPA
```yaml
apiVersion: v1
kind: Service
metadata: { name: arteci-date-api }
spec:
  selector: { app: arteci-date-api }
  ports: [{ port: 80, targetPort: 8000 }]
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: arteci-date-api }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: arteci-date-api }
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource: { name: cpu, target: { type: Utilization, averageUtilization: 70 } }
```
Pourquoi le HPA marche bien ici : l'API est **stateless**, pas de session ni de cache
local entre requêtes (les fichiers temp sont par-requête et nettoyés). N'importe quel
réplica peut servir n'importe quelle requête, donc ajouter des pods augmente le débit de
façon linéaire. C'est exactement l'exigence de scalabilité : absorber plus de volume en
ajoutant des réplicas, pas en grossissant une seule machine.

## Déployer les dépendances (MinIO + SigNoz)

Ce sont des services stateful : on les installe via Helm, dans leurs propres namespaces.
```bash
# MinIO
helm repo add minio https://charts.min.io/ && helm repo update
helm install minio minio/minio -n minio --create-namespace

# SigNoz
helm repo add signoz https://charts.signoz.io && helm repo update
helm install signoz signoz/signoz -n signoz --create-namespace
```
> Vérifiez ensuite les **noms réels** des services (`kubectl get svc -n minio`,
> `kubectl get svc -n signoz`) et ajustez `MINIO_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT`
> dans le ConfigMap en conséquence.

## Appliquer les manifests de l'API
```bash
# 1. Namespace dédié
kubectl create namespace arteci

# 2. Renseignez les vrais credentials MinIO dans k8s/secret.yaml (remplacez CHANGE_ME)
#    OU créez le Secret directement, sans le committer :
# kubectl -n arteci create secret generic arteci-minio \
#   --from-literal=MINIO_ACCESS_KEY=... --from-literal=MINIO_SECRET_KEY=...

# 3. Appliquez tout le dossier (configmap, secret, deployment, service, hpa, ingress)
kubectl -n arteci apply -f k8s/

# 4. Attendez que le rollout soit prêt
kubectl -n arteci rollout status deploy/arteci-date-api
```

## Vérifier le déploiement
```bash
# Les pods doivent être Running et Ready (2/2)
kubectl -n arteci get pods
# Le HPA doit voir les métriques (pas <unknown> en permanence)
kubectl -n arteci get hpa
# Tester l'API sans Ingress, via un port-forward local
kubectl -n arteci port-forward svc/arteci-date-api 8000:80
# puis, dans un autre terminal :
curl http://localhost:8000/health        # -> {"status":"ok"}
```
> Si un pod reste en `CrashLoopBackOff` ou `Pending` : `kubectl -n arteci describe pod <nom>`
> et `kubectl -n arteci logs <nom>` donnent la cause (souvent un endpoint MinIO/SigNoz
> injoignable ou un Secret manquant).

## Durcissements de production à garder en tête
- **Jobs longs vs timeouts HTTP :** le fichier de 931 Mo (~65 s) passe encore en requête
  synchrone, mais pour des fichiers plus gros ou des pics, on ajouterait un pattern de
  **worker async** : `POST /processDate` met un job en file (Redis/RabbitMQ par
  exemple), les workers traitent et mettent à jour le statut, le frontend poll. Ça garde
  l'API réactive et permet de scaler les workers indépendamment.
- **Écriture en place atomique :** uploader sur une clé temporaire puis copie côté
  serveur vers la clé cible, pour qu'un crash ne laisse jamais un objet à moitié écrit.
- **PodDisruptionBudget** + **topologySpreadConstraints** pour la disponibilité.
- **NetworkPolicy** qui restreint l'egress à MinIO + SigNoz uniquement.
