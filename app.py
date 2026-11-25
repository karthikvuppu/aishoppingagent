"""Telegram webhook for the shopping assistant."""

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

PRODUCTS = {
    "ghee": ("A2 Desi Cow Cultured Ghee", "https://vaaradhifarms.com/products/a2-desi-cow-ghee"),
    "honey": ("Farm Fresh Raw Natural Honey", "https://vaaradhifarms.com/products/natural-honey"),
    "shampoo": ("Familio All in One Kids Shampoo & Conditioner", "https://familio.in/products/all-in-one-hair-shampoo-conditioner-for-babies-kids-teens"),
}
AAROGYA_HOST = "www.aarogyanutrimix.in"
STORE_NAMES = {
    "vaaradhifarms.com": "Vaaradhi Farms",
    "familio.in": "Familio",
    AAROGYA_HOST: "Aarogya Nutri Mix",
}
VARAADHI_PRODUCTS = ("ghee", "honey")
PRODUCT_WORD = "|".join(PRODUCTS)
BUY_ITEM = rf"(?:[1-9][0-9]?\s+)?(?:{PRODUCT_WORD})"
BUY_REQUEST = re.compile(
    rf"^/?(?:buy|order)\s+(both|{BUY_ITEM}(?:(?:\s+and\s+|,\s*){BUY_ITEM})*)[.!]?$",
    re.IGNORECASE,
)
AAROGYA_ORIGIN = f"https://{AAROGYA_HOST}"
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


def aarogya_checkout_button(product, quantity):
    handle = product.get("handle")
    title = product.get("title")
    if not isinstance(handle, str) or not re.fullmatch(r"[a-z0-9-]+", handle):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    try:
        variant = available_variant(f"{AAROGYA_ORIGIN}/products/{handle}")
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    if variant is None:
        return None
    size = "" if variant["title"] == "Default Title" else f" · {variant['title']}"
    return {
        "text": f"{quantity} × {title}{size}",
        "url": checkout_button_url(AAROGYA_HOST, [f"{variant['id']}:{quantity}"]),
    }


def aarogya_catalog_reply(chat_id, page):
    browse_url = f"{AAROGYA_ORIGIN}/collections/all-products"
    buttons = []
    try:
        request = Request(
            f"{browse_url}/products.json?limit=6&page={page}",
            headers={"Accept": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            products = json.loads(response.read(524_288))["products"]
        for product in products:
            title = product["title"]
            handle = product["handle"]
            if not re.fullmatch(r"[a-z0-9-]+", handle):
                continue
            variants = [
                variant for variant in product["variants"]
                if variant["available"] is True and type(variant["id"]) is int and variant["id"] > 0
            ]
            if variants:
                variant = min(variants, key=lambda item: Decimal(item["price"]))
                size = "" if variant["title"] == "Default Title" else f" · {variant['title']}"
                button = {
                    "text": f"Buy 1 × {title}{size}",
                    "url": checkout_button_url(AAROGYA_HOST, [f"{variant['id']}:1"]),
                }
            else:
                button = {"text": f"View {title} (sold out)", "url": f"{AAROGYA_ORIGIN}/products/{handle}"}
            buttons.append([button])
        message = f"Aarogya catalog · Page {page}. Tap an available item to open checkout."
        if not products:
            message = f"No products on page {page}."
        if len(products) == 6:
            message += f"\nNext page: Aarogya all {page + 1}"
        if page > 1:
            message += f"\nPrevious page: Aarogya all {page - 1}"
    except (URLError, TimeoutError, ValueError, KeyError, TypeError, InvalidOperation):
        message = "The catalog could not load. You can open Aarogya’s full catalog below."

    return {
        "chat_id": chat_id,
        "text": message,
        "reply_markup": {"inline_keyboard": buttons + [[{"text": "View full catalog on Aarogya", "url": browse_url}]]},
    }


def aarogya_reply(chat_id, text):
    command = re.fullmatch(r"/?(?:buy\s+)?aarogya(?:(?:\s*:\s*|\s+)(.*))?", text, re.IGNORECASE)
    if not command:
        return None
    query = (command.group(1) or "").strip()
    catalog = re.fullmatch(r"all(?:\s+([1-9][0-9]{0,2}))?", query, re.IGNORECASE)
    if catalog or not query:
        return aarogya_catalog_reply(chat_id, int(catalog.group(1) or "1") if catalog else 1)
    quantity = 1
    amount = re.fullmatch(r"([1-9][0-9]?)\s+(.+)", query)
    if amount:
        quantity, query = int(amount.group(1)), amount.group(2)
    if not query or len(query) > 200:
        return {"chat_id": chat_id, "text": "Try “Aarogya all” to browse or “Aarogya: hair oil” to search."}

    params = {
        "q": query,
        "resources[type]": "product",
        "resources[limit]": 4,
        "resources[options][unavailable_products]": "hide",
    }
    search_url = f"{AAROGYA_ORIGIN}/search?{urlencode({'q': query, 'type': 'product'})}"
    try:
        request = Request(
            f"{AAROGYA_ORIGIN}/search/suggest.json?{urlencode(params)}",
            headers={"Accept": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            products = json.loads(response.read(262_144))["resources"]["results"]["products"]
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = workers.map(
                lambda product: aarogya_checkout_button(product, quantity),
                (product for product in products[:4] if isinstance(product, dict)),
            )
            buttons = [[button] for button in results if button is not None]
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        buttons = []

    return {
        "chat_id": chat_id,
        "text": (
            "Select a product to open Aarogya checkout. Each option uses the lowest-priced available size."
            if buttons else "I couldn’t find an available checkout option. You can check Aarogya’s search results."
        ),
        "reply_markup": {
            "inline_keyboard": buttons + [[{"text": "Search on Aarogya", "url": search_url}]]
        },
    }


def amazon_reply(chat_id, text):
    command = re.fullmatch(r"/?amazon(?:\s*:\s*|\s+)(.*)", text, re.IGNORECASE)
    query = command.group(1).strip() if command else text
    if query.lower().startswith("https://"):
        try:
            url = urlsplit(query)
        except ValueError:
            return None
        if url.netloc.lower() not in {"amazon.in", "www.amazon.in"}:
            return None
        asin = re.search(r"/(?:dp|gp/product)/([a-z0-9]{10})(?:/|$)", url.path, re.IGNORECASE)
        if not asin:
            return {"chat_id": chat_id, "text": "Paste an Amazon.in product link or try “Amazon: kids shampoo”."}
        destination = f"https://www.amazon.in/dp/{asin.group(1).upper()}"
        label = "Open product on Amazon India"
    elif command and query and len(query) <= 250:
        destination = f"https://www.amazon.in/s?{urlencode({'k': query})}"
        label = "View Amazon India search results"
    elif command or text.lower() in {"amazon", "/amazon"}:
        return {"chat_id": chat_id, "text": "Try “Amazon: kids shampoo” or paste an Amazon.in product link."}
    else:
        return None

    return {
        "chat_id": chat_id,
        "text": "Choose your item and complete checkout on Amazon India.",
        "reply_markup": {"inline_keyboard": [[{"text": label, "url": destination}]]},
    }



COURT_PORTALS = {
    "district": "https://services.ecourts.gov.in/ecourtindia_v6/",
    "high": "https://hcservices.ecourts.gov.in/hcservices/main.php",
}
CASE_METHODS = {
    "1": ("Case number", "Send the case type, registered number, and registration year."),
    "2": ("Filing number", "Send the filing number and filing year."),
    "3": ("Party name", "Send the full or partial party name and year, if known. Include Pending, Disposed, or Both."),
    "4": ("Advocate", "Send the advocate name or bar registration number. Include Pending, Disposed, or Both if known."),
    "5": ("FIR", "Send the police station, FIR number, and FIR year."),
    "6": ("Act", "Send the act and section, if known. Include Pending, Disposed, or Both."),
    "7": ("Case type", "Send the case type and registration year. Include Pending, Disposed, or Both."),
}
CASE_SESSIONS = {}
CASE_LOCK = threading.Lock()
CNR_PATTERN = re.compile(r"[A-Z]{4}[0-9]{12}", re.IGNORECASE)
CASE_FIELDS = {
    "CNR": r"CNR(?: Number)?",
    "Case number": r"(?:Case|Registration) Number",
    "Status": r"(?:Case Status|Current Status|Status)",
    "Stage": r"(?:Stage of Case|Case Stage|Stage)",
    "Next hearing": r"(?:Next Hearing Date|Next Hearing|Next Date)",
    "Disposal date": r"(?:Date of Disposal|Disposal Date)",
    "Court": r"(?:Court Name|Court Number and Judge|Court)",
}


def case_lookup(chat_id, court, method, reference, location=""):
    portal = COURT_PORTALS[court]
    if method == "CNR":
        instructions = "Open Search by CNR and enter the CNR below."
        button_label = "Open eCourts CNR search"
    else:
        if court == "district" and method == "Case number" and re.search(r"\branga\s*reddy\b", location, re.I):
            portal = "https://rangareddy.dcourts.gov.in/case-status-search-by-case-number/"
            instructions = "Choose your court complex or establishment and enter the case details below."
        else:
            instructions = f"Open Case Status, select your court, and choose {method}. Enter the details below."
        button_label = "Open eCourts Case Status"
    return {
        "chat_id": chat_id,
        "text": (
            f"{instructions}\n\n{reference}\n\nComplete the CAPTCHA on eCourts. "
            "If several records match, open the correct one. Paste the Case Status text here "
            "with the field labels so I can summarize it."
        ),
        "reply_markup": {"inline_keyboard": [[{"text": button_label, "url": portal}]]},
    }


def court_reply(chat_id, text):
    normalized = text.lower()
    start = re.fullmatch(r"(?:/case(?:status)?|(?:check\s+)?(?:ecourts\s+)?case\s+status)(?:[ :.,]+(.*))?", text, re.I)
    with CASE_LOCK:
        now = time.monotonic()
        session = CASE_SESSIONS.get(chat_id)
        if session and now > session["expires"]:
            CASE_SESSIONS.pop(chat_id, None)
            session = None

        def respond(message):
            return {"chat_id": chat_id, "text": message}

        if normalized in {"/cancelcase", "/cancel"} and session:
            CASE_SESSIONS.pop(chat_id)
            return respond("Case lookup cancelled.")
        if start:
            session = {"step": "start", "expires": now + 1800}
            CASE_SESSIONS[chat_id] = session
            text = (start.group(1) or "").strip()
            normalized = text.lower()
        if not session:
            return None

        step = session["step"]
        session["expires"] = now + 1800
        compact = re.sub(r"[\s-]", "", text)
        if step in {"start", "court"} and CNR_PATTERN.fullmatch(compact):
            session["step"] = "result"
            court = "high" if compact[2:4].upper() == "HC" else "district"
            return case_lookup(chat_id, court, "CNR", f"CNR: {compact.upper()}")

        if step in {"start", "court"}:
            if normalized in {"district", "district court", "taluka", "taluka court"}:
                session.update(step="location", court="district")
                return respond("Send the state, district, and court complex.")
            if normalized in {"high", "high court"}:
                session.update(step="location", court="high")
                return respond("Send the High Court and bench.")
            session["step"] = "court"
            return respond("Send the 16-character CNR if you have it. Otherwise reply District or High Court. /cancelcase ends the lookup.")

        if step == "location":
            if not text or len(text) > 250:
                return respond("Send the court location in a short message.")
            session.update(step="method", location=text)
            choices = "\n".join(f"{number}. {label}" for number, (label, _) in CASE_METHODS.items())
            return respond(f"What details do you have? Reply with a number.\n{choices}")

        if step == "method":
            if normalized not in CASE_METHODS:
                return respond("Reply 1 for case number, 2 for filing number, 3 for party name, 4 for advocate, 5 for FIR, 6 for act, or 7 for case type.")
            label, question = CASE_METHODS[normalized]
            session.update(step="details", method=label)
            return respond(question)

        if step == "details":
            if not text or len(text) > 1000:
                return respond("Send the lookup details in a short message.")
            reference = f"Court: {session['location']}\nDetails: {text}"
            if session["method"] == "Case number":
                match = re.fullmatch(r"([A-Za-z][A-Za-z. ]*?)[,\s]+(\d+)\s*[,/ -]\s*(\d{4})", text)
                if match:
                    kind, number, year = match.groups()
                    reference = (f"Court: {session['location']}\nCase type: {kind.upper()}\n"
                                 f"Case number: {number}\nRegistration year: {year}")
            session["step"] = "result"
            return case_lookup(chat_id, session["court"], session["method"], reference, session["location"])

        fields = []
        for label, pattern in CASE_FIELDS.items():
            match = re.search(rf"^[ \t]*(?:{pattern})[ \t]*(?::[ \t]*|\t+|\n[ \t]*)([^\r\n]+)", text, re.I | re.M)
            if match:
                fields.append(f"{label}: {match.group(1).strip()[:220]}")
        if not fields:
            return respond("Paste the eCourts result as text with its labels, such as Case Status, Stage of Case, or Next Hearing Date.")
        CASE_SESSIONS.pop(chat_id)
        return respond("From the eCourts result you pasted:\n" + "\n".join(fields) + "\n\nCheck the official record for any updates.")


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

    text = text.strip()
    aarogya = aarogya_reply(chat["id"], text)
    if aarogya is not None:
        return aarogya

    amazon = amazon_reply(chat["id"], text)
    if amazon is not None:
        return amazon

    purchase = BUY_REQUEST.fullmatch(text)
    if purchase:
        return checkout_reply(chat["id"], purchase.group(1))

    if text.lower() not in {*PRODUCTS, "ghee and honey", "both", "shop", "products"}:
        court = court_reply(chat["id"], text)
        if court is not None:
            return court

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
        result["text"] = 'Ask for ghee, honey, or shampoo. For checkout, try “buy shampoo” or “buy ghee and shampoo”. Try “Amazon: kids shampoo”, “Aarogya all”, or “case status”.'
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
