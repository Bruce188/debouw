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
| Inzageloket Vlaanderen | Fase 4 actief | robots.txt restrictief; Playwright headed browser; 1 req/5 s; SSRF-allowlist op bijlages |
| RvVb rechtspraak | Fase 3 actief | research-scrape; 1 req/10 s (robots.txt Crawl-Delay) |
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

## Engine version policy

De risicoengine is versioned via het veld `engine_version` (huidig: `0.3.0-rules-precedents-v1`).
Elke versiestap maakt de eerder gecachte LLM-rationale ongeldig: de combinatiesleutel
`(project_external_id, engine_version)` in de `risk_narration_cache`-tabel wordt niet
overschreven, maar er wordt een nieuwe rij aangemaakt. Verouderde rijen worden niet
automatisch verwijderd en accumuleren op schijf (~1 KB per rij). Het commando
`debouw cache prune` (gepland in Fase 6+) verwijdert verouderde cache-entries.

## LLM rationale stability

De regelscores (`probability`, `severity`, `expected_delay_days`) zijn deterministisch
voor dezelfde invoer en dezelfde `engine_version`. De Nederlandstalige rationale gegenereerd
door Claude Sonnet kan variëren tussen runs — dit is verwacht gedrag. De cache
(`risk_narration_cache`) garandeert dat eenzelfde project slechts één API-call kost per
`engine_version`. Tests controleren gelijkheid van regelscores, niet van de LLM-tekst.

## API-key fallback chain

De narratorketen werkt als volgt:
1. **Anthropic (Claude Sonnet)** — primair; prompt caching ingeschakeld.
2. **OpenAI (GPT-4o)** — fallback wanneer `ANTHROPIC_API_KEY` ontbreekt of de API
   een niet-herstelbare fout geeft.
3. **Statisch Nederlandstalig sjabloon** — veiligheidsnet wanneer beide API-sleutels
   ontbreken. De rationale is minder specifiek maar altijd beschikbaar.

Het ontbreken van beide sleutels wordt éénmalig gelogd bij initialisatie van de engine
(`narrator_no_api_keys`). Geen foutmelding — de applicatie functioneert volledig
via het statische sjabloon.

## Precedent corpus stability

De RvVb-precedentenkorpus (LanceDB-tabel `rvvb_arrests`) is een **bevroren snapshot**
van de DBRC-rechtspraakwebsite op het moment van `debouw backfill-rvvb`. Volgende runs
voegen nieuwe arresten toe maar verwijderen nooit:

- `arrest_id` is uniek; herhaald upsert is idempotent (rij wordt overgeslagen).
- Wijziging van `arrest_extractor_version` (Settings-veld) leidt tot een **tweede
  rij** voor hetzelfde arrest, niet tot een vervanging. Verouderde
  `extractor_version`-rijen blijven in de korpus aanwezig totdat ze handmatig
  worden verwijderd.
- Bij toekomstige correcties op DBRC-zijde (nooit waargenomen, maar mogelijk)
  draaien we `debouw backfill-rvvb` opnieuw met een bumped `arrest_extractor_version`;
  oude rijen blijven beschikbaar voor traceerbaarheid.
- Embeddingmodelversie (`text-embedding-3-large`, 3072-dim) is **gepind** in
  `Settings.embedding_model`. Wijzigen van het model zonder volledige
  re-embedding maakt cosineafstanden onvergelijkbaar tussen oude en nieuwe rijen.

## LanceDB single-writer

LanceDB schrijft op disk via een native bestandsslot. Concurrent
`debouw backfill-rvvb`-processen worden geblokkeerd op het slot — de eerste
schrijver wint, de tweede wacht. **Eén backfill-proces per machine** is de
operationele aanname. Reads (`engine.classify()` met LanceDB-zoekopdracht) zijn
veilig parallel.

**Python 3.14 native-binding bug:** `lancedb` 0.19.0's `_lancedb.abi3.so`
segfaults op een bare `lancedb.connect(...)` op Python 3.14. De engine-purity
tests verwerken dit via de empty-vector fallback (`embed_text` retourneert `[]`
zonder OpenAI key → `search` retourneert `[]` zonder LanceDB-call). Voor
volledige LanceDB-tests draait u op Python 3.12 (zie `tests/test_precedents.py`
- skipped onder 3.14). Productie-aanbeveling: pin Python 3.12 voor de
backfill-machine totdat lancedb een Python-3.14-compatibele wheel uitbrengt.

## Gold-set bootstrap

De kalibratieharnas (`debouw eval`) berekent Brier + P@5 over een handgelabeld
gold set in `debouw/risk/eval/gold_set.jsonl`. Bij minder dan
`Settings.gold_set_min_n` (= 30) cases vallen de metrics terug op `None` en
worden de gates gemarkeerd als `"insufficient_gold_set"`.

Het project levert **3 zaad-cases** mee (Bothuyne Oudenaarde 2025, Lindepark
Sint-Niklaas 2024, De Lijn Wondelgem 2024). De gebruiker moet **27-47
extra cases** handmatig labelen vanuit de RvVb-korpus om de gates uit
`insufficient_gold_set` te tillen. Kalibratiebins (10 buckets) worden wel
gerapporteerd ongeacht N — diagnostisch nuttig vanaf de eerste run.

## Inzageloket Vlaanderen — Phase 4 posture

debouw scrapet `omgevingsloketinzage.omgeving.vlaanderen.be` via **Playwright
met een echte (headed) Chromium** — niet headless. De site is beschermd door
**Anubis** (proof-of-work anti-bot) en `robots.txt` staat enkel Googlebot/
Bingbot expliciet toe; een echte browser is verplicht. Headless-impersonatie
wordt geweigerd.

| Beleid | Waarde |
|---|---|
| User-Agent | `debouw-research/0.x (contact: brucieboyy99@gmail.com)` (zelfde als alle bronnen) |
| Throttle | 1 req / 5 s tussen `page.goto`-aanroepen (Settings.throttle_inzageloket_seconds) |
| Browser | `chromium.launch(headless=False)` — vereist `python -m playwright install chromium` |
| Sessions | Verse `BrowserContext` per dossier (Anubis-token-staleness vermeden) |
| Wall-clock | ~30-90 min voor `--limit 200` op een typische dev-machine |

**SSRF-bescherming:** Bijlage-URL's worden gevalideerd tegen een allowlist
(host == `omgevingsloketinzage.omgeving.vlaanderen.be`) vóór elke download.
Niet-toegelaten hosts worden gelogd en overgeslagen.

**PDF-grootte/paginabegrenzing:** Bijlagen groter dan 50 MB of met meer dan
1000 pagina's worden niet geëxtraheerd (gelogd als `pdf_too_large` of
`pdf_too_many_pages`); de pipeline gaat verder zonder PDF-tekst voor het
betreffende dossier.

**GDPR posture:** Identiek aan Gent — `applicant_name` wordt nooit
gepersisteerd. De ruwe HTML (`raw_html_path`) bevat enkel `external_id`
en publiek toegankelijke veldwaarden.

**Idempotentie:** Bij heruitvoer worden alleen nieuwe of gewijzigde
dossiers (gewijzigde `content_hash`) opnieuw geclassificeerd. Bestaande
dossiers worden overgeslagen — een volledige run kost dus alleen op de
eerste keer ~30-90 min.

## Brussel — Fase 5 (COBAT)

debouw scrapet `openpermits.brussels` via **bs4 + httpx** (Track B —
statisch HTML, geen JavaScript-rendering vereist). De site draait op
nginx met een Redis-cache en heeft geen Anubis-bescherming.

| Beleid | Waarde |
|---|---|
| Source | `openpermits.brussels` (`source="brussels_openpermits"`) |
| Track | Track B: bs4 + httpx |
| User-Agent | `debouw-research/0.x (contact: brucieboyy99@gmail.com)` |
| Throttle | 1 req / 2 s (Settings.throttle_brussels_seconds = 2.0) |
| robots.txt | `Allow: /fr/*` en `/nl/*`; `Disallow: /api/*` — huidige track gebruikt enkel /fr/* HTML-pagina's |

**FR-narrator branch:** Voor dossiers met `region="brussels"` gebruikt de
narrator `INSTRUCTIONS_FR` en de Franstalige taxonomie-markdown
(`_build_taxonomy_markdown(language="fr", region="brussels")`). De statische
terugval gebruikt eveneens `static_rationale_fr` en `legal_basis_fr`.
`engine_version` is ongewijzigd (`0.3.0-rules-precedents-v1` — geen fase-3
escalatie heeft plaatsgevonden).

**UrbIS / Brusselse ruimtelijke overlays:** Geopunt-verrijking (watertoets,
natura-2000, erfgoed) is Vlaams en is **niet van toepassing** op
Brusselse dossiers. Brussel-dossiers stromen door de pipeline met
`overlays=None`; regels verlagen naar `confidence="laag"`. UrbIS-API
integratie is uitgesteld tot Fase 6+.

**VL-only risicocategorieën:** `BPA_RUP_CONFLICT` en `WATER_FLOOD` zijn
enkel van toepassing op Vlaamse dossiers (`applicable_regions={"vl"}`).
Ze worden automatisch overgeslagen voor Brusselse dossiers.

**Wallonische dossiers:** Nog niet beschikbaar — Fase 6+.

**GDPR posture:** Identiek aan Gent en Inzageloket — `applicant_name`
wordt nooit gepersisteerd. `openpermits.brussels` toont ook geen naam.

**SSRF-bescherming:** Bijlage-URL's worden gevalideerd tegen de allowlist
(host == `openpermits.brussels`, schema == `https`) vóór elke download.

**mybrugis.brussels:** DNS-onbereikbaar tijdens de Phase-1-spike (april 2026);
alternatieve bronnen niet geactiveerd.

## Brussel — Fase 6 (score-differentiatie)

Versie `0.6.0` van de engine voegt score-differentiatie toe voor Brusselse
dossiers. Oude `0.3.0-rules-precedents-v1`-rijen blijven in de database
voor audit-traceerbaarheid maar worden niet meer weergegeven in het dashboard.

**Heuristische inferentie (ENABLE_IIOA_HEURISTIC):**
Wanneer een dossier geen expliciete MER-status (`mer_status=None`) heeft maar
een bruto vloeroppervlak ≥ 5 000 m² vermeldt, leidt de engine automatisch
`"screening"` af als MER-status. Wanneer een Brussels dossier geen expliciet
IIOA-klasse heeft maar een vloeroppervlak ≥ 1 500 m² vermeldt, leidt de engine
automatisch IIOA-klasse 2 af. Beide heuristieken worden **gelogd** als
`mer_heuristic_fired` / `iioa_heuristic_fired` in structlog.

Stel `ENABLE_IIOA_HEURISTIC=0` in `.env` om de heuristieken uit te schakelen
(aanbevolen voor kalibratieruns waarbij expliciete labels vereist zijn).

**PDF-features (post-enrich):**
Na de Geopunt-verrijkingsstap worden de reeds gecachte bijlage-PDF's van
Brusselse dossiers gemijnd op signalen: `units`, `floors`, `iioa_class`,
`mentions_ongunstig`. Deze extractie vindt bewust **na** `enrich()` plaats
voor latentieredenen (~2-8 s per dossier, niet per scrape-request).

**engine_version 0.6.0:**
De enige wijzigingen ten opzichte van `0.3.0-rules-precedents-v1` zijn
Brussels-specifieke feature-engineering toevoegingen; de regelscores voor
Vlaamse dossiers zijn ongewijzigd.
