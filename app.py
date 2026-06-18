"""
Envoi de mails personnalisés (cold outreach B2B) — Equation
------------------------------------------------------------
Flask + SMTP OVH Email Pro. Secrets en variables d'environnement.
Construction MIME multipart (HTML + texte alterné + PJ + images inline cid:).
Throttling configurable, journal des envois, interface protégée par mot de passe.

IMPORTANT (Render) : lancer avec UN SEUL worker gunicorn (-w 1 --threads 8),
sinon l'état du job d'envoi (en mémoire) n'est pas partagé entre workers
et le suivi de progression casse.
"""

import os
import re
import time
import json
import uuid
import smtplib
import tempfile
import threading
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.utils import formataddr, make_msgid
from email import encoders
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, jsonify, send_file, flash, abort
)
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------- #
# Configuration (tout vient des variables d'environnement)
# --------------------------------------------------------------------------- #
SMTP_HOST     = os.environ.get("SMTP_HOST", "pro1.mail.ovh.net")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")          # ex. contact@equation-sie.com
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL    = os.environ.get("FROM_EMAIL", SMTP_USER)  # par défaut = compte SMTP
FROM_NAME     = os.environ.get("FROM_NAME", "Equation")
REPLY_TO      = os.environ.get("REPLY_TO", "")           # optionnel

APP_PASSWORD  = os.environ.get("APP_PASSWORD", "change-moi")
SECRET_KEY    = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# Plafonds / garde-fous
MAX_TOTAL_ATTACH_MB = float(os.environ.get("MAX_TOTAL_ATTACH_MB", "10"))   # par mail
DEFAULT_DELAY_S     = int(os.environ.get("DEFAULT_DELAY_S", "45"))
OVH_HOURLY_LIMIT    = 200  # mails / heure / compte (source : doc OVH)

# Journal : sur Render Free le disque est éphémère (perdu au redéploiement).
# Pour conserver durablement -> brancher un disque Render (Starter) et pointer
# LOG_DIR dessus. Sinon, télécharger le journal après chaque série.
LOG_DIR  = os.environ.get("LOG_DIR", os.path.join(tempfile.gettempdir(), "relances_data"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "journal_envois.jsonl")

ALLOWED_ATTACH = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                  ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt"}
ALLOWED_INLINE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # garde-fou upload global

# État des jobs d'envoi (en mémoire — d'où le worker unique)
JOBS = {}
JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Authentification (mot de passe simple)
# --------------------------------------------------------------------------- #
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["auth"] = True
            return redirect(url_for("index"))
        flash("Mot de passe incorrect.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Outils : personnalisation, parsing, conversion texte
# --------------------------------------------------------------------------- #
FIELD_RE = re.compile(r"\{(\w+)\}")


def personalize(template: str, row: dict) -> str:
    """Remplace {champ} par la valeur de la ligne (insensible aux clés inconnues)."""
    def repl(m):
        key = m.group(1)
        return str(row.get(key, m.group(0)))  # laisse {inconnu} tel quel
    return FIELD_RE.sub(repl, template or "")


def parse_recipients(raw: str):
    """
    Parse une liste collée (TSV ou CSV) avec ligne d'en-tête.
    La 1re ligne définit les noms de champs -> variables {nom}.
    'email' est obligatoire (insensible à la casse).
    Retourne (lignes[list[dict]], erreurs[list[str]]).
    """
    raw = (raw or "").strip()
    if not raw:
        return [], ["Liste de destinataires vide."]

    lines = [l for l in raw.splitlines() if l.strip()]
    sep = "\t" if "\t" in lines[0] else (";" if ";" in lines[0] else ",")
    headers = [h.strip().lower() for h in lines[0].split(sep)]

    if "email" not in headers:
        return [], ["La 1re ligne (en-tête) doit contenir une colonne 'email'. "
                    "Ex. : email,prenom,societe"]

    rows, errors = [], []
    seen = set()
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
    """Version texte brut minimale (alternative MIME) à partir du HTML."""
    txt = html or ""
    txt = re.sub(r"(?is)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?is)</p>", "\n\n", txt)
    txt = re.sub(r'(?is)<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                 lambda m: f"{re.sub('<[^>]+>', '', m.group(2))} ({m.group(1)})", txt)
    txt = re.sub(r"(?is)<[^>]+>", "", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


# --------------------------------------------------------------------------- #
# Construction du message MIME
# --------------------------------------------------------------------------- #
def build_message(to_email, subject, html_body, attachments, inline_images):
    """
    Arborescence MIME :
        mixed
        ├── related
        │   ├── alternative (text/plain + text/html)
        │   └── images inline (cid:)
        └── pièces jointes
    attachments / inline_images : listes de dicts {path, filename, ext, cid?}
    """
    root = MIMEMultipart("mixed")
    root["Subject"] = subject
    root["From"] = formataddr((FROM_NAME, FROM_EMAIL))
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
        part.add_header("Content-Disposition", "attachment",
                        filename=att["filename"])
        root.attach(part)

    return root


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #
def log_send(entry: dict):
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Thread d'envoi (avec throttling)
# --------------------------------------------------------------------------- #
def run_job(job_id):
    with JOBS_LOCK:
        job = JOBS[job_id]
    job["status"] = "running"

    subject_t = job["subject"]
    body_t = job["body"]
    delay = job["delay"]
    attachments = job["attachments"]
    inline_images = job["inline_images"]
    recipients = job["recipients"]

    for idx, row in enumerate(recipients):
        if job.get("cancel"):
            job["status"] = "annulé"
            break

        to_email = row["email"]
        subject = personalize(subject_t, row)
        body = personalize(body_t, row)
        result = {"email": to_email, "subject": subject}

        try:
            msg = build_message(to_email, subject, body,
                                attachments, inline_images)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
            result["status"] = "OK"
        except Exception as e:
            result["status"] = "ERREUR"
            result["error"] = str(e)

        log_send(dict(result))
        with JOBS_LOCK:
            job["results"].append(result)
            job["done"] = idx + 1
            if result["status"] == "OK":
                job["ok"] += 1
            else:
                job["ko"] += 1

        # Throttling : pas d'attente après le dernier envoi
        if idx < len(recipients) - 1 and not job.get("cancel"):
            slept = 0
            while slept < delay and not job.get("cancel"):
                time.sleep(1)
                slept += 1

    if job["status"] != "annulé":
        job["status"] = "terminé"

    # Nettoyage des fichiers temporaires
    tmp = job.get("tmpdir")
    if tmp and os.path.isdir(tmp):
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Routes principales
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    return render_template("index.html",
                           default_delay=DEFAULT_DELAY_S,
                           max_mb=MAX_TOTAL_ATTACH_MB,
                           ovh_limit=OVH_HOURLY_LIMIT,
                           from_name=FROM_NAME,
                           from_email=FROM_EMAIL)


@app.route("/preview", methods=["POST"])
@login_required
def preview():
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    delay = max(0, int(request.form.get("delay") or DEFAULT_DELAY_S))
    rows, errors = parse_recipients(request.form.get("recipients", ""))

    if not subject:
        errors.append("Objet manquant.")
    if not body:
        errors.append("Corps du mail manquant.")
    if not rows:
        errors.append("Aucun destinataire valide.")

    # Sauvegarde des fichiers (réutilisés à l'envoi sans ré-upload)
    tmpdir = tempfile.mkdtemp(prefix="relance_")
    attachments, inline_images = [], []
    total_bytes = 0

    for f in request.files.getlist("attachments"):
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_ATTACH:
            errors.append(f"Pièce jointe refusée (type {ext}) : {f.filename}")
            continue
        fname = secure_filename(f.filename)
        path = os.path.join(tmpdir, "att_" + fname)
        f.save(path)
        size = os.path.getsize(path)
        total_bytes += size
        attachments.append({"path": path, "filename": fname,
                            "size_kb": round(size / 1024)})

    for f in request.files.getlist("inline"):
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_INLINE:
            errors.append(f"Image inline refusée (type {ext}) : {f.filename}")
            continue
        fname = secure_filename(f.filename)
        path = os.path.join(tmpdir, "inl_" + fname)
        f.save(path)
        size = os.path.getsize(path)
        total_bytes += size
        # cid = nom de fichier nettoyé -> on référence cid:fname dans le HTML
        inline_images.append({"path": path, "filename": fname,
                              "cid": fname, "size_kb": round(size / 1024)})

    total_mb = total_bytes / (1024 * 1024)
    if total_mb > MAX_TOTAL_ATTACH_MB:
        errors.append(f"Poids des pièces jointes ({total_mb:.1f} Mo) "
                      f"au-dessus du plafond de {MAX_TOTAL_ATTACH_MB} Mo par mail.")

    if errors and (not rows or not subject or not body):
        # Erreurs bloquantes -> on revient au formulaire
        for e in errors:
            flash(e)
        return redirect(url_for("index"))

    # Aperçus personnalisés
    previews = []
    for row in rows:
        previews.append({
            "email": row["email"],
            "subject": personalize(subject, row),
            "body": personalize(body, row),
        })

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "status": "préparé",
            "subject": subject, "body": body, "delay": delay,
            "recipients": rows, "attachments": attachments,
            "inline_images": inline_images, "tmpdir": tmpdir,
            "results": [], "done": 0, "ok": 0, "ko": 0,
            "total": len(rows), "cancel": False,
        }

    est_min = round((len(rows) - 1) * delay / 60, 1) if len(rows) > 1 else 0
    return render_template("preview.html",
                           job_id=job_id, previews=previews,
                           attachments=attachments, inline_images=inline_images,
                           total_mb=round(total_mb, 2), delay=delay,
                           est_min=est_min, warnings=errors,
                           ovh_limit=OVH_HOURLY_LIMIT, count=len(rows),
                           from_name=FROM_NAME, from_email=FROM_EMAIL)


@app.route("/send", methods=["POST"])
@login_required
def send():
    job_id = request.form.get("job_id", "")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] not in ("préparé",):
        flash("Session d'envoi introuvable ou déjà lancée. Recommencez.")
        return redirect(url_for("index"))

    if not SMTP_USER or not SMTP_PASSWORD:
        flash("Identifiants SMTP non configurés (variables d'environnement).")
        return redirect(url_for("index"))

    job["status"] = "running"
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return redirect(url_for("progress_page", job_id=job_id))


@app.route("/progress/<job_id>")
@login_required
def progress_page(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        flash("Job introuvable.")
        return redirect(url_for("index"))
    return render_template("progress.html", job_id=job_id, total=job["total"])


@app.route("/progress/<job_id>/data")
@login_required
def progress_data(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "introuvable"}), 404
    return jsonify({
        "status": job["status"], "total": job["total"],
        "done": job["done"], "ok": job["ok"], "ko": job["ko"],
        "results": job["results"],
    })


@app.route("/cancel/<job_id>", methods=["POST"])
@login_required
def cancel(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job:
        job["cancel"] = True
    return jsonify({"ok": True})


@app.route("/journal")
@login_required
def journal():
    entries = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    entries.reverse()  # plus récents en premier
    return render_template("journal.html", entries=entries[:500],
                           total=len(entries))


@app.route("/journal/download")
@login_required
def journal_download():
    if not os.path.exists(LOG_FILE):
        flash("Aucun journal pour le moment.")
        return redirect(url_for("journal"))
    return send_file(LOG_FILE, as_attachment=True,
                     download_name="journal_envois.jsonl")


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
