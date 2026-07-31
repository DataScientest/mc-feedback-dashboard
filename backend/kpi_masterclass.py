# Masterclass related functions for the backend


from backend.utils import *
# from backend.kpi_animators import *
# from backend.kpi_comments import *



def get_moyenne_masterclass(df, masterclass):
    df = df[df["Masterclass"] == masterclass]
    if df.empty:
        return None
    return df["Content Grade"].mean().round(2)


def get_moyenne_masterclass_30j(df, masterclass):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    return get_moyenne_masterclass(df_30j, masterclass)


def get_nb_sessions_masterclass(df, masterclass):
    df = df[df["Masterclass"] == masterclass]
    if df.empty:
        return 0
    return df["Meeting ID"].nunique()


def get_nb_sessions_masterclass_30j(df, masterclass):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    return get_nb_sessions_masterclass(df_30j, masterclass)


def get_masterclass_plus_de_10_sessions(df, min_sessions=10):
    counts = (
        df.dropna(subset=["Masterclass", "Meeting ID"])
          .groupby("Masterclass")["Meeting ID"]
          .nunique()
    )
    masterclasses_20_plus = counts[counts >= min_sessions].index
    return df[df["Masterclass"].isin(masterclasses_20_plus)]


def get_position_classement_masterclass(df, masterclass, min_sessions=10):
    df = get_masterclass_plus_de_10_sessions(df, min_sessions=min_sessions)
    moyennes = (
        df.groupby("Masterclass")["Content Grade"]
          .mean()
          .round(2)
          .reset_index()
    )
    moyennes = moyennes.rename(columns={"Content Grade": "Moyenne"})
    moyennes = moyennes.sort_values(by="Moyenne", ascending=False).reset_index(drop=True)
    try:
        rang = moyennes[moyennes["Masterclass"] == masterclass].index[0] + 1
    except IndexError:
        return None
    return rang


def get_position_classement_masterclass_30j(df, masterclass):
    df = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    moyennes = (
        df.groupby("Masterclass")["Content Grade"]
          .mean()
          .round(2)
          .reset_index()
    )
    moyennes = moyennes.rename(columns={"Content Grade": "Moyenne"})
    moyennes = moyennes.sort_values(by="Moyenne", ascending=False).reset_index(drop=True)
    try:
        rang = moyennes[moyennes["Masterclass"] == masterclass].index[0] + 1
    except IndexError:
        return None
    return rang


# Retourne les meilleurs animateurs d'une MC
def get_mc_best_animateurs(df, masterclass):
    df = df[df["Masterclass"] == masterclass]
    if df.empty:
        return pd.DataFrame(columns=["Meeting Animator", "Moyenne", "Nombre de sessions"])
    return (
        df.groupby("Meeting Animator").agg(
            Moyenne=("Animator Grade", "mean"),
            Nombre_de_Sessions=("Meeting ID", "nunique"),
        )
        .round(2)
        .sort_values(by="Moyenne", ascending=False)
        .reset_index()
        .rename(columns={"Nombre_de_Sessions": "Nombre de sessions"})
    )


def get_mc_best_animateur(df, masterclass):
    df_mc = get_mc_best_animateurs(df, masterclass)
    if df_mc.empty:
        return None
    return df_mc.iloc[0]


def get_mc_worst_animateur(df, masterclass):
    df_mc = get_mc_best_animateurs(df, masterclass)
    if df_mc.empty:
        return None
    return df_mc.iloc[-1]


# Leaderboard masterclasses
def get_leaderboard_masterclasses(df, min_sessions=10):
    df_rank = get_masterclass_plus_de_10_sessions(df, min_sessions=min_sessions)
    return build_leaderboard(df_rank, "Masterclass", "Content Grade", "Masterclass")


def get_leaderboard_masterclasses_30j(df):
    df_30j = filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30))
    return build_leaderboard(df_30j, "Masterclass", "Content Grade", "Masterclass")
