"""Preferências, entrega local simulada e recuperação da fila de avisos."""
from concurrent.futures import Future
import fcntl
import json
import os
from pathlib import Path
import pwd
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"service"))
import agent
import backup_mail
import common


class BackupMailTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        patcher=patch.multiple(agent,STATE=self.root,STORE=self.root/"panel.json",
                              LAST=self.root/"last",JOURNAL=self.root/"journal")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conf={"web_user":pwd.getpwuid(os.getuid()).pw_name}
        self.control=self.reload()
        self.job="abcdef0123456789"

    def reload(self):
        control=agent.Agent(self.conf)
        self.addCleanup(control.mail_pool.shutdown)
        return control

    def preferences(self,mode):
        self.control.dispatch({"op":"settings","actor":4,"hours":24,"browser":True,"email":False,"backup_email":mode})

    def finish(self,op="backup",rc=0,job=None):
        self.control.process=Mock(returncode=rc)
        self.control.running_job={"op":op,"id":job or self.job}
        self.control.poll()

    def test_off_by_default_and_old_settings_keep_choice(self):
        self.finish()
        self.assertEqual(self.control.s["backup_queue"],[])
        self.preferences("all")
        self.control.dispatch({"op":"settings","actor":4,"hours":24,"browser":False,"email":False})
        self.assertEqual(self.reload().s["backup_email"],"all")
        with self.assertRaises(ValueError):
            self.preferences("random")
        self.assertEqual(self.control.s["backup_email"],"all")

    def test_filter_and_cancellation(self):
        self.preferences("all")
        self.finish()
        self.finish(rc=1,job="b"*16)
        self.preferences("issues")
        self.assertEqual([x["level"] for x in self.control.s["backup_queue"]],["error"])
        self.finish(job="c"*16)
        self.assertEqual(len(self.control.s["backup_queue"]),1)
        self.preferences("off")
        self.assertEqual(self.control.s["backup_queue"],[])

    def test_healthy_copy_and_retention_failure_is_warning(self):
        self.preferences("issues")
        common.write(self.root/"backup-attention.json",{"job":self.job,"message":"Backup verificado. A limpeza não terminou."})
        self.finish(rc=1)
        event,=self.control.s["backup_queue"]
        self.assertEqual(event["level"],"warning")
        self.assertIn("limpeza",event["message"])

    def test_restore_health_warning_and_missed_schedule(self):
        self.preferences("issues")
        common.write(self.root/"health.json",{"Acesso público":"Falha após reabrir"})
        self.finish("brestore")
        common.write(self.root/agent.vault.POLICY,{**agent.vault.DEFAULT,"rule":{"kind":"daily","at":"01:00","zone":"UTC"},"next":1,"actor":4})
        self.assertFalse(self.control.scheduled_backup(3600))
        self.assertEqual([i["level"] for i in self.control.s["backup_queue"]],["warning","warning"])

    def test_interrupted_operation_is_reported_once(self):
        self.preferences("issues")
        self.control.save(phase="criando backup",active_backup={"id":self.job,"op":"backup"})
        self.reload()
        item,=self.reload().s["backup_queue"]
        self.assertEqual(item["level"],"warning")
        self.assertIn("interrompido",item["message"])

    def test_safety_copy_status_independent_of_update(self):
        self.preferences("all")
        common.write(self.root/"checkpoint-notice.json",{"job":self.job,"op":"before_update","level":"success","message":"Cópia verificada."})
        self.finish("apply",1)
        event,=self.control.s["backup_queue"]
        self.assertEqual(event["level"],"success")
        self.assertEqual(self.control.s["phase"],"falha")

    def test_diagnostic_keeps_credentials_and_remote_paths_out_of_email(self):
        self.preferences("issues")
        agent.record_backup_failure(self.job,subprocess.CalledProcessError(1,["rclone","secret:private/path"]))
        self.finish(rc=1)
        item,=self.control.s["backup_queue"]
        self.assertEqual(item["level"],"error")
        self.assertIn("destino",item["message"])
        self.assertNotIn("secret",json.dumps(item))

    def test_scheduled_success_is_distinct_from_manual(self):
        self.preferences("all")
        self.control.process=Mock(returncode=0)
        self.control.running_job={"op":"backup","scheduled":True,"id":self.job}
        self.control.poll()
        self.assertEqual(self.control.s["backup_queue"][0]["operation"],"Backup agendado")

    def test_partial_delivery_persists_and_new_events_can_pass(self):
        self.preferences("all")
        self.finish()
        task=Future()
        task.set_result(("error",[7]))
        self.control.mail_task=task
        self.control.mail_id=self.job
        self.control.mail_tick(100)
        self.control=self.reload()
        pending,=self.control.s["backup_queue"]
        self.assertEqual((pending["acked"],pending["due"]),([7],1000))
        self.assertEqual(self.control.s["phase"],"concluída")
        self.assertNotIn("backup_queue",self.control.dispatch({"op":"status"}))
        self.assertTrue(self.control.dispatch({"op":"status"})["backup_mail_failed"])
        with patch.object(agent.time,"time",return_value=101):
            self.finish(job="c"*16)
        with patch.object(self.control.mail_pool,"submit",return_value=Future()) as submit:
            self.control.mail_tick(102)
            self.assertEqual(submit.call_args.args[-1]["id"],"c"*16)

    def test_acceptance_clears_queue_without_replacing_backup_result(self):
        self.preferences("all")
        self.finish()
        original=self.control.s["backup_result"]
        self.control.mail_id=self.job
        self.control.mail_task=Future()
        self.control.mail_task.set_result(("ok",[4]))
        self.control.mail_tick(101)
        restarted=self.reload()
        self.assertEqual(restarted.s["backup_queue"],[])
        self.assertEqual(restarted.s["backup_result"],original)

    def test_turning_off_does_not_requeue_inflight_failure(self):
        self.preferences("all")
        self.finish()
        self.control.mail_task=Future()
        self.control.mail_task.set_result(("error",[]))
        self.control.mail_id=self.job
        self.preferences("off")
        self.control.mail_tick(1)
        self.assertEqual(self.control.s["backup_queue"],[])

    def test_console_protocol_and_transport_failure(self):
        (self.root/"bin").mkdir()
        cli=self.root/"bin/console"
        event=backup_mail.event(self.job,"backup","success","Cópia verificada.",1)
        # Real subprocess with the same CLI arguments and pipes as the PHP sender.
        cli.write_text("""import json, sys
e=json.load(sys.stdin)
assert sys.argv[1:]==['--no-interaction','--no-ansi','plugins:secureupdate:notify','backup']
assert e['level']=='success'
if 7 not in e['acked']:
    print('SU_ACK 7')
    sys.exit(1)
print('SU_ACK 8')
print('SU_DONE')
""")
        host=Mock(php=sys.executable,web={"cwd":self.root})
        with patch.object(backup_mail,"Host",return_value=host):
            self.assertEqual(backup_mail.deliver(self.conf,self.root,event),("error",[7]))
            self.assertEqual(backup_mail.deliver(self.conf,self.root,{**event,"acked":[7]}),("ok",[8]))
            cli.write_text("print('GLPI indisponível')")
            self.assertEqual(backup_mail.deliver(self.conf,self.root,event),("error",[]))
            with (self.root/"operation.lock").open("a") as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                self.assertEqual(backup_mail.deliver(self.conf,self.root,event),("busy",[]))
