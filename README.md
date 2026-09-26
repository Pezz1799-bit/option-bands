# Option Bands v2 — Massive Basic gratuito

Novità:
- Posizione IV in sigma
- storico giornaliero SQLite
- endpoint /api/history/<ticker>
- screener sotto il grafico basato sugli ultimi valori salvati
- IV/delta calcolati localmente, senza Option Chain Snapshot

Nota importante: su Render Free il filesystem locale è effimero. SQLite è utile per testare, ma per conservare davvero lo storico tra restart/deploy serve un database persistente (es. Postgres).
