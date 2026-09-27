# Option Bands V3 — Massive Options Starter

Versione per il piano Massive Options Starter ($29/mese).

## Novità
- Option Chain Snapshot `/v3/snapshot/options/{ticker}`.
- Put reale più vicina a delta -0,25 e Call reale più vicina a delta +0,25.
- IV e Greeks letti direttamente dallo snapshot Massive.
- Bande asimmetriche ±1σ / ±2σ.
- Salvataggio del box giornaliero e posizione IV rispetto al box fissato quel giorno.
- Pulsante Storico per sovrapporre i box già salvati.
- Screener dei titoli già analizzati/salvati, ordinato dalla posizione σ più negativa.

## Render
Mantieni `MASSIVE_API_KEY` nelle Environment Variables di Render. Non inserirla nel codice o su GitHub.

## Nota database
Di default usa `/tmp/option_bands.db`. Su Render Free questo archivio non è persistente ai redeploy/riavvii: per uno storico serio va collegato un database persistente.

## Nota screener
Questa V3 usa già i dati Options Starter corretti, ma lo screener mostra i titoli che sono stati analizzati e salvati. La scansione automatica dell'intero universo NYSE/Nasdaq ≥ $1B richiede il prossimo modulo batch/universe.
