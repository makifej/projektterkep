#!/usr/bin/env python3
"""Projekttérkép: helyi kimutatás a tokenmérésről.

  $PY projektterkep/kimutatas.py            # http://localhost:4478
  $PY projektterkep/kimutatas.py --port 5000

Mutatja a valós használatot (Claude Code naplóiból, 10 percenként frissül), a
kérdésenkénti méréseket, és élőben futtat új mérést a megadott kérdésre.
A projektek térképe (graph.html) is innen nyitható meg.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ITT = Path(__file__).resolve().parent
sys.path.insert(0, str(ITT))
import meres as M  # noqa: E402
import projektterkep as P  # noqa: E402

NAPLO = P.KIMENET / "meresek.jsonl"
ELO = {"adat": None, "ido": None, "fut": False}
ZAR = threading.Lock()


def elo_frissit():
    while True:
        ELO["fut"] = True
        try:
            adat = M.elo_meres()
            with ZAR:
                ELO["adat"], ELO["ido"] = adat, time.strftime("%H:%M")
        except Exception as exc:  # a kimutatás akkor is menjen, ha a napló nem olvasható
            print("élő mérés hiba:", exc, file=sys.stderr)
        ELO["fut"] = False
        time.sleep(600)


def meresek() -> list[dict]:
    if not NAPLO.exists():
        return []
    return [json.loads(s) for s in NAPLO.read_text(encoding="utf-8").splitlines() if s.strip()]


def projektek_terkeppel() -> list[dict]:
    osszes = {}
    f = P.KIMENET / "projektek.json"
    if f.exists():
        for r in json.loads(f.read_text(encoding="utf-8")):
            osszes[r.get("projekt")] = r
    ki = []
    for p in P.projektek():
        if (P.PROJEKT_KIMENET / p.name / "graph.json").exists():
            r = osszes.get(p.name, {})
            ki.append({"nev": p.name, "csomopont": r.get("csomopont"), "el": r.get("el"),
                       "html": (P.PROJEKT_KIMENET / p.name / "graph.html").exists()})
    return ki


class Kezelo(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _kuld(self, kod, tipus, test: bytes):
        self.send_response(kod)
        self.send_header("Content-Type", tipus)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(test)

    def _json(self, adat, kod=200):
        self._kuld(kod, "application/json; charset=utf-8", json.dumps(adat, ensure_ascii=False).encode())

    def do_GET(self):
        ut = unquote(urlparse(self.path).path)
        if ut == "/":
            return self._kuld(200, "text/html; charset=utf-8", (ITT / "kimutatas.html").read_bytes())
        if ut == "/api/adat":
            with ZAR:
                elo, ido = ELO["adat"], ELO["ido"]
            return self._json({"elo": elo, "elo_ido": ido, "elo_fut": ELO["fut"],
                               "osszesites": M.osszesites(NAPLO), "meresek": meresek()[-50:][::-1],
                               "projektek": projektek_terkeppel(), "munkaterulet": P.GYOKER.name})
        if ut.startswith("/terkep/"):
            nev = ut.split("/")[2]
            if nev in {p.name for p in P.projektek()}:
                f = P.PROJEKT_KIMENET / nev / "graph.html"
                if f.is_file():
                    return self._kuld(200, "text/html; charset=utf-8", f.read_bytes())
            if nev == "_osszevont" and (P.KIMENET / "graph.html").is_file():
                return self._kuld(200, "text/html; charset=utf-8", (P.KIMENET / "graph.html").read_bytes())
        self._kuld(404, "text/plain; charset=utf-8", "Nincs ilyen oldal".encode())

    def do_POST(self):
        if urlparse(self.path).path != "/api/meres":
            return self._kuld(404, "text/plain", b"")
        try:
            hossz = int(self.headers.get("Content-Length", "0"))
            d = json.loads(self.rfile.read(min(hossz, 10_000)) or b"{}")
            nev, kerdes = str(d.get("projekt", "")), str(d.get("kerdes", "")).strip()[:300]
            if nev not in {p.name for p in P.projektek()} or not kerdes:
                return self._json({"hiba": "Válassz projektet és írj kérdést."}, 400)
            proj = P.GYOKER / nev
            graph = P.PROJEKT_KIMENET / nev / "graph.json"
            if not graph.exists():
                return self._json({"hiba": "Ennek a projektnek még nincs térképe."}, 400)
            return self._json(M.meres(proj, graph, proj.parent, kerdes, NAPLO))
        except Exception as exc:
            return self._json({"hiba": str(exc)[:300]}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4478)
    a = ap.parse_args()
    threading.Thread(target=elo_frissit, daemon=True).start()
    print(f"Projekttérkép kimutatás: http://localhost:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Kezelo).serve_forever()


if __name__ == "__main__":
    main()
