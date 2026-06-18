# Relances ciblées — Equation

Petit outil web pour envoyer des séries de mails personnalisés (10–50 destinataires)
en cold outreach B2B, depuis ton adresse pro OVH, en gardant la main sur l'expéditeur
et la délivrabilité.

## Multi-expéditeur (4–5 commerciaux)
Chaque commercial se connecte avec **sa propre adresse + mot de passe OVH**.
Les mails partent **réellement de sa boîte** (From = lui, réponses vers lui,
alignement SPF/DKIM/DMARC du domaine). C'est le bon réglage pour du cold
outreach peer-to-peer : OVH refuse souvent un `From` qui ne correspond pas au
compte authentifié, et un envoi « au nom de » quelqu'un d'autre dégrade la
délivrabilité.

- **Aucun mot de passe stocké** : il vit en mémoire le temps de la session, jamais sur disque, jamais dans le journal.
- **Liste blanche** (`ALLOWED_SENDERS`) : toi, admin, décides qui peut se connecter.
- Chacun ne voit/lance que ses propres séries.
- Le journal trace l'expéditeur de chaque mail.

> Variante possible si tu préfères que personne ne tape son mot de passe OVH :
> des profils pré-configurés côté serveur. Moins souple, plus de secrets à gérer.
> Dis-le-moi, c'est une adaptation modérée.

## Ce que ça fait
- Interface protégée par mot de passe.
- Liste de destinataires collée (Excel/CSV), avec champs libres -> variables `{prenom}`, `{societe}`, etc.
- Objet + corps HTML avec variables, liens cliquables.
- Pièces jointes (PDF, images…) + images **inline** via `cid:`.
- Aperçu personnalisé de chaque mail avant envoi.
- Throttling configurable entre chaque envoi (30–60 s recommandé).
- Journal des envois (qui / quand / OK ou erreur), téléchargeable.

## Limites OVH Email Pro (vérifié)
OVH plafonne à **200 mails/heure/compte** (et 300/heure/IP).
Pour des séries de 10–50, tu es très large. Au-delà (envoi de masse récurrent),
il faudra basculer sur un service transactionnel (Brevo/Postmark) — hors scope ici.

---

## Déploiement sur Render (pas-à-pas)

### 1. Mettre le code sur GitHub
Crée un repo (ex. `relances-equation`) et pousse ces fichiers
(`app.py`, `requirements.txt`, `render.yaml`, dossier `templates/`).

### 2. Créer le service sur Render
- New > **Web Service** > connecte le repo.
- Render lit `render.yaml` automatiquement (Blueprint). Sinon, à la main :
  - **Build** : `pip install -r requirements.txt`
  - **Start** : `gunicorn app:app -w 1 --threads 8 --timeout 120 -b 0.0.0.0:$PORT`
  - ⚠ **Un seul worker (`-w 1`)** : obligatoire, sinon le suivi de progression casse.

### 3. Variables d'environnement (Dashboard > Environment)
| Clé | Valeur |
|---|---|
| `SMTP_HOST` | `pro1.mail.ovh.net` |
| `SMTP_PORT` | `587` |
| `ALLOWED_SENDERS` | adresses autorisées, séparées par des virgules. Ex. `mbureau@equation-sie.com,lbastian@equation-sie.com,...` |
| `SECRET_KEY` | longue chaîne aléatoire (Render peut la générer) |
| `REPLY_TO` | (optionnel) adresse de réponse globale |
| `DEFAULT_DELAY_S` | `45` |
| `MAX_TOTAL_ATTACH_MB` | `10` |
| `LOG_DIR` | `/var/data` si disque persistant (sinon laisse vide) |

→ **Aucun identifiant SMTP en variable** : chacun saisit les siens à la connexion.
Les noms affichés (From) sont dans le dictionnaire `SENDER_NAMES` en haut de `app.py` — modifiable librement pour ajouter/retirer un commercial.

### 4. Journal persistant
Sur Render **Free**, le disque est éphémère : le journal disparaît à chaque
redéploiement (mais reste téléchargeable tant que le service tourne).
Pour le conserver : offre **Starter** + un disque monté sur `/var/data`
(déjà décrit dans `render.yaml`), puis `LOG_DIR=/var/data`.

### 5. Veille (Free vs Starter)
Le plan **Free** s'endort après 15 min sans trafic. Pendant une série avec
throttling, les pauses entre envois ne génèrent pas de trafic → risque que le
service s'endorme en plein milieu si tu fermes l'onglet.
- **Garde l'onglet "Envoi en cours" ouvert** (le polling réveille le service), **ou**
- prends **Starter** (pas de mise en veille) — recommandé pour cet usage.

---

## Bonnes pratiques délivrabilité
- SPF / DKIM / DMARC : déjà configurés sur ton domaine ✔
- Garde un vrai ratio texte/image : un mail tout-image part au spam.
- Throttling 30–60 s : plus naturel, meilleure réputation.
- Personnalise pour de vrai (`{prenom}`, `{societe}`) : c'est le cœur du peer-to-peer.

## Format de la liste collée
Première ligne = en-têtes. Colonne `email` obligatoire. Séparateur tab, `,` ou `;`.
```
email,prenom,societe,fonction
j.durand@acme.fr,Jean,ACME Immobilier,Directeur
m.leroy@beta.com,Marie,Beta Conseil,Gérante
```
Chaque colonne devient une variable utilisable dans l'objet et le corps :
`{prenom}`, `{societe}`, `{fonction}`…

## Images inline
Uploade l'image dans le champ "Images inline", puis référence-la dans le HTML par
son nom de fichier : `<img src="cid:signature.png">`.

## Lancer en local (test, optionnel)
```bash
pip install -r requirements.txt
export APP_PASSWORD=secret SMTP_USER=... SMTP_PASSWORD=...
python app.py        # http://localhost:5000
```
