import os
import json
import math
import logging
import threading
from pathlib import Path

import pandas as pd
from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta

from backend.preprocess import preprocess_data, light_preprocess
from backend.kpi_animators import *
from backend.kpi_masterclass import *
from backend.kpi_comments import *
from backend.kpi_techaway import *
from backend.reporting import *
from backend.utils import meeting_mapping, pole_mapping, format_paris, read_last_update, write_last_update
from backend.nexus_client import load_credentials, save_credentials, get_auth, refresh_data_from_nexus, refresh_techaway_from_nexus
from backend.audit import audit_log
from .extensions import limiter

log = logging.getLogger(__name__)

GENERIC_ERROR = "Une erreur interne est survenue. Consultez les logs du serveur."

def _internal_error(context: str):
    """Log the full traceback server-side, show a generic message to the user."""
    current_app.logger.exception(context)
    return GENERIC_ERROR, 500

ALLOWED_EXTENSIONS = {"csv"}
bp = Blueprint("main", __name__)


@bp.app_context_processor
def inject_last_update():
    """Date de dernière mise à jour (heure de Paris) affichée dans la barre de navigation."""
    return {"last_update": format_paris(read_last_update())}


@bp.app_template_filter("date_fr")
def date_fr(value, with_time=False):
    """Formatte une date pour l'affichage : 05/10/2023 (ou 05/10/2023 14:30)."""
    if value is None or value == "":
        return ""
    try:
        moment = pd.to_datetime(value)
    except (ValueError, TypeError):
        return str(value)
    if pd.isna(moment):
        return ""
    return moment.strftime("%d/%m/%Y %H:%M" if with_time else "%d/%m/%Y")

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
_REFRESH_STATE_FILE_TA = "data/refresh_state_ta.json"

# Fichiers bruts déposés par Nexus ou par l'upload manuel, groupés par paire FR/EN
_SOURCE_FILES = [
    ("data/uploads/post_meeting_masterclass.csv", "data/uploads/post_meeting_masterclass_en.csv"),
    ("data/uploads/techaway_post_tp.csv", "data/uploads/techaway_post_tp_en.csv"),
]


def _write_refresh_state(status, message="", path=_REFRESH_STATE_FILE):
    os.makedirs("data", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"status": status, "message": message}, f)

def _read_refresh_state(path=_REFRESH_STATE_FILE):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"status": "idle", "message": ""}

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _read_csv_or_none(path):
    try:
        return pd.read_csv(path)
    except (OSError, ValueError, pd.errors.ParserError):
        return None


def _reprocess_source_files() -> bool:
    """
    Retraite les CSV bruts présents sur le disque vers merged_processed.csv.
    Retourne False si aucune donnée exploitable n'a été produite.
    """
    dfs_processed = []
    for fr_path, en_path in _SOURCE_FILES:
        df = preprocess_data(_read_csv_or_none(fr_path), _read_csv_or_none(en_path))
        if df is not None and not df.empty:
            dfs_processed.append(df)

    if not dfs_processed:
        return False

    merged = pd.concat(dfs_processed, ignore_index=True)
    _processed_path().parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(_processed_path(), index=False)
    write_last_update()
    return True


def _run_refresh_in_background(fetch, state_file, key, subject):
    """Télécharge depuis Nexus puis retraite les CSV, dans un thread dédié."""
    def report(status, message):
        _write_refresh_state(status, message, state_file)

    report("running", f"Téléchargement des données {subject}en cours...")
    try:
        if not fetch():
            report("error", "Échec du téléchargement via l'API Nexus.")
            return

        report("running", "Traitement des données...")
        if _reprocess_source_files():
            report("success", "Données mises à jour avec succès !")
        else:
            report("error", "Aucune donnée après le traitement.")
    except Exception as e:
        log.exception("Background %s refresh failed", key)
        msg = str(e).lower()
        if "credentials" in msg or "auth" in msg or "invalid" in msg:
            report("credentials_required", "Identifiants requis ou invalides.")
        else:
            report("error", "Une erreur interne est survenue pendant l'actualisation.")


def _start_refresh(fetch, state_file, key, subject=""):
    """Logique commune aux endpoints /refresh_data et /refresh_techaway."""
    state = _read_refresh_state(state_file)
    if state.get("status") == "running":
        return jsonify(state)

    if request.method == "POST":
        # get_json(silent=True) ne lève jamais, même si le Content-Type est faux
        data = request.get_json(silent=True) or {}
        if "email" in data and "password" in data:
            save_credentials(data["email"], data["password"])

    if not load_credentials():
        return jsonify({"status": "credentials_required"})

    audit_log(f"refresh_{key}_started")
    _write_refresh_state("running", "Démarrage du téléchargement...", state_file)
    threading.Thread(
        target=_run_refresh_in_background, args=(fetch, state_file, key, subject), daemon=True
    ).start()
    return jsonify({"status": "running", "message": "Téléchargement des données en cours..."})


@bp.route("/refresh_data", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def refresh_data():
    try:
        return _start_refresh(refresh_data_from_nexus, _REFRESH_STATE_FILE, "data")
    except Exception:
        current_app.logger.exception("refresh_data failed")
        return jsonify({"status": "error", "message": "Erreur interne du serveur."}), 500


@bp.route("/refresh_status", methods=["GET"])
def refresh_status():
    """Returns the current status of the background refresh operation."""
    return jsonify(_read_refresh_state())


@bp.route("/refresh_techaway", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def refresh_techaway():
    try:
        return _start_refresh(refresh_techaway_from_nexus, _REFRESH_STATE_FILE_TA, "techaway", "TechAway ")
    except Exception:
        current_app.logger.exception("refresh_techaway failed")
        return jsonify({"status": "error", "message": "Erreur interne du serveur."}), 500


@bp.route("/refresh_techaway_status", methods=["GET"])
def refresh_techaway_status():
    return jsonify(_read_refresh_state(_REFRESH_STATE_FILE_TA))


def _upload_dir() -> Path:
    p = Path(current_app.config.get("UPLOAD_FOLDER", "data/uploads"))
    p.mkdir(parents=True, exist_ok=True)
    Path("data/processed").mkdir(parents=True, exist_ok=True)
    return p

def _processed_path() -> Path:
    return Path("data/processed/merged_processed.csv")


# Cache du CSV traité, invalidé dès que le fichier change sur le disque.
# light_preprocess() copie le DataFrame avant de le modifier, l'objet mis en
# cache n'est donc jamais muté par les vues.
_processed_cache = {"signature": None, "df": None}


def _load_processed_df() -> pd.DataFrame:
    path = _processed_path()
    if not path.exists():
        raise FileNotFoundError(
            "Aucun fichier traité. Uploade un ou deux CSV puis traite-les depuis la page d'accueil."
        )

    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    if _processed_cache["signature"] != signature:
        _processed_cache["df"] = pd.read_csv(path)
        _processed_cache["signature"] = signature
        log.info("merged_processed.csv rechargé (%d lignes)", len(_processed_cache["df"]))
    return _processed_cache["df"]


def _load_df():
    """DataFrame prêt à l'emploi pour les vues (lecture + filtre de langue)."""
    return light_preprocess(_load_processed_df())


def _column(df: pd.DataFrame, candidates, expected: str) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    raise KeyError(
        f"Colonne '{expected}' introuvable. Colonnes disponibles : {df.columns.tolist()}"
    )


def _animator_column(df: pd.DataFrame) -> str:
    return _column(
        df,
        ["meeting_animator", "Meeting Animator", "meeting_animator_email", "Meeting Animator Email"],
        "Meeting Animator",
    )


def _masterclass_column(df: pd.DataFrame) -> str:
    return _column(df, ["Masterclass", "MC", "masterclass", "mc"], "Masterclass")


def _table_payload(d) -> dict:
    """Sérialise un DataFrame pour la macro data_table du template."""
    if d is None or getattr(d, "empty", True):
        return {"columns": [], "rows": []}
    return {"columns": list(d.columns), "rows": d.to_dict(orient="records")}


def _comment_filters():
    """Filtres communs aux pages qui listent des avis : période et notes négatives."""
    days = max(1, request.args.get("days", default=30, type=int) or 30)
    negative_only = bool(request.args.get("negative_only", type=int))
    end_date = datetime.now()
    return days, negative_only, end_date - timedelta(days=days), end_date



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
        write_last_update()

        audit_log("files_uploaded")
        flash("Fichiers uploadés et traités avec succès.")
        return redirect(url_for("main.dashboard"))

    return render_template("upload.html", last_upload_date=format_paris(read_last_update()))


@bp.route("/dashboard")
def dashboard():
    try:
        df = _load_df()

        feedback = get_masterclasses_with_negative_feedback(df)
        if isinstance(feedback, str):
            # La fonction renvoie un message explicatif quand il n'y a rien à montrer
            negative_feedback, negative_message = [], feedback
        else:
            negative_feedback = feedback.to_dict(orient="records")
            negative_message = None if negative_feedback else "Aucun avis négatif à afficher."

        return render_template(
            "dashboard.html",
            wof=_table_payload(wall_of_fame(df)),
            wos=_table_payload(wall_of_not_fame(df)),
            negative_feedback=negative_feedback,
            negative_message=negative_message,
        )
    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/animateurs")
def animateurs():
    """
    Liste de tous les animateurs, groupés par rôle, avec recherche par nom.
    """
    try:
        df = _load_df()

        q = (request.args.get("q") or "").strip().lower()

        col = _animator_column(df)  # colonne contenant le nom d'animateur
        role_col = "Animator Role" if "Animator Role" in df.columns else ("Role" if "Role" in df.columns else None)

        df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
        active_animators_30j = set(df_30j[col].dropna().unique()) if col in df_30j.columns else set()

        if role_col:
            pairs = df[[col, role_col]].dropna().drop_duplicates()
            grouped = {}
            seen = set()
            for name, role in zip(pairs[col].astype(str).str.strip(), pairs[role_col].astype(str).str.strip()):
                if not name or not role or (role, name) in seen:
                    continue
                if q and q not in name.lower():
                    continue
                seen.add((role, name))
                grouped.setdefault(role, []).append({"name": name, "active": name in active_animators_30j})

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
    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")




@bp.route("/animateur/<path:animateur>")
def animateur_detail(animateur: str):
    """
    Page dédiée à un animateur (email unique).
    """
    try:
        df = _load_df()
        days, negative_only, start_date, end_date = _comment_filters()

        best = get_best_mc_animateur(df, animateur)
        worst = get_worst_mc_animateur(df, animateur)
        moyenne_30j = get_moyenne_animateur_30j(df, animateur)
        classement = get_position_classement_animateur(df, animateur)
        classement_30j = get_position_classement_animateur_30j(df, animateur)

        df_animateur = df[df[_animator_column(df)] == animateur]
        comments_df = get_comments(
            df_animateur,
            seuil={1, 2, 3} if negative_only else {1, 2, 3, 4, 5},
            longueur_min_avis=3,
            start_date=start_date,
            end_date=end_date,
        )

        return render_template(
            "animateur.html",
            animateur=animateur,
            sessions_total=get_nb_sessions_animateur(df, animateur),
            moyenne_generale=get_moyenne_animateur(df, animateur),
            classement=classement or "Non classé",
            best_label=best["Masterclass"] if best is not None else "Aucune masterclass trouvée",
            worst_label=worst["Masterclass"] if worst is not None else "Aucune masterclass trouvée",
            sessions_30j=get_nb_sessions_animateur_30j(df, animateur),
            moyenne_30j=round(moyenne_30j, 2) if moyenne_30j and moyenne_30j > 0 else None,
            classement_30j=classement_30j or "Non classé",
            sessions_30j_table=_table_payload(get_sessions_30j_animateur(df, animateur)),
            mc_table=_table_payload(get_masterclasses_by_animateur(df, animateur)),
            days=days,
            negative_only=negative_only,
            comments=comments_df.to_dict("records"),
        )

    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/masterclasses")
def masterclasses():
    """
    Liste de toutes les masterclass.
    """
    try:
        df = _load_df()

        # Une seule agrégation pour toutes les masterclass plutôt qu'un balayage par MC
        col = _masterclass_column(df)
        moyennes = df.groupby(col)["Content Grade"].mean().round(2)

        mapping_values = set(meeting_mapping.values())
        mapped_masterclasses = []
        other_masterclasses = []

        for mc in sorted(moyennes.index):
            avg = moyennes[mc]
            item = {"name": mc, "avg": 0 if pd.isna(avg) else avg}
            target = mapped_masterclasses if mc in mapping_values else other_masterclasses
            target.append(item)

        # Regrouper les masterclasses officielles par pôle
        masterclasses_by_pole = {}
        for item in mapped_masterclasses:
            for p in pole_mapping.get(item["name"]) or ["Non assigné"]:
                masterclasses_by_pole.setdefault(p, []).append(item)

        # Tri des listes par moyenne décroissante et calcul de la moyenne du pôle
        pole_stats = {}
        for p, items in masterclasses_by_pole.items():
            items.sort(key=lambda x: x["avg"], reverse=True)
            valid_avgs = [i["avg"] for i in items if i["avg"] > 0]
            pole_stats[p] = round(sum(valid_avgs) / len(valid_avgs), 2) if valid_avgs else 0

        # Couleurs dynamiques HSL pour les pôles : rouge pour le plus faible, vert pour le meilleur
        neutral = "background-color: #6c757d; color: white;"
        valid_stats = [v for v in pole_stats.values() if v > 0]
        min_s, max_s = (min(valid_stats), max(valid_stats)) if valid_stats else (0, 0)
        pole_colors = {}
        for p, val in pole_stats.items():
            if val <= 0:
                pole_colors[p] = neutral
            else:
                pct = 0.5 if max_s == min_s else (val - min_s) / (max_s - min_s)
                pole_colors[p] = f"background-color: hsl({int(pct * 120)}, 70%, 45%); color: white;"

        # Tri des pôles par ordre alphabétique, avec 'Non assigné' à la fin
        sorted_poles = sorted(p for p in masterclasses_by_pole if p != "Non assigné")
        if "Non assigné" in masterclasses_by_pole:
            sorted_poles.append("Non assigné")

        other_masterclasses.sort(key=lambda x: x["avg"], reverse=True)

        return render_template(
            "masterclasses.html",
            masterclasses_by_pole={p: masterclasses_by_pole[p] for p in sorted_poles},
            other_masterclasses=other_masterclasses,
            pole_stats=pole_stats,
            pole_colors=pole_colors,
        )
    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/masterclass/<path:masterclass>")
def masterclass_detail(masterclass: str):
    """
    Page dédiée à une masterclass (nom unique).
    """
    try:
        df = _load_df()
        days, negative_only, start_date, end_date = _comment_filters()

        df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
        moyenne_30j = get_moyenne_masterclass(df_30j, masterclass)
        classement = get_position_classement_masterclass(df, masterclass)
        classement_30j = get_position_classement_masterclass_30j(df, masterclass)

        comments_df = get_comments(
            df[df["Masterclass"] == masterclass],
            seuil={1, 2, 3} if negative_only else {1, 2, 3, 4, 5},
            longueur_min_avis=3,
            start_date=start_date,
            end_date=end_date,
        )

        return render_template(
            "masterclass.html",
            masterclass=masterclass,
            sessions_total=get_nb_sessions_masterclass(df, masterclass),
            moyenne_generale=get_moyenne_masterclass(df, masterclass),
            classement=classement or "Non classé",
            sessions_30j=get_nb_sessions_masterclass(df_30j, masterclass),
            moyenne_30j=round(moyenne_30j, 2) if moyenne_30j else None,
            classement_30j=classement_30j or "Non classé",
            sessions_30j_table=_table_payload(get_mc_best_animateurs(df_30j, masterclass)),
            mc_table=_table_payload(get_mc_best_animateurs(df, masterclass)),
            days=days,
            negative_only=negative_only,
            comments=comments_df.to_dict("records"),
        )

    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/leaderboard")
def leaderboard():
    try:
        df = _load_df()

        roles = (
            sorted(df["Animator Role"].dropna().astype(str).unique().tolist())
            if "Animator Role" in df.columns else []
        )

        # Seuil minimum de sessions pour apparaître au classement
        min_sessions = request.args.get("min_sessions", default=20, type=int)
        if not min_sessions or min_sessions < 1:
            min_sessions = 20

        # Filtre multi-rôles : /leaderboard?role=Expert&role=PM
        selected_roles = [r for r in request.args.getlist("role") if r in roles]
        if selected_roles:
            # filtrer le DF principal, tout le reste (30j, etc.) en hérite
            df = df[df["Animator Role"].astype(str).isin(selected_roles)]

        # Les tableaux sont construits à la demande : seuls les deux affichés sont calculés
        options = {
            "anim": {
                "label": "Animateurs (tout)", "type": "animateur",
                "build": lambda: get_leaderboard_animateurs(
                    get_animateurs_plus_de_20_sessions(df, min_sessions), min_sessions),
            },
            "anim30": {
                "label": "Animateurs (30 jours)", "type": "animateur",
                "build": lambda: get_leaderboard_animateurs_30j(df),
            },
            "mc": {
                "label": "Masterclasses (tout)", "type": "masterclass",
                "build": lambda: get_leaderboard_masterclasses(
                    get_masterclass_plus_de_10_sessions(df, min_sessions), min_sessions),
            },
            "mc30": {
                "label": "Masterclasses (30 jours)", "type": "masterclass",
                "build": lambda: get_leaderboard_masterclasses_30j(df),
            },
        }

        left_key = (request.args.get("left") or "anim").lower()
        right_key = (request.args.get("right") or "anim30").lower()
        if left_key not in options:
            left_key = "anim"
        if right_key not in options:
            right_key = "anim30"

        cache = {}

        def prep(key):
            opt = options[key]
            if key not in cache:
                cache[key] = opt["build"]()
            table = cache[key]
            if table is None or getattr(table, "empty", True):
                return {"columns": [], "rows": [], "label_col": None, "type": opt["type"]}
            cols = list(table.columns)  # rang | label | moyenne
            return {
                "columns": cols,
                "rows": table.to_dict(orient="records"),
                "label_col": cols[1] if len(cols) >= 2 else None,
                "type": opt["type"],
            }

        return render_template(
            "leaderboard.html",
            left=prep(left_key),
            right=prep(right_key),
            left_label=options[left_key]["label"],
            right_label=options[right_key]["label"],
            left_key=left_key,
            right_key=right_key,
            all_options=options,
            min_sessions=min_sessions,
            roles=roles,
            selected_roles=selected_roles,
        )
    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")




@bp.route("/slack_bot", methods=["GET", "POST"])
def slack_bot():
    """
    Page dédiée au Slack Bot.
    """
    senders = {
        "wof": (send_top_animators_message, "Message Wall of Fame envoyé sur Slack."),
        "wos": (send_not_top_animators_message, "Message Wall of Not Fame envoyé sur Slack."),
        "bad": (send_bad_sessions, "Récap commentaires négatifs envoyé sur Slack."),
    }
    try:
        message = None

        if request.method == "POST":
            action = request.form.get("action")
            if action in senders:
                send, message = senders[action]
                send(_load_df())
                audit_log("slack_message_sent", action)
            else:
                message = "Action inconnue."

        return render_template("slack_bot.html", message=message)
    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


TECHAWAY_HERO = "TECHFORALL"
TECHAWAY_VERTICALS = ["DATAANALYSIS", "PROGRAMING", "NOCODE", "CYBERSECURITY", "AI"]
TECHAWAY_TPS = [1, 2, 3, 4]


def _score(val):
    """Note exploitable (float) ou None si absente/NaN."""
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _tile_style_and_label(val):
    """Convertit une note (1..5) en couleur HSL + libellé 'x.xx/5'."""
    v = _score(val)
    if v is None:
        return "background: #9ca3af;", "-"
    v = max(1.0, min(5.0, v))
    hue = int(round((v / 5.0) * 120))  # 1 -> rouge, 5 -> vert
    return f"background: hsl({hue}, 70%, 45%);", f"{v:.2f}/5"


def _fill_style(val):
    """Remplissage vertical proportionnel à la note (2.5 -> 50%, 4.0 -> 80%)."""
    v = _score(val)
    pct = 0.0 if v is None else max(0.0, min(5.0, v)) / 5.0 * 100.0
    return (
        f"background: linear-gradient(to top, "
        f"#16a34a {pct:.0f}%, #f3f4f6 {pct:.0f}%); "
        f"border: 1px solid #e5e7eb;"
    )


def _tp_meta(val):
    v = _score(val)
    pct = 0 if v is None else int(round(max(0.0, min(5.0, v)) / 5.0 * 100))
    return {"pct": pct, "label": "-" if v is None else f"{v:.2f}/5"}


@bp.route("/techaway")
def techaway():
    try:
        df = _load_df()

        # Trois agrégations suffisent pour toute la page (verticale, niveau, TP)
        techaway_df = df[df["Verticale"] != "Not TechAway"]
        vertical_means = techaway_df.groupby("Verticale")["Content Grade"].mean().round(2)
        level_means = (
            techaway_df
            .groupby(["Verticale", techaway_df["Masterclass"].map(get_niveau)])["Content Grade"]
            .mean().round(2)
        )
        mc_means = df.groupby("Masterclass")["Content Grade"].mean().round(2)

        hero_style, hero_score_label = _tile_style_and_label(vertical_means.get(TECHAWAY_HERO))
        hero_level_score = level_means.get((TECHAWAY_HERO, "0"))

        # Un seul bloc "Niveau 0" cliquable pour TECHFORALL
        hero_levels = [{
            "name": "Niveau 0",
            "code": "N0",
            "style": _fill_style(hero_level_score),
            "score_label": _tile_style_and_label(hero_level_score)[1],
        }]

        tiles = []
        for vertical in TECHAWAY_VERTICALS:
            tile_style, tile_label = _tile_style_and_label(vertical_means.get(vertical))
            levels = []
            for code in ("N1", "N2", "N3"):
                score = level_means.get((vertical, code[1]))
                levels.append({
                    "name": f"Niveau {code[1]}",
                    "code": code,
                    "style": _fill_style(score),
                    "score_label": _tile_style_and_label(score)[1],
                })
            tiles.append({"name": vertical, "style": tile_style, "score_label": tile_label, "levels": levels})

        # Métadonnées par TP : hero N0 puis les autres verticales N1..N3
        tp_meta = {
            f"{TECHAWAY_HERO}_N0_TP{tp}": _tp_meta(mc_means.get(f"{TECHAWAY_HERO}-N0 - TP {tp}"))
            for tp in TECHAWAY_TPS
        }
        for vertical in TECHAWAY_VERTICALS:
            for code in ("N1", "N2", "N3"):
                for tp in TECHAWAY_TPS:
                    tp_meta[f"{vertical}_{code}_TP{tp}"] = _tp_meta(
                        mc_means.get(f"{vertical}-{code} - TP {tp}")
                    )

        return render_template(
            "techaway.html",
            hero_name=TECHAWAY_HERO,
            hero_style=hero_style,
            hero_score_label=hero_score_label,
            hero_levels=hero_levels,   # contient un seul niveau N0
            tiles=tiles,
            tp_meta=tp_meta,
        )

    except FileNotFoundError:
        return render_template("no_data.html")
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
        df = _load_df()

        # Widgets (GET params)
        selected_grades = request.args.getlist("grades", type=int)  # ex: ?grades=1&grades=3
        min_words = request.args.get("min_words", default=3, type=int)
        days = max(1, request.args.get("days", default=30, type=int) or 30)

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        comments_df = get_comments(
            df,
            # Ellipsis = valeur par défaut de get_comments, soit les notes 1 à 3
            seuil=selected_grades if selected_grades else ...,
            longueur_min_avis=min_words,
            start_date=start_date,
            end_date=end_date,
        )

        return render_template(
            "commentaires.html",
            comments=comments_df.to_dict("records"),
            selected_grades=set(selected_grades),
            min_words=min_words,
            days=days,
        )

    except FileNotFoundError:
        return render_template("no_data.html")
    except Exception:
        return _internal_error(f"Unhandled error on {request.path}")


@bp.route("/set_language/<lang>")
def set_language(lang):
    if lang in ['FR', 'EN', 'ALL']:
        session['lang_filter'] = lang
    return redirect(request.referrer or url_for('main.dashboard'))


def register_routes(app):
    app.register_blueprint(bp)
