#!/usr/bin/env python3
"""Instala o painel e o serviço local. Execute a partir do pacote extraído fora do site."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

HERE=Path(__file__).resolve().parent
LIB=Path("/usr/local/lib/glpi-secureupdate")
CFG=Path("/etc/glpi-secureupdate")
UNIT=Path("/etc/systemd/system/glpi-secureupdate.service")

def check(ok,msg):
    if not ok: raise SystemExit(msg)

def privileged_dir(path):
    check(path.resolve()==path and path.is_dir(),"Diretório real necessário: "+str(path))
    for p in [path,*path.parents]:
        m=p.stat()
        check(m.st_uid==0 and m.st_mode & 0o022==0,"Proteja o dono e as permissões contra escrita sem root: "+str(p))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--glpi",required=True,help="Raiz do GLPI (contém bin/console)")
    p.add_argument("--usuario-php",required=True,help="Conta Unix do pool PHP deste GLPI")
    p.add_argument("--url-status",required=True,help="URL real do GLPI terminada em /status.php")
    p.add_argument("--parar",action="append",required=True,help="Unidade systemd dedicada a parar. Repita para todos os gravadores e ingressos.")
    p.add_argument("--externo",action="append",default=[],help="Diretório real de dados/configuração fora do GLPI. Repetível.")
    p.add_argument("--php",default="/usr/bin/php")
    a=p.parse_args()
    check(sys.platform=="linux" and os.geteuid()==0,"Execute em Linux com sudo.")
    import tarfile
    check(sys.version_info>=(3,9) and hasattr(tarfile,"data_filter"),"Python 3.9+ com tarfile.data_filter necessário.")
    os.environ["PATH"]="/usr/sbin:/usr/bin:/sbin:/bin"
    php=Path(a.php)
    check(php.is_absolute() and php.is_file() and os.access(php,os.X_OK),"Informe o caminho absoluto do PHP CLI.")
    php=php.resolve(strict=True)
    privileged_dir(php.parent)
    check(php.stat().st_uid==0 and not php.stat().st_mode & 0o022,"Proteja o executável PHP contra escrita sem root.")
    a.php=str(php)
    py=sys.executable
    app=Path(a.glpi)
    privileged_dir(app)
    privileged_dir(app/"plugins")
    check((app/"bin/console").is_file(),"bin/console ausente.")
    check(not HERE.is_relative_to(app),"Extraia o pacote fora da árvore do GLPI.")
    for f in ["curl","rsync","systemctl","sha256sum","tar","cp",a.php]:
        check(shutil.which(f) is not None,"Comando ausente: "+f)
    check(any(all(shutil.which(b) for b in pair) for pair in (("mariadb","mariadb-dump"),("mysql","mysqldump"))),"Instale o cliente e o utilitário de dump do banco.")
    check(pwd.getpwnam(a.usuario_php).pw_uid!=0,"O pool PHP precisa de um usuário dedicado sem root.")
    check(urlsplit(a.url_status).scheme in ("http","https") and urlsplit(a.url_status).path.endswith("/status.php"),"URL /status.php inválida.")
    for u in a.parar:
        check(re.fullmatch(r"[A-Za-z0-9_@][\w.@:-]*\.(service|socket|timer)",u),"Nome de unidade inválido.")
        check(subprocess.check_output(["systemctl","show","-p","LoadState","--value",u],text=True).strip()=="loaded","Unidade não carregada: "+u)
    for raw in a.externo:
        path=Path(raw)
        check(path.is_dir() and path.resolve()==path and len(path.parts)>=3,"Diretório externo inválido.")
        privileged_dir(path.parent)
    destinations=[app/"plugins/secureupdate",LIB,CFG,UNIT]
    for dest in destinations: check(not dest.exists() and not dest.is_symlink(),"Destino já existe: "+str(dest))
    for source in (HERE/"secureupdate",HERE/"service",HERE/"secureupdate.service.in"):
        check(not source.is_symlink() and not any(item.is_symlink() for item in source.rglob("*")),"Link inesperado no pacote.")
    for path in (HERE/"secureupdate").rglob("*.php"):
        subprocess.run([a.php,"-n","-l",str(path)],check=True)
    for path in (HERE/"service").glob("*.py"):
        compile(path.read_bytes(),str(path),"exec")
    rw=[str(app.parent),*a.externo]
    for path in [*rw,py]:
        check(re.fullmatch(r"/[A-Za-z0-9_./+-]+",path),"Use caminhos de serviço sem espaços/metacaracteres.")
    config=dict(app=str(app),web_user=a.usuario_php,php=a.php,status_url=a.url_status,units=a.parar,external=a.externo)
    committed=[]
    try:
        for src,dest in [(HERE/"secureupdate",destinations[0]),(HERE/"service",LIB)]:
            dest.mkdir(mode=0o755)
            committed.append(dest)
            shutil.copytree(src,dest,dirs_exist_ok=True,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
            for f in [dest,*dest.rglob("*")]:
                check(not f.is_symlink(),"Link inesperado no pacote.")
                f.chmod(0o755 if f.is_dir() else 0o644)
        CFG.mkdir(mode=0o700)
        committed.append(CFG)
        (CFG/"config.json").write_text(json.dumps(config,indent=2))
        (CFG/"config.json").chmod(0o600)
        template=(HERE/"secureupdate.service.in").read_text()
        with UNIT.open("x") as unit_file:
            committed.append(UNIT)
            unit_file.write(template.replace("@PYTHON@",py).replace("@RW@"," ".join(rw)))
        subprocess.run(["systemctl","daemon-reload"],check=True)
        subprocess.run(["systemctl","enable","--now",UNIT.name],check=True)
    except BaseException:
        if UNIT in committed:
            subprocess.run(["systemctl","disable","--now",UNIT.name],check=False)
        for path in reversed(committed):
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink(missing_ok=True)
        if UNIT in committed: subprocess.run(["systemctl","daemon-reload"],check=False)
        raise
    print("Serviço instalado. No GLPI, instale e ative Atualizações do GLPI em Configurar > Plugins.")

if __name__=="__main__":
    main()
