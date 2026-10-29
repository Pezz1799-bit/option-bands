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


## V6.5 — Evoluzione dello strike di minima IV
- Linea turchese sul prezzo: strike con IV minima osservata nella smile della scadenza selezionata. Il pulsante Minimo smile la mostra/nasconde indipendentemente dalle bande.
- Confronto delle ultime 5 smile salvate, con colori, minimi, cursore e data. Stessa scadenza per tutta la serie; nessuna concatenazione tra scadenze diverse.
- Snapshot salvato caricando le opzioni o durante una scansione. Una riga al giorno di New York, per ticker/scadenza: l'ultima rilevazione sostituisce quella precedente dello stesso giorno. Non esiste un processo automatico di acquisizione a programma chiuso.
- Dati aggiuntivi in una nuova tabella SQLite, senza modificare lo storico esistente. Impostare DB_PATH su disco persistente per non perdere le rilevazioni. Nessuno storico precedente viene inventato o ricostruito.
- Il minimo usa i punti IV del provider, senza fit/smoothing o filtri di liquidità aggiuntivi. Se c'è parità sceglie lo strike più vicino al prezzo. Richiede almeno tre punti. Un minimo all'estremo del range è indicato come minimo al bordo e non compare come vertice nella linea storica. Una smile piatta o quotazioni poco liquide possono rendere poco significativo il minimo osservato: non è un target o un segnale di trading.
- Data della rilevazione e data del prezzo di riferimento sono distinte: lo spot deriva dall'ultima candela giornaliera disponibile. La linea usa la data di osservazione della smile, non retrodata il dato alla candela dello spot.
Verificati calcolo del minimo, minimo al bordo, assenza dati, persistenza nella funzione di calcolo, sovrascrittura giornaliera e separazione per scadenza. Python e JavaScript superano i controlli di sintassi. Nessuna prova con chiave Massive reale o verifica visiva completa in browser.

Questa versione è stata applicata allo ZIP RODEQU-option-bands.zip. Flask utilizza templates/index.html; il file index.html nella radice resta quello originale.

## V6.6 — Prime tre scadenze e smile composito
Nuovo pannello indipendente dalla scadenza selezionata per bande/GEX: mostra le prime tre scadenze quotate non passate (inclusa oggi, se presente), con un colore per scadenza. Se una delle prime tre non ha almeno tre strike IV validi, mostra il problema senza sostituirla silenziosamente con una scadenza successiva.
Il selettore consente uno smile composito: media aritmetica 1/3 delle IV annualizzate a parità di strike. Su griglie diverse interpola linearmente nel solo intervallo comune. Richiede tutte e tre le curve e almeno tre strike nel range comune. Questo è un indice di confronto, non la IV di un contratto o di una scadenza reale; non usa premi o OI come pesi.
Minimi osservati indicati con un punto, linea orizzontale tratteggiata alla IV minima e linea verticale allo strike. Gli strike dei minimi sono anche livelli orizzontali tratteggiati sul grafico prezzo, separati dalla linea turchese storica. Se il minimo è al bordo viene indicato nel testo, senza presentarlo come vertice interno.
Il caricamento del titolo salva anche le tre curve nel database per lo storico per singola scadenza. Il composito non viene salvato come se fosse una scadenza. Le rilevazioni conservano la data del prezzo di riferimento e si aggiornano al caricamento; non avviano acquisizioni automatiche a programma chiuso.
Verificati media/interpolazione, limiti del range comune, selezione delle scadenze, IV mancanti, salvataggio per tre scadenze, sintassi e rendering SVG con DOM simulato (viste, tratteggi, livelli prezzo). Nessuna verifica con Massive reale o browser completo.
