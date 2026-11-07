"""Projekttérkép: saját tokenmérés.

Kétféle mérés, mindkettő LLM nélkül, ingyen:

1. Kérdésenkénti összevetés (--meres): ugyanarra a kérdésre két út tokenigénye.
   - Térkép nélkül: kulcsszavas keresés a projekt fájljaiban, és a legjobb
     találatok teljes beolvasása (ezt csinálja egy agent térkép nélkül).
   - Térképpel: a graphify-lekérdezés kimenete, és a megtalált helyek körüli
     célzott fájlrészletek.

2. Valós használat (--elo-meres): a Claude Code munkamenet-naplóiból kikeresi,
   hányszor kérdezett a térképből az agent, és a lekérdezés után célzottan
   (offset/limit) beolvasott fájloknál mennyit olvasott ténylegesen, illetve
   mennyi lett volna a teljes fájl.

Tokenbecslés: karakter / 4. Ez becslés, nem a modell pontos tokenszámlálója,
de a két út összevetésénél mindkét oldalra ugyanaz a szabály vonatkozik.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

SZOVEG_KITERJ = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".md", ".mdx", ".json",
                 ".html", ".css", ".sh", ".zsh", ".yml", ".yaml", ".toml", ".txt", ".swift",
                 ".kt", ".java", ".go", ".rs", ".rb", ".php", ".sql", ".vue", ".svelte"}
KIHAGY_MAPPA = {".git", "node_modules", "__pycache__", "graphify-out", ".venv", "venv",
                "dist", "build", ".next"}
MAX_FAJL = 400_000            # ennél nagyobb fájlt (generált, minifikált) nem számolunk
TALALAT_FAJL = 5              # térkép nélkül ennyi legjobb találatot olvas be az agent
RESZLET_HELY = 3              # térképpel ennyi megtalált helyet nyit meg
RESZLET_SOR = 40              # helyenként ennyi sort olvas a megadott sor körül
STOPSZO = {"hogy", "hogyan", "milyen", "melyik", "mikor", "miért", "mert", "vagy", "van",
           "nincs", "ahol", "amit", "amely", "kell", "lesz", "volt", "egy", "ezt", "azt",
           "where", "what", "which", "does", "that", "this", "with", "from", "the", "and"}


def token(szoveg: str) -> int:
    return round(len(szoveg) / 4)


def _ekezet_nelkul(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def kulcsszavak(kerdes: str) -> list[str]:
    szavak = re.findall(r"[\wáéíóöőúüű]+", kerdes.lower())
    return sorted({_ekezet_nelkul(w) for w in szavak if len(w) >= 4 and w not in STOPSZO})


def _szovegfajlok(proj: Path):
    for p in proj.rglob("*"):
        if any(r in KIHAGY_MAPPA or r.startswith(".") for r in p.relative_to(proj).parts[:-1]):
            continue
        if p.is_file() and p.suffix.lower() in SZOVEG_KITERJ and p.stat().st_size <= MAX_FAJL:
            yield p


def terkep_nelkul(proj: Path, kerdes: str) -> dict:
    """Kulcsszavas keresés, a legtöbb kulcsszót tartalmazó fájlok teljes beolvasása."""
    szavak = kulcsszavak(kerdes)
    rangsor = []
    for f in _szovegfajlok(proj):
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        kis = _ekezet_nelkul(t.lower())
        talalt = sum(1 for w in szavak if w in kis)
        if talalt:
            rangsor.append((talalt, -len(t), f, t))
    rangsor.sort(reverse=True)
    olvasott = rangsor[:TALALAT_FAJL]
    return {"kulcsszavak": szavak, "talalt_fajl": len(rangsor),
            "olvasott": [str(f.relative_to(proj)) for _, _, f, _ in olvasott],
            "token": sum(token(t) for _, _, _, t in olvasott)}


def graphify_parancs() -> list[str]:
    g = shutil.which("graphify")
    return [g] if g else [sys.executable, "-m", "graphify"]


def terkeppel(graph: Path, gyoker: Path, kerdes: str) -> dict:
    """A graphify-lekérdezés kimenete plusz a megtalált helyek körüli célzott részletek."""
    r = subprocess.run(graphify_parancs() + ["query", kerdes, "--graph", str(graph)],
                       capture_output=True, text=True, timeout=120)
    kimenet = r.stdout
    helyek = []
    for m in re.finditer(r"^NODE .*?\[src=(\S+) loc=L(\d+)", kimenet, re.M):
        h = (m.group(1), int(m.group(2)))
        if h not in helyek:
            helyek.append(h)
    reszlet_token, megnyitott = 0, []
    for src, sor in helyek:
        if len(megnyitott) >= RESZLET_HELY:
            break
        f = gyoker / src
        if not f.is_file() or f.suffix.lower() not in SZOVEG_KITERJ:
            continue
        sorok = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        tol = max(0, sor - 1 - RESZLET_SOR // 4)
        reszlet_token += token("\n".join(sorok[tol:tol + RESZLET_SOR]))
        megnyitott.append(f"{src}:{sor}")
    return {"lekerdezes_token": token(kimenet), "reszletek": megnyitott,
            "reszlet_token": reszlet_token, "token": token(kimenet) + reszlet_token}


def meres(proj: Path, graph: Path, gyoker: Path, kerdes: str, naplo: Path) -> dict:
    nelkul = terkep_nelkul(proj, kerdes)
    vele = terkeppel(graph, gyoker, kerdes)
    sporolt = nelkul["token"] - vele["token"]
    eredm = {"ido": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "projekt": proj.name, "kerdes": kerdes,
             "terkep_nelkul": nelkul, "terkeppel": vele, "sporolt_token": sporolt,
             "sporolt_szazalek": round(100 * sporolt / nelkul["token"], 1) if nelkul["token"] else None}
    naplo.parent.mkdir(parents=True, exist_ok=True)
    with naplo.open("a", encoding="utf-8") as f:
        f.write(json.dumps(eredm, ensure_ascii=False) + "\n")
    return eredm


def osszesites(naplo: Path) -> dict:
    if not naplo.exists():
        return {"meres": 0}
    sorok = [json.loads(s) for s in naplo.read_text(encoding="utf-8").splitlines() if s.strip()]
    ervenyes = [s for s in sorok if s["terkep_nelkul"]["token"] > 0]
    nelkul = sum(s["terkep_nelkul"]["token"] for s in ervenyes)
    vele = sum(s["terkeppel"]["token"] for s in ervenyes)
    return {"meres": len(sorok), "ervenyes": len(ervenyes), "terkep_nelkul_token": nelkul,
            "terkeppel_token": vele, "sporolt_token": nelkul - vele,
            "sporolt_szazalek": round(100 * (nelkul - vele) / nelkul, 1) if nelkul else None}


# ---------------------------------------------------------------- valós használat

GRAPHIFY_HIVAS = re.compile(r"\bgraphify\s+(query|explain|path)\b")


def _tartalom_szoveg(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def elo_meres(naplo_gyoker: Path = Path.home() / ".claude" / "projects", ablak: int = 12) -> dict:
    """A munkamenet-naplókból: térkép-lekérdezések, és az utánuk következő célzott
    (offset/limit) fájlolvasások tényleges és teljes mérete. A munkamenetek
    másolatai miatt minden eszközhívás azonosító szerint csak egyszer számít."""
    latott, lekerdezes, lek_token = set(), 0, 0
    celzott, olvasott_token, teljes_token = 0, 0, 0
    for fajl in naplo_gyoker.glob("*/*.jsonl"):
        hivasok, eredmenyek = [], {}
        try:
            sorok = fajl.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for s in sorok:
            if "tool_use" not in s and "tool_result" not in s:
                continue
            try:
                d = json.loads(s)
            except ValueError:
                continue
            for b in (d.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    hivasok.append(b)
                elif b.get("type") == "tool_result":
                    eredmenyek[b.get("tool_use_id")] = _tartalom_szoveg(b.get("content"))
        utolso_lek = None
        for i, h in enumerate(hivasok):
            hid, nev, inp = h.get("id"), h.get("name"), h.get("input") or {}
            if nev == "Bash" and GRAPHIFY_HIVAS.search(inp.get("command", "")):
                utolso_lek = i
                if hid not in latott and hid in eredmenyek:
                    latott.add(hid)
                    lekerdezes += 1
                    lek_token += token(eredmenyek[hid])
            elif (nev == "Read" and utolso_lek is not None and i - utolso_lek <= ablak
                  and ("offset" in inp or "limit" in inp) and hid not in latott):
                f = Path(inp.get("file_path", ""))
                if hid in eredmenyek and f.is_file() and f.stat().st_size <= MAX_FAJL * 5:
                    latott.add(hid)
                    celzott += 1
                    olvasott_token += token(eredmenyek[hid])
                    teljes_token += token(f.read_text(encoding="utf-8", errors="ignore"))
    vele = lek_token + olvasott_token
    return {"lekerdezes": lekerdezes, "lekerdezes_token": lek_token, "celzott_olvasas": celzott,
            "celzott_token": olvasott_token, "teljes_fajl_token": teljes_token,
            "terkeppel_token": vele, "sporolt_token": teljes_token - vele,
            "sporolt_szazalek": round(100 * (teljes_token - vele) / teljes_token, 1) if teljes_token else None}
