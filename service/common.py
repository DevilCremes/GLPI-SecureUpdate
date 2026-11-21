"""Estado transacional e catálogo de releases oficiais."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

BRANCH = "12.0"
API = "https://api.github.com/repos/glpi-project/glpi"
GITHUB = "https://github.com/glpi-project/glpi"
STATE = Path("/var/lib/glpi-secureupdate")
CONF = Path("/etc/glpi-secureupdate/config.json")
SOCKET = "/run/glpi-secureupdate/control.sock"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        if default is None:
            raise
        return default


def write(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        syncdir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def syncdir(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def patch(version):
    require(isinstance(version, str) and re.fullmatch(r"12\.0\.(0|[1-9][0-9]{0,4})", version), "Versão fora do ramo 12.0.")
    return int(version.rsplit(".", 1)[1])


def api(path):
    req = Request(API + path, headers={"User-Agent":"GLPI-SecureUpdate/1.0","Accept":"application/vnd.github+json"})
    with urlopen(req, timeout=15) as resp:
        raw = resp.read(8_000_001)
    require(len(raw) <= 8_000_000, "Resposta muito grande do GitHub.")
    return json.loads(raw)


def official(item):
    v = item["tag_name"]
    patch(v)
    require(not item["draft"] and not item["prerelease"], "A versão não é estável.")
    name = f"glpi-{v}.tgz"
    asset, = [a for a in item["assets"] if a["name"] == name]
    link = f"{GITHUB}/releases/download/{v}/{name}"
    require(asset["browser_download_url"] == link and re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""), "Pacote oficial sem SHA-256 verificável.")
    return {"version":v, "url":link, "sha":asset["digest"][7:]}


def catalog(current, fetch=api):
    start = patch(current)
    notes = {}
    for page in range(1, 21):
        rows = fetch(f"/releases?per_page=40&page={page}")
        require(isinstance(rows, list), "Catálogo inválido.")
        for item in rows:
            tag = item["tag_name"]
            if not re.fullmatch(r"12\.0\.(0|[1-9][0-9]{0,4})", tag) or item["draft"] or item["prerelease"]:
                continue
            if patch(tag) > start:
                notes[tag] = item
        if len(rows) < 40 or any(i["tag_name"] == current for i in rows):
            break
    else:
        raise ValueError("Catálogo incompleto. Consulte as releases oficiais.")
    if not notes:
        return {"source":current, "version":current,"notes":[],"checked":int(time.time()),"id":""}
    ordered = sorted(notes, key=patch, reverse=True)
    plan = official(notes[ordered[0]])
    labels = {"critical":"Crítica","high":"Alta","medium":"Média","moderate":"Média","low":"Baixa"}
    ranks = {"Crítica":4,"Alta":3,"Média":2,"Baixa":1}
    severity = []
    entries = []
    for tag in ordered:
        body = str(notes[tag].get("body") or "")
        for level in re.findall(r"\[SECURITY\s*[-:]\s*(critical|high|medium|moderate|low)\b", body, re.I):
            severity.append(labels[level.lower()])
        entries.append({"version":tag,"body":body[:12000],"url":f"{GITHUB}/releases/tag/{tag}"})
    plan.update(source=current, checked=int(time.time()), urgency=max(severity,key=ranks.get,default="Severidade não informada"), notes=entries)
    plan["id"] = hashlib.sha256((current+"|"+plan["version"]+"|"+plan["sha"]).encode()).hexdigest()
    return plan


def run(cmd, **kw):
    return subprocess.run([str(i) for i in cmd], check=True, **kw)
