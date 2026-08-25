"""
Flask routes - login, dashboard, and API endpoints for adding
users/cohorts and fetching aggregated data.
"""

import logging
import threading
import time

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

log = logging.getLogger(__name__)

from .nexus_client import fetch_user_lessons, fetch_cohort_users
from .data_processor import DataPool, extract_modules_from_lessons
from backend.nexus_client import load_credentials, get_auth
from backend.audit import audit_log

def _get_global_token():
    creds = load_credentials()
    if creds:
        return get_auth(creds["email"], creds["password"])
    return None

bp = Blueprint(
    "reeval",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/reeval/static",
)

# Application-level data pool (shared across requests for this process)
_data_pool = DataPool()

# Progress tracking for long-running fetches
_fetch_progress = {
    "running": False,
    "current": 0,
    "total": 0,
    "message": "",
    "done": False,
    "error": None,
}


# ── Pages ──────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    return redirect(url_for("reeval.dashboard"))

@bp.route("/dashboard")
def dashboard():
    return render_template("reeval/dashboard.html")


# ── API endpoints ──────────────────────────────────────────────────────

@bp.route("/api/add-source", methods=["POST"])
def add_source():
    """
    Add a user ID or cohort ID to the data pool.
    Expects JSON: { "type": "user"|"cohort", "id": <int> }
    Starts a background fetch and returns immediately.
    """
    global _fetch_progress

    if _fetch_progress["running"]:
        return jsonify({"success": False, "error": "A fetch is already in progress."}), 409

    data = request.get_json(silent=True) or {}
    source_type = data.get("type", "").strip().lower()
    source_id = data.get("id")

    if source_type not in ("user", "cohort"):
        return jsonify({"success": False, "error": "Invalid type. Use 'user' or 'cohort'."}), 400

    try:
        source_id = int(source_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "ID must be a number."}), 400

    token = _get_global_token()
    if not token:
        return jsonify({"success": False, "error": "No global auth token."}), 401

    # Reset progress
    _fetch_progress = {
        "running": True,
        "current": 0,
        "total": 0,
        "message": "Starting…",
        "done": False,
        "error": None,
    }

    audit_log("reeval_source_added", f"{source_type}:{source_id}")
    thread = threading.Thread(
        target=_background_fetch,
        args=(source_type, source_id, token),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True, "message": "Fetch started."})


def _background_fetch(source_type: str, source_id: int, token: str):
    """Run the multi-step fetch in a background thread."""
    global _fetch_progress

    try:
        if source_type == "user":
            # Step 1: fetch the user's lessons to find their cohort
            _fetch_progress["message"] = f"Fetching lessons for user {source_id}…"
            lessons = fetch_user_lessons(source_id, token)
            if not lessons:
                log.warning("No lessons returned for user %s", source_id)
                _fetch_progress.update(running=False, done=True, error=f"Could not fetch lessons for user {source_id}.")
                return

            # Normalise to list of sprints - handle wrapper format
            if isinstance(lessons, dict) and "sprints" in lessons:
                sprints = lessons["sprints"]
                log.info("User %s: unwrapped 'sprints' key → %d sprint(s)", source_id, len(sprints))
            elif isinstance(lessons, list):
                sprints = lessons
            else:
                sprints = [lessons]
            log.info("User %s has %d sprint(s)", source_id, len(sprints))

            # Find cohort IDs from sprint_cohort_id
            cohort_ids = set()
            for sprint in sprints:
                cid = sprint.get("sprint_cohort_id")
                if cid:
                    cohort_ids.add(cid)

            log.info("Found cohort IDs: %s", cohort_ids)

            if not cohort_ids:
                # No cohort found - just add this single user
                modules_data = extract_modules_from_lessons(lessons)
                log.info("No cohort found. Extracted %d modules for user %s", len(modules_data), source_id)
                _data_pool.add_user_data(source_id, modules_data)
                _fetch_progress.update(
                    running=False, done=True, current=1, total=1,
                    message=f"Added user {source_id} (no cohort found).",
                )
                return

            # Fetch users from all discovered cohorts
            all_user_ids = set()
            for cid in cohort_ids:
                _fetch_progress["message"] = f"Fetching cohort {cid} users…"
                users = fetch_cohort_users(cid, token)
                if users:
                    log.info("Cohort %s: got %d users", cid, len(users))
                    # Extract user IDs - handle both dict-with-id and plain int
                    uids = []
                    for u in users:
                        if isinstance(u, dict):
                            uid = u.get("id") or u.get("user_id")
                            if uid:
                                uids.append(uid)
                        elif isinstance(u, (int, float)):
                            uids.append(int(u))
                    log.info("  extracted %d user IDs", len(uids))
                    _data_pool.register_cohort(cid, uids)
                    all_user_ids.update(uids)
                else:
                    log.warning("Cohort %s: no users returned", cid)

            # Always include the original user
            all_user_ids.add(source_id)

            # Process the original user's data (already fetched)
            modules_data = extract_modules_from_lessons(lessons)
            log.info("Extracted %d modules from user %s's lessons", len(modules_data), source_id)
            _data_pool.add_user_data(source_id, modules_data)

            # Fetch remaining users
            remaining = all_user_ids - {source_id} - _data_pool.processed_users
            log.info("Remaining users to fetch: %d", len(remaining))
            _fetch_users(remaining, token)

        elif source_type == "cohort":
            # Fetch users in the cohort
            _fetch_progress["message"] = f"Fetching cohort {source_id} users…"
            users = fetch_cohort_users(source_id, token)
            if not users:
                log.warning("No users returned for cohort %s", source_id)
                _fetch_progress.update(running=False, done=True, error=f"Could not fetch cohort {source_id}.")
                return

            # Extract user IDs
            user_ids = []
            for u in users:
                if isinstance(u, dict):
                    uid = u.get("id") or u.get("user_id")
                    if uid:
                        user_ids.append(uid)
                elif isinstance(u, (int, float)):
                    user_ids.append(int(u))
            log.info("Cohort %s: %d user IDs extracted", source_id, len(user_ids))
            _data_pool.register_cohort(source_id, user_ids)

            remaining = set(user_ids) - _data_pool.processed_users
            _fetch_users(remaining, token)

    except Exception as e:
        log.exception("Error in background fetch")
        _fetch_progress.update(running=False, done=True, error=str(e))


def _fetch_users(user_ids: set, token: str):
    """Fetch lessons for a set of user IDs, updating progress."""
    global _fetch_progress

    user_list = sorted(user_ids)
    total = len(user_list)
    _fetch_progress["total"] = total
    _fetch_progress["current"] = 0

    for i, uid in enumerate(user_list, 1):
        _fetch_progress["current"] = i
        _fetch_progress["message"] = f"Fetching user {uid} ({i}/{total})…"

        try:
            lessons = fetch_user_lessons(uid, token)
            if lessons:
                modules_data = extract_modules_from_lessons(lessons)
                _data_pool.add_user_data(uid, modules_data)
        except Exception:
            # Un utilisateur en échec ne doit pas interrompre le lot entier.
            log.warning("Récupération des leçons impossible pour l'utilisateur %s", uid, exc_info=True)

        # Small delay to be gentle on the API
        time.sleep(0.2)

    _fetch_progress.update(
        running=False, done=True, current=total, total=total,
        message=f"Done - processed {total} users.",
    )


@bp.route("/api/progress")
def fetch_progress():
    """Return current fetch progress."""
    return jsonify(_fetch_progress)


@bp.route("/api/data")
def get_data():
    """Return aggregated comparison data."""
    return jsonify({
        "summary": _data_pool.get_summary(),
        "modules": _data_pool.get_aggregated_data(),
        "cohorts": _data_pool.get_cohorts_info(),
    })


@bp.route("/api/clear", methods=["POST"])
def clear_data():
    """Clear all pooled data."""
    audit_log("reeval_data_cleared")
    _data_pool.clear()
    return jsonify({"success": True})


@bp.route("/api/remove-module/<int:module_id>", methods=["POST"])
def remove_module(module_id):
    """Remove a module from the dashboard."""
    _data_pool.remove_module(module_id)
    return jsonify({"success": True})


@bp.route("/api/add-samples", methods=["POST"])
def add_samples():
    """Add a predefined set of sample cohorts."""
    global _fetch_progress

    if _fetch_progress["running"]:
        return jsonify({"success": False, "error": "A fetch is already in progress."}), 409

    token = _get_global_token()
    if not token:
        return jsonify({"success": False, "error": "No global auth token."}), 401

    _fetch_progress = {
        "running": True,
        "current": 0,
        "total": 0,
        "message": "Starting samples fetch…",
        "done": False,
        "error": None,
    }

    sample_cohorts = [6648, 6078, 6862, 5149, 5127, 6353, 6793, 6932, 7182, 5290, 5056, 6603, 6971, 5316]

    audit_log("reeval_samples_fetch_started")
    thread = threading.Thread(
        target=_background_fetch_samples,
        args=(sample_cohorts, token),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True, "message": "Sample fetch started. Go get a coffee now, it's a big one."})


def _background_fetch_samples(cohort_ids: list[int], token: str):
    """Run the multi-cohort fetch in a background thread."""
    global _fetch_progress

    try:
        all_user_ids = set()
        for idx, cid in enumerate(cohort_ids, 1):
            _fetch_progress["message"] = f"Fetching cohort {cid} users ({idx}/{len(cohort_ids)})…"
            users = fetch_cohort_users(cid, token)
            if users:
                log.info("Cohort %s: got %d users", cid, len(users))
                uids = []
                for u in users:
                    if isinstance(u, dict):
                        uid = u.get("id") or u.get("user_id")
                        if uid:
                            uids.append(uid)
                    elif isinstance(u, (int, float)):
                        uids.append(int(u))
                _data_pool.register_cohort(cid, uids)
                all_user_ids.update(uids)
            else:
                log.warning("Cohort %s: no users returned", cid)

        remaining = all_user_ids - _data_pool.processed_users
        log.info("Remaining sample users to fetch: %d", len(remaining))
        _fetch_users(remaining, token)

    except Exception as e:
        log.exception("Error in background sample fetch")
        _fetch_progress.update(running=False, done=True, error=str(e))
