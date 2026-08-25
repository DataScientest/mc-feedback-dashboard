import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from config import SLACK_TOKEN

log = logging.getLogger(__name__)


client = WebClient(token=SLACK_TOKEN)


def post_message(text, channel, fallback_text="Notification"):
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=fallback_text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
        return response["ts"]
    except SlackApiError as e:
        log.error("Échec de l'envoi du message Slack : %s", e.response["error"])
        raise

def post_thread_message(text, channel, thread_ts):
    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text
        )
    except SlackApiError as e:
        log.error("Échec de l'envoi du message dans le fil Slack : %s", e.response["error"])
        raise
