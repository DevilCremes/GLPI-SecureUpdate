#!/usr/bin/python3
"""Protocolo local do plugin. O serviço só aceita operações predefinidas."""
import activity
import argparse
from concurrent.futures import ThreadPoolExecutor
import backup_mail
import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import pwd
import packages
import vault
import schedule
import re
import secrets
import socket
import struct
import subprocess
import sys
import time

from common import CONF, SOCKET, STATE, catalog, read, require, write
from debug import report
from engine import Host, JOURNAL, LAST, create_job, manual, recover, trusted, security_backup, restore_security_backup

STORE=STATE/"panel.json"
BASE={"backup_email":"off","phase":"disponível","hours":24,"browser":True,"email":False,"next":0,"plan":{},"job":""}
SELF=Path(__file__).resolve()


BACKUP_OPS={"backup","bsave","btest","bdelete","brestore","bfetch","bprune","bretry","bregister"}


class Agent:
    def __init__(self, conf):
        self.conf=conf
        self.s={**BASE,**read(STORE,{})}
        self.process=None
        self.running_job=None
        self.mail_pool=ThreadPoolExecutor(max_workers=1)
        self.mail_task=None
        self.mail_id=None
        if self.s["phase"] in ("atualizando","revertendo"):
            self.checkpoint_notice(self.s["job"])
        previous=self.s.get("active_backup")
        if previous:
            self.backup_notice(previous["id"],previous["op"],"warning",
                               "O executor foi interrompido. Confira o catálogo e o estado da transação antes de repetir.",
                               active_backup=None)
        if self.s["phase"] in ("verificando","validando pacote","atualizando","revertendo","criando backup","gerenciando backups"):
            self.save(phase="interrompida",error="Consulte o registro local. Verifique a transação antes de repetir.")
        self.uid=pwd.getpwnam(conf["web_user"]).pw_uid

    def save(self,**items):
        updated={**self.s,**items}
        write(STORE,updated)
        self.s=updated

    def backup_notice(self,job,op,level,message,**changes):
        queue=list(self.s.get("backup_queue",[]))
        if backup_mail.accepts(self.s["backup_email"],level) and not any(e["id"]==job for e in queue):
            queue.append(backup_mail.event(job,op,level,message,time.time()))
            changes.setdefault("backup_mail_failed", False)
        self.save(backup_result={"job":job,"message":message},backup_queue=queue,**changes)

    def checkpoint_notice(self,job):
        item=read(STATE/"checkpoint-notice.json",{})
        if item.get("job")==job and item.get("op") in ("before_update","before_rollback"):
            self.backup_notice(job,item["op"],item["level"],item["message"])

    def mail_tick(self,now):
        if self.mail_task is not None:
            if not self.mail_task.done():
                return
            try:
                result,acked=self.mail_task.result()
            except Exception:
                result,acked="error",[]
            self.mail_task=None
            queue=list(self.s.get("backup_queue",[]))
            for index,item in enumerate(queue):
                if item["id"]!=self.mail_id:
                    continue
                if result=="ok":
                    queue.pop(index)
                    self.save(backup_queue=queue, backup_mail_failed=False)
                elif result=="terminal" or item.get("attempts",0)+1>=5:
                    queue.pop(index)
                    message=("Nenhum Super-Admin ativo com e-mail foi encontrado. Confira os destinatários."
                             if result=="terminal" else "O aviso de backup falhou após cinco tentativas. Confira o transporte de e-mail.")
                    self.save(backup_queue=queue, backup_mail_failed=True,
                              backup_result={"job":item["id"],"message":message})
                else:
                    queue[index]={**item,"acked":sorted(set(item["acked"])|set(acked)),
                                  "attempts":item.get("attempts",0)+(0 if result=="busy" else 1),
                                  "due":now+(15 if result=="busy" else 900),
                                  "failed":bool(item.get("failed") or result=="error")}
                    self.save(backup_queue=queue, backup_mail_failed=bool(item.get("failed") or result=="error"))
                break
            self.mail_id=None
            return
        if self.process:
            return
        queue=self.s.get("backup_queue",[])
        due=next((item for item in queue if item["due"]<=now),None)
        if due:
            self.mail_id=due["id"]
            self.mail_task=self.mail_pool.submit(backup_mail.deliver,self.conf,STATE,due)

    def poll(self):
        if not self.process or self.process.poll() is None:
            return
        rc=self.process.returncode
        self.process=None
        j=self.running_job
        self.running_job=None
        if j["op"] in ("apply","rollback"):
            self.checkpoint_notice(j["id"])
        if j["op"] in BACKUP_OPS and j["op"] != "bfetch":
            labels={"backup":"Backup criado e verificado.","bsave":"Política de backup salva.",
                    "bregister":"Destino testado e cadastrado. Selecione-o na política de backup.",
                    "btest":"O destino confirmou a gravação, a leitura e a exclusão de teste.",
                    "bdelete":"Backup excluído.","bprune":"Limpeza concluída.",
                    "brestore":"Restauração concluída.","bretry":"Cópia enviada e verificada.",
                    "bfetch":"Arquivo de download enviado pelo executor."}
            attention=j["op"]=="brestore" and rc==0 and read(STATE/"health.json",{}).get("Acesso público","").startswith("Falha")
            if attention:
                self.backup_notice(j["id"],j["op"],"warning","Dados restaurados. O teste HTTP após a reabertura falhou. Verifique a aba Saúde.",
                                   phase="requer revisão",error="",plan={},prepared={},active_backup=None)
                return
            detail=read(STATE/"backup-attention.json",{}) if rc else {}
            failure=detail["message"] if detail.get("job")==j["id"] else "A operação de backup falhou. Consulte o log deste trabalho no servidor."
            level="success" if rc==0 else (detail.get("level","warning") if detail.get("job")==j["id"] else "error")
            topic="scheduled_backup" if j.get("scheduled") else j["op"]
            self.backup_notice(j["id"],topic,level,labels[j["op"]] if rc==0 else failure,active_backup=None)
        if rc:
            if j["op"] in ("download","upload"):
                reason=read(STATE/"package-error.json",{})
                message=reason.get("message") if reason.get("id")==j["id"] else None
                self.save(phase="falha",error=message or "Falha na preparação do pacote. Consulte o trabalho no servidor.",prepared={})
                return
            last=read(STATE/"outcome.json",{})
            rolled=j["op"]=="apply" and last.get("result")=="revertida" and last.get("job")==j["id"]
            self.save(phase="revertida" if rolled else "falha",error="Atualização revertida. Consulte o diagnóstico." if rolled else "A operação falhou. Consulte o log deste trabalho no servidor.",active_backup=None)
            return
        if j["op"]=="check":
            self.save(phase="disponível",plan=read(STATE/"catalog.json"),error=read(STATE/"mail.json",{}).get("error",""))
        elif j["op"] in BACKUP_OPS:
            reset={"plan":{},"prepared":{}} if j["op"]=="brestore" else {}
            self.save(phase="concluída",error="",active_backup=None,**reset)
        elif j["op"] in ("download","upload"):
            receipt=read(STATE/"verified.json",{})
            if receipt.get("id")!=j["id"]:
                self.save(phase="falha",error="Recibo de verificação indisponível.",prepared={})
                return
            self.save(phase="disponível",prepared=receipt,error="")
        else:
            summary=read(STATE/"health.json",{})
            attention=summary.get("Acesso público","").startswith("Falha") and j["op"]=="apply"
            self.save(phase="requer revisão" if attention else "concluída",plan={},error=summary["Acesso público"] if attention else "")

    def start(self,op,actor,reference="",size=None,channel=None,options=None,scheduled=False):
        require(self.mail_task is None,"Envio de e-mail de backup em andamento. Tente novamente em instantes.")
        require(not self.process and not JOURNAL.exists(),"Outra operação ou recuperação está pendente.")
        plan=self.s["plan"]
        receipt=self.s.get("prepared",{})
        if op in ("download","upload"):
            require(plan.get("id")==reference and bool(reference) and packages.fresh(plan.get("checked")),
                    "Consulte novamente a versão antes de preparar o pacote.")
        if op=="upload":
            require(type(size) is int and 0<size<=packages.MAX_BYTES and channel is not None,"Envio de arquivo inválido.")
        if op=="apply":
            require(packages.matches(receipt,plan) and receipt["id"]==reference and receipt["actor"]==actor,
                    "Verifique o pacote com este Super-Admin antes de aprovar.")
        if op=="rollback":
            last=read(LAST,{})
            require(last.get("result")=="aplicada" and last["clone"]==reference,"Ponto de restauração indisponível.")
        job=secrets.token_hex(8)
        request={"op":op,"actor":actor,"ref":reference,"id":job}
        topic="scheduled_backup" if scheduled else op
        if scheduled:
            request["scheduled"]=True
        if options is not None and op not in ("check", "bregister"):
            request["options"]=options
        if op=="check":
            request["scope"] = options or "same"
            request["notify"] = request["scope"] == "same" and (actor==0 or self.s.get("schedule") is None)
        if op in ("download","upload","apply"):
            request["plan"]=plan
        if op=="upload":
            request["size"]=size
        ticket=STATE/("request-"+job+".json")
        write(ticket,request)
        try:
            activity.begin(STATE, job, op)
            phases={"backup":"criando backup","check":"verificando","download":"validando pacote","upload":"validando pacote","apply":"atualizando","rollback":"revertendo"}
            prepared={} if op in ("download","upload","apply","rollback") else receipt
            due=self.s["next"]
            if op=="check" and actor==0:
                due=schedule.next_check(self.s,time.time())
            if op == "check" and request["scope"] != plan.get("scope", "same"):
                plan, prepared = {}, {}
            self.save(plan=plan,next=due,job=job,phase=phases.get(op,"gerenciando backups"),error="",actor=actor,prepared=prepared,
                      active_backup={"id":job,"op":topic} if op in BACKUP_OPS and op!="bfetch" else None)
            prune_job_logs(STATE)
            with contextlib.ExitStack() as stack:
                log = stack.enter_context((STATE/("job-"+job+".log")).open("xb"))
                child_input = channel if op in ("upload","bfetch") else subprocess.DEVNULL
                if op == "bregister":
                    # Credentials live only in this anonymous Linux descriptor until the child exits.
                    child_input = stack.enter_context(os.fdopen(os.memfd_create("cadastro"), "w+b"))
                    child_input.write(json.dumps(options).encode("ascii"))
                    child_input.seek(0)
                self.process=subprocess.Popen([sys.executable,"-E","-s",str(SELF),"job",job],
                    cwd=SELF.parent,stdin=child_input,stdout=log,stderr=subprocess.STDOUT,close_fds=True)
            self.running_job=request
        except OSError:
            ticket.unlink(missing_ok=True)
            if op in BACKUP_OPS and op!="bfetch":
                self.backup_notice(job,topic,"error","Não foi possível iniciar a operação de backup.",active_backup=None)
            self.save(phase="falha",error="Não foi possível iniciar o trabalho.")
            raise

    def audit_backup_download(self, reference, actor):
        path = STATE / "backup-download-audit.json"
        rows = read(path, [])
        require(isinstance(rows, list), "O registro de auditoria está inválido.")
        now = int(time.time())
        recent = [row for row in rows[-100:] if isinstance(row,dict) and row.get("actor")==actor
                  and type(row.get("time")) is int and 0<=now-row["time"]<60]
        require(not recent, "Aguarde um minuto antes de baixar outro backup.")
        rows.append({"actor":actor,"time":now,"backup":reference})
        write(path, rows[-1000:])

    def dispatch(self,req,channel=None):
        require(isinstance(req,dict),"Mensagem inválida.")
        op=req.get("op")
        fields={"activity":set(),"backups":set(),"backup":{"actor","confirm"},"debug":set(),"notice":set(),"status":set(),"check":{"actor"},"settings":{"actor","hours","browser","email"},
                "download":{"actor","ref"},"upload":{"actor","ref","size"},"apply":{"actor","ref"},"rollback":{"actor","ref"}}
        fields.update(bregister={"actor","options","confirm"},bsave={"actor","options","confirm"},btest={"actor","ref"},bdelete={"actor","ref","confirm"},
                      bfetch={"actor","ref","typed","confirm"},
                      brestore={"actor","ref","confirm"},bprune={"actor","ref","confirm"},bretry={"actor","ref"})
        if op=="check" and "scope" in req:
            fields[op] = {"actor","scope"}
        if op=="backups" and "offset" in req:
            fields[op]={"offset"}
        if op=="settings" and "schedule" in req:
            fields[op] = {"actor","schedule","browser","email"}
        if op=="settings" and "backup_email" in req:
            fields[op].add("backup_email")
        require(isinstance(op,str) and op in fields and set(req)==fields[op]|{"op"},"Operação ou campos inválidos.")
        if op=="backups":
            offset=req.get("offset",0)
            require(type(offset) is int and offset>=0,"Página inválida.")
            return vault.public_state(STATE,offset)
        if op=="activity":
            return activity.snapshot(STATE, self.s, read(JOURNAL,{}), self.mail_task is not None)
        if op=="debug":
            return report(self.s,read(JOURNAL,{}),read(LAST,{}))
        if op=="notice":
            plan=self.s["plan"]
            return {"browser":self.s["browser"],"plan":{k:plan.get(k) for k in ("id","version","urgency")}}
        if op=="status":
            j=read(JOURNAL,{})
            last=read(LAST,{})
            queue=self.s.get("backup_queue",[])
            public={k:v for k,v in self.s.items() if k not in ("backup_queue","active_backup")}
            if self.mail_task is not None:
                public["mail_busy"]=True
            return {**public,"backup_mail_pending":len(queue),"backup_mail_failed":bool(self.s.get("backup_mail_failed")) or any(e.get("failed") for e in queue),"prepared":self.s.get("prepared",{}) if packages.matches(self.s.get("prepared",{}),self.s["plan"]) else {},"step":j.get("phase",""),"pending":bool(j),"restore":last.get("clone","") if last.get("result")=="aplicada" else "",
                    "health":read(STATE/"health.json",{}),"backup_time":last.get("time",0),"last_result":last.get("result","")}
        require(type(req.get("actor")) is int and req["actor"]>0,"Sessão administrativa necessária.")
        if op=="check":
            scope = req.get("scope", "same")
            require(scope in ("same", "next"), "Ramo inválido.")
            self.start("check", req["actor"], options=scope)
        elif op=="backup":
            require(req["confirm"] is True,"Confirme a manutenção para criar o backup.")
            self.start("backup",req["actor"])
        elif op in BACKUP_OPS:
            if op in ("bsave","bdelete","brestore","bprune","bregister"):
                require(req["confirm"] is True,"Confirmação necessária.")
            ref=req.get("ref","")
            if op in ("bfetch","bdelete","brestore","bretry"):
                require(isinstance(ref,str) and vault.KEY.fullmatch(ref),"Identificador de backup inválido.")
            if op=="bfetch":
                require(req.get("confirm") is True and req.get("typed")==ref,"Confirme o identificador completo para baixar esta cópia.")
                require(channel is not None,"Fluxo indisponível.")
                self.audit_backup_download(ref, req["actor"])
            self.start(op,req["actor"],ref,channel=channel,options=req.get("options"))
        elif op=="settings":
            require(all(type(req[k]) is bool for k in ("browser","email")), "Canais de notificação inválidos.")
            rule=req.get("schedule")
            hours=self.s["hours"]
            if "schedule" in req:
                schedule.validate(rule)
            else:
                hours=req["hours"]
                require(type(hours) is int and 1<=hours<=168, "O intervalo deve ser de 1 a 168 horas.")
            due=self.s["next"]
            if rule!=self.s.get("schedule") or hours!=self.s["hours"] or not due:
                due=schedule.next_check({"schedule":rule,"hours":hours},time.time())
            mode=req.get("backup_email",self.s["backup_email"])
            require(mode in backup_mail.MODES,"Preferência de e-mail de backups inválida.")
            queue=[e for e in self.s.get("backup_queue",[]) if backup_mail.accepts(mode,e["level"])]
            self.save(hours=hours,schedule=rule,browser=req["browser"],email=req["email"],next=due,
                      backup_email=mode,backup_queue=queue)
        else:
            require(op in ("download","upload","apply","rollback"),"Operação inválida.")
            ref=req.get("ref","")
            require(isinstance(ref,str) and ref.isascii() and len(ref)<=64,"Referência inválida.")
            self.start(op,req["actor"],ref,req.get("size"),channel)
        return {"accepted":True}

    def scheduled_backup(self,now):
        policy=read(STATE/vault.POLICY,vault.DEFAULT)
        if not policy["rule"] or now<policy["next"]:
            return False
        # Evitar iniciar manutenção fora da janela após longa indisponibilidade.
        new_due=schedule.next_check({"schedule":policy["rule"]},now)
        write(STATE/vault.POLICY,{**policy,"next":new_due})
        if now-policy["next"]>=600:
            self.backup_notice(secrets.token_hex(8),"missed","warning",
                               "A janela de manutenção expirou. O backup agendado não foi iniciado.")
            return False
        try:
            self.start("backup",policy["actor"],scheduled=True)
        except Exception:
            self.backup_notice(secrets.token_hex(8),"missed","error",
                               "O backup agendado não foi iniciado. Consulte o executor e o próximo horário.")
            return False
        return True

    def loop(self):
        sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        with sock:
            Path(SOCKET).unlink(missing_ok=True)
            sock.bind(SOCKET)
            os.chown(SOCKET,0,pwd.getpwnam(self.conf["web_user"]).pw_gid)
            os.chmod(SOCKET,0o660)
            sock.listen(8)
            sock.settimeout(1)
            while True:
                self.poll()
                if not self.process and self.mail_task is None and not JOURNAL.exists():
                    try:
                        now=time.time()
                        if not self.scheduled_backup(now) and now>=self.s["next"]:
                            self.start("check",0)
                    except OSError:
                        time.sleep(2)
                self.mail_tick(time.time())
                try: conn,_=sock.accept()
                except socket.timeout: continue
                with conn:
                    try:
                        pid,uid,gid=struct.unpack("3i",conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                        require(uid==self.uid,"Usuário Unix não autorizado.")
                        conn.settimeout(2)
                        with conn.makefile("rb") as f: line=f.readline(16385)
                        require(len(line)<=16384 and line.endswith(b"\n"),"Mensagem incompleta.")
                        req=json.loads(line)
                        result=self.dispatch(req,conn)
                        if req["op"]=="bfetch":
                            continue
                        reply={"ok":True,"data":result}
                    except Exception as e:
                        reply={"ok":False,"error":str(e)[:300]}
                    with contextlib.suppress(OSError):
                        conn.sendall((json.dumps(reply,ensure_ascii=False)+"\n").encode())


def prune_job_logs(directory, keep=200):
    files = sorted((path for path in directory.glob("job-????????????????.log")
                    if path.is_file() and not path.is_symlink()),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[max(0, keep-1):]:
        try:
            path.unlink()
        except OSError:
            continue


def consume_request(directory,job_id):
    require(isinstance(job_id,str) and re.fullmatch("[0-9a-f]{16}",job_id),"Identificador de trabalho inválido.")
    ticket=directory/("request-"+job_id+".json")
    j=read(ticket)
    require(j["id"]==job_id,"Pedido inconsistente.")
    ticket.unlink()
    from common import syncdir
    syncdir(directory)
    return j


def verify_admin(host,actor):
    require(type(actor) is int and actor>0,"Usuário inválido.")
    stmt=("SELECT COUNT(*) FROM glpi_users u WHERE u.is_active=1 AND u.is_deleted=0 "
          "AND (u.begin_date IS NULL OR u.begin_date<=NOW()) AND (u.end_date IS NULL OR u.end_date>=NOW()) "
          "AND EXISTS (SELECT 1 FROM glpi_profiles_users p WHERE p.users_id=u.id AND p.profiles_id=4) "
          f"AND u.id={actor}")
    with host.client(host.inspect()) as (sql,_,__):
        require(sql(stmt)=="1","A atribuição Super-Admin deixou de ser válida.")


def record_backup_failure(job,exc):
    path=STATE/"backup-attention.json"
    if read(path,{}).get("job")==job:
        return
    message="A operação de backup falhou. Consulte o log deste trabalho no servidor."
    if isinstance(exc,subprocess.CalledProcessError) and isinstance(exc.cmd,(list,tuple)) and exc.cmd:
        tool=Path(exc.cmd[0]).name
        if tool=="rclone":
            message="Falha de comunicação com o destino. Confira a conexão e as permissões."
        elif tool in ("tar","sha256sum"):
            message="Falha ao arquivar ou verificar os arquivos do backup."
        elif tool in ("mariadb","mysql","mariadb-dump","mysqldump"):
            message="Falha durante a operação no banco. Consulte o log do trabalho."
    elif isinstance(exc,OSError) and exc.errno in (errno.ENOSPC,errno.EDQUOT):
        message="Espaço ou cota de armazenamento insuficiente."
    elif isinstance(exc,ValueError):
        detail=str(exc).lower()
        if "espaço" in detail:
            message="Espaço insuficiente para concluir a operação de backup."
        elif "integridade" in detail or "manifesto" in detail:
            message="A verificação de integridade do backup falhou."
        elif "super-admin" in detail:
            message="A autorização Super-Admin deixou de ser válida."
    write(path,{"job":job,"level":"error","message":message})


def backup_job(host,j):
    op=j["op"]
    policy=read(STATE/vault.POLICY,vault.DEFAULT)
    if op=="bregister":
        try:
            vault.register_destination(host, j["id"], json.load(sys.stdin))
        except Exception as exc:
            from backup_destinations import RegistrationError
            reason = "O teste do destino falhou. Confira o caminho, as permissões, as credenciais e a instalação do rclone."
            if isinstance(exc, RegistrationError):
                reason = str(exc)
            write(STATE/"backup-attention.json", {"job":j["id"], "level":"error", "message":reason})
            raise ValueError(reason) from None
    elif op=="bsave":
        vault.configure(STATE,j["options"],j["actor"])
    elif op=="btest":
        vault.check_destination(vault.destinations()[j["ref"]],STATE)
    elif op=="backup":
        dest=policy["destination"]
        spec=vault.destinations()[dest]
        if spec["type"]=="local" and "target" in spec:
            path=Path(spec["target"])
            from backup_destinations import LOCAL_ROOT, overlap
            private_folder = "profile" in spec and STATE / LOCAL_ROOT in path.parents
            require(not any(overlap(path, p) for p in [host.app, *host.ext])
                    and (private_folder or not overlap(path, STATE)),
                    "Destino sobreposto à instalação.")
        vault.check_destination(spec,STATE)
        folder=security_backup(host,j["id"],dest,spec if dest!="local" else None)
        try:
            activity.record(STATE, j["id"], "publish")
            vault.publish(folder,dest)
        except Exception:
            write(STATE/"backup-attention.json",{"job":j["id"],"message":"Backup local criado. Confira o envio no catálogo e tente novamente se estiver pendente."})
            raise
        if policy["auto"]:
            try:
                if policy["actor"]!=j["actor"]:
                    verify_admin(host,policy["actor"])
                vault.prune(STATE,policy)
            except Exception:
                write(STATE/"backup-attention.json",{"job":j["id"],"message":"Backup verificado. A limpeza não terminou. Confira o catálogo e a prévia de retenção."})
                raise
    elif op=="bprune":
        keys,digest=vault.preview(vault.catalog(STATE),policy)
        require(j["ref"]==digest,"A lista de retenção mudou. Revise a prévia.")
        for key in keys:
            vault.remove(STATE,key)
    else:
        folder=vault.directory(STATE,j["ref"])
        if op=="bdelete":
            vault.remove(STATE,j["ref"])
        elif op=="bretry":
            row=read(folder/"resumo.json")
            require(row.get("pending"),"Não há envio pendente.")
            vault.publish(folder,row["destination"])
        elif op=="brestore":
            with vault.materialize(folder) as payload:
                restore_security_backup(host,payload,j["id"])
        elif op=="bfetch":
            with socket.socket(fileno=os.dup(0)) as channel:
                channel.settimeout(120)
                vault.stream_export(folder,channel)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("action",choices=["serve","job","recover","doctor"])
    p.add_argument("job_id",nargs="?")
    args=p.parse_args()
    action=args.action
    os.umask(0o077)
    require(os.geteuid()==0,"Execute com a identidade do serviço.")
    trusted(CONF,False)
    conf=read(CONF)
    host=Host(conf)
    if action=="serve":
        Agent(conf).loop()
        return
    with (STATE/"operation.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if action=="recover":
            recover(host)
        elif action=="doctor":
            info=host.inspect()
            host.layout(info)
            host.console("system:check_requirements")
            host.console("db:check")
            for unit in host.units: host.service("active",unit)
            import health
            report=health.inspect(host)
            print(f"Health Check #{report['id']}: {report['score']}/100")
            print(health.http_probe(host.c["status_url"]))
            print("Verificações locais concluídas.")
        else:
            require(isinstance(args.job_id,str) and re.fullmatch("[0-9a-f]{16}",args.job_id),"Identificador de trabalho inválido.")
            j=consume_request(STATE,args.job_id)
            if j["op"] in BACKUP_OPS:
                try:
                    verify_admin(host,j["actor"])
                    backup_job(host,j)
                except Exception as exc:
                    record_backup_failure(j["id"],exc)
                    raise
                return
            if j["op"] in ("apply","rollback","upload","download") or (j["op"]=="check" and j.get("scope")=="next"):
                verify_admin(host,j["actor"])
            if j["op"]=="check":
                plan=catalog(host.inspect()["version"], scope=j.get("scope","same"))
                write(STATE/"catalog.json",plan)
                if plan["id"] and j.get("notify", True) and read(STORE,{**BASE})["email"]:
                    # Uma falha de SMTP não deve apagar o aviso do painel.
                    try:
                        from common import run
                        run([host.php,"bin/console","--no-interaction","plugins:secureupdate:notify",plan["version"]],**host.web)
                    except subprocess.CalledProcessError:
                        write(STATE/"mail.json",{"error":"Falha no e-mail. Nova tentativa na próxima consulta."})
                    else:
                        write(STATE/"mail.json",{"error":""})
                else:
                    write(STATE/"mail.json",{"error":""})
            elif j["op"] in ("download","upload"):
                require(host.inspect()["version"]==j["plan"]["source"],"A versão instalada mudou.")
                try:
                    packages.prepare(STATE,j)
                except ValueError as exc:
                    write(STATE/"package-error.json",{"id":j["id"],"message":str(exc)})
                    raise
            elif j["op"]=="apply":
                receipt=read(STATE/"verified.json")
                require(packages.matches(receipt,j["plan"]) and receipt["id"]==j["ref"] and receipt["actor"]==j["actor"],
                        "A aprovação não corresponde ao pacote verificado.")
                archive=STATE/"verified.tgz"
                try:
                    create_job(host,{**j["plan"],"actor":j["actor"],"job":j["id"]},archive)
                finally:
                    archive.unlink(missing_ok=True)
                    (STATE/"verified.json").unlink(missing_ok=True)
            elif j["op"]=="rollback":
                manual(host,j["ref"],j["id"])
            else: raise ValueError("Operação desconhecida.")


if __name__=="__main__":
    main()
