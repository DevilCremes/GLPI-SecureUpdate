"""Avisos de backup pelo transporte do GLPI, com recibos por destinatário."""
import fcntl
import json
import re
import subprocess
from datetime import datetime, timezone

from engine import Host

MODES = ("off", "issues", "all")
OPERATIONS = {
    "backup": "Backup manual", "scheduled_backup": "Backup agendado", "bretry": "Reenvio ao destino",
    "brestore": "Restauração de backup", "bprune": "Limpeza por retenção",
    "btest": "Teste do destino", "bdelete": "Exclusão de backup",
    "bregister": "Cadastro de destino", "bfetch": "Download de backup", "bsave": "Configuração dos backups",
    "missed": "Backup agendado", "before_update": "Backup antes da atualização",
    "before_rollback": "Backup antes do rollback",
}


def accepts(mode, level):
    return mode == "all" or (mode == "issues" and level != "success")


def event(job, op, level, message, now):
    return {"id": job, "operation": OPERATIONS[op], "level": level, "message": message,
            "time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "acked": [], "due": now}


def deliver(conf, state, notice):
    # A leitura do GLPI e o transporte devem terminar antes de uma troca de banco.
    with (state / "operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy", []
        host = Host(conf)
        args = [host.php, "bin/console", "--no-interaction", "--no-ansi", "plugins:secureupdate:notify", "backup"]
        try:
            result = subprocess.run(args, input=json.dumps(notice).encode(), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=90, **host.web)
            output = result.stdout
            ok = result.returncode == 0 and b"SU_DONE" in output.splitlines()
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or b""
            ok = False
        acked = [int(match[1]) for match in re.finditer(rb"^SU_ACK ([1-9][0-9]{0,9})$", output, re.M)]
        return ("ok" if ok else "error"), acked
