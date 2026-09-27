# Option Bands V4

- Massive Options Starter Option Chain Snapshot
- Put/Call reali più vicine a 25 delta
- Bande asimmetriche ±1σ / ±2σ
- Motore di scansione Nasdaq + NYSE (azioni ordinarie / common stock)
- Filtro screener ≤−2σ, ≤−1σ, ≥+1σ, ≥+2σ
- Storico box salvati
- Indicatore sotto il prezzo: posizione IV in sigma rispetto al box precedente

## Nota importante sul market cap
Il motore V4 scansiona le common stock Nasdaq/NYSE. Il filtro automatico market cap >= $1B richiede un dato fondamentale per ogni società; con Stocks Basic non è efficiente interrogare migliaia di Ticker Overview ad ogni scansione. Possiamo aggiungere una cache dell'universo market-cap o usare un piano/dataset stocks adatto al bulk screening.

## Persistenza
Su Render free `/tmp` non è persistente. Per conservare mesi/anni di storico, impostare un database persistente (prossimo step consigliato).
