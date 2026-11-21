import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'service'))
import health

class Reply(io.BytesIO):
    status=200

class HTTP(unittest.TestCase):
    def probe(self,content,url="https://glpi.example/status.php"):
        with patch.object(health.time,"sleep"), patch.object(health,"build_opener") as opener:
            opener.return_value.open.side_effect=lambda *a,**k: Reply(content)
            return health.http_probe(url)
    def test_real_shape(self):
        self.assertIn("HTTP 200",self.probe(json.dumps({"db":{"status":"OK"},"filesystem":{"status":"OK"}}).encode()))
    def test_login_page_is_not_health(self):
        with self.assertRaises(ValueError): self.probe(b"<html>login</html>")
    def test_problem(self):
        with self.assertRaises(ValueError): self.probe(b'{"db":{"status":"PROBLEM"},"filesystem":{"status":"OK"}}')
    def test_oversize(self):
        with self.assertRaises(ValueError): self.probe(b'{"db":{"status":"OK"},"filesystem":{"status":"OK"}}'+b" "*524288)
    def test_missing(self):
        with self.assertRaises(ValueError): self.probe(b'{}')
    def test_scheme(self):
        with self.assertRaises(ValueError): self.probe(b'{}',"file:///status.php")
    def test_no_redirect(self):
        self.assertIsNone(health.NoRedirect().redirect_request(None,None,None,None,None,None))
