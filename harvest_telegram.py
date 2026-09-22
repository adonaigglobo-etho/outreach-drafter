#!/usr/bin/env python3
"""
Telegram greenlight harvester (skill 2, part 1).

Reads YOUR replies to the scout bot and moves the items you approved into
context/approved_queue.json, which the drafter reads.

How you reply (sweep code REQUIRED when more than one sweep is live):
    yes 0922a: 1, 3      |  0922a 1 3         |  ok 0922a 3,5
    no 0922a: 4          |  0922a all         |  0922a none
Names still work as a fallback, scoped to the sweep:  yes 0922a: Per Jensen

Each digest the scout delivers carries a short SWEEP CODE (MMDD + a letter, e.g.
0922a, 0922b) shown in its header and footer. The scout stores a per-sweep index
in context/digest_registry.json, so number 3 of sweep 0922a and number 3 of sweep
0922b never collide. This harvester matches each reply against the sweep whose
code it names. If numbers arrive with NO code while several sweeps are live, it
does NOT guess - it asks you for the code.

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
INDEX = CTX / "last_digest_index.json"        # legacy single-index cache (fallback)
REGISTRY_CACHE = CTX / "digest_registry.json"  # per-sweep index cache
OFFSET = CTX / "tg_offset.json"

# The scout (a SEPARATE repo) publishes candidate data. We prefer the per-sweep
# REGISTRY (context/digest_registry.json) so numbers resolve against the exact sweep
# the user named. We fall back to the single last_digest_index.json (older scouts),
# and finally to output/latest_candidates.json (pre-Chair list; last resort).
SCOUT_REPO = os.environ.get("SCOUT_REPO", "adonaigglobo-etho/opportunity-scout")
SCOUT_BRANCH = os.environ.get("SCOUT_BRANCH", "main")
REGISTRY_PATH = "context/digest_registry.json"
INDEX_PATHS = ["context/last_digest_index.json", "output/latest_candidates.json"]

def _gh_get_json(path):
    """Fetch a file from the (possibly PRIVATE) scout repo via the GitHub API,
    authenticated with GH_TOKEN. Returns parsed JSON or raises."""
    import base64
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

def _wrap_single(index):
    """Wrap a flat {num: rec} index as a one-entry registry so the rest of the
    code has a single shape to work with."""
    return {"_latest": {"code": "_latest", "date": None,
                        "items": {str(k): v for k, v in index.items()}}}

def _fetch_remote_registry():
    """Return the per-sweep registry {code: {items:{num:rec}, date}} from the scout
    repo, falling back to a single wrapped index if no registry is published yet."""
    try:
        data = _gh_get_json(REGISTRY_PATH)
        if isinstance(data, dict) and data:
            # normalize items keys to str
            reg = {}
            for code, sweep in data.items():
                items = sweep.get("items") if isinstance(sweep, dict) else None
                if not items:
                    continue
                reg[code] = {"code": code, "date": (sweep.get("date") if isinstance(sweep, dict) else None),
                             "created": (sweep.get("created") if isinstance(sweep, dict) else None),
                             "items": {str(k): v for k, v in items.items()}}
            if reg:
                return reg
    except Exception as e:
        print(f"  [scout fetch] {REGISTRY_PATH}: {e}", file=sys.stderr)
    for path in INDEX_PATHS:
        try:
            data = _gh_get_json(path)
        except Exception as e:
            print(f"  [scout fetch] {path}: {e}", file=sys.stderr)
            continue
        if isinstance(data, dict) and data:
            return _wrap_single(data)
        if isinstance(data, list) and data:
            return _wrap_single({i: rec for i, rec in enumerate(data, 1)})
    return {}

import unicodedata
def _deaccent(x):
    return "".join(c for c in unicodedata.normalize("NFD", (x or "").lower())
                   if unicodedata.category(c) != "Mn")

# Words too common across opportunity titles to identify anything on their own.
GENERIC = {_deaccent(w) for w in (
    "grant grants fellowship fellowships award awards scholarship scholarships "
    "research society university universidad institute institut program programme "
    "fund funding travel travelling traveling doctoral postdoctoral predoctoral "
    "exchange scientific support career smaller actions action network networks "
    "the of and for de la el los las en with center centre national international "
    "regional junior senior early student students".split())}

# Words that appear in the *reply wrapper* ("from the first sweep of ...") but never
# name an item - drop name segments made up only of these so they don't spam
# "couldn't match by name".
NAME_STOP = {"sweep", "sweeps", "first", "second", "third", "fourth", "fifth",
             "from", "of", "the", "to", "for", "yes", "no", "ok", "si", "please",
             "greenlight", "greenlights", "item", "items", "number", "numbers",
             "all", "none", "todos", "todas", "ninguno", "ninguna"}

def _title_words(title):
    return set(w for w in re.findall(r"[a-z0-9]+", _deaccent(title)) if len(w) >= 2)

def match_segment(seg, index):
    """Match one reply segment (e.g. 'Per Jensen') to an index number within one
    sweep. Returns (num, ambiguous_list); num is None if unmatched or ambiguous."""
    seg_words = [w for w in re.findall(r"[a-z0-9]+", _deaccent(seg)) if len(w) >= 3]
    if not seg_words:
        return None, []
    distinctive = [w for w in seg_words if w not in GENERIC]
    scored = {}
    for num, rec in index.items():
        tw = _title_words(rec.get("title", ""))
        d = sum(1 for w in distinctive if w in tw)
        if d:
            g = sum(1 for w in seg_words if w in GENERIC and w in tw)
            scored[num] = (d, g)
    if not scored:
        return None, []
    best = max(scored.values())
    winners = [n for n, s in scored.items() if s == best]
    if len(winners) == 1:
        return int(winners[0]), []
    return None, winners

APPROVE = {"yes", "si", "sí", "ok", "okay", "vale", "draft", "go", "approve", "y"}
REJECT = {"no", "skip", "nope", "n"}
CODE_RE = re.compile(r"\b(\d{4}[a-z])\b")

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

def find_code(text):
    m = CODE_RE.search((text or "").lower())
    return m.group(1) if m else None

def extract_numbers(text):
    """Item numbers only. Strips sweep codes, ISO dates and bare 4-digit years first
    so '2026-09-22' can't inject 9 or 22, and caps at 2 digits (digests are small)."""
    t = (text or "").lower()
    t = re.sub(r"\b\d{4}[a-z]\b", " ", t)          # sweep codes (0922a)
    t = re.sub(r"\b\d{4}-\d{1,2}-\d{1,2}\b", " ", t)  # ISO dates
    t = re.sub(r"\b\d{4}\b", " ", t)               # bare years
    return set(int(n) for n in re.findall(r"\b(\d{1,2})\b", t))

def parse_message(text):
    """Parse ONE reply. Returns (code, approve_nums, reject_nums, name_segments, special)."""
    raw = (text or "").strip()
    if not raw:
        return None, set(), set(), [], None
    code = find_code(raw)
    body = re.sub(r"\b\d{4}[a-z]\b", " ", raw)     # remove the code token for the rest

    special = None
    tl = re.sub(r"[^a-zà-ú ]", " ", body.lower()).strip()
    tl_words = tl.split()
    if tl_words and tl_words[-1] in ("all", "todos", "todas"):
        special = "all"
    elif tl_words and tl_words[-1] in ("none", "ninguno", "ninguna"):
        special = "none"

    nums = extract_numbers(raw)

    first = re.split(r"[\s,:]+", body.strip())[0].lower() if body.strip() else ""
    reject = first in REJECT

    m = re.match(r"^\s*([A-Za-zà-úÀ-Ú]+)", body)
    if m and m.group(1).lower() in (APPROVE | REJECT):
        body = body[m.end():]
    names = []
    for s in re.split(r"[,;\n:]| y | and ", body):
        s = s.strip(" .:-")
        if not re.search(r"[A-Za-zà-úÀ-Ú]{3,}", s):
            continue
        if s.lower() in (APPROVE | REJECT):
            continue
        words = [w for w in re.findall(r"[a-zà-ú]+", s.lower()) if len(w) >= 2]
        if words and all(w in NAME_STOP for w in words):
            continue  # pure wrapper phrase like "first sweep of"
        names.append(s)

    if reject:
        return code, set(), nums, [], special
    return code, nums, set(), names, special

def _latest_code(registry):
    best, bestkey = None, None
    for c, v in registry.items():
        key = v.get("created") or v.get("date") or c
        if bestkey is None or key > bestkey:
            best, bestkey = c, key
    return best

def _live_sweeps_hint(registry):
    bits = []
    for c in sorted(registry):
        first = registry[c]["items"].get("1", {})
        bits.append(f"{c} ({registry[c].get('date') or '?'}: {first.get('title','?')})")
    return "; ".join(bits)

def harvest():
    token, chat = _creds()
    if not (token and chat):
        print("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return 0

    registry = _fetch_remote_registry()
    if registry:
        save_json(REGISTRY_CACHE, registry)
        print(f"Refreshed digest registry from scout repo ({len(registry)} sweep(s): "
              f"{', '.join(sorted(registry))}).")
    else:
        registry = load_json(REGISTRY_CACHE, {})
        if registry:
            print(f"Scout fetch failed; using cached registry ({len(registry)} sweep(s)).")
    if not registry:
        print("No digest registry/index found (remote or cache).")
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
    max_id = off
    msgs = []
    for u in updates:
        max_id = max(max_id, u.get("update_id", 0))
        msg = u.get("message") or u.get("edited_message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat):
            continue
        code, a, r, names, sp = parse_message(msg.get("text", ""))
        if not (a or r or names or sp):
            continue
        msgs.append({"code": code, "a": a, "r": r, "names": names, "sp": sp})

    approved = {}                      # id -> (code, num, rec)
    need_code = False
    unknown_codes, missing = [], []
    name_ambiguous, name_unmatched = [], []

    for mm in msgs:
        code = mm["code"]
        if code is None:
            if len(registry) == 1:
                code = next(iter(registry))
            else:
                need_code = True
                continue
        sweep = registry.get(code)
        if not sweep:
            unknown_codes.append(code)
            continue
        index = sweep["items"]
        a, r = set(mm["a"]), set(mm["r"])
        if mm["sp"] == "all":
            a = set(int(k) for k in index.keys())
        elif mm["sp"] == "none":
            a = set()
        for seg in mm["names"]:
            num, winners = match_segment(seg, index)
            if num is not None:
                a.add(num)
            elif winners:
                name_ambiguous.append((code, seg, winners))
            else:
                name_unmatched.append((code, seg))
        a -= r
        for n in sorted(a):
            rec = index.get(str(n))
            if not rec:
                missing.append(f"{code}#{n}")
                continue
            rid = rec.get("id")
            if rid in have or rid in approved:
                continue
            approved[rid] = (code, n, rec)

    added = []
    for rid, (code, n, rec) in approved.items():
        rec = dict(rec)
        rec["approved_at"] = dt.date.today().isoformat()
        rec["sweep_code"] = code
        queue.append(rec)
        have.add(rid)
        added.append((code, n, rec.get("title", "?")))

    save_json(QUEUE, queue)
    save_json(OFFSET, {"offset": max_id})

    if added:
        listing = "; ".join(f"{c} #{n} {t}" for c, n, t in sorted(added))
        tg_send(f"Queued {len(added)} for drafting: {listing}. "
                f"The drafter will prepare them on its next run.")
    if need_code:
        tg_send("You sent numbers without a sweep code, and more than one sweep is "
                "live - I won't guess which. Reply with the code, e.g. 'yes 0922a: 1, 3'. "
                "Live sweeps: " + _live_sweeps_hint(registry))
    if unknown_codes:
        tg_send(f"I don't have sweep code(s) {sorted(set(unknown_codes))}. "
                f"Live sweeps: {', '.join(sorted(registry))}.")
    if name_ambiguous:
        for code, seg, winners in name_ambiguous:
            opts = ", ".join(f"#{w} {registry[code]['items'][str(w)].get('title','?')}"
                             for w in sorted(winners, key=int))
            tg_send(f"[{code}] '{seg}' matches more than one item ({opts}). "
                    f"Reply with the number.")
    if name_unmatched:
        tg_send("Couldn't match by name: "
                + "; ".join(f"[{c}] {s}" for c, s in name_unmatched)
                + ". Reply with the item NUMBER and its sweep code.")
    if missing:
        tg_send(f"These numbers aren't in their sweep: {missing}. "
                f"Reply with numbers from the digest whose code you name.")

    print(f"harvest: {len(added)} approved, "
          f"{len(missing) + len(name_unmatched)} unmatched, "
          f"{len(name_ambiguous)} ambiguous, need_code={need_code}. "
          f"Queue size: {len(queue)}")
    return len(added)

if __name__ == "__main__":
    harvest()
