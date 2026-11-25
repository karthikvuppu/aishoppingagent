# Vaaradhi Farms shopping assistant

Send “ghee”, “honey”, or “both” to your private Telegram bot. It returns links to the corresponding Vaaradhi Farms product pages. Select quantities, review delivery and pricing, and pay on the merchant’s site. The bot does not collect card details, submit orders, or schedule reorders.

## Run locally

Python 3.11 or newer is sufficient. Start the server with `python app.py` and check `http://localhost:8000/healthz`.

## Connect Telegram on Render

1. Create a bot with Telegram’s @BotFather and message the bot from your Telegram account.
2. Obtain your numeric Telegram user ID from the `message.from.id` field in the bot’s `getUpdates` response before registering a webhook.
3. Create a free Render web service from this repo or import its Blueprint. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, and `TELEGRAM_WEBHOOK_SECRET` in the service’s environment settings. Generate the webhook secret with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
4. With the same token and secret set in your local environment, run `python app.py register-webhook https://YOUR-SERVICE.onrender.com` once after the service is live.
5. Send “ghee and honey” to the bot from your private chat and check that both links appear. A Render free instance can take time to respond after a period of inactivity.

Keep the bot token and webhook secret in environment variables. The service only replies to the configured owner in a private chat.
