"""Política, transporte real e limites das operações de backup."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import agent
import common
import vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(vault, "CONF", self.root / "config.json").start()
        patch("engine.trusted").start()  # Fixtures belong to the unprivileged test account.
        self.target = self.root / "storage"
        self.target.mkdir(mode=0o700)
        self.spec = {"type":"nas", "label":"NAS de teste", "target":"fixture:backups"}
        common.write(self.root / "destinations.json", {"nas":self.spec})
        config = self.root / "rclone.conf"
        config.write_text(f"[fixture]\ntype = alias\nremote = {self.target}\n")
        config.chmod(0o600)

    def fixture(self, number=1):
        key = f"20261120T120000Z-{number:016x}"
        d = self.root / "backups" / "manuais" / key
        d.mkdir(parents=True)
        for name in vault.FILES[:2]:
            (d/name).write_bytes(name.encode()*40)
        checksums = [f"{hashlib.sha256((d/name).read_bytes()).hexdigest()}  {name}\n" for name in vault.FILES[:2]]
        (d/"SHA256SUMS").write_text("".join(checksums))
        common.write(d/"resumo.json", {"time":number, "size":1000, "version":"12.0.1"})
        return d

    def test_retention_protects_newest_recent_other_destination_and_incomplete(self):
        rows = [dict(id=str(i), time=t, destination="local") for i,t in enumerate([0,1,990000,999999])]
        rows.extend([{**rows[0],"id":"a","pending":True}, {**rows[0],"id":"b","deleting":True},
                     {**rows[0],"id":"c","destination":"nas"}])
        self.assertEqual(vault.candidates(rows, {**vault.DEFAULT,"keep":1,"days":1},1000000),["1","0"])
        self.assertEqual(vault.candidates(rows, {**vault.DEFAULT,"keep":1,"days":0},1000000),["2","1","0"])

    def test_catalog_does_not_include_update_or_restore_points(self):
        valid = self.fixture()
        for root in ("restore-points","backups/update"):
            (self.root/root/valid.name).mkdir(parents=True)
        view = vault.public_state(self.root)
        self.assertEqual([r["id"] for r in view["items"]],[valid.name])
        self.assertNotIn("target",str(view["destinations"]))
        for invalid in ("../"+valid.name,valid.name+"/","other"):
            with self.assertRaises(ValueError):
                vault.directory(self.root,invalid)

    def test_schedule_one_occurrence_and_skip_missed_maintenance(self):
        patch.object(agent,"STATE",self.root).start()
        rule = {"kind":"weekly","weekday":1,"at":"02:00","zone":"UTC"}
        policy = {**vault.DEFAULT,"rule":rule,"actor":42,"next":1000}
        patch.object(agent,"STORE",self.root/"panel.json").start()
        controller = agent.Agent.__new__(agent.Agent)
        controller.s=dict(agent.BASE)
        with patch.object(controller,"start") as start:
            for delay, expected in [(25,True),(600,False)]:
                common.write(self.root/vault.POLICY,policy)
                self.assertEqual(controller.scheduled_backup(1000+delay),expected)
                self.assertFalse(controller.scheduled_backup(1001+delay))
            start.assert_called_once_with("backup",42,scheduled=True)

    def test_tampered_export_fails_before_sending_bytes(self):
        d = self.fixture()
        (d/"files.tar").write_bytes(b"alterado")
        with self.assertRaisesRegex(ValueError,"Integridade"):
            vault.stream_export(d,None)

    def test_invalid_policy_does_not_write(self):
        policy = dict(destination="local",keep=0,days=10,auto=False,rule=None)
        with self.assertRaises(ValueError):
            vault.configure(self.root,policy,42)
        self.assertFalse((self.root/vault.POLICY).exists())

    def test_failed_transfer_keeps_local_payload(self):
        d = self.fixture()
        with patch.object(vault,"transport",side_effect=OSError("disconnected")), self.assertRaises(OSError):
            vault.publish(d,"nas")
        self.assertTrue(common.read(d/"resumo.json")["pending"])
        with vault.materialize(d) as copy:
            self.assertEqual(copy,d)

    @unittest.skipUnless(shutil.which("rclone"),"rclone necessário para o teste de transporte")
    def test_real_rclone_export_and_scoped_delete(self):
        d = self.fixture()
        vault.check_destination(self.spec,self.root)
        vault.publish(d,"nas")
        self.assertFalse((d/"database.sql").exists())
        remote_summary = json.loads((self.target/"backups"/d.name/"resumo.json").read_text())
        self.assertFalse(remote_summary["pending"])
        out = io.BytesIO()
        class Sink:
            sendall = out.write
        vault.stream_export(d,Sink())
        header, data = out.getvalue().split(b"\n",1)
        self.assertEqual(json.loads(header)["size"],len(data))
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            self.assertEqual(set(archive.getnames()),set(vault.FILES))
            self.assertEqual(archive.extractfile("database.sql").read(),b"database.sql"*40)
        objects = self.target/"backups"/d.name
        (objects/"outro").touch()
        with self.assertRaisesRegex(ValueError,"desconhecidos"):
            vault.remove(self.root,d.name)
        self.assertTrue((objects/"files.tar").is_file())
        (objects/"outro").unlink()
        (objects/"files.tar").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError,"Integridade"), vault.materialize(d):
            pass
        vault.remove(self.root,d.name)
        self.assertFalse(d.exists())
        self.assertFalse(objects.exists())

    def test_changed_pruning_preview_removes_nothing(self):
        first=self.fixture(1)
        self.fixture(2)
        policy={**vault.DEFAULT,"keep":1,"days":0}
        common.write(self.root/vault.POLICY,policy)
        _,digest=vault.preview(vault.catalog(self.root),policy)
        self.fixture(3)
        with patch.object(agent,"STATE",self.root), self.assertRaisesRegex(ValueError,"lista"):
            agent.backup_job(None,{"op":"bprune","ref":digest})
        self.assertTrue(first.exists())
        self.assertEqual(len(vault.catalog(self.root)),3)

    def test_incomplete_delete_blocks_reuse_but_can_be_resumed(self):
        d=self.fixture()
        common.write(d/"resumo.json",{**common.read(d/"resumo.json"),"deleting":True})
        with self.assertRaisesRegex(ValueError,"exclusão pendente"), vault.materialize(d):
            pass
        vault.remove(self.root,d.name)
        self.assertFalse(d.exists())

    def test_builtin_local_destination_without_rclone(self):
        with patch.object(vault,"transport",side_effect=AssertionError("rclone")):
            vault.check_destination(vault.LOCAL,self.root)
            d=self.fixture()
            vault.publish(d,"local")
            with vault.materialize(d) as copy:
                self.assertEqual(copy,d)

    def test_changed_target_cannot_read_or_delete_old_backup(self):
        d=self.fixture()
        common.write(d/"resumo.json",{**common.read(d/"resumo.json"),"destination":"nas",
                     "storage":{**self.spec,"target":"fixture:outro"}})
        with self.assertRaisesRegex(ValueError,"original"), vault.materialize(d):
            pass
        with self.assertRaisesRegex(ValueError,"original"):
            vault.remove(self.root,d.name)
        self.assertTrue(d.is_dir())

    def test_backup_download_requires_confirmation_is_audited_and_rate_limited(self):
        patch.multiple(agent, STATE=self.root, STORE=self.root/"panel.json",
                       JOURNAL=self.root/"transaction.json", LAST=self.root/"last.json").start()
        controller = agent.Agent({"web_user": __import__("pwd").getpwuid(__import__("os").getuid()).pw_name})
        ref = "20261120T120000Z-" + "a"*16
        request = {"op":"bfetch","actor":9,"ref":ref}
        with self.assertRaisesRegex(ValueError, "Confirme"):
            controller.dispatch({**request,"confirm":False,"typed":ref}, object())
        with self.assertRaisesRegex(ValueError, "Confirme"):
            controller.dispatch({**request,"confirm":True,"typed":"different"}, object())
        with patch.object(agent.time,"time",return_value=1000):
            controller.audit_backup_download(ref,9)
            audit = common.read(self.root/"backup-download-audit.json")
            self.assertEqual(audit,[{"actor":9,"time":1000,"backup":ref}])
            with self.assertRaisesRegex(ValueError,"um minuto"):
                controller.audit_backup_download(ref,9)
            controller.audit_backup_download(ref,10)

    def test_backup_download_completion_never_emails_or_leaves_stale_active_job(self):
        controller = agent.Agent.__new__(agent.Agent)
        controller.process = type("Done",(),{"poll":lambda self:0,"returncode":0})()
        controller.running_job={"id":"a"*16,"op":"bfetch"}
        controller.s=dict(agent.BASE,active_backup=None)
        controller.mail_task=None
        controller.backup_notice=Mock()
        with patch.object(agent,"STATE",self.root), patch.object(agent,"STORE",self.root/"panel.json"):
            controller.poll()
        controller.backup_notice.assert_not_called()
        self.assertIsNone(controller.s["active_backup"])
        self.assertEqual(controller.s["phase"],"concluída")

    def test_remote_download_preflights_materialization_and_archive_space_together(self):
        d=self.fixture()
        row={"time":1,"size":100*1024**2,"version":"12.0.1","destination":"nas","storage":self.spec}
        common.write(d/"resumo.json",row)
        with patch.object(vault.shutil,"disk_usage",return_value=SimpleNamespace(free=150*1024**2)), \
             patch.object(vault,"materialize") as materialize:
            with self.assertRaisesRegex(ValueError,"remoto"):
                vault.stream_export(d,None)
            materialize.assert_not_called()

    def test_renaming_destination_does_not_break_existing_backup(self):
        d=self.fixture()
        common.write(d/"resumo.json",{**common.read(d/"resumo.json"),"destination":"nas","storage":self.spec})
        common.write(self.root/"destinations.json", {"nas":{**self.spec,"label":"NAS renomeado"}})
        self.assertEqual(vault.recorded_spec(common.read(d/"resumo.json"))["target"],self.spec["target"])

    def test_confidential_rclone_config(self):
        (self.root/"rclone.conf").chmod(0o644)
        with self.assertRaisesRegex(ValueError,"0600"):
            vault.transport(self.spec,"version")
