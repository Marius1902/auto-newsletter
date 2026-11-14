#!/usr/bin/env python3
"""Veille RSS -> édition rédigée et relue par Claude -> site web + email."""
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import pathlib
import re
import string
import sys
import time

import feedparser
import markdown
import requests
import yaml

ROOT = pathlib.Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
STATE = ROOT / "state.json"
SITE = ROOT / "docs"
INDEX = SITE / "issues.json"
feedparser.USER_AGENT = "Mozilla/5.0 (compatible; NewsletterBot/1.0)"
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


# ---------- Utilitaires ----------
def site_url():
    u = (CFG.get("site_url") or "").strip().rstrip("/")
    if u:
        return u
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def fr_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {MOIS[d.month - 1]} {d.year}"


def load_issues():
    return json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else []


# ---------- 1. Collecte ----------
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
        n = 0
        for e in feed.entries:
            link = e.get("link")
            if not link or link in seen:
                continue
            ts = time.mktime(e.published_parsed) if e.get("published_parsed") else time.time()
            if ts < cutoff:
                continue
            n += 1
            items.append({"title": clean(e.get("title")), "link": link, "ts": ts,
                          "summary": clean(e.get("summary"))[:700],
                          "source": feed.feed.get("title", url)})
        print(f"{n:>3} article(s) neuf(s) : {url}")
    items.sort(key=lambda i: -i["ts"])
    return items[: CFG["max_items"]]


# ---------- 2. Rédaction ----------
def ask(system, prompt, max_tokens=3500):
    import anthropic
    client = anthropic.Anthropic()
    last = None
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=CFG["model"], max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in msg.content if b.type == "text").strip()
        except anthropic.AuthenticationError:
            sys.exit("ERREUR : la clé ANTHROPIC_API_KEY est refusée. Vérifie qu'elle est "
                     "copiée en entier, sans espace, dans les secrets GitHub.")
        except anthropic.NotFoundError:
            sys.exit("ERREUR : modèle introuvable. Vérifie la ligne 'model' de config.yaml.")
        except anthropic.BadRequestError as e:
            sys.exit(f"ERREUR : demande refusée par Anthropic ({e}). "
                     "Cause fréquente : crédit épuisé sur console.anthropic.com.")
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    sys.exit(f"ERREUR : l'IA ne répond pas ({last}).")


def write_issue(items):
    sources = "\n\n".join(
        f"[{i+1}] {it['title']} ({it['source']})\n{it['link']}\n{it['summary']}"
        for i, it in enumerate(items))
    system = (
        f"Tu es le rédacteur en chef de « {CFG['name']} », newsletter quotidienne en français. "
        f"Angle éditorial : {CFG['focus']} "
        "Ton : vivant, précis, tutoiement, comme un ami expert qui te briefe en 3 minutes. "
        "Règles strictes : tu n'utilises QUE les informations des sources fournies; "
        "tu n'inventes aucun chiffre, nom ou citation; tu reformules avec tes propres mots; "
        "tu évites les clichés (« révolution », « game changer », « dans un monde où »).")
    prompt = (
        f"Sources du jour :\n\n{sources}\n\n"
        "Rédige l'édition. Format EXACT :\n"
        "Ligne 1 : l'objet de l'email (accrocheur, 60 caractères max, sans #, sans guillemets).\n"
        "Puis une ligne vide, puis en Markdown :\n"
        "- une accroche de 2 phrases qui donne envie de lire;\n"
        "- « ## L'essentiel » avec 3 puces d'une ligne;\n"
        "- 3 à 5 sujets, chacun avec un titre « ## », 2-3 phrases d'analyse, "
        "« **Pourquoi ça compte :** … », « **Ton move :** une action concrète à tester aujourd'hui », "
        "et « [Source : nom du site](url) » avec l'URL exacte de la source;\n"
        "- une conclusion d'une phrase.\n"
        "Ignore les sources hors sujet. Choisis les plus utiles pour des créateurs et des développeurs de jeux.")
    draft = ask(system, prompt)

    review = ask(
        system,
        f"Sources :\n\n{sources}\n\nBrouillon :\n\n{draft}\n\n"
        "Relis ce brouillon comme un éditeur exigeant : (1) supprime ou corrige toute affirmation "
        "absente des sources; (2) resserre le style, supprime les redites et les clichés; "
        "(3) vérifie que chaque lien source est l'URL exacte fournie. "
        "Renvoie UNIQUEMENT la version finale, dans exactement le même format "
        "(1re ligne = objet de l'email), sans commentaire.")

    subject, _, body = review.partition("\n")
    subject = subject.strip("# *\"«» ").strip()
    body = body.strip()
    allowed = {it["link"] for it in items}

    def keep_known(m):  # supprime tout lien qui ne vient pas des sources
        return m.group(0) if m.group(2) in allowed else m.group(1)
    body = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", keep_known, body)

    if not subject or len(subject) > 120 or len(body) < 400:
        sys.exit("ERREUR : la rédaction est incomplète, aucune édition publiée (relance le robot).")
    return subject, body


def excerpt(body):
    for para in body.split("\n"):
        t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", para)
        t = re.sub(r"[#*_>`-]", "", t).strip()
        if len(t) > 50:
            return t[:157] + "…" if len(t) > 160 else t
    return CFG["tagline"]


def monetize(body):
    out = body
    low = body.lower()
    hits = [a for a in CFG.get("affiliates") or []
            if any(k.lower() in low for k in a["keywords"])]
    sp = CFG.get("sponsor") or {}
    if sp.get("text"):
        out += f"\n\n---\n**Sponsor** — {sp['text']} [En savoir plus]({sp.get('url', '')})"
    if hits:
        out += "\n\n---\n## À essayer\n" + "\n".join(
            f"- [{a['name']}]({a['url']}) — {a['pitch']}" for a in hits)
        out += ("\n\n*Transparence : certains liens ci-dessus sont affiliés; "
                "ils peuvent nous rémunérer, sans surcoût pour toi.*")
    return out


# ---------- 3. Site web ----------
CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#6b6560;--card:#fff;--line:#e7e5e4;--a:#6d28d9;--b:#db2777;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0c0a09;--fg:#f5f5f4;--muted:#a8a29e;--card:#1c1917;--line:#2b2724;--a:#a78bfa;--b:#f472b6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.65 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:0 20px 48px}
.top{display:flex;justify-content:space-between;align-items:center;padding:22px 0}
.brand{font-weight:800;font-size:1.1rem;color:var(--fg);text-decoration:none;letter-spacing:-.01em}
.rss{font-size:.8rem;color:var(--muted);text-decoration:none;border:1px solid var(--line);padding:2px 10px;border-radius:99px}
.hero{padding:28px 0 8px}
.hero h1{font-size:clamp(2rem,6vw,3rem);line-height:1.1;margin:0 0 12px;letter-spacing:-.03em;
background:linear-gradient(90deg,var(--a),var(--b));-webkit-background-clip:text;background-clip:text;color:transparent}
.tag{font-size:1.15rem;color:var(--muted);margin:0 0 20px}
.sub{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}
.sub input{flex:1 1 220px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);font:inherit}
.sub button{padding:12px 20px;border:0;border-radius:10px;background:linear-gradient(90deg,var(--a),var(--b));color:#fff;font:inherit;font-weight:700;cursor:pointer}
.subbox{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin:36px 0}
.subbox p{margin:0 0 4px;font-weight:700}
.sec{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:36px 0 12px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:0 0 12px;text-decoration:none;color:inherit;transition:transform .15s,border-color .15s}
.card:hover{transform:translateY(-2px);border-color:var(--a)}
.card .meta{display:block;font-size:.8rem;color:var(--muted);margin-bottom:4px}
.card strong{display:block;font-size:1.15rem;line-height:1.3;margin-bottom:4px}
.card .ex{color:var(--muted);font-size:.95rem}
.banner{display:flex;justify-content:space-between;align-items:flex-end;min-height:120px;border-radius:16px;padding:18px 20px;color:#fff;font-weight:700;margin:6px 0 24px}
.banner time{font-weight:500;opacity:.9}
article h1{font-size:clamp(1.7rem,5vw,2.3rem);line-height:1.2;letter-spacing:-.02em;margin:0 0 18px}
article h2{font-size:1.35rem;line-height:1.3;margin:2rem 0 .5rem;padding-left:12px;border-left:4px solid var(--a)}
article p,article li{margin:.6rem 0}
article a{color:var(--a)}
article hr{border:0;border-top:1px solid var(--line);margin:2rem 0}
footer{margin-top:48px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:.85rem}
"""

BASE = string.Template("""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title><meta name="description" content="$desc">
<meta property="og:title" content="$title"><meta property="og:description" content="$desc">
<meta property="og:type" content="$ogtype"><meta property="og:site_name" content="$site">
<link rel="alternate" type="application/rss+xml" title="$site" href="feed.xml">
<style>$css</style></head><body><div class="wrap">
<header class="top"><a class="brand" href="index.html">$site</a><a class="rss" href="feed.xml">RSS</a></header>
$main
<footer>Édition rédigée avec l'aide de l'IA à partir des sources liées dans chaque article.
Les liens affiliés éventuels sont signalés. &copy; $year $site</footer>
</div></body></html>""")


def sub_form():
    u = (CFG.get("buttondown_username") or "").strip()
    if not u:
        return ""
    return (f'<form class="sub" action="https://buttondown.com/api/emails/embed-subscribe/{u}" '
            f'method="post" target="popupwindow" '
            f'onsubmit="window.open(\'https://buttondown.com/{u}\',\'popupwindow\')">'
            '<input type="email" name="email" placeholder="ton@email.com" required aria-label="Email">'
            '<button type="submit">Recevoir l\'édition</button></form>')


def page(title, desc, main, ogtype="website"):
    return BASE.substitute(title=html.escape(title), desc=html.escape(desc, quote=True),
                           ogtype=ogtype, site=html.escape(CFG["name"]), css=CSS,
                           main=main, year=dt.date.today().year)


def hue(date):
    return int(hashlib.md5(date.encode()).hexdigest()[:4], 16) % 360


def render_index(issues):
    cards = "".join(
        f'<a class="card" href="{i["file"]}"><span class="meta">Édition n°{i["n"]} · {fr_date(i["date"])}</span>'
        f'<strong>{html.escape(i["subject"])}</strong><span class="ex">{html.escape(i.get("excerpt", ""))}</span></a>'
        for i in issues) or '<p style="color:var(--muted)">La première édition arrive très bientôt.</p>'
    main = (f'<section class="hero"><h1>{html.escape(CFG["name"])}</h1>'
            f'<p class="tag">{html.escape(CFG["tagline"])}</p>{sub_form()}</section>'
            f'<h2 class="sec">Dernières éditions</h2>{cards}')
    (SITE / "index.html").write_text(page(CFG["name"], CFG["tagline"], main), encoding="utf-8")

    base = site_url()
    entries = "".join(
        f"<item><title>{html.escape(i['subject'])}</title><link>{base}/{i['file']}</link>"
        f"<guid>{base}/{i['file']}</guid>"
        f"<pubDate>{email.utils.format_datetime(dt.datetime.fromisoformat(i['date']).replace(tzinfo=dt.timezone.utc))}</pubDate>"
        f"<description>{html.escape(i.get('excerpt', ''))}</description></item>"
        for i in issues[:20])
    (SITE / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{html.escape(CFG['name'])}</title><link>{base}/</link>"
        f"<description>{html.escape(CFG['tagline'])}</description>{entries}</channel></rss>",
        encoding="utf-8")


def publish_site(subject, body_md):
    SITE.mkdir(exist_ok=True)
    today = dt.date.today().isoformat()
    issues = load_issues()
    old = next((i for i in issues if i["date"] == today), None)
    n = old["n"] if old else max([i["n"] for i in issues], default=0) + 1
    ex = excerpt(body_md)
    issues = [i for i in issues if i["date"] != today]
    issues.insert(0, {"n": n, "date": today, "subject": subject, "file": f"{today}.html", "excerpt": ex})
    h = hue(today)
    main = (f'<div class="banner" style="background:linear-gradient(135deg,hsl({h},70%,45%),hsl({(h+55)%360},75%,36%))">'
            f'<span>Édition n°{n}</span><time>{fr_date(today)}</time></div>'
            f'<article><h1>{html.escape(subject)}</h1>{markdown.markdown(body_md)}</article>'
            f'<div class="subbox"><p>Tu veux la prochaine édition dans ta boîte mail ?</p>{sub_form()}</div>'
            if sub_form() else
            f'<div class="banner" style="background:linear-gradient(135deg,hsl({h},70%,45%),hsl({(h+55)%360},75%,36%))">'
            f'<span>Édition n°{n}</span><time>{fr_date(today)}</time></div>'
            f'<article><h1>{html.escape(subject)}</h1>{markdown.markdown(body_md)}</article>')
    (SITE / f"{today}.html").write_text(page(subject, ex, main, "article"), encoding="utf-8")
    INDEX.write_text(json.dumps(issues, ensure_ascii=False, indent=1), encoding="utf-8")
    render_index(issues)
    return f"{site_url()}/{today}.html" if site_url() else ""


# ---------- 4. Email ----------
def send_email(subject, body_md, url):
    key = os.getenv("BUTTONDOWN_API_KEY")
    if not (CFG["email"]["enabled"] and key):
        print("Email : ignoré (clé Buttondown non configurée).")
        return
    status = "about_to_send" if CFG["email"]["auto_send"] else "draft"
    body = (f"[Lire en ligne]({url})\n\n" if url else "") + body_md
    try:
        r = requests.post("https://api.buttondown.email/v1/emails",
                          headers={"Authorization": f"Token {key}"},
                          json={"subject": subject, "body": body, "status": status}, timeout=30)
        print(f"Email : Buttondown a répondu {r.status_code} (statut {status}).")
        if r.status_code >= 300:
            print(r.text[:300])
    except Exception as e:
        print(f"Email : échec ({e}). Le site, lui, est bien publié.")


# ---------- Programme principal ----------
def main():
    SITE.mkdir(exist_ok=True)
    items = collect()
    if len(items) < CFG["min_items"]:
        print(f"Seulement {len(items)} article(s) neuf(s) : pas d'édition aujourd'hui.")
        render_index(load_issues())
        return 0
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ERREUR : le secret ANTHROPIC_API_KEY est absent de GitHub (étape 6 du guide).")
    subject, body = write_issue(items)
    body = monetize(body)
    url = publish_site(subject, body)
    send_email(subject, body, url)
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    seen.update(i["link"] for i in items)
    STATE.write_text(json.dumps(sorted(seen)[-3000:]))
    print("Édition publiée :", subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
