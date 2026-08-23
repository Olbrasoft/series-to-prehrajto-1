# Pokyny pro agenty

Tento repozitar je produkcni provozni stav pro kontinuální pripravu a nahravani
serialovych epizod na Prehraj.to. Nez agent provede zmeny nebo zasah do
provozu, musi si precist tento soubor a `docs/operations-handoff.md`.

## Jazyk a styl prace

- S uzivatelem komunikuj cesky.
- Kód, komentáře v kódu, názvy větví, commit messages a GitHub texty piš
  anglicky.
- Nevracej ani nemaž lokální změny, které jsi sám nevytvořil. V tomto repu
  často existují rozdělané soubory mimo aktuální úkol.
- Pro změny používej čistý dočasný worktree z aktuálního `origin/main`, pokud
  pracovní kopie není čistá.
- Repository je současně provozní databáze. GitHub Actions průběžně commitují
  stav do `main`, takže před čtením stavu vždy pracuj s aktuálním `origin/main`.

## Cíl workflow

Cílem je nepřetržitě nahrávat epizody seriálů na aktuálně nastavený Prehraj.to
účet a zároveň připravovat dostatečnou zásobu dalších epizod. Zdravý stav není
jen zelená GitHub Action. Zdravý stav znamená:

- jeden `sync` běh aktivně nahrává a oba upload shardy jsou ve fázi
  `Run sync batch`,
- další `sync` běh je pending, nebo ho po dokončení založí `queue-next`,
- fronta upload-ready epizod je ideálně blízko limitu 1000,
- `prepare-manifest`, `prepare-sources`, popisy, jazyky a titulky běží nebo
  pravidelně dobíhají zeleně,
- v `state/uploaded-shard-*.json` přibývají nové uploady s čerstvým
  `uploaded_at` / `last_updated`.

## Hlavní datové soubory

- `backlog/series-episodes.jsonl.gz`: export epizod z produkční databáze,
  pouze pro čtení.
- `backlog/enriched-audit-queue.jsonl.gz`: pomocná fronta známých kandidátů.
- `plans/prepared-episodes.jsonl`: trvalý rezervoár připravených epizod.
  Toto je hlavní zásoba pro manifest.
- `manifests/upload-ready.jsonl.gz`: aktivní upload okno, nejvýše 1000 položek.
- `reports/upload-manifest.json`: report posledního buildu upload manifestu.
- `state/uploaded-shard-0.json` a `state/uploaded-shard-1.json`: trvalý stav
  nahraných epizod a failů pro oba upload shardy.
- `state/sync-shard-0.log` a `state/sync-shard-1.log`: provozní logy uploadu.
- `plans/whisper-review-queue.jsonl`: kandidáti čekající na jazykové ověření
  Whisprem.
- `audits/language-audit-latest.jsonl.gz`: poslední jazykové výsledky.
- `plans/subtitle-followup-queue.jsonl`: epizody nahrané jako `CZ Titulky`,
  kterým je potřeba po zpracování na Prehraj.to doplnit titulky.
- `reports/subtitle-backfill-status.jsonl`: stav doplňování titulků.
- `plans/descriptions.jsonl`: připravené popisy.
- `reports/ops-status.json`: agregovaný provozní status z watchdogu/status jobů.

## Source preparation

Epizody se neberou jako hotové zdroje z produkční databáze. Backlog dodá metadata
epizody, ale aktuální zdroj videa se dohledává živě na Prehraj.to.

Příprava zdrojů:

1. Načte epizodu z backlogu.
2. Hledá na Prehraj.to podle názvu seriálu a kódu epizody, například
   `Dexter S07E04` nebo `Dexter 7x4`.
3. Používá browser-like HTTP hlavičky a šetří requesty.
4. Z první stránky výsledků vybere kandidáty odpovídající epizodě.
5. Preferuje zdroje s českým audio hintem a velikostí alespoň 300 MB.
6. Zdroj bez českého hintu se nezahazuje, pokud sedí epizoda a kvalita; jde do
   Whisper review.
7. Zdroj s jiným jazykem může být použit jako `CZ Titulky`, pokud má titulky,
   a zapíše se do subtitle follow-up fronty.
8. Výsledek se uloží do `plans/prepared-episodes.jsonl`.

`build_upload_manifest.py` z rezervoáru skládá aktivní manifest a vyřazuje už
nahrané, duplicitní, podměrečné, burned nebo neřešitelné zdroje.

## Upload workflow

`sync.yml` je hlavní upload workflow.

- Běží každých 10 minut a lze ho spustit ručně.
- Má workflow concurrency group `series-to-prehrajto-sync` a
  `cancel-in-progress: false`.
- Zdravě má být jeden aktivní `sync` a jeden pending nástupce.
- Upload má dva matrix shardy: `Upload series episodes (0)` a
  `Upload series episodes (1)`.
- Každý shard volá `src/sync_batch.py`.
- Upload používá `REQUIRE_PREPARED_SOURCES=1` a
  `REQUIRE_PREPARED_DESCRIPTIONS=0`; popis tedy upload neblokuje.
- Subtitles-only epizody jsou povolené přes `allow_subtitles=true`.
- Stavy se commitují přes `src/upload_state_merge.py`, aby se nepřepsaly
  souběžné uploady druhého shardu.

Zdravý upload poznáš podle:

- oba shardy jsou v `Run sync batch`, nebo jeden doběhl a druhý ještě běží,
- ve state souborech přibyl novější `last_updated`,
- v posledních upload položkách je aktuální čas,
- po dokončení proběhne `Queue next upload and preparation`.

Pokud jeden shard visí dlouho bez nových state commitů a blokuje pending `sync`,
zkontroluj detail běhu a state. Když je poslední upload výrazně starý a druhý
sync čeká, je provozně správné zrušit starý zaseknutý `sync`, aby pending běh
nastartoval. Po zrušení vždy ověř, že nový `sync` přešel do `in_progress` a
oba shardy jsou v `Run sync batch`.

## Preparation workflows

`prepare-manifest.yml` je rychlá průběžná příprava.

- Běží každých 10 minut a spouští se i z watchdogu nebo z dalších workflow.
- Má concurrency group `series-to-prehrajto-prepare`.
- `claim` rezervuje epizody do `plans/preparation-claims.jsonl`.
- Dva prepare shardy hledají a ověřují zdroje.
- Před hledáním se rychle přestaví manifest z již připraveného rezervoáru.
- Po přípravě se výsledky slučují přes `merge_preparation_results.py` a znovu
  se staví manifest s limitem 1000.
- `verify-growth` může selhat, pokud příprava nepřidala žádnou upload-ready
  epizodu. Ne každý takový fail znamená zastavený provoz; vždy kontroluj, zda
  běží novější příprava a zda je upload-ready fronta zdravá.

`prepare-sources.yml` je hlubší background discovery.

- Běží hodinově.
- Umí delší dávky, defaultně s Whisprem.
- Dlouhý běh je normální; sleduj krok `Prepare episode sources` a pozdější
  commit `chore: prepare series episode sources`.
- Po úspěchu může založit upload, pokud žádný `sync` neběží.

## Language, Whisper and subtitles

`audit-language.yml` ověřuje jazyk zdrojů pomocí metadat a Whisperu.

`process-whisper-review.yml` zpracovává `plans/whisper-review-queue.jsonl`.

- Český zvuk se promění na upload-ready `CZ Dabing`.
- Jiný zvuk se může proměnit na `CZ Titulky` a dostane záznam do
  `plans/subtitle-followup-queue.jsonl`.
- Workflow rebuildí manifest a má delší retry push logiku, protože upload shardy
  často commitují do `main`.

`backfill-subtitles.yml` doplňuje titulky k už nahraným a zpracovaným epizodám,
které mají v názvu `CZ Titulky`.

- Defaultně bere malé dávky.
- Titulky lze doplnit až po zpracování videa na Prehraj.to.
- Výstupy zapisuje do `reports/subtitle-backfill-status.jsonl`.
- Pokud se zobrazí `target_processing`, nejde nutně o chybu; dané video ještě
  nemusí být připravené pro doplnění titulků.

## Descriptions

`prepare-descriptions.yml` generuje popisy přes Gemma/Gemini.

- Běží po 30 minutách.
- Ukládá `plans/descriptions.jsonl` a `state/gemini-quota-state.json`.
- Timeout není automaticky chyba; workflow má commitovat částečné výsledky.
- Po úspěchu může založit menší `prepare-manifest`.

`update-descriptions.yml` aplikuje hotové popisy na už nahraná videa a slučuje
změny zpět do upload state.

Upload popisem aktuálně nečeká; chybějící popis se opravuje následně.

## Watchdog a status

`ops-watchdog.yml` běží každých 5 minut a po dokončení core workflow. Volá
`src/ops_watchdog.py` a podle stavu zakládá chybějící práci.

Aktuální cíle:

- alespoň 1000 upload-ready epizod v aktivním okně,
- širší přípravný cíl 3000 epizod,
- emergency manifest batch 120 epizod,
- prepared source target 10000,
- průběžné jazykové, Whisper a description práce.

Watchdog sám nedokazuje, že upload běží. Je pouze recovery controller. Vždy
porovnávej skutečný stav uploadů a fronty.

## Standardní kontrola provozu

Používej čistý worktree nebo nejdřív fetchni aktuální `origin/main`.

```bash
gh run list --repo Olbrasoft/series-to-prehrajto --limit 40 \
  --json databaseId,workflowName,status,conclusion,createdAt,updatedAt,event,headSha

gh run list --repo Olbrasoft/series-to-prehrajto --workflow sync.yml --limit 12 \
  --json databaseId,status,conclusion,createdAt,updatedAt,event,headSha

gh run view RUN_ID --repo Olbrasoft/series-to-prehrajto --json status,conclusion,jobs \
  --jq '{status,conclusion,jobs:[.jobs[]|{name,status,conclusion,startedAt,completedAt,current:[.steps[]|select(.status=="in_progress")|.name]}]}'
```

Pro reálný stav fronty a uploadů:

```bash
git fetch --depth=100 origin main
git worktree add --detach /tmp/series-current-check origin/main
cd /tmp/series-current-check
python3 src/upload_queue_status.py --no-require-description --respect-shard --json
python3 - <<'PY'
import json
for name in ["state/uploaded-shard-0.json", "state/uploaded-shard-1.json"]:
    data = json.load(open(name))
    uploads = data.get("uploads", [])
    print(name, len(uploads), data.get("last_updated"))
    for upload in uploads[-5:]:
        print(" ", upload.get("uploaded_at"), upload.get("display_name"))
PY
```

Interpretace:

- `remaining_upload_ready` poblíž 1000 znamená plné aktivní upload okno.
- Starší `sync` ve stavu `in_progress` s jedním dokončeným a jedním visícím
  shardem nemusí být zdravý. Ověř `last_updated`.
- Pending `sync` je zdravý jen tehdy, když před ním skutečně běží aktivní sync.
- `prepare-manifest` pending je obvykle normální concurrency stav.
- Cancelled prepare běhy často znamenají nahrazení novějším během, ne chybu.
- Červený `verify-growth` kontroluj podle fronty a novějších běhů; sám o sobě
  nemusí blokovat upload.

## Ruční obnovení uploadu

Když je upload-ready fronta zdravá, ale upload stojí na starém zaseknutém
`sync`:

1. Otevři detail aktivního sync běhu.
2. Ověř, že jeden shard dlouho visí v `Run sync batch`.
3. Ověř, že `state/uploaded-shard-*.json` nemá čerstvý `last_updated`.
4. Ověř, že další `sync` je pending nebo že fronta má položky.
5. Zruš starý běh:

```bash
gh run cancel RUN_ID --repo Olbrasoft/series-to-prehrajto
```

6. Počkej, až pending `sync` přejde do `in_progress`.
7. Ověř, že oba upload shardy jsou v `Run sync batch`.
8. Po chvíli znovu ověř state a čerstvé uploady.

Pokud žádný `sync` neběží ani nečeká a fronta má připravené epizody, spusť:

```bash
gh workflow run sync.yml --repo Olbrasoft/series-to-prehrajto \
  -f batch_size=30 \
  -f num_shards=2 \
  -f allow_subtitles=true \
  -f download_timeout_seconds=600 \
  -f max_episode_attempts=60 \
  -f continue_uploads=true
```

## Zásady pro úpravy

- Stavové soubory v `state/`, `plans/`, `reports/`, `manifests/`, `audits/`
  měň ručně jen tehdy, když je to výslovně cílem.
- Při úpravě workflow mysli na push races s upload shardy; commity musí fetchovat
  aktuální `main`, slučovat relevantní soubory a retryovat push.
- Nepoužívej dlouhou historii, pokud není nutná; workflows používají shallow
  checkout kvůli velikosti repozitáře.
- Před finálním hlášením po provozním zásahu vždy ověř reálný stav: aktivní
  shardy, frontu a poslední upload timestamps.
