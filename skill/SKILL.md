---
name: projektterkep
description: Projekttérkép, a munkaterület projektjeinek kódtérképe (graphify-gráf projektenként és összevonva, fájlkorlát és LLM nélkül). Használd, ha a kérdés egy projekt kódját, szerkezetét vagy összefüggéseit érinti (hol van egy függvény, mi hívja, mihez kapcsolódik egy fájl), vagy ha a térképet frissíteni kell.
---

# Projekttérkép

Kódos kérdésnél előbb a térképből dolgozz, és csak a gráf által megmutatott
fájlrészletet olvasd el. Ne olvass be teljes fájlokat elölről.

A lenti `<repo>` a projekttérkép mappája, `PY` a graphify Pythonja:
`PY=~/.local/share/uv/tools/graphifyy/bin/python`.

1. **Hol a térkép:** `$PY <repo>/projektterkep.py --hol <projektgyökér>`
2. **Elkészítés vagy frissítés**, ha nincs még, vagy változott a projekt
   (pár másodperc, nincs költség):
   `$PY <repo>/projektterkep.py --mappa <projektgyökér>`, a munkaterület
   projektjeinél `$PY <repo>/projektterkep.py <projektnév> --nincs-osszevonas`
3. **Lekérdezés:** `graphify query "<kérdés>" --graph <graph.json>`,
   részletek: `graphify explain "<név>" --graph ...`, út: `graphify path "A" "B" --graph ...`
4. **Csak ezután olvass fájlt**, a gráf `src=` és `loc=` adata alapján, célzottan.

Kivétel: ha a pontos fájlt és sort már tudod, vagy egyetlen célzott keresés elég.

## Korlátok

- Képek, videók és PDF-ek tartalmát nem ismeri, csak fájlcsomópontként vannak benne.
- A közösségnevek gépiesek.
- A projektmappákba nem ír, minden a `graphify-out` alá kerül.
