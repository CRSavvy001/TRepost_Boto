import os
import re
import time
import logging
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repost-alert-bot")

# --- Config from environment variables (set these in Railway) ---
SOURCE_CHANNEL = os.environ["SOURCE_CHANNEL"]      # public channel username, e.g. "axioscan" (no @)
BOT_TOKEN = os.environ["BOT_TOKEN"]                # from @BotFather
ALERT_CHAT_ID = os.environ["ALERT_CHAT_ID"]        # your own numeric user id

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", 15))
MIN_WINDOW = int(os.environ.get("MIN_WINDOW_SECONDS", 60))     # 1 min
MAX_WINDOW = int(os.environ.get("MAX_WINDOW_SECONDS", 300))    # 5 min
STALE_AFTER = int(os.environ.get("STALE_AFTER_SECONDS", 900))  # purge entries older than this
DEBUG_HTML = os.environ.get("DEBUG_HTML", "false").lower() == "true"

PREVIEW_URL = f"https://t.me/s/{SOURCE_CHANNEL}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Solana contract addresses are base58: no 0, O, I, l. Typically 32-44 chars.
CA_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
LABELED_CA_REGEX = re.compile(
    r"(?:CA|Contract|Address)\s*[:\-]?\s*([1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
HASHTAG_REGEX = re.compile(r"#(\w+)")

# Original signal posts (contain a CA), keyed by message id:
#   { message_id: {"ca": str, "symbol": str, "ts": float} }
originals = {}

# Fallback index: latest original message id seen for a given #symbol.
by_symbol = {}

last_seen_id = 0  # highest message id already processed, to avoid reprocessing on each poll


def extract_ca(text: str):
    if not text:
        return None
    # This channel always explicitly labels the address as "CA: ...", so we
    # only trust that labeled form rather than matching any base58-looking
    # string, which risks false positives from unrelated text.
    labeled = LABELED_CA_REGEX.search(text)
    return labeled.group(1) if labeled else None


def extract_symbol(text: str):
    if not text:
        return None
    match = HASHTAG_REGEX.search(text)
    return match.group(1).upper() if match else None


def send_alert(ca: str, symbol: str, delta_seconds: float, msg_link: str | None):
    text = (
        "🚨 Repost detected (1-5 min window)\n\n"
        f"Symbol: #{symbol}\n"
        f"Contract: `{ca}`\n"
        f"Gap between posts: {int(delta_seconds)}s (~{delta_seconds/60:.1f} min)\n"
    )
    if msg_link:
        text += f"Update post: {msg_link}\n"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": ALERT_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not resp.ok:
            log.error("Failed to send alert: %s", resp.text)
    except Exception:
        log.exception("Error sending alert")


def cleanup():
    now = time.time()
    stale_ids = [mid for mid, data in originals.items() if now - data["ts"] > STALE_AFTER]
    for mid in stale_ids:
        symbol = originals[mid]["symbol"]
        del originals[mid]
        if by_symbol.get(symbol) == mid:
            del by_symbol[symbol]


def parse_timestamp(time_tag):
    """Parse the <time datetime="..."> attribute into epoch seconds."""
    if time_tag is None or not time_tag.get("datetime"):
        return time.time()  # fallback: treat as "now" if we can't parse
    try:
        dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return time.time()


def fetch_messages():
    """
    Fetch and parse the public t.me/s/<channel> preview page.
    Returns a list of dicts sorted oldest -> newest:
        {"id": int, "text": str, "ts": float, "reply_to": int or None}
    """
    resp = requests.get(PREVIEW_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    if DEBUG_HTML:
        log.info("--- RAW HTML (first 2000 chars) ---\n%s", resp.text[:2000])

    soup = BeautifulSoup(resp.text, "html.parser")

    blocks = soup.select("div.tgme_widget_message[data-post]")
    if not blocks:
        log.warning(
            "No message blocks found with expected selector 'div.tgme_widget_message[data-post]'. "
            "Telegram's page markup may have changed - set DEBUG_HTML=true to inspect raw HTML."
        )
        return []

    # Telegram's preview page can render multi-photo posts (albums) as several
    # HTML blocks sharing the same data-post message id. Dedupe by id so each
    # logical message is only processed once.
    messages_by_id = {}

    for block in blocks:
        data_post = block.get("data-post", "")
        try:
            msg_id = int(data_post.split("/")[-1])
        except (ValueError, IndexError):
            continue

        # Pull the reply target id BEFORE removing the reply-preview element,
        # then strip that element out entirely. Telegram's page embeds the
        # FULL text of the quoted/replied-to message inside this block (even
        # though it's shown truncated in the app), which was leaking the
        # original post's CA into the update post's own text extraction.
        reply_to = None
        reply_block = block.select_one("a.tgme_widget_message_reply, div.tgme_widget_message_reply")
        if reply_block is not None:
            href = reply_block.get("href")
            if href:
                try:
                    reply_to = int(href.rstrip("/").split("/")[-1])
                except (ValueError, IndexError):
                    reply_to = None
            reply_block.extract()

        text_div = block.select_one("div.tgme_widget_message_text")
        text = text_div.get_text(separator="\n") if text_div else ""

        time_tag = block.select_one("time")
        ts = parse_timestamp(time_tag)

        entry = {"id": msg_id, "text": text, "ts": ts, "reply_to": reply_to}

        # Prefer the block with the most text (album items after the first
        # sometimes carry an empty caption) if we've already seen this id.
        existing = messages_by_id.get(msg_id)
        if existing is None or len(text) > len(existing["text"]):
            messages_by_id[msg_id] = entry

    messages = sorted(messages_by_id.values(), key=lambda m: m["id"])
    return messages


def process_message(msg, alert_enabled=True):
    ca = extract_ca(msg["text"])
    symbol = extract_symbol(msg["text"])
    now = msg["ts"]

    # Case 1: original signal post (has both a CA and a symbol).
    if ca and symbol:
        originals[msg["id"]] = {"ca": ca, "symbol": symbol, "ts": now}
        by_symbol[symbol] = msg["id"]
        log.info("New signal: #%s %s", symbol, ca)
        return

    # Case 2: looks like an update/repost (has a symbol, no CA).
    if symbol and not ca:
        origin = originals.get(msg["reply_to"]) if msg["reply_to"] else None

        # Fallback: no usable reply link, match by most recent same symbol.
        if origin is None:
            fallback_id = by_symbol.get(symbol)
            origin = originals.get(fallback_id) if fallback_id else None

        if origin is None:
            log.info("Update for #%s but no matching original found", symbol)
            return

        delta = now - origin["ts"]
        if MIN_WINDOW <= delta <= MAX_WINDOW:
            if alert_enabled:
                msg_link = f"https://t.me/{SOURCE_CHANNEL}/{msg['id']}"
                log.info("Repost match for #%s after %.0fs", symbol, delta)
                send_alert(origin["ca"], symbol, delta, msg_link)
            else:
                log.info("Repost match for #%s during startup backfill - not alerting", symbol)
        else:
            log.info("Update for #%s outside window (%.0fs)", symbol, delta)


def poll_loop():
    global last_seen_id
    log.info("Polling %s every %ss", PREVIEW_URL, POLL_INTERVAL)

    while True:
        try:
            messages = fetch_messages()
            new_messages = [m for m in messages if m["id"] > last_seen_id]

            if new_messages:
                for msg in new_messages:
                    process_message(msg)
                    last_seen_id = max(last_seen_id, msg["id"])
                cleanup()
            elif last_seen_id == 0 and messages:
                # First run: populate state from current history but don't alert on it.
                last_seen_id = max(m["id"] for m in messages)
                for msg in messages:
                    process_message(msg, alert_enabled=False)
                log.info("Initialized. Latest message id: %s", last_seen_id)

        except requests.RequestException:
            log.exception("Error fetching channel page")
        except Exception:
            log.exception("Unexpected error in poll loop")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll_loop()
