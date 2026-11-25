# Vaaradhi Farms shopping assistant

Send “ghee”, “honey”, or “both” to your private Telegram bot. It returns links to the corresponding Vaaradhi Farms product pages. Select quantities, review delivery and pricing, and pay on the merchant’s site. The bot does not collect card details, submit orders, or schedule reorders.

## Run locally

Python 3.11 or newer is sufficient. Start the server with `python app.py` and check `http://localhost:8000/healthz`.

## Connect Telegram on Render

1. Create a dedicated bot with Telegram’s @BotFather and message it from your Telegram account.
2. Obtain your numeric Telegram user ID from the `message.from.id` field in the bot’s `getUpdates` response before enabling its webhook.
3. In the Render service’s **Environment** settings, set `TELEGRAM_BOT_TOKEN` to the BotFather token and `TELEGRAM_OWNER_ID` to your numeric Telegram user ID. Choose **Save and deploy**. Render supplies the public URL and the app registers the webhook on startup.
4. Send “ghee and honey” to the bot from your private chat and check that both links appear. A Render free instance can take time to respond after inactivity.

Keep the bot token in Render's environment settings. The app derives the webhook secret from the token unless you set `TELEGRAM_WEBHOOK_SECRET` explicitly. The bot replies only to the configured owner in a private chat.

For another HTTPS host, run `python app.py register-webhook HTTPS_SERVICE_ORIGIN` with the same Telegram environment variables as the server.
