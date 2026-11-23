"""Resumo de suporte com campos permitidos; nunca lê logs, ambiente ou configuração do banco."""
import datetime
import re

PHASES={"gerenciando backups","criando backup","disponível","verificando","validando pacote","atualizando","revertendo","falha","concluída","interrompida","revertida","requer revisão"}
STEPS={"preparando_restauracao","cancelando_restauracao","trocando_restauracao","reabrindo_restauracao","backup_manual","reabrindo_backup_manual","manutencao","backup","preparando","trocando","migrando","healthcheck","reabrindo_nova","reabrindo_original","backup_antes_de_reverter","cancelando_reversao","revertendo"}

def stamp(value):
    if type(value) not in (float,int) or not 0<value<4102444800:
        return "Sem registro"
    return datetime.datetime.fromtimestamp(value,datetime.timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

def report(panel,transaction,last):
    job=panel.get("job")
    phase=panel.get("phase")
    step=transaction.get("phase")
    plan=panel.get("plan")
    checked=plan.get("checked") if isinstance(plan,dict) else None
    return {
        "Serviço local":"Conectado",
        "Versão do executor":"1.1.0",
        "Estado do trabalho":phase if isinstance(phase,str) and phase in PHASES else "Indeterminado",
        "Identificador":job if isinstance(job,str) and re.fullmatch("[0-9a-f]{16}",job) else "Sem trabalho",
        "Etapa da transação":step if isinstance(step,str) and step in STEPS else ("Revisão necessária" if transaction else "Nenhuma em andamento"),
        "Última consulta":stamp(checked),
        "Próxima consulta":stamp(panel.get("next")),
        "Ponto de rollback":"Disponível" if last.get("result")=="aplicada" else "Indisponível"
    }
