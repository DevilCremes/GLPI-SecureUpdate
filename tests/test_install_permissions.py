import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import instalar


class InstallPermissions(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = Path(tmp.name) / "glpi"
        self.app.mkdir(mode=0o755)
        (self.app / "plugins").mkdir(mode=0o755)
        self.data = self.app / "files"
        self.data.mkdir(mode=0o700)
        self.app.chmod(0o777)

    def test_detection_describes_problem_without_changing_permissions(self):
        with patch.object(instalar, "privileged_dir"), patch("builtins.input") as ask:
            with self.assertRaisesRegex(SystemExit, r"glpi.*0777"):
                instalar.installation_permissions(self.app, preview=True)
            ask.assert_not_called()
        self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o777)

    def test_declined_repair_keeps_directory_unchanged(self):
        with patch.object(instalar, "privileged_dir"), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="n"), patch("os.fchown") as chown:
            with self.assertRaises(SystemExit):
                instalar.installation_permissions(self.app, preview=False)
            chown.assert_not_called()
        self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o777)

    def test_confirmed_repair_targets_only_two_directories(self):
        touched = []
        def chown(fd, uid, gid):
            touched.append((os.readlink(f"/proc/self/fd/{fd}"), uid, gid))
        with patch.object(instalar, "privileged_dir"), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="s"), patch("os.fchown", side_effect=chown):
            instalar.installation_permissions(self.app, preview=False)
        self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)
        if os.getuid() != 0:
            self.assertEqual(touched, [(str(self.app),0,-1), (str(self.app/"plugins"),0,-1)])

    def test_untrusted_ancestor_disallows_repair(self):
        with patch.object(instalar, "privileged_dir", side_effect=SystemExit("pai")) as validate, patch("os.fchmod") as chmod:
            with self.assertRaisesRegex(SystemExit, "pai"):
                instalar.installation_permissions(self.app, preview=False)
            validate.assert_called_once_with(self.app.parent)
            chmod.assert_not_called()

    def test_link_in_place_of_plugins_is_refused(self):
        (self.app / "plugins").rmdir()
        (self.app / "plugins").symlink_to(self.data)
        with patch.object(instalar, "privileged_dir"), patch("sys.stdin.isatty",return_value=True), patch("builtins.input",return_value="s"), patch("os.fchown"):
            with self.assertRaisesRegex(SystemExit, "Diretório real"):
                instalar.installation_permissions(self.app, preview=False)
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)

    def test_special_permissions_require_review(self):
        self.app.chmod(0o2777)
        with patch.object(instalar, "privileged_dir"), patch("sys.stdin.isatty",return_value=True), patch("builtins.input") as ask:
            with self.assertRaisesRegex(SystemExit, "manualmente"):
                instalar.installation_permissions(self.app, preview=False)
            ask.assert_not_called()
        self.assertEqual(stat.S_IMODE(self.app.stat().st_mode),0o2777)

    def test_access_acl_requires_review(self):
        with patch.object(instalar, "privileged_dir"), patch("os.listxattr",return_value=["system.posix_acl_access"]), patch("sys.stdin.isatty",return_value=True), patch("builtins.input") as ask:
            with self.assertRaisesRegex(SystemExit, "ACLs"):
                instalar.installation_permissions(self.app, preview=False)
            ask.assert_not_called()

    def test_english_yes_approves_nonrecursive_permission_repair(self):
        for answer in ("y", " YES "):
            with self.subTest(answer=answer):
                self.app.chmod(0o777)
                with patch.object(instalar, "privileged_dir"), patch("sys.stdin.isatty", return_value=True), \
                     patch("builtins.input", return_value=answer), patch("os.fchown"):
                    instalar.installation_permissions(self.app, preview=False)
                self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)

    def test_empty_or_unrecognized_answer_does_not_authorize_repair(self):
        for answer in ("", "no", "talvez"):
            with self.subTest(answer=answer), patch.object(instalar, "privileged_dir"), \
                 patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=answer), \
                 patch("os.fchown") as chown, patch("os.fchmod") as chmod:
                with self.assertRaisesRegex(SystemExit, "Ajuste recusado"):
                    instalar.installation_permissions(self.app, preview=False)
                chown.assert_not_called()
                chmod.assert_not_called()
