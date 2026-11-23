import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import detectar_instalacao as detect
import instalar


class Detection(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.etc = self.base / "etc"
        self.app = self.base / "helpdesk"
        for marker in ("bin/console","vendor/autoload.php","inc/based_config.php"):
            self.put(self.app / marker)
        self.site = self.etc / "nginx/sites-enabled/helpdesk"
        self.put(self.site, f"root {self.app}/public;\nfastcgi_pass unix:/run/php/helpdesk.sock;\n")

    @staticmethod
    def put(path, text=""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_nginx_site_locates_real_installation(self):
        self.assertIn(self.app, detect.installations(self.etc))
        (self.app/"vendor/autoload.php").unlink()
        self.assertNotIn(self.app, detect.installations(self.etc))

    def test_socket_connects_vhost_to_renamed_fpm_pool(self):
        p = self.etc/"php/8.4/fpm/pool.d/pools.conf"
        self.put(p, "[other]\nuser = another\nlisten = /run/php/other.sock\n"
                    "[tickets]\nuser = tickets-web\nlisten = /run/php/helpdesk.sock\n")
        self.assertEqual(detect.pool_candidates(self.app, self.etc), [("tickets-web","8.4")])
        self.site.write_text(self.site.read_text() + "\nroot /srv/unrelated/public;\n")
        self.assertEqual(detect.pool_candidates(self.app, self.etc), [])

    def test_apache_account_requires_mod_php_and_matching_root(self):
        etc = self.etc/"apache2"
        self.put(etc/"envvars", "export APACHE_RUN_USER=apache-web\n")
        self.put(etc/"sites-enabled/desk", f'DocumentRoot "{self.app}/public"\n')
        self.assertIsNone(detect.apache_account(self.app,etc))
        self.put(etc/"mods-enabled/php8.4.load")
        self.assertEqual(detect.apache_account(self.app,etc),"apache-web")

    def test_ambiguous_unattended_layout_requires_override(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(SystemExit, "--glpi"):
                instalar.choose(None, "--glpi", "Raiz", ["/srv/a","/srv/b"])
            self.assertEqual(instalar.choose("/srv/b","--glpi","Raiz",["/srv/a"]),"/srv/b")

    def test_only_conventional_external_roots_inferred(self):
        paths = [self.app/"config", "/var/lib/glpi/_sessions", "/etc/glpi", "/var/lib/glpi/_log"]
        self.assertEqual(detect.external_roots(self.app, paths, []), ["/etc/glpi","/var/lib/glpi"])
        with self.assertRaisesRegex(ValueError,"--externo"):
            detect.external_roots(self.app, ["/srv/private/data"], [])
        self.assertEqual(detect.external_roots(self.app, ["/srv/desk/data"], ["/srv/desk"]), ["/srv/desk"])

    def test_trigger_workers_and_web_in_stop_order(self):
        units = ["php-fpm-helpdesk.service","helpdesk-cron.timer","ssh.service","other-cron.timer"]
        args = argparse.Namespace(parar=None, detectar=False, aceitar_unidades=True)
        with patch.object(detect,"loaded_units",return_value=units), patch.object(subprocess,"check_output",return_value="helpdesk-jobs.service"):
            self.assertEqual(instalar.maintenance_units(self.app,args,None,[]),
                             ["helpdesk-cron.timer","helpdesk-jobs.service","php-fpm-helpdesk.service"])

    def test_background_timer_alone_cannot_approve_unattended_install(self):
        args = argparse.Namespace(parar=None, detectar=False, aceitar_unidades=True)
        with patch.object(detect,"loaded_units",return_value=["helpdesk-cron.service"]), patch("sys.stdin.isatty",return_value=False):
            with self.assertRaisesRegex(SystemExit, "--parar"):
                instalar.maintenance_units(self.app,args,None,[])

    def test_missing_packages_require_opt_in_and_preview_never_installs(self):
        with patch("sys.stdin.isatty", return_value=False), patch.object(detect,"missing_dependencies",return_value=["rsync"]), patch.object(subprocess,"run") as run:
            instalar.require_tools(True, True)
            with self.assertRaisesRegex(SystemExit,"--instalar-dependencias"):
                instalar.require_tools(False,False)
            run.assert_not_called()
        with patch.object(detect,"missing_dependencies",side_effect=[["rsync"],[]]), patch("shutil.which",return_value="apt-get"), patch.object(subprocess,"run") as run:
            instalar.require_tools(True, False)
            run.assert_called_once_with(["apt-get","install","-y","--no-install-recommends","rsync"],check=True)

    def test_probe_reports_only_needed_fields_and_checks_execution(self):
        data = {"version":"11.0.1","states":{},"url":"","dirs":[]}
        with patch.object(subprocess,"run") as run:
            run.return_value = Mock(returncode=0,stdout=json.dumps(data))
            self.assertEqual(detect.probe("php",{}),data)
            run.return_value.stdout = json.dumps({**data,"execution":"suspended_manually"})
            with self.assertRaisesRegex(ValueError,"suspensa"):
                detect.probe("php",{})

    def test_registration_retries_activation_and_verifies(self):
        config = {"php":"/usr/bin/php"}
        context = {"cwd":self.app,"user":999,"group":999,"extra_groups":[999]}
        marker = self.base/"registro-pendente"
        marker.touch()
        with patch.object(instalar,"CFG",self.base), patch.object(subprocess,"run") as run, patch.object(detect,"probe") as probe:
            data = {"states":{},"version":"10.0.28","admin":"glpi-admin"}
            probe.return_value = {"states":{"secureupdate":4}}
            admin = instalar.registration_user(data,None,config,context)
            run.side_effect = [None,subprocess.CalledProcessError(1,"activate")]
            with self.assertRaises(subprocess.CalledProcessError):
                instalar.register(config,data,context,admin)
            cli = [config["php"],"bin/console","--no-interaction","--no-ansi"]
            self.assertEqual(run.call_args_list[0],call(cli+["plugin:install","secureupdate","--username=glpi-admin"],check=True,**context))
            self.assertTrue(marker.exists())
            run.reset_mock(side_effect=True)
            probe.return_value = {"states":{"secureupdate":1}}
            instalar.register(config,{"states":{"secureupdate":4}},context,None)
            self.assertEqual(run.call_args_list[0], call(cli+["plugin:activate","secureupdate"],check=True,**context))
            self.assertFalse(marker.exists())

    def test_detection_entry_point_cannot_install_or_register(self):
        with patch("sys.argv", ["instalar.py","--detectar","--instalar-dependencias"]), patch("os.geteuid",return_value=0), patch("shutil.which",return_value="/usr/bin/systemctl"), patch.object(instalar,"read_settings",return_value=({},{"version":"11.0.1"},{})), patch.object(instalar,"install") as install, patch.object(instalar,"require_tools") as deps:
            instalar.main()
            deps.assert_called_once_with(False,True)
            install.assert_not_called()

    def test_first_install_and_activation_retry_through_entry_point(self):
        plugin = self.app/"plugins"
        plugin.mkdir()
        cfg, lib, unit = self.base/"configuration", self.base/"lib", self.base/"executor.service"
        settings = dict(app=str(self.app), php="/usr/bin/php", web_user="glpi-web",
                        units=["glpi-web.service"], external=[], status_url="https://lab/status.php")
        args = argparse.Namespace(glpi=None, admin_glpi=None, instalar_dependencias=False)
        states = {"version":"11.0.13","states":{}}
        context = {"cwd":self.app, "user": 1234, "group":1234}
        fail = True

        def command(cmd, **kwargs):
            nonlocal fail
            if "plugin:activate" in cmd and fail:
                fail = False
                raise subprocess.CalledProcessError(1, cmd)
            return Mock(returncode=0)

        with patch.multiple(instalar,CFG=cfg, LIB=lib, UNIT=unit), patch.object(instalar,"read_settings",return_value=(settings,states,context)) as read, patch.object(instalar,"require_tools"), patch.object(subprocess,"run",side_effect=command) as run, patch.object(instalar,"privileged_dir"), patch.object(instalar,"verified_php"), patch.object(instalar,"php_user"), patch.object(detect,"php_context",return_value=context), patch.object(detect,"probe",side_effect=[{"states":{"secureupdate":4}},{"version":"11.0.13","states":{"secureupdate":4}},{"states":{"secureupdate":1}}]):
            with self.assertRaises(subprocess.CalledProcessError):
                instalar.install(args)
            self.assertTrue((cfg/"registro-pendente").exists())
            self.assertEqual(json.loads((cfg/"config.json").read_text()),settings)
            self.assertTrue((plugin/"secureupdate/setup.php").exists())
            self.assertTrue((lib/"agent.py").exists())
            self.assertIn("ReadWritePaths="+str(self.app.parent), unit.read_text())
            instalar.install(args)
            self.assertFalse((cfg/"registro-pendente").exists())
            read.assert_called_once()
            self.assertEqual(sum("plugin:install" in c.args[0] for c in run.call_args_list),1)
            with self.assertRaisesRegex(SystemExit,"Já existe"):
                instalar.install(args)

    def test_glpi10_rejects_explicit_login_without_superadmin_profile(self):
        data = {"version":"10.0.28", "admin":None, "states":{}}
        with patch.object(detect,"probe",return_value=data) as probe:
            with self.assertRaisesRegex(SystemExit,"Super-Admin ativo"):
                instalar.registration_user(data,"tecnico",{"php":"/usr/bin/php"},{})
            probe.assert_called_once_with("/usr/bin/php",{}, "tecnico")

    def test_full_discovery_with_only_one_site_and_pool(self):
        self.put(self.etc/"php/8.4/fpm/pool.d/desk.conf",
                 "[desk]\nuser = tickets-web\nlisten = /run/php/helpdesk.sock\n")
        (self.app/"plugins").mkdir()
        data = {"version":"12.0.1","dirs":[str(self.app/"plugins"),"/var/lib/glpi/_cache"],
                "url":"https://tickets.example/glpi/","states":{"healthcheck":1}}
        a = argparse.Namespace(glpi=None, usuario_php=None, php=None, url_status=None, parar=None,
                               detectar=False, aceitar_unidades=True, externo=[])
        pools = detect.pool_candidates(self.app,self.etc)
        with patch.object(detect,"installations",return_value=[self.app]), patch.object(detect,"pool_candidates",return_value=pools), patch.object(detect,"apache_account",return_value=None), patch.object(instalar,"privileged_dir"), patch.object(instalar,"verified_php",side_effect=lambda p:p), patch.object(instalar,"php_user"), patch.object(instalar,"installation_permissions"), patch.object(detect,"php_context",return_value={}), patch.object(detect,"probe",return_value=data), patch.object(detect,"loaded_units",return_value={"php8.4-fpm.service"}), patch.object(subprocess,"check_output",return_value="loaded"), patch.object(Path,"is_dir",return_value=True):
            config, _, _ = instalar.read_settings(a)
        self.assertEqual(config, {"app":str(self.app),"web_user":"tickets-web","php":"/usr/bin/php8.4",
                                  "status_url":"https://tickets.example/glpi/status.php",
                                  "units":["php8.4-fpm.service"],"external":["/var/lib/glpi"]})
