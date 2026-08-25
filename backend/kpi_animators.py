# KPI Animators Module


from backend.utils import *
# from backend.kpi_masterclass import *
# from backend.kpi_comments import *


# Un animateur remplit-il le critère du mur (au-dessus pour le top, en dessous sinon) ?
def _matches_wall(moyenne, seuil, top):
    return moyenne >= seuil if top else moyenne < seuil


def is_in_wall(df, animateur_email, start_date, end_date, seuil, min_sessions, top):
    df_window = filter_by_date_range(df, start_date=start_date, end_date=end_date)
    if get_nb_sessions_animateur(df_window, animateur_email) < min_sessions:
        return False
    return _matches_wall(get_moyenne_animateur(df_window, animateur_email), seuil, top)


# Nombre de fenêtres de 30 jours consécutives passées sur le même mur
def get_nb_mois_consecutifs(df, animateur_email, seuil, min_sessions, top):
    count = 1
    while is_in_wall(
        df, animateur_email,
        start_date=datetime.now() - timedelta(days=30 * (count + 1)),
        end_date=datetime.now() - timedelta(days=30 * count),
        seuil=seuil, min_sessions=min_sessions, top=top,
    ):
        count += 1
    return count


def _build_wall(df, seuil, min_sessions, top):
    """Tableau des animateurs du mois retenus par le critère du mur."""
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))

    # Une seule agrégation pour moyennes, sessions et nombre d'avis
    stats = df_30j.groupby('Meeting Animator').agg(
        Moyenne=('Animator Grade', 'mean'),
        Nb_Sessions=('Meeting ID', 'nunique'),
        Nb_Avis=('Animator Grade', 'size'),
    )

    wall = []
    for animateur, row in stats.iterrows():
        if get_role(animateur) in ['PM']:
            continue
        moyenne = round(row['Moyenne'], 2)
        if row['Nb_Sessions'] < min_sessions or not _matches_wall(moyenne, seuil, top):
            continue
        wall.append({
            "Animateur": animateur,
            "Moyenne": moyenne,
            "Nb Sessions": int(row['Nb_Sessions']),
            "Nb Avis": int(row['Nb_Avis']),
            "Streak": get_nb_mois_consecutifs(df, animateur, seuil, min_sessions, top),
        })

    if not wall:
        return None

    return pd.DataFrame(wall).sort_values(by='Moyenne', ascending=False).reset_index(drop=True)


# Wall of Fame
def wall_of_fame(df, seuil=4.8, min_sessions=2):
    return _build_wall(df, seuil, min_sessions, top=True)


# Wall of Not Fame
def wall_of_not_fame(df, seuil=4.0, min_sessions=2):
    return _build_wall(df, seuil, min_sessions, top=False)



def get_moyenne_animateur(df, animateur_email):
    df = df[df["Meeting Animator"] == animateur_email]
    if df.empty:
        return -1
    return df["Animator Grade"].mean().round(2)


def get_moyenne_animateur_30j(df, animateur_email):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    return get_moyenne_animateur(df_30j, animateur_email)


def get_nb_sessions_animateur(df, animateur_email):
    df = df[df["Meeting Animator"] == animateur_email]
    if df.empty:
        return 0
    return df["Meeting ID"].nunique()


def get_nb_sessions_animateur_30j(df, animateur_email):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    return get_nb_sessions_animateur(df_30j, animateur_email)


def get_animateurs_plus_de_20_sessions(df, min_sessions=20):
    counts = (
        df.dropna(subset=["Meeting Animator", "Meeting ID"])
          .groupby("Meeting Animator")["Meeting ID"]
          .nunique()
    )
    animateurs_20_plus = counts[counts >= min_sessions].index
    return df[df["Meeting Animator"].isin(animateurs_20_plus)]


def get_position_classement_animateur(df, animateur_email, min_sessions=20):
    df = get_animateurs_plus_de_20_sessions(df, min_sessions=min_sessions)
    moyennes = (
        df.groupby("Meeting Animator")["Animator Grade"]
          .mean()
          .round(2)
          .reset_index()
    )
    moyennes = moyennes.rename(columns={"Animator Grade": "Moyenne"})
    moyennes = moyennes.sort_values(by="Moyenne", ascending=False).reset_index(drop=True)
    try:
        rang = moyennes[moyennes["Meeting Animator"] == animateur_email].index[0] + 1
    except IndexError:
        return None
    return rang


def get_position_classement_animateur_30j(df, animateur_email):
    df = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    moyennes = (
        df.groupby("Meeting Animator")["Animator Grade"]
          .mean()
          .round(2)
          .reset_index()
    )
    moyennes = moyennes.rename(columns={"Animator Grade": "Moyenne"})
    moyennes = moyennes.sort_values(by="Moyenne", ascending=False).reset_index(drop=True)
    try:
        rang = moyennes[moyennes["Meeting Animator"] == animateur_email].index[0] + 1
    except IndexError:
        return None
    return rang



# Retourne le df des MC les plus appréciées pour un animateur
def get_masterclasses_by_animateur(df, animateur):
    df_animateur = df[df['Meeting Animator'] == animateur]
    grouped = df_animateur.groupby('Masterclass').agg(
        Moyenne=('Animator Grade', 'mean'),
        Nb_Sessions=('Meeting ID', 'nunique')
    ).reset_index().rename(columns={'Nb_Sessions': 'Nombre de sessions'})
    grouped['Moyenne'] = grouped['Moyenne'].round(2)
    return grouped.sort_values(by='Moyenne', ascending=False).reset_index(drop=True)


# Retourne la meilleure MC d'un animateur
def get_best_mc_animateur(df, animateur):
    df_mc = get_masterclasses_by_animateur(df, animateur)
    if df_mc.empty:
        return None
    return df_mc.iloc[0]


# Retourne la pire MC d'un animateur
def get_worst_mc_animateur(df, animateur):
    df_mc = get_masterclasses_by_animateur(df, animateur)
    if df_mc.empty:
        return None
    return df_mc.iloc[-1]


# Met en forme une note pour l'affichage (5.0 -> 5, 4.5 -> 4.5)
def _format_note(note):
    note = float(note)
    return int(note) if note.is_integer() else round(note, 1)


# Retourne les sessions d'un animateur sur les `days` derniers jours
def get_sessions_animateur(df, animateur, days=30):
    cols = ['Meeting Start Date', 'Masterclass', 'Note_Moyenne', 'Nb_Notes', 'Notes', 'Meeting ID']

    df_periode = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=days))
    df_periode = df_periode[df_periode["Meeting Animator"] == animateur]

    if df_periode.empty:
        return pd.DataFrame(columns=cols)

    df_sessions = (
        df_periode
        .groupby(['Masterclass', 'Meeting Start Date', 'Meeting ID'], as_index=False)
        .agg(
            Note_Moyenne=('Animator Grade', 'mean'),
            Nb_Notes=('Animator Grade', 'count'),
            # Toutes les notes de la session, y compris celles laissées sans commentaire
            Notes=('Animator Grade', lambda s: [
                _format_note(n) for n in sorted(s.dropna().tolist(), reverse=True)
            ]),
        )
    )
    df_sessions['Note_Moyenne'] = df_sessions['Note_Moyenne'].round(2)
    df_final = df_sessions.sort_values(by='Meeting Start Date', ascending=False).reset_index(drop=True)
    df_final['Meeting Start Date'] = df_final['Meeting Start Date'].apply(format_date)

    return df_final[cols]


# Retourne les avis d'un animteur sur les 30 derniers jours
def get_last_feedback(df, animateur):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    df_30j = df_30j[df_30j["Meeting Animator"] == animateur]

    feedback = df_30j[["User ID", "User Fullname", "Comment", "Meeting ID", "Animator Grade", "Meeting Animator", "Meeting Start Date", "Masterclass"]]

    if feedback.empty or feedback is None:
        return pd.DataFrame([])

    return feedback


# Leaderboard animateurs
def get_leaderboard_animateurs(df, min_sessions=20):
    df_rank = get_animateurs_plus_de_20_sessions(df, min_sessions=min_sessions)
    return build_leaderboard(df_rank, "Meeting Animator", "Animator Grade", "Animateur")


# Leaderboard animateurs 30j
def get_leaderboard_animateurs_30j(df):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    df_rank = get_animateurs_plus_de_20_sessions(df_30j, min_sessions=1)
    return build_leaderboard(df_rank, "Meeting Animator", "Animator Grade", "Animateur")
