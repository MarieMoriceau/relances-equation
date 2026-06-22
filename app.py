"""
Envoi de mails personnalisés (cold outreach B2B) — Equation
------------------------------------------------------------
MULTI-EXPÉDITEUR : chaque commercial se connecte avec sa propre adresse +
mot de passe OVH. Le mail part réellement de sa boîte (From = lui, réponses
vers lui, alignement SPF/DKIM/DMARC du domaine). Aucun mot de passe n'est
stocké sur disque : il vit uniquement en mémoire le temps de la session.

Flask + SMTP OVH Email Pro. Construction MIME multipart (HTML + texte alterné
+ PJ + images inline cid:). Throttling, journal, accès par liste blanche.

IMPORTANT (Render) : UN SEUL worker gunicorn (-w 1 --threads 8). Les sessions
et les jobs vivent en mémoire ; plusieurs workers casseraient l'état.
"""

import os
import re
import time
import json
import uuid
import base64
import mimetypes
import smtplib
import imaplib
import tempfile
import threading
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.utils import formataddr
from email import encoders
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, jsonify, send_file, flash
)
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------- #
# Configuration (variables d'environnement)
# --------------------------------------------------------------------------- #
SMTP_HOST = os.environ.get("SMTP_HOST", "pro1.mail.ovh.net")   # infra partagée
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

# Copie dans le dossier "Envoyés" via IMAP (même serveur OVH que le SMTP).
SAVE_TO_SENT = os.environ.get("SAVE_TO_SENT", "1") not in ("0", "", "false", "False")
IMAP_HOST = os.environ.get("IMAP_HOST", SMTP_HOST)             # = SMTP_HOST par défaut
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
SENT_FOLDER = os.environ.get("SENT_FOLDER", "")               # vide = détection auto
REPLY_TO  = os.environ.get("REPLY_TO", "")                     # optionnel (global)

# Journal des envois dans Notion (optionnel) : renseigne NOTION_TOKEN +
# NOTION_DATABASE_ID dans Render pour activer. Sinon, ignoré silencieusement.
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "6b7a962f02ef4207a0fdae7f244dd656").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# Liste blanche des expéditeurs autorisés (séparés par des virgules).
# Vide = toute adresse OVH valide peut se connecter. Recommandé : la remplir.
ALLOWED_SENDERS = {
    e.strip().lower()
    for e in os.environ.get("ALLOWED_SENDERS", "").split(",")
    if e.strip()
}

# Noms affichés (From). Modifiable librement. Fallback = partie avant @.
SENDER_NAMES = {
    "mbureau@equation-sie.com":      "Marine Bureau de Rotalier",
    "lbastian@equation-sie.com":     "Lionel Bastian",
    "rabou-khalil@equation-sie.com": "Richard Abou Khalil",
    "nvial@equation-sie.com":        "Nicolas Vial",
    "mmoriceau@equation-sie.com":    "Marie Moriceau",
    "smoinet@equation-sie.com":      "Séverine Moinet",
    "mbastian@equation-sie.com":     "Michel Bastian",
}

# Signatures HTML par expéditeur (fichier dédié signatures.py, facile à éditer).
try:
    from signatures import SIGNATURES
except ImportError:
    SIGNATURES = {}


MAX_TOTAL_ATTACH_MB = float(os.environ.get("MAX_TOTAL_ATTACH_MB", "25"))
COMPRESS_PDF_OVER_MB = float(os.environ.get("COMPRESS_PDF_OVER_MB", "4"))  # auto-compression au-delà
DEFAULT_DELAY_S     = int(os.environ.get("DEFAULT_DELAY_S", "45"))
OVH_HOURLY_LIMIT    = 200  # mails / heure / compte (doc OVH)

LOG_DIR  = os.environ.get("LOG_DIR", os.path.join(tempfile.gettempdir(), "relances_data"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "journal_envois.jsonl")

ALLOWED_ATTACH = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                  ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt"}
ALLOWED_INLINE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

app = Flask(__name__, template_folder=".")
app.secret_key = SECRET_KEY
# Marge au-dessus du plafond des PJ (en-têtes, encodage, plusieurs fichiers)
app.config["MAX_CONTENT_LENGTH"] = int((MAX_TOTAL_ATTACH_MB + 20) * 1024 * 1024)

# État en mémoire (d'où le worker unique)
# Identité (email, name) -> stockée dans le cookie signé (survit aux redémarrages).
# Mot de passe OVH -> uniquement en mémoire ici, jamais sur disque ni dans le cookie.
PASSWORDS = {}         # email -> mot de passe (perdu au redémarrage -> reconnexion)
JOBS = {}
LOCK = threading.Lock()

# --- Auto-login optionnel ---------------------------------------------------- #
# Pré-charge des mots de passe au démarrage pour éviter de se reconnecter après
# chaque redémarrage. À DÉFINIR UNIQUEMENT dans l'onglet Environment de Render,
# JAMAIS dans le code/GitHub.
#   - Cas simple (ton compte) : AUTOLOGIN_EMAIL + AUTOLOGIN_PASSWORD
#   - Plusieurs comptes        : AUTOLOGIN = "email1:mdp1;email2:mdp2"
# La connexion reste protégée par le cookie : un mot de passe pré-chargé ne
# connecte personne tout seul, il évite juste d'avoir à le re-saisir.
def _preload_passwords():
    # Compte simple : AUTOLOGIN_EMAIL + AUTOLOGIN_PASSWORD
    em = os.environ.get("AUTOLOGIN_EMAIL", "").strip().lower()
    pw = os.environ.get("AUTOLOGIN_PASSWORD", "")
    if em and pw:
        PASSWORDS[em] = pw
    # Plusieurs comptes (commerciaux) : paires numérotées, robustes aux caractères
    # spéciaux dans les mots de passe.
    #   AUTOLOGIN_1_EMAIL / AUTOLOGIN_1_PASSWORD, AUTOLOGIN_2_EMAIL / ...
    for n in range(1, 21):
        e = os.environ.get("AUTOLOGIN_%d_EMAIL" % n, "").strip().lower()
        p = os.environ.get("AUTOLOGIN_%d_PASSWORD" % n, "")
        if e and p:
            PASSWORDS[e] = p
    # Format compact optionnel : AUTOLOGIN = "email1:mdp1;email2:mdp2"
    blob = os.environ.get("AUTOLOGIN", "").strip()
    for pair in blob.split(";"):
        if ":" in pair:
            e, p = pair.split(":", 1)
            e = e.strip().lower(); p = p.strip()
            if e and p:
                PASSWORDS[e] = p
_preload_passwords()


# --------------------------------------------------------------------------- #
# Authentification (identifiants OVH de chacun)
# --------------------------------------------------------------------------- #
def display_name(email: str) -> str:
    return SENDER_NAMES.get(email.lower(), email.split("@")[0].replace(".", " ").title())


def smtp_check(email: str, password: str):
    """Valide les identifiants en se connectant réellement à OVH."""
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(email, password)
        return True, None
    except smtplib.SMTPAuthenticationError:
        return False, "Adresse ou mot de passe OVH incorrect."
    except Exception as e:
        return False, f"Connexion au serveur impossible : {e}"


def current_user():
    """Identité issue du cookie signé (survit aux redémarrages)."""
    email = session.get("email")
    if not email:
        return None
    return {"email": email, "name": session.get("name", display_name(email))}


def has_password(email):
    return bool(PASSWORDS.get(email))


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        # Mot de passe perdu (redémarrage du service) -> reconnexion immédiate,
        # plutôt que de laisser composer puis bloquer à l'envoi.
        if not has_password(u["email"]):
            flash("Session expirée (le service a redémarré). Reconnecte-toi pour continuer.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if ALLOWED_SENDERS and email not in ALLOWED_SENDERS:
            flash("Cette adresse n'est pas autorisée à utiliser l'outil.")
            return render_template("login.html")
        ok, err = smtp_check(email, password)
        if not ok:
            flash(err)
            return render_template("login.html")
        session["email"] = email
        session["name"] = display_name(email)
        PASSWORDS[email] = password
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    email = session.get("email")
    PASSWORDS.pop(email, None)
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Outils : personnalisation, parsing, conversion texte
# --------------------------------------------------------------------------- #
FIELD_RE = re.compile(r"\{(\w+)\}")


def personalize(template: str, row: dict) -> str:
    return FIELD_RE.sub(lambda m: str(row.get(m.group(1), m.group(0))), template or "")


def parse_recipients(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return [], ["Liste de destinataires vide."]
    lines = [l for l in raw.splitlines() if l.strip()]

    # MODE SIMPLE : si la 1re ligne contient déjà un "@", c'est une liste
    # d'adresses (une par ligne et/ou séparées par des virgules), pas un en-tête.
    if "@" in lines[0]:
        rows, errors, seen = [], [], set()
        for tok in re.split(r"[\s,;]+", raw):
            tok = tok.strip()
            if not tok:
                continue
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", tok):
                errors.append(f"Adresse ignorée (invalide) : {tok}")
                continue
            if tok.lower() in seen:
                continue
            seen.add(tok.lower())
            rows.append({"email": tok})
        if not rows:
            errors.append("Aucune adresse e-mail valide trouvée.")
        return rows, errors

    # MODE TABLEAU : en-tête avec colonnes (pour personnalisation {prenom}, etc.)
    sep = "\t" if "\t" in lines[0] else (";" if ";" in lines[0] else ",")
    headers = [h.strip().lower() for h in lines[0].split(sep)]
    if "email" not in headers:
        return [], ["Colle des adresses (une par ligne ou séparées par des virgules), "
                    "ou un tableau dont la 1re ligne contient une colonne 'email'."]
    rows, errors, seen = [], [], set()
    for i, line in enumerate(lines[1:], start=2):
        cells = [c.strip() for c in line.split(sep)]
        row = {headers[j]: (cells[j] if j < len(cells) else "")
               for j in range(len(headers))}
        email = row.get("email", "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors.append(f"Ligne {i} : email invalide ({email or 'vide'}).")
            continue
        if email.lower() in seen:
            errors.append(f"Ligne {i} : doublon ignoré ({email}).")
            continue
        seen.add(email.lower())
        rows.append(row)
    return rows, errors


def html_to_text(html: str) -> str:
    txt = html or ""
    txt = re.sub(r"(?is)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?is)</p>", "\n\n", txt)
    txt = re.sub(r'(?is)<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                 lambda m: f"{re.sub('<[^>]+>', '', m.group(2))} ({m.group(1)})", txt)
    txt = re.sub(r"(?is)<[^>]+>", "", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


# --------------------------------------------------------------------------- #
# Photos dans le corps : insertion auto + rendu dans l'aperçu
# --------------------------------------------------------------------------- #
PHOTO_TAG = ('<img src="cid:{cid}" alt="{name}" '
             'style="max-width:100%;height:auto;display:block;margin:14px 0;border-radius:4px;">')


def photo_tag(cid, name, center=False):
    """Toutes les photos à la même largeur (600px, réduites si l'écran est plus
    petit). Centrées ou alignées à gauche selon le choix."""
    margin = "14px auto" if center else "14px 0"
    return ('<img src="cid:%s" alt="%s" '
            'style="width:600px;max-width:100%%;height:auto;display:block;'
            'margin:%s;border-radius:4px;">' % (cid, name, margin))


def embed_photos(body: str, inline_images, center=False) -> str:
    """Place chaque photo là où un repère apparaît dans le corps. Repères tolérés
    (espaces et majuscules ignorés) : [photo1], [photo 1], [Photo1], ou [nomdufichier].
    Sinon (aucun repère, ni cid: manuel), la photo est ajoutée à la fin."""
    body = body or ""
    for i, img in enumerate(inline_images, start=1):
        tag = photo_tag(img["cid"], img["filename"], center)
        base = os.path.splitext(img["filename"])[0]
        patterns = [
            r"\[\s*photo\s*" + str(i) + r"\s*\]",                 # [photo1] / [ photo 1 ]
            r"\[\s*" + re.escape(base) + r"\s*\]",                # [nomdufichier]
            r"\[\s*" + re.escape(img["filename"]) + r"\s*\]",     # [nomdufichier.png]
        ]
        for pat in patterns:
            body = re.sub(pat, lambda _m: tag, body, flags=re.IGNORECASE)
    # Filet de sécurité : photos non placées -> à la fin
    extra = "".join(
        photo_tag(img["cid"], img["filename"], center)
        for img in inline_images
        if f"cid:{img['cid']}" not in body
    )
    return body + extra


def nl2br(text: str) -> str:
    """Convertit les sauts de ligne tapés (touche Entrée) en vrais retours HTML,
    pour que le texte s'affiche comme à l'écran. Ne casse pas les liens (inline)
    ni le HTML déjà présent : on évite les doubles sauts autour des blocs."""
    if not text:
        return text
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\n", "<br>\n")
    # pas de <br> superflu juste après/avant un bloc HTML
    t = re.sub(r"(</(?:p|div|ul|ol|li|h[1-6]|blockquote|table|tr)>)\s*<br>", r"\1", t, flags=re.I)
    t = re.sub(r"<br>\s*(<(?:p|div|ul|ol|li|h[1-6]|blockquote|table|tr)[ >])", r"\1", t, flags=re.I)
    return t


def assemble_body(template: str, row: dict, photos, signature_html: str, center=False) -> str:
    """Corps final : texte personnalisé -> photos -> signature."""
    body = personalize(template, row)
    body = nl2br(body)                       # touche Entrée = vrai retour à la ligne
    body = embed_photos(body, photos, center)
    if signature_html:
        body = body + "<br>" + signature_html
    return body


def inline_to_data(body: str, photos) -> str:
    """Rend les photos cid: visibles dans l'aperçu (base64). La signature
    utilise une image en ligne (URL) qui s'affiche déjà toute seule."""
    for img in photos:
        try:
            with open(img["path"], "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            mime = mimetypes.guess_type(img["filename"])[0] or "image/png"
            body = body.replace(f"cid:{img['cid']}", f"data:{mime};base64,{b64}")
        except OSError:
            pass
    return body


# --------------------------------------------------------------------------- #
# Compression PDF (ré-échantillonne les images). Best-effort, optionnelle.
# --------------------------------------------------------------------------- #
def compress_pdf(in_path, max_dim=1600, quality=72):
    """Réduit le poids d'un PDF trop lourd en ré-échantillonnant ses images.
    Retourne (chemin, nouvelle_taille) ; si échec/inutile -> (in_path, None)."""
    try:
        import fitz
        from PIL import Image
        import io as _io
    except ImportError:
        return in_path, None
    try:
        doc = fitz.open(in_path)
        for page in doc:
            for info in page.get_images(full=True):
                xref = info[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n >= 5:                       # CMYK / alpha -> RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    w, h = pix.width, pix.height
                    mode = "RGB" if pix.n >= 3 else "L"
                    img = Image.frombytes(mode, (w, h), pix.samples)
                    scale = min(1.0, max_dim / max(w, h))
                    if scale < 1.0:
                        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                         Image.LANCZOS)
                    buf = _io.BytesIO()
                    img.convert("RGB").save(buf, "JPEG", quality=quality)
                    doc.update_stream(xref, buf.getvalue())
                    doc.xref_set_key(xref, "Filter", "/DCTDecode")
                    doc.xref_set_key(xref, "Width", str(img.width))
                    doc.xref_set_key(xref, "Height", str(img.height))
                    doc.xref_set_key(xref, "BitsPerComponent", "8")
                    doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")
                    pix = None
                except Exception:
                    continue
        out = in_path + ".min.pdf"
        doc.save(out, garbage=4, deflate=True, clean=True)
        doc.close()
        if os.path.exists(out) and os.path.getsize(out) < os.path.getsize(in_path):
            return out, os.path.getsize(out)
        if os.path.exists(out):
            os.remove(out)
        return in_path, None
    except Exception:
        return in_path, None


# --------------------------------------------------------------------------- #
# Construction du message MIME
# --------------------------------------------------------------------------- #
def build_message(from_email, from_name, to_email, subject,
                  html_body, attachments, inline_images):
    root = MIMEMultipart("mixed")
    root["Subject"] = subject
    root["From"] = formataddr((from_name, from_email))
    root["To"] = to_email
    if REPLY_TO:
        root["Reply-To"] = REPLY_TO

    related = MIMEMultipart("related")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_to_text(html_body), "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    related.attach(alt)

    for img in inline_images:
        with open(img["path"], "rb") as fh:
            part = MIMEImage(fh.read())
        part.add_header("Content-ID", f"<{img['cid']}>")
        part.add_header("Content-Disposition", "inline", filename=img["filename"])
        related.attach(part)
    root.attach(related)

    for att in attachments:
        with open(att["path"], "rb") as fh:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(fh.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=att["filename"])
        root.attach(part)
    return root


# --------------------------------------------------------------------------- #
# Copie dans le dossier "Envoyés" (IMAP)
# --------------------------------------------------------------------------- #
def _sent_candidates(M):
    """Liste ordonnée de dossiers 'Envoyés' à essayer : flag \\Sent d'abord,
    puis noms contenant sent/envoy, puis valeurs par défaut courantes OVH."""
    cands = []
    if SENT_FOLDER:
        cands.append(SENT_FOLDER)
    try:
        typ, data = M.list()
        if typ == "OK" and data:
            flagged, named = [], []
            for raw in data:
                line = raw.decode("ascii", "ignore") if isinstance(raw, bytes) else str(raw)
                quoted = re.findall(r'"([^"]*)"', line)
                name = quoted[-1] if quoted else line.split()[-1].strip('"')
                if "\\Sent" in line:
                    flagged.append(name)
                elif re.search(r"(?i)(sent|envoy)", name):
                    named.append(name)
            cands += flagged + named
    except Exception:
        pass
    # Valeurs par défaut courantes (OVH / Open-Xchange)
    cands += ["Sent", "Envoy&AOk-s", "INBOX.Sent", "INBOX.Envoy&AOk-s",
              "Sent Messages", "INBOX.Sent Messages"]
    seen, ordered = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c); ordered.append(c)
    return ordered


def save_to_sent(email, password, msg):
    """Range une copie du message dans le dossier Envoyés. Best-effort.
    Essaie plusieurs noms de dossier ; renvoie la dernière erreur si tout échoue."""
    if not SAVE_TO_SENT:
        return True, None
    last_err = "aucun dossier Envoyés trouvé"
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
        try:
            M.login(email, password)
            when = imaplib.Time2Internaldate(time.time())
            raw = msg.as_bytes()
            for folder in _sent_candidates(M):
                try:
                    typ, _ = M.append('"%s"' % folder, "\\Seen", when, raw)
                    if typ == "OK":
                        return True, None
                    last_err = "réponse %s pour le dossier %s" % (typ, folder)
                except Exception as e:
                    last_err = "%s (dossier %s)" % (e, folder)
        finally:
            try: M.logout()
            except Exception: pass
    except Exception as e:
        return False, "IMAP %s:%s — %s" % (IMAP_HOST, IMAP_PORT, e)
    return False, last_err
# --------------------------------------------------------------------------- #
def log_to_notion(commercial, destinataire, statut, objet="", copie_ok=False, detail=""):
    """Crée une ligne dans la base Notion (best-effort, ne bloque jamais l'envoi)."""
    if not (NOTION_TOKEN and NOTION_DATABASE_ID):
        return
    props = {
        "Destinataire": {"title": [{"text": {"content": (destinataire or "—")[:1900]}}]},
        "Statut": {"select": {"name": statut}},
        "Date d'envoi": {"date": {"start": datetime.now().isoformat(timespec="seconds")}},
        "Copie Envoyés": {"checkbox": bool(copie_ok)},
    }
    if objet:
        props["Objet"] = {"rich_text": [{"text": {"content": objet[:1900]}}]}
    if detail:
        props["Détail erreur"] = {"rich_text": [{"text": {"content": detail[:1900]}}]}
    if commercial:
        props["Commercial"] = {"select": {"name": commercial[:100]}}
    payload = json.dumps({"parent": {"database_id": NOTION_DATABASE_ID},
                          "properties": props}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages", data=payload, method="POST",
        headers={"Authorization": "Bearer " + NOTION_TOKEN,
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=12)
    except urllib.error.HTTPError as e:
        try: detail_msg = e.read().decode("utf-8", "ignore")[:300]
        except Exception: detail_msg = ""
        print("[notion] échec HTTP %s : %s" % (e.code, detail_msg), flush=True)
    except Exception as e:
        print("[notion] échec : %s" % e, flush=True)


def log_send(entry: dict):
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Thread d'envoi (throttling) — utilise les identifiants de l'expéditeur du job
# --------------------------------------------------------------------------- #
def run_job(job_id):
    with LOCK:
        job = JOBS[job_id]

    # Compression des gros PDF ici (en tâche de fond, ne bloque pas l'aperçu)
    if any(a.get("to_compress") for a in job["attachments"]):
        job["status"] = "préparation"
        for a in job["attachments"]:
            if a.get("to_compress"):
                try:
                    orig = os.path.getsize(a["path"])
                    newpath, newsize = compress_pdf(a["path"])
                    if newsize and newsize < orig:
                        a["path"] = newpath
                        a["size_kb"] = round(newsize / 1024)
                        a["orig_kb"] = round(orig / 1024)
                except Exception:
                    pass
                a["to_compress"] = False

    job["status"] = "running"

    s_email = job["sender_email"]
    s_pass  = job["sender_password"]
    s_name  = job["sender_name"]
    delay   = job["delay"]
    recipients = job["recipients"]

    # --- Copie "Envoyés" : UNE seule connexion IMAP réutilisée pour toute la
    # série (fiable même pour plusieurs mails rapprochés ; reconnexion si coupée).
    sent = {"M": None, "folders": None}
    def _imap_ok():
        if not SAVE_TO_SENT:
            return False
        if sent["M"] is not None:
            try:
                sent["M"].noop(); return True
            except Exception:
                sent["M"] = None
        try:
            M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
            M.login(s_email, s_pass)
            sent["M"] = M
            if sent["folders"] is None:
                sent["folders"] = _sent_candidates(M) or ["Sent"]
            return True
        except Exception:
            sent["M"] = None
            return False
    def _copy_sent(msg):
        if not SAVE_TO_SENT:
            return True, None
        last = "connexion IMAP impossible"
        for _ in range(2):                      # 1 essai + 1 reconnexion
            if not _imap_ok():
                continue
            when = imaplib.Time2Internaldate(time.time())
            raw = msg.as_bytes()
            for folder in sent["folders"]:
                try:
                    typ, _r = sent["M"].append('"%s"' % folder, "\\Seen", when, raw)
                    if typ == "OK":
                        sent["folders"] = [folder]   # fige le dossier qui marche
                        return True, None
                    last = "réponse %s (%s)" % (typ, folder)
                except Exception as e:
                    last = "%s (%s)" % (e, folder)
                    sent["M"] = None                 # force la reconnexion
                    break
        return False, last

    for idx, row in enumerate(recipients):
        if job.get("cancel"):
            job["status"] = "annulé"; break
        to_email = row["email"]
        subject = personalize(job["subject"], row)
        body = assemble_body(job["body"], row, job["inline_images"], job["signature_html"], job.get("center", False))
        result = {"email": to_email, "subject": subject, "expediteur": s_email}
        try:
            msg = build_message(s_email, s_name, to_email, subject, body,
                                job["attachments"], job["inline_images"])
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo(); server.starttls(); server.ehlo()
                server.login(s_email, s_pass)
                server.send_message(msg)
            result["status"] = "OK"
            # Copie dans Envoyés (n'échoue jamais l'envoi lui-même)
            copied, cerr = _copy_sent(msg)
            result["copie"] = "OK" if copied else "KO"
            if not copied:
                result["copie_err"] = cerr
        except Exception as e:
            result["status"] = "ERREUR"; result["error"] = str(e)

        log_send(dict(result))
        # Journal Notion (best-effort)
        detail = result.get("error") or result.get("copie_err") or ""
        log_to_notion(commercial=s_name, destinataire=to_email,
                      statut=("Envoyé" if result["status"] == "OK" else "Erreur"),
                      objet=subject, copie_ok=(result.get("copie") == "OK"), detail=detail)
        with LOCK:
            job["results"].append(result); job["done"] = idx + 1
            job["ok" if result["status"] == "OK" else "ko"] += 1

        if idx < len(recipients) - 1 and not job.get("cancel"):
            slept = 0
            while slept < delay and not job.get("cancel"):
                time.sleep(1); slept += 1

    if job["status"] != "annulé":
        job["status"] = "terminé"
    if sent["M"] is not None:               # ferme la connexion Envoyés
        try: sent["M"].logout()
        except Exception: pass
    job["sender_password"] = None  # on ne garde pas le mot de passe après usage
    tmp = job.get("tmpdir")
    if tmp and os.path.isdir(tmp):
        for f in os.listdir(tmp):
            try: os.remove(os.path.join(tmp, f))
            except OSError: pass


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    u = current_user()
    pf = {}
    edit_id = request.args.get("edit", "")
    if edit_id:
        with LOCK:
            job = JOBS.get(edit_id)
        if job and job.get("sender_email") == u["email"]:
            pf = {
                "recipients": job.get("recipients_raw", ""),
                "subject": job.get("subject", ""),
                "body": job.get("body", ""),
                "delay": job.get("delay", DEFAULT_DELAY_S),
                "signature": job.get("want_sig", True),
                "center": job.get("center", False),
                "had_files": bool(job.get("attachments") or job.get("inline_images")),
            }
    return render_template("index.html", default_delay=DEFAULT_DELAY_S,
                           max_mb=MAX_TOTAL_ATTACH_MB, ovh_limit=OVH_HOURLY_LIMIT,
                           from_name=u["name"], from_email=u["email"], pf=pf)


@app.route("/aide")
@login_required
def aide():
    return render_template("aide.html")


@app.route("/modele")
@login_required
def modele():
    """Génère et renvoie le modèle Excel des destinataires."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from io import BytesIO
    wb = Workbook(); ws = wb.active; ws.title = "Destinataires"
    ws.append(["email", "prenom", "societe"])
    for r in [["jean.dupont@entreprise.fr", "Jean", "Entreprise SA"],
              ["s.martin@societe-exemple.com", "Sophie", "Société Martin"],
              ["contact@globex.fr", "Camille", "Globex"]]:
        ws.append(r)
    thin = Side(style="thin", color="DDDDDD"); border = Border(thin, thin, thin, thin)
    hf = Font(name="Arial", bold=True, color="FFFFFF"); hfill = PatternFill("solid", fgColor="3C214B")
    for c in range(1, 4):
        cell = ws.cell(row=1, column=c)
        cell.font = hf; cell.fill = hfill
        cell.alignment = Alignment(horizontal="left", vertical="center"); cell.border = border
    ws.row_dimensions[1].height = 22
    for rr in range(2, 5):
        for c in range(1, 4):
            cell = ws.cell(row=rr, column=c)
            cell.font = Font(name="Arial", italic=True, color="888888"); cell.border = border
    ws.column_dimensions["A"].width = 34; ws.column_dimensions["B"].width = 18; ws.column_dimensions["C"].width = 26
    ws.freeze_panes = "A2"
    ws2 = wb.create_sheet("Lisez-moi")
    notes = ["Modèle — Liste de destinataires", "",
             "1. Remplace les lignes d'exemple (en gris) par tes vrais destinataires.",
             "2. Garde la 1re ligne d'en-têtes : email, prenom, societe.",
             "3. La colonne « email » est obligatoire ; « prenom » et « societe » sont optionnelles.",
             "4. Dans l'outil, glisse ce fichier dans la zone Destinataires (ou clique pour le choisir).",
             "",
             "Personnalisation : écris {prenom} ou {societe} dans ton mail,",
             "et chacun recevra sa version. Garde des en-têtes en minuscules, sans accent."]
    for i, t in enumerate(notes, start=1):
        cell = ws2.cell(row=i, column=1, value=t)
        cell.font = (Font(name="Arial", bold=True, size=14, color="3C214B") if i == 1
                     else Font(name="Arial"))
    ws2.column_dimensions["A"].width = 90
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="Modele-destinataires.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/preview", methods=["POST"])
@login_required
def preview():
    u = current_user()
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    delay = max(0, int(request.form.get("delay") or DEFAULT_DELAY_S))
    recipients_raw = request.form.get("recipients", "")
    rows, errors = parse_recipients(recipients_raw)
    if not subject: errors.append("Objet manquant.")
    if not body: errors.append("Corps du mail manquant.")
    if not rows: errors.append("Aucun destinataire valide.")

    tmpdir = tempfile.mkdtemp(prefix="relance_")
    attachments, inline_images, total_bytes = [], [], 0

    for f in request.files.getlist("attachments"):
        if not f or not f.filename: continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_ATTACH:
            errors.append(f"Pièce jointe refusée (type {ext}) : {f.filename}"); continue
        fname = secure_filename(f.filename)
        path = os.path.join(tmpdir, "att_" + fname); f.save(path)
        sz = os.path.getsize(path)
        entry = {"path": path, "filename": fname}
        # Gros PDF : on NE compresse PAS ici (trop lent dans l'aperçu).
        # On le marque ; la compression se fera à l'envoi, en tâche de fond.
        if ext == ".pdf" and sz > COMPRESS_PDF_OVER_MB * 1024 * 1024:
            entry["to_compress"] = True
        total_bytes += sz
        entry["size_kb"] = round(sz / 1024)
        attachments.append(entry)

    for f in request.files.getlist("inline"):
        if not f or not f.filename: continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_INLINE:
            errors.append(f"Image inline refusée (type {ext}) : {f.filename}"); continue
        fname = secure_filename(f.filename)
        path = os.path.join(tmpdir, "inl_" + fname); f.save(path)
        sz = os.path.getsize(path); total_bytes += sz
        inline_images.append({"path": path, "filename": fname, "cid": fname,
                              "size_kb": round(sz/1024)})

    total_mb = total_bytes / (1024*1024)
    if total_mb > MAX_TOTAL_ATTACH_MB:
        errors.append(f"Poids des pièces jointes ({total_mb:.1f} Mo) au-dessus "
                      f"du plafond de {MAX_TOTAL_ATTACH_MB} Mo par mail.")

    if not rows or not subject or not body:
        for e in errors: flash(e)
        return redirect(url_for("index"))

    # Garde-fou : variables citées dans l'objet/corps mais absentes des destinataires
    referenced = set(FIELD_RE.findall(subject + " " + body))
    available = set().union(*[set(r.keys()) for r in rows]) if rows else set()
    unresolved = referenced - available
    if unresolved:
        errors.append("Variables non remplies (resteront telles quelles dans le mail) : "
                      + ", ".join("{%s}" % v for v in sorted(unresolved))
                      + ". En liste d'adresses simple, seule {email} est connue.")

    want_sig = request.form.get("signature") == "on"
    signature_html = SIGNATURES.get(u["email"].lower(), "") if want_sig else ""
    if want_sig and not signature_html:
        errors.append("Aucune signature configurée pour ton adresse — le mail partira sans signature.")

    # Photos : miniatures légères pour l'aperçu (le mail réel garde la pleine qualité)
    photo_data = {}
    for img in inline_images:
        uri = None
        try:
            from PIL import Image as _Image
            import io as _io2
            im = _Image.open(img["path"])
            im.thumbnail((640, 640))
            buf = _io2.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=70)
            uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            uri = None
        if uri is None:   # repli : image d'origine
            try:
                with open(img["path"], "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode()
                mime = mimetypes.guess_type(img["filename"])[0] or "image/png"
                uri = f"data:{mime};base64,{b64}"
            except OSError:
                continue
        photo_data[img["cid"]] = uri

    def _links_new_tab(html):
        # Dans l'aperçu, tout lien s'ouvre dans un nouvel onglet (on ne quitte
        # pas l'outil en cliquant).
        def add(m):
            tag = m.group(0)
            if re.search(r"\btarget\s*=", tag, re.I):
                return tag
            return tag[:-1] + ' target="_blank" rel="noopener">'
        return re.sub(r"<a\b[^>]*>", add, html, flags=re.I)

    def render_preview(html):
        for cid, uri in photo_data.items():
            html = html.replace(f"cid:{cid}", uri)
        return _links_new_tab(html)

    PREVIEW_LIMIT = 5
    center = request.form.get("center_photos") == "on"
    previews = [{"email": r["email"], "subject": personalize(subject, r),
                 "body": render_preview(assemble_body(body, r, inline_images, signature_html, center))}
                for r in rows[:PREVIEW_LIMIT]]
    more_count = max(0, len(rows) - PREVIEW_LIMIT)

    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {
            "id": job_id, "status": "préparé", "subject": subject, "body": body,
            "delay": delay, "recipients": rows, "attachments": attachments,
            "inline_images": inline_images, "tmpdir": tmpdir,
            "signature_html": signature_html, "center": center,
            "recipients_raw": recipients_raw, "want_sig": want_sig,
            "sender_email": u["email"], "sender_password": None,
            "sender_name": u["name"],
            "results": [], "done": 0, "ok": 0, "ko": 0,
            "total": len(rows), "cancel": False,
        }
    est_min = round((len(rows)-1)*delay/60, 1) if len(rows) > 1 else 0
    heavy_pdf = next((a for a in attachments
                      if a["filename"].lower().endswith(".pdf") and a["size_kb"] > 12*1024), None)
    return render_template("preview.html", job_id=job_id, previews=previews,
                           attachments=attachments, inline_images=inline_images,
                           total_mb=round(total_mb, 2), delay=delay, est_min=est_min,
                           warnings=errors, count=len(rows), more_count=more_count,
                           from_name=u["name"], from_email=u["email"], heavy_pdf=heavy_pdf)


@app.route("/send", methods=["POST"])
@login_required
def send():
    job_id = request.form.get("job_id", "")
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "préparé":
        flash("Session d'envoi introuvable ou déjà lancée. Recommencez.")
        return redirect(url_for("index"))
    # Sécurité : on n'envoie que ses propres séries
    if job["sender_email"] != current_user()["email"]:
        flash("Cette série appartient à un autre expéditeur.")
        return redirect(url_for("index"))
    # Mot de passe (en mémoire) : perdu après un redémarrage -> reconnexion
    pwd = PASSWORDS.get(job["sender_email"])
    if not pwd:
        flash("Session de sécurité expirée (le service a redémarré). "
              "Reconnecte-toi puis relance l'envoi.")
        return redirect(url_for("login"))
    job["sender_password"] = pwd
    job["status"] = "running"
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return redirect(url_for("progress_page", job_id=job_id))


def identity_required(f):
    """Comme login_required mais SANS exiger le mot de passe : sert à consulter
    la progression d'un envoi déjà lancé, même après un redémarrage."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/progress/<job_id>")
@identity_required
def progress_page(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        flash("Job introuvable."); return redirect(url_for("index"))
    return render_template("progress.html", job_id=job_id, total=job["total"])


@app.route("/progress/<job_id>/data")
@identity_required
def progress_data(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "introuvable", "total": 0, "done": 0,
                        "ok": 0, "ko": 0, "results": []}), 200
    return jsonify({"status": job["status"], "total": job["total"], "done": job["done"],
                    "ok": job["ok"], "ko": job["ko"], "results": job["results"]})


@app.route("/cancel/<job_id>", methods=["POST"])
@identity_required
def cancel(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if job: job["cancel"] = True
    return jsonify({"ok": True})


@app.route("/journal")
@login_required
def journal():
    entries = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                try: entries.append(json.loads(line))
                except json.JSONDecodeError: pass
    entries.reverse()
    return render_template("journal.html", entries=entries[:500], total=len(entries))


@app.route("/journal/download")
@login_required
def journal_download():
    if not os.path.exists(LOG_FILE):
        flash("Aucun journal pour le moment."); return redirect(url_for("journal"))
    return send_file(LOG_FILE, as_attachment=True, download_name="journal_envois.jsonl")


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.errorhandler(413)
def too_large(e):
    flash(f"Fichiers trop volumineux : la limite d'upload est d'environ "
          f"{int(MAX_TOTAL_ATTACH_MB) + 20} Mo. Compresse le PDF ou envoie un lien "
          f"(rappel : au-delà de ~25 Mo, beaucoup de boîtes rejettent le mail).")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
