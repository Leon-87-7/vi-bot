import logging
import re

import google.auth.exceptions
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

logger = logging.getLogger(__name__)

_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("_")
    return text[:60]


def build_services(service_account_path: str, oauth_client_id: str, oauth_client_secret: str, oauth_refresh_token: str):
    drive_creds = OAuthCredentials(
        token=None,
        refresh_token=oauth_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=oauth_client_id,
        client_secret=oauth_client_secret,
        scopes=_DRIVE_SCOPES,
    )
    try:
        sheets_creds = service_account.Credentials.from_service_account_file(
            service_account_path, scopes=_SHEETS_SCOPES
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Service account file not found: {service_account_path}") from exc
    except (ValueError, google.auth.exceptions.GoogleAuthError) as exc:
        raise ValueError(f"Invalid service account file ({service_account_path}): {exc}") from exc
    drive_service = build("drive", "v3", credentials=drive_creds)
    sheets_service = build("sheets", "v4", credentials=sheets_creds)
    return drive_service, sheets_service


def upload_to_drive(drive_service, folder_id: str, filename: str, content: str) -> str:
    file_metadata = {
        "name": filename,
        "parents": [folder_id],
        "mimeType": "text/markdown",
    }
    media = MediaInMemoryUpload(content.encode(), mimetype="text/markdown")
    try:
        file = (
            drive_service.files()
            .create(body=file_metadata, media_body=media, fields="webViewLink", supportsAllDrives=True)
            .execute()
        )
    except HttpError as exc:
        logger.error("Drive upload failed for %s in folder %s: %s", filename, folder_id, exc)
        raise
    return file["webViewLink"]


def append_to_sheet(sheets_service, spreadsheet_id: str, row: list) -> None:
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except HttpError as exc:
        logger.error("Sheets append failed for %s row %s: %s", spreadsheet_id, row, exc)
        raise