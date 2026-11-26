#!/usr/bin/env python3
"""Instala o painel e o serviço local. Execute a partir do pacote extraído fora do site."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit

import detectar_instalacao as detect

HERE=Path(__file__).resolve().parent
LIB=Path("/usr/local/lib/glpi-secureupdate")
CFG=Path("/etc/glpi-secureupdate")
UNIT=Path("/etc/systemd/system/glpi-secureupdate.service")

def check(ok,msg):
    if not ok: raise SystemExit(msg)

def confirm(message):
    return input(f"{message} [s/y/N] ").strip().lower() in ("s", "sim", "y", "yes")


def permission_details(path, info):
    mode = stat.S_IMODE(info.st_mode)
    return f"{path} (UID={info.st_uid}, GID={info.st_gid}, modo={mode:04o})"


def privileged_dir(path):
    check(path.resolve()==path and path.is_dir(),"Diretório real necessário: "+str(path))
    for p in [path,*path.parents]:
        m=p.stat()
        if m.st_uid != 0 or m.st_mode & 0o022:
            raise SystemExit(f"{permission_details(p, m)} precisa pertencer a root e permitir escrita apenas ao dono. "
                             "Revise este caminho com namei -l antes de repetir a instalação.")

def installation_permissions(app, preview):
    privileged_dir(app.parent)
    for path in (app, app/"plugins"):
        check(path.is_absolute() and path.is_dir() and path.resolve() == path,
              "Diretório real necessário: " + str(path))

    for path in (app, app/"plugins"):
        # The parent is already trusted. Pin the directory across the confirmation.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid == 0 and not mode & 0o022:
                continue
            proposed = mode & ~0o022
            detail = permission_details(path, info)
            message = (f"{detail} precisa de UID 0 e modo {proposed:04o} para o SecureUpdate. "
                       f"O ajuste afeta somente esse diretório e preserva o grupo {info.st_gid}.")
            # Transferring ownership must not remove read/traverse access for the
            # previous owner. Nontrivial ACLs and special mode bits need local review.
            restricted_owner = info.st_uid != 0 and (mode & 0o055) != 0o055
            if restricted_owner or mode & 0o7000 or any(
                    name.startswith("system.posix_acl_") for name in os.listxattr(fd)):
                raise SystemExit(message + " Revise manualmente as permissões de acesso do usuário PHP e as ACLs.")
            if preview or not sys.stdin.isatty():
                raise SystemExit(message + " Execute o instalador em um terminal interativo para revisar o ajuste.")
            print(message, flush=True)
            if not confirm("Aplicar este ajuste sem recursão e continuar?"):
                raise SystemExit("Ajuste recusado para " + str(path))
            if info.st_uid != 0:
                os.fchown(fd, 0, -1)
            os.fchmod(fd, proposed)
        finally:
            os.close(fd)
        privileged_dir(path)


def choose(value, flag, message, candidates=()):
    if value:
        return value
    hint = " Opções: " + ", ".join(map(str, candidates)) if candidates else ""
    if not sys.stdin.isatty():
        raise SystemExit(message + hint + " Informe " + flag + ".")
    detected = f" Detecção: {candidates[0]}. Pressione Enter para confirmar." if len(candidates) == 1 else ""
    result = input(message + hint + detected + " > ").strip()
    if result:
        return result
    if len(candidates) == 1:
        return str(candidates[0])
    raise SystemExit(message + hint + " Informe " + flag + ".")


def verified_php(value):
    php = Path(value)
    check(php.is_absolute() and php.is_file() and os.access(php,os.X_OK),
          "PHP CLI ausente. Instale o CLI correspondente ao PHP web ou informe --php.")
    php = php.resolve(strict=True)
    privileged_dir(php.parent)
    check(php.stat().st_uid==0 and not php.stat().st_mode & 0o022,"Proteja o executável PHP contra escrita sem root.")
    return str(php)


def php_user(name):
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        raise SystemExit("Conta Unix inexistente. Informe --usuario-php.") from None
    check(account.pw_uid != 0, "O PHP do GLPI deve usar uma conta dedicada sem root.")
    return account


def require_tools(allow_install, preview):
    missing = detect.missing_dependencies()
    if not missing:
        return
    print("Dependências ausentes: " + ", ".join(missing), flush=True)
    if preview:
        return
    if not allow_install and sys.stdin.isatty():
        allow_install = confirm("Instalar esses utilitários pelo gerenciador do sistema?")
    check(allow_install, "Para instalar os utilitários ausentes, repita com --instalar-dependencias.")
    if shutil.which("apt-get"):
        command = ["apt-get", "install", "-y", "--no-install-recommends", *missing]
    elif shutil.which("dnf"):
        command = ["dnf", "install", "-y", *["mariadb" if p=="mariadb-client" else p for p in missing]]
    else:
        raise SystemExit("Instale os pacotes indicados pelo gerenciador da distribuição.")
    subprocess.run(command, check=True)
    check(not detect.missing_dependencies(), "O gerenciador não forneceu todos os utilitários necessários.")


def maintenance_units(app, args, apache, versions):
    if args.parar:
        return args.parar
    loaded = detect.loaded_units()
    units, has_web = detect.unit_suggestions(app.name, loaded)
    if apache and "apache2.service" in loaded and not has_web:
        units.append("apache2.service")
        has_web = True
    if not has_web and len(versions) == 1:
        fpm = f"php{versions[0]}-fpm.service"
        if fpm in loaded:
            units.append(fpm)
            has_web = True
    for unit in list(units):
        if unit.endswith((".timer", ".socket")):
            triggers = subprocess.check_output(["systemctl","show","-p","Triggers","--value",unit], text=True).split()
            for service in triggers:
                if service.endswith(".service") and service not in units:
                    units.insert(units.index(unit) + 1, service)
    # Stop all triggers before their workers.
    units.sort(key=lambda u: not u.endswith((".timer", ".socket")))
    print("Unidades de manutenção sugeridas:", ", ".join(units) or "nenhuma")
    if "cron.service" in loaded or "crond.service" in loaded:
        print("Cron global carregado. Confira se há tarefas do GLPI fora das unidades listadas.")
    if args.detectar:
        if not has_web:
            print("Ingresso web sem associação confirmada. Informe --parar ao instalar.")
        return units
    print("Confira se a lista cobre o ingresso, cron, timers, workers e integrações desta instalação.")
    if apache or any(re.fullmatch(r"php\d+\.\d+-fpm\.service", u) for u in units):
        print("A unidade web pode atender outros sites. Confirme que ela e a conta PHP são dedicadas ao GLPI.")
    if has_web and args.aceitar_unidades:
        return units
    if not sys.stdin.isatty():
        raise SystemExit("Informe --parar para cada unidade ou revise --detectar e use --aceitar-unidades quando o ingresso for identificado.")
    answer = input("Digite a lista completa em ordem de parada" + (" ou Enter para aceitar" if has_web else "") + " > ").strip()
    if answer:
        return answer.split()
    check(has_web, "Informe também a unidade que atende ao GLPI.")
    return units


def read_settings(a):
    app = Path(choose(a.glpi, "--glpi", "Qual a raiz do GLPI?", detect.installations() if not a.glpi else ()))
    check((app/"bin/console").is_file() and (app/"vendor/autoload.php").is_file(),
          "A raiz selecionada precisa conter bin/console e vendor/autoload.php.")
    check(not HERE.is_relative_to(app),"Extraia o pacote fora da árvore do GLPI.")
    installation_permissions(app, a.detectar)
    pools = detect.pool_candidates(app)
    apache = detect.apache_account(app)
    names = sorted({u for u, _ in pools} | ({apache} if apache else set()))
    if not a.usuario_php:
        associations = [f"{user} (pool FPM ligado ao vhost do GLPI, PHP {version})" for user, version in pools]
        if apache:
            associations.append(f"{apache} (Apache com raiz correspondente ao GLPI)")
        if associations:
            print("Contas associadas a este GLPI:\n - " + "\n - ".join(associations), flush=True)
    user = choose(a.usuario_php, "--usuario-php", "Qual a conta Unix que executa o PHP deste GLPI?", names)
    versions = sorted({v for u, v in pools if u == user and v})
    php_names = [f"/usr/bin/php{v}" for v in versions] or [shutil.which("php") or "/usr/bin/php"]
    php = verified_php(choose(a.php, "--php", "Qual o executável PHP CLI?", php_names))
    context = detect.php_context(app, php_user(user))
    data = detect.probe(php, context)
    url = choose(a.url_status, "--url-status", "Qual a URL de status do GLPI?",
                 [data["url"].rstrip("/") + "/status.php"] if data["url"] else [])
    parsed = urlsplit(url)
    check(parsed.scheme in ("http","https") and parsed.hostname and not parsed.username and not parsed.password
          and not parsed.fragment and not parsed.query and parsed.path.endswith("/status.php")
          and all(ord(ch) > 32 and ord(ch) != 127 for ch in url), "URL /status.php inválida.")
    units = maintenance_units(app, a, apache and user == apache, versions)
    check(len(set(units)) == len(units), "Unidade repetida.")
    for u in units:
        check(re.fullmatch(r"[A-Za-z0-9_@][\w.@:-]*\.(service|socket|timer)",u),"Nome de unidade inválido.")
        check(subprocess.check_output(["systemctl","show","-p","LoadState","--value",u],text=True).strip()=="loaded","Unidade não carregada: "+u)
    external = detect.external_roots(app, data["dirs"], a.externo)
    for raw in external:
        path = Path(raw)
        check(path.is_dir() and path.resolve()==path and len(path.parts)>=3,"Diretório externo inválido: "+raw)
        protected = [app, Path("/var/lib/glpi-secureupdate")]
        check(not any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected)
              and not any(Path(other).is_relative_to(path) for other in external if other != raw),
              "Diretórios externos sobrepostos ao GLPI, ao estado ou entre si.")
        privileged_dir(path.parent)
    config = dict(app=str(app),web_user=user,php=php,status_url=url,units=units,external=external)
    print(f"GLPI {data['version']} em {app} | PHP {php} ({user})")
    print("Status:", url, "| Diretórios externos:", ", ".join(external) or "nenhum")
    print("Ordem de parada:", ", ".join(units) or "a definir")
    if data["states"].get("healthcheck") != 1:
        print("GLPI Health Check de João Paulo S. Costa ausente ou inativo. Ative-o antes de atualizar o GLPI.")
    return config, data, context


def install_files(config):
    app = Path(config["app"])
    py = sys.executable
    destinations=[app/"plugins/secureupdate",LIB,CFG,UNIT]
    for dest in destinations: check(not dest.exists() and not dest.is_symlink(),"Destino já existe: "+str(dest))
    for source in (HERE/"secureupdate",HERE/"service",HERE/"secureupdate.service.in"):
        check(not source.is_symlink() and not any(item.is_symlink() for item in source.rglob("*")),"Link inesperado no pacote.")
    for path in (HERE/"secureupdate").rglob("*.php"):
        subprocess.run([config["php"],"-n","-l",str(path)],check=True)
    for path in (HERE/"service").glob("*.py"):
        compile(path.read_bytes(),str(path),"exec")
    rw=[str(app.parent),*config["external"]]
    for path in [*rw,py]:
        check(re.fullmatch(r"/[A-Za-z0-9_./+-]+",path),"Use caminhos de serviço sem espaços/metacaracteres.")
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
        (CFG/"registro-pendente").touch(mode=0o600)
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


def registration_user(data, explicit, config, context):
    if data["version"].startswith("10.") and data["states"].get("secureupdate") not in (1, 4):
        selected = choose(explicit, "--admin-glpi", "Qual o login do administrador do GLPI para registrar o plugin?",
                      [data["admin"]] if data.get("admin") else [])
        if selected != data.get("admin"):
            matched = detect.probe(config["php"], context, selected)
            check(matched.get("admin") == selected, "O login informado precisa ser um Super-Admin ativo.")
        return selected
    return None


def register(config, data, context, admin):
    cli = [config["php"], "bin/console", "--no-interaction", "--no-ansi"]
    state = data["states"].get("secureupdate")
    if state not in (1, 4):
        command = [*cli, "plugin:install", "secureupdate"]
        if admin:
            command.append("--username=" + admin)
        subprocess.run(command, check=True, **context)
        state = detect.probe(config["php"], context)["states"].get("secureupdate")
    if state != 1:
        subprocess.run([*cli, "plugin:activate", "secureupdate"], check=True, **context)
    check(detect.probe(config["php"], context)["states"].get("secureupdate") == 1,
          "A ativação não foi confirmada no GLPI. Corrija o erro e repita o instalador.")
    subprocess.run(["systemctl","is-active","--quiet",UNIT.name], check=True)
    (CFG/"registro-pendente").unlink()
    print("SecureUpdate instalado e ativo. Entre no GLPI como Super-Admin e abra Configurar > Plugins.")


def install(a):
    pending = CFG/"registro-pendente"
    if CFG.exists():
        privileged_dir(CFG)
        if not pending.is_file():
            raise SystemExit("Já existe uma instalação ou configuração local. Para trocar de versão, siga o manual de atualização do próprio SecureUpdate.")
        check(not pending.is_symlink(), "Marcador de instalação inválido.")
        config = json.loads((CFG/"config.json").read_text())
        app = Path(config["app"])
        check(not a.glpi or str(app) == a.glpi, "O registro pendente pertence a outra instalação.")
        privileged_dir(app)
        verified_php(config["php"])
        context = detect.php_context(app, php_user(config["web_user"]))
        data = detect.probe(config["php"], context)
        print("Retomando o registro do SecureUpdate em", app)
        admin = registration_user(data, a.admin_glpi, config, context)
        check(UNIT.is_file() and LIB.is_dir() and (app/"plugins/secureupdate/setup.php").is_file(),
              "Arquivos da instalação pendente incompletos. Verifique as pastas do painel e do executor.")
        subprocess.run(["systemctl","daemon-reload"], check=True)
        subprocess.run(["systemctl","enable","--now",UNIT.name], check=True)
    else:
        config, data, context = read_settings(a)
        admin = registration_user(data, a.admin_glpi, config, context)
        require_tools(a.instalar_dependencias, False)
        install_files(config)
    print("Registrando e ativando o plugin. Se esta etapa falhar, repita o instalador para retomá-la.", flush=True)
    register(config, data, context, admin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glpi", help="Raiz do GLPI")
    parser.add_argument("--usuario-php", help="Conta Unix do PHP deste site")
    parser.add_argument("--php", help="Caminho do PHP CLI")
    parser.add_argument("--url-status", help="URL do GLPI terminada em /status.php")
    parser.add_argument("--parar", action="append", help="Unidade dedicada, em ordem de parada (repetível)")
    parser.add_argument("--externo", action="append", default=[], help="Raiz externa dedicada (repetível)")
    parser.add_argument("--admin-glpi", help="Login do administrador para registrar o plugin no GLPI 10")
    parser.add_argument("--detectar", action="store_true", help="Mostrar a configuração sem instalar")
    parser.add_argument("--aceitar-unidades", action="store_true", help="Aceitar em lote as unidades de manutenção identificadas")
    parser.add_argument("--instalar-dependencias", action="store_true", help="Instalar utilitários ausentes via APT ou DNF")
    a = parser.parse_args()
    check(sys.platform=="linux" and os.geteuid()==0,"Execute em Linux com sudo.")
    import tarfile
    check(sys.version_info >= (3,9) and hasattr(tarfile,"data_filter"), "Python 3.9+ com tarfile.data_filter necessário.")
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    check(shutil.which("systemctl"), "systemctl ausente. É necessário Linux com systemd.")
    if a.detectar:
        require_tools(False, True)
        config, data, _ = read_settings(a)
        if data["version"].startswith("10.") and not (a.admin_glpi or data.get("admin")):
            print("O registro inicial no GLPI 10 pode exigir --admin-glpi LOGIN.")
        return
    fd = os.open("/run/glpi-secureupdate-instalar.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Outra instalação do SecureUpdate está em andamento.")
        install(a)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Instalação interrompida: {exc}") from None
