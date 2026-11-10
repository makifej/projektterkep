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
OLVASAS_SOR = 2000            # egy teljes Read legfeljebb ennyi sort ad vissza
MODSZER = 2                   # a számolási szabályok változata, minden eredmény mellé kerül
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


def teljes_olvasas_token(szoveg: str, akkori_sor: int | None = None, szamozott: bool = False) -> int:
    """Mennyi lett volna a fájl teljes beolvasása: egy Read legfeljebb OLVASAS_SOR sort ad
    vissza, a hosszabb fájl addig számít. Ha a napló megőrizte, hány soros volt a fájl
    az olvasáskor, az azóta megnőtt vagy rövidült fájlt arra arányosítja. A `szamozott`
    a Read sorszám-előtagját is hozzáadja, mert a kivonatokban is benne van."""
    sorok = szoveg.splitlines()
    latszik = min(len(sorok), OLVASAS_SOR)
    kar = len(szoveg) if len(sorok) <= OLVASAS_SOR else len("\n".join(sorok[:OLVASAS_SOR]))
    if akkori_sor and latszik and akkori_sor != len(sorok):
        akkor = min(akkori_sor, OLVASAS_SOR)
        kar, latszik = round(kar / latszik * akkor), akkor
    if szamozott:
        kar += sum(len(str(i)) + 1 for i in range(1, latszik + 1))
    return round(kar / 4)


def _terkep_fajljai(graph: Path | None, gyoker: Path | None) -> list[Path]:
    """A fájlok, amelyekből a térkép tartalmat ismer (függvényt, osztályt, címsort), nem
    azok, amelyeket csak a mappafában sorol fel. A térkép nélküli keresés ugyanezeken
    fut, így egy nagy adatfájl nem növelheti meg azt az oldalt."""
    if not graph or not gyoker:
        return []
    try:
        csomopontok = json.loads(graph.read_text(encoding="utf-8")).get("nodes", [])
    except (OSError, ValueError):
        return []
    fa = {"file", "folder", "project", "workspace", "fajl", "mappa", "projekt"}
    ki = []
    for src in sorted({n.get("source_file") for n in csomopontok
                       if n.get("source_file") and n.get("node_kind") not in fa}):
        f = gyoker / src
        try:
            if f.is_file() and f.suffix.lower() in SZOVEG_KITERJ and f.stat().st_size <= MAX_FAJL:
                ki.append(f)
        except OSError:
            pass
    return ki


def terkep_nelkul(proj: Path, kerdes: str, graph: Path | None = None, gyoker: Path | None = None) -> dict:
    """Kulcsszavas keresés, a legtöbb kulcsszót tartalmazó fájlok teljes beolvasása
    (fájlonként egy Read, vagyis legfeljebb OLVASAS_SOR sor)."""
    szavak = kulcsszavak(kerdes)
    rangsor = []
    for f in _terkep_fajljai(graph, gyoker) or _szovegfajlok(proj):
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
            "olvasott": [str(f.relative_to(proj)) if f.is_relative_to(proj) else f.name for _, _, f, _ in olvasott],
            "token": sum(teljes_olvasas_token(t) for _, _, _, t in olvasott)}


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
    nelkul = terkep_nelkul(proj, kerdes, graph, gyoker)
    vele = terkeppel(graph, gyoker, kerdes)
    sporolt = nelkul["token"] - vele["token"]
    eredm = {"ido": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "modszer": MODSZER, "projekt": proj.name, "kerdes": kerdes,
             "terkep_nelkul": nelkul, "terkeppel": vele, "sporolt_token": sporolt,
             "sporolt_szazalek": round(100 * sporolt / nelkul["token"], 1) if nelkul["token"] else None}
    naplo.parent.mkdir(parents=True, exist_ok=True)
    with naplo.open("a", encoding="utf-8") as f:
        f.write(json.dumps(eredm, ensure_ascii=False) + "\n")
    return eredm


def osszesites(naplo: Path) -> dict:
    if not naplo.exists():
        return {"meres": 0}
    sorok = []
    for s in naplo.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(s) if s.strip() else None
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("terkep_nelkul"), dict) and isinstance(r.get("terkeppel"), dict):
            sorok.append(r)
    # a korábbi számolási szabályokkal készült eredmények a naplóban maradnak, az összegből kimaradnak
    ervenyes = [s for s in sorok if s.get("modszer") == MODSZER and (s["terkep_nelkul"].get("token") or 0) > 0]
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


SED_CIM = re.compile(r"^\s*(\d+|\$)\s*(?:,\s*(\d+|\$|\+\d+))?\s*p\s*$")


def _sed_tartomany(szkriptek: list[str]):
    """Egy `sed -n` kiíró szkript sorszám-tartományai, vagy None, ha mintát használ."""
    tart = []
    for szkript in szkriptek:
        for p in filter(None, (c.strip() for c in szkript.split(";"))):
            m = SED_CIM.match(p)
            if not m:
                return None
            tart.append((m.group(1), m.group(2)))
    return ("sed", tart) if tart else None


def _darab(nev: str, arg: list[str]):
    """Mennyit ír ki a fájlból a head/tail: (név, egység, előjel, szám)."""
    egyseg, elojel, szam, i = "lines", "", 10, 0
    while i < len(arg):
        a, ert = arg[i], None
        if a in ("-n", "--lines", "-c", "--bytes"):
            ert, i = (arg[i + 1] if i + 1 < len(arg) else ""), i + 2
            egyseg = "bytes" if a in ("-c", "--bytes") else "lines"
        elif re.match(r"^--(lines|bytes)=", a):
            egyseg, ert, i = ("bytes" if a.startswith("--bytes") else "lines"), a.split("=", 1)[1], i + 1
        elif re.match(r"^-[nc][+-]?\d+$", a):
            egyseg, ert, i = ("bytes" if a[1] == "c" else "lines"), a[2:], i + 1
        elif re.match(r"^-\d+$", a):
            egyseg, ert, i = "lines", a[1:], i + 1
        else:
            i += 1
            continue
        m = re.match(r"^([+-]?)(\d+)$", ert or "")
        if not m:
            return None
        elojel, szam = m.group(1), int(m.group(2))
    return (nev, egyseg, elojel, szam)


def reszleges(szoveg: str, mod) -> bool | None:
    """Kevesebbet írt-e ki a parancs, mint a teljes fájl? None, ha ez a parancsból
    magából nem dönthető el (grep környező sorokkal, mintás sed)."""
    if mod is None:
        return None
    n = len(szoveg.splitlines())
    if mod[0] == "sed":
        lefedett = set()
        for a, b in mod[1]:
            eleje = n if a == "$" else int(a)
            if b is None:
                vege = eleje
            elif b == "$":
                vege = n
            elif b.startswith("+"):
                vege = eleje + int(b[1:])
            else:
                vege = int(b)
            lefedett.update(range(max(1, eleje), min(n, vege) + 1))
        return len(lefedett) < n
    nev, egyseg, elojel, szam = mod
    meret = len(szoveg.encode("utf-8")) if egyseg == "bytes" else n
    if nev == "head":
        return szam > 0 if elojel == "-" else szam < meret
    return szam > 1 if elojel == "+" else szam < meret


def _lepes_olvas(le: list[str]) -> tuple[str | None, list[str], object]:
    """('celzott', fájlok, mód), ('forras', fájlok, None) cat/nl esetén, vagy (None, [], None)."""
    nev, arg = Path(le[0]).name, le[1:]
    if nev == "sed":
        if any(a.startswith("-i") or a.startswith("--in-place") for a in arg):
            return None, [], None
        szkriptek, maradek, csendes, i = [], [], False, 0
        while i < len(arg):
            a = arg[i]
            if a in ("-e", "--expression"):
                szkriptek.append(arg[i + 1] if i + 1 < len(arg) else "")
                i += 2
                continue
            if a in ("-f", "--file"):
                return None, [], None
            if a in ("--quiet", "--silent") or (re.match(r"^-[a-zA-Z]+$", a) and "n" in a):
                csendes = True
            elif not a.startswith("-") or a == "-":
                maradek.append(a)
            i += 1
        if not szkriptek and maradek:
            szkriptek, maradek = [maradek[0]], maradek[1:]
        jo = csendes and szkriptek and all(x.strip().rstrip(";").strip().endswith("p") for x in szkriptek)
        return ("celzott", maradek, _sed_tartomany(szkriptek)) if jo else (None, [], None)
    if nev in ("head", "tail"):
        if any(a in ("-f", "-F", "--follow") for a in arg):
            return None, [], None
        return "celzott", _operandusok(arg, {"-n", "-c", "--lines", "--bytes"}), _darab(nev, arg)
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
                    return None, [], None
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                if re.search(r"[rRlLc]", a[1:]):
                    return None, [], None
                if re.search(r"[ABC]\d*$", a):
                    kornyezet = True
                i += 1
                continue
            if not minta:
                minta = True
            else:
                fajlok.append(a)
            i += 1
        return ("celzott", fajlok, None) if kornyezet else (None, [], None)
    if nev == "cat":
        return "forras", _operandusok(arg, set()), None
    if nev == "nl":
        return "forras", _operandusok(arg, {"-b", "-n", "-w", "-s", "-v", "-i", "-d", "-h", "-f", "-l"}), None
    return None, [], None


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


def parancssori_celzott_fajlok(parancs: str, cwd: str | None) -> list[tuple[Path, object]]:
    """Létező szövegfájlok (MAX_FAJL-ig), amelyekből egy parancs olvasott, mindegyik
    azzal együtt, hogy mennyit írt ki belőle (lásd reszleges)."""
    itt = Path(cwd) if cwd else None
    talalt = []
    for cso in _lepesek(parancs):
        if cso[0][0] == "cd":
            itt = _feloldas(cso[0][1] if len(cso[0]) > 1 else "~", itt)
            continue
        forras = []
        for le in cso:
            fajta, arg, mod = _lepes_olvas(le)
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
                    if jo and p not in [f for f, _ in talalt]:
                        talalt.append((p, mod))
            forras = []
    return talalt


TERKEP_FORRAS = re.compile(r"\[src=([^\s\]]+)|^\s*Source:\s+(\S+)|\]\s+(\S+?):L\d+\s*$", re.M)
# amit egy Read visszaadott, a napló saját feljegyzéséből: mutatott sorok, első sor, a fájl sorai
READ_ADAT = re.compile(r'"numLines"\s*:\s*(\d+)\s*,\s*"startLine"\s*:\s*(\d+)\s*,\s*"totalLines"\s*:\s*(\d+)')


def terkep_forrasok(valasz: str) -> set[str]:
    """A forrásfájlok, amelyeket a térkép válasza megnevezett."""
    return {(a or b or c).lstrip("./") for a, b, c in TERKEP_FORRAS.findall(valasz) if (a or b or c).strip("./")}


def _megnevezte(p: Path, forrasok: set[str]) -> bool:
    s = str(p)
    return any(s.endswith("/" + x) for x in forrasok)


def parancssori_teljes_fajlok(parancs: str, cwd: str | None) -> list[Path]:
    """Szövegfájlok, amelyeket egy parancs egészben kiírt: `cat fájl` vagy `nl fájl`,
    ami után a csővezetékben nincs más."""
    itt = Path(cwd) if cwd else None
    ki = []
    for cso in _lepesek(parancs):
        if cso[0][0] == "cd":
            itt = _feloldas(cso[0][1] if len(cso[0]) > 1 else "~", itt)
            continue
        fajta, arg, _ = _lepes_olvas(cso[-1])
        if fajta == "forras":
            for a in arg:
                p = _feloldas(a, itt)
                try:
                    if p is not None and p.suffix.lower() in SZOVEG_KITERJ and p.is_file():
                        ki.append(p)
                except OSError:
                    pass
    return ki


def _gyokerek(valasz: str, projekt: str) -> set[str]:
    """A projektmappák, amelyekről a térkép válaszolt: a mutatott forrásútvonalak első
    része ([src=quicknews/build/x.py] -> quicknews) és a lekérdezett gráf projektje."""
    forrasok = terkep_forrasok(valasz)
    g = {x.split("/")[0] for x in forrasok if "/" in x}
    if projekt:
        g.add(projekt)
    return {x for x in g if x and x not in (".", "..")}


def _projektben(p: Path, gyokerek: set[str]) -> bool:
    s = str(p)
    return any(f"/{g}/" in s for g in gyokerek)


HIVAS_BLOKK = re.compile(r'"type"\s*:\s*"tool_use"')
EREDMENY_ID = re.compile(r'"tool_use_id"\s*:\s*"([^"]+)"')
ERDEKES_ESZKOZ = re.compile(r'"name"\s*:\s*"(?:Bash|Read)"')
# naplónként: (méret, módosítás) -> az események, így a változatlan naplót nem olvassa újra
_NAPLO_TAR: dict[str, tuple] = {}


def _naplo_esemenyek(fajl: Path, ablak: int) -> list[tuple]:
    """Egy munkamenet-napló eseményei: (hívásazonosító, kulcs, érték). A naplót soronként
    olvassa, és csak a lényeges sorokat bontja ki: a Bash és Read hívásokat és azok
    eredményét. A képernyőképeket és más nagy eredményeket kibontás nélkül átugorja,
    így egy gigabájtos napló sosem kerül egyben a memóriába."""
    hivasok, eredmenyek, kell = [], {}, set()
    read_idk, adat = set(), {}
    try:
        fh = fajl.open(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    with fh:
        for s in fh:
            n = len(HIVAS_BLOKK.findall(s)) if '"tool_use"' in s else 0
            if n:
                if not ERDEKES_ESZKOZ.search(s):
                    hivasok.extend([None] * n)     # a 12 lépéses ablakba ezek is beleszámítanak
                    continue
                try:
                    d = json.loads(s)
                except ValueError:
                    continue
                for b in (d.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        if b.get("name") == "Bash" or (b.get("name") == "Read" and ("offset" in inp or "limit" in inp)):
                            kell.add(b.get("id"))
                        if b.get("name") == "Read":
                            read_idk.add(b.get("id"))
                        hivasok.append((b, d.get("cwd")))
            elif '"tool_result"' in s:
                idk = EREDMENY_ID.findall(s)
                rid = next((x for x in idk if x in read_idk), None)
                if rid:
                    talalt = READ_ADAT.findall(s)
                    if talalt:                    # az utolsó a napló feljegyzése, nem fájltartalom
                        adat[rid] = tuple(int(x) for x in talalt[-1])
                if not any(x in kell for x in idk):
                    continue
                try:
                    d = json.loads(s)
                except ValueError:
                    continue
                for b in (d.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in kell:
                        eredmenyek[b.get("tool_use_id")] = _tartalom_szoveg(b.get("content"))

    # Fájlok, amelyeket a munkamenet valamikor egészben beolvasott (minden sort visszaadó
    # Read, vagy `cat fájl`). Ezeken a térkép nem spórolt: a kivonat előtt a fájl már
    # megvolt, utána pedig az egész úgyis beolvasásra került.
    egeszben = set()
    for hivas in hivasok:
        if hivas is None:
            continue
        h, cwd = hivas
        inp = h.get("input") or {}
        if h.get("name") == "Read":
            m = adat.get(h.get("id"))
            if m:
                if m[1] <= 1 and m[0] >= m[2]:
                    egeszben.add(str(Path(inp.get("file_path", ""))))
            elif "offset" not in inp and "limit" not in inp:
                try:
                    f = Path(inp.get("file_path", ""))
                    if (f.suffix.lower() in SZOVEG_KITERJ and f.is_file() and f.stat().st_size <= MAX_FAJL
                            and len(f.read_text(encoding="utf-8", errors="ignore").splitlines()) <= OLVASAS_SOR):
                        egeszben.add(str(f))
                except OSError:
                    pass
        elif h.get("name") == "Bash" and re.search(r"\b(?:cat|nl)\b", inp.get("command", "")):
            egeszben.update(str(p) for p in parancssori_teljes_fajlok(inp.get("command", ""), cwd))

    esem, kesz = [], set()
    beszamitva = set()   # fájlok, amelyek teljes méretével ez a munkamenet már összevetett
    utolso_lek, gyokerek, forrasok = None, set(), set()

    def terkep_vezette(f: Path) -> bool:
        # a válasz megnevezte a fájlt; ha a válasz egyetlen fájlt sem nevez meg (két
        # csomópont közti út), a lekérdezett projekt fájljai számítanak
        return _megnevezte(f, forrasok) if forrasok else _projektben(f, gyokerek)

    def alap(f: Path, teljes: int) -> int:
        """A teljes fájl munkamenetenként és fájlonként egyszer számít: ugyanannak a
        fájlnak a második kivonata nem lett volna második teljes olvasás. Olyan fájlnál
        soha, amelyet a munkamenet egészben is beolvasott."""
        kulcs = str(f)
        if kulcs in egeszben or kulcs in beszamitva:
            return 0
        beszamitva.add(kulcs)
        return teljes

    for i, hivas in enumerate(hivasok):
        if hivas is None:
            continue
        h, cwd = hivas
        hid, nev, inp = h.get("id"), h.get("name"), h.get("input") or {}
        if (nev == "Bash" and GRAPHIFY_HIVAS.search(inp.get("command", ""))
                and TERKEP_VALASZ.search(eredmenyek.get(hid, ""))):
            uj = _gyokerek(eredmenyek[hid], _projekt_neve(inp.get("command", "")))
            ujf = terkep_forrasok(eredmenyek[hid])
            ablakban = utolso_lek is not None and i - utolso_lek <= ablak
            gyokerek = gyokerek | uj if ablakban else uj
            forrasok = forrasok | ujf if ablakban else ujf
            utolso_lek = i
            if hid not in kesz:
                kesz.add(hid)
                esem += [(hid, "lekerdezes", 1), (hid, "lek_token", token(eredmenyek[hid]))]
        elif (nev == "Read" and utolso_lek is not None and i - utolso_lek <= ablak
              and ("offset" in inp or "limit" in inp) and hid not in kesz):
            f = Path(inp.get("file_path", ""))
            try:
                jo = hid in eredmenyek and f.is_file() and f.stat().st_size <= MAX_FAJL * 5 and terkep_vezette(f)
            except OSError:
                jo = False
            if jo:
                szoveg = f.read_text(encoding="utf-8", errors="ignore")
                m = adat.get(hid)
                if m:        # a napló saját feljegyzése: a mutatott sorok és a fájl akkori sorai
                    resz, akkori = m[0] < m[2], m[2]
                else:
                    eleje, limit = max(1, int(inp.get("offset") or 1)), inp.get("limit")
                    resz = eleje > 1 or bool(limit and eleje + int(limit) - 1 < len(szoveg.splitlines()))
                    akkori = None
                # ami a teljes fájlt beolvasta, az nem célzott olvasás
                if resz:
                    kesz.add(hid)
                    olv = token(eredmenyek[hid])
                    teljes = alap(f, max(olv, teljes_olvasas_token(szoveg, akkori, szamozott=True)))
                    esem += [(hid, "celzott", 1), (hid, "olvasott_token", olv), (hid, "teljes_token", teljes)]
        elif (nev == "Bash" and utolso_lek is not None and i - utolso_lek <= ablak
              and hid not in kesz and hid in eredmenyek):
            # csak a térkép által megnevezett fájlok: egy jegyzetbe, naplóba vagy más
            # fájlba nézés közvetlenül a lekérdezés után nem a térkép spórolása
            ki = token(eredmenyek[hid])
            fajlok = []
            for f, mod in parancssori_celzott_fajlok(inp.get("command", ""), cwd):
                if not terkep_vezette(f):
                    continue
                szoveg = f.read_text(encoding="utf-8", errors="ignore")
                resz = reszleges(szoveg, mod)
                # ami a teljes fájlt kiírta (head -20 egy 7 soros fájlon), az nem célzott
                # olvasás; ha a parancsból nem dönthető el, a kimenet legyen kisebb a fájlnál
                if resz or (resz is None and ki < token(szoveg)):
                    fajlok.append((f, teljes_olvasas_token(szoveg)))
            if fajlok:
                kesz.add(hid)
                meret = sum(t for _, t in fajlok)
                # a teljes kimenet számít térképpel olvasottnak, a máshonnan jött sorok
                # is, de soha nem több, mint maguk a teljes fájlok
                esem += [(hid, "celzott", len(fajlok)), (hid, "olvasott_token", min(ki, meret)),
                         (hid, "teljes_token", sum(alap(f, t) for f, t in fajlok))]
    return esem


def elo_meres(naplo_gyoker: Path = Path.home() / ".claude" / "projects", ablak: int = 12) -> dict:
    """A munkamenet-naplókból: a választ adó térkép-lekérdezések, és az utánuk
    következő célzott fájlolvasások (Read offset/limit-tel, vagy a parancssorban
    sed -n, head, tail, grep környező sorokkal) tényleges és teljes mérete. Csak a
    térkép által megnevezett fájlok számítanak, a teljes méret munkamenetenként egyszer. A munkamenetek másolatai miatt minden
    eszközhívás azonosító szerint csak egyszer számít. Az előző futás óta nem változott
    naplókat a memóriából veszi, nem olvassa be újra."""
    latott = set()
    o = {"lekerdezes": 0, "lek_token": 0, "celzott": 0, "olvasott_token": 0, "teljes_token": 0}
    van = set()
    for fajl in naplo_gyoker.glob("*/*.jsonl"):
        try:
            st = fajl.stat()
        except OSError:
            continue
        kulcs, belyeg = str(fajl), (st.st_size, st.st_mtime_ns, ablak)
        van.add(kulcs)
        tar = _NAPLO_TAR.get(kulcs)
        if tar is None or tar[0] != belyeg:
            tar = (belyeg, _naplo_esemenyek(fajl, ablak))
            _NAPLO_TAR[kulcs] = tar
        itt = set()
        for hid, k, v in tar[1]:
            if hid in latott:
                continue
            itt.add(hid)
            o[k] += v
        latott |= itt
    for eltunt in set(_NAPLO_TAR) - van:
        del _NAPLO_TAR[eltunt]
    lek_token, olvasott_token, teljes_token = o["lek_token"], o["olvasott_token"], o["teljes_token"]
    vele = lek_token + olvasott_token
    return {"lekerdezes": o["lekerdezes"], "lekerdezes_token": lek_token, "celzott_olvasas": o["celzott"],
            "celzott_token": olvasott_token, "teljes_fajl_token": teljes_token,
            "terkeppel_token": vele, "sporolt_token": teljes_token - vele,
            "sporolt_szazalek": round(100 * (teljes_token - vele) / teljes_token, 1) if teljes_token else None}
