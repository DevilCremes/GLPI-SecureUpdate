"""Integridade, transporte local e autorização da preparação do pacote."""
from contextlib import ExitStack, closing
import hashlib
import io
from pathlib import Path
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
import activity
import agent
import common
import packages


class Packages(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.content = b"fixture from release"
        self.digest = hashlib.sha256(self.content).hexdigest()
        version = "12.0.2"
        url = f"{common.GITHUB}/releases/download/{version}/glpi-{version}.tgz"
        self.asset = dict(name=f"glpi-{version}.tgz", browser_download_url=url,
                          size=len(self.content), digest="sha256:" + self.digest)
        release = dict(tag_name=version, draft=False, prerelease=False, assets=[self.asset])
        self.plan = common.catalog("12.0.1", lambda _: [release])
        self.stack.enter_context(patch.object(packages, "api", return_value=release))
        self.stack.enter_context(patch.multiple(agent, STATE=self.root, STORE=self.root/"panel.json",
                                                 JOURNAL=self.root/"journal", LAST=self.root/"last"))
        self.agent = agent.Agent({"web_user": pwd.getpwuid(os.getuid()).pw_name})
        self.agent.s["plan"] = self.plan

    def prepare(self, mode="download", data=None, job="a"*16):
        request = dict(op=mode, plan=self.plan, actor=7, id=job, size=len(self.content))
        stream = io.BytesIO(self.content if data is None else data)
        name = "uploaded" if mode=="upload" else "downloaded"
        with patch.object(packages, name, return_value=closing(stream)) as reader:
            receipt = packages.prepare(self.root, request)
        if mode=="download":
            reader.assert_called_once_with(self.asset["browser_download_url"])
        else:
            reader.assert_called_once_with()
        return receipt

    def test_old_job_logs_are_bounded_and_symlinks_are_ignored(self):
        for number in range(205):
            path=self.root/f"job-{number:016x}.log"
            path.write_text("done")
            os.utime(path,(number+1,number+1))
        outside=self.root/"private.log"
        outside.write_text("keep")
        (self.root/("job-"+"f"*16+".log")).symlink_to(outside)
        agent.prune_job_logs(self.root)
        remaining=[p for p in self.root.glob("job-????????????????.log") if not p.is_symlink()]
        self.assertEqual(len(remaining),199)
        self.assertTrue(outside.exists())
        self.assertFalse((self.root/("job-"+"0"*16+".log")).exists())

    def test_both_sources_produce_the_same_digest_before_maintenance(self):
        for mode in ("download","upload"):
            receipt = self.prepare(mode)
            self.assertEqual((receipt["sha"],receipt["source"]), (self.digest,mode))
            self.assertEqual((self.root/"verified.tgz").read_bytes(), self.content)
            self.assertFalse(agent.JOURNAL.exists())

    def test_console_tracks_validated_package_and_rejects_stale_work(self):
        job = "a"*16
        activity.begin(self.root, job, "download")
        self.prepare(job=job)
        self.agent.s.update(job=job, phase="disponível", prepared={"id":job})
        activity.record(self.root, "b"*16, "receiving", [1, 100])
        state = self.agent.dispatch({"op":"activity"})
        self.assertFalse(state["busy"])
        self.assertTrue(state["hold"])
        self.assertIsNone(state["transfer"])
        self.assertEqual([e[1] for e in state["events"]], [activity.OPERATIONS["download"],
                         *[activity.STEPS[s] for s in ("metadata","receiving","digest","verified")]])

    def test_console_progress_failure_and_allowlisted_events(self):
        job = "a"*16
        activity.begin(self.root, job, "upload")
        self.agent.s.update(job=job, phase="validando pacote")
        for count in (1, 8):
            activity.record(self.root, job, "receiving", [count, 10])
        activity.record(self.root, job, "private log line")
        (self.root / ("job-" + job + ".log")).write_text("sensitive raw log")
        state = self.agent.dispatch({"op":"activity"})
        self.assertEqual(state["transfer"], [8, 10])
        self.assertEqual(len(state["events"]), 2)
        self.assertNotIn("private", str(state))
        self.assertNotIn("sensitive", str(state))
        self.assertTrue(state["busy"])
        with self.assertRaises(ValueError):
            self.prepare(mode="upload", data=b"x"*len(self.content), job=job)
        self.agent.s["phase"] = "falha"
        state = self.agent.dispatch({"op":"activity"})
        self.assertFalse(state["busy"])
        self.assertTrue(state["attention"])
        self.assertNotIn(activity.STEPS["verified"], str(state))
        self.assertIsNone(state["transfer"])

    def test_incorrect_bytes_never_publish_receipt(self):
        for data in (self.content[:-1], self.content+b"x", b"x"*len(self.content)):
            self.prepare()
            with self.assertRaises(ValueError):
                self.prepare("upload", data)
            for name in ("verified.json","verified.tgz","candidate.part"):
                self.assertFalse((self.root/name).exists())

    def test_release_changed(self):
        self.asset["digest"]="sha256:"+"0"*64
        with self.assertRaisesRegex(ValueError,"release mudou"):
            self.prepare()

    def test_size_from_official_metadata(self):
        for size in (0,True,None,packages.MAX_BYTES+1):
            self.asset["size"]=size
            with self.assertRaisesRegex(ValueError,"Tamanho"):
                self.prepare()

    def test_declared_upload_must_match(self):
        self.asset["size"]+=1
        with self.assertRaisesRegex(ValueError,"Tamanho diferente"):
            self.prepare("upload")

    def test_can_only_apply_verified_once_by_same_actor(self):
        r=self.prepare()
        self.agent.s["prepared"]=r
        for actor, ref in [(8,r["id"]),(7,self.plan["id"])]:
            with self.assertRaises(ValueError):
                self.agent.dispatch(dict(op="apply",actor=actor,ref=ref))
        with patch.object(agent.subprocess,"Popen"):
            self.agent.dispatch(dict(op="apply",actor=7,ref=r["id"]))
        ticket=common.read(next(self.root.glob("request-*.json")))
        self.assertEqual(ticket["ref"],r["id"])
        self.agent.process=None
        with self.assertRaises(ValueError):
            self.agent.dispatch(dict(op="apply",actor=7,ref=r["id"]))

    def test_stale_verification_and_changed_catalog(self):
        r=self.prepare()
        self.assertFalse(packages.matches({**r,"checked":time.time()-86401}, self.plan))
        self.assertFalse(packages.matches(r,{**self.plan,"id":"other"}))
        self.assertFalse(packages.matches(r,{**self.plan,"checked":0}))

    def test_new_preparation_hides_old_approval(self):
        r=self.prepare()
        self.agent.s["prepared"]=r
        with patch.object(agent.subprocess,"Popen"):
            self.agent.dispatch(dict(op="download",actor=7,ref=self.plan["id"]))
        self.assertFalse(self.agent.dispatch({"op":"status"})["prepared"])
        self.assertEqual(self.agent.running_job["op"],"download")

    def test_poll_success_only_offers_approval(self):
        r=self.prepare("upload")
        self.agent.process=Mock(returncode=0)
        self.agent.running_job={"op":"upload","id":r["id"]}
        self.agent.poll()
        self.assertEqual(self.agent.dispatch({"op":"status"})["prepared"],r)
        self.assertFalse(list(self.root.glob("request-*.json")))

    def test_local_socket_stream_survives_parent_close(self):
        # Envia bytes pelo mesmo descritor herdado que o worker Linux recebe no stdin.
        payload = self.content * 9000
        code = "import packages,hashlib\nwith packages.uploaded() as f: print(hashlib.sha256(f.read()).hexdigest())"
        with ExitStack() as stack:
            client, server = socket.socketpair()
            stack.enter_context(client)
            stack.enter_context(server)
            server.settimeout(2)
            with subprocess.Popen([sys.executable,"-E","-s","-c",code],cwd=SERVICE,
                                  stdin=server,stdout=subprocess.PIPE) as child:
                server.close()
                client.settimeout(10)
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                result=child.communicate(timeout=10)[0]
                self.assertEqual(child.returncode,0)
                self.assertEqual(result.decode().strip(),hashlib.sha256(payload).hexdigest())
