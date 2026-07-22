import os
import io
import base64
import threading
from pathlib import Path

import pandas as pd
from flask import current_app, render_template, request, redirect, url_for, flash, session, Blueprint
from werkzeug.utils import secure_filename
from functools import wraps
import math
from backend.preprocess import preprocess_data, light_preprocess
from backend.kpi_animators import *
from backend.kpi_masterclass import *
from backend.kpi_comments import *
from backend.kpi_techaway import *
from backend.reporting import *
from backend.utils import meeting_mapping, pole_mapping
from datetime import datetime, timedelta
from flask import jsonify
from backend.nexus_client import load_credentials, save_credentials, get_auth, refresh_data_from_nexus, refresh_techaway_from_nexus
from backend.scheduler import job_generate_weekly_report
from backend.audit import audit_log
from .extensions import limiter
import logging

log = logging.getLogger(__name__)

GENERIC_ERROR = "Une erreur interne est survenue. Consultez les logs du serveur."

def _internal_error(context: str):
    """Log the full traceback server-side, show a generic message to the user."""
    current_app.logger.exception(context)
    return GENERIC_ERROR, 500

ALLOWED_EXTENSIONS = {"csv"}
bp = Blueprint("main", __name__)

@bp.before_app_request
def require_global_auth():
    # Allow static files and the login route itself
    if request.endpoint and (
        request.endpoint.startswith('static') or 
        request.endpoint == 'main.login' or 
        request.endpoint.endswith('.static')
    ):
        return
        
    creds = load_credentials()
    if not creds:
        if request.path.startswith("/api/") or "/api/" in request.path or request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"success": False, "error": "Authentication required."}), 401
        return redirect(url_for("main.login"))

@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if load_credentials():
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            return render_template("login.html", error="Please enter both email and password.")

        token = get_auth(email, password)
        if token:
            save_credentials(email, password)
            audit_log("login_success")
            return redirect(url_for("main.dashboard"))
        else:
            audit_log("login_failed")
            return render_template("login.html", error="Authentication failed. Check your Nexus credentials.")
            
    return render_template("login.html")


# File-based state so all Gunicorn worker processes see the same status
_REFRESH_STATE_FILE = "data/refresh_state.json"

def _write_refresh_state(status, message=""):
    import json as _json
    os.makedirs("data", exist_ok=True)
    with open(_REFRESH_STATE_FILE, "w") as f:
        _json.dump({"status": status, "message": message}, f)

def _read_refresh_state():
    import json as _json
    try:
        with open(_REFRESH_STATE_FILE, "r") as f:
            return _json.load(f)
    except Exception:
        return {"status": "idle", "message": ""}

_REFRESH_STATE_FILE_TA = "data/refresh_state_ta.json"

def _write_refresh_state_ta(status, message=""):
    import json as _json
    os.makedirs("data", exist_ok=True)
    with open(_REFRESH_STATE_FILE_TA, "w") as f:
        _json.dump({"status": status, "message": message}, f)

def _read_refresh_state_ta():
    import json as _json
    try:
        with open(_REFRESH_STATE_FILE_TA, "r") as f:
            return _json.load(f)
    except Exception:
        return {"status": "idle", "message": ""}

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _run_refresh_in_background():
    """Runs the full Nexus fetch + preprocess in a background thread."""
    _write_refresh_state("running", "Téléchargement des données en cours...")
    try:
        success = refresh_data_from_nexus()
        if not success:
            _write_refresh_state("error", "Échec du téléchargement via l'API Nexus.")
            return

        _write_refresh_state("running", "Traitement des données...")
        dfs_processed = []
        try:
            df_fr_raw = pd.read_csv("data/uploads/post_meeting_masterclass.csv")
        except Exception:
            df_fr_raw = None
        try:
            df_en_raw = pd.read_csv("data/uploads/post_meeting_masterclass_en.csv")
        except Exception:
            df_en_raw = None

        df_mc = preprocess_data(df_fr_raw, df_en_raw)
        if df_mc is not None and not df_mc.empty:
            dfs_processed.append(df_mc)
            
        try:
            df_ta_fr = pd.read_csv("data/uploads/techaway_post_tp.csv")
        except Exception:
            df_ta_fr = None
        try:
            df_ta_en = pd.read_csv("data/uploads/techaway_post_tp_en.csv")
        except Exception:
            df_ta_en = None

        df_ta = preprocess_data(df_ta_fr, df_ta_en)
        if df_ta is not None and not df_ta.empty:
            dfs_processed.append(df_ta)

        if dfs_processed:
            df = pd.concat(dfs_processed, ignore_index=True)
            os.makedirs("data/processed", exist_ok=True)
            df.to_csv("data/processed/merged_processed.csv", index=False)
            with open("data/last_upload.txt", "w") as f:
                f.write(datetime.now().strftime("%d/%m/%Y à %H:%M"))
            _write_refresh_state("success", "Données mises à jour avec succès !")
        else:
            _write_refresh_state("error", "Aucune donnée après le traitement.")
    except Exception as e:
        log.exception("Background data refresh failed")
        msg = str(e).lower()
        if "credentials" in msg or "auth" in msg or "invalid" in msg:
            _write_refresh_state("credentials_required", "Identifiants requis ou invalides.")
        else:
            _write_refresh_state("error", "Une erreur interne est survenue pendant l'actualisation.")


def _run_techaway_refresh_in_background():
    """Runs the full Nexus fetch + preprocess for techaway in a background thread."""
    _write_refresh_state_ta("running", "Téléchargement des données TechAway en cours...")
    try:
        success = refresh_techaway_from_nexus()
        if not success:
            _write_refresh_state_ta("error", "Échec du téléchargement via l'API Nexus.")
            return

        _write_refresh_state_ta("running", "Traitement des données...")
        dfs_processed = []
        try:
            df_fr_raw = pd.read_csv("data/uploads/post_meeting_masterclass.csv")
        except Exception:
            df_fr_raw = None
        try:
            df_en_raw = pd.read_csv("data/uploads/post_meeting_masterclass_en.csv")
        except Exception:
            df_en_raw = None

        df_mc = preprocess_data(df_fr_raw, df_en_raw)
        if df_mc is not None and not df_mc.empty:
            dfs_processed.append(df_mc)
            
        try:
            df_ta_fr = pd.read_csv("data/uploads/techaway_post_tp.csv")
        except Exception:
            df_ta_fr = None
        try:
            df_ta_en = pd.read_csv("data/uploads/techaway_post_tp_en.csv")
        except Exception:
            df_ta_en = None

        df_ta = preprocess_data(df_ta_fr, df_ta_en)
        if df_ta is not None and not df_ta.empty:
            dfs_processed.append(df_ta)

        if dfs_processed:
            df = pd.concat(dfs_processed, ignore_index=True)
            os.makedirs("data/processed", exist_ok=True)
            df.to_csv("data/processed/merged_processed.csv", index=False)
            with open("data/last_upload.txt", "w") as f:
                f.write(datetime.now().strftime("%d/%m/%Y à %H:%M"))
            _write_refresh_state_ta("success", "Données mises à jour avec succès !")
        else:
            _write_refresh_state_ta("error", "Aucune donnée après le traitement.")
    except Exception as e:
        log.exception("Background TechAway refresh failed")
        msg = str(e).lower()
        if "credentials" in msg or "auth" in msg or "invalid" in msg:
            _write_refresh_state_ta("credentials_required", "Identifiants requis ou invalides.")
        else:
            _write_refresh_state_ta("error", "Une erreur interne est survenue pendant l'actualisation.")


@bp.route("/refresh_data", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def refresh_data():
    try:
        state = _read_refresh_state()

        # If a refresh is already running, don't start another one
        if state.get("status") == "running":
            return jsonify(state)

        if request.method == "POST":
            # Use get_json(silent=True) - never raises even if Content-Type is wrong
            data = request.get_json(silent=True) or {}
            if "email" in data and "password" in data:
                save_credentials(data["email"], data["password"])

        # Check credentials before starting
        creds = load_credentials()
        if not creds:
            return jsonify({"status": "credentials_required"})

        # Write initial state and kick off background thread
        audit_log("refresh_data_started")
        _write_refresh_state("running", "Démarrage du téléchargement...")
        t = threading.Thread(target=_run_refresh_in_background, daemon=True)
        t.start()
        return jsonify({"status": "running", "message": "Téléchargement des données en cours..."})

    except Exception:
        current_app.logger.exception("refresh_data failed")
        return jsonify({"status": "error", "message": "Erreur interne du serveur."}), 500


@bp.route("/refresh_status", methods=["GET"])
def refresh_status():
    """Returns the current status of the background refresh operation."""
    try:
        return jsonify(_read_refresh_state())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@bp.route("/refresh_techaway", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def refresh_techaway():
    try:
        state = _read_refresh_state_ta()

        if state.get("status") == "running":
            return jsonify(state)

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            if "email" in data and "password" in data:
                save_credentials(data["email"], data["password"])

        creds = load_credentials()
        if not creds:
            return jsonify({"status": "credentials_required"})

        audit_log("refresh_techaway_started")
        _write_refresh_state_ta("running", "Démarrage du téléchargement...")
        t = threading.Thread(target=_run_techaway_refresh_in_background, daemon=True)
        t.start()
        return jsonify({"status": "running", "message": "Téléchargement des données en cours..."})

    except Exception:
        current_app.logger.exception("refresh_techaway failed")
        return jsonify({"status": "error", "message": "Erreur interne du serveur."}), 500

@bp.route("/refresh_techaway_status", methods=["GET"])
def refresh_techaway_status():
    try:
        return jsonify(_read_refresh_state_ta())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def _upload_dir() -> Path:
    p = Path(current_app.config.get("UPLOAD_FOLDER", "data/uploads"))
    p.mkdir(parents=True, exist_ok=True)
    Path("data/processed").mkdir(parents=True, exist_ok=True)
    return p

def _processed_path() -> Path:
    return Path("data/processed/merged_processed.csv")

def _load_processed_df() -> pd.DataFrame:
    path = _processed_path()
    if not path.exists():
        raise FileNotFoundError(
            "Aucun fichier traité. Uploade un ou deux CSV puis traite-les depuis la page d’accueil."
        )
    return pd.read_csv(path)

def _animator_column(df: pd.DataFrame) -> str:
    candidates = [
        "meeting_animator",
        "Meeting Animator",
        "meeting_animator_email",
        "Meeting Animator Email"
    ]
    for c in candidates:
        if c in df.columns:
            return c
    raise KeyError(
        f"Colonne 'Meeting Animator' introuvable. Colonnes disponibles : {df.columns.tolist()}"
    )


def _masterclass_column(df: pd.DataFrame) -> str:
    candidates = [
        "Masterclass",
        "MC",
        "masterclass",
        "mc"
    ]
    for c in candidates:
        if c in df.columns:
            return c
    raise KeyError(
        f"Colonne 'Meeting Animator' introuvable. Colonnes disponibles : {df.columns.tolist()}"
    )
    

@bp.route("/")
def index():
    return redirect(url_for("main.dashboard"))


@bp.route("/upload", methods=["GET", "POST"])
def upload_files():
    if request.method == "POST":
        # Récupération des 4 fichiers potentiels
        mc_file_fr = request.files.get("mc_file_fr")
        mc_file_en = request.files.get("mc_file_en")
        ta_file_fr = request.files.get("ta_file_fr")
        ta_file_en = request.files.get("ta_file_en")

        # Au moins un fichier requis
        if not any([mc_file_fr, mc_file_en, ta_file_fr, ta_file_en]):
            flash("Veuillez uploader au moins un fichier (FR ou EN) dans l'une des sections.")
            return redirect(request.url)

        upload_dir = _upload_dir()

        def read_csv_if_ok(file_storage):
            """Sauvegarde sécurisée + lecture CSV si fourni & extension OK, sinon None."""
            if not file_storage:
                return None
            if not allowed_file(file_storage.filename or ""):
                return None
            fname = secure_filename(file_storage.filename or "")
            if not fname:
                return None
            save_path = upload_dir / fname
            file_storage.save(str(save_path))
            return pd.read_csv(save_path)

        # Lecture des CSV (chacun peut être None)
        df_mc_fr = read_csv_if_ok(mc_file_fr)
        df_mc_en = read_csv_if_ok(mc_file_en)
        df_ta_fr = read_csv_if_ok(ta_file_fr)
        df_ta_en = read_csv_if_ok(ta_file_en)

        # Rien de valide ?
        if not any([df_mc_fr is not None, df_mc_en is not None, df_ta_fr is not None, df_ta_en is not None]):
            flash("Aucun fichier valide (.csv) n'a été détecté.")
            return redirect(request.url)

        # --- Traitement : même logique pour chaque paire (FR/EN) ---
        dfs_processed = []

        # Paire Masterclass
        if (df_mc_fr is not None) or (df_mc_en is not None):
            df_proc_mc = preprocess_data(df_mc_fr, df_mc_en)
            if df_proc_mc is not None and not df_proc_mc.empty:
                dfs_processed.append(df_proc_mc)

        # Paire TechAway
        if (df_ta_fr is not None) or (df_ta_en is not None):
            df_proc_ta = preprocess_data(df_ta_fr, df_ta_en)
            if df_proc_ta is not None and not df_proc_ta.empty:
                dfs_processed.append(df_proc_ta)

        if not dfs_processed:
            flash("Les fichiers fournis n'ont produit aucune donnée après traitement.")
            return redirect(request.url)

        # Concat finale (empilement vertical)
        df_processed = pd.concat(dfs_processed, ignore_index=True)

        # Sauvegarde unique vers le chemin déjà utilisé par l'app
        df_processed.to_csv(_processed_path(), index=False)

        # Enregistrement de la date et l'heure de l'upload
        with open("data/last_upload.txt", "w") as f:
            f.write(datetime.now().strftime("%d/%m/%Y à %H:%M"))

        audit_log("files_uploaded")
        flash("Fichiers uploadés et traités avec succès.")
        return redirect(url_for("main.dashboard"))

    # Récupération de la date de dernier upload pour l'affichage
    last_upload_date = None
    if os.path.exists("data/last_upload.txt"):
        with open("data/last_upload.txt", "r") as f:
            last_upload_date = f.read().strip()

    return render_template("upload.html", last_upload_date=last_upload_date)


@bp.route("/dashboard")
def dashboard():
    try:
        processed_path = os.path.join("data/processed", "merged_processed.csv")
        if not os.path.exists(processed_path):
            return render_template("no_data.html")

        df = light_preprocess(pd.read_csv(processed_path))

        WoF = wall_of_fame(df)
        WoS = wall_of_not_fame(df)
        comments = get_masterclasses_with_negative_feedback(df)

        comments_html = ""
                
        if isinstance(comments, str):
            comments_html = f"<p>{comments}</p>"
        elif hasattr(comments, "empty") and comments.empty:
            comments_html = "<p>Aucun avis négatif à afficher.</p>"
        else:
            for _, row in comments.iterrows():
                mc = row.get("Masterclass")
                n_neg = row.get("Nombre Avis Négatifs", 0)
                n_sess = row.get("Nombre Sessions 30j", 0)

                comments_html += f"<div class='mb-2 text-primary fw-bold'>{mc} - {n_neg} avis négatifs sur {n_sess} sessions</div>"

                avis_list = row.get("Avis Négatifs", [])
                if isinstance(avis_list, pd.DataFrame):
                    avis_iter = avis_list.to_dict(orient="records")
                elif isinstance(avis_list, list):
                    avis_iter = avis_list
                else:
                    avis_iter = []

                for avis in avis_iter:
                    meeting_id = avis.get("Meeting ID")
                    mc_link = f"https://nexus.datascientest.com/meeting/{meeting_id}" if meeting_id else "#"

                    user_id = avis.get("User ID")
                    user_link = f"https://nexus.datascientest.com/user/{user_id}" if user_id else "#"

                    date_val = avis["Meeting Start Date"]
                    date_str = format_date(date_val)

                    animator = avis.get("Meeting Animator", "")
                    comment_text = (avis.get("Comment") or "").strip().replace("\n", " ")
                    author_name = avis.get("User Fullname", "")
                    rating = avis.get("Answer") or avis.get("Content Grade") or avis.get("Note")
                    rating_str = f"{rating}/5" if rating is not None else "-/5"

                    comments_html += (
                        f"<div class='ps-3 border-start mb-3'>"
                        f"<div><span class='text-muted'>Date :</span> <a href=\"{mc_link}\" target=\"_blank\" rel=\"noopener\" class='text-decoration-none'>{date_str}</a></div>"
                        f"<div><span class='text-muted'>Animateur :</span> {animator}</div>"
                        f"<div><span class='text-muted'>Auteur :</span> <a href=\"{user_link}\" target=\"_blank\" rel=\"noopener\" class='text-decoration-none'>{author_name}</a></div>"
                        f"<div><span class='text-muted'>Note :</span> {rating_str}</div>"
                        f"<div class='mt-1 fst-italic'>\"{comment_text}\"</div>"
                        f"</div>"
                    )

                comments_html += "<hr>"

        return render_template("dashboard.html", wof=WoF, wos=WoS, comments=comments, comments_html=comments_html)
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/animateurs")
def animateurs():
    """
    Liste de tous les animateurs, groupés par rôle, avec recherche par nom.
    """
    try:
        df = light_preprocess(_load_processed_df())

        q = (request.args.get("q") or "").strip().lower()

        col = _animator_column(df)  # colonne contenant le nom d'animateur
        role_col = "Animator Role" if "Animator Role" in df.columns else ("Role" if "Role" in df.columns else None)

        start_30 = datetime.now() - timedelta(days=30)
        df_30j = filter_by_date_range(df, start_date=start_30)
        active_animators_30j = set(df_30j[col].dropna().unique()) if col in df_30j.columns else set()

        if role_col:
            sub = df[[col, role_col]].dropna().drop_duplicates()
            grouped = {}
            for _, r in sub.iterrows():
                name = str(r[col]).strip()
                role = str(r[role_col]).strip()
                if not name or not role:
                    continue
                if q and q not in name.lower():
                    continue
                # Stocker sous forme de dict
                item = {"name": name, "active": name in active_animators_30j}
                if role not in grouped:
                    grouped[role] = []
                if name not in [x["name"] for x in grouped[role]]:
                    grouped[role].append(item)

            # tri des rôles et des noms + supprime les rôles vides après filtre
            animateurs_by_role = {
                role: sorted(items, key=lambda x: x["name"].lower())
                for role, items in sorted(grouped.items(), key=lambda x: x[0].lower())
                if items
            }
        else:
            # fallback si pas de colonne rôle
            names = [str(a).strip() for a in df[col].dropna().unique()]
            if q:
                names = [n for n in names if q in n.lower()]
            
            items = [{"name": n, "active": n in active_animators_30j} for n in names]
            animateurs_by_role = {"Autres": sorted(items, key=lambda x: x["name"].lower())}

        return render_template("animateurs.html", animateurs_by_role=animateurs_by_role, q=request.args.get("q", ""))
    except FileNotFoundError as e:
        # Message métier volontairement affichable (aucun fichier traité)
        return str(e), 404
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")




@bp.route("/animateur/<path:animateur>")
def animateur_detail(animateur: str):
    """
    Page dédiée à un animateur (email unique).
    """
    try:
        df = light_preprocess(_load_processed_df())

        # Métriques générales
        sessions_total = get_nb_sessions_animateur(df, animateur)
        moyenne_generale = get_moyenne_animateur(df, animateur)
        classement = get_position_classement_animateur(df, animateur)

        best = get_best_mc_animateur(df, animateur)
        worst = get_worst_mc_animateur(df, animateur)

        # 30 derniers jours
        sessions_30j = get_nb_sessions_animateur_30j(df, animateur)
        moyenne_30j = get_moyenne_animateur_30j(df, animateur)
        classement_30j = get_position_classement_animateur_30j(df, animateur)

        df_sessions_30j = get_sessions_30j_animateur(df, animateur)
        df_mc = get_masterclasses_by_animateur(df, animateur)

        # Tables vers HTML
        def df_table_payload(d: pd.DataFrame):
            if d is None or d.empty:
                return {"columns": [], "rows": []}
            return {"columns": list(d.columns), "rows": d.to_dict(orient="records")}

        payload_sessions_30j = df_table_payload(df_sessions_30j)
        payload_mc = df_table_payload(df_mc)

        best_label = (
            f"{best['Masterclass']}" if best is not None else "Aucune masterclass trouvée")
        worst_label = (
            f"{worst['Masterclass']}" if worst is not None else "Aucune masterclass trouvée")
        
        days = request.args.get("days", default=30, type=int)
        negative_only = request.args.get("negative_only", type=int)
        seuil = {1, 2, 3} if negative_only else {1, 2, 3, 4, 5}
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=max(1, days))
        
        col_anim = _animator_column(df)
        df_animator_filter = df[df[col_anim] == animateur]
        comments_df = get_comments(df_animator_filter, seuil=seuil, longueur_min_avis=3, start_date=start_date, end_date=end_date)
        comments = comments_df.to_dict('records') if not comments_df.empty else []
        
        context = {
            "animateur": animateur,
            "sessions_total": sessions_total,
            "moyenne_generale": moyenne_generale,
            "classement": classement if classement else "Non classé",
            "best_label": best_label,
            "worst_label": worst_label,
            "sessions_30j": sessions_30j,
            "moyenne_30j": round(moyenne_30j, 2) if moyenne_30j else None,
            "classement_30j": classement_30j if classement_30j else "Non classé",
            "sessions_30j_table": payload_sessions_30j,
            "mc_table": payload_mc,
            "days": days,
            "negative_only": bool(negative_only),
            "comments": comments,
        }
        return render_template("animateur.html", **context)

    except FileNotFoundError as e:
        # Message métier volontairement affichable (aucun fichier traité)
        return str(e), 404
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/masterclasses")
def masterclasses():
    """
    Liste de toutes les masterclass.
    """
    try:
        df = light_preprocess(_load_processed_df())

        # Liste cliquable des masterclass
        col = _masterclass_column(df)
        all_masterclasses = sorted(m for m in df[col].dropna().unique())

        mapped_masterclasses = []
        other_masterclasses = []

        # Récupération des valeurs de mapping (les noms officiels)
        mapping_values = set(meeting_mapping.values())

        for mc in all_masterclasses:
            avg_grade = get_moyenne_masterclass(df, mc)
            item = {"name": mc, "avg": avg_grade if avg_grade is not None else 0}
            
            if mc in mapping_values:
                mapped_masterclasses.append(item)
            else:
                other_masterclasses.append(item)

        # Regrouper les masterclasses officielles par pôle
        masterclasses_by_pole = {}
        for item in mapped_masterclasses:
            poles = pole_mapping.get(item["name"], [])
            if not poles:
                poles = ["Non assigné"]
                
            for p in poles:
                if p not in masterclasses_by_pole:
                    masterclasses_by_pole[p] = []
                masterclasses_by_pole[p].append(item)
                
        # Tri des listes par moyenne décroissante et calcul de la moyenne du pôle
        pole_stats = {}
        for p in masterclasses_by_pole:
            masterclasses_by_pole[p].sort(key=lambda x: x["avg"], reverse=True)
            valid_avgs = [item["avg"] for item in masterclasses_by_pole[p] if item["avg"] > 0]
            if valid_avgs:
                pole_stats[p] = round(sum(valid_avgs) / len(valid_avgs), 2)
            else:
                pole_stats[p] = 0
            
        # Couleurs dynamiques HSL pour les pôles
        pole_colors = {}
        valid_stats = [v for v in pole_stats.values() if v > 0]
        if valid_stats:
            min_s = min(valid_stats)
            max_s = max(valid_stats)
            for p, val in pole_stats.items():
                if val <= 0:
                    pole_colors[p] = "background-color: #6c757d; color: white;"
                else:
                    if max_s == min_s:
                        pct = 0.5
                    else:
                        pct = (val - min_s) / (max_s - min_s)
                    hue = int(pct * 120)
                    pole_colors[p] = f"background-color: hsl({hue}, 70%, 45%); color: white;"
        else:
            for p in pole_stats:
                pole_colors[p] = "background-color: #6c757d; color: white;"
            
        # Tri des pôles par ordre alphabétique, avec 'Non assigné' à la fin
        sorted_poles = sorted([p for p in masterclasses_by_pole.keys() if p != "Non assigné"])
        if "Non assigné" in masterclasses_by_pole:
            sorted_poles.append("Non assigné")
            
        ordered_masterclasses_by_pole = {p: masterclasses_by_pole[p] for p in sorted_poles}

        other_masterclasses.sort(key=lambda x: x["avg"], reverse=True)

        return render_template(
            "masterclasses.html", 
            masterclasses_by_pole=ordered_masterclasses_by_pole, 
            other_masterclasses=other_masterclasses,
            pole_stats=pole_stats,
            pole_colors=pole_colors
        )
    except FileNotFoundError as e:
        # Message métier volontairement affichable (aucun fichier traité)
        return str(e), 404
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/masterclass/<path:masterclass>")
def masterclass_detail(masterclass: str):
    """
    Page dédiée à une masterclass (nom unique).
    """
    try:
        df = light_preprocess(_load_processed_df())
        
        days = request.args.get("days", default=30, type=int)
        negative_only = request.args.get("negative_only", type=int)
        seuil = {1, 2, 3} if negative_only else {1, 2, 3, 4, 5}
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=max(1, days))

        # Métriques générales
        sessions_total = get_nb_sessions_masterclass(df, masterclass)
        moyenne_generale = get_moyenne_masterclass(df, masterclass)
        classement = get_position_classement_masterclass(df, masterclass)

        # 30 derniers jours
        sessions_30j = get_nb_sessions_masterclass_30j(df, masterclass)
        moyenne_30j = get_moyenne_masterclass_30j(df, masterclass)

        df_mc_animateurs = get_mc_best_animateurs(df, masterclass)
        df_mc_animateurs_30j = get_mc_best_animateurs(filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30)), masterclass)
        
        classement_30j = get_position_classement_masterclass_30j(df, masterclass)

        # Tables vers HTML
        def df_table_payload(d: pd.DataFrame):
            if d is None or d.empty:
                return {"columns": [], "rows": []}
            return {"columns": list(d.columns), "rows": d.to_dict(orient="records")}

        payload_sessions_30j = df_table_payload(df_mc_animateurs_30j)
        payload_mc = df_table_payload(df_mc_animateurs)

        df_mc_filter = df[df["Masterclass"] == masterclass]
        comments_df = get_comments(df_mc_filter, seuil=seuil, longueur_min_avis=3, start_date=start_date, end_date=end_date)
        comments = comments_df.to_dict('records') if not comments_df.empty else []

        context = {
            "masterclass": masterclass,
            "sessions_total": sessions_total,
            "moyenne_generale": moyenne_generale,
            "classement": classement if classement else "Non classé",
            "sessions_30j": sessions_30j,
            "moyenne_30j": round(moyenne_30j, 2) if moyenne_30j else None,
            "classement_30j": classement_30j if classement_30j else "Non classé",
            "sessions_30j_table": payload_sessions_30j,
            "mc_table": payload_mc,
            "days": days,
            "negative_only": bool(negative_only),
            "comments": comments
        }
        return render_template("masterclass.html", **context)

    except FileNotFoundError as e:
        # Message métier volontairement affichable (aucun fichier traité)
        return str(e), 404
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")
    
    
# routes.py (extrait)
@bp.route("/leaderboard")
def leaderboard():
    try:
        processed_path = os.path.join("data/processed", "merged_processed.csv")
        if not os.path.exists(processed_path):
            return render_template("no_data.html")

        df = light_preprocess(pd.read_csv(processed_path))

        # --- NOUVEAU : collecte des rôles disponibles ---
        if "Animator Role" in df.columns:
            roles = sorted(df["Animator Role"].dropna().astype(str).unique().tolist())
        else:
            roles = []

        # Slider / query param: seuil minimum de sessions
        min_sessions = request.args.get("min_sessions", default=20, type=int)
        if not isinstance(min_sessions, int) or min_sessions < 1:
            min_sessions = 20

        # --- NOUVEAU : lecture & application du filtre de rôles (multi) ---
        # /leaderboard?...&role=cyber&role=expert
        selected_roles = request.args.getlist("role")
        # sécurité : on ne garde que les valeurs connues
        selected_roles = [r for r in selected_roles if r in roles]

        if selected_roles:
            # filtrer le DF principal, tout le reste (30j, etc.) en hérite
            df = df[df["Animator Role"].astype(str).isin(selected_roles)].copy()

        # DFs filtrés sur le min_sessions (comme avant)
        df_anim = get_animateurs_plus_de_20_sessions(df, min_sessions)
        df_mc = get_masterclass_plus_de_10_sessions(df, min_sessions)

        lb_anim = get_leaderboard_animateurs(df_anim, min_sessions)
        lb_anim30 = get_leaderboard_animateurs_30j(df)
        lb_mc = get_leaderboard_masterclasses(df_mc, min_sessions)
        lb_mc30 = get_leaderboard_masterclasses_30j(df)

        # Selections (left/right), defaults: animateurs | animateurs 30j
        left_key = (request.args.get("left") or "anim").lower()
        right_key = (request.args.get("right") or "anim30").lower()

        options = {
            "anim": {"label": "Animateurs (tout)", "type": "animateur", "df": lb_anim},
            "anim30": {"label": "Animateurs (30 jours)", "type": "animateur", "df": lb_anim30},
            "mc": {"label": "Masterclasses (tout)", "type": "masterclass", "df": lb_mc},
            "mc30": {"label": "Masterclasses (30 jours)", "type": "masterclass", "df": lb_mc30},
        }
        if left_key not in options:
            left_key = "anim"
        if right_key not in options:
            right_key = "anim30"

        def prep(table, typ):
            if table is None or getattr(table, "empty", True):
                return {"columns": [], "rows": [], "label_col": None, "type": typ}
            cols = list(table.columns)  # rang | label | moyenne
            label_col = cols[1] if len(cols) >= 2 else None
            rows = table[cols].to_dict(orient="records")
            return {"columns": cols, "rows": rows, "label_col": label_col, "type": typ}

        left = prep(options[left_key]["df"], options[left_key]["type"])
        right = prep(options[right_key]["df"], options[right_key]["type"])

        return render_template(
            "leaderboard.html",
            left=left,
            right=right,
            left_label=options[left_key]["label"],
            right_label=options[right_key]["label"],
            left_key=left_key,
            right_key=right_key,
            all_options=options,
            min_sessions=min_sessions,
            # --- NOUVEAU : passer les rôles et la sélection à Jinja ---
            roles=roles,
            selected_roles=selected_roles,
        )
    except FileNotFoundError as e:
        # Message métier volontairement affichable (aucun fichier traité)
        return str(e), 404
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")




@bp.route("/slack_bot", methods=["GET", "POST"])
def slack_bot():
    """
    Page dédiée au Slack Bot.
    """
    try:
        message = None

        if request.method == "POST":
            df = light_preprocess(_load_processed_df())
            action = request.form.get("action")

            if action == "wof":
                send_top_animators_message(df)
                message = "Message Wall of Fame envoyé sur Slack."
            elif action == "wos":
                send_not_top_animators_message(df)
                message = "Message Wall of Not Fame envoyé sur Slack."
            elif action == "bad":
                send_bad_sessions(df)
                message = "Récap commentaires négatifs envoyé sur Slack."
            else:
                message = "Action inconnue."

            if action in ("wof", "wos", "bad"):
                audit_log("slack_message_sent", action)

        return render_template("slack_bot.html", message=message)
    except FileNotFoundError as e:
        # Message métier volontairement affichable (aucun fichier traité)
        return str(e), 404
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/techaway")
def techaway():
    try:
        processed_path = os.path.join("data/processed", "merged_processed.csv")
        if not os.path.exists(processed_path):
            return "Aucun fichier traité disponible. Veuillez utiliser l'endpoint /upload."

        df = light_preprocess(pd.read_csv(processed_path))
        
        T4A_satisf = get_satisfaction_moyenne_niveau(df, verticale="TECHFORALL", niveau="0")
        DA_N1_satisf = get_satisfaction_moyenne_niveau(df, verticale="DATAANALYSIS", niveau="1")
        DA_N2_satisf = get_satisfaction_moyenne_niveau(df, verticale="DATAANALYSIS", niveau="2")
        DA_N3_satisf = get_satisfaction_moyenne_niveau(df, verticale="DATAANALYSIS", niveau="3")
        PROG_N1_satisf = get_satisfaction_moyenne_niveau(df, verticale="PROGRAMING", niveau="1")
        PROG_N2_satisf = get_satisfaction_moyenne_niveau(df, verticale="PROGRAMING", niveau="2")
        PROG_N3_satisf = get_satisfaction_moyenne_niveau(df, verticale="PROGRAMING", niveau="3")
        NOCODE_N1_satisf = get_satisfaction_moyenne_niveau(df, verticale="NOCODE", niveau="1")
        NOCODE_N2_satisf = get_satisfaction_moyenne_niveau(df, verticale="NOCODE", niveau="2")
        NOCODE_N3_satisf = get_satisfaction_moyenne_niveau(df, verticale="NOCODE", niveau="3")
        CYBER_N1_satisf = get_satisfaction_moyenne_niveau(df, verticale="CYBERSECURITY", niveau="1")
        CYBER_N2_satisf = get_satisfaction_moyenne_niveau(df, verticale="CYBERSECURITY", niveau="2")
        CYBER_N3_satisf = get_satisfaction_moyenne_niveau(df, verticale="CYBERSECURITY", niveau="3")
        AI_N1_satisf = get_satisfaction_moyenne_niveau(df, verticale="AI", niveau="1")
        AI_N2_satisf = get_satisfaction_moyenne_niveau(df, verticale="AI", niveau="2")
        AI_N3_satisf = get_satisfaction_moyenne_niveau(df, verticale="AI", niveau="3")
        
        def style_and_label(val):
            # Convertit une note (1..5) en couleur HSL + libellé "x.xx/5"
            if val is None:
                return "background: #9ca3af;", "-"
            try:
                v = float(val)
            except Exception:
                return "background: #9ca3af;", "-"
            if math.isnan(v):
                return "background: #9ca3af;", "-"
            v = max(1.0, min(5.0, v))
            hue = int(round((v / 5.0) * 120))  # 1->rouge, 5->vert
            return f"background: hsl({hue}, 70%, 45%);", f"{v:.2f}/5"
        
        
        # ➜ Nouveau: style de remplissage pour les NIVEAUX (2.5 → 50%, 4.0 → 80%, etc.)
        def fill_style(val):
            try:
                v = float(val)
                if math.isnan(v):
                    raise ValueError
            except Exception:
                pct = 0.0
            else:
                v = max(0.0, min(5.0, v))
                pct = (v / 5.0) * 100.0
            # fond vert rempli jusqu’à pct%, puis gris au-dessus
            return (
                f"background: linear-gradient(to top, "
                f"#16a34a {pct:.0f}%, #f3f4f6 {pct:.0f}%); "
                f"border: 1px solid #e5e7eb;"
            )

        # ====== HERO : TECHFORALL ======
        hero_name = "TECHFORALL"
        hero_overall = get_satisfaction_moyenne_verticale(df, hero_name)
        hero_style, hero_score_label = style_and_label(hero_overall)

        # Un seul bloc "Niveau 0" cliquable (fusion des 3 niveaux précédents)
        hero_levels = [{
            "name": "Niveau 0",
            "code": "N0",
            "style": fill_style(T4A_satisf),
            "score_label": style_and_label(T4A_satisf)[1],
        }]

        # --- Autres verticales + leurs niveaux ---
        level_vars = {
            "DATAANALYSIS": {"N1": DA_N1_satisf, "N2": DA_N2_satisf, "N3": DA_N3_satisf},
            "PROGRAMING":   {"N1": PROG_N1_satisf, "N2": PROG_N2_satisf, "N3": PROG_N3_satisf},
            "NOCODE":       {"N1": NOCODE_N1_satisf, "N2": NOCODE_N2_satisf, "N3": NOCODE_N3_satisf},
            "CYBERSECURITY":{"N1": CYBER_N1_satisf, "N2": CYBER_N2_satisf, "N3": CYBER_N3_satisf},
            "AI":           {"N1": AI_N1_satisf, "N2": AI_N2_satisf, "N3": AI_N3_satisf},
        }

        verticals = ["DATAANALYSIS", "PROGRAMING", "NOCODE", "CYBERSECURITY", "AI"]
        tiles = []
        for v in verticals:
            overall = get_satisfaction_moyenne_verticale(df, v)
            tile_style, tile_label = style_and_label(overall)

            levels = []
            for code, label in [("N1", "Niveau 1"), ("N2", "Niveau 2"), ("N3", "Niveau 3")]:
                lvl_val = level_vars.get(v, {}).get(code)
                levels.append({
                    "name": label,
                    "code": code,
                    "style": fill_style(lvl_val),
                    "score_label": style_and_label(lvl_val)[1],
                })

            tiles.append({"name": v, "style": tile_style, "score_label": tile_label, "levels": levels})

        # ====== META par TP (pour Hero N0 + autres verticales N1..N3) ======
        tp_meta = {}
        
        T4A_TP1_satisf = get_moyenne_masterclass(df, "TECHFORALL-N0 - TP 1")
        T4A_TP2_satisf = get_moyenne_masterclass(df, "TECHFORALL-N0 - TP 2")
        T4A_TP3_satisf = get_moyenne_masterclass(df, "TECHFORALL-N0 - TP 3")
        T4A_TP4_satisf = get_moyenne_masterclass(df, "TECHFORALL-N0 - TP 4")


        # Hero N0
        for tp, val in [(1, T4A_TP1_satisf), (2, T4A_TP2_satisf), (3, T4A_TP3_satisf), (4, T4A_TP4_satisf)]:
            v = 0.0 if val is None or (isinstance(val, float) and math.isnan(val)) else float(val)
            pct = int(round(max(0.0, min(5.0, v)) / 5.0 * 100))
            tp_meta[f"{hero_name}_N0_TP{tp}"] = {"pct": pct, "label": f"{v:.2f}/5" if val is not None else "-"}

        # Autres verticales
        for v in verticals:
            for code in ["N1", "N2", "N3"]:
                for tp in [1, 2, 3, 4]:
                    val = get_moyenne_masterclass(df, f"{v}-{code} - TP {tp}")
                    vv = 0.0 if val is None or (isinstance(val, float) and math.isnan(val)) else float(val)
                    pct = int(round(max(0.0, min(5.0, vv)) / 5.0 * 100))
                    tp_meta[f"{v}_{code}_TP{tp}"] = {"pct": pct, "label": f"{vv:.2f}/5" if val is not None else "-"}

        return render_template(
            "techaway.html",
            hero_name=hero_name,
            hero_style=hero_style,
            hero_score_label=hero_score_label,
            hero_levels=hero_levels,   # contient un seul niveau N0
            tiles=tiles,
            tp_meta=tp_meta,
        )

    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/commentaires", methods=["GET"])
def commentaires():
    """
    Page listant les commentaires des masterclasses avec filtres :
    - étoiles (Content Grade) multi-choix
    - nombre minimum de mots
    - période en jours (par défaut 30)
    """
    try:
        processed_path = os.path.join("data/processed", "merged_processed.csv")
        if not os.path.exists(processed_path):
            return "Aucun fichier traité disponible. Veuillez utiliser l'endpoint /upload."

        df = light_preprocess(pd.read_csv(processed_path))

        # Widgets (GET params)
        selected_grades = request.args.getlist("grades", type=int)  # ex: ?grades=1&grades=3
        min_words = request.args.get("min_words", default=3, type=int)
        days = request.args.get("days", default=30, type=int)

        # Période
        end_date = datetime.now()
        start_date = end_date - timedelta(days=max(1, days))

        # 'seuil' : si rien sélectionné -> Ellipsis (...) => par défaut {1,2,3}
        seuil = selected_grades if selected_grades else ...

        comments_df = get_comments(
            df,
            seuil=seuil,
            longueur_min_avis=min_words,
            start_date=start_date,
            end_date=end_date,
        )

        return render_template(
            "commentaires.html",
            comments_df=comments_df,
            selected_grades=set(selected_grades),
            min_words=min_words,
            days=days,
        )

    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/set_language/<lang>")
def set_language(lang):
    if lang in ['FR', 'EN', 'ALL']:
        session['lang_filter'] = lang
    return redirect(request.referrer or url_for('main.dashboard'))


def register_routes(app):
    app.register_blueprint(bp)
