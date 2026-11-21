"""Diagnóstico de núcleo, disponibilidade via HTTP e regressões do Health Check."""
import json
import subprocess as sp
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
from common import STATE, require, run, write


def compare(before,after):
    old=before["checks"]
    new=after["checks"]
    severity={"error":1,"critical":2}
    lost=sorted(key for key in old if old[key]!="skipped" and new.get(key,"skipped")=="skipped")
    regressions=sorted(key for key,value in new.items() if severity.get(value,0)>severity.get(old.get(key),0))
    return regressions,lost


def inspect(host):
    def data(*commands):
        r=run([host.php,"bin/console","--no-interaction","--no-ansi",*commands],
              stdout=sp.PIPE,timeout=900,**host.web)
        return json.loads(r.stdout)
    native=data("system:status","--private")
    for svc in ("db","filesystem"):
        require(native.get(svc,{}).get("status")=="OK","Healthcheck nativo reprovado: "+svc)
    require(native.get("glpi",{}).get("database_version",{}).get("uptodate") is True,
            "O núcleo informa esquema de banco desatualizado.")
    report=data("plugins:secureupdate:health")
    require(type(report.get("checks")) is dict and report["checks"] and
            all(s in ("ok","info","warning","error","critical","skipped") for s in report["checks"].values()),
            "O diagnóstico completo retornou dados inválidos.")
    report["services"]={k:v.get("status","NO_DATA") for k,v in native.items() if isinstance(v,dict)}
    return report


def assess(host,baseline,folder):
    try:
        post=inspect(host)
        write(folder/"health-after.json",post)
        bad,lost=compare(baseline,post)
        native=sorted(k for k,v in post["services"].items() if v=="PROBLEM" and baseline["services"].get(k)!="PROBLEM" and k!="glpi")
        summary={"Núcleo":"Banco, esquema e filesystem verificados","GLPI Health Check":f"Diagnóstico #{post['id']} · pontuação {post['score']}/100",
                 "Regressões críticas":", ".join(bad+native) or "Nenhuma detectada",
                 "Cobertura perdida":", ".join(lost) or "Nenhuma",
                 "Alertas existentes":str(sum(v in ("warning","error","critical") for v in baseline["checks"].values())),
                 "Verificações ignoradas":str(list(post["checks"].values()).count("skipped"))}
        write(STATE/"health.json",summary)
        require(not bad and not lost and not native,"Regressão de saúde ou perda de cobertura. Consulte o relatório.")
    except Exception as e:
        from common import read
        summary=read(STATE/"health.json",{})
        summary["Diagnóstico antes da reabertura"]=str(e)[:350]
        write(STATE/"health.json",summary)
        raise


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None


def http_probe(url):
    u=urlsplit(url)
    require(u.scheme in ("http","https") and u.hostname and not u.username and not u.password and u.path.endswith("/status.php"), "Configure a URL real terminada em /status.php.")
    for attempt in range(3):
        try:
            with build_opener(NoRedirect).open(Request(url,headers={"Accept":"application/json","Cache-Control":"no-cache"}),timeout=8) as r:
                require(r.status==200,"HTTP não retornou 200.")
                raw=r.read(524289)
                require(len(raw)<=524288,"Resposta de status excede 512 KiB.")
                data=json.loads(raw)
                require(isinstance(data,dict),"Formato do endpoint de status inválido.")
            require(data.get("db",{}).get("status")=="OK" and data.get("filesystem",{}).get("status")=="OK","O endpoint público informou falha no banco ou filesystem.")
            return "HTTP 200, JSON do GLPI, banco e filesystem OK"
        except Exception:
            if attempt==2: raise
            time.sleep(2)
