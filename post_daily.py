#!/usr/bin/env python3
"""Posts one daily promotional tweet for the Destroy the Flag Steam Playtest
campaign, quote-tweeting the pinned announcement post.

Usage:
    python3 post_daily.py --dry-run          # see today's screenshot/context, no API calls
    python3 post_daily.py --text "Your tweet text here"   # actually post

Behavior:
- Reads campaign_config.json (same directory) for pinned_tweet_id,
  campaign_start_date, and campaign_days.
- The screenshot pool is NOT listed in the config - it's discovered by
  scanning the screenshots/ directory (sorted filename order) each run. Drop
  a new .jpg/.jpeg/.png into screenshots/ and push it; it enters the
  rotation automatically next time its index comes up, no config edit
  needed. Whoever writes the caption (the calling agent, via --dry-run) is
  expected to look at the actual image file to describe it - there is no
  per-image desc field to maintain.
- Computes which day of the campaign "today" (UTC date) is, and picks the
  screenshot for that day deterministically (day_index % len(screenshot
  files)). No external state/memory is needed across runs, so this is safe
  to fire from a stateless recurring cloud routine.
- Safety gates (all silent no-ops, exit code 0, so a misfire never posts):
    * pinned_tweet_id is null/empty -> skip
    * campaign_start_date is null/empty -> skip
    * today is before campaign_start_date -> skip
    * today is on/after campaign_start_date + campaign_days -> skip
- On success, uploads the day's screenshot (v1.1 media/upload) and creates
  a quote-tweet (v2 /tweets) with the given text + that image.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "campaign_config.json")
SCREENSHOTS_DIR = os.path.join(SCRIPT_DIR, "screenshots")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")

# X (Twitter) OAuth1.0a user-context credentials for the Destroy the Flag
# account. Scoped only to this account's posting ability.
CONSUMER_KEY = "8GIQAtP9tGyGdsXvRg64oBcMB"
CONSUMER_SECRET = "lGU7dzs5KKXjUPhAHtlRycWtjWV2ZPIzeIR4pMilfqxUxRGV0Z"
ACCESS_TOKEN = "1226762480354856962-rk6jAyyAAJdYzlk0kQbx5dPKC7v3lY"
ACCESS_TOKEN_SECRET = "jRJzKcbQ8YaqMgG55WdbmzvMipdSHLnWmClhc18ueC2Jg"


def percent_encode(s):
    return urllib.parse.quote(str(s), safe="~")


def oauth_header(method, url, extra_params):
    """Build an OAuth1.0a Authorization header per RFC5849 / X's docs.

    extra_params should only contain query-string params (GET) or
    x-www-form-urlencoded body params - never JSON bodies or multipart
    payloads, which are excluded from the OAuth1.0a signature base string.
    """
    oauth_params = {
        "oauth_consumer_key": CONSUMER_KEY,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": ACCESS_TOKEN,
        "oauth_version": "1.0",
    }
    all_params = {**oauth_params, **extra_params}
    param_string = "&".join(
        f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(all_params.items())
    )
    base_string = "&".join([method.upper(), percent_encode(url), percent_encode(param_string)])
    signing_key = f"{percent_encode(CONSUMER_SECRET)}&{percent_encode(ACCESS_TOKEN_SECRET)}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params["oauth_signature"] = signature
    return "OAuth " + ", ".join(
        f'{percent_encode(k)}="{percent_encode(v)}"' for k, v in sorted(oauth_params.items())
    )


def api_request(method, url, extra_params=None, data=None, content_type=None):
    extra_params = extra_params or {}
    headers = {"Authorization": oauth_header(method, url, extra_params)}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def upload_media(image_path):
    with open(image_path, "rb") as f:
        media_data = f.read()
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{os.path.basename(image_path)}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + media_data + f"\r\n--{boundary}--\r\n".encode()
    status, body_text = api_request(
        "POST",
        "https://upload.twitter.com/1.1/media/upload.json",
        data=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    if status not in (200, 201):
        raise RuntimeError(f"media upload failed: {status} {body_text}")
    return json.loads(body_text)["media_id_string"]


def post_tweet(text, media_id, quote_tweet_id):
    payload = {
        "text": text,
        "media": {"media_ids": [media_id]},
        "quote_tweet_id": str(quote_tweet_id),
    }
    status, body_text = api_request(
        "POST",
        "https://api.twitter.com/2/tweets",
        data=json.dumps(payload).encode(),
        content_type="application/json",
    )
    if status not in (200, 201):
        raise RuntimeError(f"tweet create failed: {status} {body_text}")
    return json.loads(body_text)


def list_screenshots():
    if not os.path.isdir(SCREENSHOTS_DIR):
        return []
    return sorted(
        f for f in os.listdir(SCREENSHOTS_DIR) if f.lower().endswith(IMAGE_EXTENSIONS)
    )


def resolve_today():
    """Returns (filename, day_index, config, skip_reason). Exactly one of
    (filename) or (skip_reason) is non-None. Pure/deterministic given
    campaign_config.json + the current contents of screenshots/ + today's
    UTC date - no external state needed.
    """
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    pinned_tweet_id = config.get("pinned_tweet_id")
    start_date_str = config.get("campaign_start_date")
    campaign_days = config.get("campaign_days", 21)
    screenshots = list_screenshots()

    if not pinned_tweet_id:
        return None, None, config, "pinned_tweet_id not set yet."
    if not start_date_str:
        return None, None, config, "campaign_start_date not set yet."
    if not screenshots:
        return None, None, config, "no screenshots found in screenshots/."

    start_date = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_index = (today - start_date).days

    if day_index < 0:
        return None, None, config, f"campaign has not started yet (starts {start_date_str})."
    if day_index >= campaign_days:
        return None, None, config, f"campaign already finished (day_index={day_index} >= {campaign_days})."

    filename = screenshots[day_index % len(screenshots)]
    return filename, day_index, config, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="Tweet text to post today (required unless --dry-run)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which screenshot/day today resolves to (or the skip reason) and exit "
        "without calling the X API. Use this first to see what to write about.",
    )
    args = parser.parse_args()

    filename, day_index, config, skip_reason = resolve_today()

    if args.dry_run:
        if skip_reason:
            print(json.dumps({"skip_reason": skip_reason}))
        else:
            print(json.dumps({
                "day_index": day_index,
                "day_number": day_index + 1,
                "campaign_days": config.get("campaign_days", 21),
                "file": f"screenshots/{filename}",
                "note": "No pre-written description - look at the image file yourself "
                        "(it's a real screenshot from the game) and write the caption "
                        "based on what you actually see in it.",
                "game_name": config.get("game_name", ""),
                "game_blurb": config.get("game_blurb", ""),
                "cta": config.get("cta", ""),
            }, indent=2))
        return

    if skip_reason:
        print(f"SKIP: {skip_reason}")
        return
    if not args.text:
        raise SystemExit("--text is required when not using --dry-run")

    image_path = os.path.join(SCREENSHOTS_DIR, filename)
    print(f"Day {day_index + 1}/{config.get('campaign_days', 21)}: using screenshots/{filename}")

    media_id = upload_media(image_path)
    print("Uploaded media_id:", media_id)

    result = post_tweet(args.text, media_id, config["pinned_tweet_id"])
    print("Posted:", json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
