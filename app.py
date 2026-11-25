"""Telegram webhook for the shopping assistant."""

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

PRODUCTS = {
    "ghee": ("A2 Desi Cow Cultured Ghee", "https://vaaradhifarms.com/products/a2-desi-cow-ghee"),
    "honey": ("Farm Fresh Raw Natural Honey", "https://vaaradhifarms.com/products/natural-honey"),
    "shampoo": ("Familio All in One Kids Shampoo & Conditioner", "https://familio.in/products/all-in-one-hair-shampoo-conditioner-for-babies-kids-teens"),
}
STORE_NAMES = {"vaaradhifarms.com": "Vaaradhi Farms", "familio.in": "Familio"}
VARAADHI_PRODUCTS = ("ghee", "honey")
PRODUCT_WORD = "|".join(PRODUCTS)
BUY_ITEM = rf"(?:[1-9][0-9]?\s+)?(?:{PRODUCT_WORD})"
BUY_REQUEST = re.compile(
    rf"^/?(?:buy|order)\s+(both|{BUY_ITEM}(?:(?:\s+and\s+|,\s*){BUY_ITEM})*)[.!]?$",
    re.IGNORECASE,
)
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


def available_variant(product_url):
    request = Request(product_url + ".js", headers={"Accept": "application/json"})
    with urlopen(request, timeout=3) as response:
        product = json.loads(response.read(262_144))
    variants = [
        variant for variant in product["variants"]
        if variant["available"] is True and type(variant["id"]) is int
        and variant["id"] > 0 and type(variant["price"]) is int
        and isinstance(variant["title"], str)
    ]
    return min(variants, key=lambda variant: variant["price"], default=None)


def checkout_signature(path):
    return hmac.digest(
        webhook_secret().encode(), f"checkout-address:{path}".encode(), "sha256"
    ).hex()


def checkout_button_url(host, items):
    cart = ",".join(items)
    origin = os.getenv("RENDER_EXTERNAL_URL")
    if origin and webhook_secret() and os.getenv("CHECKOUT_SHIPPING_ADDRESS"):
        path = f"/checkout/{host}/{cart}/{int(time.time()) + 900}"
        return f"{origin.rstrip('/')}{path}/{checkout_signature(path)}"
    return f"https://{host}/cart/{cart}"


def checkout_destination(path):
    match = re.fullmatch(
        r"(/checkout/([a-z.]+)/([0-9]+:[0-9]+(?:,[0-9]+:[0-9]+)*)/([0-9]{1,12}))/([a-f0-9]{64})",
        path,
    )
    if not match or not webhook_secret():
        return None
    signed_path, host, cart, expiry, signature = match.groups()
    if host not in STORE_NAMES or not hmac.compare_digest(checkout_signature(signed_path), signature):
        return None
    if int(expiry) < time.time():
        return None
    destination = f"https://{host}/cart/{cart}"
    try:
        address = json.loads(os.getenv("CHECKOUT_SHIPPING_ADDRESS", "{}"))
    except ValueError:
        return destination
    if not isinstance(address, dict):
        return destination
    params = {
        f"checkout[shipping_address][{field}]": address[field]
        for field in ("first_name", "last_name", "address1", "address2", "city", "province", "zip", "country", "phone")
        if isinstance(address.get(field), str) and address[field]
    }
    return f"{destination}?{urlencode(params)}" if params else destination


def checkout_reply(chat_id, request):
    quantities = {}
    if request.lower() == "both":
        quantities = dict.fromkeys(VARAADHI_PRODUCTS, 1)
    else:
        for quantity, key in re.findall(rf"(?:(\d+)\s+)?({PRODUCT_WORD})", request.lower()):
            quantities[key] = quantities.get(key, 0) + int(quantity or "1")

    carts = {}
    summary = []
    for key, quantity in quantities.items():
        name, url = PRODUCTS[key]
        try:
            variant = available_variant(url)
        except (URLError, TimeoutError, ValueError, KeyError, TypeError):
            variant = None
        if variant is None:
            return {
                "chat_id": chat_id,
                "text": f"I couldn’t confirm availability for {name}. Check the store before ordering.",
                "reply_markup": {"inline_keyboard": [[{"text": name, "url": url}]]},
            }
        host = urlsplit(url).netloc
        carts.setdefault(host, []).append(f"{variant['id']}:{quantity}")
        summary.append(f"{quantity} × {name} ({variant['title']})")

    checkout_buttons = [
        [{"text": f"Review and pay on {STORE_NAMES[host]}", "url": checkout_button_url(host, items)}]
        for host, items in carts.items()
    ]
    return {
        "chat_id": chat_id,
        "text": "Checkout is ready with the lowest-priced available size for each item.\n\n"
                + "\n".join(summary)
                + "\n\nReview shipping and the total, then pay on the store’s checkout page.",
        "reply_markup": {"inline_keyboard": checkout_buttons},
    }


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

    purchase = BUY_REQUEST.fullmatch(text.strip())
    if purchase:
        return checkout_reply(chat["id"], purchase.group(1))

    words = set(re.findall(r"[a-z]+", text.lower()))
    selected = [product for key, product in PRODUCTS.items() if key in words]
    if words.intersection({"shop", "products"}):
        selected = list(PRODUCTS.values())
    elif "both" in words:
        selected = [product for key, product in PRODUCTS.items() if key in VARAADHI_PRODUCTS or key in words]

    result = {"chat_id": chat["id"]}
    if selected:
        result["text"] = "Choose a product to select its size and quantity and check out on the store’s website."
        result["reply_markup"] = {
            "inline_keyboard": [[{"text": name, "url": url}] for name, url in selected]
        }
    else:
        result["text"] = 'Ask for ghee, honey, or shampoo. For checkout, try “buy shampoo” or “buy ghee and shampoo”.'
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
        elif self.path.startswith("/checkout/"):
            destination = checkout_destination(self.path)
            if destination is None:
                self.respond(404, b"Checkout link expired or invalid. Ask the bot for a new link.")
                return
            self.send_response(302)
            self.send_header("Location", destination)
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
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
