#!/usr/bin/env python3
"""Validation-only script: confirms OAuth1.0a auth works and that a media
upload succeeds, WITHOUT posting any tweet. Safe to run any time - unattached
media uploads are not publicly visible and expire on X's side if unused.
"""
import json
import os

import post_daily as pd


def main():
    print("=== GET /2/users/me ===")
    status, body = pd.api_request("GET", "https://api.twitter.com/2/users/me")
    print(status, body)

    print("\n=== POST media/upload (v1.1) using screenshots/freerun.jpg ===")
    image_path = os.path.join(pd.SCRIPT_DIR, "screenshots", "freerun.jpg")
    media_id = pd.upload_media(image_path)
    print("media_id_string:", media_id)

    print("\n=== SUMMARY ===")
    print("auth OK:", status == 200)
    print("media upload OK: True (see media_id above)")
    print("NOTE: no tweet was posted - this script only validates the pipeline.")


if __name__ == "__main__":
    main()
