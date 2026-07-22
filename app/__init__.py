from flask import Flask, request, redirect, render_template, jsonify
import config
from apscheduler.schedulers.background import BackgroundScheduler
from backend.scheduler import job_generate_weekly_report
import logging
from logging.handlers import RotatingFileHandler
import os
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import limiter

_logging_configured = False


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


def create_app():
    app = Flask(__name__)
    app.config["UPLOAD_FOLDER"] = "data/uploads"
    app.secret_key = config.SECRET_KEY

    _setup_logging()

    # Trust the X-Forwarded-* headers set by the reverse proxy (nginx) so
    # request.remote_addr and request.is_secure reflect the real client.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    limiter.init_app(app)

    @app.errorhandler(429)
    def _too_many_requests(e):
        if request.endpoint == "main.login":
            return render_template(
                "login.html",
                error="Trop de tentatives de connexion. Réessayez dans une minute.",
            ), 429
        return jsonify({"status": "error", "message": "Trop de requêtes. Réessayez plus tard."}), 429

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
        import socket
        try:
            # Bind to a local port to ensure only one worker starts the scheduler
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 47200))
        except socket.error:
            app.logger.info("Scheduler already started in another worker.")
        else:
            scheduler = BackgroundScheduler()
            # Run every Monday at 05:00 AM
            scheduler.add_job(func=job_generate_weekly_report, trigger="cron", day_of_week='mon', hour=5, minute=0)
            scheduler.start()

    return app
