# Config values

import os
from dotenv import load_dotenv

load_dotenv()


SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-12345")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
SLACK_TOKEN = os.getenv("SLACK_TOKEN")
