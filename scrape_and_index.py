"""
scrape_and_index.py
-------------------
One-time script to scrape chicago.gov/city/en/depts.html and all department
pages one level deep, chunk the text, embed with BGE, and store in a FAISS
index + metadata JSON.

Run once:  python scrape_and_index.py
Re-run whenever you want to refresh data from the site.
"""

import os
import multiprocessing
import json
import time
import socket
import requests
import numpy as np
import voyageai
import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()


def _ipv4_connect_params(dsn: str) -> dict:
    """Parse DSN and inject hostaddr (IPv4) so psycopg2 never tries IPv6."""
    params = psycopg2.extensions.parse_dsn(dsn)
    hostname = params.get("host", "")
    if hostname:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
            params["hostaddr"] = infos[0][4][0]
            print(f"Resolved {hostname} → {params['hostaddr']} (IPv4)", flush=True)
        except Exception as e:
            print(f"IPv4 resolution failed for {hostname}: {e}", flush=True)
    return params


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL       = "https://www.chicago.gov"
DEPTS_URL      = "https://www.chicago.gov/city/en/depts.html"
VECTORS_DIR          = Path("vectors")
PAGES_CACHE          = VECTORS_DIR / "scraped_pages.json"

# URL slug fragments that identify site-wide boilerplate pages present on every page
# (footer links, legal notices, etc.).  Filtering these avoids indexing the same
# policy/legal/navigation content dozens of times across the index.
_BOILERPLATE_SLUGS = frozenset([
    "privacy", "terms-of-use", "terms-of-service", "terms-and-conditions",
    "accessibility", "sitemap", "copyright", "disclaimer",
    "feedback", "subscribe", "newsletter",
    "cookie", "legal-notice", "contact-us",
    "advertising", "sponsorship",
])

# Additional city-related sites to scrape (one level deep from each seed URL)
EXTRA_SOURCES = [
    {
        "name"     : "Chicago Public Schools",
        "base_url" : "https://www.cps.edu",
        "seed_url" : "https://www.cps.edu",
        "level1"   : "Education",
        # Keep only same-domain paths; skip anchors, media files, login pages, and boilerplate
        "link_filter": lambda href: (
            href.startswith("/")
            and not any(x in href for x in ["#", "javascript:", ".pdf", ".doc",
                                             ".xls", ".ppt", "login", "logout",
                                             "account", "calendar/event"])
            and not any(slug in href.lower() for slug in _BOILERPLATE_SLUGS)
        ),
    },
    {
        "name"     : "Chicago Park District",
        "base_url" : "https://www.chicagoparkdistrict.com",
        "seed_url" : "https://www.chicagoparkdistrict.com",
        "level1"   : "Parks & Recreation",
        "link_filter": lambda href: (
            href.startswith("/")
            and not any(x in href for x in ["#", "javascript:", ".pdf", ".doc",
                                             ".xls", ".ppt", "login", "logout",
                                             "account", "/events/",
                                             "capital-improvement", "careers"])
            and not any(slug in href.lower() for slug in _BOILERPLATE_SLUGS)
        ),
    },
    # ── Illinois state sources ────────────────────────────────────────────────
    {
        "name"         : "Illinois State Government",
        "base_url"     : "https://www.illinois.gov",
        "seed_url"     : "https://www.illinois.gov/services",
        "level1"       : "Illinois State Services",
        "source_scope" : "state_il",
        "link_filter": lambda href: (
            href.startswith("/")
            and not any(x in href for x in ["#", "javascript:", ".pdf", ".doc",
                                             ".xls", ".ppt", "login", "logout"])
            and not any(slug in href.lower() for slug in _BOILERPLATE_SLUGS)
        ),
    },
    {
        "name"         : "IDHS Illinois Dept of Human Services",
        "base_url"     : "https://www.dhs.state.il.us",
        "seed_url"     : "https://www.dhs.state.il.us/page.aspx",
        "level1"       : "Illinois State Services",
        "source_scope" : "state_il",
        "link_filter": lambda href: (
            href.startswith("/")
            and not any(x in href for x in ["#", "javascript:", ".pdf", "login", "logout"])
            and not any(slug in href.lower() for slug in _BOILERPLATE_SLUGS)
        ),
    },
    {
        "name"         : "IDES Illinois Dept of Employment Security",
        "base_url"     : "https://ides.illinois.gov",
        "seed_url"     : "https://ides.illinois.gov",
        "level1"       : "Illinois State Services",
        "source_scope" : "state_il",
        "link_filter": lambda href: (
            href.startswith("/")
            and not any(x in href for x in ["#", "javascript:", ".pdf", "login", "logout"])
            and not any(slug in href.lower() for slug in _BOILERPLATE_SLUGS)
        ),
    },
]

# Max pages to collect per extra source (keeps runtime reasonable)
EXTRA_SOURCE_MAX_PAGES   = 60
CHICAGO_L2_MAX_PER_DEPT  = 10   # max sub-pages to scrape per chicago.gov department
EXTRA_L2_MAX_TOTAL       = 60   # max additional level-2 pages per extra source
EMBEDDINGS_CKPT      = Path("vectors/embeddings_checkpoint.npz")
MODEL_NAME           = "voyage-multilingual-2"
MAX_CHARS            = 2000   # target chunk size (chars; ~500 tokens)
OVERLAP_CHARS        = 300    # overlap between consecutive chunks
REQUEST_DELAY        = 0.5    # seconds between HTTP requests (be polite)
EMBED_BATCH_SIZE     = 64     # Voyage supports up to 128 inputs per request
HEADERS        = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

def get_dept_links(url: str) -> list[str]:
    """Return all unique department page URLs found on the main depts page."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = []
    seen  = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Keep only department sub-pages (not the index page itself or boilerplate)
        if (
            "/depts/" in href
            and href not in ("/city/en/depts.html", "/content/city/en/depts.html")
            and not any(slug in href.lower() for slug in _BOILERPLATE_SLUGS)
        ):
            full = BASE_URL + href if href.startswith("/") else href
            if full not in seen:
                seen.add(full)
                links.append(full)
    return links


# Markers that signal the start of boilerplate present on every chicago.gov page
# (contact modal + "I Want To" sidebar). Everything from the first match onward
# is site-wide chrome with no page-specific content.
_CHICAGO_BOILERPLATE_MARKERS = [
    "\nContact\n×\n* Your email address:",
    "\n* Your email address:",
    "\nI Want To\nApply For\n",
]


def strip_chicago_boilerplate(text: str) -> str:
    """Remove the contact-form / 'I Want To' sidebar that appears on every chicago.gov page."""
    for marker in _CHICAGO_BOILERPLATE_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            return text[:idx].strip()
    return text


def scrape_page(url: str) -> dict | None:
    """
    Fetch a single page and return a dict with title, url, and cleaned text.
    Returns None on any error so the pipeline can continue.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Strip boilerplate HTML tags
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        title_tag = soup.find("title")
        title     = title_tag.get_text(strip=True) if title_tag else url

        # Prefer the <main> element; fall back to <body>
        body = soup.find("main") or soup.find("div", {"id": "page-content"}) or soup.body
        text = body.get_text(separator="\n", strip=True) if body else ""

        # Remove site-wide boilerplate text that adds no page-specific signal
        if "chicago.gov" in url:
            text = strip_chicago_boilerplate(text)

        return {"title": title, "url": url, "text": text}

    except Exception as exc:
        print(f"  [WARN] Could not scrape {url}: {exc}")
        return None


def get_chicago_sublinks(dept_url: str) -> list[str]:
    """
    Fetch a chicago.gov department page and return links to its sub-pages (level 2).
    Sub-pages share a path prefix with the department URL, e.g.:
      dept_url  → https://www.chicago.gov/city/en/depts/cpd.html
      sub-pages → https://www.chicago.gov/city/en/depts/cpd/supp_info/...
    """
    try:
        resp = requests.get(dept_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        print(f"  [WARN] Could not fetch sub-links from {dept_url}: {exc}")
        return []

    # Build dept path prefix: strip BASE_URL and trailing ".html"
    # e.g. /city/en/depts/cpd.html → /city/en/depts/cpd
    dept_path = dept_url.replace(BASE_URL, "")
    if dept_path.endswith(".html"):
        dept_path = dept_path[:-5]

    seen  = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/")
        if (
            href.startswith(dept_path + "/")
            and not any(slug in href.lower() for slug in _BOILERPLATE_SLUGS)
        ):
            full = BASE_URL + href
            if full not in seen:
                seen.add(full)
                links.append(full)
    return links


def get_external_links(seed_url: str, base_url: str, link_filter, max_pages: int) -> list[str]:
    """
    Fetch `seed_url`, collect href links that pass `link_filter`, and return
    up to `max_pages` unique absolute URLs (seed URL is always included first).
    """
    try:
        resp = requests.get(seed_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        print(f"  [WARN] Could not fetch seed {seed_url}: {exc}")
        return []

    seen  = {seed_url}
    links = [seed_url]  # always scrape the home page itself
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/")  # strip query strings
        if link_filter(href):
            full = base_url + href
            if full not in seen:
                seen.add(full)
                links.append(full)
                if len(links) >= max_pages:
                    break
    return links


# ---------------------------------------------------------------------------
# Level classification
# ---------------------------------------------------------------------------

# URL slug → Level 1 top-level category
LEVEL1_MAP = {
    "cpd"        : "Public Safety",
    "police"     : "Public Safety",
    "cfd"        : "Public Safety",
    "fire"       : "Public Safety",
    "copa"       : "Public Safety",
    "oemc"       : "Public Safety",
    "igchicago"  : "Public Safety",
    "bacp"       : "Business & Licensing",
    "zoning"     : "Business & Licensing",
    "bldgs"      : "Housing & Buildings",
    "doh"        : "Housing & Buildings",
    "landmarks"  : "Housing & Buildings",
    "cdph"       : "Health & Human Services",
    "dfs"        : "Health & Human Services",
    "aging"      : "Health & Human Services",
    "dhs"        : "Health & Human Services",
    "cdot"       : "Transportation & Infrastructure",
    "aviation"   : "Transportation & Infrastructure",
    "water"      : "Transportation & Infrastructure",
    "fleet"      : "Transportation & Infrastructure",
    "sewers"     : "Transportation & Infrastructure",
    "finance"    : "Finance & Administration",
    "budget"     : "Finance & Administration",
    "revenue"    : "Finance & Administration",
    "treasurer"  : "Finance & Administration",
    "dps"        : "Finance & Administration",
    "dca"        : "Culture, Arts & Recreation",
    "cpl"        : "Culture, Arts & Recreation",
    "mayor"      : "City Government",
    "cityclerk"  : "City Government",
    "law"        : "City Government",
    "cofa"       : "City Government",
    "ah"         : "City Government",
    "hr"         : "City Government",
    "311"        : "City Services",
}

# Keyword sets → Level 3 content type
LEVEL3_KEYWORDS = {
    "how_to"  : ["how to", "apply", "application", "permit", "license",
                 "register", "registration", "payment", "pay", "submit",
                 "request", "steps", "process", "fee", "fees", "requirements"],
    "contact" : ["contact", "phone", "call", "email", "address",
                 "location", "hours", "fax", "tty", "reach", "visit us"],
    "programs": ["program", "service", "initiative", "resource",
                 "benefit", "grant", "funding", "assistance", "support"],
    "overview": ["mission", "vision", "about", "department", "office",
                 "bureau", "division", "role", "responsibility", "overview"],
}


def classify_level1(url: str) -> str:
    """Infer Level 1 category from URL slug or domain."""
    url_lower = url.lower()
    # Domain-level classification for external sites
    if "cps.edu" in url_lower:
        return "Education"
    if "chicagoparkdistrict.com" in url_lower:
        return "Parks & Recreation"
    if any(d in url_lower for d in ["illinois.gov", "dhs.state.il.us", "ides.illinois.gov"]):
        return "Illinois State Services"
    # Slug-based classification for chicago.gov
    for slug, category in LEVEL1_MAP.items():
        if f"/{slug}" in url_lower or f"/{slug}." in url_lower:
            return category
    return "City Services"


def classify_level3(text: str) -> str:
    """Infer Level 3 content type from chunk text keywords."""
    text_lower = text.lower()
    scores = {label: sum(1 for kw in kws if kw in text_lower)
              for label, kws in LEVEL3_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "overview"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """
    Sliding-window character chunker.
    Tries to break on newlines to avoid splitting mid-sentence.
    Returns a list of non-empty chunk strings.
    """
    chunks = []
    start  = 0
    length = len(text)

    while start < length:
        end = min(start + max_chars, length)

        # If not at the end, try to break on the last newline in the window
        if end < length:
            break_at = text.rfind("\n", start + max_chars // 2, end)
            if break_at != -1:
                end = break_at

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        next_start = end - overlap
        start = next_start if next_start > start else end  # always advance

    return chunks


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    VECTORS_DIR.mkdir(exist_ok=True)

    print(f"=== Chicago.gov Department Scraper ===")
    print(f"Target      : {DEPTS_URL}\n")

    # 1 & 2. Scrape pages (or load from cache)
    if PAGES_CACHE.exists():
        print("Step 1/4 — Loading scraped pages from cache...")
        with open(PAGES_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        pages       = cache["pages"]
        scrape_date = cache["scrape_date"]
        print(f"  Loaded {len(pages)} pages (scraped {scrape_date})\n")
    else:
        scrape_date = datetime.now().strftime("%Y-%m-%d")
        print(f"Scrape date : {scrape_date}\n")

        print("Step 1/4 — Discovering department links...")
        dept_links = get_dept_links(DEPTS_URL)
        print(f"  Found {len(dept_links)} department pages\n")

        print("Step 2/4 — Scraping pages...")
        pages = []

        def save_cache():
            with open(PAGES_CACHE, "w", encoding="utf-8") as f:
                json.dump({"scrape_date": scrape_date, "pages": pages}, f, ensure_ascii=False)

        main_page = scrape_page(DEPTS_URL)
        if main_page:
            pages.append(main_page)
            save_cache()

        scraped_urls = {p["url"] for p in pages}
        for i, url in enumerate(dept_links, 1):
            print(f"  [{i}/{len(dept_links)}] {url}")
            if url not in scraped_urls:
                page = scrape_page(url)
                if page:
                    pages.append(page)
                    scraped_urls.add(url)
                    save_cache()
            time.sleep(REQUEST_DELAY)

            # Level 2: scrape sub-pages found on this department page
            sublinks = get_chicago_sublinks(url)
            time.sleep(REQUEST_DELAY)
            sub_scraped = 0
            for suburl in sublinks:
                if sub_scraped >= CHICAGO_L2_MAX_PER_DEPT:
                    break
                if suburl in scraped_urls:
                    continue
                print(f"    [L2] {suburl}")
                subpage = scrape_page(suburl)
                if subpage:
                    pages.append(subpage)
                    scraped_urls.add(suburl)
                    save_cache()
                sub_scraped += 1
                time.sleep(REQUEST_DELAY)

        print(f"\n  chicago.gov: {len(pages)} pages scraped — cache saved to {PAGES_CACHE}\n")

    # Always-run: scrape any extra source not yet represented in the cache.
    # This lets you add new sources without re-scraping chicago.gov.
    def save_cache():
        with open(PAGES_CACHE, "w", encoding="utf-8") as f:
            json.dump({"scrape_date": scrape_date, "pages": pages}, f, ensure_ascii=False)

    cached_domains = {p["url"].split("/")[2] for p in pages}  # e.g. {"www.chicago.gov"}
    for source in EXTRA_SOURCES:
        source_domain = source["base_url"].split("/")[2]  # e.g. "www.cps.edu"
        if source_domain in cached_domains:
            print(f"  Skipping {source['name']} — already in cache\n")
            continue

        print(f"Step 2/4 — Scraping {source['name']} ({source['seed_url']}) — level 1...")
        ext_links = get_external_links(
            source["seed_url"], source["base_url"],
            source["link_filter"], EXTRA_SOURCE_MAX_PAGES,
        )
        print(f"  Discovered {len(ext_links)} level-1 links")
        l1_scraped = set()
        for j, url in enumerate(ext_links, 1):
            print(f"  [{j}/{len(ext_links)}] {url}")
            page = scrape_page(url)
            if page:
                pages.append(page)
                save_cache()
            l1_scraped.add(url)
            time.sleep(REQUEST_DELAY)

        # Level 2: discover and scrape sub-pages found on each level-1 page
        print(f"  Scraping {source['name']} — level 2...")
        seen_l2  = set(ext_links)
        l2_count = 0
        for l1_url in list(l1_scraped):
            if l2_count >= EXTRA_L2_MAX_TOTAL:
                break
            l2_links = get_external_links(
                l1_url, source["base_url"], source["link_filter"], EXTRA_L2_MAX_TOTAL + 1,
            )
            time.sleep(REQUEST_DELAY)
            for url in l2_links:
                if url in seen_l2 or l2_count >= EXTRA_L2_MAX_TOTAL:
                    continue
                seen_l2.add(url)
                print(f"  [L2 {l2_count + 1}/{EXTRA_L2_MAX_TOTAL}] {url}")
                page = scrape_page(url)
                if page:
                    pages.append(page)
                    save_cache()
                l2_count += 1
                time.sleep(REQUEST_DELAY)
        print(f"  {source['name']}: done\n")

    print(f"  Total pages in index: {len(pages)}\n")

    # 3. Chunk all page text
    print("Step 3/4 — Chunking text...")
    all_chunks = []
    # Build a map from domain → source_scope for extra sources
    _domain_scope = {
        src["base_url"].split("/")[2]: src.get("source_scope", "city")
        for src in EXTRA_SOURCES
    }
    for page in pages:
        chunks = chunk_text(page["text"])
        level1 = classify_level1(page["url"])
        level2 = page["title"].split(" | ")[0].strip()   # e.g. "Chicago Police Department"
        domain = page["url"].split("/")[2]
        source_scope = _domain_scope.get(domain, "city")
        for i, chunk in enumerate(chunks):
            all_chunks.append({
                "id"           : f"{page['url']}__chunk_{i}",
                "url"          : page["url"],
                "title"        : page["title"],
                "text"         : chunk,
                "chunk_index"  : i,
                "level1"       : level1,
                "level2"       : level2,
                "level3"       : classify_level3(chunk),
                "source_scope" : source_scope,
            })

    print(f"  Created {len(all_chunks)} chunks from {len(pages)} pages\n")

    # 4. Embed + build FAISS index
    print(f"Step 4/4 — Embedding with {MODEL_NAME} via Voyage AI API...")
    voyage_client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])

    texts = [c["text"] for c in all_chunks]
    print(f"  Embedding {len(texts)} chunks in batches of {EMBED_BATCH_SIZE}...")
    all_embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        result = voyage_client.embed(batch, model=MODEL_NAME, input_type="document")
        all_embeddings.extend(result.embeddings)
        print(f"  [{min(i + EMBED_BATCH_SIZE, len(texts))}/{len(texts)}] batches embedded")

    embeddings = np.array(all_embeddings, dtype=np.float32)
    # Normalize to unit vectors so IndexFlatIP == cosine similarity
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    embeddings /= np.maximum(norms, 1e-12)
    print()

    # Clear checkpoint on clean finish
    if EMBEDDINGS_CKPT.exists():
        EMBEDDINGS_CKPT.unlink()
        print("  Checkpoint cleared.\n")

    # 5. Write to Supabase
    print("Step 5/5 — Writing to Supabase...")
    conn = psycopg2.connect(**_ipv4_connect_params(os.environ["DATABASE_URL"]))
    register_vector(conn)
    cur = conn.cursor()

    cur.execute("DELETE FROM scrape_info")
    cur.execute("DELETE FROM chunks")

    cur.execute(
        "INSERT INTO scrape_info (scrape_date, model, total_pages, total_chunks) VALUES (%s,%s,%s,%s)",
        (scrape_date, MODEL_NAME, len(pages), len(all_chunks)),
    )

    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO chunks (id, url, title, text, chunk_index, level1, level2, level3, source_scope, embedding) "
        "VALUES %s",
        [
            (c["id"], c["url"], c["title"], c["text"],
             c["chunk_index"], c["level1"], c["level2"], c["level3"],
             c.get("source_scope", "city"), embeddings[i].tolist())
            for i, c in enumerate(all_chunks)
        ],
        page_size=100,
    )

    conn.commit()
    cur.close()
    conn.close()
    del embeddings

    print(f"\n=== Done ===")
    print(f"  {len(all_chunks)} chunks written to Supabase")
    print(f"  Scrape date : {scrape_date}")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)
    main()
