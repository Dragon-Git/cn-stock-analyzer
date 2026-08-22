#!/usr/bin/env python3
"""
Tests for scripts/ima_upload.py.

Mocking strategy: import the script as a module and patch `urllib.request.urlopen`
directly. (Spawning a subprocess would isolate the mock — subprocess tests would
need a real mock server.)

Covers: success path, title extraction (H1 / filename fallback), UTF-8 validation,
missing creds, API error code, HTTP error, network error, alternate env var names,
folder_id passthrough.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 把 scripts/ 目录加进 sys.path, 然后导入 ima_upload
SCRIPTS_DIR = Path(__file__).parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import ima_upload  # type: ignore  # noqa: E402


def _ok_response(note_id: str = "12345") -> "mock.MagicMock":
    """构造一个成功响应的 mock context manager"""
    resp = mock.MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps(
        {"code": 0, "msg": "success", "data": {"note_id": note_id}}
    ).encode("utf-8")
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    return resp


def _http_error(status: int, body: dict) -> "urllib.error.HTTPError":
    """构造一个 urllib HTTPError"""
    from urllib.error import HTTPError
    return HTTPError(
        "https://ima.qq.com/openapi/note/v1/import_doc",
        status, "Error", {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


class TestImaUpload(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ima_test_")
        self.good_md = Path(self.tmpdir) / "report.md"
        self.good_md.write_text(
            "# 中国神华 (601088) post_close 2026-08-22\n\n## 一、行情\n\n42.50 +1.2%\n",
            encoding="utf-8",
        )
        self.h1_md = Path(self.tmpdir) / "h1.md"
        self.h1_md.write_text("# Hello World\n\nbody\n", encoding="utf-8")
        self.no_h1_md = Path(self.tmpdir) / "no_h1.md"
        self.no_h1_md.write_text("just plain text\n", encoding="utf-8")

    def _run(self, *args, env=None):
        """
        调用 ima_upload.main() 并捕获 stdout/stderr/exit.
        env: dict of extra env vars (merged on top of os.environ).
        """
        saved_env = os.environ.copy()
        os.environ["IMA_CLIENT_ID"] = "test_client"
        os.environ["IMA_API_KEY"] = "test_key"
        if env:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        saved_argv = sys.argv
        sys.argv = ["ima_upload.py", *args]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
             mock.patch("urllib.request.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _ok_response()
            try:
                rc = ima_upload.main()
            except SystemExit as e:
                rc = e.code
        # restore
        sys.argv = saved_argv
        os.environ.clear()
        os.environ.update(saved_env)
        return rc, out.getvalue(), err.getvalue(), urlopen_mock

    def test_success_with_explicit_title(self):
        rc, out, err, mock_open = self._run(
            str(self.good_md), "--title", "my title"
        )
        self.assertEqual(rc, 0, f"stderr: {err}")
        self.assertIn("note_id=12345", out)
        # 校验请求
        call = mock_open.call_args
        req = call.args[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "https://ima.qq.com/openapi/note/v1/import_doc")
        # urllib.request.Request 把 header 名字 canonicalize 成 "Title-Case-With-Dashes"
        # (每段首字母大写, 其余小写). HTTP 协议本身是大小写不敏感的, IMA 服务端会认.
        self.assertEqual(req.headers["Ima-openapi-clientid"], "test_client")
        self.assertEqual(req.headers["Ima-openapi-apikey"], "test_key")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["content_format"], 1)
        self.assertEqual(body["title"], "my title")
        self.assertIn("中国神华", body["content"])

    def test_title_from_h1(self):
        rc, out, err, mock_open = self._run(str(self.h1_md))
        self.assertEqual(rc, 0, f"stderr: {err}")
        body = json.loads(mock_open.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["title"], "Hello World")

    def test_title_fallback_to_filename(self):
        rc, out, err, mock_open = self._run(str(self.no_h1_md))
        self.assertEqual(rc, 0, f"stderr: {err}")
        body = json.loads(mock_open.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["title"], "no_h1")

    def test_missing_credentials(self):
        rc, out, err, mock_open = self._run(
            str(self.good_md),
            env={"IMA_CLIENT_ID": "", "IMA_API_KEY": ""},
        )
        self.assertEqual(rc, 1)
        self.assertIn("missing IMA credentials", err)
        mock_open.assert_not_called()

    def test_file_not_found(self):
        rc, out, err, mock_open = self._run("/tmp/nonexistent_xyz.md")
        self.assertEqual(rc, 2)
        self.assertIn("not found", err)
        mock_open.assert_not_called()

    def test_bad_utf8(self):
        bad = Path(self.tmpdir) / "bad.md"
        bad.write_bytes(b"\x80\x81\x82")
        rc, out, err, mock_open = self._run(str(bad))
        self.assertEqual(rc, 2)
        self.assertIn("not valid UTF-8", err)
        mock_open.assert_not_called()

    def test_api_error_code(self):
        with mock.patch.dict(os.environ, {
            "IMA_CLIENT_ID": "id", "IMA_API_KEY": "key",
        }, clear=False), \
        mock.patch("urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps(
                {"code": 200002, "msg": "skill auth failed", "data": {}}
            ).encode("utf-8")
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            urlopen.return_value = resp

            saved_argv = sys.argv
            sys.argv = ["ima_upload.py", str(self.good_md)]
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit) as cm:
                    ima_upload.main()
                self.assertEqual(cm.exception.code, 4)
            self.assertIn("skill auth failed", err.getvalue())
            sys.argv = saved_argv

    def test_http_error(self):
        with mock.patch.dict(os.environ, {
            "IMA_CLIENT_ID": "id", "IMA_API_KEY": "key",
        }, clear=False), \
        mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = _http_error(401, {"code": 200002, "msg": "bad key"})

            saved_argv = sys.argv
            sys.argv = ["ima_upload.py", str(self.good_md)]
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit) as cm:
                    ima_upload.main()
                self.assertEqual(cm.exception.code, 3)
            self.assertIn("401", err.getvalue())
            sys.argv = saved_argv

    def test_network_error(self):
        from urllib.error import URLError
        with mock.patch.dict(os.environ, {
            "IMA_CLIENT_ID": "id", "IMA_API_KEY": "key",
        }, clear=False), \
        mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = URLError("connection refused")

            saved_argv = sys.argv
            sys.argv = ["ima_upload.py", str(self.good_md)]
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit) as cm:
                    ima_upload.main()
                self.assertEqual(cm.exception.code, 3)
            self.assertIn("connection refused", err.getvalue())
            sys.argv = saved_argv

    def test_folder_id_passed_through(self):
        rc, out, err, mock_open = self._run(
            str(self.good_md), "--folder-id", "FOLDER_42"
        )
        self.assertEqual(rc, 0, f"stderr: {err}")
        body = json.loads(mock_open.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["folder_id"], "FOLDER_42")

    def test_alternate_env_var_names(self):
        """兼容 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY"""
        rc, out, err, mock_open = self._run(
            str(self.good_md),
            env={
                "IMA_CLIENT_ID": "",  # 短名清空, 强制用长名
                "IMA_API_KEY": "",
                "IMA_OPENAPI_CLIENTID": "alt_id",
                "IMA_OPENAPI_APIKEY": "alt_key",
            },
        )
        self.assertEqual(rc, 0, f"stderr: {err}")
        req = mock_open.call_args.args[0]
        self.assertEqual(req.headers["Ima-openapi-clientid"], "alt_id")
        self.assertEqual(req.headers["Ima-openapi-apikey"], "alt_key")

    def test_h1_title_truncated_to_120_chars(self):
        """很长的 H1 标题应被截到 120 字符, 防止 IMA 拒绝超长 title"""
        long_md = Path(self.tmpdir) / "long.md"
        long_md.write_text("# " + "X" * 500 + "\n\nbody\n", encoding="utf-8")
        rc, out, err, mock_open = self._run(str(long_md))
        self.assertEqual(rc, 0, f"stderr: {err}")
        body = json.loads(mock_open.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(len(body["title"]), 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
