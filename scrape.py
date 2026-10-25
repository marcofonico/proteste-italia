#!/usr/bin/env python3
"""Radar proteste: raccoglie titoli da feed RSS, li raggruppa in eventi e scrive docs/data.json.

Usa solo titoli, link e brevi sommari esposti dai feed (nessuna copia di articoli).
Le classificazioni sono automatiche: ogni evento ha "auto": true e va verificato.
"""
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

try:
    import feedparser
except ImportError:
    sys.exit("Installa le dipendenze: pip install -r requirements.txt")

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
MANUAL = json.loads((ROOT / "manual.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "data.json"
TODAY = dt.date.today()
KEEP_DAYS = 120          # eventi più vecchi vengono scartati
MIN_OVERLAP = 3          # parole significative in comune per unire due titoli
MESI = "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split()

KW = re.compile(r"sciopero|scioperi|corteo|cortei|manifestazion|presidio|picchett|blocco stradale|blocco ferroviario|bloccat[oi]|occupazion|occupat[oi]|mobilitazion|protesta|proteste|manifestanti", re.I)
NOISE = re.compile(r"sagra|concerto|gara ciclistica|sciopero della fame|oroscopo|calciomercato", re.I)
TYPES = [("Occupazione", r"occupa"), ("Sciopero", r"sciopero|scioperi"), ("Blocco", r"blocc|picchett"),
         ("Corteo", r"corteo|cortei"), ("Manifestazione", r"manifest|presidio|protest|mobilitaz")]
THEMES = {
    "Lavoro": r"lavorator|operai|cassa integrazione|ex ilva|stabilimento|contratto|salari|licenziament|sindacat|vertenza",
    "Scuola e università": r"student|scuola|universit|docenti|ateneo|atenei",
    "Diritti e libertà": r"diritto di manifest|decreto sicurezza|libertà|repressione|costituzione|diritti",
    "Costo della vita": r"carovita|affitt|bollette|prezzi|caro vita",
    "Pace e riarmo": r"gaza|palestin|riarmo|guerra|ucraina|armi|pace",
    "Sfruttamento": r"caporalato|braccianti|sfruttamento|lavoro nero|rider",
    "Cultura e spettacolo": r"teatri|spettacolo|cinema|cultura",
    "Ambiente e territorio": r"clima|ambient|ultima generazione|extinction|no tav|ponte sullo stretto|trattori|agricol",
    "Migrazioni e accoglienza": r"migrant|cpr|accoglienza|sbarch|ius scholae|richiedenti asilo",
}
REGIONS = {
    "Piemonte": r"torino|alessandria|asti|cuneo|alba|novara|vercelli|biella|piemonte",
    "Valle d'Aosta": r"aosta|valle d'aosta",
    "Lombardia": r"milano|bergamo|brescia|como|cremona|mantova|pavia|varese|lecco|monza|lombardia",
    "Trentino-Alto Adige": r"trento|bolzano|trentino|alto adige",
    "Veneto": r"venezia|verona|padova|vicenza|treviso|belluno|rovigo|mestre|veneto",
    "Friuli-Venezia Giulia": r"trieste|udine|pordenone|gorizia|porcia|friuli",
    "Liguria": r"genova|savona|la spezia|imperia|liguria",
    "Emilia-Romagna": r"bologna|modena|parma|reggio emilia|ferrara|ravenna|forlì|cesena|rimini|piacenza|emilia",
    "Toscana": r"firenze|pisa|livorno|siena|arezzo|lucca|massa|carrara|prato|toscana",
    "Umbria": r"perugia|terni|umbria",
    "Marche": r"ancona|pesaro|macerata|ascoli|marche",
    "Lazio": r"roma|latina|frosinone|viterbo|rieti|lazio",
    "Abruzzo": r"pescara|chieti|l'aquila|teramo|abruzzo",
    "Molise": r"campobasso|isernia|molise",
    "Campania": r"napoli|salerno|caserta|avellino|benevento|campania",
    "Puglia": r"bari|taranto|lecce|brindisi|foggia|puglia",
    "Basilicata": r"potenza|matera|basilicata",
    "Calabria": r"catanzaro|cosenza|reggio calabria|crotone|calabria",
    "Sicilia": r"palermo|catania|messina|siracusa|trapani|sicilia",
    "Sardegna": r"cagliari|sassari|nuoro|sardegna",
}
TOWN = re.compile(r"\b(?:a|ad)\s+([A-ZÀ-Ý][\wà-ÿ'’]+(?:\s+[A-ZÀ-Ý][\wà-ÿ'’]+)?)")
TOWN_STOP = {"Natale", "Pasqua", "Capodanno", "Ferragosto", "Palazzo", "Piazza", "Via", "Ministero", "Stazione", "Bruxelles"}
ACTORS = r"CGIL|CISL|UIL|USB|CUB|Cobas|USI-CIT|FIOM|FIM|UILM|FLC|SISA|Unicobas|Libera|Arci|Caritas|Ultima Generazione|Extinction Rebellion|studenti|lavoratori|agricoltori|riders?"
STOP = set("della delle degli dello alla alle agli sulla sulle dopo anni tutti sono nuovo nuova come tra fra per con che non una uno gli dei del dal dai nel nei sui sul più già ancora verso contro oggi ieri domani italia italiano italiane".split())


def tokens(text):
    return {w for w in re.findall(r"[a-zàèéìòù0-9]{4,}", text.lower()) if w not in STOP}


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_date(entry):
    p = entry.get("published_parsed") or entry.get("updated_parsed")
    return dt.date(*p[:3]) if p else TODAY


def date_in_title(text, pub):
    """Se il testo cita 'il 30 ottobre', usa quella data (anno dedotto dalla pubblicazione)."""
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MESI) + r")\b", text.lower())
    if not m:
        return pub
    day, mon = int(m.group(1)), MESI.index(m.group(2)) + 1
    for year in (pub.year, pub.year + 1):
        try:
            d = dt.date(year, mon, day)
        except ValueError:
            return pub
        if d >= pub - dt.timedelta(days=30):
            return d
    return pub


GQ = "(sciopero OR corteo OR manifestazione OR presidio OR occupazione OR protesta OR blocco)"


def gnews(q):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q + " when:7d", "hl": "it", "gl": "IT", "ceid": "IT:it"})


def feed_urls():
    """(url, nome testata o None, tipo, (regione, città) per le testate locali)."""
    for f in CFG["feeds"]:
        yield f["url"], f["name"], f.get("type", "Online"), None
    for r in CFG.get("regional", []):
        yield gnews(f"{GQ} site:{r['domain']}"), r["name"], "Quotidiano locale", (r["region"], r["city"])
    for r in CFG.get("extra", []):
        yield gnews(f"{GQ} site:{r['domain']}"), r["name"], r["type"], None
    for q in CFG["google_news_queries"]:
        yield gnews(q), None, "Online", None


def collect():
    items, seen = [], set()
    for url, name, typ, hint in feed_urls():
        if "news.google.com" in url:
            time.sleep(1)  # gentilezza verso Google News
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (radar-proteste)")
        except Exception as e:  # un feed rotto non deve fermare tutto
            print("ERRORE", url, e, file=sys.stderr)
            continue
        if not feed.entries:
            print("vuoto:", url, file=sys.stderr)
        for e in feed.entries:
            title = clean(e.get("title"))
            st = (e.get("source") or {}).get("title") or ""
            if st and title.endswith(" - " + st):
                title = title[: -len(st) - 3]
            outlet = name or st
            summary = clean(e.get("summary"))
            if not title or not outlet or not KW.search(title + " " + summary[:200]) or NOISE.search(title):
                continue
            key = (outlet, title.lower())
            if key in seen:
                continue
            seen.add(key)
            pub = parse_date(e)
            items.append({"title": title, "outlet": outlet, "type": typ, "url": e.get("link", ""),
                          "summary": summary if summary.lower() != title.lower() else "",
                          "pub": pub, "date": date_in_title(title, pub), "hint": hint})
    return items


def region_of(text):
    t = text.lower()
    for reg, rx in REGIONS.items():
        m = re.search(r"\b(" + rx + r")\b", t)
        if m:
            return reg, m.group(1).title()
    return "Nazionale", "Italia"


def cluster(items):
    clusters = []
    for it in sorted(items, key=lambda i: i["date"]):
        it["tok"] = tokens(it["title"])
        for c in clusters:
            if abs((c["date"] - it["date"]).days) <= 2 and len(c["tok"] & it["tok"]) >= MIN_OVERLAP:
                c["items"].append(it)
                c["tok"] |= it["tok"]
                break
        else:
            clusters.append({"date": it["date"], "tok": set(it["tok"]), "items": [it]})
    return clusters


def to_event(c, src_index):
    its = c["items"]
    text = " ".join(i["title"] + " " + i["summary"] for i in its)
    low = text.lower()
    etype = next((n for n, rx in TYPES if re.search(rx, low)), "Manifestazione")
    theme, reasons = "Diritti e libertà", []
    best = 0
    for name, rx in THEMES.items():
        found = re.findall(rx, low)
        if len(found) > best:
            best, theme, reasons = len(found), name, found
    region, place = region_of(text)
    if place == "Italia":  # comune non in elenco: prova a leggerlo da «a/ad <Località>» nei titoli
        m = TOWN.search(" ".join(i["title"] for i in its))
        if m and m.group(1).split()[0] not in TOWN_STOP:
            place = m.group(1)
    # se tutte le fonti sono quotidiani locali della stessa regione e il testo non parla di
    # una mobilitazione nazionale, assegna l'evento a quella regione
    hints = {i["hint"] for i in its}
    if region == "Nazionale" and len(hints) == 1 and None not in hints and not re.search(r"generale|nazionale|tutta italia", low):
        region, place = next(iter(hints))
    actors = sorted({a.upper() if a.isupper() else a for a in re.findall(ACTORS, text)}, key=str.lower)
    first = sorted(its, key=lambda i: (i["type"] != "Agenzia", len(i["title"])))[0]
    names = sorted({i["outlet"] for i in its})
    return {"d": c["date"].isoformat(), "t": etype, "h": theme, "r": region, "l": place,
            "a": ", ".join(actors) or "da verificare", "x": first["title"],
            "m": list(dict.fromkeys(reasons))[:6] or ["da verificare"],
            "o": (first["summary"][:240] or "Da verificare sulle fonti."),
            "s": [src_index(n, next(i for i in its if i["outlet"] == n)) for n in names], "auto": True,
            "news": [{"t": i["title"], "o": i["outlet"], "u": i["url"], "d": i["pub"].isoformat()} for i in its]}


G = json.loads((ROOT / "glossary.json").read_text(encoding="utf-8"))


def _norm(t):
    t = t.lower().replace("’", "'").strip()
    return G["alias"].get(t, t)


def build_terms(events):
    """Termini significativi per evento: frasi curate, ragioni, attori, parole ricorrenti e bigrammi frequenti."""
    stop, excl = set(G["stopterms"]), set(G["escludi_singole"])
    att, vert, frasi = set(G["attori"]), set(G["vertenze"]), [p.lower() for p in G["frasi"]]
    words = [[_norm(w) for w in re.findall(r"[a-zàèéìòù0-9'’-]{4,}", e["x"].lower())] for e in events]
    ok = lambda w: w not in stop and w not in excl
    freq = Counter(w for ws in words for w in set(ws) if ok(w))
    big = Counter(b for ws in words for b in {f"{a} {c}" for a, c in zip(ws, ws[1:]) if ok(a) and ok(c)})
    for i, e in enumerate(events):
        text = (e["x"] + " " + e["a"]).lower().replace("’", "'")
        S = {_norm(p) for p in frasi if p in text} | {_norm(m) for m in e["m"]}
        S |= {a for a in att | vert if re.search(r"\b" + re.escape(a) + r"\b", text)}
        S |= {w for w in set(words[i]) if ok(w) and freq[w] >= 2}
        S |= {f"{a} {c}" for a, c in zip(words[i], words[i][1:]) if big[f"{a} {c}"] >= 2}
        place = set(re.findall(r"[a-zàèéìòù']{3,}", (e["l"] + " " + e["r"]).lower()))
        S = {t for t in S if t and t not in stop and t not in place and not (" " not in t and t in excl) and (len(t) >= 4 or t in att)}
        S = {t for t in S if not any(t != u and re.search(r"\b" + re.escape(t) + r"\b", u) for u in S)}
        e["terms"] = sorted([t, "attori" if t in att else "vertenze" if t in vert else "temi"] for t in S)


def assign_vicende(events):
    """Raggruppa gli eventi in vicende (regole del glossario); gli altri formano una vicenda a sé."""
    rules = [(r, re.compile(r["rx"], re.I)) for r in G["vicende"]]
    titles = {}
    for e in events:
        text = " ".join([e["x"], *e["m"], e["a"]])
        for r, rx in rules:
            if rx.search(text) and r.get("from", "0") <= e["d"] <= r.get("to", "9"):
                e["v"] = r["id"]
                titles[r["id"]] = r["t"]
                break
        else:
            e["v"] = "e-" + re.sub(r"[^a-z0-9]+", "-", (e["d"] + " " + e["x"]).lower())[:60]
            titles[e["v"]] = e["x"]
    return [{"id": k, "t": v} for k, v in titles.items()]


def main():
    sources = [list(s) for s in MANUAL["sources"]]
    idx = {s[0].lower(): k for k, s in enumerate(sources)}

    def add(name, typ, url):
        if name.lower() not in idx:
            idx[name.lower()] = len(sources)
            sources.append([name, typ, url])

    # il catalogo mostra tutte le testate monitorate, anche quelle senza eventi
    for f in CFG["feeds"]:
        u = urllib.parse.urlparse(f["url"])
        add(f["name"], f.get("type", "Online"), f"{u.scheme}://{u.netloc}")
    for r in CFG.get("regional", []):
        add(r["name"], f"Locale · {r['city']}", "https://" + r["domain"])
    for r in CFG.get("extra", []):
        add(r["name"], r["type"], "https://" + r["domain"])
    # il catalogo non si restringe mai: si mantengono le fonti già scoperte nelle esecuzioni precedenti
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    prev_src = prev.get("sources", [])
    for ps in prev_src:
        add(ps[0], ps[1], ps[2])

    def src_index(name, item):
        if name.lower() not in idx:
            idx[name.lower()] = len(sources)
            sources.append([name, item["type"], item["url"]])
        return idx[name.lower()]

    events = list(MANUAL["events"])
    cutoff = (TODAY - dt.timedelta(days=KEEP_DAYS)).isoformat()
    items = collect()
    print(f"{len(items)} titoli pertinenti")
    for c in cluster(items):
        ev = to_event(c, src_index)
        twin = next((m for m in events if m["d"] == ev["d"] and len(tokens(m["x"]) & tokens(ev["x"])) >= 2), None)
        if twin:  # evento già curato a mano: aggiungi solo le fonti
            twin["s"] = sorted(set(twin["s"]) | set(ev["s"]))
            twin["news"] = twin.get("news", []) + ev["news"]
        else:
            events.append(ev)
    # gli eventi automatici delle esecuzioni precedenti restano finché non scadono (KEEP_DAYS)
    for pe in prev.get("events", []):
        if not pe.get("auto") or pe["d"] < cutoff:
            continue
        if any(m["d"] == pe["d"] and len(tokens(m["x"]) & tokens(pe["x"])) >= 2 for m in events):
            continue
        pe["s"] = sorted({idx[prev_src[i][0].lower()] for i in pe["s"] if i < len(prev_src)})
        if pe["s"]:
            events.append(pe)
    events = [e for e in events if e["d"] >= cutoff]
    events.sort(key=lambda e: e["d"])
    build_terms(events)
    # pannello stabile: fonti monitorate con continuità (denominatore della «copertura sulle fonti attese»)
    panel = {"nazionali": list(dict.fromkeys(f["name"].split(" · ")[0] for f in CFG["feeds"])),
             "locali": [{"n": r["name"], "r": r["region"], "c": r["city"]} for r in CFG.get("regional", [])]}
    vicende = assign_vicende(events)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": dt.datetime.now().isoformat(timespec="minutes"),
                               "sources": sources, "panel": panel, "vicende": vicende, "events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"scritto {OUT}: {len(events)} eventi, {len(sources)} fonti")


if __name__ == "__main__":
    main()
