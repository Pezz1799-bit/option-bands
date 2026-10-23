# Option Bands V6.2 — IV Term Structure

Applicazione Flask con dati Massive. Avvio: `pip install -r requirements.txt`, configurare `MASSIVE_API_KEY`, poi `gunicorn app:app --timeout 180`. Per sviluppo: `python app.py`.

## Modifiche
- Rimossa la sezione indici SPX/NDX 0DTE e il relativo endpoint.
- IV Term Structure sotto il volatility skew: scadenze da 1 a 365 giorni, asse X in giorni di calendario, asse Y in IV annualizzata.
- IV ATM: interpolazione lineare allo stesso prezzo di riferimento delle IV call e put, poi media delle due. Sono richiesti strike che comprendano lo spot, entro ±10%, su entrambi i lati; dati mancanti non vengono inventati.
- Etichetta Contango / Backwardation / Piatta / Mista, sulla sequenza delle scadenze disponibili, con tolleranza di 0,1 punti percentuali IV tra punti consecutivi. Non è un segnale long/short. Le trimestrali possono creare gobbe.
- Cursore a croce e valori al passaggio del mouse su skew, term structure e GEX; valori OHLC sul prezzo. I cursori sono indipendenti perché gli assi rappresentano date storiche, strike e scadenze diverse.
- Preservati bande, storico, profili volume/OI e screener a 15 blocchi da 200 titoli, market cap ≥ $700M.
- Paginazione completa delle scadenze e delle chain.

## Dati e tempi
La curva IV viene caricata separatamente, senza attendere tutte le scadenze per mostrare il prezzo. Fino a quattro richieste chain simultanee; il primo caricamento può richiedere tempo in funzione del piano e del rate limit Massive. Mostra copertura e scadenze non disponibili. Il prezzo di riferimento deriva dall'ultima candela giornaliera disponibile e la sua data è indicata sopra la curva: non è un feed tick sincronizzato. Cache chain fino a 5 minuti; scadenze fino a un'ora. Eventuali ritardi dipendono anche dal piano dati.

GEX resta una stima gamma × OI con convenzione call positiva / put negativa, non una posizione dealer verificata.

## Persistenza
Impostare `DB_PATH` su un disco persistente per conservare lo storico e i risultati; il valore predefinito `/tmp/option_bands.db` è temporaneo.

## Verifiche eseguite
Compilazione Python e sintassi JavaScript; test isolati di interpolazione ATM, IV non valide/mancanti, paginazione e classificazione delle curve. Nessuna verifica su dati Massive reali: chiave API non presente nell'ambiente di sviluppo.

## V6.3 — Curva GEX al variare del prezzo
Il grafico GEX dispone delle viste Curva (predefinita), Barre e Curva + barre. In vista combinata la curva usa l'asse sinistro e le barre l'asse destro. La curva somma il gamma ricalcolato su 201 prezzi ipotetici, con Black-Scholes europeo, IV e OI fissi, call positive e put negative; non rappresenta il posizionamento osservato dei dealer. Usa solo la scadenza selezionata e i contratti con IV/OI disponibili. Gamma flip cercati nel solo range mostrato. Il modello usa tasso 4% e dividend yield 0%, esplicitati nell'interfaccia: sulle azioni con dividendi ed esercizio americano è un'approssimazione. Il gamma modellato allo spot può differire dal gamma dello snapshot delle barre. Il range è ±2 IV√T, limitato tra ±10% e ±40%. Il moltiplicatore viene letto da shares_per_contract o impostato a 100. Contratti rettificati con deliverable complesso possono richiedere trattamenti ulteriori. Nessun calcolo aggiuntivo durante le scansioni dei 15 blocchi.
Verificati segno del gamma, proporzionalità all'OI, gamma flip e assenza dati; compilazione Python e sintassi JavaScript. Dati Massive reali e rendering completo in browser non verificati nell'ambiente di sviluppo.

## V6.4 — Nuovi filtri screener
Rimossi dal menu i quattro filtri ±1σ/±2σ. Aggiunti IV in Contango, IV in Backwardation, prezzo sopra/sotto Gamma Flip. Le bande sul grafico restano disponibili.
La scansione calcola la stessa curva IV del grafico (scadenze valide 1–365 giorni; almeno due punti; tolleranza 0,1 punti IV). Curve miste/piatte o senza dati sufficienti non entrano nei due filtri direzionali della curva. Le colonne indicano scadenze valide e mancanti: la classificazione riguarda i punti disponibili.
Il gamma flip usa la scadenza scelta dallo screener, più vicina a 30 giorni; se ci sono più attraversamenti viene usato quello più vicino al prezzo di riferimento. Se manca un flip nel range simulato, o il prezzo coincide con esso, il titolo non entra nei filtri sopra/sotto. Sopra/sotto non equivale necessariamente a gamma positivo/negativo in presenza di più flip. Il prezzo di riferimento è l'ultima candela giornaliera disponibile.
La migrazione del database è automatica e conserva lo storico. Ripetere i blocchi già scansionati per ottenere le nuove colonne. La scansione richiede più richieste API e tempo per elaborare le scadenze IV. Filtri e colonne seguono il blocco visualizzato.
Test isolati su database SQLite: migrazione ripetuta, quattro filtri, casi senza flip e prezzo uguale al flip, opzioni del menu; sintassi Python e JavaScript. Nessun test con API Massive reali.
