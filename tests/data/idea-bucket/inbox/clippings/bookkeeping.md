---
title: "bookkeeping"
source: "https://gemini.google.com/app/860b7fd33128527e?is_sa=1&is_sa=1&android-min-version=301356232&ios-min-version=322.0&campaign_id=bkws&utm_source=sem&utm_medium=paid-media&utm_campaign=bkws&pt=9008&mt=8&ct=p-growth-sem-bkws&gclsrc=aw.ds&gad_source=1&gad_campaignid=22921538072&gbraid=0AAAAApk5BhmhKd8GXD9a-E1e32DpF91oV&gclid=CjwKCAjwx7LSBhB3EiwAjcodxKVVuc2PHBDRoUP4FIzmbqaPcwryazHwldPqN_Sz3aPwDNZC7W2XvhoC4QgQAvD_BwE"
author:
published:
created: 2026-09-08
description: "Gemini conversation with 34 messages"
tags:
  - "clippings"
---
**You**

invoices van cloudflare zijn in us and laten geen btw zien, hoe verwerk ik die

---

**Gemini**

Cloudflare is een Amerikaans bedrijf (gevestigd buiten de EU). Omdat het een dienst betreft die je zakelijk afneemt van een niet-EU-leverancier, geldt daar een specifieke fiscale behandeling voor.

### 1\. Hoe werkt de BTW bij Cloudflare? (Verlegging buiten EU)

- **0% BTW op de factuur:** Cloudflare rekent geen Amerikaanse *sales tax* of Nederlandse btw op zakelijke facturen.
- **Btw verleggen (Import van diensten van buiten de EU):** Voor de Nederlandse Belastingdienst moet de btw op diensten uit niet-EU-landen **"verlegd"** worden naar jezelf.
- **Per saldo € 0,- effect:** Je berekent zelf 21% Nederlandse btw over het omgerekende eurobedrag en geeft dit op in je btw-aangifte. Omdat het zakelijke kosten zijn, trek je diezelfde 21% meteen weer af als voorbelasting. Je betaalt onderaan de streep dus niets extra, maar het moet wel verplicht in de aangifte staan.

### 2\. Hoe voer je een Cloudflare-factuur in e-Boekhouden.nl?

#### Stap 1: Reken het USD-bedrag om naar EUR

Sla de USD-factuur op in je archief. Kijk op je (zakelijke of privé) bankafschrift welk **exacte bedrag in Euro's** er door de bank is afgeschreven voor deze invoice. Gebruik dat Eurobedrag voor de boeking.

#### Stap 2: Maak de inkoopboeking

1. Ga in e-Boekhouden.nl naar **Boekhouden** > **Invoeren** > **Inkoop**.
2. **Relatie:** Kies of maak *Cloudflare Inc.* aan (land instellen op *Verenigde Staten*).
3. **Factuurdatum:** De datum die op de Cloudflare-invoice staat.
4. **Bedrag:** Vul het omgerekende bedrag in **Euro’s** in.
5. **BTW-tarief:** Kies het specifieke btw-tarief:
	- **`Diensten van buiten de EU (21%)`** *(of "Btw verlegd buiten EU")*.
6. **Kostenrubriek:** Kies *Software/Automatiseringskosten*, *Hosting* of *Kantoorkosten*.
7. **Betaling:** Kies **Privé** (als je het voor de start met je privékaart hebt betaald) of **Zakelijk** (als het via je zakelijke rekening/creditcard is gegaan).

### Wat doet e-Boekhouden.nl vervolgens in de BTW-aangifte?

Het systeem zet dit bedrag automatisch op de juiste plek in je btw-aangifte:

- **Rubriek 4a:** *Leveringen/diensten uit landen buiten de EU* (hier wordt de 21% btw bijgeteld).
- **Rubriek 5b:** *Voorbelasting* (hier wordt diezelfde 21% btw weer afgetrokken).

---

**You**

wat is tegenrekening voor de betaling inschrijvingsksoten kvk

---

**Gemini**

Wanneer je de inschrijfkosten van de Kamer van Koophandel (KvK) verwerkt, gebruik je twee verschillende accounts (rekeningen) in je boekhouding: een **kostenrekening** en een **tegenrekening** (de betalingswijze).

### 1\. Welke rekeningen gebruik je?

- **Kostenrekening (Winst-en-Verlies):** **Oprichtingskosten**, **Algemene kosten** of **Kantoorkosten**.
- **Tegenrekening (Balans):** Dit hangt af van **hoe** je de nota hebt betaald:

| Hoe heb je de KvK betaald? | Tegenrekening in e-Boekhouden.nl |
| --- | --- |
| **Met je privérekening / contant / privé-PIN** | **Privé** (of *Privéstortingen / Eigen Vermogen*) |
| **Met je nieuwe zakelijke bankrekening** | **Zakelijke bankrekening** (bijv. *ING / Rabobank / Knab*) |

### 2\. Hoe boek je dit in e-Boekhouden.nl?

In de praktijk voer je dit op de volgende manier in via **Boekhouden > Invoeren > Inkoop**:

1. **Relatie:** Kamer van Koophandel (KvK).
2. **Factuurdatum:** De datum van de KvK-factuur/pinbon.
3. **Kostenrubriek:** Kies *Oprichtingskosten* (of *Algemene kosten*).
4. **BTW-tarief:** **Geen / Vrijgesteld / 0%** *(Let op: De KvK rekent wettelijk geen btw over inschrijfkosten)*.
5. **Bedrag:** Vul het totale bedrag in (bijv. € 80,10).
6. **Wijze van betaling (de tegenrekening):**
	- Heb je dit vóór de start van de onderneming met je privékaart betaald? Kies bij wijze van betaling voor **Privé**. Het systeem zet de tegenrekening dan automatisch op de balanspost *Privéstortingen*.
		- Is het direct van de zakelijke rekening gegaan? Kies dan voor **Bank**.

---

**You**

ik zie

---

**Gemini**

In het veld **Tegenrekening** kies je de kostenrubriek waarop de uitgave terecht moet komen (de **verlies-en-winstrekening**).

### Wat kies je bij Tegenrekening?

Selecteer in dat uitrolmenu een van de volgende categorieën:

- **Oprichtingskosten** *(meest logisch)*
- **Kantoorkosten**
- **Algemene kosten**

### Waar geef je dan aan hoe je hebt betaald?

Omdat je bovenin als *Soort boeking* hebt gekozen voor **Factuur ontvangen**, registreer je hiermee de inkoopfactuur.

Nadat je deze pagina opslaat, vraagt e-Boekhouden.nl hoe de factuur is voldaan (of je vinkt 'm af via betalingen):

- **Heb je dit privé betaald?** Kies bij de betaling voor **Privé** (of *Privéstortingen*).
- **Heb je dit zakelijk betaald?** Kies bij de betaling voor **Bank** (je zakelijke rekening).

---

**You**

ik zie geen goed bij Wat kies je bij Tegenrekening? moet ik er dan een toe voegen

---

**Gemini**

**Ja, dan kun je eenvoudig een nieuwe grootboekrekening toevoegen.**

Als categorieën zoals *Oprichtingskosten* of *Algemene kosten* niet in de keuzelijst staan, maak je deze eenmalig aan in e-Boekhouden.nl.

### Optie 1: Snel toevoegen via het plusje (+)

Klik op het **`+` pictogram** direct rechts naast het veld *Tegenrekening*. Er opent een scherm om een nieuwe categorie aan te maken:

- **Nummer:** Kies een vrij nummer in de 4000-serie (bijv. `4500` of `4900`).
- **Omschrijving:** *Oprichtingskosten* (of *Algemene kosten*).
- **Categorie:** Kies **Winst-en-verlies (Kosten)**.

### Optie 2: Kies een bestaande alternatieve rubriek

Wil je niks nieuws aanmaken? Kijk dan of een van deze standaard rubrieken al in de lijst staat:

- **Kantoorkosten**
- **Administratiekosten**
- **Overige algemene kosten**

### Snelle controle van je invoer op de screenshot

Je overige gegevens staan helemaal juist ingesteld:

- **Datum:** 31-07-2026 (net voor de start, prima voor aanloopkosten).
- **Btw-code:** **Geen btw** (klopt, KvK is vrijgesteld van btw).
- **Bedrag:** € 85,15.

---

**You**

ik kreeg deze vraag niet

Heb je dit privé betaald? Kies bij de betaling voor Privé (of Privéstortingen).

---

**Gemini**

Dat klopt! Omdat je bovenin hebt gekozen voor **Soort boeking: Factuur ontvangen**, slaat e-Boekhouden.nl de nota op als een *openstaande factuur* (te betalen schuld). Het systeem vraagt op dat scherm dus nog niet direct naar de betaling.

Je rondt dit af in **2 korte stappen**:

### Stap 1: Sla de factuur op

Vul bij **Tegenrekening** de gewenste kostenrubriek in (bijv. *Kantoorkosten* of de nieuw aangemaakte post *Oprichtingskosten*) en klik onderaan op **Opslaan**.

### Stap 2: Registreer dat je deze privé hebt betaald

Nadat je op opslaan hebt geklikt, staat de factuur in het systeem. Om aan te geven dat je dit met je privékaart hebt voldaan:

1. Ga in het menu naar **Boekhouden** > **Invoeren** > **Betalingen** (of ga naar het overzicht *Openstaande inkoopfacturen*).
2. Zoek de factuur van de **Kamer van Koophandel** op en klik op **Betalen**.
3. Kies bij **Wijze van betaling / Rekening**: **Privé** (of *Privéstortingen / Eigen vermogen*).
4. Vul de datum in waarop je de KvK-nota privé hebt gepind of overgeboekt.
5. Klik op **Opslaan**.

De factuur staat nu netjes op **0 (voldaan)** en de uitgave is correct verwerkt als privé-inbreng in je onderneming.

---

**You**

welke moet ik kiezen

---

**Gemini**

Kies voor **Open posten** onder het kopje *Overzichten*.

### Hoe je de betaling nu registreert:

1. Klik in het menu op **Open posten**.
2. Zoek de factuur van de **Kamer van Koophandel** op in de lijst.
3. Klik bij die factuur op de knop/optie **Betalen** (of het euro-icoontje).
4. Selecteer bij de betalingswijze **Privé** (of *Privéstortingen*).
5. Vul de datum in waarop je de factuur hebt voldaan en sla op.

### Handige tip voor de volgende keer

Als je direct wil aangeven dat een factuur al privé is betaald, kun je in plaats van *Factuur ontvangen* ook kiezen voor het menu-item **Afschrift/bonnetjes** onder *Invoeren*. Daar voer je de nota én de privé-betaling in één scherm tegelijk in.

---

**You**

ik zie niks voor betalen

---

**Gemini**

In dit scherm kun je het heel snel afboeken via de **Acties** onderaan!

### Hoe boek je deze betaling nu af?

1. **Vink de factuur aan:** Zet helemaal links in de regel (voor de datum `31-07-2026`) een **vinkje in het vakje**.
2. **Klik onderaan op Acties:** Klik op de tekst **`De geselecteerde facturen direct naar tegenrekening afboeken`**.
3. **Kies de privé-rekening:**
	- Selecteer in het schermpje dat verschijnt de rekening **Privé** (of *Privéstortingen / Eigen vermogen*).
		- Vul de datum van betaling in en bevestig.

De factuur verdwijnt nu uit je openstaande posten en staat correct verwerkt!

---

**You**

dan zegt ie betalingsverschil

---

**Gemini**

Aha, deze knop is inderdaad bedoeld om een bedrag te *minderen* bij een verschil, en niet voor een normale privébetaling. Sluit dit pop-upje even via het **kruisje rechtsboven (X)**.

Om de privébetaling er wél goed in te krijgen:

### De juiste manier via "Invoeren"

1. Ga in het linkermenu naar **Invoeren** > **Afschrift/bonnetjes**.
2. Vul de velden als volgt in:
	- **Rekening:** Kies **Privé** (of *Privéstortingen / Eigen vermogen*).
		- **Datum:** De datum van de betaling (bijv. 31-07-2026).
		- **Soort boeking:** Kies **Factuur betaald**.
		- **Relatie:** Kies **KL001 Kamer van Koophandel**.
3. Zodra je de KvK kiest, verschijnt de openstaande factuur van **€ 85,15** automatisch onderin beeld.
4. Vink de factuur aan en klik op **Opslaan**.

Nu is hij gegarandeerd correct gekoppeld en staat de open post op nul!

---

**Gemini**

Je bent op de juiste plek! Vul de velden in jouw scherm als volgt in:

- **Soort boeking:** Verander dit naar **Factuurbetaling verstuurd** *(of "Factuur betaald", want het is een inkoopfactuur die jij hebt betaald, niet geld dat je hebt ontvangen)*.
- **Rekening:** Kies **Privé** (of *Privéstortingen / Eigen vermogen*).
- **Open post:** Klik op het **vergrootglas** (of de balk) en kies de factuur van de **Kamer van Koophandel**.
- **Datum:** De datum van de betaling (bijv. 31-07-2026).
- **Omschrijving:** *Privébetaling KvK inschrijving*.

Klik daarna op **Opslaan**. De factuur staat nu definitief op voldaan!

---

**Gemini**

Pas de invulvelden op jouw scherm aan naar de volgende instellingen:

- **Soort boeking:** Kies **Factuurbetaling verstuurd**.
- **Rekening:** Selecteer **Privé** (of *Privéstortingen / Eigen vermogen*).

Zodra je **Factuurbetaling verstuurd** kiest, verandert het scherm automatisch: de tabel onderin verdwijnt en maakt plaats voor het veld **Open post** (met een vergrootglas).

Daar kies je de factuur van de Kamer van Koophandel, vul je de datum in (31-07-2026) en klik je op **Opslaan**.

---

**Gemini**

Je bent er bijna! Je hoeft alleen nog 2 dingen aan te passen:

1. **Rekening:** Pas `1000 - Kas` aan naar **Privé** (of *Privéstortingen / Eigen vermogen*).
2. **Datum:** Pas `09-08-2026` aan naar de datum waarop je het privé hebt betaald (bijvoorbeeld `31-07-2026`).

Klik daarna op **Opslaan**. Gefeliciteerd, je KvK-factuur is nu helemaal correct en volledig verwerkt!

---

**You**

ik zie alleen kas en bank

---

**Gemini**

Duidelijk! In het scherm *Afschrift/bonnetjes* laat e-Boekhouden.nl bij **Rekening** alleen de liquide middelen (Bank en Kas) zien.

Omdat dit een privébetaling is, is **Afschrift/bonnetjes** niet de juiste route. Je moet een **Memoriam-boeking** maken.

### Zo los je dit direct op

Sluit het huidige scherm en doe het volgende:

1. Ga in het linkermenu naar **Invoeren** > **Overige boekingen**.
2. Vul het scherm als volgt in:
	- **Soort boeking:** Kies **Memoriam**.
		- **Datum:** Vul de datum van betaling in (bijv. `31-07-2026`).
		- **Omschrijving:** *Privébetaling KvK*.
		- **Rekening / Tegenrekening:** Kies hier de balansrekening **Privéstortingen** (of *Eigen vermogen / Privé*).
		- **Open post:** Selecteer het vergrootglas en vink de factuur van de **Kamer van Koophandel (€ 85,15)** aan.
3. Klik op **Opslaan**.

### Waarom werkt dit?

Een Memoriam-boeking wordt in e-Boekhouden.nl gebruikt voor alle transacties waar geen directe bank- of kasrekening van de zaak bij betrokken is (zoals privé-inbreng). Hiermee streep je de schuld aan de KvK weg tegen jouw privé-inbreng op de balans.

---

**Gemini**

Zie je verder naar beneden onder het kopje **Invoeren** (of **Overig**) de optie **Overige boekingen** of **Memoriam** staan?

Als die er in dit menu niet tussen staat, is er een nog makkelijkere manier die e-Boekhouden.nl hiervoor heeft ingesteld:

### Methode via de factuur zelf (Snelst)

1. Ga in het menu naar **Overzichten** > **Open posten**.
2. Zoek de factuur van de **Kamer van Koophandel** en klik helemaal links op het **potloodje** (bewerken).
3. In het scherm van de factuur zie je onderin de betalingsgegevens.
4. Kies bij **Betaling / Wijze van betaling**: **Privé** (of *Privéstortingen*).
5. Vul de datum in (`31-07-2026`) en klik op **Opslaan**.

### Mocht dat niet lukken:

Verwijder de factuur kort via het kruisje/prullenbakje bij Open Posten, en voer hem opnieuw in via **Invoeren** > **Afschrift/bonnetjes**:

- **Soort boeking:** Geld uitgegeven
- **Rekening:** Kies **Privé** (als deze hier wel tussen staat) OF voer hem in als **Geld uitgegeven** en koppel direct de kostenpost *Oprichtingskosten*.

Met de eerste methode (factuur bewerken) heb je 'm binnen 10 seconden netjes weggeboekt!

---

**Gemini**

Top! Dít is de makkelijkste manier in e-Boekhouden.nl.

Wijzig het veld helemaal bovenaan als volgt:

- **Soort boeking:** Verander *Factuur ontvangen* naar **Geld uitgegeven**.

Zodra je dat kiest, verschijnt er direct een veld **Rekening**. Kies daar voor **Privé** (of *Privéstortingen*), controleer of de datum op `31-07-2026` staat en klik op **Opslaan**.

Daarmee staat de factuur én de privébetaling meteen in één keer goed verwerkt!

---

**Gemini**

Het uitrolmenu bij **Rekening** laat hier inderdaad alleen de zakelijke rekeningen (Kas/Bank) zien.

Laat de boeking daarom staan op **Soort boeking: Factuur ontvangen** en verwerk de privébetaling eenvoudig via een **Kruispost/Privé-boeking** via de balans.

### Stap 1: Zorg dat de factuur zo staat ingesteld

1. Zet **Soort boeking** weer terug op **Factuur ontvangen**.
2. **Tegenrekening:** `4100 - Kantoorkosten` (staat al goed).
3. **Btw-code:** `Geen btw` (staat al goed).
4. Klik op **Opslaan**.

### Stap 2: Koppel de privébetaling definitief af

Om de factuur nu van de lijst 'Open posten' te krijgen:

1. Ga in het linkermenu naar **Invoeren** > **Importeer / Bank / Kas** (of ga naar **Grootboek** > **Boeken**).
2. Kies als soort boeking **Memoriam** (of *Overige boeking*).
3. Vul het overzicht zo in:
	- **Datum:** `31-07-2026`
		- **Omschrijving:** *Privébetaling KvK*
		- **Debet / Rekening:** Kies **2000 - Crediteuren** (en selecteer de *Kamer van Koophandel* factuur).
		- **Credit / Tegenrekening:** Kies **0600 - Privéstortingen** (of *Eigen vermogen / Privé*).
		- **Bedrag:** `85,15`
4. Klik op **Opslaan**.

Hiermee staat de kostenpost op `4100 Kantoorkosten`, is de schuld aan de KvK netjes € 0,00 en is de uitgave geboekt als privé-inbreng op je balans.

---

**You**

hoe weet ik zeker dat de btw goed wordt gedaan over de aanloopkosten voor 1 aug, kan ik een excel export doen van alle ingevoerde facturen

---

**Gemini**

Your XLSX file is ready

![XLSX icon](https://drive-thirdparty.googleusercontent.com/32/type/application/vnd.openxmlformats-officedocument.spreadsheetml.sheet)

Aanloopkosten\_BTW\_Controle\_Overzicht

XLSX

### Hoe weet je zeker dat de BTW goed wordt verwerkt?

Bij zakelijke uitgaven en aanloopkosten die je hebt gemaakt **vóór de startdatum / 1 augustus** geldt het volgende t.a.v. de btw:

1. **KvK-inschrijfkosten:** De Kamer van Koophandel berekent **wettelijk geen btw (0%)**. Dat de btw op € 0,00 staat bij de KvK is dus **100% correct**.
2. **Aanloopkosten met btw (bijv. hosting, drukwerk, advies):** Alle btw op zakelijke kosten die je vóór de startdatum hebt gemaakt op privé- of zakelijke naam, mag je in je **eerste btw-aangifte** aftrekken als **voorbelasting (Rubriek 5b)**.
3. **Voorwaarden:** De facturen/bonnen moeten op jouw naam of handelsnaam staan en een duidelijke zakelijke bestemming hebben.

### Ja, je kunt eenvoudig een Excel-export maken in e-Boekhouden.nl

Je kunt al je ingevoerde facturen en bonnetjes exporteren naar Excel om dit in één overzicht te controleren:

#### Methode 1: Export via Mutaties (Meest compleet)

1. Ga in het linkermenu naar **Overzichten** > **Mutaties**.
2. Vul bij de datumperiode bijvoorbeeld in: `01-01-2026` t/m `01-08-2026`.
3. Klik op **Tonen**.
4. Klik rechtsboven in het scherm op het groen/witte **XLS-icoontje** (of XLS/CSV) om het direct als Excel-bestand te downloaden.

#### Methode 2: Export via het Btw-overzicht

1. Ga in het linkermenu naar **Aangiftes** > **Btw-aangifte**.
2. Kies de periode waarin je eerste btw-aangifte valt (bijv. 3e kwartaal 2026).
3. Klik op **Specificatie**.
4. Hier zie je per rubriek precies welke facturen meegaan. Ook hier staat rechtsboven de knop **XLS** om de specificatie naar Excel te exporteren.

### Voorbeeldoverzicht Excel

In het gegenereerde bestand hierboven zie je een sjabloon van hoe een overzicht van de aanloopkosten met btw-controle eruitziet.

---

**You**

in excel de btw staat er niet bij

---

**Gemini**

Als de BTW-kolom in de Excel-export uit e-Boekhouden.nl op **€ 0,00** staat of leeg is, zijn daar twee hele duidelijke redenen voor:

### 1\. Voor de KvK-factuur klopt dit 100% (€ 0,00 BTW)

De **Kamer van Koophandel brengt wettelijk géén BTW in rekening**.

- De KvK is een overheidsinstantie en de inschrijfvergoeding is vrijgesteld van omzetbelasting.
- Dat er in Excel € 0,00 BTW bij de KvK-regel staat, is dus **helemaal correct**. Er valt bij de KvK simpelweg geen BTW terug te vragen.

### 2\. Voor overige aanloopkosten (bijv. hosting, drukwerk, spullen)

Zie je bij ándere facturen (waar wél BTW op het bonnetje staat) ook géén BTW in de Excel-export? Dan is er tijdens het invoeren een BTW-code ingesteld die geen BTW berekent.

**Zo controleer en herstel je dat in e-Boekhouden.nl:**

1. Ga naar **Overzichten** > **Mutaties** (of *Open posten*).
2. Klik op het **potloodje** (bewerken) naast de betreffende factuur.
3. Kijk naar het veld **Btw-code**:
	- Staat deze op *Geen btw* of *Vrijgesteld*? Dan rekent het systeem 0% BTW.
		- Wijzig de BTW-code naar **Hoog, 21%** (of *Laag, 9%*).
4. Controleer of de bolletjes-keuze bovenaan op **Incl. btw** of **Excl. btw** staat zoals op je factuur/bonnetje staat.
5. Klik op **Opslaan**.

### Btw-aangifte controle

Als je straks je eerste BTW-aangifte doet via **Aangiftes** > **Btw-aangifte**:

- Ga naar **Specificatie**.
- Je zult zien dat de BTW van de herstelde facturen automatisch in **Rubriek 5b (Voorbelasting)** terechtkomt, waarna je dit bedrag terugontvangt van de Belastingdienst.

---

**You**

in de export mutaties is geen kolom btw

---

**Gemini**

Your XLSX file is ready

![XLSX icon](https://drive-thirdparty.googleusercontent.com/32/type/application/vnd.openxmlformats-officedocument.spreadsheetml.sheet)

Mutaties\_Export\_Met\_BTW\_Herberekening

XLSX

Dat klopt helemaal. In e-Boekhouden.nl is het rapport **Mutaties** een puur boekhoudkundig overzicht van de **Grootboekkaarten** (Debet en Credit). Daarom heeft dat specifieke overzicht standaard géén apparte BTW-kolom.

### Waar vind je wél de Excel-export mét BTW-kolommen?

Om een Excel te downloaden waarin per factuur exact het **Bedrag Excl. BTW**, het **BTW-bedrag** en het **Totaal Incl. BTW** staan, gebruik je een van deze twee plekken in e-Boekhouden:

#### Optie 1: Via de BTW-aangifte Specificatie (Aanbevolen!)

1. Ga in het menu naar **Aangiftes** > **Btw-aangifte**.
2. Kies de periode waarin de facturen vallen (bijv. 3e kwartaal 2026).
3. Klik op de knop **Specificatie**.
4. Hier zie je per rubriek (zoals *5b Voorbelasting*) precies per factuur de **Grondslag (Excl)** en het **BTW-bedrag**.
5. Klik rechtsboven op **XLS** om dit overzicht naar Excel te downloaden.

#### Optie 2: Via Inkoopfacturen overzicht

1. Ga naar **Overzichten** > **Open posten** (of *Inkoopfacturen*).
2. Zet het filter/status op **Alle facturen** (of *Voldaan*).
3. Klik op het **XLS-icoontje** rechtsboven. Dit overzicht bevat wél de opgesplitste BTW-bedragen per factuur.

### Zelf BTW berekenen in je Mutaties-Excel?

Als je toch met je geëxporteerde Mutaties-bestand wilt werken, kun je eenvoudig zelf een kolom toevoegen met de formule:

$$
\text{BTW Bedrag (21\%)} = \frac{\text{Totaalbedrag (Debet)}}{121} \times 21
$$

---

**You**

in btw aangifte is geen knop specificatie

---

**Gemini**

In het scherm van de BTW-aangifte werkt dit **interactief**:

### Klik direct op de bedragen (getallen)

In plaats van een aparte knop *Specificatie*, gebruikt e-Boekhouden.nl doorklikbare cijfers:

1. Ga naar **Boekhouden** (of *Aangiftes*) > **Btw-aangifte**.
2. Open de BTW-aangifte van de gewenste periode (bijv. 3e kwartaal).
3. **Klik direct op het blauwe/onderstreepte bedrag** bij de betreffende rubriek (zoals bij **Rubriek 5b Voorbelasting** of **Rubriek 1a**).
4. Er opent zich nu automatisch een pop-up of nieuw scherm met de **volledige specificatie** van alle facturen die op de achtergrond aan dat bedrag bijdragen.
5. Vanuit dat specificatiescherm kun je rechtsboven kiezen voor het **XLS- / Excel-icoontje** om die onderliggende lijst inclusief BTW te downloaden.

### Alternatief: BTW-overzicht via rapporten

Als je het complete overzicht van al je transacties met BTW-uitsplitsing in één Excel-bestand wil exporten:

1. Ga in het hoofdmenu naar **Overzichten** > **Grootboek**.
2. Kies de rekening **1500 (Te vorderen BTW)** of **1510 (Te betalen BTW)**.
3. Selecteer de gewenste datumperiode.
4. Klik rechtsboven op het **XLS-icoontje** om dit overzicht te exporteren naar Excel.

---

**You**

ik zie dit overzicht uit het verleden, maar bedrag lijkt veel te laag

---

**Gemini**

Dat het bedrag veel te laag lijkt, is waarschijnlijk heel goed te verklaren door de titel van de tabel op je afbeelding:

**2a Leveringen/diensten waarbij de heffing van omzetbelasting naar u is verlegd**

Dit is namelijk **niet** een overzicht van al je normale inkoopfacturen of kosten!

### Waarom dit bedrag laag is (of leeg lijkt)

- **Rubriek 2a** wordt uitsluitend gebruikt voor specifieke inkopen waarbij de btw is **verlegd** naar jou. Denk aan:
	- Inkopen bij een Nederlandse leverancier (zoals een onderaannemer) die expliciet "btw verlegd" op de factuur heeft gezet.
		- *Let op:* Inkopen uit het buitenland (zoals Facebook/Google advertenties of software uit de EU) vallen onder **Rubriek 4a/4b** (import uit EU/buiten EU), niet onder 2a.
- **Gewone inkoopfacturen** (zoals je KvK-inschrijving, kantoorspullen, Nederlandse webhosting, etc.) staan **nooit** in Rubriek 2a. Die vallen onder **Rubriek 5b (Voorbelasting)**.

### Waar vind je wél het volledige BTW-bedrag van ál je kosten?

Kijk op dezelfde pagina iets verder naar beneden (of scrol omlaag) naar:

- **Rubriek 5b - Voorbelasting**

Daar telt e-Boekhouden.nl de btw van **alle** normale Nederlandse inkoopfacturen, bonnetjes en kosten bij elkaar op. Dat is de plek waar je het verwachte (hogere) bedrag zult terugzien!