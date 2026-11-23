import unittest
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

import common
import engine


def release(version):
    filename = f"glpi-{version}.tgz"
    return {"tag_name": version, "draft": False, "prerelease": False,
            "assets": [{"name": filename, "digest": "sha256:" + "a"*64,
                        "browser_download_url": f"{common.GITHUB}/releases/download/{version}/{filename}"}]}


class Branches(unittest.TestCase):
    def setUp(self):
        self.rows = list(map(release, ("10.0.10", "11.0.1", "12.0.2", "10.0.9", "11.0.3", "13.0.0", "11.0.4-rc1")))

    def test_maintenance_uses_installed_branch(self):
        for major, expected in [(10, "10.0.10"), (11, "11.0.3"), (12, "12.0.2")]:
            with self.subTest(major=major):
                plan = common.catalog(f"{major}.0.0", lambda _: self.rows)
                self.assertEqual(plan["version"], expected)
                self.assertTrue(all(n["version"].startswith(f"{major}.0.") for n in plan["notes"]))

    def test_select_next_major(self):
        for current, target in [("10.0.9", "11.0.3"), ("11.0.3", "12.0.2")]:
            plan = common.catalog(current, lambda _: self.rows, scope="next")
            self.assertEqual((plan["source"], plan["version"], plan["scope"]), (current, target, "next"))
        with self.assertRaises(ValueError):
            common.catalog("12.0.1", lambda _: self.rows, scope="next")

    def test_installed_tag_before_available_major_on_second_page(self):
        pages = [[release("10.0.10")]*40, [release("11.0.1")]]
        plan = common.catalog("10.0.10", lambda _: pages.pop(0), scope="next")
        self.assertEqual(plan["version"], "11.0.1")

    def test_engine_rejects_invalid_or_unselected_transition_before_io(self):
        for source, target, scope in [("10.0.1","12.0.2","next"), ("10.0.1","11.0.3","same"),
                ("11.0.3","10.0.8","next"), ("10.0.5","10.0.4","same"), ("12.0.1","13.0.0","next")]:
            host = Mock()
            host.inspect.return_value = {"version":source}
            with self.subTest(source=source,target=target), patch.object(engine, "JOURNAL") as journal, patch.object(engine, "api") as api:
                journal.exists.return_value = False
                with self.assertRaises(ValueError):
                    engine.create_job(host, dict(source=source, version=target, scope=scope), None)
                api.assert_not_called()

    def test_only_declared_conventional_external_links_are_preserved(self):
        with TemporaryDirectory() as tmp, patch.object(engine, "trusted"):
            base = Path(tmp)
            host = engine.Host.__new__(engine.Host)
            host.app = base/"glpi"
            host.app.mkdir()
            host.ext = [base/"config-data", base/"files-data"]
            for name, target in zip(("config","files"), host.ext):
                target.mkdir()
                (host.app/name).symlink_to(target)
            info = {"dirs":list(map(str,host.ext)), "cfg":str(host.ext[0])}
            self.assertEqual(set(host.layout(info)), {host.app/"config",host.app/"files"})
            (host.app/"plugins").symlink_to(host.ext[0])
            with self.assertRaises(ValueError):
                host.layout(info)

class RuntimeCompatibility(unittest.TestCase):
    def test_private_status_for_each_branch_and_native_schema_check(self):
        import json
        import health
        for major in (10, 11, 12):
            host = Mock(php="php", web={})
            host.inspect.return_value = {"version": f"{major}.0.1"}
            native = {"db":{"status":"OK"}, "filesystem":{"status":"OK"}}
            if major == 12:
                native["glpi"] = {"database_version":{"uptodate":True}}
            report = {"checks":{"db":"ok"}, "schema_current":True, "plugins":["secureupdate", "healthcheck"]}
            with self.subTest(major=major), patch.object(health, "run") as runner:
                runner.side_effect = [Mock(stdout=json.dumps(native)), Mock(stdout=json.dumps(report))]
                health.inspect(host)
                self.assertEqual("--format=json" in runner.call_args_list[0].args[0], major == 10)
                report["schema_current"] = False
                runner.side_effect = [Mock(stdout=json.dumps(native)), Mock(stdout=json.dumps(report))]
                with self.assertRaisesRegex(ValueError, "esquema"):
                    health.inspect(host)

    def test_only_previously_active_plugins_resume_on_clone(self):
        from contextlib import contextmanager
        from unittest.mock import call
        host = Mock()
        mode = "suspended_by_update"

        @contextmanager
        def client(info):
            yield lambda stmt: mode if "plugins_execution_mode" in stmt else "healthcheck", None, None

        host.client = client
        info = {"version":"11.0.13"}
        engine.restore_plugin_execution(host, info, ["secureupdate","healthcheck"])
        self.assertEqual(host.console.call_args_list, [call("plugin:resume_execution"), call("plugin:activate","secureupdate")])
        mode = "suspended_manually"
        host.console.reset_mock()
        with self.assertRaisesRegex(ValueError, "administrador"):
            engine.restore_plugin_execution(host, info, ["secureupdate"])
        host.console.assert_not_called()
