"""Telegram webhook for the Vaaradhi Farms shopping assistant."""

import hmac
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PRODUCTS = {
    "ghee": ("A2 Desi Cow Cultured Ghee", "https://vaaradhifarms.com/products/a2-desi-cow-ghee"),
    "honey": ("Farm Fresh Raw Natural Honey", "https://vaaradhifarms.com/products/natural-honey"),
}
MAX_UPDATE_BYTES = 256 * 1024
LOG = logging.getLogger(__name__)


def telegram(method, payload):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=8) as response:
        if not json.load(response).get("ok"):
            raise ValueError("Telegram rejected the request")


def webhook_secret():
    explicit_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if explicit_secret:
        return explicit_secret
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token:
        return hmac.digest(token.encode(), b"shopping-assistant-webhook-v1", "sha256").hex()
    return ""


def reply(update, owner_id):
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    text = message.get("text")
    if (
        not isinstance(chat, dict)
        or chat.get("type") != "private"
        or str(chat.get("id")) != owner_id
        or not isinstance(sender, dict)
        or str(sender.get("id")) != owner_id
        or not isinstance(text, str)
    ):
        return None

    words = set(re.findall(r"[a-z]+", text.lower()))
    selected = [product for key, product in PRODUCTS.items() if key in words]
    if words.intersection({"both", "shop", "products"}):
        selected = list(PRODUCTS.values())

    result = {"chat_id": chat["id"]}
    if selected:
        result["text"] = "Choose a product to select its size and quantity and check out on Vaaradhi Farms."
        result["reply_markup"] = {
            "inline_keyboard": [[{"text": name, "url": url}] for name, url in selected]
        }
    else:
        result["text"] = 'Ask for ghee, honey, or both. For example, “Order ghee and honey”.'
    return result


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Avoid retaining request paths or Telegram identifiers in logs.
        return

    def do_GET(self):
        if self.path == "/healthz":
            self.respond(200, b"ok\n")
        else:
            self.respond(404)

    def do_POST(self):
        if self.path != "/telegram/webhook":
            self.respond(404)
            return
        expected = webhook_secret()
        owner_id = os.getenv("TELEGRAM_OWNER_ID", "")
        if not expected or not owner_id or not os.getenv("TELEGRAM_BOT_TOKEN"):
            self.respond(503)
            return
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            self.respond(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_UPDATE_BYTES:
                self.respond(400)
                return
            update = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError, TimeoutError):
            self.respond(400)
            return

        result = reply(update, owner_id)
        if result:
            try:
                telegram("sendMessage", result)
            except (URLError, ValueError, TimeoutError):
                LOG.warning("Telegram sendMessage failed")
                self.respond(502)
                return
        self.respond(200)

    def setup(self):
        super().setup()
        self.connection.settimeout(10)


def register_webhook(base_url):
    url = urlsplit(base_url)
    if url.scheme != "https" or not url.hostname or url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("Provide the HTTPS service origin")
    secret = webhook_secret()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise ValueError("Invalid webhook secret format")
    telegram(
        "setWebhook",
        {
            "url": f"{base_url.rstrip('/')}/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["message"],
        },
    )


def register_render_webhook(base_url, stopping):
    while not stopping.is_set():
        try:
            register_webhook(base_url)
        except ValueError:
            LOG.error("Invalid Telegram webhook configuration")
            return
        except (URLError, TimeoutError):
            LOG.warning("Telegram webhook registration failed. Retrying in 60 seconds.")
            stopping.wait(60)
        else:
            LOG.info("Telegram webhook registered.")
            return


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) == 3 and sys.argv[1] == "register-webhook":
        try:
            register_webhook(sys.argv[2])
        except (URLError, ValueError, TimeoutError, KeyError):
            sys.exit("Webhook registration failed. Check the service URL and Telegram settings.")
        print("Telegram webhook registered.")
    elif len(sys.argv) == 1:
        with ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8000"))), Handler) as server:
            origin = os.getenv("RENDER_EXTERNAL_URL")
            stopping = threading.Event()
            if origin and os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_OWNER_ID"):
                threading.Thread(target=register_render_webhook, args=(origin, stopping), daemon=True).start()
            try:
                server.serve_forever()
            finally:
                stopping.set()
    else:
        sys.exit("Usage: python app.py [register-webhook HTTPS_SERVICE_ORIGIN]")
