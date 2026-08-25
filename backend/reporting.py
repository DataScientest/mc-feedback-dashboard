from config import SLACK_CHANNEL
from app.slack_handler import *
from backend.kpi_animators import *
from backend.kpi_masterclass import *
from backend.kpi_comments import *


def send_top_animators_message(df):
    top_df = wall_of_fame(df)

    if top_df is None or top_df.empty:
        post_message("Aucun animateur au-dessus de 4.8 ce mois-ci 👀", SLACK_CHANNEL)
        return

    header_message = "✦•······················•✦ :deal_with_parrot: *Animateurs ≥ 4.8* :deal_with_parrot: ✦•······················•✦"
    post_message(header_message, SLACK_CHANNEL, fallback_text="Top animateurs du mois")

    for _, row in top_df.iterrows():
        # Message principal (un par animateur)
        if row['Streak'] == 1:
            base_label = (
                f"✨ {format_animator_label(row['Animateur'])} "
                f"({row['Moyenne']}/5) - "
                f"{row['Nb Avis']} avis - "
                f"{row['Nb Sessions']} session{'s' if row['Nb Sessions'] > 1 else ''}"
            )
        else:
            base_label = (
                f"🏅 {format_animator_label(row['Animateur'])} "
                f"({row['Moyenne']}/5) - "
                f"{row['Nb Avis']} avis - "
                f"{row['Nb Sessions']} session{'s' if row['Nb Sessions'] > 1 else ''}"
                f" - {row['Streak']} mois consécutifs 🔥"
            )

        ts = post_message(base_label, SLACK_CHANNEL, fallback_text="Top animateur")

        # Détails dans le thread (sessions de l’animateur)
        sessions = df[df["Meeting Animator"] == row["Animateur"]]["Meeting ID"].unique()
        for meeting_id in sessions:
            info = get_meeting_info(filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30)), meeting_id)
            if info is None:
                continue

            comments_text = ""
            if info["Avis"]:
                comments_text = "\n".join([f"• _{c}_" for c in info["Avis"]])

            detail_text = (
                f"- *{info['Masterclass']}*\n"
                f"*Date* : {info['Date'].date()} "
                f"<{info['Link']}|(lien)>\n"
                f"*Moyenne* : {info['Moyenne']}/5\n"
                f"*Notes individuelles* : {info['Animator Grades']}\n"
            )
            if comments_text:
                detail_text += f"*Avis* :\n{comments_text}"

            post_thread_message(detail_text, SLACK_CHANNEL, thread_ts=ts)

    return


def send_not_top_animators_message(df):
    not_top_df = wall_of_not_fame(df)

    if not_top_df is None or not_top_df.empty:
        post_message("Aucun animateur sous 4.0 ce mois-ci :sunny:", SLACK_CHANNEL)
        return

    header_message = "✦•······················•✦ 🚨 *Animateurs < 4.0* 🚨 ✦•······················•✦"
    post_message(header_message, SLACK_CHANNEL, fallback_text="Animateurs du mois sous 4")

    for _, row in not_top_df.iterrows():
        # Message principal (un par animateur)
        if row['Streak'] == 1:
            base_label = (
                f":not_stonks: {format_animator_label(row['Animateur'])} "
                f"({row['Moyenne']}/5) - "
                f"{row['Nb Avis']} avis - "
                f"{row['Nb Sessions']} session{'s' if row['Nb Sessions'] > 1 else ''}"
            )
        else:
            base_label = (
                f"‼️ {format_animator_label(row['Animateur'])} "
                f"({row['Moyenne']}/5) - "
                f"{row['Nb Avis']} avis - "
                f"{row['Nb Sessions']} session{'s' if row['Nb Sessions'] > 1 else ''}"
                f" - {row['Streak']} mois consécutifs ‼️"
            )

        ts = post_message(base_label, SLACK_CHANNEL, fallback_text="Animateur sous 4.0")

        # Détails dans le thread (sessions de l’animateur)
        sessions = df[df["Meeting Animator"] == row["Animateur"]]["Meeting ID"].unique()
        for meeting_id in sessions:
            info = get_meeting_info(filter_by_date_range(df, start_date=datetime.now() - timedelta(days=30)), meeting_id)
            if info is None:
                continue

            comments_text = ""
            if info["Avis"]:
                comments_text = "\n".join([f"• _{c}_" for c in info["Avis"]])

            detail_text = (
                f"- *{info['Masterclass']}*\n"
                f"*Date* : {info['Date'].date()} "
                f"<{info['Link']}|(lien)>\n"
                f"*Moyenne* : {info['Moyenne']}/5\n"
                f"*Notes individuelles* : {info['Animator Grades']}\n"
            )
            if comments_text:
                detail_text += f"*Avis* :\n{comments_text}"

            post_thread_message(detail_text, SLACK_CHANNEL, thread_ts=ts)

    return

def send_bad_sessions(df):
    # DataFrame des sessions dont la moyenne Content Grade < 4 (nouvelle fonction)
    sessions_df = get_bad_rated_masterclasses(df)

    if sessions_df is None or sessions_df.empty:
        post_message("☀️ Aucun avis négatif notable dernièrement.", SLACK_CHANNEL)
        return

    header_message = "✦•······················•✦ ⚠️ *Sessions peu appréciées de la semaine* ⚠️ ✦•······················•✦"
    post_message(header_message, SLACK_CHANNEL, fallback_text="Sessions peu appréciées")

    for _, row in sessions_df.iterrows():
        mc_id = row['Meeting ID']
        mc_link = f"<https://nexus.datascientest.com/meeting/{mc_id}|Voir la session>"

        # Message principal par session
        session_text = (
            f":arrow_right: *{row['Masterclass']}*\n"
            f"*Moyenne* : {row['Moyenne Content Grade']:.2f}  |  *Avis* : {int(row['Nombre Avis'])}  |  "
            f"*Date* : {row['Meeting Start Date'].date()}  |  {mc_link}"
        )
        ts = post_message(session_text, SLACK_CHANNEL, fallback_text="Session peu appréciée")

        # Entête du thread
        thread_text = (
            f"*Animateur* : {format_animator_label(row['Meeting Animator'])}"
        )
        post_thread_message(thread_text, SLACK_CHANNEL, thread_ts=ts)

        # --- NOUVEAU : affichage des commentaires négatifs en thread ---
        neg_comments = get_negative_comments_for_session(
            df,
            meeting_id=mc_id,
            days=7,
            note_seuil=3,
            longueur_min_avis=3,
        )

        if not neg_comments:
            post_thread_message(
                "Aucun commentaire négatif saisi pour cette session sur la période.",
                SLACK_CHANNEL,
                thread_ts=ts
            )
            continue

        for avis in neg_comments:
            user_link = f"<https://nexus.datascientest.com/user/{avis['User ID']}|{avis['User Fullname']}>"
            detail_text = (
                f"*Commentaire* : _{avis['Comment']}_\n"
                f"*Auteur* : {user_link}\n"
                f"*Note* : {avis['Content Grade']}"
            )
            post_thread_message(detail_text, SLACK_CHANNEL, thread_ts=ts)

    return
