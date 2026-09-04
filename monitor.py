#!/usr/bin/env python3
"""Porto District house-for-sale monitor. Scans 5 portals, dedupes against
listings_db.json, emails new qualifying listings via Resend. Never fabricates:
every fact reported comes from content actually fetched this run.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright

DB_PATH = "listings_db.json"
STATUS_PATH = "status.json"

PRICE_MIN = 50000
PRICE_MAX = 180000

PORTO_MUNICIPALITIES = [
    "amarante", "baião", "baiao", "felgueiras", "gondomar", "lousada", "maia",
    "marco de canaveses", "matosinhos", "paços de ferreira", "pacos de ferreira",
    "paredes", "penafiel", "porto", "póvoa de varzim", "povoa de varzim",
    "santo tirso", "trofa", "valongo", "vila do conde", "vila nova de gaia",
]

EXCLUSION_KEYWORDS = [
    "arrendada", "arrendado", "com contrato de arrendamento",
    "arrendamento em vigor", "ocupado por inquilino", "ocupada por inquilinos",
    "vendida com inquilino", "vendido com inquilino", "inquilino no imóvel",
    "inquilino no imovel", "com arrendatário", "com arrendatario",
]

INACTIVE_KEYWORDS = [
    "imóvel vendido", "imovel vendido", "casa vendida", "moradia vendida",
    "já vendido", "ja vendido", "já vendida", "ja vendida",
    "imóvel reservado", "imovel reservado", "casa reservada", "moradia reservada",
    "já reservado", "ja reservado", "já reservada", "ja reservada",
    "estado: vendido", "estado: reservado", "negócio concluído", "negocio concluido",
    "imóvel indisponível", "imovel indisponivel", "já não disponível",
    "ja nao disponivel", "anúncio removido", "anuncio removido",
    "página não encontrada", "pagina nao encontrada",
]

RESTORATION_KEYWORDS = [
    "restauro", "restaurar", "reabilitação",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "jorge_hernani@msn.com")
FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_url(url):
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def contains_any(text, keywords):
    t = text.lower()
    return any(k in t for k in keywords)


def find_municipality(text):
    t = text.lower()
    for m in PORTO_MUNICIPALITIES:
        if m in t:
            return m
    return None


def extract_price_eur(text):
    candidates = []
    normalized = text.replace("\xa0", " ").replace(" ", " ")
    for m in re.finditer(r"(\d{1,3}(?:[.\s]\d{3})+|\d{4,7})\s*€", normalized):
        raw = m.group(1).replace(".", "").replace(" ", "")
        try:
            val = int(raw)
        except ValueError:
            continue
        if 1000 <= val <= 10_000_000:
            candidates.append(val)
    for m in re.finditer(r"€\s*(\d{1,3}(?:[.,\s]\d{3})+|\d{4,7})", normalized):
        raw = m.group(1).replace(".", "").replace(",", "").replace(" ", "")
        try:
            val = int(raw)
        except ValueError:
            continue
        if 1000 <= val <= 10_000_000:
            candidates.append(val)
    if not candidates:
        return None
    from collections import Counter
    return Counter(candidates).most_common(1)[0][0]


def scan_imovirtual(status):
    candidates = []
    try:
        for page_num in (1, 2):
            url = (
                "https://www.imovirtual.com/pt/resultados/comprar/moradia/porto"
                f"?priceMin={PRICE_MIN}&priceMax={PRICE_MAX}&page={page_num}"
            )
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            if r.status_code != 200:
                status["imovirtual"] = f"error: HTTP {r.status_code}"
                break
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if not m:
                status["imovirtual"] = "error: no __NEXT_DATA__ found"
                break
            data = json.loads(m.group(1))
            items = data["props"]["pageProps"]["data"]["searchAds"]["items"]
            if not items:
                break
            for it in items:
                if it.get("estate") != "HOUSE":
                    continue
                price = (it.get("totalPrice") or {}).get("value")
                if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                    continue
                href = it.get("href", "").replace("[lang]", "pt", 1)
                href = href.lstrip("/").replace("/ad/", "/anuncio/", 1)
                full_url = "https://www.imovirtual.com/" + href
                loc = it.get("location", {})
                council = None
                for entry in (loc.get("reverseGeocoding", {}) or {}).get("locations", []):
                    if entry.get("locationLevel") == "council":
                        council = entry.get("name")
                candidates.append({
                    "url": full_url,
                    "title": it.get("title", ""),
                    "price": price,
                    "source": "imovirtual",
                    "municipality_hint": council,
                    "short_text": it.get("shortDescription", "") or "",
                })
        if "imovirtual" not in status:
            status["imovirtual"] = "ok" if candidates else "empty"
    except Exception as e:
        status["imovirtual"] = f"error: {e}"
    return candidates


def _scroll_container(page, selector, max_rounds=25, pause_ms=500):
    try:
        page.wait_for_selector(selector, timeout=8000)
    except Exception:
        return
    prev = -1
    for _ in range(max_rounds):
        try:
            h = page.eval_on_selector(selector, "el => el.scrollHeight")
            page.eval_on_selector(selector, "el => el.scrollTo(0, el.scrollHeight)")
        except Exception:
            break
        page.wait_for_timeout(pause_ms)
        if h == prev:
            break
        prev = h


def scan_remax(browser, status):
    candidates = []
    try:
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1000})
        page.goto("https://www.remax.pt/comprar/moradia/porto", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        _scroll_container(page, "div.overflow-y-auto.custom-scrollbar")
        cards = page.query_selector_all('a[data-id="listing-card-link"]')
        for card in cards:
            href = card.get_attribute("href") or ""
            if not href:
                continue
            full_url = "https://www.remax.pt" + href if href.startswith("/") else href
            text = card.inner_text()
            price = extract_price_eur(text)
            if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                continue
            title = text.split("\n")[0] if text else ""
            candidates.append({
                "url": full_url, "title": title, "price": price,
                "source": "remax", "municipality_hint": None, "short_text": text,
            })
        page.close()
        status["remax"] = "ok" if candidates else "empty"
    except Exception as e:
        status["remax"] = f"error: {e}"
    return candidates


def _cards_by_link_ancestor(page, link_selector, card_css="div.card"):
    out = []
    anchors = page.query_selector_all(link_selector)
    seen_hrefs = set()
    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        card = a.evaluate_handle(
            f"el => el.closest({card_css!r}) || el.parentElement && el.parentElement.parentElement"
        )
        el = card.as_element()
        text = el.inner_text() if el else (a.inner_text() or "")
        out.append((href, text))
    return out


def scan_era(browser, status):
    candidates = []
    try:
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1000})
        page.goto("https://www.era.pt/comprar/moradias/porto", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        for _ in range(10):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(500)
        pairs = _cards_by_link_ancestor(page, 'a[href*="/imovel/"]', "div.card")
        page.close()
        for href, text in pairs:
            full_url = href if href.startswith("http") else "https://www.era.pt" + href
            if "moradia" not in full_url.lower() and "moradia" not in text.lower():
                continue
            price = extract_price_eur(text)
            if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                continue
            title = text.split("\n")[0] if text else ""
            candidates.append({
                "url": full_url, "title": title, "price": price,
                "source": "era", "municipality_hint": None, "short_text": text,
            })
        status["era"] = "ok" if candidates else "empty"
    except Exception as e:
        status["era"] = f"error: {e}"
    return candidates


def scan_century21(browser, status):
    candidates = []
    try:
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1000})
        page.goto(
            "https://www.century21.pt/comprar?addresses=13&address_names=Porto+-+Distrito",
            wait_until="networkidle", timeout=30000,
        )
        page.wait_for_timeout(2000)
        _scroll_container(page, "div.overflow-y-auto.w-full.flex.flex-col", max_rounds=30, pause_ms=400)
        pairs = _cards_by_link_ancestor(page, 'a[href^="/comprar/"]', "[class*=card]")
        page.close()
        for href, text in pairs:
            if not re.match(r'^/comprar/[A-Za-z]\d{4}-\d+$', href):
                continue
            if "moradia" not in text.lower():
                continue
            full_url = "https://www.century21.pt" + href
            price = extract_price_eur(text)
            if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                continue
            lines = [l for l in text.split("\n") if l.strip()]
            title = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
            candidates.append({
                "url": full_url, "title": title, "price": price,
                "source": "century21", "municipality_hint": None, "short_text": text,
            })
        status["century21"] = "ok" if candidates else "empty"
    except Exception as e:
        status["century21"] = f"error: {e}"
    return candidates


def scan_idealista(browser, status):
    candidates = []
    try:
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1000})
        resp = page.goto(
            f"https://www.idealista.pt/comprar-casas/porto-distrito/"
            f"com-preco-max_{PRICE_MAX},preco-min_{PRICE_MIN},moradias/",
            wait_until="domcontentloaded", timeout=20000,
        )
        if resp is not None and resp.status == 403:
            status["idealista"] = "blocked"
            page.close()
            return candidates
        page.wait_for_timeout(1500)
        pairs = _cards_by_link_ancestor(page, 'a[href*="/imovel/"]', "article")
        page.close()
        for href, text in pairs:
            full_url = href if href.startswith("http") else "https://www.idealista.pt" + href
            price = extract_price_eur(text)
            if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
                continue
            title = text.split("\n")[2] if len(text.split("\n")) > 2 else (text.split("\n")[0] if text else "")
            candidates.append({
                "url": full_url, "title": title, "price": price, "source": "idealista",
                "municipality_hint": None, "short_text": text,
            })
        status["idealista"] = "ok" if candidates else "empty"
    except Exception as e:
        status["idealista"] = f"error: {e}"
    return candidates


def get_full_text(browser, url):
    page = browser.new_page(user_agent=UA)
    try:
        resp = page.goto(url, wait_until="networkidle", timeout=25000)
        if resp is None or resp.status >= 400:
            return None, None
        page.wait_for_timeout(800)
        text = page.inner_text("body")
        h1 = page.query_selector("h1")
        heading = h1.inner_text().strip() if h1 else page.title()
        return text, heading
    except Exception:
        return None, None
    finally:
        page.close()


def verify_candidate(browser, candidate):
    text, heading = get_full_text(browser, candidate["url"])
    if text is None:
        return False, "detail page unreachable", None, None

    if contains_any(text, INACTIVE_KEYWORDS):
        return False, "listing inactive/sold/reserved", None, None
    if contains_any(text, EXCLUSION_KEYWORDS):
        return False, "listing is rented/tenant-occupied", None, None
    if contains_any(text, RESTORATION_KEYWORDS):
        return False, "listing needs restoration (restauro/restaurar/reabilitação)", None, None

    muni = find_municipality(text)
    if not muni:
        return False, "no Porto District municipality mentioned on page", None, None

    price = extract_price_eur(text) or candidate.get("price")
    if price is None or not (PRICE_MIN <= price <= PRICE_MAX):
        return False, f"price {price} outside range", None, None

    title = heading or candidate.get("title") or (text.split("\n")[0] if text else "")
    return True, "ok", price, title.strip()[:200]


def send_email(listing, status):
    if not RESEND_API_KEY:
        status.setdefault("failed_emails", []).append({"url": listing["url"], "error": "no API key configured"})
        return False
    subject = f"New Porto listing: {listing['title']} — €{listing['price']:,}".replace(",", " ")
    body = (
        f"{listing['title']}\n"
        f"Price: €{listing['price']:,}\n".replace(",", " ") +
        f"Municipality: {listing.get('municipality', 'Porto District')}\n"
        f"Source: {listing['source']}\n"
        f"Link: {listing['url']}\n"
        f"Detected: {listing['first_detected']}\n"
    )
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": NOTIFY_EMAIL, "subject": subject, "text": body},
            timeout=20,
        )
        if r.status_code >= 300:
            status.setdefault("failed_emails", []).append({"url": listing["url"], "error": f"HTTP {r.status_code}: {r.text[:200]}"})
            return False
        return True
    except Exception as e:
        status.setdefault("failed_emails", []).append({"url": listing["url"], "error": str(e)})
        return False


def git_commit_and_push(message):
    for path in (DB_PATH, STATUS_PATH):
        if os.path.exists(path):
            subprocess.run(["git", "add", path])
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return True
    commit = subprocess.run(["git", "-c", "user.name=porto-house-monitor",
                              "-c", "user.email=actions@users.noreply.github.com",
                              "commit", "-m", message])
    if commit.returncode != 0:
        return False
    push = subprocess.run(["git", "push"])
    return push.returncode == 0


def main():
    db = load_json(DB_PATH, {})
    status = load_json(STATUS_PATH, {"bootstrapped": False, "last_run_utc": None, "last_run_portal_status": {}})
    is_backfill = not status.get("bootstrapped", False)
    save_json(DB_PATH, db)
    save_json(STATUS_PATH, status)

    portal_status = {}
    all_candidates = []

    all_candidates += scan_imovirtual(portal_status)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        all_candidates += scan_remax(browser, portal_status)
        all_candidates += scan_era(browser, portal_status)
        all_candidates += scan_century21(browser, portal_status)
        all_candidates += scan_idealista(browser, portal_status)

        new_count = 0
        checked = 0
        for cand in all_candidates:
            key = normalize_url(cand["url"])
            if key in db:
                continue
            checked += 1
            if checked > 60:
                break
            qualifies, reason, price, title = verify_candidate(browser, cand)
            if not qualifies:
                continue

            muni = find_municipality(cand.get("short_text", "") + " " + (title or "")) or "Porto District"
            entry = {
                "url": cand["url"],
                "listing_id": None,
                "title": title or cand.get("title") or "",
                "price": price,
                "publication_date": None,
                "source": cand["source"],
                "first_detected": now_utc_iso(),
            }
            db[key] = entry
            save_json(DB_PATH, db)
            git_commit_and_push(f"Add listing: {entry['title'][:60]}")

            if not is_backfill:
                listing_for_email = dict(entry)
                listing_for_email["municipality"] = muni
                if send_email(listing_for_email, portal_status):
                    new_count += 1

        browser.close()

    status["last_run_utc"] = now_utc_iso()
    status["last_run_portal_status"] = portal_status
    if is_backfill:
        status["bootstrapped"] = True
        status["last_run_note"] = f"Backfill run: {len(db)} listings recorded, 0 emails sent (by design)."
    else:
        status["last_run_note"] = f"{new_count} new listing(s) emailed this run."
    save_json(STATUS_PATH, status)
    git_commit_and_push("Update run status" + (" (backfill complete)" if is_backfill else ""))

    print(f"Done. backfill={is_backfill} total_db={len(db)} portal_status={portal_status}")


if __name__ == "__main__":
    main()
