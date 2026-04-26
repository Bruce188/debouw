# LIMITATIONS — debouw omgevingsvergunningen risico-monitor

## Wat we doen / Wat we niet doen

debouw is een **onderzoeksprototype** dat risico-indicatoren berekent voor Belgische
omgevingsvergunningsdossiers. Het systeem geeft een informatieve risicorangschikking
op basis van publiek toegankelijke bronnen en een regelgebaseerde engine.

**Uitdrukkelijk buiten scope:**
- Geen juridisch advies. De uitkomst van het systeem is geen juridisch advies en kan
  nooit een advocaat of erkend juridisch adviseur vervangen.
- Geen MER-vervanging. debouw is geen milieu-effectenrapportage (MER) en vervangt
  de wettelijke MER-procedure niet.
- Geen beslissingsgarantie. Een lage risicoscore is geen garantie dat een vergunning
  wordt verleend; een hoge score is geen zekerheid dat ze wordt geweigerd.
- Geen volledigheid. De bronnen die worden gescraped zijn een selectie; brussel,
  Inzageloket (inzageloket.omgeving.vlaanderen.be) en RvVb-precedenten worden
  pas opgenomen in latere fases (Fase 3-5).

## Bronnen + ToS / polite-scrape posture

| Bron | Status | ToS-posture |
|------|--------|-------------|
| Gent consultatieomgeving | Fase 1 actief | Geen robots.txt; geïdentificeerde User-Agent `debouw-research/0.x`; 1 req/2 s |
| Geopunt WMS/WFS | Fase 1 actief | Publiek overheidsplatform; geïdentificeerde UA; ≤1 req/s |
| Nominatim (OSM) | Fase 1 actief | OSM ToS: geïdentificeerde UA vereist, 1 req/s, 30-day cache, geen autocomplete/rastergebruik |
| Onroerend Erfgoed WFS | Fase 1 actief | Publiek overheidsplatform; geïdentificeerde UA; ≤1 req/s |
| Inzageloket Vlaanderen | Fase 4 gepland | robots.txt is restrictief; Playwright headed browser (geen headless impersonation) |
| RvVb rechtspraak | Fase 3 gepland | research-scrape; 1 req/3 s |
| Brussels OpenPermits | Fase 5 gepland | Open API; nader te evalueren |

Elke scraper gebruikt `debouw-research/0.x (contact: brucieboyy99@gmail.com)` als
User-Agent en respecteert de bovenstaande rate-limits via een asyncio throttler.

## GDPR + privacy

- **Geen aanvragersnaam** (`applicant_name=None`): Gent toont de naam van de aanvrager
  niet in de publieke raadpleging; het systeem persisteert nooit `applicant_name`.
- **Ruwe HTML** wordt bewaard voor audit-traceerbaarheid; koppeling is enkel via
  `external_id` (geen naam of BSN).
- **Bezwaarindieners** (Fase 6+): alleen samenvatting + gezouten hash van het
  indienersidentificator wordt bijgehouden; bewaartermijn 18 maanden.
- Recht op verwijdering: stuur een e-mail naar `brucieboyy99@gmail.com`. We streven
  ernaar binnen 10 werkdagen te reageren.

## Geldstroom / Money-flow rule

debouw is een **B2B informatiedienst aan bouwheren en projectontwikkelaars**.
De dienst wordt uitsluitend betaald door bouwers die informatie wensen.

- We aanvaarden **nooit** betaling van bezwaarindieners of omwonenden.
- We bieden **nooit** diensten aan om het vergunningsresultaat te beïnvloeden.
  Dit zou strijdig zijn met art. 470 Strafwetboek (corruptie).
- De risicoscore is een informatie-instrument; het systeem werkt nooit
  als adviseur voor bezwaarschriften.

## Calibratieregime

Alle dossiers die na 1 januari 2026 worden ingegeven, vallen onder het nieuwe
Vlaamse omgevingsvergunningsdecreet (hervorming december 2025). Het veld
`decision_regime` slaat `"post_2026_reform"` op voor alle Fase 1-dossiers.

RvVb-precedenten (Fase 3) worden opgeplitst per regime om valse
kalibrering te vermijden. De risicoengine documenteert welk regime wordt
gebruikt in elk `RiskAssessment.calibration_regime`.

## Takedown

Contactpersoon: **brucieboyy99@gmail.com**

Bij vragen of verzoeken tot verwijdering van gegevens:
- Stuur een e-mail met het `external_id` of de URL van het dossier.
- Wij streven ernaar binnen **10 werkdagen** te reageren en het dossier te verwijderen.
