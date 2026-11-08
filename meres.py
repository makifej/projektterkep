"""Projekttérkép: saját tokenmérés.

Kétféle mérés, mindkettő LLM nélkül, ingyen:

1. Kérdésenkénti összevetés (--meres): ugyanarra a kérdésre két út tokenigénye.
   - Térkép nélkül: kulcsszavas keresés a projekt fájljaiban, és a legjobb
     találatok teljes beolvasása (ezt csinálja egy agent térkép nélkül).
   - Térképpel: a graphify-lekérdezés kimenete, és a megtalált helyek körüli
     célzott fájlrészletek.

2. Valós használat (--elo-meres): a Claude Code munkamenet-naplóiból kikeresi,
   hányszor kérdezett a térképből az agent és kapott választ, és a lekérdezés
   után célzottan beolvasott fájloknál (Read offset/limit-tel, vagy a parancssorban
   sed -n, head, tail, grep környező sorokkal) mennyit olvasott ténylegesen,
   illetve mennyi lett volna a teljes fájl.

Tokenbecslés: karakter / 4. Ez becslés, nem a modell pontos tokenszámlálója,
de a két út összevetésénél mindkét oldalra ugyanaz a szabály vonatkozik.
"""
from __future__ import annotations

import json
import re
import shlex
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

# Térkép-lekérdezés: graphify query/explain/path hívás parancs helyén (a sor elején,
# vagy ; && || | ( $( do then else timeout python -m után), nem ugyanezek a szavak
# idézőjelben, grep-mintában vagy dokumentációt író heredocban.
GRAPHIFY_HIVAS = re.compile(
    r"(?:^|(?<!\\)[;&|()\n]|\$\(|\b(?:do|then|else)\s|\btimeout\s+(?:-\S+\s+)*\S+\s|-m\s)"
    r"\s*(?:[\w./~$-]*/)?graphify\s+(?:query|explain|path)\b")
# Csak akkor számít, ha válaszolt: a kimenetben a graphify saját válaszformája van.
# Az elbukott hívás ("command not found", "No matching nodes") semmit nem adott.
TERKEP_VALASZ = re.compile(r"Graph: [^\n]*\| Traversal:|Shortest path \(\d|^Node: [^\n]+\n\s+ID:"
                           r"|NODE [^\n]*\[src=", re.M)


def _tartalom_szoveg(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


GRAF_UTVONAL = re.compile(r"([^\s\"'=]*?)/?([^/\s\"'=]+)/graph\.json")


def _projekt_neve(parancs: str) -> str:
    """Melyik projektről kérdezett, a parancsban lévő graph.json útvonalából."""
    for m in GRAF_UTVONAL.finditer(parancs):
        nev = re.sub(r"-[0-9a-f]{8}$", "", m.group(2)).strip("`*_.:,;()[]{}")
        if "<" in nev or "$" in nev or not nev or nev == "graphify-out":
            continue
        return nev
    return ""


# Célzott olvasás a parancssorban: `sed -n 'A,Bp' fájl`, `head -n N fájl`,
# `tail -n N fájl`, `grep -A/-B/-C ... fájl`, akkor is, ha a fájl a csővezetékben
# előbb álló `cat`/`nl`-ből jön. Csak létező, MAX_FAJL alatti szövegfájl számít,
# így egy nagy adatfájlba vagy naplóba nézés sosem könyvelődik spórolásnak.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
LANC_VALASZTO = {";", "&&", "||", "&", "(", ")", ";;"}
ELOTAG = {"do", "then", "else", "if", "while", "until", "!", "time", "command", "builtin", "exec"}


def _lepesek(parancs: str) -> list[list[list[str]]]:
    """A parancs csővezetékekre bontva, azok lépésekre (szólistákra)."""
    parancs = HEREDOC.sub(lambda m: m.group(3), parancs).replace("\n", " ; ")
    try:
        lex = shlex.shlex(parancs, posix=True, punctuation_chars=True)
        lex.whitespace_split, lex.commenters = True, ""
        szavak = list(lex)
    except ValueError:
        return []
    csovek, lepes, cso = [], [], []
    for t in szavak + [";"]:
        if t in LANC_VALASZTO or t == "|":
            if lepes:
                cso.append(lepes)
            lepes = []
            if t != "|" and cso:
                csovek.append(cso)
                cso = []
        else:
            lepes.append(t)
    tiszta = []
    for cso in csovek:
        tc = []
        for le in cso:
            ki, i = [], 0
            while i < len(le):                      # átirányítás és célpontja ki
                t = le[i]
                if t and set(t) <= set("<>&"):
                    i += 2
                    continue
                if t.isdigit() and i + 1 < len(le) and le[i + 1][:1] in "<>":
                    i += 1
                    continue
                ki.append(t)
                i += 1
            while ki and (ki[0] in ELOTAG or re.match(r"^\w+=", ki[0])):
                ki = ki[1:]
            if ki and Path(ki[0]).name == "timeout":    # timeout [kapcsolók] N parancs ...
                ki = ki[1:]
                while ki and ki[0].startswith("-"):
                    ki = ki[1:]
                ki = ki[1:]
            if ki:
                tc.append(ki)
        if tc:
            tiszta.append(tc)
    return tiszta


def _operandusok(arg: list[str], ertekes: set[str]) -> list[str]:
    ki, i = [], 0
    while i < len(arg):
        a = arg[i]
        if a in ertekes:
            i += 2
            continue
        if a.startswith("-") and len(a) > 1:
            i += 1
            continue
        ki.append(a)
        i += 1
    return ki


def _lepes_olvas(le: list[str]) -> tuple[str | None, list[str]]:
    """('celzott', fájlok), ('forras', fájlok) cat/nl esetén, vagy (None, [])."""
    nev, arg = Path(le[0]).name, le[1:]
    if nev == "sed":
        if any(a.startswith("-i") or a.startswith("--in-place") for a in arg):
            return None, []
        szkriptek, maradek, csendes, i = [], [], False, 0
        while i < len(arg):
            a = arg[i]
            if a in ("-e", "--expression"):
                szkriptek.append(arg[i + 1] if i + 1 < len(arg) else "")
                i += 2
                continue
            if a in ("-f", "--file"):
                return None, []
            if a in ("--quiet", "--silent") or (re.match(r"^-[a-zA-Z]+$", a) and "n" in a):
                csendes = True
            elif not a.startswith("-") or a == "-":
                maradek.append(a)
            i += 1
        if not szkriptek and maradek:
            szkriptek, maradek = [maradek[0]], maradek[1:]
        jo = csendes and szkriptek and all(x.strip().rstrip(";").strip().endswith("p") for x in szkriptek)
        return ("celzott", maradek) if jo else (None, [])
    if nev in ("head", "tail"):
        if any(a in ("-f", "-F", "--follow") for a in arg):
            return None, []
        return "celzott", _operandusok(arg, {"-n", "-c", "--lines", "--bytes"})
    if nev in ("grep", "egrep", "fgrep"):
        kornyezet, minta, fajlok, i = False, False, [], 0
        while i < len(arg):
            a = arg[i]
            if a in ("-A", "-B", "-C", "--context", "--after-context", "--before-context"):
                kornyezet, i = True, i + 2
                continue
            if re.match(r"^-[ABC]\d+$", a) or re.match(r"^--(after-|before-)?context=", a):
                kornyezet, i = True, i + 1
                continue
            if a in ("-e", "--regexp"):
                minta, i = True, i + 2
                continue
            if a in ("-f", "--file", "-m", "--max-count", "-d", "-D", "--include", "--exclude"):
                i += 2
                continue
            if a.startswith("--"):
                if a in ("--recursive", "--files-with-matches", "--files-without-match", "--count"):
                    return None, []
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                if re.search(r"[rRlLc]", a[1:]):
                    return None, []
                if re.search(r"[ABC]\d*$", a):
                    kornyezet = True
                i += 1
                continue
            if not minta:
                minta = True
            else:
                fajlok.append(a)
            i += 1
        return ("celzott", fajlok) if kornyezet else (None, [])
    if nev == "cat":
        return "forras", _operandusok(arg, set())
    if nev == "nl":
        return "forras", _operandusok(arg, {"-b", "-n", "-w", "-s", "-v", "-i", "-d", "-h", "-f", "-l"})
    return None, []


def _feloldas(arg: str, itt: Path | None) -> Path | None:
    arg = arg.replace("$HOME", str(Path.home())).replace("${HOME}", str(Path.home()))
    if "$" in arg or "*" in arg or "?" in arg or arg == "-":
        return None
    p = Path(arg).expanduser()
    if not p.is_absolute():
        if itt is None:
            return None
        p = itt / p
    return p


def parancssori_celzott_fajlok(parancs: str, cwd: str | None) -> list[Path]:
    """Létező szövegfájlok (MAX_FAJL-ig), amelyeket egy parancs csak részben olvasott."""
    itt = Path(cwd) if cwd else None
    talalt = []
    for cso in _lepesek(parancs):
        if cso[0][0] == "cd":
            itt = _feloldas(cso[0][1] if len(cso[0]) > 1 else "~", itt)
            continue
        forras = []
        for le in cso:
            fajta, arg = _lepes_olvas(le)
            if fajta == "forras":
                forras = arg
                continue
            if fajta == "celzott":
                for a in (arg or forras):
                    p = _feloldas(a, itt)
                    try:
                        jo = (p is not None and p.suffix.lower() in SZOVEG_KITERJ and p.is_file()
                              and p.stat().st_size <= MAX_FAJL)
                    except OSError:
                        jo = False
                    if jo and p not in talalt:
                        talalt.append(p)
            forras = []
    return talalt


TERKEP_FORRAS = re.compile(r"\[src=([^\s\]]+)|^\s*Source:\s+(\S+)", re.M)


def _gyokerek(valasz: str, projekt: str) -> set[str]:
    """A projektmappák, amelyekről a térkép válaszolt: a mutatott forrásútvonalak első
    része ([src=quicknews/build/x.py] -> quicknews) és a lekérdezett gráf projektje."""
    forrasok = {(a or b).lstrip("./") for a, b in TERKEP_FORRAS.findall(valasz)}
    g = {x.split("/")[0] for x in forrasok if "/" in x}
    if projekt:
        g.add(projekt)
    return {x for x in g if x and x not in (".", "..")}


def _projektben(p: Path, gyokerek: set[str]) -> bool:
    s = str(p)
    return any(f"/{g}/" in s for g in gyokerek)


def elo_meres(naplo_gyoker: Path = Path.home() / ".claude" / "projects", ablak: int = 12) -> dict:
    """A munkamenet-naplókból: a választ adó térkép-lekérdezések, és az utánuk
    következő célzott fájlolvasások (Read offset/limit-tel, vagy a parancssorban
    sed -n, head, tail, grep környező sorokkal) tényleges és teljes mérete. Csak a
    lekérdezett projekt fájljai számítanak. A munkamenetek másolatai miatt minden
    eszközhívás azonosító szerint csak egyszer számít."""
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
                    hivasok.append((b, d.get("cwd")))
                elif b.get("type") == "tool_result":
                    eredmenyek[b.get("tool_use_id")] = _tartalom_szoveg(b.get("content"))
        utolso_lek, gyokerek = None, set()
        for i, (h, cwd) in enumerate(hivasok):
            hid, nev, inp = h.get("id"), h.get("name"), h.get("input") or {}
            if (nev == "Bash" and GRAPHIFY_HIVAS.search(inp.get("command", ""))
                    and TERKEP_VALASZ.search(eredmenyek.get(hid, ""))):
                uj = _gyokerek(eredmenyek[hid], _projekt_neve(inp.get("command", "")))
                gyokerek = gyokerek | uj if utolso_lek is not None and i - utolso_lek <= ablak else uj
                utolso_lek = i
                if hid not in latott:
                    latott.add(hid)
                    lekerdezes += 1
                    lek_token += token(eredmenyek[hid])
            elif (nev == "Read" and utolso_lek is not None and i - utolso_lek <= ablak
                  and ("offset" in inp or "limit" in inp) and hid not in latott):
                f = Path(inp.get("file_path", ""))
                if (hid in eredmenyek and f.is_file() and f.stat().st_size <= MAX_FAJL * 5
                        and _projektben(f, gyokerek)):
                    latott.add(hid)
                    celzott += 1
                    olvasott_token += token(eredmenyek[hid])
                    teljes_token += token(f.read_text(encoding="utf-8", errors="ignore"))
            elif (nev == "Bash" and utolso_lek is not None and i - utolso_lek <= ablak
                  and hid not in latott and hid in eredmenyek):
                # csak a lekérdezett projekt fájljai: egy jegyzetbe, naplóba vagy más
                # fájlba nézés közvetlenül a lekérdezés után nem a térkép spórolása
                fajlok = [f for f in parancssori_celzott_fajlok(inp.get("command", ""), cwd)
                          if _projektben(f, gyokerek)]
                if fajlok:
                    latott.add(hid)
                    teljes = sum(token(f.read_text(encoding="utf-8", errors="ignore")) for f in fajlok)
                    celzott += len(fajlok)
                    # a teljes kimenet számít térképpel olvasottnak, a máshonnan jött sorok
                    # is, de soha nem több, mint maguk a teljes fájlok
                    olvasott_token += min(token(eredmenyek[hid]), teljes)
                    teljes_token += teljes
    vele = lek_token + olvasott_token
    return {"lekerdezes": lekerdezes, "lekerdezes_token": lek_token, "celzott_olvasas": celzott,
            "celzott_token": olvasott_token, "teljes_fajl_token": teljes_token,
            "terkeppel_token": vele, "sporolt_token": teljes_token - vele,
            "sporolt_szazalek": round(100 * (teljes_token - vele) / teljes_token, 1) if teljes_token else None}
