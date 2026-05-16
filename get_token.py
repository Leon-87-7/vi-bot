"""
One-time script to get a Google OAuth refresh token for Drive access.

Usage:
    pip install google-auth-oauthlib
    python get_token.py <path-to-client-secret.json>

Copy the printed values into your .env file.
"""
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    if len(sys.argv) < 2:
        print("Usage: python get_token.py <client_secret.json>")
        sys.exit(1)

    secrets_file = sys.argv[1]
    flow = InstalledAppFlow.from_client_secrets_file(secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)

    print("\nAdd these to your .env:\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
