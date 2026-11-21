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

    def test_both_sources_produce_the_same_digest_before_maintenance(self):
        for mode in ("download","upload"):
            receipt = self.prepare(mode)
            self.assertEqual((receipt["sha"],receipt["source"]), (self.digest,mode))
            self.assertEqual((self.root/"verified.tgz").read_bytes(), self.content)
            self.assertFalse(agent.JOURNAL.exists())

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
