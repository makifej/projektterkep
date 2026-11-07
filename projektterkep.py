#!/usr/bin/env python3
"""Projekttérkép: projektenkénti kódtérkép Claude Code-hoz, a graphify motorjára építve.

A munkaterület (alapból a szkript mappája fölötti mappa, vagy a
PROJEKTTERKEP_GYOKER környezeti változó) minden első szintű almappája egy
projekt. Mindegyikből ugyanazt a három kimenetet készíti, amit a graphify:
graph.json, GRAPH_REPORT.md, graph.html. A végén egy összevont gráf is készül
az összes projektből.

Ingyenes rész (alapértelmezés, nincs LLM-hívás):
  - kód és Markdown: a graphify saját szerkezeti (AST) kinyerése,
  - minden más fájl (kép, videó, PDF, txt, html, office): fájlcsomópont,
  - a mappaszerkezet: projekt -> mappa -> fájl "contains" élek, így a média
    és a kód egy gráfban kapcsolódik.

Futtatás (a graphify saját Pythonjával):
  PY=~/.local/share/uv/tools/graphifyy/bin/python
  $PY projektterkep/projektterkep.py                 # minden projekt + összevont gráf
  $PY projektterkep/projektterkep.py egyik masik    # csak ezek
  $PY projektterkep/projektterkep.py --lista         # projektek listája
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

GYOKER = Path(os.environ.get("PROJEKTTERKEP_GYOKER") or Path(__file__).resolve().parent.parent).expanduser().resolve()
KIMENET = GYOKER / "graphify-out"
PROJEKT_KIMENET = KIMENET / "projektek"
KIHAGY = {"graphify-out", "projektterkep", Path(__file__).resolve().parent.name, "node_modules", "__pycache__"}
# A munkaterületen kívüli mappák térképe ide kerül: ~/.projektterkep/<mappanév>/
KULSO_KIMENET = Path.home() / ".projektterkep"

# Ezeket a graphify extract() szerkezetileg fel tudja dolgozni LLM nélkül.
SZERKEZETI_KITERJ = {".md", ".mdx", ".qmd", ".skill"}


def projektek() -> list[Path]:
    return sorted(
        p for p in GYOKER.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in KIHAGY
    )


def kimenet_mappa(proj: Path) -> Path:
    """Hova kerül egy projekt térképe. A munkaterület projektjei (a szimbolikus
    linkkel bekötöttek is) a munkaterület graphify-out mappája alá,
    minden más mappa a ~/.projektterkep alá."""
    cel = proj.resolve()
    for p in projektek():
        if p.resolve() == cel:
            return PROJEKT_KIMENET / p.name
    return KULSO_KIMENET / cel.name


def _rel(p: Path, gyoker: Path) -> str:
    try:
        return p.resolve().relative_to(gyoker.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _kozosseg_nevek(G, communities: dict[int, list[str]]) -> dict[int, str]:
    """LLM nélküli közösségnév: a leggyakoribb almappa és a legkötöttebb csomópont."""
    nevek = {}
    for cid, tagok in communities.items():
        mappak = Counter()
        for n in tagok:
            sf = G.nodes[n].get("source_file") or ""
            reszek = sf.split("/")
            mappak[reszek[1] if len(reszek) > 2 else (reszek[0] if reszek else "")] += 1
        fo = max(tagok, key=lambda n: G.degree(n))
        cimke = str(G.nodes[fo].get("label", fo))[:40]
        mappa = mappak.most_common(1)[0][0] if mappak else ""
        nevek[cid] = f"{mappa} · {cimke}" if mappa and mappa not in cimke else cimke
    return nevek


def _gyart(extraction: dict, gyoker_str: str, kimenet: Path, detect_eredm: dict, cim: str) -> dict:
    """graph.json + GRAPH_REPORT.md + graph.html, a graphify saját függvényeivel."""
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.analyze import god_nodes, surprising_connections, suggest_questions
    from graphify.report import generate
    from graphify.export import to_json
    from graphify.exporters.html import to_html

    kimenet.mkdir(parents=True, exist_ok=True)
    G = build_from_json(extraction, root=gyoker_str, directed=False)
    if G.number_of_nodes() == 0:
        return {"projekt": cim, "csomopont": 0, "el": 0, "kozosseg": 0, "megj": "üres"}
    communities = cluster(G)
    cohesion = score_all(G, communities)
    labels = _kozosseg_nevek(G, communities)
    gods = god_nodes(G)
    surprises = surprising_connections(G, communities)
    questions = suggest_questions(G, communities, labels)
    to_json(G, communities, str(kimenet / "graph.json"), force=True, community_labels=labels)
    report = generate(G, communities, cohesion, labels, gods, surprises, detect_eredm,
                      {"input": 0, "output": 0}, gyoker_str, suggested_questions=questions)
    (kimenet / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    (kimenet / ".graphify_labels.json").write_text(
        json.dumps({str(k): v for k, v in labels.items()}, ensure_ascii=False), encoding="utf-8")
    try:
        to_html(G, communities, str(kimenet / "graph.html"), community_labels=labels, node_limit=5000)
    except Exception as exc:  # nagyon nagy gráfnál a nézet elmaradhat, a JSON megvan
        print(f"[{cim}] HTML kihagyva: {exc}", file=sys.stderr)
    return {"projekt": cim, "csomopont": G.number_of_nodes(), "el": G.number_of_edges(),
            "kozosseg": len(communities)}


def projekt_extrakcio(proj: Path) -> tuple[dict, dict]:
    """Egy projekt teljes, korlát nélküli kinyerése. Visszaad: (extraction, detect)."""
    from graphify.detect import detect
    from graphify.extract import collect_files, extract

    gyoker = proj.parent
    det = detect(proj, cache_root=kimenet_mappa(proj))
    fajlok = {k: [Path(f) for f in v] for k, v in det["files"].items()}

    # 1) Szerkezeti kinyerés: kód + Markdown
    kod = []
    for f in fajlok.get("code", []):
        kod.extend(collect_files(f) if f.is_dir() else [f])
    md = [f for f in fajlok.get("document", []) if f.suffix.lower() in SZERKEZETI_KITERJ]
    szerk = kod + md
    cache = kimenet_mappa(proj)
    cache.mkdir(parents=True, exist_ok=True)
    if szerk:
        ext = extract(szerk, cache_root=cache, root=gyoker)
    else:
        ext = {"nodes": [], "edges": []}
    nodes = list(ext.get("nodes", []))
    edges = list(ext.get("edges", []))

    # A kinyert fájlok saját csomópontja (pl. a Markdown "page" csomópontja)
    fajl_csp: dict[str, str] = {}
    for n in nodes:
        sf = n.get("source_file")
        if not sf:
            continue
        # a fájlnévvel egyező címkéjű csomópont a fájl saját csomópontja, különben az első
        if n.get("label") == Path(sf).name or sf not in fajl_csp:
            fajl_csp[sf] = n["id"]

    # 2) Mappaszerkezet + nem szerkezeti fájlok (kép, videó, PDF, txt, html, office)
    ids = {n["id"] for n in nodes}
    proj_rel = _rel(proj, gyoker)
    proj_id = f"projekt::{proj_rel}"
    nodes.append({"id": proj_id, "label": proj.name, "file_type": "document", "node_kind": "projekt",
                  "source_file": proj_rel, "source_location": "L1", "_origin": "ast"})
    ids.add(proj_id)

    def mappa_id(rel_mappa: str) -> str:
        if rel_mappa == proj_rel:
            return proj_id
        mid = f"mappa::{rel_mappa}"
        if mid not in ids:
            ids.add(mid)
            nodes.append({"id": mid, "label": Path(rel_mappa).name + "/", "file_type": "document",
                          "node_kind": "mappa", "source_file": rel_mappa, "source_location": "L1",
                          "_origin": "ast"})
            szulo = Path(rel_mappa).parent.as_posix()
            edges.append({"source": mappa_id(szulo), "target": mid, "relation": "contains",
                          "confidence": "EXTRACTED", "source_file": rel_mappa, "source_location": "L1",
                          "weight": 1.0, "_origin": "ast"})
        return mid

    tipus = {"image": "image", "video": "video", "paper": "paper", "document": "document",
             "code": "code"}
    for kat, lista in fajlok.items():
        for f in lista:
            if f.is_dir():
                continue
            rel = _rel(f, gyoker)
            cel = fajl_csp.get(rel)
            if cel is None:
                cel = f"fajl::{rel}"
                if cel not in ids:
                    ids.add(cel)
                    nodes.append({"id": cel, "label": f.name, "file_type": tipus.get(kat, "document"),
                                  "node_kind": "fajl", "source_file": rel, "source_location": "L1",
                                  "_origin": "ast"})
            edges.append({"source": mappa_id(Path(rel).parent.as_posix()), "target": cel,
                          "relation": "contains", "confidence": "EXTRACTED", "source_file": rel,
                          "source_location": "L1", "weight": 1.0, "_origin": "ast"})

    return {"nodes": nodes, "edges": edges, "hyperedges": [],
            "input_tokens": 0, "output_tokens": 0}, det


def egy_projekt(proj_str: str) -> dict:
    proj = Path(proj_str)
    t0 = time.time()
    extraction, det = projekt_extrakcio(proj)
    kimenet = kimenet_mappa(proj)
    (kimenet / "extraction.json").write_text(json.dumps(extraction, ensure_ascii=False), encoding="utf-8")
    eredm = _gyart(extraction, str(proj.parent), kimenet, det, proj.name)
    eredm["fajl"] = det.get("total_files", 0)
    eredm["mp"] = round(time.time() - t0, 1)
    return eredm


def osszevon(nevek: list[str]) -> dict:
    """Az összes projekt kinyerését egy gráfba fűzi (az azonosítók projektenként egyediek,
    mert minden source_file a munkaterülethez képest relatív)."""
    nodes, edges, seen = [], [], set()
    for nev in nevek:
        f = PROJEKT_KIMENET / nev / "extraction.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        elotag = f"{nev}::"
        atnev = {}
        for n in d["nodes"]:
            uj = n["id"] if n["id"].startswith(("projekt::", "mappa::", "fajl::")) else elotag + n["id"]
            atnev[n["id"]] = uj
            if uj in seen:
                continue
            seen.add(uj)
            nodes.append({**n, "id": uj})
        for e in d["edges"]:
            s, t = atnev.get(e["source"]), atnev.get(e["target"])
            if s and t:
                edges.append({**e, "source": s, "target": t})
    gyoker_id = "munkaterulet::gyoker"
    nodes.append({"id": gyoker_id, "label": GYOKER.name, "file_type": "document",
                  "node_kind": "gyoker", "source_file": ".", "source_location": "L1", "_origin": "ast"})
    for n in list(nodes):
        if n.get("node_kind") == "projekt":
            edges.append({"source": gyoker_id, "target": n["id"], "relation": "contains",
                          "confidence": "EXTRACTED", "source_file": n["source_file"],
                          "source_location": "L1", "weight": 1.0, "_origin": "ast"})
    extraction = {"nodes": nodes, "edges": edges, "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
    det = {"total_files": 0, "total_words": 0, "files": {}}
    return _gyart(extraction, str(GYOKER), KIMENET, det, f"{GYOKER.name} (összevont)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("projektek", nargs="*", help="projektmappák nevei (alapból mind)")
    ap.add_argument("--mappa", action="append", default=[],
                    help="tetszőleges projektmappa útvonala (ismételhető); ilyenkor nincs összevonás")
    ap.add_argument("--hol", metavar="UTVONAL", help="kiírja, hol van (lesz) ennek a mappának a térképe")
    ap.add_argument("--lista", action="store_true", help="csak listázza a projekteket")
    ap.add_argument("--nincs-osszevonas", action="store_true", help="ne készüljön összevont gráf")
    ap.add_argument("--szalak", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    ap.add_argument("--meres", metavar="KERDES", action="append", default=[],
                    help="tokenmérés erre a kérdésre: térkép nélkül vs. térképpel (ismételhető); "
                         "a projektet a projektnév vagy a --mappa adja meg")
    ap.add_argument("--meres-osszesites", action="store_true", help="az eddigi mérések összesítése")
    ap.add_argument("--elo-meres", action="store_true",
                    help="valós használat a Claude Code munkamenet-naplóiból")
    a = ap.parse_args()

    if a.meres or a.meres_osszesites or a.elo_meres:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import meres as M
        naplo = KIMENET / "meresek.jsonl"
        if a.meres:
            celok = [Path(m).expanduser() for m in a.mappa] or [GYOKER / n for n in a.projektek]
            if len(celok) != 1 or not celok[0].is_dir():
                sys.exit("Méréshez pontosan egy projektet adj meg (projektnév vagy --mappa).")
            proj = celok[0]
            graph = kimenet_mappa(proj) / "graph.json"
            if not graph.exists():
                sys.exit(f"Nincs még térkép: {graph}. Előbb készítsd el.")
            if a.mappa:
                naplo = KULSO_KIMENET / "meresek.jsonl"
            for k in a.meres:
                print(json.dumps(M.meres(proj, graph, proj.parent, k, naplo), ensure_ascii=False))
        if a.meres_osszesites:
            print(json.dumps({"munkaterulet": M.osszesites(KIMENET / "meresek.jsonl"),
                              "kulso_mappak": M.osszesites(KULSO_KIMENET / "meresek.jsonl")},
                             ensure_ascii=False))
        if a.elo_meres:
            print(json.dumps(M.elo_meres(), ensure_ascii=False))
        return

    if a.hol:
        print(kimenet_mappa(Path(a.hol).expanduser()) / "graph.json")
        return
    if a.mappa:
        for m in a.mappa:
            proj = Path(m).expanduser()
            if not proj.is_dir():
                sys.exit(f"Nincs ilyen mappa: {m}")
            if proj.resolve() == Path.home().resolve():
                sys.exit("A teljes saját mappára nem készül térkép, adj meg egy projektmappát.")
            r = egy_projekt(str(proj))
            r["graph"] = str(kimenet_mappa(proj) / "graph.json")
            print(json.dumps(r, ensure_ascii=False), flush=True)
        return

    mind = projektek()
    if a.lista:
        for p in mind:
            print(p.name)
        return
    cel = [p for p in mind if not a.projektek or p.name in a.projektek]
    hiany = set(a.projektek) - {p.name for p in mind}
    if hiany:
        sys.exit(f"Nincs ilyen projekt: {', '.join(sorted(hiany))}")

    PROJEKT_KIMENET.mkdir(parents=True, exist_ok=True)
    osszes = []
    with ProcessPoolExecutor(max_workers=a.szalak) as ex:
        fut = {ex.submit(egy_projekt, str(p)): p.name for p in cel}
        for f in as_completed(fut):
            try:
                r = f.result()
            except Exception as exc:
                r = {"projekt": fut[f], "hiba": repr(exc)[:300]}
            osszes.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)

    (KIMENET / "projektek.json").write_text(
        json.dumps(sorted(osszes, key=lambda r: r["projekt"]), ensure_ascii=False, indent=1), encoding="utf-8")

    if not a.nincs_osszevonas:
        nevek = [p.name for p in mind if (PROJEKT_KIMENET / p.name / "extraction.json").exists()]
        print("Összevont:", json.dumps(osszevon(nevek), ensure_ascii=False))


if __name__ == "__main__":
    main()
