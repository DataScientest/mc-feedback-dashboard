# Garde-fous partagés par les workers Gunicorn et le scheduler, pour qu'un
# même envoi Slack ne parte jamais deux fois et qu'un envoi hebdomadaire raté
# (app redémarrée, API Nexus indisponible) soit rattrapé au lieu d'être perdu.

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.utils import get_paris_tz

log = logging.getLogger(__name__)

STATE_DIR = Path("data")

# Instant du dernier récap hebdomadaire réellement envoyé (ISO 8601 UTC)
LAST_WEEKLY_REPORT_FILE = STATE_DIR / "last_weekly_report.txt"

# Créneau attendu du récap : lundi 05:00, heure de Paris
WEEKLY_DAY = 0  # lundi
WEEKLY_HOUR = 5

# Un verrou plus vieux que ça vient d'un process tué en plein envoi : on le lève.
STALE_LOCK_SECONDS = 30 * 60


def _lock_path(key):
    return STATE_DIR / f"{key}.lock"


def _clear_if_stale(path, stale_after):
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return
    if age > stale_after:
        log.warning("Verrou %s abandonné depuis %.0f s : il est levé.", path.name, age)
        try:
            path.unlink()
        except OSError:
            pass


@contextmanager
def single_run(key, stale_after=STALE_LOCK_SECONDS):
    """
    Verrou inter-processus basé sur un fichier créé de façon atomique.

    Utilisé comme `with single_run("slack_bad") as acquired:` : `acquired` vaut
    False quand un autre worker (ou le scheduler) exécute déjà le même envoi,
    et l'appelant doit alors s'abstenir.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path(key)
    _clear_if_stale(path, stale_after)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        log.warning("Tâche '%s' déjà en cours ailleurs : exécution ignorée.", key)
        yield False
        return

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {int(time.time())}")
        yield True
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def read_last_weekly_report():
    """Instant du dernier récap envoyé (aware, UTC), ou None."""
    try:
        raw = LAST_WEEKLY_REPORT_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("Horodatage illisible dans %s : %r", LAST_WEEKLY_REPORT_FILE, raw)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def mark_weekly_report_sent(moment=None):
    moment = moment or datetime.now(timezone.utc)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_WEEKLY_REPORT_FILE.write_text(
        moment.astimezone(timezone.utc).isoformat(), encoding="utf-8"
    )
    return moment


def last_scheduled_occurrence(now=None):
    """Dernier lundi 05:00 (heure de Paris) déjà passé."""
    paris = get_paris_tz()
    now = (now or datetime.now(timezone.utc)).astimezone(paris)
    occurrence = now.replace(hour=WEEKLY_HOUR, minute=0, second=0, microsecond=0)
    occurrence -= timedelta(days=(now.weekday() - WEEKLY_DAY) % 7)
    if occurrence > now:
        occurrence -= timedelta(days=7)
    return occurrence


def weekly_report_due(now=None):
    """
    True tant que le récap du dernier lundi 05:00 n'a pas été envoyé.

    C'est ce qui permet à la fois de ne pas doubler l'envoi (le scheduler passe
    tous les jours) et de rattraper une semaine manquée.
    """
    last = read_last_weekly_report()
    if last is None:
        return True
    return last < last_scheduled_occurrence(now)


def bootstrap_weekly_marker():
    """
    Premier démarrage après l'installation : on pose l'horodatage sans envoyer,
    pour ne pas déclencher un rattrapage inattendu dans le canal Slack.
    """
    if read_last_weekly_report() is not None:
        return False
    mark_weekly_report_sent()
    log.info("Premier démarrage : récap hebdo initialisé, aucun rattrapage envoyé.")
    return True
