# Récap hebdomadaire des commentaires négatifs sur les masterclasses.
#
# Le job est déclenché tous les jours à 05:00 (Europe/Paris) mais ne fait rien
# tant que le récap du lundi a déjà été envoyé : une semaine ratée (Nexus
# indisponible, conteneur redémarré) est donc rattrapée le lendemain, sans
# jamais envoyer deux fois le même récap.

import logging
import os
import pandas as pd
from backend.utils import *
from backend.preprocess import preprocess_data, light_preprocess
from backend.nexus_client import refresh_data_from_nexus
from backend.reporting import send_bad_sessions
from backend.job_state import (
    mark_weekly_report_sent,
    single_run,
    weekly_report_due,
)

log = logging.getLogger(__name__)

WEEKLY_JOB_KEY = "weekly_report"


def job_generate_weekly_report(force=False):
    """
    Point d'entrée du scheduler. Retourne True si le récap a bien été envoyé.

    `force=True` ignore le garde-fou hebdomadaire (utile pour un envoi manuel).
    """
    with single_run(WEEKLY_JOB_KEY) as acquired:
        if not acquired:
            # Un autre worker fait déjà tourner le job : ne pas doubler l'envoi.
            return False

        if not force and not weekly_report_due():
            log.info("Récap hebdo déjà envoyé cette semaine : rien à faire.")
            return False

        return _run_weekly_report()


def _run_weekly_report():
    log.info("Job hebdo : téléchargement des nouvelles données Nexus...")
    # 1. Récupération des datasets avec les identifiants enregistrés
    try:
        if not refresh_data_from_nexus():
            log.error("Job hebdo interrompu : échec du téléchargement depuis Nexus.")
            return False
    except Exception:
        # Sans marquage de succès, le job réessaiera automatiquement demain 05:00.
        log.exception("Job hebdo interrompu : erreur pendant la récupération Nexus.")
        return False

    # 2. Traitement des données
    df_fr_raw = _read_csv_or_none("data/uploads/post_meeting_masterclass.csv")
    df_en_raw = _read_csv_or_none("data/uploads/post_meeting_masterclass_en.csv")

    if df_fr_raw is None and df_en_raw is None:
        log.error("Job hebdo interrompu : aucun CSV masterclass à traiter.")
        return False

    log.info("Job hebdo : traitement des données téléchargées...")
    try:
        df = preprocess_data(df_fr_raw, df_en_raw)
    except Exception:
        log.exception("Job hebdo interrompu : erreur pendant le prétraitement.")
        return False

    if df is None or df.empty:
        log.error("Job hebdo interrompu : aucune donnée exploitable après traitement.")
        return False

    os.makedirs("data/processed", exist_ok=True)
    # On écrase pour conserver le dernier historique complet
    df.to_csv("data/processed/merged_processed.csv", index=False)
    write_last_update()

    # 3. Envoi du récap des sessions mal notées
    log.info("Job hebdo : envoi du récap des commentaires négatifs sur Slack...")
    try:
        send_bad_sessions(light_preprocess(df))
    except Exception:
        log.exception("Job hebdo interrompu : échec de l'envoi Slack.")
        return False

    # Marqué seulement après un envoi réussi : c'est ce qui évite le doublon
    # et déclenche un rattrapage tant que l'envoi n'a pas abouti.
    mark_weekly_report_sent()
    log.info("Job hebdo terminé avec succès.")
    return True


def _read_csv_or_none(path):
    try:
        return pd.read_csv(path)
    except (OSError, ValueError, pd.errors.ParserError):
        return None


def main():
    job_generate_weekly_report(force=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
