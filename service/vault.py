"""Catálogo local confiável e destinos de backup provisionados no Linux."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

import backup_destinations as profiles
from common import CONF, STATE, read, require, run, syncdir, write
import schedule

FILES = ("database.sql", "files.tar", "SHA256SUMS", "resumo.json")
KEY = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}")
DEFAULT = {"destination":"local","rule":None,"keep":7,"days":30,"auto":False,"next":0,"actor":0}
LOCAL = {"label":"Neste servidor","type":"local"}
POLICY = "backup-policy.json"


def presets():
    from engine import trusted
    path = CONF.parent / "destinations.json"
    result = {"local":LOCAL}
    if path.exists():
        trusted(path,False)
        for alias, spec in read(path).items():
            require(alias!="local" and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}",alias),"Alias de destino inválido.")
            kind, target = spec["type"], spec["target"]
            require(kind in ("local","nas","cloud") and isinstance(spec["label"],str),"Tipo de destino inválido.")
            if kind=="local":
                require(isinstance(target,str) and Path(target).is_absolute() and ".." not in Path(target).parts,"Diretório absoluto necessário.")
                require(len(Path(target).parts)>=3,"Use diretório exclusivo para backups.")
            else:
                require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*:[A-Za-z0-9_./-]+",target)
                        and not any(p in ("",".","..") for p in target.split(":",1)[1].split("/")),
                        "Configure um prefixo exclusivo do rclone.")
            result[alias]=spec
    return result


def destinations():
    result = presets()
    directory = STATE / profiles.DIRECTORY
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if profiles.PROFILE.fullmatch(entry.name):
                require(entry.name not in result, "Conflito de alias de armazenamento.")
                result[entry.name] = read(entry / "spec.json")
    return result


def register_destination(host, job, options):
    profiles.register(STATE, host, "dest-" + job, options, presets(), destinations(), check_destination)



def catalog(state):
    root=state/"backups"/"manuais"
    if not root.is_dir():
        return []
    rows=[]
    for p in sorted(root.iterdir(),reverse=True):
        if KEY.fullmatch(p.name) and not p.is_symlink() and (p/"resumo.json").is_file():
            row=read(p/"resumo.json")
            rows.append({**row,"id":p.name,"path":str(p),"destination":row.get("destination","local")})
    return rows


def directory(state, key):
    require(isinstance(key,str) and KEY.fullmatch(key),"Backup inválido.")
    d=state/"backups"/"manuais"/key
    require(d.is_dir() and not d.is_symlink() and (d/"resumo.json").is_file(),"Backup ausente.")
    return d


def configure(state, conf, actor):
    require(isinstance(conf,dict) and set(conf)=={"destination","rule","keep","days","auto"},"Política inválida.")
    require(conf["destination"] in destinations(),"Destino não cadastrado.")
    require(type(conf["keep"]) is int and 1<=conf["keep"]<=1000 and
            type(conf["days"]) is int and 0<=conf["days"]<=3650 and type(conf["auto"]) is bool,"Retenção inválida.")
    if conf["rule"] is not None:
        schedule.validate(conf["rule"])
    old=read(state/POLICY,DEFAULT)
    due=0
    if conf["rule"]:
        due=old["next"] if conf["rule"]==old["rule"] and old["next"]>time.time() else schedule.next_check({"schedule":conf["rule"]},time.time())
    write(state/POLICY,{**conf,"next":due,"actor":actor})


def candidates(rows, policy, now):
    eligible=sorted((r for r in rows if r["destination"]==policy["destination"] and not r.get("pending") and not r.get("deleting")),
                    key=lambda r:(r["time"],r["id"]),reverse=True)
    return [r["id"] for r in eligible[policy["keep"]:] if r["time"]<=now-policy["days"]*86400]


def remote(spec, key, name):
    return spec["target"].rstrip("/")+"/"+key+"/"+name


def transport(spec, *args, profile_path=None, **kw):
    from engine import trusted
    cfg="/dev/null"
    if spec["type"]=="local":
        path=Path(spec["target"])
        trusted(path)
        require(not path.stat().st_mode & 0o077,"O diretório de destino deve ser privado de root.")
    else:
        cfg=CONF.parent/"rclone.conf"
        if "profile" in spec:
            require(profiles.PROFILE.fullmatch(spec["profile"]), "Perfil de armazenamento inválido.")
            cfg = profile_path or STATE / profiles.DIRECTORY / spec["profile"] / "rclone.conf"
        trusted(cfg,False)
        require(not cfg.stat().st_mode & 0o077,"Proteja rclone.conf com modo 0600.")
    return run(["rclone","--config",str(cfg),"--retries","2","--contimeout","15s","--timeout","2m",*args],**kw)


def check_destination(spec,state, *, profile_path=None):
    if spec==LOCAL:
        with tempfile.TemporaryFile(dir=state) as probe:
            data=os.urandom(32)
            probe.write(data)
            probe.flush()
            os.fsync(probe.fileno())
            probe.seek(0)
            require(probe.read()==data,"Falha de leitura do armazenamento local.")
        return
    with tempfile.TemporaryDirectory() as tmp:
        probe=Path(tmp)/"probe"
        probe.write_bytes(os.urandom(32))
        target=remote(spec,".secureupdate-test",os.urandom(12).hex())
        transport(spec,"copyto",probe,target,profile_path=profile_path,stderr=subprocess.DEVNULL)
        try:
            require(transport(spec,"cat",target,stdout=subprocess.PIPE,profile_path=profile_path,stderr=subprocess.DEVNULL).stdout==probe.read_bytes(),"Leitura de teste divergente.")
        finally:
            transport(spec,"deletefile",target,profile_path=profile_path,stderr=subprocess.DEVNULL)


def verify(d, sums):
    expected={}
    for line in sums.splitlines():
        m=re.fullmatch(r"([0-9a-f]{64})  (database\.sql|files\.tar)",line)
        require(m and m[2] not in expected,"Manifesto de backup inválido.")
        expected[m[2]]=m[1]
    require(set(expected)==set(FILES[:2]),"Manifesto incompleto.")
    for name,digest in expected.items():
        h=hashlib.sha256()
        p=d/name
        require(p.is_file() and not p.is_symlink(),"Arquivo de backup ausente.")
        with p.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""):
                h.update(chunk)
        require(h.hexdigest()==digest,"Integridade do backup divergente.")


def recorded_spec(row):
    spec=destinations().get(row["destination"])
    require(spec and spec==row["storage"],"Restaure a configuração original do destino.")
    return spec


def publish(d, alias):
    if alias=="local":
        return
    spec=destinations()[alias]
    row=read(d/"resumo.json")
    require(not row.get("deleting"),"Exclusão pendente.")
    if "storage" in row:
        require(alias==row["destination"] and spec==row["storage"],"Destino alterado.")
    row.update(destination=alias,storage=spec,pending=True)
    write(d/"resumo.json",row)
    verify(d,(d/"SHA256SUMS").read_text())
    for name in FILES:
        transport(spec,"copyto",d/name,remote(spec,d.name,name))
    transport(spec,"check","--download","--one-way",d,remote(spec,d.name,""))
    write(d/"resumo.json",{**row,"pending":False})
    for name in FILES[:2]:
        (d/name).unlink(missing_ok=True)
    syncdir(d)


@contextmanager
def materialize(d):
    row=read(d/"resumo.json")
    require(not row.get("deleting"),"Esta cópia tem uma exclusão pendente.")
    sums=(d/"SHA256SUMS").read_text()
    if row.get("destination","local")=="local" or row.get("pending"):
        verify(d,sums)
        yield d
        return
    spec=recorded_spec(row)
    require(shutil.disk_usage(d).free>=row["size"]+64*1024**2,"Espaço de preparação insuficiente.")
    with tempfile.TemporaryDirectory(dir=d.parent,prefix=".leitura-") as tmp:
        work=Path(tmp)
        for name in FILES[:2]:
            transport(spec,"copyto",remote(spec,d.name,name),work/name)
        for name in FILES[2:]:
            shutil.copyfile(d/name,work/name)
        verify(work,sums)
        yield work


def remove(state,key):
    d=directory(state,key)
    row=read(d/"resumo.json")
    if row.get("storage"):
        spec=recorded_spec(row)
        parent=spec["target"].rstrip("/")
        names=transport(spec,"lsf","--dirs-only","--max-depth","1",parent,stdout=subprocess.PIPE,text=True).stdout.splitlines()
        require(d.name+"/" in names or row.get("pending") or row.get("deleting"),
                "A cópia não foi localizada no destino. Confira o perfil antes de excluir o catálogo.")
        if d.name+"/" in names:
            target=parent+"/"+d.name
            contents=transport(spec,"lsf",target,stdout=subprocess.PIPE,text=True).stdout.splitlines()
            require(set(contents)<=set(FILES),"O diretório contém arquivos desconhecidos. Revise o destino.")
            write(d/"resumo.json",{**row,"deleting":True})
            for name in contents:
                transport(spec,"deletefile",remote(spec,key,name))
            # Alguns serviços de objetos eliminam automaticamente um prefixo vazio.
            names=transport(spec,"lsf","--dirs-only",parent,stdout=subprocess.PIPE,text=True).stdout.splitlines()
            if d.name+"/" in names:
                transport(spec,"rmdir",target)
    tomb=d.with_name(".removido-"+d.name)
    d.rename(tomb)
    syncdir(tomb.parent)
    shutil.rmtree(tomb)


def preview(rows,policy):
    keys=candidates(rows,policy,time.time())
    digest=hashlib.sha256(json.dumps(keys,separators=(",",":")).encode()).hexdigest()
    return keys,digest


def location(spec, state):
    if spec == LOCAL:
        return str(state / "backups" / "manuais")
    if "server" in spec:
        return spec["server"] + " / " + spec["target"].partition(":")[2]
    return spec["target"]


def public_state(state,offset=0):
    rows=catalog(state)
    policy=read(state/POLICY,DEFAULT)
    keys,digest=preview(rows,policy)
    return {"items":[{k:v for k,v in r.items() if k in ("id","time","version","size","path","destination","pending","deleting")}
                    for r in rows[offset:offset+25]],"total":len(rows),"policy":policy,"candidates":keys[offset:offset+25],
            "bases":[{"id":k,"path":str(v)} for k,v in profiles.local_bases(state,presets()).items()],
            "count":len(keys),"digest":digest,"destinations":[{"id":key,"label":spec["label"],"type":spec["type"],
                                                    "location":location(spec,state),
                                                    "type_label":{"local":"Local","nas":"NAS","cloud":"Nuvem"}[spec["type"]]}
                                                      for key,spec in destinations().items()]}


def prune(state,policy):
    for key in candidates(catalog(state),policy,time.time()):
        remove(state,key)


def stream_export(d, sock):
    with materialize(d) as files:
        # Precompute exact Content-Length so clients detect an interrupted transfer.
        need=sum((files/name).stat().st_size for name in FILES)+64*1024**2
        require(shutil.disk_usage(d.parent).free>=need,"Espaço insuficiente para preparar o download.")
        with tempfile.TemporaryFile(dir=d.parent) as bundle:
            with tarfile.open(fileobj=bundle,mode="w") as archive:
                for name in FILES:
                    archive.add(files/name,arcname=name,recursive=False)
            size=bundle.tell()
            bundle.seek(0)
            sock.sendall(('{"ok":true,"size":%d}\n'%size).encode())
            for block in iter(lambda:bundle.read(1024*1024),b""):
                sock.sendall(block)
