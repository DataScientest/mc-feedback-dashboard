import json
import os
import requests
import re
import pandas as pd
from datetime import datetime

URL = "https://api.hub.datascientest.com"
CREDENTIALS_FILE = "data/credentials.json"

from backend.crypto_utils import encrypt, decrypt, InvalidToken

def save_credentials(email, password):
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    payload = json.dumps({"email": email, "password": password}).encode("utf-8")
    with open(CREDENTIALS_FILE, 'wb') as f:
        f.write(encrypt(payload))

def load_credentials():
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    with open(CREDENTIALS_FILE, 'rb') as f:
        raw = f.read()

    try:
        return json.loads(decrypt(raw))
    except (InvalidToken, ValueError):
        pass

    # Legacy plaintext file: migrate it to encrypted storage on first read
    try:
        creds = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(creds, dict) and "email" in creds and "password" in creds:
        save_credentials(creds["email"], creds["password"])
        return creds
    return None

def get_auth(username, password):
    endpoint = "/auth/login/nexus"
    data = {
        "email": username,
        "password": password
    }
    response = requests.post(URL + endpoint, json=data)
    if response.status_code == 200:
        return response.json().get("return", {}).get("access_token")
    else:
        print(f"Auth Error: {response.text}")
        return None

def download_survey_answers(locale, token):
    endpoint = f"/surveys/post_meeting_masterclass/{locale}/answers/download?get_meeting_dt_start=true"
    header = {
        "Authorization": "Bearer " + token
    }
    print(f"Downloading survey answers for {locale}...")
    response = requests.get(URL + endpoint, headers=header, stream=True)
    if response.status_code == 200:
        # We explicitly save to standardized filenames so preprocess_data works easily
        filename = f"data/uploads/post_meeting_masterclass.csv" if locale == "fr-FR" else f"data/uploads/post_meeting_masterclass_en.csv"
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        print(f"Successfully saved to {filename}")
        return filename
    else:
        print(f"Error downloading {locale}: {response.status_code} - {response.text}")
        return None

def download_techaway_answers(locale, token):
    endpoint = f"/surveys/techaway_post_tp/{locale}/answers/download?get_meeting_dt_start=true"
    header = {
        "Authorization": "Bearer " + token
    }
    print(f"Downloading Techaway answers for {locale}...")
    response = requests.get(URL + endpoint, headers=header, stream=True)
    if response.status_code == 200:
        filename = f"data/uploads/techaway_post_tp.csv" if locale == "fr-FR" else f"data/uploads/techaway_post_tp_en.csv"
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        print(f"Successfully saved to {filename}")
        return filename
    else:
        print(f"Error downloading techaway {locale}: {response.status_code} - {response.text}")
        return None

def refresh_data_from_nexus():
    """
    Returns True if successful, False otherwise.
    Raises Exception if credentials are missing or invalid.
    """
    creds = load_credentials()
    if not creds:
        raise ValueError("Credentials missing")
    
    token = get_auth(creds["email"], creds["password"])
    if not token:
        raise ValueError("Invalid credentials or authentication failed")

    fr_file = download_survey_answers("fr-FR", token)
    en_file = download_survey_answers("en-US", token)

    if not fr_file and not en_file:
        return False

    return True

def refresh_techaway_from_nexus():
    """
    Returns True if successful, False otherwise.
    Raises Exception if credentials are missing or invalid.
    """
    creds = load_credentials()
    if not creds:
        raise ValueError("Credentials missing")
    
    token = get_auth(creds["email"], creds["password"])
    if not token:
        raise ValueError("Invalid credentials or authentication failed")

    fr_file = download_techaway_answers("fr-FR", token)
    en_file = download_techaway_answers("en-US", token)

    if not fr_file and not en_file:
        return False

    return True
