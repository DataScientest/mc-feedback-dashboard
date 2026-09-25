"""
Journal des pages consultées : une ligne JSON par page vue, dans logs/visits.log.

Seules les pages HTML affichées avec succès (GET, 200) sont comptées : les
fichiers statiques, les appels fetch() en JSON, les redirections et les
erreurs sont ignorés. Aucune identité ni IP n'est enregistrée.
Le fichier est lu par `make visits` (cf. backend/visit_stats.py).
"""

import json
import logging
from datetime import datetime, timezone

from flask import request

VISITS_LOG = "logs/visits.log"

_visits_logger = logging.getLogger("visits")
log = logging.getLogger(__name__)


def record_visit(response):
    """Hook after_request : consigne la page vue puis renvoie la réponse telle quelle."""
    try:
        if (
            request.method == "GET"
            and response.status_code == 200
            and response.mimetype == "text/html"
            and request.endpoint
        ):
            _visits_logger.info(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "endpoint": request.endpoint,
                "path": request.path,
                # Paramètres d'URL décodés, ex. {"animateur": "jean@..."}
                "args": request.view_args or {},
            }, ensure_ascii=False))
    except Exception:
        # Le suivi des visites ne doit jamais casser l'affichage d'une page.
        log.exception("Enregistrement de la visite impossible")
    return response
