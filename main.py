#!/usr/bin/env python3
"""Veille RSS -> édition rédigée par Claude -> site statique + email (Buttondown)."""
import datetime as dt, html, json, os, pathlib, re, sys, time
import feedparser, markdown, requests, yaml

ROOT = pathlib.Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
STATE = ROOT / "state.json"
SITE = ROOT / "docs"
INDEX = SITE / "issues.json"


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def collect():
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    cutoff = time.time() - CFG["lookback_days"] * 86400
    items = []
    for url in CFG["feeds"]:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"Flux ignoré {url}: {e}")
            continue
        for e in feed.entries:
            link = e.get("link")
            if not link or link in seen:
                continue
            ts = time.mktime(e.published_parsed) if e.get("published_parsed") else time.time()
            if ts < cutoff:
                continue
            items.append({
                "title": clean(e.get("title")), "link": link, "ts": ts,
                "summary": clean(e.get("summary"))[:600],
                "source": feed.feed.get("title", url),
            })
    items.sort(key=lambda i: -i["ts"])
    return items[: CFG["max_items"]]


def write_issue(items):
    import anthropic
    sources = "\n\n".join(
        f"[{i+1}] {it['title']} ({it['source']})\n{it['link']}\n{it['summary']}"
        for i, it in enumerate(items))
    system = (
        f"Tu rédiges « {CFG['name']} », une newsletter quotidienne en français. "
        f"Angle éditorial: {CFG['focus']} Ton: vivant, concret, accrocheur, sans jargon inutile. Règles strictes: "
        "n'utilise que les informations des sources fournies, n'invente aucun chiffre, "
        "reformule avec tes propres mots (pas de citation longue), "
        "et ajoute pour chaque sujet le lien source en Markdown.")
    prompt = (
        "Voici les sources du jour:\n\n" + sources + "\n\n"
        "Format de sortie: la 1re ligne est l'objet de l'email (sans #, 60 caractères max). "
        "Puis une ligne vide, puis le corps en Markdown: une courte intro (2 phrases), "
        "un titre ## par sujet avec 2-3 phrases d'analyse et « Pourquoi ça compte », "
        "puis une conclusion d'une phrase. Ignore les sources hors sujet.")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=CFG["model"], max_tokens=2500, system=system,
        messages=[{"role": "user", "content": prompt}])
    text = msg.content[0].text.strip()
    subject, _, body = text.partition("\n")
    return subject.strip("# ").strip(), body.strip()


def monetize(body):
    low = body.lower()
    hits = [a for a in CFG.get("affiliates", [])
            if any(k.lower() in low for k in a["keywords"])]
    out = body
    sp = CFG.get("sponsor") or {}
    if sp.get("text"):
        out += f"\n\n---\n**Sponsor** — {sp['text']} [En savoir plus]({sp.get('url', '')})"
    if hits:
        out += "\n\n---\n## À essayer\n" + "\n".join(
            f"- [{a['name']}]({a['url']}) — {a['pitch']}" for a in hits)
        out += "\n\n*Transparence : certains liens ci-dessus sont affiliés; ils peuvent nous rémunérer sans surcoût pour vous.*"
    return out


PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>$TITLE</title>
<link rel="alternate" type="application/rss+xml" href="feed.xml">
<style>:root{color-scheme:light dark}body{font:17px/1.6 system-ui,sans-serif;max-width:680px;margin:2rem auto;padding:0 1rem}
a{color:#2563eb}h1{line-height:1.2}li{margin:.4rem 0}small{opacity:.7}</style></head>
<body><p><a href="index.html">← $NAME</a></p>$BODY</body></html>"""


def publish_site(subject, body_md):
    SITE.mkdir(exist_ok=True)
    today = dt.date.today().isoformat()
    page = PAGE.replace("$TITLE", html.escape(subject)).replace("$NAME", html.escape(CFG["name"]))
    page = page.replace("$BODY", f"<h1>{html.escape(subject)}</h1><small>{today}</small>"
                        + markdown.markdown(body_md))
    (SITE / f"{today}.html").write_text(page, encoding="utf-8")

    issues = json.loads(INDEX.read_text()) if INDEX.exists() else []
    issues = [i for i in issues if i["date"] != today]
    issues.insert(0, {"date": today, "subject": subject, "file": f"{today}.html"})
    INDEX.write_text(json.dumps(issues, ensure_ascii=False, indent=1))

    lis = "".join(f'<li><a href="{i["file"]}">{html.escape(i["subject"])}</a> <small>{i["date"]}</small></li>'
                  for i in issues)
    home = PAGE.replace("$TITLE", html.escape(CFG["name"])).replace("← $NAME", "").replace(
        "$BODY", f"<h1>{html.escape(CFG['name'])}</h1><p>{html.escape(CFG['tagline'])}</p>"
                 f"<p><a href=\"https://buttondown.com\">S'abonner par email</a> · <a href=\"feed.xml\">RSS</a></p><ul>{lis}</ul>")
    (SITE / "index.html").write_text(home, encoding="utf-8")

    base = CFG["site_url"].rstrip("/")
    entries = "".join(
        f"<item><title>{html.escape(i['subject'])}</title><link>{base}/{i['file']}</link>"
        f"<guid>{base}/{i['file']}</guid></item>" for i in issues[:20])
    (SITE / "feed.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{html.escape(CFG['name'])}</title><link>{base}</link>"
        f"<description>{html.escape(CFG['tagline'])}</description>{entries}</channel></rss>",
        encoding="utf-8")


def send_email(subject, body_md):
    key = os.getenv("BUTTONDOWN_API_KEY")
    if not (CFG["email"]["enabled"] and key):
        print("Email ignoré (désactivé ou clé absente).")
        return
    status = "about_to_send" if CFG["email"]["auto_send"] else "draft"
    r = requests.post("https://api.buttondown.email/v1/emails",
                      headers={"Authorization": f"Token {key}"},
                      json={"subject": subject, "body": body_md, "status": status}, timeout=30)
    print("Buttondown:", r.status_code, status)
    r.raise_for_status()


def main():
    items = collect()
    if len(items) < CFG["min_items"]:
        print(f"Seulement {len(items)} articles neufs, édition sautée.")
        return 0
    subject, body = write_issue(items)
    body = monetize(body)
    publish_site(subject, body)
    send_email(subject, body)
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    seen.update(i["link"] for i in items)
    STATE.write_text(json.dumps(sorted(seen)[-2000:]))
    print("Édition publiée:", subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
