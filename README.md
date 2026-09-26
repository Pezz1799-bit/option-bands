
# Option Bands

Web app per azioni NYSE/Nasdaq con market cap >= $1 miliardo.
Mostra candele giornaliere e bande asimmetriche derivate dalla IV di Put/Call circa 25-delta.

## Sicurezza
NON inserire la API key nel codice. La chiave già condivisa in chat va revocata/rigenerata.
Usa la nuova chiave come variabile d'ambiente MASSIVE_API_KEY.

## Avvio locale
1. Installa Python 3.11+
2. `pip install -r requirements.txt`
3. Windows PowerShell: `$env:MASSIVE_API_KEY="LA_TUA_NUOVA_CHIAVE"`
4. `python app.py`
5. Apri http://127.0.0.1:5000

## Pubblicazione su Render
1. Carica questa cartella in un repository GitHub.
2. Su Render: New > Web Service > collega il repository.
3. Il file render.yaml contiene già build/start.
4. In Environment aggiungi MASSIVE_API_KEY con la nuova chiave.
5. Deploy.

## Dati Massive
- Prezzi: daily aggregates.
- Anagrafica: ticker overview (exchange + market cap).
- Opzioni: Option Chain Snapshot (IV, Greeks, OI).
- La disponibilità degli endpoint dipende dal piano Massive.

## Universo
Ogni ticker aperto/ricercato viene verificato lato server:
- market = stocks
- primary_exchange = XNAS o XNYS
- market_cap >= 1,000,000,000 USD

Questo evita che l'interfaccia mostri bande per titoli fuori dal requisito.
