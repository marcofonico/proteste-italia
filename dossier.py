#!/usr/bin/env python3
"""Dossier: approfondimenti su eventi di forte impatto.

Per ogni dossier definito in dossiers.json:
  1. raccoglie i titoli pertinenti (da quelli già letti da scrape.py) e li accumula nel tempo;
  2. dove la pagina è liberamente leggibile e il sito lo consente (robots.txt), scarica il testo
     e lo analizza con regole a dizionario (codebook.json);
  3. scrive docs/dossier/<id>.json con SOLO gli esiti dell'analisi, link alla fonte e brevi
     estratti che giustificano ogni risultato. Il testo integrale non viene mai salvato.
Gratuito: nessuna API a pagamento. Ogni risultato è «sperimentale» finché non viene validato a mano
(vedi admin.html).
"""
import base64
import datetime as dt
import hashlib
import html as htmllib
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
OUTDIR = ROOT / "docs" / "dossier"
CFGF = ROOT / "dossiers.json"
CB = json.loads((ROOT / "codebook.json").read_text(encoding="utf-8"))
CV = CB["versione"]
UA = "RadarProteste/1.0 (+https://proteste.marcofonico.com)"
MAX_FETCH = 40      # download per esecuzione (il resto alla successiva)
DELAY = 1.5         # secondi tra due download
TIMEOUT = 15
MIN_CHARS = 700     # sotto questa lunghezza il testo è considerato incompleto (paywall o pagina vuota)
MAX_TENT = 3        # tentativi per articolo
PAYWALL = re.compile(r"abbonati|abbonamento|accedi per (?:leggere|continuare)|contenuto riservato|riservato agli abbonati|registrati per", re.I)
NEG = re.compile(r"(?:senza|nessun\w*|niente|evitat\w*|scongiurat\w*)\s+(?:\w+\s+){0,2}$")


# ---------- dizionari ----------
def term_rx(t):
    t = t.lower().replace("’", "'")
    p = re.escape(t).replace(r"\*", r"\w*").replace(r"\ ", r"\s+").replace("'", "['’]")
    return re.compile(r"(?<!\w)" + p + r"(?!\w)", re.I)


def build_families(G):
    ext = CB.get("estensioni", {})
    fams = []
    for f in G["etichette"]:
        fams.append({"id": f["id"], "n": f["n"], "d": f["d"], "c": f["c"], "base": True,
                     "rx_base": [term_rx(t) for t in f["t"]],
                     "rx": [term_rx(t) for t in f["t"] + ext.get(f["id"], [])]})
    for f in CB.get("famiglie_testo", []):
        rx = [term_rx(t) for t in f["t"]]
        fams.append({"id": f["id"], "n": f["n"], "d": f["d"], "c": f["c"], "base": False, "rx_base": [], "rx": rx})
    return fams


def hits(s_low, rxs):
    """(posizione, testo) delle parole trovate, escluse quelle negate («senza scontri»)."""
    out = []
    for rx in rxs:
        for m in rx.finditer(s_low):
            if m.end() > m.start() and not NEG.search(s_low[max(0, m.start() - 32):m.start()]):
                out.append((m.start(), m.group(0)))
    return sorted(out)


VERB_RX = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(v).replace(r"\ ", r"\s+") for v in sorted(CB["verbi_dire"], key=len, reverse=True)) + r")(?!\w)", re.I)
SECONDO_RX = re.compile(r"(?<!\w)secondo\b", re.I)
QUOTE_RX = re.compile(r"«[^»]*»|“[^”]*”|\"[^\"]*\"")
VOCI = [(v["id"], [term_rx(t) for t in v["t"]]) for v in CB["voci"]]
ATTR = [(k, [term_rx(t) for t in v]) for k, v in CB["attribuzione_cifre"].items()]


# ---------- testo ----------
def snippet(s, pos=0, w=200):
    s = " ".join(s.split())
    if len(s) <= w:
        return s
    pos = min(pos, len(s) - 1)
    a = max(0, pos - w // 2)
    b = min(len(s), a + w)
    a = max(0, b - w)
    return ("…" if a else "") + s[a:b].strip() + ("…" if b < len(s) else "")


def split_sentences(text):
    out = []
    for pi, p in enumerate([p.strip() for p in re.split(r"\n+", text) if p.strip()]):
        for s in re.split(r"(?<=[\.\!\?…])\s+(?=[A-ZÀ-ÝÈ«“\"'0-9])", p):
            s = s.strip()
            if len(s) >= 25:
                out.append((pi, s))
    return out


def lead_set(sents):
    if not sents:
        return set()
    if max(pi for pi, _ in sents) == 0:
        return set(range(min(3, len(sents))))
    return {i for i, (pi, _) in enumerate(sents) if pi < 2}


def voci_of(sent):
    """Chi parla: cerca un verbo del dire e il soggetto più vicino (fuori dalle virgolette) oppure «secondo X»."""
    low = sent.lower().replace("’", "'")
    quotes = [(m.start(), m.end()) for m in QUOTE_RX.finditer(sent)]
    has_q = bool(quotes)
    inq = lambda p: any(a <= p < b for a, b in quotes)
    spk = []
    for cat, rxs in VOCI:
        for rx in rxs:
            for m in rx.finditer(low):
                if not inq(m.start()):
                    spk.append((m.start(), m.end(), cat))
    found = {}
    for v in VERB_RX.finditer(low):
        if inq(v.start()):
            continue
        left = [s for s in spk if s[1] <= v.start() and v.start() - s[1] <= 80]
        if left:
            s = max(left, key=lambda x: x[1])
            d = v.start() - s[1]
            conf = "alta" if has_q and d <= 40 else "media"
        else:
            right = [s for s in spk if s[0] >= v.end() and s[0] - v.end() <= 40]
            if not right:
                continue
            s = min(right, key=lambda x: x[0])
            conf = "alta" if has_q else "media"
        if s[2] not in found or conf == "alta":
            found[s[2]] = (conf, v.start())
    for m in SECONDO_RX.finditer(low):
        c = [s for s in spk if m.end() <= s[0] <= m.end() + 35]
        if c:
            s = min(c, key=lambda x: x[0])
            found[s[2]] = ("alta", m.start())
    return [(cat, conf, snippet(sent, pos)) for cat, (conf, pos) in found.items()]


NUM = r"(\d{1,3}(?:[\.  ]\d{3})+|\d+(?:,\d+)?)\s*(mila|milioni|milione|mln)?"
UNIT = r"(persone|manifestanti|partecipanti|lavoratori|studenti|aderenti|presenti|scioperanti|in piazza|in corteo|in strada)"
CIF_RX = re.compile(NUM + r"\s*" + UNIT + r"(?!\w)", re.I)
REV_RX = re.compile(r"(manifestanti|partecipanti|persone|presenti|aderenti)\s+(?:erano|sono|sarebbero|stimati|circa|in)\s+(?:circa\s+)?" + NUM + r"(?!\w)", re.I)
CTX_RX = re.compile(r"corteo|piazza|manifest|adesion|partecip|sciopero|presidio|in strada|sfilat|aderit|mobilit", re.I)
ADE_RX = re.compile(r"adesion\w*[^%\d]{0,40}?(\d{1,3}(?:,\d+)?)\s*(?:%|per cento)", re.I)
FONTE_RX = re.compile(r"secondo|stima\w*|dati|fonti|per (?:la|il|gli|i)\b", re.I)


def parse_num(n, m):
    n = n.replace(" ", "").replace(" ", "")
    v = float(n.replace(".", "")) if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", n) else float(n.replace(",", "."))
    mult = 1000 if m and m.lower() == "mila" else 1_000_000 if m else 1
    return int(v * mult)


CUT_RX = re.compile(r",\s*(?:mentre|ma|invece|per|secondo)\b|;")


def attribution(low, pos):
    """Chi fornisce la cifra: il soggetto più vicino nella stessa frase secondaria, di preferenza prima del numero."""
    cuts = list(CUT_RX.finditer(low))
    cs = max([c.end() for c in cuts if c.end() <= pos] or [0])
    ce = min([c.start() for c in cuts if c.start() > pos] or [len(low)])
    cand = []
    for cat, rxs in ATTR:
        for rx in rxs:
            for m in rx.finditer(low):
                if cs <= m.start() < ce:
                    cand.append((m.start() > pos, abs(m.start() - pos), cat, m.start()))
    if not cand:
        return "non attribuita", "bassa"
    _, _, cat, at = min(cand)
    pre = low[max(0, at - 30):at]
    return cat, ("alta" if FONTE_RX.search(pre) or (cs and FONTE_RX.search(low[max(0, cs - 12):cs + 12])) else "media")


def cifre_of(sent):
    low = sent.lower().replace("’", "'")
    if not CTX_RX.search(low):
        return [], []
    out, ade, seen = [], [], set()
    forms = [(m.start(), m.group(1), m.group(2), m.group(3)) for m in CIF_RX.finditer(sent)]
    forms += [(m.start(2), m.group(2), m.group(3), m.group(1)) for m in REV_RX.finditer(sent)]
    for pos, n, mult, unit in forms:
        try:
            v = parse_num(n, mult)
        except ValueError:
            continue
        if v < 50 or (v, pos) in seen:
            continue
        seen.add((v, pos))
        a, conf = attribution(low, pos)
        out.append({"v": v, "u": unit.lower(), "a": a, "conf": conf, "ev": snippet(sent, pos)})
    for m in ADE_RX.finditer(sent):
        a, conf = attribution(low, m.start())
        ade.append({"p": float(m.group(1).replace(",", ".")), "a": a, "conf": conf, "ev": snippet(sent, m.start())})
    return out, ade


def analyze(title, text, fams):
    sents = split_sentences(text)
    n = len(sents)
    lead = lead_set(sents)
    lows = [s.lower().replace("’", "'") for _, s in sents]
    cornici = {}
    for f in fams:
        hs = []
        for i, low in enumerate(lows):
            h = hits(low, f["rx"])
            if h:
                hs.append((i, len(h), h[0][0]))
        k = len(hs)
        inlead = any(i in lead for i, _, _ in hs)
        if k >= 2 or (k >= 1 and inlead):
            top = sorted(hs, key=lambda x: (-x[1], x[0]))[:2]
            cornici[f["id"]] = {"n": k, "s": round(k / max(n, 1), 3), "lead": inlead,
                                "conf": "alta" if k >= 3 else "media" if k == 2 else "bassa",
                                "ev": [snippet(sents[i][1], pos) for i, _, pos in top]}
    dom = max(cornici, key=lambda c: cornici[c]["s"] + (0.05 if cornici[c]["lead"] else 0)) if cornici else ""
    voci = defaultdict(lambda: {"n": 0, "conf": "media", "ev": []})
    for _, s in sents:
        for cat, conf, ev in voci_of(s):
            v = voci[cat]
            v["n"] += 1
            if conf == "alta":
                v["conf"] = "alta"
            if len(v["ev"]) < 2:
                v["ev"].append(ev)
    cif, ade = [], []
    for _, s in sents:
        c, a = cifre_of(s)
        cif += c
        ade += a
    # titolo contro testo: etichette presenti nel titolo (dizionario dei titoli) e assenti nel testo
    tl = title.lower().replace("’", "'")
    ttags = [f["id"] for f in fams if f["base"] and hits(tl, f["rx_base"])]
    scarto = [t for t in ttags if t not in cornici]
    return {"cv": CV, "ok": True, "fr": n, "cornici": cornici, "dom": dom,
            "voci": [{"c": c, **v} for c, v in sorted(voci.items())],
            "cifre": cif[:6], "adesione": ade[:3], "tt": ttags, "scarto": scarto}


# ---------- download ----------
_robots = {}


def allowed(url):
    p = urllib.parse.urlparse(url)
    host = p.scheme + "://" + p.netloc
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(host + "/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                rp.parse(r.read().decode("utf-8", "ignore").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.disallow_all = True
            else:
                rp.parse([])
        except Exception:
            return None  # robots non leggibile: per prudenza non scarico, riproverò
        _robots[host] = rp
    return _robots[host].can_fetch(UA, url)


def resolve_gnews(url):
    """I link di Google News sono reindirizzamenti codificati: provo solo la decodifica semplice."""
    if "news.google.com" not in url:
        return url
    seg = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
    try:
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
        m = re.search(rb"https?://[^\s\"'\x00-\x1f]+", raw)
        return m.group(0).decode() if m else None
    except Exception:
        return None


def simple_extract(page):
    page = re.sub(r"(?is)<(script|style|nav|footer|aside|form|header|noscript)\b.*?</\1>", " ", page)
    m = re.search(r"(?is)<article\b.*?</article>", page)
    body = m.group(0) if m else page
    paras = [htmllib.unescape(re.sub(r"<[^>]+>", " ", p)) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", body)]
    return "\n".join(" ".join(p.split()) for p in paras if len(p.strip()) >= 50)


def extract(page):
    try:
        import trafilatura
        t = trafilatura.extract(page, include_comments=False, include_tables=False, favor_precision=True)
        if t:
            return t
    except Exception:
        pass
    return simple_extract(page)


def fetch_text(url):
    """(testo, motivo): motivo è vuoto se il testo è stato recuperato."""
    real = resolve_gnews(url)
    if not real:
        return None, "link Google News non risolvibile"
    ok = allowed(real)
    if ok is None:
        return None, "robots.txt non leggibile"
    if not ok:
        return None, "vietato dal sito (robots.txt)"
    try:
        req = urllib.request.Request(real, headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(2_500_000)
            cs = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        return None, "accesso negato o a pagamento" if e.code in (401, 402, 403, 451) else "pagina non trovata" if e.code in (404, 410) else f"errore {e.code}"
    except Exception:
        return None, "errore di rete"
    page = raw.decode(cs, "ignore")
    text = extract(page)
    if len(text) < MIN_CHARS:
        return None, "a pagamento o testo incompleto" if PAYWALL.search(page) or text else "testo non estraibile"
    if PAYWALL.search(text) and len(text) < 1800:
        return None, "a pagamento o testo incompleto"
    return text, ""


# ---------- termini dei titoli ----------
def title_terms(arts, G, norm):
    stop, excl = set(G["stopterms"]), set(G["escludi_singole"])
    att, vert, frasi = set(G["attori"]), set(G["vertenze"]), [p.lower() for p in G["frasi"]]
    ok = lambda w: w not in stop and w not in excl
    words = [[norm(w) for w in re.findall(r"[a-zàèéìòù0-9'’-]{4,}", a["t"].lower())] for a in arts]
    freq = Counter(w for ws in words for w in set(ws) if ok(w))
    big = Counter(b for ws in words for b in {f"{a} {c}" for a, c in zip(ws, ws[1:]) if ok(a) and ok(c)})
    kind = {}
    for a, ws in zip(arts, words):
        text = a["t"].lower().replace("’", "'")
        S = {norm(p) for p in frasi if p in text}
        S |= {x for x in att | vert if re.search(r"\b" + re.escape(x) + r"\b", text)}
        S |= {w for w in set(ws) if ok(w) and freq[w] >= 2}
        S |= {f"{x} {y}" for x, y in zip(ws, ws[1:]) if big[f"{x} {y}"] >= 2}
        place = set(re.findall(r"[a-zàèéìòù']{3,}", (a.get("l", "") + " " + a.get("r", "")).lower()))
        S = {t for t in S if t and t not in stop and t not in place and (len(t) >= 4 or t in att)}
        S = {t for t in S if not any(t != u and re.search(r"\b" + re.escape(t) + r"\b", u) for u in S)}
        a["terms"] = sorted(S)
        for t in S:
            kind[t] = "attori" if t in att else "vertenze" if t in vert else "temi"
    return kind


# ---------- dossier ----------
def art_id(it):
    return hashlib.sha1((it["outlet"] + "|" + it["url"]).encode()).hexdigest()[:10]


def covers(cfg, it, today):
    rx = re.compile(cfg["rx"], re.I)
    if not rx.search(it["title"] + " " + it.get("summary", "")):
        return False
    return cfg.get("da", "0000") <= it["pub"].isoformat() <= cfg.get("a", "9999")


def run_one(cfg, items, today, G, region_of, norm, fams):
    OUTDIR.mkdir(parents=True, exist_ok=True)
    path = OUTDIR / f"{cfg['id']}.json"
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    arts = {a["id"]: a for a in prev.get("articoli", [])}
    seen = {(a["o"].lower(), a["t"].lower()) for a in arts.values()}
    if cfg.get("stato") != "chiuso":
        for it in items:
            if not covers(cfg, it, today):
                continue
            aid = art_id(it)
            key = (it["outlet"].lower(), it["title"].lower())
            if aid in arts or key in seen:
                continue
            reg, city = region_of(it["title"] + " " + it.get("summary", ""))
            if reg == "Nazionale" and it.get("hint") and not re.search(r"generale|nazionale|tutta italia", it["title"].lower()):
                reg, city = it["hint"]
            seen.add(key)
            arts[aid] = {"id": aid, "o": it["outlet"], "t": it["title"], "u": it["url"], "d": it["pub"].isoformat(),
                         "r": reg, "l": city}
    giorno = cfg["giorno"]
    for a in arts.values():
        a["f"] = 0 if a["d"] < giorno else 1 if a["d"] == giorno else 2
    # testi: prima gli articoli mai provati, poi quelli da rianalizzare o da ritentare
    todo = sorted((a for a in arts.values() if needs_text(a)), key=lambda a: (a.get("tx", {}).get("tent", 0), a["d"]))
    fetched = 0
    for a in todo:
        if fetched >= MAX_FETCH:
            break
        text, why = fetch_text(a["u"])
        fetched += 1
        time.sleep(DELAY)
        tent = a.get("tx", {}).get("tent", 0) + 1
        if text:
            a["tx"] = analyze(a["t"], text, fams)
            a["tx"]["tent"] = tent
        else:
            a["tx"] = {"cv": CV, "ok": False, "motivo": why, "tent": tent}
    lst = sorted(arts.values(), key=lambda a: (a["d"], a["o"], a["t"]))
    kind = title_terms(lst, G, norm)
    per = defaultdict(lambda: [0, 0])
    why = Counter()
    for a in lst:
        per[a["o"]][0] += 1
        ok = bool(a.get("tx", {}).get("ok"))
        per[a["o"]][1] += ok
        if not ok:
            why[a.get("tx", {}).get("motivo", "non ancora provato")] += 1
    out = {"id": cfg["id"], "titolo": cfg["titolo"], "descrizione": cfg.get("descrizione", ""), "giorno": giorno,
           "da": cfg.get("da", ""), "a": cfg.get("a", ""), "stato": cfg.get("stato", "in corso"),
           "aggiornato": dt.datetime.now().isoformat(timespec="minutes"), "versione_codebook": CV,
           "etichette": [{"id": f["id"], "n": f["n"], "d": f["d"], "c": f["c"], "t": next(e["t"] for e in G["etichette"] if e["id"] == f["id"])} for f in fams if f["base"]],
           "cornici_testo": [{"id": f["id"], "n": f["n"], "d": f["d"], "c": f["c"]} for f in fams],
           "voci_cat": [{"id": v["id"], "n": v["n"], "d": v["d"]} for v in CB["voci"]],
           "copertura": {"titoli": len(lst), "testi": sum(v[1] for v in per.values()), "motivi": dict(why),
                         "testate": {k: v for k, v in sorted(per.items())}},
           "termini": kind, "articoli": lst}
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"dossier {cfg['id']}: {len(lst)} titoli, {out['copertura']['testi']} testi analizzati (scaricati ora: {fetched})")
    return out


def needs_text(a):
    tx = a.get("tx")
    if not tx:
        return True
    if tx.get("cv") != CV:
        return tx.get("ok") or tx.get("tent", 0) < MAX_TENT
    if tx.get("ok"):
        return False
    transient = tx.get("motivo") in ("errore di rete", "robots.txt non leggibile") or str(tx.get("motivo", "")).startswith("errore ")
    return transient and tx.get("tent", 0) < MAX_TENT


def suggestions(events, cfgs, today):
    """Eventi molto coperti non ancora in un dossier: li propone, la decisione resta a chi cura il sito."""
    rxs = [re.compile(c["rx"], re.I) for c in cfgs]
    cutoff = (today - dt.timedelta(days=5)).isoformat()
    groups = defaultdict(list)
    for e in events:
        if e["d"] >= cutoff and e.get("news"):
            groups[e.get("v", e["x"])].append(e)
    out = []
    for vid, es in groups.items():
        news = {n["u"] or n["t"]: n for e in es for n in e["news"]}
        outlets = {n["o"] for n in news.values()}
        regs = {e["r"] for e in es if e["r"] != "Nazionale"}
        if len(outlets) < 5 and len(news) < 8:
            continue
        titles = [n["t"] for n in news.values()]
        if any(sum(bool(rx.search(t)) for t in titles) >= len(titles) / 2 for rx in rxs):
            continue
        main = max(es, key=lambda e: len(e["news"]))
        d0 = min(e["d"] for e in es)
        top = [t for t, _ in Counter(w for t in titles for w in set(re.findall(r"[a-zàèéìòù]{5,}", t.lower()))).most_common(3)]
        out.append({"titolo": main["x"], "titoli": len(news), "testate": len(outlets), "regioni": len(regs),
                    "esempi": titles[:3],
                    "proposta": {"id": re.sub(r"[^a-z0-9]+", "-", (d0 + " " + main["x"]).lower())[:50].strip("-"),
                                 "titolo": main["x"], "descrizione": "", "giorno": main["d"],
                                 "da": (dt.date.fromisoformat(d0) - dt.timedelta(days=3)).isoformat(),
                                 "a": (dt.date.fromisoformat(d0) + dt.timedelta(days=14)).isoformat(),
                                 "stato": "in corso", "rx": "|".join(top)}})
    out.sort(key=lambda s: -s["titoli"])
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "suggerimenti.json").write_text(
        json.dumps({"aggiornato": dt.datetime.now().isoformat(timespec="minutes"), "suggerimenti": out[:8]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(out)} possibili nuovi dossier")


def run(items, events, today, G, region_of, norm):
    try:
        cfgs = json.loads(CFGF.read_text(encoding="utf-8"))
    except Exception:
        cfgs = []
    fams = build_families(G)
    for cfg in cfgs:
        try:
            run_one(cfg, items, today, G, region_of, norm, fams)
        except Exception as e:  # un dossier rotto non deve fermare l'aggiornamento dei dati
            print("ERRORE dossier", cfg.get("id"), e, file=sys.stderr)
    try:
        suggestions(events, cfgs, today)
    except Exception as e:
        print("ERRORE suggerimenti", e, file=sys.stderr)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "codebook.json", OUTDIR / "codebook.json")  # pubblico: le regole sono trasparenti
    (OUTDIR / "indice.json").write_text(json.dumps(
        [{"id": c["id"], "titolo": c["titolo"], "giorno": c["giorno"], "stato": c.get("stato", "in corso")} for c in cfgs],
        ensure_ascii=False, indent=1), encoding="utf-8")
