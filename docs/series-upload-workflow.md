# Workflow nahravani serialovych epizod na Prehraj.to

Tento dokument popisuje cilovy obecny workflow. Neni to detailni popis jednoho
skriptu, ale domluveny tok dat a rozhodovani, podle ktereho se maji skripty a
GitHub Actions chovat.

## Cil

Cilem je mit nepretrzite nahravani serialovych epizod na ucet
`serialy.prehrajto@seznam.cz` a soucasne si stale dopredu pripravovat dalsi
epizody. Nahravani nesmi cekat na dlouhe dohledavani zdroju nebo popisu, pokud
je mozne mit pripravenou frontu dopredu.

Kazda epizoda, ktera jde do uploadu, ma mit pripraveno:

- nazev videa ve formatu `Nazev serialu SxxExx - Nazev epizody CZ Dabing`
  nebo `Nazev serialu SxxExx - Nazev epizody CZ Titulky`,
- zdrojove video z Prehraj.to,
- informaci, proc byl zdroj vybran,
- jazykove dukazy, idealne potvrzene metadaty nebo Whispers,
- velikost nebo rozliseni zdroje,
- popis epizody nebo alespon popis serialu jako docasny fallback.

## 1. Export zakladnich epizod z produkcni databaze

Prvni krok je exportovat z produkcni databaze zakladni seznam serialu a epizod.
Produkce se pouziva pouze pro cteni.

Z databaze potrebujeme hlavne:

- ID serialu a epizody,
- slug serialu,
- cesky a puvodni nazev serialu,
- cislo serie a epizody,
- nazev epizody, pokud existuje,
- TMDB/IMDB metadata,
- hodnoceni a popularitu pro razeni,
- existujici ceske nebo anglicke popisy, pokud jsou k dispozici.

Zdroje videi ulozene v produkcni databazi se mohou pouzit jako pomocny vstup,
ale nemaji byt hlavni pravdou. Hlavni zdroje pro upload se maji dohledavat
aktualne na Prehraj.to, protoze databaze muze obsahovat stare nebo nekompletni
zdroje.

Vystupem exportu je backlog epizod v repozitari:

```text
backlog/series-episodes.jsonl.gz
```

Backlog slouzi jako lokalni kopie produkcnich dat, aby se pri kazdem dalsim
kroku nemuselo chodit do produkcni databaze.

## 2. Vyber poradi serialu a epizod

Epizody se maji pripravovat a nahravat prioritne podle popularity serialu.
Prakticky to znamena radit serialy podle dostupnych signalu, napriklad:

- pocet IMDB hlasu,
- IMDB hodnoceni,
- CSFD hodnoceni,
- interni sledovanost, pokud je dostupna.

U vybraneho serialu je idealni snazit se pripravovat cele serie nebo cely
serial, ne nahodne jednotlive epizody. Duvod je uzivatelsky: na cilovem uctu
ma byt pokud mozno kompletni serial, ne jen roztrousene epizody.

## 3. Aktualni hledani zdroju na Prehraj.to

Pro kazdou epizodu se musi zkusit aktualni hledani na Prehraj.to. Zdroj se
nebere z produkcni databaze — ta poskytuje pouze metadata epizody (nazev,
serial, cislo epizody). Samotny zdroj videa se vzdy dohledava na Prehraj.to.

Dotazy maji obsahovat nazev serialu a kod epizody, napriklad:

```text
Dexter S07E04
Dexter 7x4
```

Pouzivat se ma cesky i originalni nazev serialu, pokud jsou dostupne.
Pro konkretni nazev epizody a cislo epizody se nejdriv udela jeden search
request na Prehraj.to. Vracene HTML obsahuje seznam kandidatu vcetne velikosti
souboru, takze se nemaji naslepo probeovat vsechny vysledky. Z HTML se nejdriv
vyberou kandidati, kteri odpovidaji hledane epizode a maji alespon 300 MB nebo
jasny signal vyssi kvality. Kandidat s `CZ`, `CS`, `CZ dabing`, `CS dabing`
nebo podobnym ceskym hintem se muze brat jako pravdepodobny cesky dabing a
nemusi kvuli tomu hned na Whisper. Kandidat bez ceskeho hintu, nebo s jinym
jazykovym signalem, se nesmi zahodit, pokud sedi serial, serie a epizoda a
splnuje velikost nebo rozliseni; takovy kandidat jde do Whisper review fronty.

Search request musi vypadat jako bezny browser request. Minimalne se maji
posilat realisticke hlavicky `User-Agent`, `Accept`, `Accept-Language`,
`Accept-Encoding`, `Referer`, `Upgrade-Insecure-Requests` a `Sec-Fetch-*`.
Prilis strohe HTTP hlavicky mohou lokalne vracet 429, i kdyz stejna URL v
prohlizeci normalne vraci vysledky.

Pokud vyhledavani vrati vice stranek vysledku, nejdriv se zpracuje pouze prvni
stranka. Druha stranka se nacita az ve chvili, kdy na prvni strance neni zadny
vhodny kandidat. Cilem je setrit requesty na Prehraj.to a nevytvaret zbytecnou
zatez.

Pokud neni nalezen vhodny zdroj s ceskym zvukem, smi se jako fallback vybrat
zdroj s ceskymi titulky, pokud ma spravnou epizodu a velikost alespon 300 MB.
Takovy upload musi mit v nazvu jasne uvedeno `CZ Titulky`, ne `CZ Dabing`.
Soucasne se musi zapsat follow-up zaznam do fronty pro pozdejsi nastaveni
titulku, protoze titulky lze k videu doplnit az po zpracovani nahraneho videa
na Prehraj.to.

Fronta epizod, ktere potrebuji dodatecne nastavit titulky:

```text
plans/subtitle-followup-queue.jsonl
```

Z vysledku hledani se ulozi vsechny kandidati, kteri vypadaji jako stejna
epizoda. Shoda se overuje podle:

- nazvu serialu,
- kodu epizody `SxxExx` nebo `x` formatu,
- pripadne nazvu epizody,
- detailni URL zdroje.

Z hledani se ziskavaji take dulezite metadata:

- URL detailu na Prehraj.to,
- externi ID zdroje,
- nazev videa,
- delka,
- velikost souboru,
- format nebo rozliseni, pokud je ve vysledku,
- pripadne dalsi signal kvality.

Z hledani se ukladaji kandidati, kteri:

- odpovidaji hledane epizode,
- maji velikost alespon 300 MB nebo jasny signal vysokeho rozliseni,
- maji cesky hint v nazvu, nemaji zadny jazykovy hint, nebo maji jiny jazykovy
  signal, ktery se ma overit Whisprem.

Tyto nalezene zdroje se musi ukladat, protoze pozdeji mohou slouzit i pro
zpetny import do produkcni databaze.

## 4. Prvni filtr kvality zdroje

Hned po hledani na Prehraj.to se vysledky vyfiltruji na zaklade signalu
viditelnych v HTML: shoda epizody a kvalita zdroje. Do dalsiho kroku jdou
kandidati, kteri maji alespon 300 MB, nebo u nich probe pozdeji ukaze aspon
1080p. Cesky jazykovy hint v nazvu urcuje, ze se kandidat muze brat jako
pravdepodobny dabing. Chybejici nebo jiny jazykovy hint znamena, ze kandidat
ma jit do Whisper review fronty, ne ze se zahodi.

Preferovany zdroj je:

- cesky dabing nebo cesky zvuk,
- 1080p nebo vyssi,
- pokud rozliseni neni jasne, tak velikost alespon 300 MB,
- funkcni detail stranka a rozbalitelny stream,
- pokud mozno zdroj z Prehraj.to, ne jen odkaz z jineho poskytovatele.

Zdroje mensi nez 300 MB se nemaji vybirat pro upload, pokud existuje jina
varianta. Kvalitni zdroj bez ceskeho jazykoveho hintu nebo s jinym jazykovym
signalem se probeuje nebo ulozi do Whisper review fronty, misto aby se oznacil
jako `no acceptable source`. Samotny vyber kandidata pro upload resi az krok 6.

## 5. Odhad a overeni jazyka

Jazyk se kontroluje ve vice vrstvach. Kazda vrstva se uklada jako dukaz, aby
bylo pozdeji jasne, proc byl zdroj vybran nebo odmitnut.

Prvni rychla vrstva je nazev souboru:

- `CZ Dabing`,
- `CZ`,
- `cesky dabing`,
- `CZ titulky`,
- `SK dabing`.

Druha vrstva jsou metadata:

- jazyk zvuku z databaze, pokud existuje,
- jazyk z provider stranky,
- titulky nebo tracky nalezene pri rozbaleni detailu,
- informace z predchozich auditu.

Treti vrstva je Whisper (volitelna):

- rozbalit stream,
- vytahnout kratky audio vzorek,
- detekovat jazyk pomoci Whisper/faster-whisper language detection,
- ulozit jazyk, pravdepodobnost a stav kontroly.

V tomto kroku se Whisper nepouziva na tvorbu titulku ani na analyzu obsahu.
Pouziva se jen jeho detekce jazyka z audio vzorku, protoze model je pro tento
typ rozpoznavani vhodny. Vystupem je jazyk, pravdepodobnost a technicky stav
kontroly.

Whisper je nejužitečnějsi pro pripady, kdy se video podle nazvu tvari jako
ceske, ale ve skutecnosti cesky neni. Pokud Whisper odporuje nazvu nebo metadatum,
ma mit prednost Whisper a zdroj se nema nahravat jako cesky dabing.
Whisper je potreba i v opacnem pripade: kdyz vysledek hledani sedi na serial,
serii a epizodu, ma dostatecnou velikost nebo rozliseni, ale v nazvu neni
`CZ`, `CS`, `CZ dabing` ani jiny jazykovy hint. Takovy zdroj se nesmi zahodit
jako `no acceptable source`. Musi se ulozit do fronty pro Whisper a pozdeji
overit zvukem.

Fronta kandidatu cekajicich na Whisper je:

```text
plans/whisper-review-queue.jsonl
```

Do teto fronty patri napr. zdroje typu `Kriminalka Las Vegas 01x16`, pokud:

- nazev odpovida hledane epizode,
- velikost je alespon 300 MB nebo probe ukaze aspon 1080p,
- zdroj jde rozbalit na stream,
- chybi cesky jazykovy hint v nazvu nebo metadatech, pripadne existuje jiny
  jazykovy signal, ktery je potreba overit.

Pravidlo je tedy: kandidat, ktery splnuje kvalitu a shodu epizody, ale nema
primarne napsano, ze je cesky, nebo ma signal jineho jazyka, postupuje do
Whisper fronty. Tam se zjisti, zda je zvuk v cestine nebo v jinem jazyce.
Podle vysledku se s nim zachazi takto:

- Whisper potvrdi cestinu: epizoda se pripravi jako `CZ Dabing`.
- Whisper potvrdi jiny jazyk: epizoda se pripravi jako `CZ Titulky` a zaroven
  se zapise do `plans/subtitle-followup-queue.jsonl`.
- Whisper selze: kandidat zustava v review fronte se stavem chyby a muze se
  zkusit znovu nebo rucne prověřit.

Samostatny Whisper krok musi tuto frontu prubezne odbavovat:

1. vzit nekolik kandidatu z `plans/whisper-review-queue.jsonl`,
2. rozbalit stream,
3. vytahnout kratky audio vzorek,
4. detekovat jazyk,
5. zapsat vysledek do `audits/language-audit.jsonl`,
6. aktualizovat `audits/language-audit-latest.jsonl`,
7. pokud je jazyk cesky, prevest zdroj z review fronty do
   `plans/prepared-episodes.jsonl` jako upload-ready kandidat.
8. pokud je zdroj funkcni a kvalitni, ale Whisper potvrdi jiny jazyk nez
   cestinu, nezahazovat ho; pripravit ho jako upload typu `CZ Titulky` a
   zapsat ho do `plans/subtitle-followup-queue.jsonl`, aby bylo jasne, ze po
   zpracovani na Prehraj.to potrebuje doplnit ceske titulky.

Tento krok je implementovan lokalnim skriptem:

```text
src/process_whisper_review_queue.py
```

Skript pouziva `faster-whisper` pouze pro detekci jazyka. Cesky vysledek
promuje zdroj do `plans/prepared-episodes.jsonl` jako `upload_kind=audio`.
Necesky vysledek promuje funkcni a dost velky zdroj jako
`upload_kind=subtitles`, nastavuje `needs_subtitles_after_upload=true` a
zapise follow-up do `plans/subtitle-followup-queue.jsonl`.

Whisper se pro bezny rychly prepare nemusi poustet na vsechno. Nesmime ale
ztracet kvalitni kandidaty bez jazykoveho hintu; ty musi zustat v review fronte,
dokud je nepotvrdi nebo nevyradi Whisper.

Vystupy jazykove kontroly:

```text
audits/language-audit.jsonl
audits/language-audit-latest.jsonl
```

## 6. Vyber zdroje pro epizodu

Kandidati z hledani se seradi podle skore (cesky bonus + kvalita). Pokkud je
k dispozici i fronta drive naleznych zdroju (napr. z drivejsiho exportu), tyto
se pripoji k live vysledkum. Vysledkem je jeden serazeny seznam od
nejlepsiho po nejhorsi.

Vyber preferuje:

1. potvrzeny cesky zvuk,
2. pravdepodobny cesky zvuk,
3. ceske titulky nebo necesky zvuk pripraveny jako `CZ Titulky`,
4. kandidata bez jazykoveho hintu cekajiciho na Whisper,
5. vyssi rozliseni,
6. vetsi velikost souboru,
7. zdroj, ktery jeste nebyl neuspesne vyzkousen.

K epizode se vybere **prvni probe-overeny kandidat**. Znamena to:

- kandidati se probeuji v poradi podle skore (od nejlepsiho),
- prvni kandidat s funkcnim streamem a ceskym zvukem vyhrava,
- pokud neni cesky zvuk a kandidat bez jazykoveho hintu ceka na jazyk, ulozi se
  do `plans/whisper-review-queue.jsonl`,
- pokud Whisper potvrdi jiny jazyk, ale zdroj je funkcni a kvalitni, muze jit
  do uploadu jako `CZ Titulky`,
- dalsi kandidati se neprobeuji — prvni fungujici staci,
- pokud zadny kandidat neprojde, epizoda neni `upload_ready` a zkusi se
  znovu za 24 hodin (muze se objevit novy zdroj).

K epizode se ulozi:

- `selected_source`,
- seznam testovanych zdroju,
- vysledek jazykove kontroly,
- informace o rozliseni a velikosti,
- zda je epizoda `upload_ready`.

Vystupem pripravy zdroju je:

```text
plans/prepared-episodes.jsonl
```

Tento soubor je jedna z nejdulezitejsich front. Musi se prubezne doplnovat,
aby upload nemel prostoje.

## 7. Priprava popisu

Popisy jsou samostatny paralelni workflow. Nemaji blokovat dohledavani zdroju,
ale idealne maji byt hotove driv, nez epizoda prijde na upload.

Poradi zdroju pro popis:

1. popis konkretni epizody z TMDB nebo produkcni databaze,
2. anglicky popis epizody z TMDB,
3. popis epizody z jineho spolehliveho serialoveho webu,
4. popis serialu, pokud epizoda vlastni popis nema,
5. docasny kratky fallback, pokud nic lepsiho neni.

Gemma nema vymyslet dej bez podkladu. Ma dostat existujici zdrojovy popis a
prepsat ho do kratkeho originalniho ceskeho popisu. Vystup ma byt pouze hotovy
popis, bez rozboru, variant, odrazek nebo vysvetlovani.

Vystupem je:

```text
plans/descriptions.jsonl
```

Popis se generuje jednou pro dany zdrojovy text. Pokud zdroj videa neprojde,
popis epizody se nema zahazovat ani generovat znovu. Meni se jen zdroj videa.

## 8. Sestaveni upload manifestu

Upload manifest spoji dohromady:

- backlog epizod,
- pripravene zdroje,
- jazykove audity,
- vygenerovane popisy,
- stav uz nahranych epizod,
- seznam zdroju, ktere uz selhaly.

Do manifestu se dostane jen epizoda, ktera:

- jeste nebyla nahrana,
- ma pripraveny upload-ready zdroj,
- nema vybrany spaleny nebo nefunkcni zdroj,
- neni mensi nez 300 MB, pokud je velikost znama,
- ma alespon fallback popis,
- ma nazev ve spravnem formatu,
- zdroj neni vyloucen, i kdyz v backlogu chybi jeho zaznam — zdroj z live search
  se akceptuje podle URL, ne podle toho, jestli existuje v backlog kandidatech.

Vystupem je:

```text
manifests/upload-ready.jsonl.gz
reports/upload-manifest.json
```

Manifest je fronta pro samotne nahravani.

## 9. Overeni dostupnosti zdroje z GitHubu (nerealizovano)

Protoze hledani muze fungovat lokalne nebo pres ceskou proxy, ale samotny upload
bezi na GitHubu, bylo by potreba overit, ze GitHub runner dokaze zdroj rozbalit.

Tento krok neni v soucasnosti implementovany v CI. V praxi se nefunkcni zdroje
odfiltruji az pri uploadu — pokud zdroj nelze rozbalit, upload selze a zdroj se
oznaci jako spaleny. Dalsi manifest ho pak vylouci.

Pokud by se kontrola implementovala, mela by overit:

- detail stranka jde nacist,
- stream jde rozbalit,
- nejlepsi varianta ma alespon 1080p,
- `HEAD` na stream vraci velikost alespon 300 MB,
- zdroj neni geoblokovany pro GitHub runner.

Vystup by byl:

```text
reports/source-availability.jsonl
```

## 10. Upload na Prehraj.to

Upload job bere epizody z manifestu. Pro kazdou epizodu:

1. prihlaseni na cilovy ucet Prehraj.to,
2. vyber dalsi epizody podle manifestu a shardu,
3. rozbaleni detailu zdroje na stream,
4. vyber nejlepsi stream varianty,
5. kontrola velikosti,
6. stazeni videa,
7. volitelna kontrola jazyka pres Whisper,
8. upload na cilovy ucet,
9. zapis vysledku do state.

Stav uploadu se uklada do:

```text
state/uploaded.json
state/uploaded-shard-0.json
state/uploaded-shard-1.json
state/sync.log
state/sync-shard-0.log
state/sync-shard-1.log
```

Po uspesnem uploadu se ulozi:

- ID epizody,
- nazev videa,
- zdrojove URL,
- ID zdroje,
- jazykovy signal,
- rozliseni,
- velikost,
- ID nahraneho videa na Prehraj.to,
- casy resolve, download a upload.

Po neuspechu se ulozi duvod. Permanentne spatne zdroje se uz nemaji zkouset.

## 11. Kontinualni provoz

Workflow musi bezet ve trech paralelnich liniich:

1. Upload nahrava pripraveny manifest.
2. Priprava zdroju dohledava dalsi epizody a zdroje.
3. Popisy a jazykove audity postupne doplnuji kvalitu dat.

Tyto linie nejsou sekvencni. Neni spravne cekat, az sync dojede, a teprve
potom zacinat prepare. Prepare musi bezet dopredu porad, dokud existuji
nenahrane epizody, pro ktere jeste nemame ulozeny pouzitelny zdroj.

Upload nesmi cekat na idealni stav vsech metadat. Pokud je pripraveny kvalitni
zdroj a alespon pouzitelny popis, epizoda muze jit do uploadu. Popisy nebo
jazykove audity se mohou zlepsovat dodatecne, ale zdroj videa musi byt vybran
spravne uz pred uploadem.

Hlavni pravidlo provozu:

- sync ma porad nahravat z hotove upload fronty,
- prepare ma porad doplnovat dalsi hotove zdroje do zasoby,
- audit jazyka ma porad zpresnovat jazykove dukazy,
- generovani popisu ma porad doplnovat popisy k pripravenym i nahranym
  epizodam,
- pokud GitHub nestaci pripravovat dost rychle, stejna priprava musi bezet i
  lokalne a vysledky se musi prubezne zapisovat do repozitare.

Hlidan musi byt minimalne tento stav:

- kolik epizod je ve frontě pro upload,
- kolik epizod ma pripraveny zdroj,
- kolik zdroju ceka na Whisper,
- jestli se Whisper review fronta prubezne zmensuje nebo prevadi na
  upload-ready epizody,
- kolik epizod nema popis,
- jestli bezi sync,
- jestli bezi priprava zdroju,
- jestli bezi generovani popisu,
- jestli posledni GitHub Actions neskoncily chybou.

Stavovy report je:

```text
reports/ops-status.json
```

Watchdog ma pri nedostatku fronty spustit pripravu zdroju nebo manifestu a pri
existujici upload-ready fronte spustit sync.

## 12. Buffer pred uploadem

Provoz nesmi byt postaveny tak, ze jedna mala davka dojede a az potom se zacne
hledat dalsi. Musi existovat buffer pripravenych epizod.

Doporucene urovne:

- kriticky stav: mene nez 100 upload-ready epizod,
- varovny stav: mene nez 500 upload-ready epizod,
- bezny cil: alespon 1000 upload-ready epizod,
- dlouhodoby cil: tisice pripravenych zdroju napric serialy.

`upload-ready` zde znamena epizodu, ktera uz ma:

- vybrany zdroj,
- jazykovy verdikt `CZ_AUDIO`, `PROBABLE_CZ_AUDIO` nebo `CZ_SUBTITLES_ONLY`,
- pokud je velikost zdroje znama, tak alespon 300 MB,
- nazev pro upload,
- popis nebo docasny fallback popis.

Stream se overuje az pri uploadu. Pokud zdroj neni rozbalitelny, upload selze a
zdroj se oznaci jako spaleny. U pripravenych planu s `--require-resolvable-source`
je stream overeny jiz behem pripravy.

Pokud upload-ready fronta klesne pod varovny stav, nesmi se jen spustit jeden
dalsi GitHub prepare job a cekat. Musi se zkontrolovat, jestli priprava realne
pribyva. Pokud nepribyva, ma se spustit lokalni priprava.

### Claim epizod pred pripravou

Epizoda se musi pred hledanim zdroju nejdriv zapsat do
`plans/preparation-claims.jsonl`. Claim obsahuje ID epizody, vlastnika davky,
cislo shardu, cas prideleni a expiraci.

- aktivne claimovana epizoda se nesmi nabidnout zadnemu jinemu prepare procesu,
- prepare job smi zpracovat pouze epizody pridelene jeho davce a shardu,
- jedna GitHub davka rozdeli kandidaty mezi tri navzajem disjunktni shardy,
- dokonceni jobu bez noveho vysledku je v poradku jen tehdy, kdyz prideleny
  zdroj neprosel kontrolou; nesmi vzniknout kvuli prekryvu s jinym jobem,
- claim po padu procesu expiruje, aby epizoda nezustala trvale zablokovana,
- lokalni priprava musi respektovat stejny claim soubor jako GitHub Actions.
- GitHub prepare workflow nesmi skoncit uspesne, pokud zadna claimovana epizoda
  nevytvorila novy upload-ready zaznam.

## 13. Lokalni priprava jako zaloha GitHubu

GitHub Actions nejsou jediny misto, kde se smi pripravovat zdroje. Pokud
GitHub nestaci, narazi na 429, ceka ve fronte, nebo bezi dlouho bez novych
vystupu, ma se priprava spustit lokalne.

Lokalni priprava smi delat hlavne tyto prace:

- aktualni hledani zdroju na Prehraj.to,
- zapis nalezenych zdroju k epizodam,
- kontrolu velikosti a rozliseni z vysledku hledani,
- rozbaleni detailu a overeni streamu,
- Whisper audit malych vzorku,
- pripravu `plans/prepared-episodes.jsonl`,
- pripravu nebo doplneni `audits/language-audit-latest.jsonl`,
- sestaveni `manifests/upload-ready.jsonl.gz`.

Lokalni priprava nesmi menit produkcni databazi. Pokud potrebuje nova data,
vezme je z exportu v repozitari nebo z read-only exportu produkce.

Lokalni priprava musi zapisovat vysledky prubezne, ne az na konci velke davky.
Kdyz proces spadne, nesmi se ztratit hodiny prace. Stejne pravidlo plati pro
popisy a jazykove audity.

Z GitHubu pak muze bezet hlavne upload, protoze upload je nejpomalejsi cast.
GitHub prepare zustava uzitecny, ale nesmi byt jediny zdroj pripravenych
epizod, pokud kvuli nemu vznikaji prostoje.

## 14. Kdy se smi spustit sync

Sync se ma spoustet automaticky vzdy, kdyz existuje upload-ready fronta a
nebezi jiny sync pro stejne shardy.

Pokud sync dobehne a upload-ready fronta neni prazdna, dalsi sync se ma
zaradit hned. Pokud sync dobehne a fronta je prazdna, je to provozni chyba
pripravy, ne normalni stav. V takovem pripade se ma:

1. zkontrolovat, proc se nevytvoril dalsi manifest,
2. okamzite spustit maly prepare pro rychle doplneni nekolika epizod,
3. soucasne spustit vetsi lokalni pripravu pro doplneni bufferu,
4. po prvnich pripravenych epizodach hned spustit sync,
5. pokracovat v lokalni nebo GitHub priprave, dokud buffer neni zpet nad
   varovnym stavem.

Toto znamena, ze reakce na "sync nebezi" nema byt jen "spust prepare". Spravna
reakce je nejdriv zjistit, zda existuje upload-ready fronta. Pokud existuje,
spustit sync. Pokud neexistuje, rychle pripravit malou davku pro okamzity
upload a vedle toho rozjet velkou pripravu do zasoby.

## 15. Stavove metriky pro rozhodovani

Pro rozhodovani je potreba sledovat oddelene tyto pocty:

- epizody v backlogu,
- epizody s nalezenym alespon jednim zdrojem,
- epizody s upload-ready zdrojem,
- epizody ve skutecnem upload manifestu,
- epizody uz nahrane,
- zdroje cekajici na Whisper,
- epizody bez popisu,
- epizody s docasnym fallback popisem,
- posledni cas, kdy pribyl upload,
- posledni cas, kdy pribyl pripraveny zdroj,
- posledni cas, kdy pribyl popis.

Nestaci videt, ze "nejaky prepare job bezi". Musi byt videt, ze se zvysuje
pocet pripravenych epizod nebo ze se meni konkretni vystupni soubor. Beznici
job bez noveho vystupu neresi problem s prazdnou upload frontou.

## 16. Import zpet do produkcni databaze

Protoze se pri priprave dohledavaji aktualni zdroje na Prehraj.to, maji se tyto
informace ukladat tak, aby je bylo mozne pozdeji importovat zpet do produkcni
databaze.

Importovatelna data:

- serial,
- serie,
- epizoda,
- zdrojove URL,
- externi ID Prehraj.to,
- nazev zdroje,
- velikost,
- rozliseni,
- jazykovy verdikt,
- jak byl jazyk zjisten,
- stav Whisper kontroly,
- zda zdroj fungoval,
- zda byl zdroj pouzit pro upload.

Tim se zlepsi vyhledavani a filtrovani jazyka na produkcnim webu a pri dalsich
bezech uz nebude nutne vsechny zdroje dohledavat znovu.

## Otevrene body k domluve

- Jestli se ma upload zastavit, kdyz neni Whisper potvrzeni, nebo staci
  metadata a nazev.
- Jestli mensi zdroj pod 300 MB smi byt nekdy nouzove nahran.
- Kde presne brat popisy epizod, ktere nejsou v TMDB.
- Jak casto delat export z produkcni databaze.
- Jak presne formatovat importni soubor pro zpetny import zdroju do produkce.
- Jestli implementovat kontrolu dostupnosti zdroje z GitHubu (kapitola 9),
  nebo staci spolehat na to, ze nefunkcni zdroj selze az pri uploadu.
