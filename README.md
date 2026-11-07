# Projekttérkép

Kódtérkép Claude Code-hoz, hogy az agent ne olvassa be újra és újra a teljes
projektet, hanem egy gráfból tudja, hol mi van, és csak a szükséges fájlrészletet
nyissa meg. A [graphify](https://github.com/Graphify-Labs/graphify) motorjára
épül, arra tesz rá egy réteget, ami több projekttel dolgozó munkaterülethez kell.

## Mit tud a graphifyhoz képest

- **Egy munkaterület, sok projekt.** A munkaterület minden első szintű almappája
  külön projekt, mindegyik saját térképet kap, a végén pedig egy összevont gráf
  készül az összesből.
- **Nincs fájlszám-korlát.** Nagy projekteken is végigmegy, párhuzamosan.
- **Nincs LLM-hívás, nincs költség.** A kódot és a Markdownt a graphify
  szerkezeti (AST) kinyerése dolgozza fel, minden más szabályalapú.
- **A nem kódfájlok is a gráfban vannak.** Kép, videó, PDF, office és HTML
  fájlcsomópontként kerül be, a mappaszerkezet pedig projekt, mappa, fájl élekként.
  Így a kreatívok és a kód egy gráfban kapcsolódik.
- **Gyorsítótár.** A változatlan fájlokat nem dolgozza fel újra, a frissítés
  pár másodperc.
- **Nem ír a projektmappákba.** Minden kimenet a `graphify-out/` alá kerül,
  a munkaterületen kívüli mappáké a `~/.projektterkep/` alá.
- **Gépies, de olvasható közösségnevek** LLM nélkül (almappa és a legkötöttebb
  csomópont alapján).

## Telepítés

```
uv tool install graphifyy
git clone https://github.com/makifej/projektterkep.git <munkaterület>/projektterkep
```

A szkript a graphify Pythonjával fut:

```
PY=~/.local/share/uv/tools/graphifyy/bin/python
```

## Használat

```
$PY projektterkep/projektterkep.py                    # minden projekt + összevont gráf
$PY projektterkep/projektterkep.py egyik masik        # csak ezek, utána újra összevon
$PY projektterkep/projektterkep.py --lista            # a projektek listája
$PY projektterkep/projektterkep.py --mappa ~/barmi    # tetszőleges mappa térképe
$PY projektterkep/projektterkep.py --hol ~/barmi      # hol van (lesz) a térképe
```

A munkaterület alapból a szkript mappája fölötti mappa. Máshol a
`PROJEKTTERKEP_GYOKER` környezeti változóval adható meg.

Kérdés a térképről a graphify saját parancsaival:

```
graphify query "hol küldjük el a levelet?" --graph graphify-out/projektek/<projekt>/graph.json
graphify explain "<csomópont>" --graph ...
graphify path "A" "B" --graph ...
```

## Tokenmérés

A Projekttérkép saját mérője megmutatja, mennyit spórol, LLM nélkül, ingyen.
A token becslés: karakter / 4, mindkét oldalra ugyanaz a szabály.

**Kérdésenként**, ugyanarra a kérdésre két út:

```
$PY projektterkep/projektterkep.py <projekt> --meres "hol küldjük el a levelet?"
$PY projektterkep/projektterkep.py --mappa ~/barmi --meres "..."
$PY projektterkep/projektterkep.py --meres-osszesites
```

- *Térkép nélkül:* kulcsszavas keresés a projekt szövegfájljaiban, és az 5 legtöbb
  kulcsszót tartalmazó fájl teljes beolvasása.
- *Térképpel:* a `graphify query` kimenete, plusz az első 3 megtalált hely körüli
  40-40 sor.

Minden mérés a `graphify-out/meresek.jsonl` naplóba kerül.

**Valós használat** a Claude Code munkamenet-naplóiból:

```
$PY projektterkep/projektterkep.py --elo-meres
```

Megszámolja a térkép-lekérdezéseket (`graphify query/explain/path`) és az utánuk
következő célzott (offset/limit) fájlolvasásokat, és összeveti azzal, amennyi a
teljes fájlok beolvasása lett volna.

**Helyi kimutatás** a mérésekről, élő kipróbálással és a projektek térképével:

```
$PY projektterkep/kimutatas.py        # http://localhost:4478
```

**Első mérésünk (2026-11-07):**

| Mérés | Térkép nélkül | Térképpel | Spórolás |
|---|---:|---:|---:|
| 6 kérdés, 3 projekt | 126 271 token | 21 268 token | 83,2% |
| Valós használat: 20 lekérdezés, 11 célzott olvasás | 161 721 token | 24 569 token | 84,8% |

A kérdések szórása nagy: 95% fölött volt, ahol a kulcsszó sok nagy fájlban
szerepelt, és -51%, ahol a keresés két kis fájlt talált, a térkép pedig
nagyobb kimenetet adott. Két kérdésnél a kulcsszavas keresés semmit nem talált
(a magyar ragozás miatt), ezek nem számítanak bele. A valós használatnál a
viszonyítási alap feltételezés: hogy térkép nélkül a teljes fájl kellett volna.

## Kimenet

- `graphify-out/projektek/<projekt>/`: `graph.json`, `GRAPH_REPORT.md`, `graph.html`
- `graphify-out/`: az összevont gráf ugyanezzel a három fájllal
- `graphify-out/projektek.json`: futási összesítő (fájl, csomópont, él, közösség)

## Claude Code-ban

A `skill/SKILL.md` egy kész skill: másold a `~/.claude/skills/projektterkep/`
mappába, és a Claude Code kódos kérdésnél a térképből indul.

## Korlátok

- A képek, videók és PDF-ek tartalmát nem olvassa ki, ahhoz LLM kellene.
- A közösségnevek gépiesek.

## Licenc és köszönet

A Projekttérkép saját kódja MIT licencű (`LICENSE`). A graphifyt nem tartalmazza,
hanem függőségként használja; a graphify a Graphify Labs munkája, Apache-2.0 és
MIT licenc alatt. Részletek: `NOTICE`.

---

## English

Projekttérkép is a code map for Claude Code, built on top of
[graphify](https://github.com/Graphify-Labs/graphify). It maps every project in a
multi-project workspace with no file-count limit and no LLM calls, adds non-code
files (images, video, PDF) and the folder structure to the graph, merges all
projects into one combined graph, and never writes into the project folders.
MIT licensed; graphify is used as a dependency (Apache-2.0 / MIT).
