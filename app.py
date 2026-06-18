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
import smtplib
import imaplib
import tempfile
import threading
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

MAX_TOTAL_ATTACH_MB = float(os.environ.get("MAX_TOTAL_ATTACH_MB", "10"))
DEFAULT_DELAY_S     = int(os.environ.get("DEFAULT_DELAY_S", "45"))
OVH_HOURLY_LIMIT    = 200  # mails / heure / compte (doc OVH)

LOG_DIR  = os.environ.get("LOG_DIR", os.path.join(tempfile.gettempdir(), "relances_data"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "journal_envois.jsonl")

ALLOWED_ATTACH = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                  ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt"}
ALLOWED_INLINE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

# État en mémoire (d'où le worker unique)
SESSIONS = {}          # token -> {"email","password","name"}
JOBS = {}
LOCK = threading.Lock()


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
    tok = session.get("token")
    return SESSIONS.get(tok) if tok else None


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
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
        tok = uuid.uuid4().hex
        SESSIONS[tok] = {"email": email, "password": password,
                         "name": display_name(email)}
        session["token"] = tok
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    tok = session.pop("token", None)
    SESSIONS.pop(tok, None)
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
    sep = "\t" if "\t" in lines[0] else (";" if ";" in lines[0] else ",")
    headers = [h.strip().lower() for h in lines[0].split(sep)]
    if "email" not in headers:
        return [], ["La 1re ligne (en-tête) doit contenir une colonne 'email'. "
                    "Ex. : email,prenom,societe"]
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
def _detect_sent_folder(M):
    """Trouve le dossier Envoyés : priorité au flag spécial \\Sent, sinon au nom."""
    if SENT_FOLDER:
        return SENT_FOLDER
    try:
        typ, data = M.list()
        if typ == "OK" and data:
            flagged = named = None
            for raw in data:
                line = raw.decode("ascii", "ignore") if isinstance(raw, bytes) else str(raw)
                quoted = re.findall(r'"([^"]*)"', line)
                name = quoted[-1] if quoted else line.split()[-1].strip('"')
                if "\\Sent" in line:
                    flagged = name
                if named is None and re.search(r"(?i)(sent|envoy)", name):
                    named = name
            return flagged or named or "Sent"
    except Exception:
        pass
    return "Sent"


def save_to_sent(email, password, msg):
    """Range une copie du message dans le dossier Envoyés. Best-effort."""
    if not SAVE_TO_SENT:
        return True, None
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
        try:
            M.login(email, password)
            folder = _detect_sent_folder(M)
            M.append(folder, "\\Seen",
                     imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        finally:
            try: M.logout()
            except Exception: pass
        return True, None
    except Exception as e:
        return False, str(e)
# --------------------------------------------------------------------------- #
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
    job["status"] = "running"

    s_email = job["sender_email"]
    s_pass  = job["sender_password"]
    s_name  = job["sender_name"]
    delay   = job["delay"]
    recipients = job["recipients"]

    for idx, row in enumerate(recipients):
        if job.get("cancel"):
            job["status"] = "annulé"; break
        to_email = row["email"]
        subject = personalize(job["subject"], row)
        body = personalize(job["body"], row)
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
            copied, cerr = save_to_sent(s_email, s_pass, msg)
            result["copie"] = "OK" if copied else "KO"
            if not copied:
                result["copie_err"] = cerr
        except Exception as e:
            result["status"] = "ERREUR"; result["error"] = str(e)

        log_send(dict(result))
        with LOCK:
            job["results"].append(result); job["done"] = idx + 1
            job["ok" if result["status"] == "OK" else "ko"] += 1

        if idx < len(recipients) - 1 and not job.get("cancel"):
            slept = 0
            while slept < delay and not job.get("cancel"):
                time.sleep(1); slept += 1

    if job["status"] != "annulé":
        job["status"] = "terminé"
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
    return render_template("index.html", default_delay=DEFAULT_DELAY_S,
                           max_mb=MAX_TOTAL_ATTACH_MB, ovh_limit=OVH_HOURLY_LIMIT,
                           from_name=u["name"], from_email=u["email"])


@app.route("/preview", methods=["POST"])
@login_required
def preview():
    u = current_user()
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    delay = max(0, int(request.form.get("delay") or DEFAULT_DELAY_S))
    rows, errors = parse_recipients(request.form.get("recipients", ""))
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
        sz = os.path.getsize(path); total_bytes += sz
        attachments.append({"path": path, "filename": fname, "size_kb": round(sz/1024)})

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

    previews = [{"email": r["email"], "subject": personalize(subject, r),
                 "body": personalize(body, r)} for r in rows]

    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {
            "id": job_id, "status": "préparé", "subject": subject, "body": body,
            "delay": delay, "recipients": rows, "attachments": attachments,
            "inline_images": inline_images, "tmpdir": tmpdir,
            "sender_email": u["email"], "sender_password": u["password"],
            "sender_name": u["name"],
            "results": [], "done": 0, "ok": 0, "ko": 0,
            "total": len(rows), "cancel": False,
        }
    est_min = round((len(rows)-1)*delay/60, 1) if len(rows) > 1 else 0
    return render_template("preview.html", job_id=job_id, previews=previews,
                           attachments=attachments, inline_images=inline_images,
                           total_mb=round(total_mb, 2), delay=delay, est_min=est_min,
                           warnings=errors, count=len(rows),
                           from_name=u["name"], from_email=u["email"])


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
    job["status"] = "running"
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return redirect(url_for("progress_page", job_id=job_id))


@app.route("/progress/<job_id>")
@login_required
def progress_page(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        flash("Job introuvable."); return redirect(url_for("index"))
    return render_template("progress.html", job_id=job_id, total=job["total"])


@app.route("/progress/<job_id>/data")
@login_required
def progress_data(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "introuvable"}), 404
    return jsonify({"status": job["status"], "total": job["total"], "done": job["done"],
                    "ok": job["ok"], "ko": job["ko"], "results": job["results"]})


@app.route("/cancel/<job_id>", methods=["POST"])
@login_required
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
