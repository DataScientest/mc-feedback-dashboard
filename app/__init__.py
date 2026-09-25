from flask import Flask, request, redirect, render_template, jsonify
import config
from apscheduler.schedulers.background import BackgroundScheduler
from backend.scheduler import job_generate_weekly_report
from backend.job_state import bootstrap_weekly_marker, weekly_report_due
from backend.visits import VISITS_LOG, record_visit
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import threading
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import limiter

_logging_configured = False

# Port sans usage réseau, servant uniquement de verrou entre workers Gunicorn
SCHEDULER_LOCK_PORT = 47200

# Le socket du verrou et le scheduler sont gardés dans des globales : une
# variable locale serait collectée dès la sortie de create_app(), le port
# serait relibéré et chaque worker démarrerait alors son propre scheduler.
_scheduler_lock_socket = None
_scheduler = None


def _setup_logging():
    """File logging for the whole app (logs/app.log) + audit trail (logs/audit.log)."""
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True

    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    app_handler = RotatingFileHandler(
        "logs/app.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    app_handler.setFormatter(fmt)
    app_handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(app_handler)

    # Sans ce handler, les tracebacks n'iraient que dans logs/app.log et
    # `make logs` (docker compose logs) n'afficherait rien d'utile.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)
    root.addHandler(console_handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    audit_handler = RotatingFileHandler(
        "logs/audit.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    audit_handler.setFormatter(fmt)
    audit = logging.getLogger("audit")
    audit.addHandler(audit_handler)
    audit.setLevel(logging.INFO)
    audit.propagate = False

    # Pages vues (lu par `make visits`). Pas de rotation : le classement doit
    # couvrir tout l'historique, et deux workers Gunicorn qui feraient tourner
    # le même fichier se marcheraient dessus. Une visite pèse ~150 octets.
    visits_handler = logging.FileHandler(VISITS_LOG, encoding="utf-8")
    visits_handler.setFormatter(logging.Formatter("%(message)s"))
    visits = logging.getLogger("visits")
    visits.addHandler(visits_handler)
    visits.setLevel(logging.INFO)
    visits.propagate = False


def create_app():
    app = Flask(__name__)
    app.config["UPLOAD_FOLDER"] = "data/uploads"
    app.secret_key = config.SECRET_KEY

    _setup_logging()

    # Trust the X-Forwarded-* headers set by the reverse proxy (nginx) so
    # request.remote_addr and request.is_secure reflect the real client.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    limiter.init_app(app)
    app.after_request(record_visit)

    @app.errorhandler(429)
    def _too_many_requests(e):
        if request.endpoint == "main.login":
            return render_template(
                "login.html",
                error="Trop de tentatives de connexion. Réessayez dans une minute.",
            ), 429
        return jsonify({"status": "error", "message": "Trop de requêtes. Réessayez plus tard."}), 429

    # Endpoints interrogés en fetch() par le front : ils doivent répondre en
    # JSON même en erreur, sinon le JSON.parse() côté navigateur échoue.
    JSON_ENDPOINTS = {
        "main.refresh_data", "main.refresh_status",
        "main.refresh_techaway", "main.refresh_techaway_status",
    }

    def _wants_json():
        """Les appels fetch() du front attendent du JSON, pas une page HTML."""
        return (
            "/api/" in request.path
            or request.endpoint in JSON_ENDPOINTS
            or request.is_json
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

    def _error_page(code, title, message):
        """Page d'erreur avec la navbar et la marche à suivre (cf. error.html)."""
        if _wants_json():
            return jsonify({"status": "error", "message": message}), code
        try:
            return render_template(
                "error.html", status_code=code, title=title, message=message
            ), code
        except Exception:
            # Dernier recours : ne jamais masquer l'erreur d'origine par une
            # erreur de rendu du template.
            app.logger.exception("Rendu de error.html impossible")
            return f"{message} (erreur {code})", code

    @app.errorhandler(404)
    def _not_found(e):
        return _error_page(
            404,
            "Page introuvable",
            "Cette adresse ne correspond à aucune page du dashboard.",
        )

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def _unhandled(e):
        # Laisser passer les erreurs HTTP explicites (403, 405...) vers leur
        # propre handler plutôt que de toutes les afficher en 500.
        if isinstance(e, HTTPException) and e.code != 500:
            return e
        app.logger.exception("Unhandled error on %s", request.path)
        return _error_page(
            500,
            "Une erreur est survenue",
            "Le serveur n'a pas pu afficher cette page.",
        )

    # HTTPS enforcement - enable with ENFORCE_HTTPS=true in production
    # (behind the nginx reverse proxy). Off by default for local dev.
    enforce_https = os.getenv("ENFORCE_HTTPS", "false").strip().lower() in ("1", "true", "yes")
    if enforce_https:
        app.config.update(
            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            PREFERRED_URL_SCHEME="https",
        )

        @app.before_request
        def _force_https():
            if not request.is_secure:
                return redirect(request.url.replace("http://", "https://", 1), code=301)

        @app.after_request
        def _add_hsts(resp):
            if request.is_secure:
                resp.headers.setdefault(
                    "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                )
            return resp

    with app.app_context():
        from .routes import register_routes
        register_routes(app)

        # Register the reevaluation module as a Blueprint
        from reevaluation_time_exercise.app import bp as reeval_bp
        app.register_blueprint(reeval_bp, url_prefix="/reeval")

    # Prevent APScheduler from running twice with Flask auto reloader or Gunicorn workers
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_scheduler(app)

    return app


def _start_scheduler(app):
    """Démarre le scheduler dans un seul worker et rattrape un récap manqué."""
    global _scheduler_lock_socket, _scheduler

    if _scheduler is not None:
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Le premier worker qui réserve le port est celui qui porte le scheduler
        sock.bind(("127.0.0.1", SCHEDULER_LOCK_PORT))
    except OSError:
        sock.close()
        app.logger.info("Scheduler déjà démarré dans un autre worker.")
        return
    _scheduler_lock_socket = sock

    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    # Déclenché tous les jours à 05:00 : le job lui-même n'envoie que si le
    # récap du lundi n'est pas encore parti, ce qui rattrape une semaine ratée
    # (Nexus HS, conteneur redémarré) sans jamais envoyer deux fois.
    scheduler.add_job(
        func=job_generate_weekly_report,
        trigger="cron",
        hour=5,
        minute=0,
        id="weekly_report",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        # Sans ça, APScheduler abandonne l'exécution dès 1 s de retard
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    app.logger.info(
        "Scheduler démarré : récap hebdo du lundi, vérifié chaque jour à 05:00 (Europe/Paris)."
    )

    threading.Thread(target=_weekly_catch_up, args=(app,), daemon=True).start()


def _weekly_catch_up(app):
    """
    Au démarrage, envoie le récap si le créneau du lundi est passé sans envoi
    (typiquement : l'app était arrêtée à 05:00 lundi matin).
    """
    try:
        if bootstrap_weekly_marker() or not weekly_report_due():
            return
        app.logger.warning("Récap hebdo manquant au démarrage : rattrapage en cours.")
        job_generate_weekly_report()
    except Exception:
        app.logger.exception("Échec du rattrapage du récap hebdomadaire.")
