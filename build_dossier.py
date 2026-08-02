#!/usr/bin/env python3
"""
Build PDF dossiers (skill 2, final stage).

For each dossiers/<date>_<safe>.data.json (written by draft.py) plus its matching
dossiers/<date>_<safe>.email.md (the email you/the drafter wrote), render a one- to
two-page PDF dossier to dossiers/<date>_<safe>.pdf containing:

  1. The finished email — subject + body (ready to copy/send yourself)
  2. Research — signature paper, recent work, funding (from OpenAlex)
  3. Flags — eligibility red flags, warm-tie/confirm-before-naming notes

PDF-only: nothing is emailed or sent. The PDFs live in dossiers/ (committed to the
repo, so `git pull` brings them to your local folder).
"""
from __future__ import annotations
import io, os, json, sys, datetime as dt
from pathlib import Path

try:
    from fpdf import FPDF
except ImportError:
    sys.exit("Missing dependency: pip install fpdf2")

ROOT = Path(__file__).parent
DOSSIERS = ROOT / "dossiers"
DOSSIERS.mkdir(exist_ok=True)

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_ITAL = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"

def _load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default

def parse_email_md(path):
    """First 'Subject:' line -> subject; rest -> body."""
    if not Path(path).exists():
        return None, None
    text = Path(path).read_text(encoding="utf-8")
    subject, body_lines, seen = "", [], False
    for line in text.splitlines():
        if not seen and line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip(); seen = True
            continue
        body_lines.append(line)
    return subject, "\n".join(body_lines).strip()

class Dossier(FPDF):
    def header(self):
        pass
    def footer(self):
        self.set_y(-12)
        self._font("", 8)
        self.set_text_color(140,140,140)
        self.cell(0, 8, f"Opportunity Scout dossier · {dt.date.today().isoformat()} · "
                        f"review before sending — nothing was sent", align="C")
        self.set_text_color(0,0,0)
    def _font(self, style="", size=11):
        if style == "I" and not getattr(self, "has_italic", False):
            style = ""
        self.set_font("DejaVu", style, size)
    def mc(self, txt, h=6):
        # always return cursor to the left margin on the next line
        self.multi_cell(self.epw, h, txt if txt is not None else " ",
                        new_x="LMARGIN", new_y="NEXT")

def build_one(data_path):
    data = _load(data_path, {})
    if not data:
        return None
    item = data.get("item", {})
    research = data.get("research", {})
    flags = data.get("brief_flags", {})
    safe = data.get("safe", "item")
    stamp = data.get("date", dt.date.today().isoformat())
    email_md = DOSSIERS / f"{stamp}_{safe}.email.md"
    subject, body = parse_email_md(email_md)

    pdf = Dossier()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_font("DejaVu", "", FONT_REG)
    pdf.add_font("DejaVu", "B", FONT_BOLD)
    pdf.has_italic = os.path.exists(FONT_ITAL)
    if pdf.has_italic:
        pdf.add_font("DejaVu", "I", FONT_ITAL)
    pdf.add_page()

    # Title
    pdf._font("B", 16)
    pdf.mc(item.get("title", "(untitled)"), 9)
    pdf._font("", 9); pdf.set_text_color(90,90,90)
    meta = f"{item.get('kind','')} · tier: {item.get('tier','')} · {item.get('institution','')}"
    pdf.mc(meta); pdf.set_text_color(0,0,0)
    if item.get("url"):
        pdf._font("", 9); pdf.set_text_color(40,80,200)
        pdf.mc(item["url"]); pdf.set_text_color(0,0,0)
    pdf.ln(3)

    # Flags (top, never hidden)
    wt = flags.get("warm_tie")
    elig = item.get("eligibility_notes", "")
    if wt or elig:
        pdf._font("B", 12); pdf.mc("Flags"); pdf._font("", 10)
        if elig:
            pdf.mc(f"• Eligibility: {elig}", 6)
        if wt:
            cw = f" (confirm with {wt.get('confirm_with')})" if wt.get("confirm_with") else ""
            pdf.mc(f"• Warm tie [{wt.get('status')}]{cw}: {wt.get('connection','')}", 6)
        elif item.get("kind") == "person":
            pdf.mc("• No warm tie on file — cold contact.", 6)
        pdf.ln(2)

    # The email
    pdf._font("B", 13); pdf.mc("Email draft"); pdf._font("", 10)
    pdf.set_text_color(70,70,70)
    pdf.mc(f"Language: {flags.get('language','?')}   (copy & send yourself)", 6)
    pdf.set_text_color(0,0,0); pdf.ln(1)
    if subject is None:
        pdf._font("I", 10)
        pdf.mc("[No email written yet — create dossiers/"
               f"{stamp}_{safe}.email.md with 'Subject:' + body, then rebuild.]", 6)
        pdf._font("", 10)
    else:
        pdf._font("B", 10); pdf.mc(f"Subject: {subject}", 6)
        pdf._font("", 10); pdf.ln(1)
        for para in (body or "").split("\n"):
            pdf.mc(para if para.strip() else " ", 6)
    pdf.ln(3)

    # Research
    pdf._font("B", 13); pdf.mc("Research", 8); pdf._font("", 10)
    sg = research.get("signature")
    if sg:
        pdf._font("B", 10); pdf.mc("Signature paper", 6)
        pdf._font("", 10)
        pdf.mc(f"\"{sg.get('title','')}\" ({sg.get('year','')}), "
               f"{sg.get('cited','?')} citations", 6)
    if research.get("recent"):
        pdf._font("B", 10); pdf.mc("Recent / current work", 6)
        pdf._font("", 10)
        for r in research["recent"][:3]:
            pdf.mc(f"• \"{r.get('title','')}\" ({r.get('year','')})", 6)
    if research.get("funders"):
        pdf._font("B", 10); pdf.mc("Funding (from paper acknowledgements)", 6)
        pdf._font("", 10)
        pdf.mc(", ".join(research["funders"]), 6)
    if research.get("note"):
        pdf._font("I", 9); pdf.set_text_color(120,120,120)
        pdf.mc(research["note"]); pdf.set_text_color(0,0,0); pdf._font("", 10)

    out = DOSSIERS / f"{stamp}_{safe}.pdf"
    pdf.output(str(out))
    return out

def main():
    data_files = sorted(DOSSIERS.glob("*.data.json"))
    if not data_files:
        print("No dossier data files in dossiers/. Run draft.py first.")
        return
    built = []
    for d in data_files:
        try:
            out = build_one(d)
            if out: built.append(out)
        except Exception as e:
            print(f"  [dossier] {d.name}: {e}", file=sys.stderr)
    print(f"Built {len(built)} PDF dossier(s):")
    for b in built:
        print("  -", b)

if __name__ == "__main__":
    main()
