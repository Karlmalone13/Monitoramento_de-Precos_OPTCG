import re
import os
import logging
import urllib.request
import urllib.parse
import json
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

CARDTRADER_TOKEN = os.environ.get("CARDTRADER_TOKEN", "")
CT_BASE = "https://api.cardtrader.com/api/v2"
OP_GAME_ID = 15

logger = logging.getLogger(__name__)

# Cache de imagens por card_name
_image_cache = {}


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    clean = re.sub(r"[^\d.,]", "", str(text).strip())
    if re.search(r"\d\.\d{3}", clean):
        clean = clean.replace(".", "").replace(",", ".")
    else:
        clean = clean.replace(",", ".")
    try:
        return round(float(clean), 2)
    except ValueError:
        return None


def _ct_get(path: str):
    if not CARDTRADER_TOKEN:
        return None
    url = f"{CT_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {CARDTRADER_TOKEN}",
            "Content-Type": "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, dict) and "array" in data:
                return data["array"]
            return data
    except Exception as e:
        logger.error(f"[CardTrader API] {path}: {e}")
        return None


def _pw_get_html(url: str, wait_selector: str = None, timeout: int = 25000) -> str:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    import time
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)
        page = ctx.new_page()
        try:
            page.goto(url, timeout=timeout, wait_until="networkidle")
        except PWTimeout:
            pass
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=10000)
            except PWTimeout:
                pass
        time.sleep(2)
        html = page.content()
        browser.close()
    return html


def _fetch_image_optcgapi(card_code: str) -> Optional[str]:
    """Busca imagem na optcgapi.com pelo código da carta (ex: OP01-001)."""
    try:
        url = f"https://optcgapi.com/api/cards/{card_code.upper()}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            # Tenta vários campos de imagem
            for field in ["image", "image_url", "img", "art"]:
                if data.get(field):
                    return data[field]
            # Se retornou lista
            if isinstance(data, list) and data:
                for field in ["image", "image_url", "img", "art"]:
                    if data[0].get(field):
                        return data[0][field]
    except Exception as e:
        logger.debug(f"[optcgapi] {card_code}: {e}")
    return None


def _fetch_image_apitcg(card_code: str) -> Optional[str]:
    """Busca imagem na apitcg.com pelo código da carta."""
    try:
        url = f"https://www.apitcg.com/api/one-piece/cards?id={card_code.upper()}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, list) and data:
                for field in ["image", "image_url", "img", "thumbnail"]:
                    if data[0].get(field):
                        return data[0][field]
            elif isinstance(data, dict):
                for field in ["image", "image_url", "img", "thumbnail"]:
                    if data.get(field):
                        return data[field]
    except Exception as e:
        logger.debug(f"[apitcg] {card_code}: {e}")
    return None


def _ct_search_blueprint(card_name: str) -> tuple[Optional[int], Optional[str]]:
    """
    Retorna (blueprint_id, image_url) para a carta buscada.
    """
    if not CARDTRADER_TOKEN:
        return None, None

    expansions = _ct_get("/expansions")
    if not expansions:
        return None, None

    op_expansions = [e for e in expansions if e.get("game_id") == OP_GAME_ID]
    if not op_expansions:
        return None, None

    code_match = re.match(r"([A-Za-z]{2}\d{2})-(\d{3})", card_name.strip())
    is_code = bool(code_match)
    search_code = card_name.strip().upper() if is_code else None
    exp_prefix = code_match.group(1).lower() if is_code else None

    def normalize(s):
        return re.sub(r"[\s\.]+", " ", s.lower().strip())

    name_norm = normalize(card_name)

    for exp in op_expansions:
        exp_name = exp.get("name", "")
        if exp_prefix and exp.get("code", "").lower() != exp_prefix:
            continue

        blueprints = _ct_get(f"/blueprints/export?expansion_id={exp['id']}")
        if not blueprints:
            continue

        for bp in blueprints:
            matched = False
            if is_code:
                collector = bp.get("fixed_properties", {}).get("collector_number", "")
                if collector.upper() == search_code:
                    matched = True
            else:
                bp_norm = normalize(bp.get("name", ""))
                if name_norm and (name_norm in bp_norm or bp_norm in name_norm):
                    matched = True

            if matched:
                bp_id = bp["id"]
                # Tenta pegar imagem do CardTrader
                image_url = bp.get("image_url") or ""
                if image_url and not image_url.startswith("http"):
                    image_url = "https://cardtrader.com" + image_url
                logger.info(f"[CardTrader] Encontrado: {bp['name']} (id={bp_id}) img={bool(image_url)}")
                card_display_name = bp.get("name", "").replace(".", " ").strip()
                return bp_id, image_url or None, card_display_name, exp_name

    return None, None, None, None


def get_card_image(card_name: str) -> Optional[str]:
    """
    Tenta obter a URL da imagem da carta.
    Ordem: CardTrader → optcgapi → apitcg
    """
    if card_name in _image_cache:
        return _image_cache[card_name]

    image_url = None

    # 1. CardTrader (já obtido no search, mas podemos chamar separado se necessário)
    code_match = re.match(r"([A-Za-z]{2}\d{2}-\d{3})", card_name.strip())
    card_code = code_match.group(1) if code_match else None

    # 2. optcgapi
    if not image_url and card_code:
        image_url = _fetch_image_optcgapi(card_code)

    # 3. apitcg
    if not image_url and card_code:
        image_url = _fetch_image_apitcg(card_code)

    if image_url:
        _image_cache[card_name] = image_url

    return image_url


def scrape_cardtrader(card_name: str) -> dict:
    search_url = f"https://www.cardtrader.com/en/search?q={urllib.parse.quote_plus(card_name)}&game_id={OP_GAME_ID}"
    result = {"price": None, "currency": "EUR", "url": search_url, "error": None, "image_url": None}

    if not CARDTRADER_TOKEN:
        result["error"] = "Token CardTrader não configurado"
        return result

    blueprint_id, image_url, display_name, expansion_name = _ct_search_blueprint(card_name)
    result["display_name"] = display_name
    result["expansion_name"] = expansion_name
    if not blueprint_id:
        result["error"] = f"Blueprint não encontrado para '{card_name}'"
        return result

    result["url"] = f"https://www.cardtrader.com/en/cards/{blueprint_id}"
    result["image_url"] = image_url
    result["display_name"] = display_name

    # Fallback para outras APIs se CardTrader não tiver imagem
    if not image_url:
        result["image_url"] = get_card_image(card_name)

    products_data = _ct_get(f"/marketplace/products?blueprint_id={blueprint_id}&language=en")
    if not products_data:
        result["error"] = "Sem produtos no marketplace"
        return result

    products = []
    if isinstance(products_data, dict):
        for val in products_data.values():
            if isinstance(val, list):
                products.extend(val)
    elif isinstance(products_data, list):
        products = products_data

    available = [
        p for p in products
        if not p.get("on_vacation") and p.get("price", {}).get("cents")
    ]
    if not available:
        result["error"] = "Sem produtos disponíveis"
        return result

    cheapest = min(available, key=lambda p: p["price"]["cents"])
    result["price"] = round(cheapest["price"]["cents"] / 100, 2)
    result["currency"] = cheapest["price"].get("currency", "EUR")

    logger.info(f"[CardTrader API] {card_name}: {result['price']} {result['currency']}")
    return result


def scrape_cardmarket(card_name: str) -> dict:
    from bs4 import BeautifulSoup
    query = urllib.parse.quote_plus(card_name)
    url = (
        f"https://www.cardmarket.com/en/OnePiece/Products/Search"
        f"?searchString={query}&sortBy=price_asc&minCondition=3"
    )
    result = {"price": None, "currency": "EUR", "url": url, "error": None}

    try:
        html = _pw_get_html(url, timeout=30000)
        if "Just a moment" in html or "challenge-platform" in html:
            result["error"] = "Bloqueado por Cloudflare"
            return result

        soup = BeautifulSoup(html, "lxml")
        price_candidates = []

        for sel in [".price-container span", ".col-offer .fw-bold",
                    "[class*='price'] span", ".article-row .price", "dd.col-offer"]:
            for el in soup.select(sel):
                p = _parse_price(el.get_text(strip=True))
                if p and 0.01 < p < 5000:
                    price_candidates.append(p)

        for el in soup.find_all(string=re.compile(r"€\s*\d")):
            p = _parse_price(el)
            if p and 0.01 < p < 5000:
                price_candidates.append(p)

        if price_candidates:
            result["price"] = min(price_candidates)
        else:
            result["error"] = "Preço não encontrado"

    except Exception as e:
        result["error"] = str(e)

    return result


def scrape_liga(card_name: str) -> dict:
    from bs4 import BeautifulSoup
    query = urllib.parse.quote_plus(card_name)
    url = f"https://www.ligaonepiece.com.br/?view=cards/cards&busca={query}"
    result = {"price": None, "currency": "BRL", "url": url, "error": None}

    try:
        html = _pw_get_html(url, timeout=30000)
        if "Just a moment" in html or "challenge-platform" in html:
            result["error"] = "Bloqueado por Cloudflare"
            return result

        soup = BeautifulSoup(html, "lxml")
        price_candidates = []

        for sel in [".preco", ".price", ".valor", "[class*='preco']",
                    "[class*='price']", "[class*='valor']", ".produto-preco", ".card-price"]:
            for el in soup.select(sel):
                p = _parse_price(el.get_text(strip=True))
                if p and 0.5 < p < 10000:
                    price_candidates.append(p)

        for el in soup.find_all(string=re.compile(r"R\$\s*\d")):
            p = _parse_price(el)
            if p and 0.5 < p < 10000:
                price_candidates.append(p)

        if price_candidates:
            result["price"] = min(price_candidates)
        else:
            result["error"] = "Preço não encontrado"

    except Exception as e:
        result["error"] = str(e)

    return result


def scrape_all(card_name: str) -> dict:
    logger.info(f"Scraping prices for: {card_name}")
    return {
        "liga":       scrape_liga(card_name),
        "cardmarket": scrape_cardmarket(card_name),
        "cardtrader": scrape_cardtrader(card_name),
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    name = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "OP01-001"
    results = scrape_all(name)
    print(json.dumps(results, indent=2, ensure_ascii=False))