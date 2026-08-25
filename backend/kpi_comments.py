# Comments on Masterclass and Animators KPIs


from backend.utils import *
# from backend.kpi_masterclass import *
# from backend.kpi_animators import *



def get_nb_sessions_masterclass_30j_in_kpi_comments(df, masterclass):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    df_30j = df_30j[df_30j["Masterclass"] == masterclass]
    if df_30j.empty:
        return 0
    return df_30j["Meeting ID"].nunique()


# Récupérer les masterclasses avec des avis négatifs récents
def get_masterclasses_with_negative_feedback(df, seuil=3, min_negatifs=1, longueur_min_avis=3):
    # Filtrer les 7 derniers jours
    df_1s = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=7))

    # if df_1s is empty, return message
    if df_1s.empty:
        return "Aucun avis dans la dernière semaine."

    # Filtrer uniquement les notes <= seuil
    df_neg = df_1s[df_1s['Content Grade'] <= seuil].copy()

    # Garder seulement les commentaires avec un minimum de mots
    df_neg = df_neg[df_neg['Comment'].apply(
        lambda x: isinstance(x, str) and len(x.strip().split()) > longueur_min_avis
    )]

    # Regrouper par masterclass
    grouped = df_neg.groupby('Masterclass')
    records = []

    for mc, group in grouped:
        nb_negatifs = len(group)
        if nb_negatifs >= min_negatifs:
            avis_list = (
                group[['Meeting ID', 'Meeting Start Date', 'Meeting Animator',
                       'Comment', 'User Fullname', 'User ID', 'Content Grade']]
                .sort_values(by='Meeting Start Date', ascending=False)
                .to_dict('records')
            )

            nb_sessions = get_nb_sessions_masterclass_30j_in_kpi_comments(df, mc)

            records.append({
                'Masterclass': mc,
                'Nombre Avis Négatifs': nb_negatifs,
                'Nombre Sessions 30j': nb_sessions,
                'Avis Négatifs': avis_list
            })

    if not records:
        return "Aucun avis négatif notable dernièrement."

    df_final = pd.DataFrame(records).sort_values(by='Nombre Avis Négatifs', ascending=False).reset_index(drop=True)
    return df_final


# Récupère les sessions de moins de 3.5 de moyenne
def get_bad_rated_masterclasses(
    df,
    seuil_moyenne=4,
    longueur_min_avis=3,
    days=7
):
    df_1s = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=days))
    if df_1s.empty:
        return pd.DataFrame(columns=[
            'Meeting ID', 'Masterclass', 'Meeting Animator', 'Meeting Start Date',
            'Moyenne Content Grade', 'Nombre Avis'
        ])

    # Nettoyage / garde des commentaires suffisamment longs
    df_1s = df_1s.copy()
    df_1s = df_1s[df_1s['Comment'].apply(
        lambda x: isinstance(x, str) and len(x.strip().split()) >= longueur_min_avis
    )]

    df_1s['Content Grade'] = pd.to_numeric(df_1s['Content Grade'], errors='coerce')
    df_1s = df_1s.dropna(subset=['Content Grade'])

    if df_1s.empty:
        return pd.DataFrame(columns=[
            'Meeting ID', 'Masterclass', 'Meeting Animator', 'Meeting Start Date',
            'Moyenne Content Grade', 'Nombre Avis'
        ])

    agg = (
        df_1s.groupby('Meeting ID')
        .agg(
            Moyenne_Content_Grade=('Content Grade', 'mean'),
            Nombre_Avis=('Content Grade', 'size'),
            Masterclass=('Masterclass', 'first'),
            Meeting_Animator=('Meeting Animator', 'first'),
            Meeting_Start_Date=('Meeting Start Date', 'min')
        )
        .reset_index()
    )

    df_final = agg[agg['Moyenne_Content_Grade'] < seuil_moyenne].copy()

    df_final = df_final.sort_values(
        by=['Moyenne_Content_Grade', 'Nombre_Avis'],
        ascending=[True, False]
    ).rename(columns={
        'Moyenne_Content_Grade': 'Moyenne Content Grade',
        'Nombre_Avis': 'Nombre Avis',
        'Meeting_Animator': 'Meeting Animator',
        'Meeting_Start_Date': 'Meeting Start Date'
    }).reset_index(drop=True)

    return df_final


def get_negative_comments_for_session(
    df,
    meeting_id,
    days=7,
    note_seuil=3,
    longueur_min_avis=3,
):
    """
    Retourne une liste de commentaires négatifs pour une session donnée (Meeting ID).
    - Période: derniers `days` jours
    - Note ≤ note_seuil (par défaut 3)
    - Commentaire avec au moins `longueur_min_avis` mots
    """
    # Filtre période
    df_periode = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=days))
    if df_periode.empty:
        return []

    # Normalise + filtre session
    dfx = df_periode.copy()
    dfx['Content Grade'] = pd.to_numeric(dfx['Content Grade'], errors='coerce')
    dfx = dfx[dfx['Meeting ID'] == meeting_id]
    dfx = dfx.dropna(subset=['Content Grade'])

    # Filtre négatif + longueur commentaire
    dfx = dfx[
        (dfx['Content Grade'] <= note_seuil) &
        (dfx['Comment'].apply(lambda x: isinstance(x, str) and len(x.strip().split()) >= longueur_min_avis))
    ]

    if dfx.empty:
        return []

    # Trie par date décroissante (plus récent au plus ancien)
    dfx = dfx.sort_values(by='Meeting Start Date', ascending=False)

    # Retourne une liste de dicts utiles
    cols = ['Meeting ID', 'Meeting Start Date', 'Meeting Animator',
            'Comment', 'User Fullname', 'User ID', 'Content Grade']
    return dfx[cols].to_dict('records')


# Similaire mais retourne tous les commentaires négatifs sur une période donnée
def get_comments(
    df,
    seuil=...,
    longueur_min_avis=3,
    start_date=None,
    end_date=None
):
    # Valeurs par défaut calculées à l'appel : figées dans la signature, elles
    # resteraient bloquées sur la date de démarrage du processus.
    end_date = end_date or datetime.now()
    start_date = start_date or end_date - timedelta(days=30)

    df_period = filter_by_date_range(df, start_date=start_date, end_date=end_date)

    target_cols = [
        'User ID', 'User Fullname', 'Meeting Animator', 'Meeting ID',
        'Meeting Start Date', 'Content Grade', 'Comment', 'Masterclass'
    ]
    if df_period.empty:
        return pd.DataFrame(columns=target_cols)

    if seuil is ...:
        allowed_grades = {1, 2, 3}
    elif isinstance(seuil, int):
        if seuil < 1 or seuil > 5:
            raise ValueError("La note 'seuil' doit être un entier entre 1 et 5.")
        allowed_grades = {seuil}
    elif isinstance(seuil, (list, tuple, set)):
        allowed_grades = {int(x) for x in seuil if 1 <= int(x) <= 5}
        if not allowed_grades:
            raise ValueError("La collection 'seuil' doit contenir au moins une note entre 1 et 5.")
    else:
        raise TypeError("Le paramètre 'seuil' doit être Ellipsis (...), un entier, ou une collection d'entiers.")

    grades = pd.to_numeric(df_period['Content Grade'], errors='coerce')
    df_period = df_period.loc[grades.isin(allowed_grades)].copy()

    df_period = df_period[df_period['Comment'].apply(
        lambda x: isinstance(x, str) and len(x.strip().split()) > longueur_min_avis
    )]

    for col in target_cols:
        if col not in df_period.columns:
            df_period[col] = pd.NA

    out = df_period[target_cols].copy()

    if not pd.api.types.is_datetime64_any_dtype(out['Meeting Start Date']):
        out['Meeting Start Date'] = pd.to_datetime(out['Meeting Start Date'], errors='coerce')

    out = out.sort_values(by='Meeting Start Date', ascending=False).reset_index(drop=True)
    return out
