# Fonctions utilitaires pour le backend


import pandas as pd
import json
import logging
import re
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta


log = logging.getLogger(__name__)

MAPPING_DIR = Path(__file__).parent / "mapping"


with open(MAPPING_DIR / "meeting_mapping.json", "r", encoding="utf-8") as f:
    meeting_mapping = json.load(f)

with open(MAPPING_DIR / "role_mapping.json", "r", encoding="utf-8") as f:
    role_mapping = json.load(f)

with open(MAPPING_DIR / "pole_mapping.json", "r", encoding="utf-8") as f:
    pole_mapping = json.load(f)


# Caractères typographiques ramenés à leur équivalent ASCII avant comparaison
_QUOTE_TRANS = dict.fromkeys(map(ord, "‘’‚‛ʼ´`"), "'")
_DASH_TRANS = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")


# Normalise un intitulé pour une comparaison souple (accents, casse, ponctuation)
def normalize_mc_text(text):
    """
    In : "Réduction de Dimension & Séries Temporelles"
    Out: "reduction de dimension et series temporelles"

    Neutralise : accents, casse, apostrophes/tirets typographiques, tirets
    répétés, "&"/"and" vs "et", espaces multiples.
    Les espaces internes sont conservés : "Data Viz" et "DataViz" restent
    deux intitulés distincts.
    """
    if not isinstance(text, str):
        return ""

    text = text.translate(_QUOTE_TRANS).translate(_DASH_TRANS)
    # Décomposition Unicode puis suppression des diacritiques (é -> e, ç -> c)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"-{2,}", "-", text)
    text = text.replace("&", " et ")
    text = re.sub(r"\band\b", "et", text)
    text = re.sub(r"\s+", " ", text)
    return text


# Index de recherche construit une fois au chargement du mapping
def _build_meeting_index(mapping):
    """
    Retourne (index, conflits) où index est une liste [(clé normalisée, valeur)]
    triée par longueur décroissante : l'intitulé le plus précis gagne
    ("Cloud Computing avec AWS" plutôt que "Cloud Computing").
    À longueur égale, l'ordre du JSON départage.

    conflits recense les clés qui deviennent identiques après normalisation
    mais qui pointent vers des masterclass différentes.
    """
    index = {}
    conflicts = {}

    for key, value in mapping.items():
        nkey = normalize_mc_text(key)
        if not nkey:
            continue
        if nkey in index:
            if index[nkey] != value:
                conflicts.setdefault(nkey, {index[nkey]}).add(value)
            continue
        index[nkey] = value

    # sorted() est stable : à longueur égale l'ordre d'insertion est préservé
    ordered = sorted(index.items(), key=lambda kv: len(kv[0]), reverse=True)
    return ordered, conflicts


meeting_index, meeting_mapping_conflicts = _build_meeting_index(meeting_mapping)

for _nkey, _values in meeting_mapping_conflicts.items():
    log.warning(
        "meeting_mapping : '%s' correspond à plusieurs masterclass %s "
        "après normalisation, la première du JSON est retenue.",
        _nkey, sorted(_values),
    )


# Fonction pour nettoyer les noms de masterclass avec dictionnaire JSON
def get_mc_name(meeting_name):
    if not isinstance(meeting_name, str):
        return meeting_name

    # If "-OPT1" appears in the meeting name, remove it
    if "-OPT1" in meeting_name:
        meeting_name = meeting_name.replace("-OPT1", "")
    elif "-OPT2" in meeting_name:
        meeting_name = meeting_name.replace("-OPT2", "")

    options = ["AI", "CYBERSECURITY", "TECHFORALL", "DATAANALYSIS", "NOCODE", "PROGRAMING", "PROGRAMMING"]
    # Replace TECHAWAY-option-L by TECHAWAY-option-N
    for option in options:
        if f"TECHAWAY-{option}-L" in meeting_name:
            meeting_name = meeting_name.replace(f"TECHAWAY-{option}-L", f"TECHAWAY-{option}-N")

    normalized = normalize_mc_text(meeting_name)
    for nkey, value in meeting_index:
        if nkey in normalized:
            return value
    return meeting_name


# Ajouter la colonne Verticale pour les TechAway
def get_verticale_techaway(masterclass):
    options = ["AI", "CYBERSECURITY", "TECHFORALL", "DATAANALYSIS", "NOCODE", "PROGRAMING"]
    for option in options:
        if option in masterclass:
            return option
    
    if "PROGRAMMING" in masterclass:
        return "PROGRAMING"
    
    if "Examen blanc" in masterclass:
        return "Examen blanc"
    
    else:
        return "Not TechAway"


# Fonction pour définir le rôle d'un animateur avec dictionnaire JSON
def get_role(animateur_email):
    for key, value in role_mapping.items():
        if key.lower() in animateur_email.lower():
            return value
    return "Rôle inconnu"


# Formatte le mail de l'animateur
def format_animator_label(animator):
    if '@' in animator:
        parts = animator.split('@')[0].split('.')
        return ' '.join(part.capitalize() for part in parts)
    return animator.title()


# Formatte une date en string + lisible
def format_date(date_str: str) -> str:
    # In : TimeStamp '2023-10-05 14:30:00'
    # Out: '05/10/2023 14:30'
    try:
        dt = pd.to_datetime(date_str)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return date_str


# Filtrer les données par plage de dates
def filter_by_date_range(df, start_date=None, end_date=None):
    df = df.copy()
    if start_date is not None:
        df = df[df['Meeting Start Date'] >= pd.to_datetime(start_date)]
    if end_date is not None:
        df = df[df['Meeting Start Date'] <= pd.to_datetime(end_date)]
    return df


# Retourne la date du meeting, la Masterclass, son animateur, les avis, la moyenne
def get_meeting_info(df, meeting_id):
    # Filtrer par date
    df = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    # Filtrer le meeting
    meeting_df = df[df["Meeting ID"] == meeting_id].copy()
    if meeting_df.empty:
        return None
    
    # Informations générales
    meeting_date = meeting_df["Meeting Start Date"].iloc[0]
    masterclass = meeting_df["Masterclass"].iloc[0]
    animator = meeting_df["Meeting Animator"].iloc[0]
    animator_label = format_animator_label(animator)
    
    # Avis (commentaires > 3 mots)
    comments = [
        c for c in meeting_df["Comment"].dropna().tolist()
        if isinstance(c, str) and len(c.strip().split()) > 3
    ]
    
    # Notes
    avg_grade = meeting_df["Animator Grade"].mean().round(2)
    animator_grades = meeting_df["Animator Grade"].dropna().tolist()
    
    return {
        "Meeting ID": meeting_id,
        "Date": meeting_date,
        "Masterclass": masterclass,
        "Animateur": animator_label,
        "Avis": comments,
        "Moyenne": avg_grade,
        "Animator Grades": animator_grades,
        "Link": f"https://nexus.datascientest.com/meeting/{meeting_id}"
    }