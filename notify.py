#!/usr/bin/env python3
"""
Telegram delivery, shared by every report and by ad-hoc posts.

Callers:
  premarket_report.py   -- the morning digest
  ah_watch.py           -- the evening after-hours digest
  market_alerts_*.py    -- midday alerts, evening digest, weekly update
  bot.py                -- report attachments in reply to a command

Credentials come from the environment (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
Importing settings loads them from .env next to the scripts or from
~/.config/stock-research-bot.env when they are not already set. With either
value missing nothing is sent: every send returns False and says so on stderr.

CLI:
    python3 notify.py --text "<b>hello</b>"
    python3 notify.py --file message.html
    python3 notify.py --file message.html --dry-run

Stdlib only.
"""

import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html import escape as html_escape

import settings  # noqa: F401  (loads .env credentials)

API = "https://api.telegram.org/bot{token}/sendMessage"
API_DOC = "https://api.telegram.org/bot{token}/sendDocument"

# Telegram's hard cap is 4096; leave room for a continuation marker.
LIMIT = 3900


# A Telegram bot token: <bot id>:<35-character secret>.
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def leaks_secret(text):
    """
    True if the text carries the bot token, or anything shaped like one.

    The last line of defence for posts written from strangers' text (the
    alerts digest and weekly): whatever went wrong upstream, a credential
    never reaches a public channel.
    """
    if not text:
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token and token in text:
        return True
    secret = token.split(":", 1)[-1] if ":" in token else ""
    if len(secret) >= 20 and secret in text:
        return True
    return bool(TOKEN_RE.search(text))


def esc(v):
    """Escape a value for Telegram's HTML parse mode."""
    return html_escape(str(v), quote=False)


def split_message(text, limit=LIMIT):
    """
    Break an over-long message on line boundaries.

    Used by the ad-hoc path and the scheduled digests, where a busy day can run
    past the cap and every line matters.
    """
    if len(text) <= limit:
        return [text]
    parts, cur = [], []
    for line in text.split("\n"):
        candidate = "\n".join(cur + [line])
        if len(candidate) > limit and cur:
            parts.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        parts.append("\n".join(cur))
    return parts


def send(text, dry=False, parse_mode="HTML", split=False, preview=False):
    """
    Post to the channel. Returns True on success.

    Never raises: callers are reports whose output is already safely on disk,
    so a Telegram outage must cost the post and nothing else.
    """
    if leaks_secret(text):
        print("  ! Telegram post REFUSED: it contains a bot token", file=sys.stderr)
        return False
    if dry:
        print("\n--- telegram (not sent) ---\n" + text)
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("  ! Telegram skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set",
              file=sys.stderr)
        return False

    # Unsplit posts keep only the first line-boundary chunk: a character cut
    # can land inside an HTML tag and Telegram rejects the whole post.
    chunks = split_message(text) if split else split_message(text)[:1]
    ok = True
    for i, chunk in enumerate(chunks, 1):
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": chunk,
            "parse_mode": parse_mode,
            # preview=True lets Telegram show the link card -- used by the
            # midday alerts, where the card IS the content.
            "disable_web_page_preview": "false" if preview else "true",
        }).encode()
        # The token is in the URL, so failures report status and body only.
        req = urllib.request.Request(API.format(token=token), data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read().decode("utf-8", errors="ignore"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:400]
            print(f"  ! Telegram HTTP {e.code}: {body}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"  ! Telegram post failed: {e}", file=sys.stderr)
            return False
        if resp.get("ok"):
            tag = f" [{i}/{len(chunks)}]" if len(chunks) > 1 else ""
            print(f"✓ Telegram posted{tag} ({len(chunk)} chars)")
        else:
            print(f"  ! Telegram rejected: {resp}", file=sys.stderr)
            ok = False
    return ok


def send_document(filename, content, caption=None, dry=False):
    """
    Attach a file to the channel or group.

    Built as multipart by hand because the whole project is stdlib -- there is
    no requests here. Used for reports too long to be messages: Telegram caps a
    message at 4096 characters but not a document.
    """
    if dry:
        print(f"\n--- telegram document (not sent): {filename}, "
              f"{len(content)} bytes ---")
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("  ! Telegram skipped: credentials not set", file=sys.stderr)
        return False
    return _post_document(token, chat, filename, content, caption)


def _post_document(token, chat, filename, content, caption=None, reply_to=None):
    text = content if isinstance(content, str) else content.decode("utf-8", "ignore")
    if leaks_secret(caption) or leaks_secret(text):
        print(f"  ! sendDocument REFUSED: {filename} contains a bot token",
              file=sys.stderr)
        return False
    boundary = "----srb" + os.urandom(8).hex()
    if isinstance(content, str):
        content = content.encode("utf-8")

    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode())
    field("chat_id", chat)
    if caption:
        field("caption", caption[:1024])
        field("parse_mode", "HTML")
    if reply_to:
        field("reply_to_message_id", reply_to)
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f'filename="{filename}"\r\n'
        # PDFs too (the evening digest); markdown stays the default.
        f"Content-Type: {mimetypes.guess_type(filename)[0] or 'text/markdown'}\r\n\r\n".encode())
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        API_DOC.format(token=token), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as e:
        print(f"  ! sendDocument HTTP {e.code}: "
              f"{e.read().decode('utf-8','ignore')[:300]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ! sendDocument failed: {e}", file=sys.stderr)
        return False
    if resp.get("ok"):
        print(f"\u2713 Telegram document sent ({filename}, {len(content)} bytes)")
        return True
    print(f"  ! sendDocument rejected: {resp}", file=sys.stderr)
    return False


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    text = None
    if "--text" in args:
        text = args[args.index("--text") + 1]
    elif "--file" in args:
        with open(args[args.index("--file") + 1]) as f:
            text = f.read()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text or not text.strip():
        print(__doc__)
        return 2
    return 0 if send(text.strip(), dry=dry, split=True) else 1


if __name__ == "__main__":
    sys.exit(main())
