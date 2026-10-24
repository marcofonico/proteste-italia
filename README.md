# Radar proteste Italia: sito pubblico

Struttura
- `scrape.py`: legge i feed, filtra i titoli su scioperi/cortei/blocchi/occupazioni, li raggruppa in eventi e scrive `docs/data.json`
- `sources.json`: feed RSS e query Google News (modificabili; i feed cambiano URL, controllali ogni tanto)
- `manual.json`: eventi curati a mano e fonti di partenza (hanno la precedenza; i risultati automatici vi aggiungono solo fonti)
- `docs/index.html`: la dashboard (legge `data.json`, nessun backend)
- `.github/workflows/update.yml`: esegue lo script ogni 3 ore e fa commit dei dati

## Pubblicazione gratuita con GitHub Pages
1. Crea un repository su GitHub e carica tutti questi file (cartella `.github` inclusa).
2. Settings → Pages → Source: *Deploy from a branch* → branch `main`, cartella `/docs`.
3. Settings → Actions → General → Workflow permissions: *Read and write*.
4. Tab Actions → «Aggiorna dati» → *Run workflow* (poi parte da solo ogni 3 ore).
Il sito sarà su `https://<utente>.github.io/<repo>/`. Alternative: Netlify o Cloudflare Pages puntando alla cartella `docs`.

## Prova in locale
    pip install -r requirements.txt
    python scrape.py
    python -m http.server -d docs      # poi apri http://localhost:8000

## Curare i dati
- Correggi o aggiungi eventi in `manual.json` (stessi campi di `data.json`, `s` = indici in `sources`).
- Gli eventi con tag «auto» sono classificati da regole semplici (tipo, tema, regione, attori): vanno verificati.
- Soglie utili in `scrape.py`: `MIN_OVERLAP` (quanto devono somigliare due titoli per essere lo stesso evento) e `KEEP_DAYS`.

## Note legali e di correttezza
- Vengono usati solo titoli, link e sommari brevi dei feed, con rimando alla fonte: non copiare articoli.
- Controlla i termini d'uso dei singoli feed e di Google News prima di un uso commerciale.
- Prima di usare un evento in un video, confermalo su almeno due fonti indipendenti.
