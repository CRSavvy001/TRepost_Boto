# # Telegram Repost Alert Bot (no login required)

Monitors one **public** Telegram channel and alerts you when the same coin
signal is reposted **1–5 minutes** after it first appeared — without logging
into any Telegram account. It works by periodically checking the channel's
public web preview page, which Telegram exposes for any public channel at
`https://t.me/s/<channelname>`.

## How it works

- The channel posts two kinds of messages:
  - **Original signal posts** — contain a contract address (CA) and a
    `#SYMBOL` hashtag.
  - **Update/repost posts** — contain only the `#SYMBOL` hashtag (no CA),
    usually posted as a reply to the original.
- Every `POLL_INTERVAL_SECONDS` (default 15s), the bot fetches
  `https://t.me/s/<SOURCE_CHANNEL>` and parses the visible messages.
- On an original post, it remembers `{message_id: {ca, symbol, timestamp}}`.
- On an update post, it tries to match it to the original it's replying to
  (most reliable). If that's not detectable, it falls back to matching the
  most recent original post with the same `#SYMBOL`.
- If the matched original was posted **60–300 seconds** before the update,
  it sends you an alert via a separate **Telegram bot** (the "notifier").

## Important limitation

This only works for **public** channels (the channel must have a public
`@username`, which AXIOSCAN does). It also depends on Telegram's public page
keeping a stable HTML structure — if they change it, parsing could silently
find zero messages. If that happens:
1. Set `DEBUG_HTML=true` as an env var and redeploy.
2. Check Railway's **Logs** tab — it will print the first 2000 characters of
   the raw page HTML so the parser can be adjusted.

## 1. Create the notifier bot

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts.
2. Copy the **bot token** it gives you (`BOT_TOKEN`).
3. Send your new bot any message (e.g. "hi") so it's allowed to message you
   back.
4. Get your numeric Telegram user ID for `ALERT_CHAT_ID` — message
   **@userinfobot** and it will reply with your ID.

## 2. Push to GitHub

Using GitHub's mobile web UI (no computer needed):

1. Go to github.com → **+** → **New repository** → name it → **Create**.
2. On the empty repo page: **Add file → Create new file** for each of these
   files, pasting in the contents, then **Commit changes**:
   - `main.py`
   - `requirements.txt`
   - `Procfile`
   - `.env.example`
   - `.gitignore`
   - `README.md`

## 3. Deploy on Railway

1. Railway dashboard → **New Project** → **Deploy from GitHub repo** →
   select this repo.
2. Railway detects the `Procfile` and runs it as a **worker** (no public web
   port needed — this is an always-on background process, that's expected).
3. Go to the service's **Variables** tab and add:
   - `SOURCE_CHANNEL` (e.g. `axioscan`, no `@`)
   - `BOT_TOKEN`
   - `ALERT_CHAT_ID`
   - (optional) `POLL_INTERVAL_SECONDS`, `MIN_WINDOW_SECONDS`,
     `MAX_WINDOW_SECONDS`, `STALE_AFTER_SECONDS`, `DEBUG_HTML`
4. Deploy. Check the **Logs** tab — you should see
   `Polling https://t.me/s/axioscan every 15s` followed by
   `Initialized. Latest message id: ...`.

## Notes / limitations

- State is kept **in memory**, so a redeploy/restart clears the "seen"
  history (not a big issue since the window is only 5 minutes).
- Polling means detection has up to `POLL_INTERVAL_SECONDS` of lag — with
  the default 15s, you'll be alerted within ~15s of the repost appearing,
  not instantly.
- The regex assumes **Solana** base58 addresses. If the channel posts
  Ethereum-style `0x...` addresses too, this would need extending.
- This only tracks *whether* a signal was reposted within the window — it
  doesn't compare price/market cap.
- The `#symbol` fallback match (used only if a repost isn't detectably a
  reply) can mis-associate if two different tokens reuse the same ticker
  within a few minutes of each other. Rare, but worth knowing.
- The very first time the bot runs, it reads the channel's currently visible
  history to initialize its state, but won't send alerts for anything that
  already happened before it started.
