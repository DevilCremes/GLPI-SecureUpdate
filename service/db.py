"""Acesso ao banco com a identidade do PHP. Senha em descritor de memória privado."""
import contextlib
import fcntl
import os

# Alguns builds Python omitem símbolos do cabeçalho Linux fcntl.h.
ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
SEAL_PRIVATE_OPTIONS = (getattr(fcntl, "F_SEAL_WRITE", 8) | getattr(fcntl, "F_SEAL_GROW", 4)
                        | getattr(fcntl, "F_SEAL_SHRINK", 2) | getattr(fcntl, "F_SEAL_SEAL", 1))

INSPECIONAR = r"""
require 'vendor/autoload.php';
new \Glpi\Kernel\Kernel();
require_once GLPI_CONFIG_DIR . '/config_db.php';
$c = (new ReflectionClass('DB'))->getDefaultProperties();
$dirs = [GLPI_CONFIG_DIR, GLPI_VAR_DIR, GLPI_MARKETPLACE_DIR, ...GLPI_PLUGINS_DIRECTORIES];
foreach (get_defined_constants() as $k => $v) {
    if (preg_match('/^GLPI_(DOC|CACHE|CRON|GRAPH|LOCAL_I18N|LOCK|LOG|PICTURE|PLUGIN_DOC|RSS|SESSION|TMP|UPLOAD|INVENTORY|THEMES)_DIR$/D', $k)) {
        $dirs[] = $v;
    }
}
echo json_encode(['version'=>GLPI_VERSION, 'cfg'=>GLPI_CONFIG_DIR, 'dirs'=>array_values(array_unique($dirs)),
    'host'=>$c['dbhost'], 'user'=>$c['dbuser'], 'password'=>rawurldecode($c['dbpassword']),
    'db'=>$c['dbdefault'], 'ssl'=>$c['dbssl'] ?? false,
    'sslopts'=>array_map(fn($k)=>$c[$k] ?? null,
        ['dbsslkey','dbsslcert','dbsslca','dbsslcapath','dbsslcacipher']),
    'port'=>ini_get('mysqli.default_port'), 'socket'=>ini_get('mysqli.default_socket')],
    JSON_THROW_ON_ERROR);
"""


def exigir(ok, mensagem):
    if not ok:
        raise ValueError(mensagem)


def opcao(valor):
    s = str(valor)
    exigir("\x00" not in s, "Valor inválido na configuração do banco.")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


@contextlib.contextmanager
def cliente_db(info, conta, maria):
    host = info["host"]
    exigir(isinstance(host, str) and host.count(":") <= 1, "Este script requer um único endpoint MySQL/MariaDB.")
    nome, sep, sufixo = host.partition(":")
    opts = {"host": nome or "localhost", "user": info["user"], "password": info["password"]}
    if sep and sufixo.isdigit():
        opts["port"] = sufixo
        opts["protocol"] = "TCP"
    elif sep:
        opts["socket"] = sufixo
    elif nome in ("", "localhost") and info.get("socket"):
        opts["socket"] = info["socket"]
    elif info.get("port"):
        opts["port"] = info["port"]
    if info["ssl"]:
        opts["ssl" if maria else "ssl-mode"] = "1" if maria else "REQUIRED"
        for k, v in zip(("ssl-key", "ssl-cert", "ssl-ca", "ssl-capath", "ssl-cipher"), info["sslopts"]):
            if v:
                opts[k] = v
    exigir(info["db"] and not info["db"].startswith("-"), "Nome do banco inválido.")
    with os.fdopen(os.memfd_create("glpi-db", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING), "w+b") as f:
        os.fchown(f.fileno(), conta.pw_uid, conta.pw_gid)
        os.fchmod(f.fileno(), 0o400)
        f.write(("[client]\n" + "".join(f"{k}={opcao(v)}\n" for k, v in opts.items())).encode())
        f.flush()
        fcntl.fcntl(f.fileno(), ADD_SEALS, SEAL_PRIVATE_OPTIONS)
        f.seek(0)
        yield f.fileno()


