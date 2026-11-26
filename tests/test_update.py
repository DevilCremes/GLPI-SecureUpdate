"""Testes isolados: rede, GLPI, MySQL e systemd são substituídos em cada transação."""
from contextlib import ExitStack, contextmanager, closing
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"service"))
import agent
import common
import engine
import health
import packages


def release(v, digest="0"*64, notes=""):
    return {"tag_name":v,"draft":False,"prerelease":False,"body":notes,"assets":[
        {"name":f"glpi-{v}.tgz","browser_download_url":f"{common.GITHUB}/releases/download/{v}/glpi-{v}.tgz","digest":"sha256:"+digest}]}


class Rules(unittest.TestCase):
    def test_branch_and_urgency(self):
        rows=[release("12.1.0"),release("12.0.9"),release("12.0.10"),release("12.0.8",notes="[SECURITY - High] corrigida"), release("12.0.11-rc1")]
        rows[1]["draft"]=True
        plan=common.catalog("12.0.7",lambda p:rows)
        self.assertEqual((plan["version"],plan["urgency"]),("12.0.10","Alta"))
        self.assertEqual(len(plan["notes"]),2)
    def test_no_update(self): self.assertEqual(common.catalog("12.0.2",lambda p:[release("12.0.1")])["id"],"")
    def test_digest(self):
        with self.assertRaises(ValueError): common.official(release("12.0.2","x"))
    def test_unknown_severity(self): self.assertEqual(common.catalog("12.0.1",lambda p:[release("12.0.2")])["urgency"],"Severidade não informada")
    def test_old_alert(self):
        self.assertEqual(health.compare({"checks":{"smtp":"critical"}},{"checks":{"smtp":"critical"}}),([],[]))
    def test_new_critical(self):
        self.assertEqual(health.compare({"checks":{"tickets":"ok"}},{"checks":{"tickets":"error"}}),(["tickets"],[]))

    def test_warning_regression_is_reported(self):
        self.assertEqual(health.compare({"checks":{"email":"ok"}},{"checks":{"email":"warning"}}),(["email"],[]))
    def test_health_improvement_and_deterioration(self):
        self.assertEqual(health.compare({"checks":{"a":"critical","b":"error"}},{"checks":{"a":"error","b":"critical"}}),(["b"],[]))
    def test_lost_coverage(self):
        self.assertEqual(health.compare({"checks":{"a":"ok","b":"ok"}},{"checks":{"a":"skipped"}}),([],["a","b"]))
    def test_archive_path(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)
            with tarfile.open(d/"a.tgz","w:gz") as f:
                f.addfile(tarfile.TarInfo("../outside"))
            with self.assertRaises(ValueError): engine.extract(d/"a.tgz",d)
    def test_archive_link(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)
            with tarfile.open(d/"a.tgz","w:gz") as f:
                m=tarfile.TarInfo("glpi/link");m.type=tarfile.SYMTYPE;m.linkname="../escape";f.addfile(m)
            with self.assertRaises(ValueError): engine.extract(d/"a.tgz",d)
    def test_restore_repoints_archived_config_database_name(self):
        with tempfile.TemporaryDirectory() as directory:
            archived = Path(directory) / "config_db.php"
            archived.write_text('<?php\n public $dbdefault = "gsu_old";\n')
            engine.switch_db_archived(archived, "gsu_" + "b"*16)
            self.assertIn('gsu_' + "b"*16, archived.read_text())
            self.assertNotIn("gsu_old", archived.read_text())

    def test_sql_config(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"config.php";p.write_text('<?php\n public $dbdefault = "glpi";\n')
            engine.switch_db(p,"glpi","gsu_"+"a"*16)
            self.assertIn("gsu_",p.read_text())
            with self.assertRaises(ValueError): engine.switch_db(p,"wrong","gsu_"+"b"*16)


class Security(unittest.TestCase):
    def test_database_options_are_sealed(self):
        import db
        import fcntl
        info=dict(host="localhost",db="glpi",user="fixture",password="fixture",ssl=False)
        with db.cliente_db(info,pwd.getpwuid(os.getuid()),True) as fd:
            self.assertTrue(fcntl.fcntl(fd,getattr(fcntl,"F_GET_SEALS",1034)) & getattr(fcntl,"F_SEAL_WRITE",8))
            with self.assertRaises(PermissionError): os.write(fd,b"changed")
            self.assertIn(b"[client]",os.read(fd,1024))

    def test_revoked_admin(self):
        @contextmanager
        def client(_):
            yield lambda stmt:"0",None,None
        with self.assertRaises(ValueError):
            agent.verify_admin(Mock(client=client),1)

    def test_unit_transition_refuses_snapshot(self):
        with patch.object(engine,"run",return_value=Mock(stdout="LoadState=loaded\nActiveState=activating\n")):
            with self.assertRaises(ValueError): engine.Host.service(None,"active","web.service")

    def test_dashboard_template_uses_twig_conditionals(self):
        text=(Path(__file__).resolve().parents[1]/"secureupdate/templates/panel.html.twig").read_text()
        self.assertNotIn("{% elif",text)
        for tab in ("saude","notificacoes","backups","debug"):
            self.assertIn("{% elseif tab == '"+tab+"'",text)

    def test_panel_inherits_host_theme(self):
        plugin = Path(__file__).resolve().parents[1] / "secureupdate"
        css = (plugin / "public/css/panel.css").read_text()
        template = (plugin / "templates/panel.html.twig").read_text()
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b")
        for token in ("--tblr-bg-surface", "--tblr-body-color", "--tblr-link-color", "--tblr-border-color"):
            self.assertIn(token, css)
        for classes in ("btn btn-primary", "form-check-input", "form-control", "alert alert-danger"):
            self.assertIn(classes, template)

    def test_archive_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with tarfile.open(root/"v.tgz","w:gz") as f:
                entry=tarfile.TarInfo("glpi/fixture");entry.size=4
                f.addfile(entry,io.BytesIO(b"test"))
            with patch.object(engine,"MAX_UNPACKED_BYTES",3),self.assertRaises(ValueError):
                engine.extract(root/"v.tgz",root)
            self.assertFalse((root/"glpi").exists())


class Control(unittest.TestCase):
    def setUp(self):
        self.d=Path(self.enter().enter_context(tempfile.TemporaryDirectory()))
        self.enter().enter_context(patch.multiple(agent,STATE=self.d,STORE=self.d/"s",JOURNAL=self.d/"j",LAST=self.d/"l"))
        self.a=agent.Agent({"web_user":pwd.getpwuid(os.getuid()).pw_name})
    def enter(self):
        if not hasattr(self,"stack"):
            self.stack=ExitStack();self.addCleanup(self.stack.close)
        return self.stack
    def test_backup_requires_explicit_confirmation(self):
        for value in (False, 1, "yes"):
            with self.assertRaises(ValueError):
                self.a.dispatch(dict(op="backup",actor=1,confirm=value))
        with patch.object(agent.subprocess,"Popen"):
            self.a.dispatch(dict(op="backup",actor=1,confirm=True))
        self.assertEqual(self.a.running_job["op"],"backup")
        self.assertTrue(self.a.dispatch({"op":"backups"})["items"]==[])

    def test_manual_branch_selection_invalidates_previous_receipt(self):
        self.a.s["plan"] = {"id":"old","source":"10.0.1"}
        self.a.s["prepared"] = {"id":"old-receipt"}
        with patch.object(agent.subprocess, "Popen"):
            self.a.dispatch({"op":"check", "actor":4, "scope":"next"})
        self.assertEqual(self.a.running_job["scope"], "next")
        self.assertFalse(self.a.s["prepared"])
        self.assertFalse(self.a.s["plan"])
        self.assertFalse(self.a.running_job["notify"])

    def test_scheduled_consultation_returns_to_maintenance(self):
        self.a.s["plan"] = {"scope":"next", "id":"manual-plan"}
        with patch.object(agent.subprocess, "Popen"):
            self.a.start("check", 0)
        self.assertEqual(self.a.running_job["scope"], "same")
        self.assertEqual(self.a.running_job["op"], "check")

    def test_no_request_install(self):
        with self.assertRaises(ValueError): self.a.dispatch({"op":"apply","actor":1,"ref":""})
    def test_stale(self):
        self.a.s["plan"]={"id":"a","checked":0}
        with self.assertRaises(ValueError): self.a.dispatch({"op":"apply","actor":1,"ref":"a"})
    def test_change_digest(self):
        self.a.s["plan"]={"id":"new","checked":time.time()}
        with self.assertRaises(ValueError): self.a.dispatch({"op":"apply","actor":1,"ref":"old"})
    def test_duplicate(self):
        self.a.process=object()
        with self.assertRaises(ValueError): self.a.dispatch({"op":"check","actor":1})
    def test_no_arguments_from_panel(self):
        with self.assertRaises(ValueError): self.a.dispatch({"op":"shell","actor":1})
    def test_no_actor(self):
        with self.assertRaises(ValueError): self.a.dispatch({"op":"apply","actor":0})
    def test_preferences(self):
        self.a.dispatch(dict(op="settings",actor=1,hours=3,browser=False,email=True))
        self.assertEqual(common.read(agent.STORE)["hours"],3)
        self.assertFalse(list(self.d.glob("request-*.json")))
    def test_invalid_interval(self):
        with self.assertRaises(ValueError): self.a.dispatch(dict(op="settings",actor=1,hours=0,browser=False,email=True))


    def test_ticket_single_use(self):
        job="a"*16
        common.write(self.d/("request-"+job+".json"),{"op":"check","id":job})
        self.assertEqual(agent.consume_request(self.d,job)["op"],"check")
        with self.assertRaises(FileNotFoundError): agent.consume_request(self.d,job)

    def test_failed_spawn_removes_ticket(self):
        with patch.object(agent.subprocess,"Popen",side_effect=OSError("fixture")),self.assertRaises(OSError):
            self.a.start("check",1)
        self.assertFalse(list(self.d.glob("request-*.json")))
        self.assertEqual(common.read(agent.STORE)["phase"],"falha")

    def test_future_approval(self):
        self.a.s["plan"]={"id":"a","checked":time.time()+600}
        with self.assertRaises(ValueError): self.a.dispatch(dict(op="apply",actor=1,ref="a"))

    def test_debug_disclosure(self):
        secret="private-fixture-value"
        self.a.s.update(error=secret,job=secret,plan={"notes":[secret],"checked":False})
        common.write(agent.JOURNAL,{"phase":secret,"db":secret})
        result=self.a.dispatch({"op":"debug"})
        self.assertNotIn(secret,str(result))
        self.assertEqual(result["Etapa da transação"],"Revisão necessária")
        self.assertEqual(result["Última consulta"],"Sem registro")
        with self.assertRaises(ValueError): self.a.dispatch({"op":"debug","file":"example"})

    def test_notice_minimizes_data(self):
        self.a.s["plan"]={"notes":["text"],"sha":"sha","version":"12.0.2"}
        self.assertEqual(set(self.a.dispatch({"op":"notice"})["plan"]),{"id","version","urgency"})

class Transaction(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.tmp=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.app=self.tmp/"www"/"glpi"; self.state=self.tmp/"state";self.state.mkdir()
        self.ext=[self.tmp/"glpi-docs"];self.ext[0].mkdir()
        (self.ext[0]/"ticket").write_text("original")
        self.units=["web.service","cron.timer"];self.active=set(self.units)
        self.c={"status_url":"https://example.invalid/status.php"}
        self.sqlversions={"glpi":"12.0.1"};self.modes={"glpi":"0"};self.fail="";self.opened=0;self.hc_targets=[]
        self.tree(self.app,"12.0.1")
        (self.app/"config/config_db.php").write_text("<?php\n public $dbdefault = 'glpi';\n")
        b=self.tmp/"build"/"glpi";self.tree(b,"12.0.2")
        self.archive=self.tmp/"official.tgz"
        with tarfile.open(self.archive,"w:gz") as tar:tar.add(b,arcname="glpi")
        tag=release("12.0.2",hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.plan={**common.official(tag),"source":"12.0.1","actor":9,"job":"test"}
        self.stack.enter_context(patch.multiple(engine,STATE=self.state,JOURNAL=self.state/"j",LAST=self.state/"l"))
        self.stack.enter_context(patch.object(health,"STATE",self.state))
        self.stack.enter_context(patch.object(engine,"api",return_value=tag))
        self.stack.enter_context(patch.object(engine,"trusted"))
        self.stack.enter_context(patch.object(engine.os,"sync"))
        self.stack.enter_context(patch.object(engine,"sync_tree",side_effect=self.copy))
        self.stack.enter_context(patch.object(engine,"run",side_effect=self.run_cmd))
        self.stack.enter_context(patch.object(health,"inspect",side_effect=self.hc))
        self.stack.enter_context(patch.object(health,"http_probe",side_effect=self.http))
    def tree(self,p,v):
        for f in ["bin/console","vendor/autoload.php","public/index.php","config/config_db.php","version"]:
            d=p/f;d.parent.mkdir(parents=True,exist_ok=True);d.write_text(v)
    def copy(self,source,dest):
        if dest.exists(): shutil.rmtree(dest)
        shutil.copytree(source,dest)
    def run_cmd(self,cmd,**kw):
        if cmd[0]=="curl":
            shutil.copyfile(self.archive,cmd[cmd.index("-o")+1])
            if self.fail=="digest": Path(cmd[cmd.index("-o")+1]).write_bytes(b"invalid")
            return
        return common.run(cmd,**kw)
    def layout(self,info): return [self.app/"config"]
    def inspect(self):
        db=re.search("dbdefault = '([^']+)'",(self.app/"config/config_db.php").read_text()).group(1)
        return dict(version=(self.app/"version").read_text(),cfg=str(self.app/"config"),db=db)
    def console(self,cmd,*args):
        db=self.inspect()["db"]
        if cmd=="maintenance:enable": self.modes[db]="1"
        if cmd=="maintenance:disable": self.modes[db]="0";self.opened+=1
        if cmd=="db:update":
            assert db!="glpi" and not self.active and self.modes[db]=="1"
            (self.ext[0]/"ticket").write_text("alterado pela migração")
            self.sqlversions[db]=self.inspect()["version"]
            if self.fail=="migration": raise ValueError("Falha simulada no banco")
    def service(self,op,unit):
        if op=="active": return unit in self.active
        if op=="start": self.active.add(unit)
        if op=="stop": self.active.discard(unit)
    def hc(self,host,target=None):
        self.hc_targets.append(target)
        version=self.inspect()["version"]
        return {"plugins":["secureupdate","healthcheck"],"checks":{"smtp":"warning","db":"critical" if self.fail=="health" and version=="12.0.2" else "ok"},"score":90,"id":1,"services":{"db":"OK"}}
    def http(self,url):
        if self.opened and self.fail=="http":raise ValueError("HTTP indisponível")
        return "OK"
    @contextmanager
    def client(self,info):
        db=info["db"]
        def sql(q):
            if "FROM glpi_plugins" in q: return "secureupdate\nhealthcheck"
            if "plugins_execution_mode" in q: return "on"
            if "maintenance_mode" in q: return self.modes[db]
            if "glpi_configs" in q: return self.sqlversions[db]
            return "0"
        def save(path):
            assert not self.active and self.modes[db]=="1"
            if self.fail=="backup":raise OSError("Sem espaço")
            path.write_text("SQL de teste")
        def clone(path,new):
            assert new not in self.sqlversions
            self.sqlversions[new]=self.sqlversions[db];self.modes[new]=self.modes[db]
        yield sql,save,clone
    def execute(self):engine.create_job(self,self.plan,self.archive)
    def original(self):
        self.assertEqual(self.inspect()["db"],"glpi")
        self.assertEqual((self.ext[0]/"ticket").read_text(),"original")
        self.assertEqual(self.active,set(self.units))
        self.assertFalse(engine.JOURNAL.exists())
    def configure_versions(self, source, target, scope):
        (self.app/"version").write_text(source)
        self.sqlversions["glpi"] = source
        tree = self.tmp/"build"/"glpi"
        (tree/"version").write_text(target)
        with tarfile.open(self.archive, "w:gz") as tar:
            tar.add(tree, arcname="glpi")
        tag = release(target, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        engine.api.return_value = tag
        self.plan = {**common.official(tag), "source":source, "scope":scope, "actor":9, "job":"test"}

    def check_transition(self, source, target, scope):
        self.configure_versions(source, target, scope)
        self.execute()
        self.assertEqual(self.inspect()["version"], target)
        self.assertEqual(self.sqlversions["glpi"], source)
        self.assertEqual(self.sqlversions[self.inspect()["db"]], target)
        self.assertEqual(self.active, set(self.units))
        engine.manual(self, self.inspect()["db"])
        self.original()
        self.assertEqual(self.inspect()["version"], source)

    def test_maintenance_10_and_rollback(self):
        self.check_transition("10.0.9", "10.0.10", "same")

    def test_maintenance_11_and_rollback(self):
        self.check_transition("11.0.8", "11.0.9", "same")

    def test_10_to_11_and_rollback(self):
        self.check_transition("10.0.28", "11.0.13", "next")

    def test_11_to_12_and_rollback(self):
        self.check_transition("11.0.13", "12.0.2", "next")

    def test_major_migration_failure_restores_source(self):
        self.configure_versions("10.0.28", "11.0.13", "next")
        self.fail = "migration"
        with self.assertRaisesRegex(ValueError, "Falha simulada"):
            self.execute()
        self.original()
        self.assertEqual(self.inspect()["version"], "10.0.28")

    def test_maintenance_inspects_plugins_against_the_target_version(self):
        self.execute()
        self.assertIn(self.plan["version"], self.hc_targets)

    def test_success(self):
        self.execute()
        self.assertTrue(self.inspect()["db"].startswith("gsu_"))
        self.assertEqual(self.sqlversions["glpi"],"12.0.1")
        self.assertEqual(self.active,set(self.units))
        self.assertEqual(common.read(engine.LAST)["result"],"aplicada")
    def test_uploaded_archive_is_used_for_approved_update_and_rollback(self):
        contents = self.archive.read_bytes()
        plan = {**self.plan, "id":"p", "checked":time.time()}
        asset = release(plan["version"], plan["sha"])
        asset["assets"][0]["size"] = len(contents)
        job = {"op":"upload", "id":"ab"*8, "actor":9, "size":len(contents), "plan":plan}
        with patch.object(packages,"api",return_value=asset), patch.object(packages,"uploaded",return_value=closing(io.BytesIO(contents))):
            receipt = packages.prepare(self.state, job)
        self.assertTrue(packages.matches(receipt,plan))
        self.assertEqual(self.active,set(self.units))
        self.assertEqual(self.opened,0)
        self.archive.unlink()
        engine.create_job(self,plan,self.state/"verified.tgz")
        self.assertEqual(self.inspect()["version"],"12.0.2")
        engine.manual(self,common.read(engine.LAST)["clone"])
        self.original()

    def test_bad_digest(self):
        self.archive.write_bytes(b"corrompido")
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.opened,0);self.original()
    def test_security_backup_without_update(self):
        engine.security_backup(self, "a"*16)
        rows=engine.recent_security_backups(self.state)
        self.assertEqual(len(rows),1)
        directory=Path(rows[0]["path"])
        self.assertEqual(rows[0]["version"],"12.0.1")
        self.assertTrue((directory/"database.sql").is_file())
        with tarfile.open(directory/"files.tar") as archive:
            self.assertEqual(archive.extractfile(str((self.ext[0]/"ticket").relative_to("/"))).read(),b"original")
        common.run(["sha256sum","--status","--check","SHA256SUMS"], cwd=directory)
        self.assertFalse(engine.LAST.exists())
        self.original()

    def test_restore_independent_backup_preserves_pre_restore_checkpoint(self):
        folder=engine.security_backup(self,"a"*16)
        ticket=self.ext[0]/"ticket"
        ticket.write_text("chamado posterior")
        engine.restore_security_backup(self,folder,"b"*16)
        self.assertEqual(ticket.read_text(),"original")
        self.assertTrue(self.inspect()["db"].startswith("gsu_"))
        self.assertEqual(common.read(engine.LAST)["result"],"restaurada")
        with tarfile.open(self.state/"restore-points"/("b"*16)/"files.tar") as archive:
            self.assertEqual(archive.extractfile(str(ticket.relative_to("/"))).read(),"chamado posterior".encode())
        self.assertEqual(self.active,set(self.units))
        self.assertFalse(engine.JOURNAL.exists())

    def test_failed_restore_health_recovers_pre_restore_ticket_and_database(self):
        folder=engine.security_backup(self,"a"*16)
        ticket=self.ext[0]/"ticket"
        ticket.write_text("chamado posterior")
        with patch.object(health,"assess",side_effect=ValueError("Falha de saúde")), self.assertRaisesRegex(ValueError,"saúde"):
            engine.restore_security_backup(self,folder,"b"*16)
        self.assertEqual(self.inspect()["db"],"glpi")
        self.assertEqual(ticket.read_text(),"chamado posterior")
        self.assertEqual(self.active,set(self.units))
        self.assertFalse(engine.JOURNAL.exists())

    def test_restore_public_failure_after_reopen_keeps_restored_data(self):
        folder=engine.security_backup(self,"a"*16)
        self.fail="http"
        engine.restore_security_backup(self,folder,"b"*16)
        self.assertEqual(common.read(engine.LAST)["result"],"restaurada")
        self.assertIn("Falha",common.read(self.state/"health.json")["Acesso público"])
        self.assertTrue(self.inspect()["db"].startswith("gsu_"))
        self.assertFalse(engine.JOURNAL.exists())

    def test_restore_rejects_wrong_version_before_maintenance(self):
        folder=engine.security_backup(self,"a"*16)
        common.write(folder/"resumo.json",{**common.read(folder/"resumo.json"),"version":"12.0.2"})
        before=self.opened
        with self.assertRaisesRegex(ValueError,"mesma versão"):
            engine.restore_security_backup(self,folder,"b"*16)
        self.assertEqual(self.opened,before)
        self.original()

    def test_security_backup_failure_no_partial_history(self):
        self.fail="backup"
        with self.assertRaises(OSError):
            engine.security_backup(self,"b"*16)
        self.assertEqual(engine.recent_security_backups(self.state),[])
        self.original()

    def test_security_backup_keeps_preexisting_maintenance_and_stopped_units(self):
        self.modes["glpi"]="1"
        self.active.discard("cron.timer")
        engine.security_backup(self,"c"*16)
        self.assertEqual(self.modes["glpi"],"1")
        self.assertEqual(self.active,{"web.service"})

    def test_recover_security_backup_after_interruption(self):
        self.active.clear()
        self.modes["glpi"]="1"
        j=dict(app=str(self.app),external=list(map(str,self.ext)),units=self.units,active=self.units,maintenance=False)
        engine.stage(j,"backup_manual")
        engine.recover(self)
        self.assertFalse(engine.LAST.exists())
        self.original()

    def test_backup_failure(self):
        self.fail="backup"
        with self.assertRaises(OSError):self.execute()
        self.original()
    def test_migration_failure(self):
        self.fail="migration"
        with self.assertRaises(ValueError):self.execute()
        self.original()
    def test_health_fail(self):
        self.fail="health"
        with self.assertRaises(ValueError):self.execute()
        self.original()
        self.assertEqual(common.read(engine.LAST)["result"],"revertida")
    def test_http_after_reopening(self):
        self.fail="http";self.execute()
        self.assertNotEqual(self.inspect()["db"],"glpi")
        self.assertIn("Falha",common.read(self.state/"health.json")["Acesso público"])
    def test_manual(self):
        self.execute()
        (self.ext[0]/"ticket").write_text("chamado novo")
        last=common.read(engine.LAST)
        engine.manual(self,last["clone"])
        self.original()
        with tarfile.open(next(Path(last["backup"]).glob("antes-de-reverter-*/files.tar"))) as f:
            self.assertEqual(f.extractfile(str((self.ext[0]/"ticket").relative_to("/"))).read(),b"chamado novo")
    def test_wrong_manual(self):
        self.execute()
        with self.assertRaises(ValueError):engine.manual(self,"old")
    def test_prior_maintenance(self):
        self.modes["glpi"]="1";self.execute()
        self.assertEqual(self.modes[self.inspect()["db"]],"1")
    def test_recovery_after_rename(self):
        self.execute()
        j=common.read(engine.LAST)
        self.active.clear(); engine.stage(j,"migrando")
        engine.recover(self);self.original()

    def test_unknown_recovery_phase(self):
        record=dict(app=str(self.app),external=list(map(str,self.ext)),units=self.units,phase="unknown")
        common.write(engine.JOURNAL,record)
        with self.assertRaises(ValueError): engine.recover(self)
        self.assertEqual(common.read(engine.JOURNAL),record)
        self.assertEqual(self.opened,0)
        self.assertEqual(self.active,set(self.units))

    def test_prior_rollback_point_survives_failed_attempt(self):
        previous={"result":"aplicada","clone":"gsu_"+"0"*16}
        common.write(engine.LAST,previous)
        self.fail="health"
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(common.read(engine.LAST),previous)
        self.assertEqual(common.read(self.state/"outcome.json")["result"],"revertida")

    def test_corrupt_external_backup_blocks_switch(self):
        self.execute()
        last=common.read(engine.LAST)
        with (Path(last["backup"])/"files.tar").open("ab") as f: f.write(b"broken")
        with self.assertRaises(engine.sp.CalledProcessError): engine.manual(self,last["clone"])
        self.assertEqual(self.inspect()["db"],last["clone"])
        self.assertEqual(self.active,set())
        self.assertEqual(common.read(engine.JOURNAL)["phase"],"revertendo")

    def test_layout_accepts_downstream_config_files(self):
        downstream=self.app/"inc/downstream.php"
        downstream.parent.mkdir();downstream.touch()
        dot=self.app/".env";dot.touch()
        roots=engine.Host.layout(self,{"dirs":[],"cfg":str(self.app/"config")})
        self.assertIn(downstream,roots)
        self.assertIn(dot,roots)

if __name__=="__main__":
    unittest.main()
