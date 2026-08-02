#!/usr/bin/env python3
"""
Drafter (skill 2, part 2).

Reads context/approved_queue.json (filled by harvest_telegram.py) and prepares a
DRAFT BRIEF for each approved item. Claude (as the Drafter) then writes the actual
text using voice.md + about_me.yaml + network.yaml.

This script does NOT write the prose - it assembles the grounded brief so that the
model has every verified fact in front of it and cannot invent:
  - the item (person or grant)
  - the warm tie, quoted verbatim from network.yaml, or explicitly "COLD"
  - the confirm_with flag, if any
  - the CV facts available
  - the language to use
  - which register (warm vs cold)

It NEVER sends anything. Output goes to drafts/ for review.
"""
from __future__ import annotations
import io, json, os, sys, datetime as dt
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml")

ROOT = Path(__file__).parent
CTX = ROOT / "context"; DRAFTS = ROOT / "drafts"
CTX.mkdir(exist_ok=True); DRAFTS.mkdir(exist_ok=True)
QUEUE = CTX / "approved_queue.json"
DONE = CTX / "drafted.json"

SPAIN_HINTS = ["spain", "españa", "espanya", "barcelona", "madrid", "valencia",
               "uab", "universitat", "universidad", "csic", "creaf", "sevilla",
               "granada", "bilbao", "zaragoza", "catalunya", "cataluña"]

def load_yaml(p):
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return yaml.safe_load(f)

def load_json(p, default):
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except Exception: return default
    return default

def _oa_get(url):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "opportunity-scout-dossier/1.0 (mailto:%s)" %
        (os.environ.get("OPENALEX_MAILTO","opportunity-scout@example.com")),
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8","replace"))

import re as _re
def _clean_title(t):
    return _re.sub(r"<[^>]+>", "", t or "")

def research_person(item):
    """Pull a small research brief from OpenAlex: signature paper, recent work, funders."""
    aid = (item.get("url") or "").rsplit("/",1)[-1]
    out = {"signature": None, "recent": [], "funders": [], "n_works": None, "note": ""}
    if not aid.startswith("A"):
        out["note"] = "No OpenAlex author id; research skipped."
        return out
    base = "https://api.openalex.org/works?filter=author.id:%s" % aid
    try:
        top = _oa_get(base + "&sort=cited_by_count:desc&per-page=1&select=title,publication_year,cited_by_count,funders,primary_location")
        r = top.get("results", [])
        if r:
            w = r[0]
            out["signature"] = {"title": _clean_title(w.get("title","")), "year": w.get("publication_year"),
                                "cited": w.get("cited_by_count")}
    except Exception as e:
        out["note"] += "signature fetch failed; "
    try:
        rec = _oa_get(base + "&sort=publication_date:desc&per-page=4&select=title,publication_year,funders")
        funders = {}
        for w in rec.get("results", []):
            out["recent"].append({"title": _clean_title(w.get("title","")), "year": w.get("publication_year")})
            for g in (w.get("funders") or []):
                fn = g.get("display_name")
                if fn: funders[fn] = funders.get(fn, 0) + 1
        # also scan top-cited work's funders
        if out.get("signature"):
            try:
                topg = _oa_get(base + "&sort=cited_by_count:desc&per-page=5&select=funders")
                for w in topg.get("results", []):
                    for g in (w.get("funders") or []):
                        fn = g.get("display_name")
                        if fn: funders[fn] = funders.get(fn, 0) + 1
            except Exception:
                pass
        out["funders"] = sorted(funders, key=funders.get, reverse=True)[:5]
    except Exception as e:
        out["note"] += "recent fetch failed; "
    if not out["funders"]:
        out["note"] += "No funding data recorded in OpenAlex (does not mean unfunded)."
    return out

def pick_language(item):
    """Match the recipient: Spanish for Spain-based, English otherwise."""
    blob = " ".join([str(item.get("institution", "")),
                     str(item.get("raw_affiliation", "")),
                     str(item.get("title", "")),
                     str(item.get("tier", ""))]).lower()
    if item.get("tier") in ("regional", "national"):
        return "Spanish"
    if any(h in blob for h in SPAIN_HINTS):
        return "Spanish"
    return "English"

def find_warm_tie(item, network):
    """Only a tie explicitly in network.yaml counts. Never infer."""
    if item.get("warm_tie"):
        return item["warm_tie"]
    name = (item.get("title") or "").lower()
    for c in network.get("connections", []):
        target = (c.get("target") or "").lower()
        tset = set(t for t in target.split() if len(t) > 1)
        nset = set(t for t in name.split() if len(t) > 1)
        if len(tset) >= 2 and len(nset) >= 2 and (tset <= nset or nset <= tset):
            return {"connection": c.get("connection", ""),
                    "usable_as": c.get("usable_as", "none"),
                    "confirm_with": c.get("confirm_with", ""),
                    "status": c.get("status", "cold")}
    return None

def build_brief(item, about, network):
    tie = find_warm_tie(item, network)
    lang = pick_language(item)
    kind = item.get("kind", "person")
    register = "WARM" if (tie and tie.get("status") == "confirmed") else "COLD"

    L = []
    L.append(f"# DRAFT BRIEF - {item.get('title','(untitled)')}")
    L.append("")
    L.append(f"- item type: **{kind}**")
    L.append(f"- geographic tier: {item.get('tier','unknown')}")
    L.append(f"- language to write in: **{lang}**")
    L.append(f"- register: **{register}**  "
             f"({'model on the Alex email' if register=='WARM' else 'model on the Joan email'})")
    L.append(f"- link: {item.get('url','')}")
    if item.get("institution"): L.append(f"- institution: {item['institution']}")
    if item.get("raw_affiliation"): L.append(f"- affiliation string: {item['raw_affiliation']}")
    if item.get("pi_last_author"): L.append(f"- senior/last author (likely PI): {item['pi_last_author']}")
    if item.get("why_it_fits"): L.append(f"- why it fits: {item['why_it_fits']}")
    if item.get("example_work"): L.append(f"- example work: {item['example_work']}")
    if item.get("deadline"): L.append(f"- DEADLINE: {item['deadline']}")
    if item.get("eligibility_notes"): L.append(f"- ELIGIBILITY NOTES: {item['eligibility_notes']}")
    L.append("")

    # Warm tie block - the single most important guardrail
    L.append("## Connection (network.yaml ground truth)")
    if tie:
        L.append(f"- connection (quote verbatim, do not embellish): \"{tie.get('connection','')}\"")
        L.append(f"- usable as: {tie.get('usable_as')}")
        L.append(f"- status: {tie.get('status')}")
        if tie.get("confirm_with"):
            L.append(f"- **CONFIRM BEFORE NAMING: {tie['confirm_with']}** - do NOT name this "
                     f"person in the body until Adonai confirms. Surface as a checklist line.")
        if tie.get("status") != "confirmed":
            L.append("- **status is NOT confirmed -> write as COLD; do not assert the tie.**")
    else:
        L.append("- **NO TIE ON FILE - this is a COLD contact.** Do not imply any relationship, "
                 "shared institution, or mutual acquaintance. A shared surname or co-author is "
                 "NOT a connection.")
    L.append("")

    # What to produce
    L.append("## What to write")
    if kind == "person":
        L.append("An outreach email in Adonai's voice (see voice.md). Follow the arc: their "
                 "relevance -> concrete self-intro -> plain ask -> offer to reduce their burden "
                 "-> pre-empt the no -> warm close. 150-300 words.")
        L.append("Ask: a brief 15-20 min meeting / whether there is scope to collaborate or "
                 "join, WITHOUT assuming a position exists.")
    else:
        L.append("Two things: (a) a short enquiry email to the funder/office if a contact makes "
                 "sense, and (b) draft application text - the framing paragraphs Adonai would "
                 "submit, grounded in his real background.")
        L.append("**Surface every eligibility flag at the TOP** as a checklist Adonai must "
                 "verify before applying.")
    L.append("")

    # Grounding facts
    L.append("## Verified facts you MAY use (from about_me.yaml)")
    L.append(f"- {about['identity']['name']}, {about['current_position']['role']} at "
             f"{about['current_position']['institution']} ({about['current_position']['lab']}, "
             f"PI {about['current_position']['pi']})")
    mt = about["masters_thesis"]
    L.append(f"- Master's thesis in the {mt['lab']} on {mt['topic']}; {mt['grade']} "
             f"(THESIS REPORT ONLY - never present as a degree GPA)")
    L.append(f"- Methods: {mt['methods'].strip()}")
    L.append(f"- Programming: {', '.join(about['skills']['programming'])}; "
             f"behavioural analysis: {', '.join(about['skills']['behavioural_analysis'])}")
    L.append(f"- Goal: {about['goals']['short_term']} {about['goals']['medium_term']}")
    L.append("")
    L.append("## You MUST NOT assert (forbidden_inferences)")
    for f in about.get("forbidden_inferences", []):
        L.append(f"- {f}")
    L.append("")
    L.append("Anything you'd otherwise guess -> write `[BLANK: what Adonai must fill]`.")
    L.append("")
    L.append("## Delivery")
    L.append("Write the draft to a file in drafts/ and create it as a **Gmail draft**. "
             "**NEVER send.** Adonai reviews and sends himself.")
    return "\n".join(L)

def main():
    about = load_yaml(ROOT / "about_me.yaml")
    # network.yaml lives in the scout repo; allow an override path
    net_path = os.environ.get("NETWORK_YAML", str(ROOT / "network.yaml"))
    network = load_yaml(net_path) if Path(net_path).exists() else {"connections": []}

    queue = load_json(QUEUE, [])
    done = set(load_json(DONE, []))
    pending = [q for q in queue if q.get("id") not in done]

    if not pending:
        print("Nothing pending in approved_queue.json.")
        return

    DOSSIERS = ROOT / "dossiers"; DOSSIERS.mkdir(exist_ok=True)
    written = []
    for item in pending:
        brief = build_brief(item, about, network)
        research = research_person(item) if item.get("kind") == "person" else {
            "note": "Grant item — research is the call page summary; see eligibility notes.",
            "signature": None, "recent": [], "funders": []}
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in (item.get("title") or "item"))[:60]
        stamp = dt.date.today().isoformat()
        # append the research to the brief so the writer sees it
        rb = ["", "## Research (from OpenAlex — verified data, cite as-is)"]
        if research.get("signature"):
            sg = research["signature"]
            rb.append(f"- Signature paper: \"{sg['title']}\" ({sg.get('year')}), {sg.get('cited')} citations")
        if research.get("recent"):
            rb.append("- Recent work: " + "; ".join(
                f"\"{r['title']}\" ({r.get('year')})" for r in research["recent"][:3]))
        if research.get("funders"):
            rb.append("- Funding (from paper acknowledgements): " + ", ".join(research["funders"]))
        if research.get("note"):
            rb.append(f"- Note: {research['note']}")
        (DRAFTS / f"{stamp}_{safe}_BRIEF.md").write_text(brief + "\n".join(rb), encoding="utf-8")
        # dossier data the PDF builder will merge with the written email
        dossier = {"item": item, "research": research, "brief_flags": {
                       "language": pick_language(item),
                       "warm_tie": find_warm_tie(item, network)},
                   "safe": safe, "date": stamp}
        (DOSSIERS / f"{stamp}_{safe}.data.json").write_text(
            json.dumps(dossier, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(safe)
        done.add(item.get("id"))

    (CTX / "drafted.json").write_text(json.dumps(sorted(done), indent=2), encoding="utf-8")
    print(f"Prepared {len(written)} item(s) with research:")
    for w in written:
        print("  -", w)
    print("\nNext: write each email from its BRIEF into dossiers/<date>_<safe>.email.md "
          "(first line 'Subject: ...', then the body). Then run build_dossier.py to render "
          "the PDF dossiers. NEVER send; PDF-only.")

if __name__ == "__main__":
    main()
