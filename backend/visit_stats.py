"""
Classement des pages les plus consultées, à partir de logs/visits.log.

Usage : python -m backend.visit_stats [--days N] [--top N]
(ou `make visits DAYS=30 TOP=20` sur le serveur)
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.visits import VISITS_LOG

PAGE_LABELS = {
    "main.dashboard": "Dashboard",
    "main.animateurs": "Animateurs (liste)",
    "main.animateur_detail": "Animateur (fiche)",
    "main.masterclasses": "Masterclasses (liste)",
    "main.masterclass_detail": "Masterclass (fiche)",
    "main.leaderboard": "Leaderboard",
    "main.techaway": "TechAway",
    "main.commentaires": "Commentaires",
    "main.slack_bot": "Slack bot",
    "main.upload_files": "Upload",
    "main.login": "Connexion",
    "reeval.dashboard": "Réévaluation temps d'exercice",
}

# Pages de détail : endpoint -> (titre de la section, paramètre d'URL ciblé)
DETAIL_PAGES = {
    "main.animateur_detail": ("Animateurs les plus consultés", "animateur"),
    "main.masterclass_detail": ("Masterclasses les plus consultées", "masterclass"),
}


def load_visits(path: Path, since: datetime | None):
    visits = []
    if not path.exists():
        return visits
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                visit = json.loads(line)
                ts = datetime.fromisoformat(visit["ts"])
            except (ValueError, KeyError, TypeError):
                continue  # ligne tronquée ou corrompue
            if since is None or ts >= since:
                visits.append(visit)
    return visits


def print_ranking(title: str, counter: Counter, top: int):
    print(f"\n{title}")
    print("-" * len(title))
    if not counter:
        print("  (aucune visite)")
        return
    total = sum(counter.values())
    rows = counter.most_common(top)
    width = len(str(rows[0][1]))
    for rank, (name, count) in enumerate(rows, 1):
        print(f"  {rank:>3}. {count:>{width}}  {count / total:6.1%}  {name}")
    if len(counter) > top:
        print(f"  ... et {len(counter) - top} autre(s)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--days", type=int, default=None,
                        help="ne compter que les N derniers jours (défaut : tout l'historique)")
    parser.add_argument("--top", type=int, default=15,
                        help="nombre de lignes par classement (défaut : 15)")
    parser.add_argument("--file", default=VISITS_LOG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    since = None
    if args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
    visits = load_visits(Path(args.file), since)

    period = f"sur les {args.days} derniers jours" if args.days else "depuis le début"
    print(f"{len(visits)} page(s) vue(s) {period}.")
    if not visits:
        return 0
    first = min(v["ts"] for v in visits)[:10]
    last = max(v["ts"] for v in visits)[:10]
    print(f"Période couverte : {first} -> {last} (UTC)")

    pages = Counter(PAGE_LABELS.get(v["endpoint"], v["endpoint"]) for v in visits)
    print_ranking("Pages les plus visitées", pages, args.top)

    for endpoint, (title, param) in DETAIL_PAGES.items():
        targets = Counter(
            v["args"].get(param, "?")
            for v in visits
            if v["endpoint"] == endpoint and isinstance(v.get("args"), dict)
        )
        print_ranking(title, targets, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
