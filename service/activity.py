"""Eventos públicos de andamento. A telemetria é opcional para a transação."""
import time

from common import read, write

ACTIVE = {"verificando", "validando pacote", "atualizando", "revertendo", "criando backup", "gerenciando backups"}
OPERATIONS = {
    "check": "Consultando versões oficiais", "download": "Preparando download do pacote",
    "upload": "Preparando pacote enviado", "apply": "Verificando requisitos da atualização",
    "rollback": "Preparando rollback", "backup": "Preparando backup", "bsave": "Salvando política de backup",
    "btest": "Testando o destino", "bdelete": "Excluindo backup", "brestore": "Preparando restauração",
    "bfetch": "Preparando download do backup", "bprune": "Aplicando retenção", "bretry": "Reenviando backup",
    "bregister": "Testando e cadastrando destino",
}
STEPS = {
    "metadata": "Consultando tamanho e SHA-256 da release oficial", "receiving": "Recebendo o pacote",
    "digest": "Conferindo tamanho e SHA-256", "verified": "Pacote verificado e registrado",
    "manutencao": "Ativando manutenção", "backup": "Copiando banco e arquivos",
    "preparando": "Preparando arquivos da nova versão", "trocando": "Trocando instalação",
    "migrando": "Migrando e verificando o banco", "healthcheck": "Executando o diagnóstico de saúde",
    "reabrindo_nova": "Reabrindo o GLPI atualizado", "reabrindo_original": "Reabrindo a instalação original",
    "backup_antes_de_reverter": "Criando backup antes do rollback", "revertendo": "Restaurando a instalação original",
    "cancelando_reversao": "Cancelando a reversão", "backup_manual": "Copiando banco e arquivos em manutenção",
    "reabrindo_backup_manual": "Reabrindo o GLPI após a cópia", "publish": "Verificando o backup no destino",
    "preparando_restauracao": "Preparando os dados da restauração", "trocando_restauracao": "Restaurando os dados",
    "cancelando_restauracao": "Cancelando a restauração", "reabrindo_restauracao": "Reabrindo o GLPI após a restauração",
}
END = {"falha": "Operação falhou", "interrompida": "Operação interrompida",
       "revertida": "Atualização revertida", "requer revisão": "Operação requer revisão"}


def begin(directory, job, op):
    try:
        write(directory / "activity.json", {"job": job, "events": [[int(time.time()), OPERATIONS[op]]],
                                            "transfer": None})
    except OSError:
        pass


def record(directory, job, step, transfer=None):
    if not job or step not in STEPS:
        return
    try:
        path = directory / "activity.json"
        data = read(path, {})
        if not isinstance(data, dict) or data.get("job") != job:
            return
        events = data["events"]
        label = STEPS[step]
        if events[-1][1] != label:
            events.append([int(time.time()), label])
        data["events"] = events[-48:]
        data["transfer"] = transfer
        write(path, data)
    except (OSError, ValueError, IndexError, KeyError):
        pass


def snapshot(directory, panel, journal, mailing):
    job = panel.get("job", "")
    phase = panel.get("phase", "")
    try:
        data = read(directory / "activity.json", {})
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("job") != job:
        data = {}
    events = data.get("events", [])
    active = phase in ACTIVE or bool(journal)
    busy = active or mailing
    if active:
        title = STEPS.get(journal.get("phase")) or (events[-1][1] if events else "Aguardando o executor")
        if phase in END:
            title = END[phase]
    elif mailing:
        title = "Enviando notificação de backup"
    elif phase == "disponível" and job and panel.get("prepared", {}).get("id") == job:
        title = "Pacote verificado. Aguardando aprovação"
    else:
        title = END.get(phase, "Operação concluída")
    return {"job": job, "busy": busy, "title": title, "events": events,
            "transfer": data.get("transfer") if active else None, "attention": phase in END}
