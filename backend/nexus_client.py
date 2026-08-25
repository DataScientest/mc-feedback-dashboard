"""
Client de l'API Nexus : authentification, téléchargement des exports de
sondages et stockage chiffré des identifiants.
"""

import json
import logging
import os

import requests

from backend.crypto_utils import encrypt, decrypt, InvalidToken

log = logging.getLogger(__name__)

URL = "https://api.hub.datascientest.com"
CREDENTIALS_FILE = "data/credentials.json"

# Sans délai maximal, une API qui ne répond plus bloque définitivement le thread
# d'actualisation : l'état resterait "running" et plus aucun refresh ne pourrait
# être lancé.
AUTH_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300

# Chaque sondage est exporté en deux fichiers, un par locale.
_SURVEY_FILES = {
    "post_meeting_masterclass": {
        "fr-FR": "data/uploads/post_meeting_masterclass.csv",
        "en-US": "data/uploads/post_meeting_masterclass_en.csv",
    },
    "techaway_post_tp": {
        "fr-FR": "data/uploads/techaway_post_tp.csv",
        "en-US": "data/uploads/techaway_post_tp_en.csv",
    },
}


def save_credentials(email, password):
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    payload = json.dumps({"email": email, "password": password}).encode("utf-8")
    with open(CREDENTIALS_FILE, "wb") as f:
        f.write(encrypt(payload))


def load_credentials():
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    with open(CREDENTIALS_FILE, "rb") as f:
        raw = f.read()

    try:
        return json.loads(decrypt(raw))
    except (InvalidToken, ValueError):
        pass

    # Legacy plaintext file: migrate it to encrypted storage on first read
    try:
        creds = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(creds, dict) and "email" in creds and "password" in creds:
        save_credentials(creds["email"], creds["password"])
        return creds
    return None


def get_auth(username, password):
    """Jeton d'accès Nexus, ou None si l'authentification échoue."""
    try:
        response = requests.post(
            URL + "/auth/login/nexus",
            json={"email": username, "password": password},
            timeout=AUTH_TIMEOUT,
        )
    except requests.RequestException:
        log.exception("Authentification Nexus impossible (API injoignable)")
        return None

    if response.status_code == 200:
        return response.json().get("return", {}).get("access_token")

    # Le corps de la réponse peut contenir des détails sensibles : on ne
    # journalise que le code HTTP.
    log.error("Authentification Nexus refusée (HTTP %s)", response.status_code)
    return None


def _download_survey(survey, locale, token):
    """
    Télécharge un export de sondage vers son fichier standardisé.
    Retourne le chemin écrit, ou None en cas d'échec.
    """
    filename = _SURVEY_FILES[survey][locale]
    endpoint = f"/surveys/{survey}/{locale}/answers/download?get_meeting_dt_start=true"
    log.info("Téléchargement de %s (%s)...", survey, locale)

    try:
        response = requests.get(
            URL + endpoint,
            headers={"Authorization": "Bearer " + token},
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
        )
    except requests.RequestException:
        log.exception("Téléchargement de %s (%s) impossible", survey, locale)
        return None

    if response.status_code != 200:
        log.error(
            "Téléchargement de %s (%s) refusé (HTTP %s)", survey, locale, response.status_code
        )
        return None

    os.makedirs(os.path.dirname(filename), exist_ok=True)
    # Fichier temporaire puis renommage : un téléchargement interrompu ne peut
    # pas laisser un CSV tronqué à la place du précédent, encore valide.
    tmp = filename + ".part"
    try:
        with open(tmp, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, filename)
    except OSError:
        log.exception("Écriture de %s impossible", filename)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    log.info("%s (%s) enregistré dans %s", survey, locale, filename)
    return filename


def download_survey_answers(locale, token):
    """Export du sondage post-masterclass pour une locale."""
    return _download_survey("post_meeting_masterclass", locale, token)


def download_techaway_answers(locale, token):
    """Export du sondage TechAway post-TP pour une locale."""
    return _download_survey("techaway_post_tp", locale, token)


def _refresh_survey(survey):
    """
    Réauthentifie puis télécharge les deux locales d'un sondage.

    Retourne True si au moins un fichier a été récupéré.
    Lève ValueError si les identifiants manquent ou sont invalides.
    """
    creds = load_credentials()
    if not creds:
        raise ValueError("Credentials missing")

    token = get_auth(creds["email"], creds["password"])
    if not token:
        raise ValueError("Invalid credentials or authentication failed")

    downloaded = [_download_survey(survey, locale, token) for locale in _SURVEY_FILES[survey]]
    return any(downloaded)


def refresh_data_from_nexus():
    """
    Returns True if successful, False otherwise.
    Raises Exception if credentials are missing or invalid.
    """
    return _refresh_survey("post_meeting_masterclass")


def refresh_techaway_from_nexus():
    """
    Returns True if successful, False otherwise.
    Raises Exception if credentials are missing or invalid.
    """
    return _refresh_survey("techaway_post_tp")
