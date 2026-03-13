#!/usr/bin/env python3
"""
EUR-Lex → GitBook Sync Tool
==============================
Haal EU-verordeningen automatisch op van EUR-Lex en genereer een
volledige GitBook-mapstructuur met SUMMARY.md, klaar voor Git Sync.

Gebruik:
  python sync_eurlex_gitbook.py                        # Gebruik regulations.json
  python sync_eurlex_gitbook.py -c mijn_config.json    # Eigen config
  python sync_eurlex_gitbook.py --git-push              # Direct committen & pushen

Vereisten:
  pip install beautifulsoup4 requests
"""

import json
import os
import re
import sys
import time
import argparse
import subprocess
from pathlib import Path
from typing import Optional

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    sys.exit("❌ Installeer eerst: pip install beautifulsoup4")

try:
    import requests
except ImportError:
    sys.exit("❌ Installeer eerst: pip install requests")


# ============================================================================
# EUR-Lex HTML → Markdown Converter
# ============================================================================

def fetch_eurlex_html(celex: str, lang: str = "NL", retries: int = 3) -> str:
    """Haal de HTML-versie op van een EUR-Lex document."""
    url = (
        f"https://eur-lex.europa.eu/legal-content/{lang}"
        f"/TXT/HTML/?uri=CELEX:{celex}"
    )
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=30, headers={
                "User-Agent": "EUR-Lex-GitBook-Sync/1.0",
                "Accept": "text/html",
            })
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"   ⚠ Poging {attempt+1} mislukt, wacht {wait}s...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Kan {celex} niet ophalen na {retries} pogingen: {e}")


def get_text(el) -> str:
    """Schone tekst uit een element."""
    if isinstance(el, NavigableString):
        return str(el).strip()
    text = el.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def table_to_md(table: Tag) -> str:
    """HTML tabel → Markdown tabel."""
    rows = table.find_all("tr")
    if not rows:
        return ""
    md = []
    for row in rows:
        cells = [get_text(c).replace("|", "\\|") for c in row.find_all(["td", "th"])]
        md.append("| " + " | ".join(cells) + " |")
    if md:
        ncols = md[0].count("|") - 1
        md.insert(1, "| " + " | ".join(["---"] * max(ncols, 1)) + " |")
    return "\n".join(md)


def classify(el: Tag) -> str:
    """Classificeer EUR-Lex element op basis van CSS-klassen."""
    classes = set(el.get("class", []))

    # Titel van het document
    if classes & {"eli-main-title", "oj-doc-ti"}:
        return "title"
    # Hoofdstuk / Titel / Deel
    if classes & {"oj-ti-grseq-1", "ti-grseq-1"}:
        return "h1"
    # Afdeling / Sectie
    if classes & {"oj-ti-grseq-2", "ti-grseq-2", "oj-ti-section-1"}:
        return "h2"
    # Onderafdeling
    if classes & {"oj-ti-grseq-3", "ti-grseq-3", "oj-ti-section-2"}:
        return "h3"
    # Artikel
    if classes & {"oj-ti-art"}:
        return "article"
    # Artikel ondertitel
    if classes & {"oj-sti-art"}:
        return "article_title"
    # Noot
    if classes & {"oj-note", "oj-note-bottom"}:
        return "note"
    # Tabel
    if el.name == "table":
        return "table"
    # Normale tekst / overweging
    if classes & {"oj-normal", "oj-recital"}:
        return "p"

    return "other"


def html_to_markdown(html: str) -> tuple[str, str]:
    """
    Converteer EUR-Lex HTML naar Markdown.
    Returns: (markdown_tekst, document_titel)
    """
    soup = BeautifulSoup(html, "html.parser")
    content = (
        soup.find("div", id="TexteOnly")
        or soup.find("div", class_="eli-container")
        or soup.find("body")
        or soup
    )

    lines = []
    doc_title = ""

    # Document titel
    title_el = content.find(class_=re.compile(r"(eli-main-title|oj-doc-ti)"))
    if title_el:
        doc_title = get_text(title_el)
        lines.append(f"# {doc_title}")
        lines.append("")

    # Doorloop elementen
    for el in content.find_all(True):
        if el.parent and el.parent.name in ("td", "th", "tr"):
            continue

        etype = classify(el)
        text = get_text(el)

        if not text or text == doc_title:
            continue

        if etype == "title" and text != doc_title:
            lines += [f"# {text}", ""]
        elif etype == "h1":
            lines += [f"## {text}", ""]
        elif etype == "h2":
            lines += [f"### {text}", ""]
        elif etype == "h3":
            lines += [f"#### {text}", ""]
        elif etype == "article":
            lines += [f"### {text}", ""]
        elif etype == "article_title":
            lines += [f"*{text}*", ""]
        elif etype == "table":
            md = table_to_md(el)
            if md:
                lines += [md, ""]
        elif etype == "note":
            lines += [f"> {text}", ""]
        elif etype == "p":
            lines += [text, ""]

    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result)

    # Als OJ-klassen niet werken, gebruik fallback
    if not lines or len(lines) < 5:
        result = _fallback_convert(soup)

    return result.strip() + "\n", doc_title


def _fallback_convert(soup: BeautifulSoup) -> str:
    """Fallback: heuristisch op basis van tekst patronen."""
    text = soup.get_text(separator="\n")
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(TITEL|TITLE|HOOFDSTUK|CHAPTER|CHAPITRE)\s+[IVXLCDM\d]+", line, re.I):
            lines += ["", f"## {line}", ""]
        elif re.match(r"^(AFDELING|SECTION)\s+\d+", line, re.I):
            lines += ["", f"### {line}", ""]
        elif re.match(r"^(Artikel|Article)\s+\d+", line, re.I):
            lines += ["", f"### {line}", ""]
        elif re.match(r"^(BIJLAGE|ANNEX)", line, re.I):
            lines += ["", f"## {line}", ""]
        else:
            lines += [line, ""]
    return "\n".join(lines)


# ============================================================================
# GitBook structuur genereren
# ============================================================================

def generate_gitbook_structure(config: dict, output_dir: str):
    """
    Genereer de volledige GitBook mapstructuur:

    docs/
    ├── .gitbook.yaml
    ├── README.md
    ├── SUMMARY.md
    └── verordeningen/
        ├── README.md
        ├── avg-gdpr.md
        ├── ai-act.md
        ├── dsa.md
        └── dma.md
    """
    out = Path(output_dir)
    reg_dir = out / "verordeningen"
    reg_dir.mkdir(parents=True, exist_ok=True)

    project_title = config.get("project_title", "EU Wetgeving")
    lang = config.get("lang", "NL")
    regulations = config.get("regulations", [])

    # --- .gitbook.yaml ---
    gitbook_yaml = "root: ./\n\nstructure:\n  readme: README.md\n  summary: SUMMARY.md\n"
    (out / ".gitbook.yaml").write_text(gitbook_yaml, encoding="utf-8")
    print("📄 .gitbook.yaml aangemaakt")

    # --- Hoofd README.md ---
    readme_lines = [
        f"# {project_title}",
        "",
        "Overzicht van EU-verordeningen, automatisch opgehaald van EUR-Lex.",
        "",
        "## Verordeningen",
        "",
    ]
    for reg in regulations:
        readme_lines.append(
            f"- [{reg['short_title']}](verordeningen/{reg['slug']}.md) — {reg.get('description', '')}"
        )
    readme_lines += [
        "",
        "---",
        "",
        f"*Bron: [EUR-Lex](https://eur-lex.europa.eu/) — automatisch gesynchroniseerd*",
        "",
    ]
    (out / "README.md").write_text("\n".join(readme_lines), encoding="utf-8")
    print("📄 README.md aangemaakt")

    # --- Verordeningen README (groepspagina) ---
    reg_readme = [
        "# Verordeningen",
        "",
        "Hieronder vindt u de volledige tekst van de opgenomen EU-verordeningen.",
        "",
    ]
    (reg_dir / "README.md").write_text("\n".join(reg_readme), encoding="utf-8")

    # --- Elke verordening ophalen en converteren ---
    summary_entries = []
    for i, reg in enumerate(regulations):
        celex = reg["celex"]
        slug = reg["slug"]
        short = reg["short_title"]

        print(f"\n🔄 [{i+1}/{len(regulations)}] {short} (CELEX:{celex})")

        try:
            print(f"   📥 HTML ophalen van EUR-Lex ({lang})...")
            html = fetch_eurlex_html(celex, lang)
            print(f"   ✓ {len(html):,} tekens ontvangen")

            print("   🔧 Omzetten naar Markdown...")
            md_content, detected_title = html_to_markdown(html)

            # Front matter voor GitBook
            title = detected_title or short
            front = (
                f"---\n"
                f"description: >-\n"
                f"  {reg.get('description', title)[:200]}\n"
                f"---\n\n"
            )

            md_path = reg_dir / f"{slug}.md"
            md_path.write_text(front + md_content, encoding="utf-8")

            line_count = md_content.count("\n")
            print(f"   ✅ {slug}.md opgeslagen ({line_count} regels)")

        except Exception as e:
            print(f"   ❌ Fout: {e}")
            # Maak een placeholder aan
            placeholder = (
                f"---\ndescription: {reg.get('description', short)}\n---\n\n"
                f"# {short}\n\n"
                f"⚠️ Deze verordening kon niet automatisch worden opgehaald.\n\n"
                f"Bekijk de originele tekst op "
                f"[EUR-Lex](https://eur-lex.europa.eu/legal-content/{lang}/TXT/?uri=CELEX:{celex}).\n"
            )
            (reg_dir / f"{slug}.md").write_text(placeholder, encoding="utf-8")

        summary_entries.append((short, f"verordeningen/{slug}.md"))

        # Rate limiting: wacht even tussen requests
        if i < len(regulations) - 1:
            time.sleep(1)

    # --- SUMMARY.md ---
    summary_lines = [
        "# Summary",
        "",
        f"* [{project_title}](README.md)",
        "",
        "## Verordeningen",
        "",
        "* [Overzicht](verordeningen/README.md)",
    ]
    for title, path in summary_entries:
        summary_lines.append(f"  * [{title}]({path})")

    summary_lines.append("")
    (out / "SUMMARY.md").write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"\n📄 SUMMARY.md aangemaakt met {len(summary_entries)} verordeningen")


# ============================================================================
# Optioneel: Git commit & push
# ============================================================================

def git_push(output_dir: str, message: Optional[str] = None):
    """Commit en push wijzigingen naar de Git remote."""
    cwd = output_dir
    msg = message or f"sync: EUR-Lex verordeningen bijgewerkt ({time.strftime('%Y-%m-%d %H:%M')})"

    try:
        subprocess.run(["git", "add", "."], cwd=cwd, check=True)
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True
        )
        if not result.stdout.strip():
            print("ℹ️  Geen wijzigingen om te committen")
            return

        subprocess.run(["git", "commit", "-m", msg], cwd=cwd, check=True)
        subprocess.run(["git", "push"], cwd=cwd, check=True)
        print("🚀 Wijzigingen gepusht naar remote — GitBook synct automatisch!")
    except FileNotFoundError:
        print("❌ Git niet gevonden. Installeer Git of push handmatig.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Git fout: {e}")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Synchroniseer EU-verordeningen van EUR-Lex naar GitBook"
    )
    parser.add_argument(
        "-c", "--config",
        default="regulations.json",
        help="Pad naar config JSON (standaard: regulations.json)"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output directory (overschrijft config)"
    )
    parser.add_argument(
        "--git-push",
        action="store_true",
        help="Automatisch committen en pushen na sync"
    )
    parser.add_argument(
        "--message", "-m",
        default=None,
        help="Commit message (alleen met --git-push)"
    )
    args = parser.parse_args()

    # Config laden
    config_path = Path(args.config)
    if not config_path.exists():
        sys.exit(f"❌ Config niet gevonden: {config_path}\n"
                 f"   Maak een regulations.json aan (zie voorbeeld).")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    output_dir = args.output or config.get("output_dir", "./docs")

    print(f"{'='*60}")
    print(f"  EUR-Lex → GitBook Sync")
    print(f"  Verordeningen: {len(config.get('regulations', []))}")
    print(f"  Taal: {config.get('lang', 'NL')}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}")

    generate_gitbook_structure(config, output_dir)

    print(f"\n{'='*60}")
    print(f"  ✅ Sync voltooid!")
    print(f"  📁 Output: {output_dir}/")
    print(f"{'='*60}")

    if args.git_push:
        print("\n🔄 Git push...")
        git_push(output_dir, args.message)
    else:
        print("\n💡 Volgende stappen:")
        print(f"   cd {output_dir}")
        print("   git add . && git commit -m 'sync: EUR-Lex update' && git push")
        print("   → GitBook synct automatisch via Git Sync!")


if __name__ == "__main__":
    main()
