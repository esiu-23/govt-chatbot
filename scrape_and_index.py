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
import requests
import faiss
import numpy as np
import torch
import torch.nn.functional as F
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from sentence_transformers import SentenceTransformer

# Prevent tokenizers from spawning child processes (causes segfault on macOS)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# Prevent PyTorch thread-pool semaphore leaks on macOS (Intel)
torch.set_num_threads(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL       = "https://www.chicago.gov"
DEPTS_URL      = "https://www.chicago.gov/city/en/depts.html"
VECTORS_DIR          = Path("vectors")
PAGES_CACHE          = VECTORS_DIR / "scraped_pages.json"

# Additional city-related sites to scrape (one level deep from each seed URL)
EXTRA_SOURCES = [
    {
        "name"     : "Chicago Public Schools",
        "base_url" : "https://www.cps.edu",
        "seed_url" : "https://www.cps.edu",
        "level1"   : "Education",
        # Keep only same-domain paths; skip anchors, media files, and login pages
        "link_filter": lambda href: (
            href.startswith("/")
            and not any(x in href for x in ["#", "javascript:", ".pdf", ".doc",
                                             ".xls", ".ppt", "login", "logout",
                                             "account", "calendar/event"])
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
                                             "privacy", "advertising", "sponsorship",
                                             "capital-improvement", "careers",
                                             "accessibility", "sitemap"])
        ),
    },
]

# Max pages to collect per extra source (keeps runtime reasonable)
EXTRA_SOURCE_MAX_PAGES = 60
EMBEDDINGS_CKPT      = Path("vectors/embeddings_checkpoint.npz")
MODEL_NAME           = "intfloat/multilingual-e5-small"
BGE_PASSAGE_PREFIX   = "passage: "
MAX_CHARS            = 2000   # target chunk size (chars; ~500 tokens)
OVERLAP_CHARS        = 300    # overlap between consecutive chunks
REQUEST_DELAY        = 0.5    # seconds between HTTP requests (be polite)
EMBED_BATCH_SIZE     = 16
CHECKPOINT_EVERY     = 5      # save checkpoint every N batches (~80 chunks)
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
        # Keep only department sub-pages (not the index page itself)
        if "/depts/" in href and href not in ("/city/en/depts.html", "/content/city/en/depts.html"):
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

        for i, url in enumerate(dept_links, 1):
            print(f"  [{i}/{len(dept_links)}] {url}")
            page = scrape_page(url)
            if page:
                pages.append(page)
                save_cache()
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

        print(f"Step 2/4 — Scraping {source['name']} ({source['seed_url']})...")
        ext_links = get_external_links(
            source["seed_url"], source["base_url"],
            source["link_filter"], EXTRA_SOURCE_MAX_PAGES,
        )
        print(f"  Discovered {len(ext_links)} links")
        for j, url in enumerate(ext_links, 1):
            print(f"  [{j}/{len(ext_links)}] {url}")
            page = scrape_page(url)
            if page:
                pages.append(page)
                save_cache()
            time.sleep(REQUEST_DELAY)
        print(f"  {source['name']}: done\n")

    print(f"  Total pages in index: {len(pages)}\n")

    # 3. Chunk all page text
    print("Step 3/4 — Chunking text...")
    all_chunks = []
    for page in pages:
        chunks = chunk_text(page["text"])
        level1 = classify_level1(page["url"])
        level2 = page["title"].split(" | ")[0].strip()   # e.g. "Chicago Police Department"
        for i, chunk in enumerate(chunks):
            all_chunks.append({
                "id"          : f"{page['url']}__chunk_{i}",
                "url"         : page["url"],
                "title"       : page["title"],
                "text"        : chunk,
                "chunk_index" : i,
                "level1"      : level1,
                "level2"      : level2,
                "level3"      : classify_level3(chunk),
            })

    print(f"  Created {len(all_chunks)} chunks from {len(pages)} pages\n")

    # 4. Embed + build FAISS index
    print(f"Step 4/4 — Embedding with {MODEL_NAME}...")
    model = SentenceTransformer(MODEL_NAME, device="cpu")

    # Extract tokenizer + base transformer so we never touch model.encode()
    # (which creates a DataLoader each call and leaks semaphores on macOS)
    _tokenizer  = model.tokenizer
    _base_model = model[0].auto_model
    _base_model.eval()

    def _embed_batch(batch_texts: list[str]) -> np.ndarray:
        """Tokenize and embed directly — no DataLoader, no semaphores."""
        encoded = _tokenizer(
            batch_texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        with torch.no_grad():
            out = _base_model(**encoded)
        mask       = encoded["attention_mask"].unsqueeze(-1).float()
        pooled     = (out.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        normalized = F.normalize(pooled, p=2, dim=1)
        return normalized.cpu().numpy()

    texts       = [BGE_PASSAGE_PREFIX + c["text"] for c in all_chunks]
    start_idx   = 0
    done_embeds = []

    # Resume from checkpoint if one exists and chunk count matches
    if EMBEDDINGS_CKPT.exists():
        ckpt = np.load(EMBEDDINGS_CKPT)
        if int(ckpt["total_chunks"]) == len(texts):
            done_embeds = list(ckpt["embeddings"])
            start_idx   = len(done_embeds)
            print(f"  Resuming from chunk {start_idx}/{len(texts)} (checkpoint found)\n")
        else:
            print("  Stale checkpoint (chunk count changed) — starting fresh\n")
            EMBEDDINGS_CKPT.unlink()

    total_batches = (len(texts) - start_idx + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
    for batch_num, batch_start in enumerate(
        range(start_idx, len(texts), EMBED_BATCH_SIZE), start=1
    ):
        batch_end    = min(batch_start + EMBED_BATCH_SIZE, len(texts))
        batch_embeds = _embed_batch(texts[batch_start:batch_end])
        done_embeds.extend(batch_embeds)

        print(f"  Batch {batch_num}/{total_batches} — chunks {batch_end}/{len(texts)}", end="\r")

        if batch_num % CHECKPOINT_EVERY == 0:
            np.savez(
                EMBEDDINGS_CKPT,
                embeddings   = np.array(done_embeds, dtype=np.float32),
                total_chunks = np.array(len(texts)),
            )
            print(f"\n  Checkpoint saved at chunk {batch_end}")

    print()  # newline after progress line
    embeddings = np.array(done_embeds, dtype=np.float32)

    # Clear checkpoint on clean finish
    if EMBEDDINGS_CKPT.exists():
        EMBEDDINGS_CKPT.unlink()
        print("  Checkpoint cleared.\n")

    dim   = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)   # IndexFlatIP on normalised vectors = cosine similarity
    index.add(embeddings.astype(np.float32))

    # Persist
    faiss.write_index(index, str(VECTORS_DIR / "index.faiss"))

    metadata = {
        "scrape_date"  : scrape_date,
        "model"        : MODEL_NAME,
        "total_pages"  : len(pages),
        "total_chunks" : len(all_chunks),
        "chunks"       : all_chunks,
    }
    with open(VECTORS_DIR / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    print(f"\n=== Done ===")
    print(f"  {len(all_chunks)} chunks indexed")
    print(f"  FAISS index  → {VECTORS_DIR}/index.faiss")
    print(f"  Metadata     → {VECTORS_DIR}/metadata.json")
    print(f"  Scrape date  : {scrape_date}")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)
    main()
