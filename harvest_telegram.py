#!/usr/bin/env python3
"""
Telegram greenlight harvester (skill 2, part 1).

Reads YOUR replies to the scout bot and moves the items you approved into
context/approved_queue.json, which the drafter reads.

How you reply (all of these work):
    yes 3, 5          |  3 5           |  ok 3,5
    si 2              |  no 4          |  skip 4
    all               |  none

It matches numbers against context/last_digest_index.json, which the scout writes
when it delivers a digest (number -> candidate record).

IMPORTANT: Telegram's getUpdates only retains messages for ~24h, so this must run
on its OWN frequent schedule (a daily routine), not with the monthly sweep.
It stores the last processed update_id in context/tg_offset.json so replies are
never double-counted.
"""
from __future__ import annotations
import io, re, json, os, sys, urllib.parse, urllib.request, datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
CTX = ROOT / "context"
CTX.mkdir(exist_ok=True)
QUEUE = CTX / "approved_queue.json"
INDEX = CTX / "last_digest_index.json"
OFFSET = CTX / "tg_offset.json"

# The scout (a SEPARATE repo) publishes candidate data. We try several artifacts
# in order, because context/last_digest_index.json doesn't always reach main
# (a run whose branch touched code isn't auto-merged). output/latest_candidates.json
# is committed reliably and carries full records, so it's the preferred source.
SCOUT_REPO = os.environ.get("SCOUT_REPO", "adonaigglobo-etho/opportunity-scout")
SCOUT_BRANCH = os.environ.get("SCOUT_BRANCH", "main")
# Files to try, in order (candidates list preferred - full records).
SCOUT_PATHS = ["output/latest_candidates.json", "context/last_digest_index.json"]

def _gh_get_json(path):
    """Fetch a file from the (possibly PRIVATE) scout repo via the GitHub API,
    authenticated with GH_TOKEN. Returns parsed JSON or raises."""
    import base64, urllib.request
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    url = (f"https://api.github.com/repos/{SCOUT_REPO}/contents/"
           f"{urllib.parse.quote(path)}?ref={SCOUT_BRANCH}")
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "opportunity-scout-harvester"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        meta = json.loads(r.read().decode("utf-8", "replace"))
    content = base64.b64decode(meta.get("content", "")).decode("utf-8", "replace")
    return json.loads(content)

def _fetch_remote_index():
    """Try each scout artifact via the authenticated API; normalize to {num: record}."""
    for path in SCOUT_PATHS:
        try:
            data = _gh_get_json(path)
        except Exception as e:
            print(f"  [scout fetch] {path}: {e}", file=sys.stderr)
            continue
        if isinstance(data, dict) and data:
            return {str(k): v for k, v in data.items()}
        if isinstance(data, list) and data:
            return {str(i): rec for i, rec in enumerate(data, 1)}
    return {}

import unicodedata
def _deaccent(x):
    return "".join(c for c in unicodedata.normalize("NFD", (x or "").lower())
                   if unicodedata.category(c) != "Mn")

APPROVE = {"yes", "si", "sí", "ok", "okay", "vale", "draft", "go", "approve", "y"}
REJECT = {"no", "skip", "nope", "n"}

def _creds():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("SCOUT_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("SCOUT_CHAT_ID")
    return token, chat

def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def tg_send(text):
    token, chat = _creds()
    if not (token and chat):
        return False
    payload = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload), timeout=20)
        return True
    except Exception as e:
        print(f"  [telegram] {e}", file=sys.stderr)
        return False

def load_json(p, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(p, data):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def parse_reply(text):
    """Return (approved_numbers, rejected_numbers, name_tokens, special)."""
    raw = (text or "").strip()
    t = raw.lower()
    if not t:
        return set(), set(), [], None
    if t in ("all", "todos", "todas"):
        return set(), set(), [], "all"
    if t in ("none", "ninguno", "ninguna"):
        return set(), set(), [], "none"

    nums = set(int(n) for n in re.findall(r"\b(\d{1,3})\b", t))

    # polarity from leading word; default approve
    first = re.split(r"[\s,]+", t)[0]
    reject = first in REJECT

    # name tokens: words that aren't numbers, polarity words, or punctuation-only.
    # Also ignore anything in parentheses is KEPT (people write "Yes 2 (Lars)").
    words = re.findall(r"[a-zA-Zà-úÀ-Ú]{3,}", raw)
    stop = APPROVE | REJECT | {"the", "and", "por", "para", "con"}
    names = [w for w in words if w.lower() not in stop]

    if reject:
        return set(), nums, [], None
    return nums, set(), names, None

def harvest():
    token, chat = _creds()
    if not (token and chat):
        print("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return 0

    index = load_json(INDEX, {})          # {"1": {...candidate...}, "2": {...}}
    if not index:
        # fall back to the scout repo's published artifacts (candidates preferred)
        index = _fetch_remote_index()
        if index:
            print(f"Fetched candidate index from scout repo ({len(index)} items).")
            save_json(INDEX, index)  # cache locally
    if not index:
        print("No digest index found (local or remote) - nothing to match against.")
        return 0

    off = load_json(OFFSET, {}).get("offset", 0)
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    if off:
        url += f"?offset={off + 1}"
    try:
        data = _get_json(url)
    except Exception as e:
        print(f"getUpdates failed: {e}", file=sys.stderr)
        return 0

    updates = data.get("result", [])
    if not updates:
        print("No new Telegram messages.")
        return 0

    queue = load_json(QUEUE, [])
    have = {c.get("id") for c in queue}
    approved_nums, rejected_nums = set(), set()
    approved_names = []
    special = None
    max_id = off

    for u in updates:
        max_id = max(max_id, u.get("update_id", 0))
        msg = u.get("message") or u.get("edited_message") or {}
        # only accept replies from YOUR chat
        if str(msg.get("chat", {}).get("id")) != str(chat):
            continue
        a, r, names, sp = parse_reply(msg.get("text", ""))
        approved_nums |= a
        rejected_nums |= r
        approved_names.extend(names)
        if sp:
            special = sp

    if special == "all":
        approved_nums = set(int(k) for k in index.keys())
    elif special == "none":
        approved_nums = set()

    approved_nums -= rejected_nums

    # Resolve name tokens (e.g. "Lars", "Chittka") to index numbers by matching
    # against each candidate's title. This is the unambiguous path and is checked
    # against the SAME item the number labelled.
    for tok in approved_names:
        tl = _deaccent(tok)
        for num, rec in index.items():
            if tl in _deaccent(rec.get("title", "")):
                approved_nums.add(int(num))

    added, missing = 0, []
    for n in sorted(approved_nums):
        rec = index.get(str(n))
        if not rec:
            missing.append(n)
            continue
        if rec.get("id") in have:
            continue
        rec = dict(rec)
        rec["approved_at"] = dt.date.today().isoformat()
        queue.append(rec)
        have.add(rec.get("id"))
        added += 1

    save_json(QUEUE, queue)
    save_json(OFFSET, {"offset": max_id})

    if added:
        names = ", ".join(str(n) for n in sorted(approved_nums) if str(n) in index)
        tg_send(f"Queued {added} item(s) for drafting: #{names}. "
                f"The drafter will prepare them on its next run.")
    if missing:
        tg_send(f"Couldn't match these numbers to the last digest: {missing}. "
                f"Reply with numbers from the most recent digest.")

    print(f"harvest: {added} approved, {len(rejected_nums)} rejected, "
          f"{len(missing)} unmatched. Queue size: {len(queue)}")
    return added

if __name__ == "__main__":
    harvest()
