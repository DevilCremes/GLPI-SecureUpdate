"""Cadastro, seleção e uso dos destinos oferecidos no painel de backups."""
import configparser
import hashlib
import json
import os
import pwd
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
import agent
import backup_destinations as profiles
import common
import vault

JOB = "44" * 8
ALIAS = "dest-" + JOB


class BackupDestinationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.host = SimpleNamespace(app=self.root / "glpi", ext=[])
        self.local = dict(kind="local", base="local", label="Diários", folder="equipe/produção")
        self.cloud = dict(kind="cloud", label="S3", endpoint="https://s3.example.test:9443",
                          region="sa-east-1", bucket="test-backups", folder="glpi/daily",
                          access_key="fixture-id", secret_key="fixture/secret+key")
        self.nas = dict(kind="nas", label="Filial", server="nas.example.test", share="TI Backup",
                        folder="GLPI 11/diario", user="backup", password="teste&<'>", domain="EMPRESA")
        self.addCleanup(patch.stopall)
        patch.object(vault, "CONF", self.root / "config.json").start()
        patch.object(vault, "STATE", self.state).start()
        patch.object(agent, "STATE", self.state).start()
        patch.object(agent, "STORE", self.state / "panel.json").start()
        # Keep the real symlink check. Fixture ownership differs from the installed service.
        def physical(path, directory=True):
            common.require(path == path.resolve() and (path.is_dir() if directory else path.is_file()),
                           "Caminho físico necessário.")
        patch.object(profiles, "trusted", side_effect=physical).start()
        patch("engine.trusted", side_effect=physical).start()

    def register(self, opts):
        vault.register_destination(self.host, JOB, opts)

    @unittest.skipUnless(shutil.which("rclone"), "Transporte rclone necessário")
    def test_local_registration_backup_restore_data_and_removal(self):
        self.register(self.local)
        spec = vault.destinations()[ALIAS]
        folder = self.state / profiles.LOCAL_ROOT / "equipe" / "produção"
        self.assertEqual(spec["target"], str(folder))
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        policy = vault.public_state(self.state)["policy"]
        self.assertEqual(policy["destination"], "local")
        vault.configure(self.state, {k: (ALIAS if k == "destination" else policy[k])
                        for k in ("rule","destination","keep","days","auto")}, 7)
        key = "20261123T110000Z-" + JOB
        d = self.state / "backups" / "manuais" / key
        d.mkdir(parents=True)
        for name in vault.FILES[:2]:
            (d / name).write_bytes(name.encode())
        (d / "SHA256SUMS").write_text("".join(hashlib.sha256(name.encode()).hexdigest() + "  " + name + "\n"
                                              for name in vault.FILES[:2]))
        common.write(d / "resumo.json", {"time":1,"size":99,"version":"11.0.1"})
        with patch.object(agent, "security_backup", return_value=d) as backup:
            agent.backup_job(self.host, {"op":"backup", "id":JOB, "actor":7})
        backup.assert_called_once_with(self.host, JOB, ALIAS, spec)
        self.assertTrue((folder / key / "database.sql").exists())
        with vault.materialize(d) as restored:
            self.assertEqual((restored / "database.sql").read_bytes(), b"database.sql")
        vault.remove(self.state, key)
        self.assertFalse((folder / key).exists())

    def test_custom_disk_base_legacy_and_duplicate_names(self):
        disk = self.root / "disco"
        disk.mkdir(mode=0o700)
        common.write(self.root / "destinations.json", {"disco":{"type":"local","target":str(disk),"label":"Disco"}})
        with patch.object(vault, "check_destination"):
            self.register({**self.local,"base":"disco"})
            with self.assertRaisesRegex(ValueError, "exclusivo"):
                vault.register_destination(self.host, "55"*8, {**self.local, "label":"DIÁRIOS"})
        view = vault.public_state(self.state)
        self.assertEqual({b["id"] for b in view["bases"]}, {"local", "disco"})
        self.assertEqual({d["id"] for d in view["destinations"]}, {"local", "disco", ALIAS})
        self.assertTrue((disk / self.local["folder"]).is_dir())

    def test_local_confinement_and_failed_registration(self):
        for opts in ({"folder":"../etc"}, {"folder":"/etc"}, {"folder":"a//b"}, {"base":"/etc"},
                     {"folder":"a\nb"}, {"folder":""}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.register({**self.local, **opts})
        root = self.state / profiles.LOCAL_ROOT
        root.mkdir(mode=0o700, exist_ok=True)
        (root / "link").symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.register({**self.local,"folder":"link/new"})
        self.assertFalse((self.root / "new").exists())
        self.host.ext = [root]
        with self.assertRaisesRegex(ValueError, "sobrepõe"):
            self.register(self.local)
        self.assertEqual(vault.destinations(), {"local":vault.LOCAL})

    def test_cloud_profile_private_and_used_for_transport(self):
        def probe(spec, state, profile_path):
            self.assertEqual(profile_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(ALIAS, vault.destinations())
            vault.transport(spec, "version", profile_path=profile_path)
            self.assertIn(str(profile_path), execute.call_args.args[0])
        with patch.object(vault, "run") as execute, patch.object(vault, "check_destination", side_effect=probe):
            self.register(self.cloud)
            spec = vault.destinations()[ALIAS]
            vault.transport(spec, "version")
            cfg = self.state / profiles.DIRECTORY / ALIAS / "rclone.conf"
            self.assertIn(str(cfg), execute.call_args.args[0])
        parser = configparser.ConfigParser()
        parser.read(cfg)
        self.assertEqual(parser["vault"]["secret_access_key"], self.cloud["secret_key"])
        catalog = json.dumps(vault.public_state(self.state))
        self.assertIn("glpi/daily", catalog)
        for key in ("secret_key","access_key"):
            self.assertNotIn(self.cloud[key], catalog)
            self.assertNotIn(self.cloud[key], json.dumps(spec))
        self.assertEqual(spec["target"], "vault:test-backups/glpi/daily")
        vault.recorded_spec({"storage":spec,"destination":ALIAS})

    @unittest.skipUnless(shutil.which("rclone"), "rclone necessário para obscure")
    def test_smb_obscures_password_and_saves_share_and_folder(self):
        with patch.object(vault, "check_destination"):
            self.register(self.nas)
        cfg = self.state / profiles.DIRECTORY / ALIAS / "rclone.conf"
        parser = configparser.ConfigParser()
        parser.read(cfg)
        self.assertTrue(parser["vault"]["pass"])
        self.assertNotIn(self.nas["password"], cfg.read_text())
        self.assertEqual(parser["vault"]["type"], "smb")
        self.assertEqual(vault.destinations()[ALIAS]["target"], "vault:TI Backup/GLPI 11/diario")

    def test_rejected_remote_inputs_and_failed_test_do_not_publish(self):
        for opts in ({"endpoint":"http://host"}, {"endpoint":"https://user@host"},
                     {"endpoint":"https://host/?x=y"}, {"bucket":"foo:bar"},
                     {"folder":"../other"}, {"secret_key":"value\ntype = alias"}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.register({**self.cloud, **opts})
        with patch.object(vault, "check_destination", side_effect=OSError), self.assertRaises(OSError):
            self.register(self.cloud)
        self.assertEqual(list((self.state / profiles.DIRECTORY).iterdir()), [])
        self.assertNotIn(ALIAS, vault.destinations())

    def test_registration_job_keeps_credentials_out_of_ticket_and_log(self):
        controller = agent.Agent({"web_user":pwd.getpwuid(os.getuid()).pw_name})
        req = dict(op="bregister", actor=7, confirm=True, options=self.cloud)
        for changes in ({"actor":0},{"confirm":False}):
            with self.assertRaises(ValueError):
                controller.dispatch({**req, **changes})
        def spawn(*args, stdin, **kwargs):
            self.assertEqual(json.load(stdin), self.cloud)
            return Mock()
        with patch.object(agent.subprocess, "Popen", side_effect=spawn):
            controller.dispatch(req)
        self.assertNotIn("options", controller.running_job)
        for path in self.state.iterdir():
            self.assertNotIn(self.cloud["secret_key"], path.read_text())
