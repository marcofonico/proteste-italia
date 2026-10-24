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
import urllib.parse
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


def feed_urls():
    for f in CFG["feeds"]:
        yield f["url"], f["name"], f.get("type", "Online")
    for q in CFG["google_news_queries"]:
        u = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:7d", "hl": "it", "gl": "IT", "ceid": "IT:it"})
        yield u, None, "Online"


def collect():
    items, seen = [], set()
    for url, name, typ in feed_urls():
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (radar-proteste)")
        except Exception as e:  # un feed rotto non deve fermare tutto
            print("ERRORE", url, e, file=sys.stderr)
            continue
        if not feed.entries:
            print("vuoto:", url, file=sys.stderr)
        for e in feed.entries:
            title = clean(e.get("title"))
            outlet = name or (e.get("source") or {}).get("title") or ""
            if not name and outlet and title.endswith(" - " + outlet):
                title = title[: -len(outlet) - 3]
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
                          "pub": pub, "date": date_in_title(title, pub)})
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
    actors = sorted({a.upper() if a.isupper() else a for a in re.findall(ACTORS, text)}, key=str.lower)
    first = sorted(its, key=lambda i: (i["type"] != "Agenzia", len(i["title"])))[0]
    names = sorted({i["outlet"] for i in its})
    return {"d": c["date"].isoformat(), "t": etype, "h": theme, "r": region, "l": place,
            "a": ", ".join(actors) or "da verificare", "x": first["title"],
            "m": list(dict.fromkeys(reasons))[:6] or ["da verificare"],
            "o": (first["summary"][:240] or "Da verificare sulle fonti."),
            "s": [src_index(n, next(i for i in its if i["outlet"] == n)) for n in names], "auto": True}


def main():
    sources = [list(s) for s in MANUAL["sources"]]
    idx = {s[0]: k for k, s in enumerate(sources)}

    def src_index(name, item):
        if name not in idx:
            idx[name] = len(sources)
            sources.append([name, item["type"], item["url"]])
        return idx[name]

    events = list(MANUAL["events"])
    cutoff = (TODAY - dt.timedelta(days=KEEP_DAYS)).isoformat()
    items = collect()
    print(f"{len(items)} titoli pertinenti")
    for c in cluster(items):
        ev = to_event(c, src_index)
        twin = next((m for m in events if m["d"] == ev["d"] and len(tokens(m["x"]) & tokens(ev["x"])) >= 2), None)
        if twin:  # evento già curato a mano: aggiungi solo le fonti
            twin["s"] = sorted(set(twin["s"]) | set(ev["s"]))
        else:
            events.append(ev)
    events = [e for e in events if e["d"] >= cutoff]
    events.sort(key=lambda e: e["d"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": dt.datetime.now().isoformat(timespec="minutes"),
                               "sources": sources, "events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"scritto {OUT}: {len(events)} eventi, {len(sources)} fonti")


if __name__ == "__main__":
    main()
