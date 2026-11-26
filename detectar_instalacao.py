"""Descoberta local para o instalador Linux do SecureUpdate."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from service.db import GLPI_LAYOUT


def web_configs(etc):
    for pattern in ("apache2/sites-enabled/*", "httpd/conf.d/*.conf",
                    "nginx/sites-enabled/*", "nginx/conf.d/*.conf"):
        for conf in etc.glob(pattern):
            if conf.is_file():
                yield conf.read_text(errors="replace")


def document_roots(config):
    pattern = r"""(?im)^\s*(?:DocumentRoot|root)\s+["']?(/[^"'\s;]+)"""
    return {Path(match[1]).resolve() for match in re.finditer(pattern, config)}


def installations(etc=Path("/etc")):
    roots = set(map(Path, ("/var/www/glpi", "/var/www/html/glpi", "/var/www/html",
                           "/usr/share/glpi", "/srv/glpi", "/srv/www/glpi", "/opt/glpi")))
    for config in web_configs(etc):
        for doc in document_roots(config):
            roots.add(doc.parent if doc.name == "public" else doc)
    return sorted({p.resolve() for p in roots if (p/"bin/console").is_file()
                   and (p/"vendor/autoload.php").is_file()
                   and ((p/"inc/based_config.php").is_file() or (p/"src/Glpi/Kernel/Kernel.php").is_file())})


def pool_candidates(app, etc=Path("/etc")):
    sockets = set()
    for config in web_configs(etc):
        roots = document_roots(config)
        if roots and roots <= {app, app/"public"}:
            sockets.update(Path(p).resolve() for p in re.findall(r"unix:(/[\w./-]+\.sock)\b", config))
    pools = set()
    for pattern in ("php/*/fpm/pool.d/*.conf", "php-fpm.d/*.conf", "opt/remi/php*/php-fpm.d/*.conf"):
        for path in etc.glob(pattern):
            sections = {}
            section = {}
            for line in path.read_text(errors="replace").splitlines():
                line = line.split(";", 1)[0].strip()
                if line.startswith("[") and line.endswith("]"):
                    section = sections.setdefault(line[1:-1].strip(), {})
                elif "=" in line:
                    key, value = line.split("=", 1)
                    section[key.strip()] = value.strip()
            for name, values in sections.items():
                user = values.get("user", "")
                sock = Path(values.get("listen", "").replace("$pool", name))
                # Match the FastCGI socket from a vhost serving this GLPI. Pool
                # naming alone is not evidence that it serves this installation.
                if not (sock.is_absolute() and sock.resolve() in sockets):
                    continue
                if re.fullmatch(r"[a-zA-Z_][\w.-]*", user) and user != "root":
                    version = next((p for p in path.parts if re.fullmatch(r"\d+\.\d+", p)), "")
                    pools.add((user, version))
    return sorted(pools)


def php_context(app, account):
    return dict(cwd=app, user=account.pw_uid, group=account.pw_gid,
                extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                umask=0o022, env={"PATH":"/usr/bin:/bin","HOME":account.pw_dir,"LANG":"C.UTF-8"})


# Return only the fields needed for setup. No database connection secrets leave PHP.
PROBE = GLPI_LAYOUT + r"""
$db = new DB();
$settings = [];
foreach ($db->request(['SELECT'=>['name','value'], 'FROM'=>'glpi_configs',
         'WHERE'=>['context'=>'core','name'=>['url_base','plugins_execution_mode']]]) as $row) { $settings[$row['name']] = $row['value']; }
$states = [];
foreach ($db->request(['SELECT'=>['directory','state'], 'FROM'=>'glpi_plugins',
         'WHERE'=>['directory'=>['secureupdate','healthcheck']]]) as $row) {
    $states[$row['directory']] = (int)$row['state'];
}
$admin = null;
if (version_compare(GLPI_VERSION, '11', '<')) {
    $names = [];
    $now = date('Y-m-d H:i:s');
    $where = ['glpi_profiles_users.profiles_id'=>4,'glpi_users.is_active'=>1,'glpi_users.is_deleted'=>0,
        ['OR'=>['glpi_users.begin_date'=>null, ['glpi_users.begin_date'=>['<=',$now]]]],
        ['OR'=>['glpi_users.end_date'=>null, ['glpi_users.end_date'=>['>=',$now]]]]];
    if (isset($argv[1])) { $where['glpi_users.name'] = $argv[1]; }
    foreach ($db->request(['SELECT'=>['glpi_users.name'], 'DISTINCT'=>true, 'FROM'=>'glpi_users',
             'INNER JOIN'=>['glpi_profiles_users'=>['ON'=>['glpi_profiles_users'=>'users_id','glpi_users'=>'id']]],
             'WHERE'=>$where,
             'LIMIT'=>2]) as $row) { $names[] = $row['name']; }
    if (count($names) === 1) { $admin = $names[0]; }
}
echo json_encode(['version'=>GLPI_VERSION,'admin'=>$admin,'dirs'=>array_values(array_unique($dirs)),
    'url'=>$settings['url_base'] ?? '', 'execution'=>$settings['plugins_execution_mode'] ?? 'on','states'=>(object)$states], JSON_THROW_ON_ERROR);
"""


def probe(php, context, admin=None):
    result = subprocess.run([php, "-r", PROBE, *(["--", admin] if admin else [])], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=45, **context)
    if result.returncode:
        raise ValueError("O PHP CLI da conta web não conseguiu carregar o GLPI e consultar o banco.")
    try:
        data = json.loads(result.stdout)
        if (not re.fullmatch(r"(10|11|12)\.0\.\d+", data["version"])
                or not isinstance(data["states"], dict) or not isinstance(data["url"], str)
                or not isinstance(data["dirs"], list) or not all(isinstance(p,str) for p in data["dirs"])):
            raise ValueError()
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError("O diagnóstico local não retornou um layout GLPI 10.0, 11.0 ou 12.0 válido.") from None
    if data.get("execution", "on") != "on":
        raise ValueError("A execução de plugins está suspensa no GLPI. Verifique o estado pelo painel.")
    return data


def external_roots(app, paths, explicit):
    roots = {Path(p) for p in explicit}
    conventional = [Path(p) for p in ("/etc/glpi", "/var/lib/glpi", "/var/log/glpi", "/var/cache/glpi")]
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            raise ValueError("O GLPI informou um diretório relativo.")
        p = p.resolve()
        if p.is_relative_to(app) or any(p.is_relative_to(root) for root in roots):
            continue
        root = next((r for r in conventional if p.is_relative_to(r)), None)
        if root is None:
            raise ValueError(f"Diretório externo personalizado {p}. Informe sua raiz dedicada com --externo.")
        roots.add(root)
    return sorted(map(str, roots))


def unit_suggestions(name, units):
    # Return only names tied to the selected instance. Require operator review of
    # global cron, ingress and external writers that systemd cannot associate.
    web = {f"php-fpm-{name}.service", f"{name}-php-fpm.service", f"php-fpm@{name}.service"}
    for unit in units:
        if re.fullmatch(r"php\d+\.\d+-fpm@" + re.escape(name) + r"\.service", unit):
            web.add(unit)
    tasks = {f"{name}-{role}.{kind}" for role in ("cron","worker","queue")
             for kind in ("timer","socket","service")}
    matches = (web | tasks).intersection(units)
    order = lambda u: (2 if u in web else 1 if u.endswith(".service") else 0, u)
    return sorted(matches, key=order), bool(web.intersection(matches))


def loaded_units():
    output = subprocess.check_output(["systemctl","list-units","--all","--plain","--no-legend",
                                      "--no-pager","--type=service,socket,timer"], text=True)
    return {line.split()[0] for line in output.splitlines() if line.strip()}


def missing_dependencies():
    packages = { "curl":"curl", "rsync":"rsync", "tar":"tar", "cp":"coreutils",
                 "sha256sum":"coreutils", "du":"coreutils" }
    missing = {pkg for exe, pkg in packages.items() if not shutil.which(exe)}
    if not any(all(shutil.which(exe) for exe in pair)
               for pair in (("mariadb","mariadb-dump"),("mysql","mysqldump"))):
        missing.add("mariadb-client")
    return sorted(missing)


def apache_account(app, etc=Path("/etc/apache2")):
    """Debian/Ubuntu mod_php with a literal vhost root and Apache runtime user."""
    if not any((etc/"mods-enabled").glob("php*.load")):
        return None
    for conf in (etc/"sites-enabled").glob("*"):
        if not conf.is_file():
            continue
        docs = re.findall(r'''(?im)^\s*DocumentRoot\s+["']?(/[^\s"']+)''', conf.read_text(errors="replace"))
        if any(Path(doc).resolve() in (app, app/"public") for doc in docs):
            envfile = etc/"envvars"
            if envfile.is_file():
                user = re.search(r"(?m)^\s*(?:export\s+)?APACHE_RUN_USER=([a-zA-Z_][\w.-]*)\s*$",
                                 envfile.read_text(errors="replace"))
                if user and user[1] != "root":
                    return user[1]
    return None
