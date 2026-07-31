# KPI Animators Module


from backend.utils import *
# from backend.kpi_masterclass import *
# from backend.kpi_comments import *


# Wall of Fame
def wall_of_fame(df, seuil=4.8, min_sessions=2):
    df = df.copy()
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    animateurs = df_30j['Meeting Animator'].unique()
    animateurs = [a for a in animateurs if get_role(a) not in ['PM']]
    wall = []
    for animateur in animateurs:
        moyenne = get_moyenne_animateur(df_30j, animateur)
        nb_sessions = get_nb_sessions_animateur(df_30j, animateur)
        
        if moyenne >= seuil and nb_sessions >= min_sessions:            
            df_animateur_30j = df_30j[df_30j['Meeting Animator'] == animateur]
            nb_avis = df_animateur_30j.shape[0]
            wall.append({
                "Animateur": animateur,
                "Moyenne": moyenne,
                "Nb Sessions": nb_sessions,
                "Nb Avis": nb_avis,
                "Streak": get_nb_mois_consecutifs_top(df, animateur)
            })
            
    if not wall:
        return None

    return pd.DataFrame(wall).sort_values(by='Moyenne', ascending=False).reset_index(drop=True)


def is_in_wall_of_fame(df, animateur_email, start_date, end_date, seuil=4.8, min_sessions=2):
    df_wof = filter_by_date_range(df, start_date=start_date, end_date=end_date)
    if get_moyenne_animateur(df_wof, animateur_email) >= seuil and get_nb_sessions_animateur(df_wof, animateur_email) >= min_sessions:
        return True
    return False


def get_nb_mois_consecutifs_top(df, animateur_email):
    df = df.copy()
    count = 1
    while is_in_wall_of_fame(df, animateur_email, 
                             start_date=datetime.now() - timedelta(days=30*(count+1)), 
                             end_date=datetime.now() - timedelta(days=30*count)):
        count += 1
    return count


# Wall of Not Fame
def wall_of_not_fame(df, seuil=4.0, min_sessions=2):
    df = df.copy()
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    animateurs = df_30j['Meeting Animator'].unique()
    animateurs = [a for a in animateurs if get_role(a) not in ['PM']]
    wall = []
    for animateur in animateurs:
        moyenne = get_moyenne_animateur(df_30j, animateur)
        nb_sessions = get_nb_sessions_animateur(df_30j, animateur)
        if moyenne < seuil and nb_sessions >= min_sessions:
            df_animateur_30j = df_30j[df_30j['Meeting Animator'] == animateur]
            nb_avis = df_animateur_30j.shape[0]
            wall.append({
                "Animateur": animateur,
                "Moyenne": moyenne,
                "Nb Sessions": nb_sessions,
                "Nb Avis": nb_avis,
                "Streak": get_nb_mois_consecutifs_top(df, animateur)
            })
            
    if not wall:
        return None

    return pd.DataFrame(wall).sort_values(by='Moyenne', ascending=False).reset_index(drop=True)


def is_in_wall_of_not_fame(df, animateur_email, start_date, end_date, seuil=4.8, min_sessions=2):
    df_wof = filter_by_date_range(df, start_date=start_date, end_date=end_date)
    if get_moyenne_animateur(df_wof, animateur_email) < seuil and get_nb_sessions_animateur(df_wof, animateur_email) >= min_sessions:
        return True
    return False


def get_nb_mois_consecutifs_bottom(df, animateur_email):
    df = df.copy()
    count = 1
    while is_in_wall_of_not_fame(df, animateur_email,
                             start_date=datetime.now() - timedelta(days=30*(count+1)),
                             end_date=datetime.now() - timedelta(days=30*count)):
        count += 1
    return count



def get_moyenne_animateur(df, animateur_email):
    df = df.copy()
    df = df[df["Meeting Animator"] == animateur_email]
    if df.empty:
        return -1
    return df["Animator Grade"].mean().round(2)


def get_moyenne_animateur_30j(df, animateur_email):
    df = df.copy()
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    return get_moyenne_animateur(df_30j, animateur_email)


def get_nb_sessions_animateur(df, animateur_email):
    df = df.copy()
    df = df[df["Meeting Animator"] == animateur_email]
    if df.empty:
        return 0
    return df["Meeting ID"].nunique()
    

def get_nb_sessions_animateur_30j(df, animateur_email):
    df = df.copy()
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    return get_nb_sessions_animateur(df_30j, animateur_email)


def get_animateurs_plus_de_20_sessions(df, min_sessions=20):
    df = df.copy()
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
    ).reset_index()
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


# Retourne les sessions d'un animateur dans les 30 derniers jours
def get_sessions_30j_animateur(df, animateur):
    cols = ['Meeting Start Date', 'Masterclass', 'Note_Moyenne', 'Nb_Notes', 'Notes', 'Meeting ID']

    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    df_30j = df_30j[df_30j["Meeting Animator"] == animateur]

    if df_30j.empty:
        return pd.DataFrame(columns=cols)

    df_sessions = (
        df_30j
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
    df = df.copy()
    df_rank = get_animateurs_plus_de_20_sessions(df, min_sessions=min_sessions)
    if df_rank.empty:
        return pd.DataFrame(columns=["Rang", "Animateur", "Moyenne"])

    moyennes = (
        df_rank.groupby("Meeting Animator")["Animator Grade"]
               .mean().round(2).reset_index()
               .rename(columns={
                   "Meeting Animator": "Animateur",
                   "Animator Grade": "Moyenne"
               })
    )

    moyennes["Rang"] = moyennes["Animateur"].apply(
        lambda a: get_position_classement_animateur(df, a, min_sessions=min_sessions)
    )

    leaderboard = moyennes.sort_values(by="Rang", ascending=True).reset_index(drop=True)
    return leaderboard[["Rang", "Animateur", "Moyenne"]]


# Leaderboard animateurs 30j
def get_leaderboard_animateurs_30j(df):
    df = df.copy()
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    df_rank = get_animateurs_plus_de_20_sessions(df_30j, min_sessions=1)
    if df_rank.empty:
        return pd.DataFrame(columns=["Rang", "Animateur", "Moyenne"])

    moyennes = (
        df_rank.groupby("Meeting Animator")["Animator Grade"]
               .mean().round(2).reset_index()
               .rename(columns={
                   "Meeting Animator": "Animateur",
                   "Animator Grade": "Moyenne"
               })
    )

    moyennes["Rang"] = moyennes["Animateur"].apply(
        lambda a: get_position_classement_animateur_30j(df, a)
    )

    leaderboard = moyennes.sort_values(by="Rang", ascending=True).reset_index(drop=True)
    return leaderboard[["Rang", "Animateur", "Moyenne"]]
