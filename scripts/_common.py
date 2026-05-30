#!/usr/bin/env python3
"""
uk-pay-hub/scripts/_common.py
Shared utilities for United Kingdom Pay Hub search scripts.
UK voluntary salary disclosure · EU Pay Transparency Directive 2023/970/EU effective June 2026 — UK employers proactively disclosing pay ranges
"""

import atexit
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

socket.setdefaulttimeout(20)

EXA_API_KEY   = os.environ.get("EXA_API_KEY", "d0d9614a-58d8-4166-9b27-4ae6b6e2761e")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "BSAodGE-EMqeQg5P6m4SW2pFXfrD06r")

_exa_exhausted = False
OLLAMA_API  = "http://127.0.0.1:11434/api/generate"
MODEL       = "qwen2.5:14b"
TODAY       = date.today().isoformat()
SHARED_DIR  = os.path.expanduser("~/.openclaw/shared")
OUTPUT_FILE = os.path.join(SHARED_DIR, f"uk-jobs-raw-{TODAY}.txt")
DATA_FILE   = os.path.expanduser("~/uk-pay-hub/data/jobs.json")
CURRENCY    = "GBP"
REGION      = "UK"

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

SKIP_PATTERNS = [
    "glassdoor.com/Salary", "payscale.com", "salary.com",
    "indeed.com/salary", "ziprecruiter.com/Salaries",
    "linkedin.com/jobs/search", "linkedin.com/jobs/?",
    "monster.com/jobs/search", "simplyhired.com/search",
    "myworkdayjobs.com",
    "newswire.com", "businesswire.com", "prnewswire.com",
    "/press-release", "/newsroom/", "/investor-relations",
    "/annual-report", "/media-advisory",
    "talent.com/salary", "levels.fyi", "finance.yahoo.com",
]

_JOB_PAGE_MARKERS = [
    "apply", "qualifications", "requirements", "responsibilities",
    "salary range", "compensation range", "salary:", "we are looking",
    "job description", "about the role", "what you will do",
    "what we offer", "about you", "your responsibilities",
    "minimum qualifications", "preferred qualifications",
]

def is_job_page(text: str) -> bool:
    if not text or len(text) < 300:
        return False
    t = text.lower()
    return sum(1 for m in _JOB_PAGE_MARKERS if m in t) >= 2

REGION_TERMS = ['london', 'united kingdom', 'england', 'scotland', 'wales', 'manchester', 'edinburgh', 'birmingham', 'bristol', 'leeds', 'glasgow', 'cardiff', 'liverpool', 'sheffield', 'nottingham', 'cambridge', 'oxford', 'reading', 'brighton', ', uk,', ', uk ', 'remote (uk)', 'remote uk']

_NON_REGION_LOC_TERMS = ['portland, or', 'eugene, or', 'las vegas, nv', 'reno, nv', 'honolulu, hi', 'hawaii', 'washington, dc', 'washington dc', 'seattle, wa', 'bellevue, wa', 'denver, co', 'boulder, co', 'chicago, il', 'new york city', 'new york, ny', 'san francisco, ca', 'los angeles, ca', 'boston, ma', 'minneapolis, mn', 'toronto', 'vancouver', 'montreal', 'calgary', 'dublin, ireland', 'amsterdam', 'berlin', 'paris']


def make_logger(log_file):
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    def log(msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_file, "a") as f:
            f.write(line + "\n")
    return log


def acquire_lock(lock_file, log):
    if os.path.exists(lock_file):
        try:
            with open(lock_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            log(f"Another instance is already running (PID {old_pid}). Exiting.")
            return False
        except (OSError, ValueError):
            log("Stale lock file — removing.")
            os.remove(lock_file)
    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))
    def _release():
        try: os.remove(lock_file)
        except OSError: pass
    atexit.register(_release)
    signal.signal(signal.SIGTERM, lambda s, f: (_release(), sys.exit(1)))
    return True


def exa_search(query, num_results=10, start_date=None, log=None):
    global _exa_exhausted
    if _exa_exhausted:
        return None
    payload = {"query": query, "numResults": num_results, "type": "auto",
                "contents": {"text": {"maxCharacters": 2000}}}
    if start_date:
        payload["startPublishedDate"] = start_date
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=data,
        headers={"Content-Type": "application/json", "x-api-key": EXA_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 402:
            _exa_exhausted = True
            if log: log("Exa quota exhausted — switching to Brave.")
        return None
    except Exception:
        return None


def brave_search(query, num_results=10, log=None):
    params = urllib.parse.urlencode({"q": query, "count": num_results})
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        if log: log(f"Brave error: {e}")
        return None


def collect_candidates(queries, num=10, start_date=None, log=None):
    results = []
    for q in queries:
        resp = exa_search(q, num_results=num, start_date=start_date, log=log)
        if resp is None:
            resp = brave_search(q, num_results=num, log=log)
            if resp:
                items = resp.get("web", {}).get("results", [])
                results.extend(items)
        else:
            results.extend(resp.get("results", []))
    return results


def fetch_page_text(url, log=None):
    for skip in SKIP_PATTERNS:
        if skip in url:
            return ""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(200_000).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", raw)
    except Exception:
        return ""


def load_existing_keys():
    try:
        with open(DATA_FILE) as f:
            db = json.load(f)
        return {
            f"{j['role'].lower().strip()}|{j['company'].lower().strip()}"
            for j in db.get("jobs", [])
        }
    except Exception:
        return set()


def load_existing_urls():
    try:
        with open(DATA_FILE) as f:
            db = json.load(f)
        return {j.get("source_url", "") for j in db.get("jobs", []) if j.get("source_url")}
    except Exception:
        return set()

def write_job(output_file, job):
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    job["currency"] = CURRENCY
    job["region"] = REGION
    with open(output_file, "a") as f:
        f.write(json.dumps(job) + "\n")


from pathlib import Path
