"""Troca de versão com duas bases. O banco original permanece intacto na migração."""
import activity
import contextlib
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import stat
import subprocess as sp
import tarfile
import tempfile
import time

from common import STATE, api, official, validate_update, version_parts, read, require, run, syncdir, write
import health
from db import INSPECIONAR, cliente_db

JOURNAL = STATE / "transaction.json"
LAST = STATE / "last.json"


def trusted(path, directory=True):
    require(path.is_absolute() and path.resolve() == path, f"Caminho físico necessário: {path}")
    for p in [path, *path.parents]:
        st = p.stat()
        require(st.st_uid == 0 and not st.st_mode & 0o022, f"Proteja a propriedade/permissão de {p} para root.")
    require(path.is_dir() if directory else path.is_file(), f"Caminho inválido: {path}")


def within(p, base):
    return p == base or base in p.parents


MAX_ARCHIVE_MEMBERS = 500_000
MAX_UNPACKED_BYTES = 4 * 1024**3


def extract(archive, dest):
    require(hasattr(tarfile, "data_filter"), "O Python precisa do filtro de segurança de tarfile.")
    seen=set()
    total=0
    limit=min(MAX_UNPACKED_BYTES,shutil.disk_usage(dest).free - 512*1024**2)
    with tarfile.open(archive, "r:gz") as f:
        for m in f:
            p = Path(m.name)
            require(p.parts and p.parts[0] == "glpi" and ".." not in p.parts and not p.is_absolute(), "Caminho inválido no .tgz.")
            total+=m.size
            require(len(seen)<MAX_ARCHIVE_MEMBERS and 0<=m.size and total<=limit,"Limite de extração excedido.")
            require(p not in seen,"Nome repetido no pacote.")
            seen.add(p)
            require(m.isdir() or m.isfile() or m.issym() or m.islnk(), "Tipo de entrada inválido.")
            safe = tarfile.data_filter(m, str(dest))
            if safe.islnk() or safe.issym():
                target = (dest / p.parent if safe.issym() else dest) / safe.linkname
                require(within(target.resolve(), dest / "glpi"), "Link fora de glpi/.")
            f.extract(safe, dest, filter="data")
    new = dest / "glpi"
    for rel in ["bin/console","vendor/autoload.php","public/index.php"]:
        require((new / rel).is_file(), f"Pacote incompleto: {rel}")
    # O filtro data usa o umask do processo para diretórios.
    for root, _, _ in os.walk(new):
        os.chmod(root, 0o755)
    return new


def switch_db(path, old, new):
    require(not path.is_symlink() and path.is_file(), "config_db.php precisa ser um arquivo regular.")
    require(re.fullmatch(r"[A-Za-z0-9_$-]+", old) and re.fullmatch(r"gsu_[0-9a-f]{16}", new), "Nome do banco incompatível.")
    raw = path.read_bytes()
    pattern = rb"""(?m)^(\s*(?:public|var)\s+(?:string\s+)?\$dbdefault\s*=\s*)(['"])""" + re.escape(old.encode()) + rb"""\2(\s*;)"""
    out, count = re.subn(pattern, lambda m:m[1]+m[2]+new.encode()+m[2]+m[3], raw)
    require(count == 1, "O config_db.php precisa usar a propriedade literal dbdefault gerada pelo GLPI.")
    st = path.stat()
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(out)
        if os.geteuid() == 0:
            os.fchown(f.fileno(),st.st_uid,st.st_gid)
        os.fchmod(f.fileno(),stat.S_IMODE(st.st_mode))
        f.flush()
        os.fsync(f.fileno())
    shutil.copystat(path,f.name,follow_symlinks=False)
    os.replace(f.name,path)
    syncdir(path.parent)


class Host:
    def __init__(self, conf):
        self.c = conf
        self.app = Path(conf["app"])
        trusted(self.app.parent)
        self.account = pwd.getpwnam(conf["web_user"])
        require(self.account.pw_uid != 0, "PHP não pode usar root.")
        self.php = str(Path(conf.get("php","/usr/bin/php")).resolve(strict=True))
        trusted(Path(self.php),False)
        self.web = dict(user=self.account.pw_uid,group=self.account.pw_gid,
                        extra_groups=os.getgrouplist(self.account.pw_name,self.account.pw_gid),
                        cwd=self.app,umask=0o022,
                        env={"PATH":"/usr/bin:/bin","HOME":self.account.pw_dir,"LANG":"C.UTF-8"})
        self.units = conf["units"]
        require(self.units and len(set(self.units)) == len(self.units) and all(re.fullmatch(r"[A-Za-z0-9_@][\w.@:-]*\.(service|timer|socket)",u) for u in self.units), "Configure os serviços de ingresso e de escrita.")
        self.ext = [Path(s) for s in conf.get("external",[])]
        for p in self.ext:
            trusted(p.parent)
            require(p.is_dir() and p.resolve()==p and len(p.parts)>=3 and not any(within(x,p) or within(p,x) for x in [self.app,STATE]), f"Diretório externo inválido: {p}")
        require(len(set(self.ext))==len(self.ext),"Diretório externo duplicado.")
        require(not any(within(p,q) for p in self.ext for q in self.ext if p!=q),"Diretórios externos sobrepostos.")

    def inspect(self):
        return json.loads(run([self.php,"-r",INSPECIONAR],stdout=sp.PIPE,**self.web).stdout)

    def console(self, command, *args):
        run([self.php,"bin/console","--no-interaction",command,*args],**self.web)

    def service(self, cmd, u):
        if cmd == "active":
            response = run(["systemctl","show","-p","LoadState","-p","ActiveState",u],
                           stdout=sp.PIPE,text=True).stdout
            state = dict(line.split("=",1) for line in response.splitlines() if "=" in line)
            require(state.get("LoadState")=="loaded", "Unidade não carregada: "+u)
            require(state.get("ActiveState") in ("active","inactive","failed"), "Estado transitório ou desconhecido: "+u)
            return state["ActiveState"]=="active"
        run(["systemctl",cmd,u])

    def layout(self, info):
        trusted(self.app)
        require(not self.app.is_mount() and not (self.app/".git").exists(),"Use uma instalação física pelo pacote oficial.")
        inside = [self.app / p for p in ("config","files","plugins","marketplace")]
        for s in [*info["dirs"],info["cfg"]]:
            p=Path(s).resolve()
            require(any(within(p,a) for a in [*inside,*self.ext]), f"Declare a raiz real dos dados externos: {s}")
        for rel in ["inc/downstream.php",".env",".env.local"]:
            if (self.app/rel).exists(): inside.append(self.app/rel)
        roots=[p for p in inside if p.exists() or p.is_symlink()]
        for root in [*roots,*self.ext]:
            if root.is_symlink() and root in (self.app/"config", self.app/"files"):
                require(root.resolve(strict=True) in self.ext, "Declare o destino físico do link config/files.")
                continue  # Validate its contents when walking the declared external root.
            require(not root.is_symlink(),"Use caminhos reais e downstream.php para dados externos.")
            if root.is_file():
                continue
            require(root.is_dir(),f"Tipo de arquivo inesperado: {root}")
            for folder,dirs,files in os.walk(root,followlinks=False,onerror=raise_error):
                for name in dirs+files:
                    p=Path(folder,name)
                    if p.is_symlink():
                        require(any(within(p.resolve(),a) for a in [*roots,*self.ext]),f"Link aponta para dados fora do backup: {p}")
        return roots

    @contextlib.contextmanager
    def client(self, info):
        maria = bool(shutil.which("mariadb") and shutil.which("mariadb-dump"))
        cli,dump=("mariadb","mariadb-dump") if maria else ("mysql","mysqldump")
        with cliente_db(info,self.account,maria) as fd:
            opt=f"--defaults-file=/proc/self/fd/{fd}"
            def sql(stmt, db=None):
                return run([cli,opt,"--batch","--skip-column-names","-e",stmt,db or info["db"]],stdout=sp.PIPE,text=True,pass_fds=(fd,),**self.web).stdout.strip()
            def save(dest):
                options=["--single-transaction","--quick","--skip-lock-tables","--hex-blob","--triggers","--routines","--events"]
                if not maria: options+=["--no-tablespaces","--set-gtid-purged=OFF"]
                with dest.open("xb") as f:
                    run([dump,opt,*options,info["db"]],stdout=f,pass_fds=(fd,),**self.web)
                    f.flush()
                    os.fsync(f.fileno())
                require(dest.stat().st_size>0,"Dump SQL vazio.")
            def clone(dest,name):
                require(re.fullmatch(r"gsu_[0-9a-f]{16}",name),"Nome de banco inválido.")
                charset,collation=sql("SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=DATABASE()").split()
                require(all(re.fullmatch(r"[A-Za-z0-9_]+",s) for s in (charset,collation)),"Charset inválido.")
                sql(f"CREATE DATABASE `{name}` CHARACTER SET {charset} COLLATE {collation}")
                with dest.open("rb") as f:
                    run([cli,opt,name],stdin=f,pass_fds=(fd,),**self.web)
                require(sql("SELECT value FROM glpi_configs WHERE context='core' AND name='version'",name)==info["version"],"A cópia restaurada não passou no teste de versão.")
            yield sql,save,clone


def raise_error(error):
    raise error


def sync_tree(src,dst):
    dst.mkdir(mode=0o700,parents=True,exist_ok=True)
    run(["rsync","-aHAX","--numeric-ids","--delete","--",str(src)+"/",str(dst)+"/"])


def stage(j, phase):
    write(JOURNAL,{**j,"phase":phase})
    j["phase"]=phase
    activity.record(JOURNAL.parent, j.get("activity_job") or j.get("job"), phase)
    print(phase,flush=True)


def clear_journal():
    JOURNAL.unlink()
    syncdir(JOURNAL.parent)


def durable_move(src,dst):
    src.rename(dst)
    syncdir(src.parent)
    if dst.parent != src.parent:
        syncdir(dst.parent)


def disk_check(host, info, clone_tree):
    """Estimativa conservadora de arquivos e dump. O servidor SQL pode ser remoto."""
    def size(path):
        return int(run(["du","-sb","--",path],stdout=sp.PIPE,text=True).stdout.split(maxsplit=1)[0])
    with host.client(info) as (sql,_,__):
        dbbytes=int(sql("SELECT COALESCE(SUM(data_length+index_length),0) FROM information_schema.tables WHERE table_schema=DATABASE()"))
    appsize=size(host.app)
    external=sum(size(p) for p in host.ext)
    need={STATE:appsize+2*external+3*dbbytes}
    if clone_tree: need[host.app.parent]=need.get(host.app.parent,0)+appsize
    grouped={}
    for path,n in need.items():
        dev=path.stat().st_dev
        grouped[dev]=(path,grouped.get(dev,(path,0))[1]+n)
    for path,n in grouped.values():
        require(shutil.disk_usage(path).free >= n+512*1024**2, f"Espaço insuficiente em {path} para dados, cópias e reserva de 512 MiB.")


def backup(host,j,info,directory):
    directory.mkdir(mode=0o700,exist_ok=True)
    with host.client(info) as (_,save,_):
        save(directory/"database.sql")
    roots=[str(p.relative_to("/")) for p in [host.app,*host.ext]]
    run(["tar","--hard-dereference","--acls","--xattrs","--selinux","--numeric-owner","-cf",directory/"files.tar","-C","/","--",*roots])
    run(["tar","-tf",directory/"files.tar"],stdout=sp.DEVNULL)
    with (directory/"SHA256SUMS").open("w") as f:
        run(["sha256sum","database.sql","files.tar"],cwd=directory,stdout=f)
    run(["sha256sum","--check","--status","SHA256SUMS"],cwd=directory)
    os.sync()


def safety_copy(host,j,info,directory,job,operation):
    def record(level,message):
        try:
            write(STATE/"checkpoint-notice.json",{"job":job,"op":operation,"level":level,"message":message})
        except OSError:
            print("Falha ao registrar aviso do ponto de segurança.",flush=True)
    record("warning","A cópia de segurança foi iniciada. A conclusão ainda não foi confirmada.")
    try:
        backup(host,j,info,directory)
    except Exception:
        record("error","Falha ao criar ou verificar o ponto de segurança. Confira o trabalho no servidor.")
        raise
    record("success","O banco e os arquivos do ponto de segurança foram copiados e verificados.")


def freeze(host,j):
    host.console("maintenance:enable")
    with host.client(host.inspect()) as (sql,_,__):
        require(sql("SELECT value FROM glpi_configs WHERE context='core' AND name='maintenance_mode'")=="1","O GLPI não confirmou o modo de manutenção.")
    for unit in host.units:
        host.service("stop",unit)
    require(not any(host.service("active",u) for u in host.units),"Ainda há um serviço de escrita ativo.")


def release(host,j,phase):
    # Persistir antes de permitir a primeira gravação. Falha a partir daqui requer retomada.
    stage(j,phase)
    if not j["maintenance"]:
        host.console("maintenance:disable")
    for unit in reversed(j["active"]):
        host.service("start",unit)


def finish_revert(j):
    result={"result":"revertida","job":j.get("job"),"source":j["source"],
            "version":j["target"],"backup":j["backup"],"time":time.time()}
    write(STATE/"outcome.json",result)
    # Uma tentativa rejeitada não invalida o último ponto que foi aplicado com sucesso.
    previous=read(LAST,{})
    if previous.get("result")!="aplicada" or previous.get("clone")==j["clone"]:
        write(LAST,result)
    clear_journal()


@contextlib.contextmanager
def rollback_files(host,j):
    if not (host.ext and j.get("snapshot") and j.get("changed")):
        yield []
        return
    backup_dir=Path(j["backup"])
    # Verifique o arquivo que efetivamente será utilizado, antes de trocar a árvore.
    run(["sha256sum","--check","--strict","--status","SHA256SUMS"],cwd=backup_dir)
    with tempfile.TemporaryDirectory(prefix="restore-",dir=backup_dir) as staging:
        base=Path(staging)
        names=[str(p.relative_to("/")) for p in host.ext]
        run(["tar","--acls","--xattrs","--selinux","--numeric-owner","-xf",backup_dir/"files.tar",
             "-C",base,"--",*names])
        paths=[base/name for name in names]
        require(all(p.is_dir() and p.resolve()==p for p in paths),"Snapshot externo incompleto.")
        yield list(zip(paths,host.ext))


def revert(host,j):
    area=Path(j["area"])
    old=area/"original"
    with rollback_files(host,j) as data:
        if old.is_dir():
            if host.app.exists():
                durable_move(host.app,area/("retirada-"+secrets.token_hex(5)))
            durable_move(old,host.app)
        for src,dst in data:
            sync_tree(src,dst)
    info=host.inspect()
    require(info["db"]==j["db"] and info["version"]==j["source"],"Árvore original ou banco inesperado.")
    host.console("db:check")
    if j.get("changed"):
        current=health.inspect(host)
        before=read(Path(j["backup"])/"health-before.json")
        bad,lost=health.compare(before,current)
        write(Path(j["backup"])/"health-rollback.json",current)
        require(not bad and not lost,"O ambiente restaurado falhou no healthcheck. A manutenção continua ativa.")
        summary=read(STATE/"health.json",{})
        summary["Rollback"]=f"Versão {j['source']} verificada · diagnóstico #{current['id']}"
        write(STATE/"health.json",summary)
    release(host,j,"reabrindo_original")
    finish_revert(j)


def reopen_new(host,j):
    release(host,j,"reabrindo_nova")
    write(LAST,{**j,"result":"aplicada","time":j["time"]})
    summary=read(STATE/"health.json",{})
    try:
        summary["Acesso público"]=health.http_probe(host.c["status_url"]) if not j["maintenance"] else "Manutenção preexistente preservada"
    except Exception as e:
        summary["Acesso público"]="Falha após reabertura. Requer revisão e aprovação para rollback: "+str(e)[:180]
    write(STATE/"health.json",summary)
    clear_journal()


def config_file(host, info):
    directory = Path(info["cfg"])
    if directory == host.app/"config" and directory.is_symlink():
        directory = directory.resolve(strict=True)
        require(directory in host.ext, "Configuração externa não declarada.")
    return directory/"config_db.php"


def restore_plugin_execution(host, info, previously_active):
    # GLPI 10 deactivates plugins on db:update. Newer cores may suspend execution
    # on a major migration. Restore the source selection on the migrated clone.
    with host.client(info) as (sql, _, __):
        if version_parts(info["version"])[0] >= 11:
            mode = sql("SELECT value FROM glpi_configs WHERE context='core' AND name='plugins_execution_mode'")
            if mode == "suspended_by_update":
                host.console("plugin:resume_execution")
            else:
                require(mode in ("", "on"), "Execução dos plugins suspensa pelo administrador.")
        active = set(sql("SELECT directory FROM glpi_plugins WHERE state=1").splitlines())
        for name in sorted(set(previously_active) - active):
            require(re.fullmatch(r"[A-Za-z0-9_]+", name), "Identificador de plugin inválido.")
            host.console("plugin:activate", name)


def create_job(host,plan,archive):
    require(not JOURNAL.exists(),"Há uma operação pendente. Consulte o serviço no servidor.")
    info=host.inspect()
    validate_update(plan)
    require(info["version"]==plan["source"],"Aprovação não corresponde à instalação.")
    asset=official(api("/releases/tags/"+plan["version"]))
    require(asset["sha"]==plan["sha"] and asset["url"]==plan["url"],"A release mudou; consulte e aprove novamente.")
    roots=host.layout(info)
    source_major, _ = version_parts(info["version"])
    migrating = plan.get("scope") == "next"
    baseline = health.inspect(host, plan["version"]) if migrating else health.inspect(host)
    health.http_probe(host.c["status_url"])
    # Evita colisões com volumes ativos na troca por renomeação.
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        point=Path(re.sub(r"\\([0-7]{3})",lambda m:chr(int(m[1],8)),line.split()[4]))
        require(not within(point,host.app),"Há um volume montado dentro da aplicação.")
    cfg=config_file(host, info)
    trusted(cfg.parent)
    require(cfg.is_file() and cfg.resolve()==cfg,"Configuração externa ou link inválido.")
    require(not (cfg.parent/"config_db_slave.php").exists(),"Réplica configurada. É necessário um procedimento para cluster.")
    with host.client(info) as (sql,_,__):
        require(sql("SELECT value FROM glpi_configs WHERE context='core' AND name='version'")==info["version"],"Versões de banco e aplicação divergentes.")
        maintenance=sql("SELECT value FROM glpi_configs WHERE context='core' AND name='maintenance_mode'")=="1"
        for table,column in [("TRIGGERS","TRIGGER_SCHEMA"),("EVENTS","EVENT_SCHEMA"),("ROUTINES","ROUTINE_SCHEMA"),("VIEWS","TABLE_SCHEMA")]:
            require(sql(f"SELECT COUNT(*) FROM information_schema.{table} WHERE {column}=DATABASE()")=="0","Objetos SQL personalizados exigem revisão antes de clonar o banco.")
    host.console("system:check_requirements")
    host.console("db:check")
    disk_check(host,info,True)
    active=[u for u in host.units if host.service("active",u)]
    job=secrets.token_hex(8)
    area=Path(tempfile.mkdtemp(prefix=".secureupdate-",dir=host.app.parent))
    bkp=STATE/"backups"/job
    bkp.mkdir(mode=0o700,parents=True)
    j=dict(source=info["version"],target=plan["version"],db=info["db"],clone="gsu_"+job,area=str(area),backup=str(bkp),active=active,
           maintenance=maintenance,app=str(host.app),external=list(map(str,host.ext)),units=host.units,approved=plan["actor"],job=plan["job"],time=time.time())
    try:
        with archive.open("rb") as f:
            h=hashlib.sha256()
            for chunk in iter(lambda:f.read(1<<20),b""): h.update(chunk)
        require(h.hexdigest()==asset["sha"],"SHA-256 do pacote não confere.")
        new=extract(archive,area)
        shutil.copystat(host.app,new)
        if os.geteuid()==0: os.chown(new,host.app.stat().st_uid,host.app.stat().st_gid)
    except BaseException:
        shutil.rmtree(area)
        raise
    write(bkp/"health-before.json",baseline)
    write(STATE/"health.json",{"Atualização":plan["version"],"Referência":f"Diagnóstico #{baseline['id']} · {baseline['score']}/100"})
    stage(j,"manutencao")
    try:
        freeze(host,j)
        j["time"]=time.time()
        stage(j,"backup")
        safety_copy(host,j,info,bkp,plan["job"],"before_update")
        with host.client(info) as (_,__,clone):
            clone(bkp/"database.sql",j["clone"])
        j["snapshot"]=True
        stage(j,"preparando")
        for root in roots:
            dest=new/root.relative_to(host.app)
            if dest.is_symlink(): dest.unlink()
            elif dest.is_dir(): shutil.rmtree(dest)
            elif dest.exists(): dest.unlink()
            run(["cp","-a","--reflink=auto","--",root,dest])
        j["changed"]=True
        stage(j,"trocando")
        target_cfg=new/cfg.relative_to(host.app) if within(cfg,host.app) else cfg
        switch_db(target_cfg,info["db"],j["clone"])
        durable_move(host.app,area/"original")
        durable_move(new,host.app)
        current=host.inspect()
        require(current["db"]==j["clone"] and current["version"]==plan["version"],"Destino incorreto antes da migração.")
        stage(j,"migrando")
        host.console("system:check_requirements")
        host.console("db:update")
        host.console("db:check")
        with host.client(current) as (sql,_,__):
            require(sql("SELECT value FROM glpi_configs WHERE context='core' AND name='version'")==j["target"],"Versão final divergente.")
        if migrating or source_major == 10:
            restore_plugin_execution(host, current, baseline["plugins"])
        stage(j,"healthcheck")
        health.assess(host,baseline,bkp)
        os.sync()
        reopen_new(host,j)
    except BaseException:
        if read(JOURNAL).get("phase")!="reabrindo_nova":
            revert(host,j)
        raise


def manual(host, expected, job=None):
    require(not JOURNAL.exists(),"Já existe uma transação pendente.")
    last=read(LAST)
    require(last.get("result")=="aplicada" and last["clone"]==expected,"Ponto de restauração expirado.")
    require(last["app"]==str(host.app) and last["external"]==list(map(str,host.ext)) and last["units"]==host.units,"A configuração do host mudou.")
    require((Path(last["area"])/"original").is_dir(),"Árvore original ausente.")
    info=host.inspect()
    require(info["db"]==last["clone"] and info["version"]==last["target"],"A instalação mudou após a atualização.")
    host.layout(info)
    j={**last,"activity_job":job or last["job"],"active":[u for u in host.units if host.service("active",u)]}
    with host.client(info) as (sql,_,__):
        j["maintenance"]=sql("SELECT value FROM glpi_configs WHERE context='core' AND name='maintenance_mode'")=="1"
    disk_check(host,info,False)
    stage(j,"backup_antes_de_reverter")
    try:
        freeze(host,j)
        safety_copy(host,j,info,Path(j["backup"])/("antes-de-reverter-"+secrets.token_hex(4)),job or j["job"],"before_rollback")
    except BaseException:
        release(host,j,"cancelando_reversao")
        clear_journal()
        raise
    stage(j,"revertendo")
    revert(host,j)


def security_backup(host, job, destination="local", storage=None):
    require(not JOURNAL.exists(), "Há uma operação pendente.")
    require(re.fullmatch(r"[0-9a-f]{16}",job), "Identificador de trabalho inválido.")
    info=host.inspect()
    host.layout(info)
    require(not any(within(STATE,p) for p in [host.app,*host.ext]), "O destino de backup faz parte dos dados de origem.")
    disk_check(host,info,False)
    with host.client(info) as (sql,_,__):
        mode=sql("SELECT value FROM glpi_configs WHERE context='core' AND name='maintenance_mode'")
        require(mode in ("0","1"),"O estado de manutenção não pôde ser determinado.")
    j={"app":str(host.app),"external":list(map(str,host.ext)),"units":host.units,
       "active":[u for u in host.units if host.service("active",u)],"maintenance":mode=="1","job":job}
    root=STATE/"backups"/"manuais"
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=time.time()
    dest=root/(time.strftime("%Y%m%dT%H%M%SZ",time.gmtime(stamp))+"-"+job)
    require(not dest.exists(),"Destino de backup já existente.")
    with tempfile.TemporaryDirectory(prefix=".parcial-",dir=root) as temp:
        directory=Path(temp)
        stage(j,"backup_manual")
        try:
            freeze(host,j)
            backup(host,j,info,directory)
            location={"destination":destination,"storage":storage,"pending":True} if storage else {}
            write(directory/"resumo.json",{**location,"time":stamp,"version":info["version"],"path":str(dest),
                  "roots":[str(p) for p in (host.app,*host.ext)],"size":sum((directory/f).stat().st_size for f in ("database.sql","files.tar"))})
            durable_move(directory,dest)
        finally:
            release(host,j,"reabrindo_backup_manual")
            clear_journal()

    return dest

def recent_security_backups(state):
    root=state/"backups"/"manuais"
    if not root.is_dir():
        return []
    import heapq
    names=(p for p in root.iterdir() if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}",p.name)
           and p.is_dir() and not p.is_symlink())
    return [read(p/"resumo.json") for p in heapq.nlargest(25,names) if (p/"resumo.json").is_file()]


def stage_restore_tree(host, directory, area):
    roots=[host.app,*host.ext]
    names=set()
    links=set()
    archive=directory/"files.tar"
    with tarfile.open(archive,"r:") as tar:
        for member in tar:
            name=Path(member.name)
            original=Path("/")/name
            require(not name.is_absolute() and ".." not in name.parts and name not in names
                    and any(within(original,p) for p in roots),"Arquivo incompatível com a instalação.")
            require(member.isfile() or member.isdir() or member.issym(),"Tipo de arquivo não suportado.")
            names.add(name)
            if member.issym():
                target=Path(os.path.normpath(original.parent/member.linkname))
                require(any(within(target,p) for p in roots),"Link fora das árvores de backup.")
                links.add(name)
    require(not any(any(p in links for p in n.parents) for n in names),"Arquivo sob um link simbólico.")
    for root in roots:
        relative=root.relative_to("/")
        require(relative in names and relative not in links,"Raiz ausente no backup.")
    work=area/"extraido"
    work.mkdir(mode=0o700)
    run(["tar","--acls","--xattrs","--selinux","--numeric-owner","-xf",archive,"-C",work])
    return work


def finish_security_restore(host,j):
    release(host,j,"reabrindo_restauracao")
    summary=read(STATE/"health.json",{})
    try:
        summary["Acesso público"]=health.http_probe(host.c["status_url"]) if not j["maintenance"] else "Manutenção preexistente preservada"
    except Exception:
        summary["Acesso público"]="Falha no teste HTTP após reabrir a restauração. Revise o acesso público."
    write(STATE/"health.json",summary)
    write(LAST,{"result":"restaurada","time":time.time(),"backup":j["backup"]})
    clear_journal()


def restore_security_backup(host, directory, job):
    require(not JOURNAL.exists(),"Há uma transação pendente.")
    meta=read(directory/"resumo.json")
    info=host.inspect()
    host.layout(info)
    require(info["version"]==meta["version"],"Restaure em uma instalação da mesma versão do GLPI.")
    roots=[host.app,*host.ext]
    require(meta.get("roots",list(map(str,roots)))==list(map(str,roots)),"Layout do backup diferente.")
    cfg=config_file(host, info)
    trusted(cfg.parent)
    require(cfg.is_file() and cfg.resolve()==cfg and any(within(cfg,p) for p in roots),"Configuração incompatível.")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        point=Path(re.sub(r"\\([0-7]{3})",lambda m:chr(int(m[1],8)),line.split()[4]))
        require(not within(point,host.app),"Não é possível trocar uma árvore com volumes montados.")
    disk_check(host,info,True)
    require(shutil.disk_usage(host.app.parent).free>=(directory/"files.tar").stat().st_size+512*1024**2,
            "Espaço insuficiente para preparar a restauração.")
    baseline=health.inspect(host)
    area=Path(tempfile.mkdtemp(prefix=".secureupdate-restore-",dir=host.app.parent))
    try:
        work=stage_restore_tree(host,directory,area)
        new=work/host.app.relative_to("/")
        cfg_new=work/cfg.relative_to("/")
        require(cfg_new.is_file() and cfg_new.resolve()==cfg_new,"Configuração arquivada inválida.")
        clone="gsu_"+secrets.token_hex(8)
        cfg_new.unlink()
        run(["cp","-a","--",cfg,cfg_new])
        switch_db(cfg_new,info["db"],clone)
        with host.client(info) as (sql,_,make_clone):
            mode=sql("SELECT value FROM glpi_configs WHERE context='core' AND name='maintenance_mode'")
            require(mode in ("0","1"),"Estado de manutenção desconhecido.")
            make_clone(directory/"database.sql",clone)
    except BaseException:
        shutil.rmtree(area)
        raise
    safe=STATE/"restore-points"/job
    safe.mkdir(mode=0o700,parents=True,exist_ok=False)
    write(safe/"health-before.json",baseline)
    j=dict(app=str(host.app),external=list(map(str,host.ext)),units=host.units,active=[u for u in host.units if host.service("active",u)],
           maintenance=mode=="1",source=info["version"],target=meta["version"],db=info["db"],clone=clone,
           area=str(area),backup=str(safe),job=job)
    stage(j,"preparando_restauracao")
    try:
        freeze(host,j)
        backup(host,j,info,safe)
    except BaseException:
        release(host,j,"cancelando_restauracao")
        clear_journal()
        raise
    j.update(changed=True,snapshot=True)
    stage(j,"trocando_restauracao")
    try:
        durable_move(host.app,area/"original")
        durable_move(new,host.app)
        for dst in host.ext:
            sync_tree(work/dst.relative_to("/"),dst)
        active=host.inspect()
        require(active["db"]==clone and active["version"]==meta["version"],"Banco ou versão restaurada divergente.")
        host.console("maintenance:enable")
        host.console("db:check")
        health.assess(host,baseline,safe)
        os.sync()
        finish_security_restore(host,j)
    except BaseException:
        if read(JOURNAL)["phase"]!="reabrindo_restauracao":
            revert(host,j)
        raise


def recover(host):
    j=read(JOURNAL)
    require(j["app"]==str(host.app) and j["external"]==list(map(str,host.ext)) and j["units"]==host.units,"Restaure a configuração original do serviço.")
    if j["phase"]=="reabrindo_restauracao":
        finish_security_restore(host,j)
    elif j["phase"] in ("preparando_restauracao","cancelando_restauracao"):
        release(host,j,"cancelando_restauracao")
        clear_journal()
    elif j["phase"] in ("backup_manual","reabrindo_backup_manual"):
        release(host,j,"reabrindo_backup_manual")
        clear_journal()
    elif j["phase"]=="reabrindo_original":
        release(host,j,"reabrindo_original")
        finish_revert(j)
    elif j["phase"]=="reabrindo_nova":
        reopen_new(host,j)
    elif j["phase"] in ("backup_antes_de_reverter","cancelando_reversao"):
        release(host,j,"cancelando_reversao")
        clear_journal()
    elif j["phase"] in ("manutencao","backup","preparando","trocando","migrando","healthcheck","revertendo","trocando_restauracao"):
        for unit in host.units: host.service("stop",unit)
        require(not any(host.service("active",u) for u in host.units),"Gravadores ativos durante recuperação.")
        revert(host,j)
    else:
        raise ValueError("Fase desconhecida. A recuperação requer inspeção do registro.")
