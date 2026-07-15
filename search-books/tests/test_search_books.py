from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "search_books.py"
SPEC = importlib.util.spec_from_file_location("search_books", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fake_pdf(label: bytes = b"one") -> bytes:
    return b"%PDF-1.7\n" + label + b"\n" + (b"x" * 600) + b"\n%%EOF\n"


class TitleTests(unittest.TestCase):
    def test_normalize_title_ignores_outer_marks_spacing_and_case(self):
        self.assertEqual(
            MODULE.normalize_title("《How-To   Read：A Book》"),
            MODULE.normalize_title("how-to read:a book"),
        )

    def test_normalize_title_preserves_semantic_punctuation(self):
        self.assertNotEqual(MODULE.normalize_title("C++"), MODULE.normalize_title("C"))
        self.assertNotEqual(MODULE.normalize_title("A/B"), MODULE.normalize_title("AB"))

    def test_sanitize_title_removes_path_characters(self):
        self.assertEqual(MODULE.sanitize_filename_title("  钱/的:第四维  "), "钱-的-第四维")
        self.assertNotIn("/", MODULE.sanitize_filename_title("../危险/书名"))

    def test_select_exact_pdf_rejects_epub_and_near_match(self):
        books = [
            {"title": "目标书", "extension": "epub", "id": 1},
            {"title": "目标书 增订版", "extension": "pdf", "id": 2},
            {"title": "《目标书》", "extension": "PDF", "id": 3},
        ]
        selected = MODULE.select_exact_pdf(books, "目标书")
        self.assertEqual(selected["id"], 3)


class StatePathTests(unittest.TestCase):
    def test_search_books_state_dir_overrides_hermes_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "codex-state"
            with mock.patch.dict(
                os.environ,
                {
                    "SEARCH_BOOKS_STATE_DIR": str(expected),
                    "HERMES_HOME": "/ignored/hermes-home",
                },
                clear=True,
            ):
                self.assertEqual(MODULE.default_state_dir(), expected)


class PdfCommitTests(unittest.TestCase):
    def make_vault(self, root: Path) -> Path:
        vault = root / "My-wiki"
        (vault / ".obsidian").mkdir(parents=True)
        return vault.resolve()

    def test_commit_saves_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = self.make_vault(root)
            source = root / "source.download"
            content = fake_pdf()
            source.write_bytes(content)

            result = MODULE.commit_pdf(source, vault, "测试书")

            self.assertEqual(result["status"], "saved")
            saved = Path(result["path"])
            self.assertEqual(saved.read_bytes(), content)
            self.assertFalse(source.exists())
            self.assertEqual(saved.parent, vault / MODULE.BOOKS_SUBDIR)

    def test_duplicate_hash_does_not_create_second_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = self.make_vault(root)
            first = root / "first.download"
            first.write_bytes(fake_pdf())
            first_result = MODULE.commit_pdf(first, vault, "第一书名")

            second = root / "second.download"
            second.write_bytes(fake_pdf())
            second_result = MODULE.commit_pdf(second, vault, "第二书名")

            self.assertEqual(first_result["status"], "saved")
            self.assertEqual(second_result["status"], "duplicate")
            self.assertEqual(first_result["path"], second_result["path"])
            self.assertEqual(len(list((vault / MODULE.BOOKS_SUBDIR).glob("*.pdf"))), 1)

    def test_conflict_never_overwrites_existing_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = self.make_vault(root)
            first = root / "first.download"
            first.write_bytes(fake_pdf(b"first"))
            saved = MODULE.commit_pdf(first, vault, "同名书")
            original = Path(saved["path"]).read_bytes()

            second = root / "second.download"
            second.write_bytes(fake_pdf(b"second"))
            conflict = MODULE.commit_pdf(second, vault, "同名书")

            self.assertEqual(conflict["status"], "conflict")
            self.assertEqual(Path(saved["path"]).read_bytes(), original)

    def test_rejects_non_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-pdf"
            path.write_bytes(b"not a pdf" * 100)
            with self.assertRaises(MODULE.SkillError):
                MODULE.validate_pdf(path)

    def test_rejects_html_that_embeds_pdf_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.pdf"
            path.write_bytes(
                b"<html><body>%PDF-1.7 not really a pdf</body></html>" + b"x" * 600
            )
            with self.assertRaises(MODULE.SkillError):
                MODULE.validate_pdf(path)

    def test_duplicate_scan_does_not_follow_pdf_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = self.make_vault(root)
            directory = vault / MODULE.BOOKS_SUBDIR
            directory.mkdir(parents=True)
            outside = root / "outside.pdf"
            outside.write_bytes(fake_pdf())
            (directory / "linked.pdf").symlink_to(outside)
            source = root / "source.download"
            source.write_bytes(fake_pdf())

            result = MODULE.commit_pdf(source, vault, "真实副本")

            self.assertEqual(result["status"], "saved")
            self.assertNotEqual(Path(result["path"]).name, "linked.pdf")


class ProtocolTests(unittest.TestCase):
    def test_parse_cli_json(self):
        envelope = json.dumps({"result": json.dumps({"status": "downloaded"})})
        self.assertEqual(MODULE.parse_cli_json(envelope)["status"], "downloaded")
        with self.assertRaises(MODULE.SkillError):
            MODULE.parse_cli_json("[]")

    def test_browser_fallback_is_fixed_to_confirmed_site_chain(self):
        code = MODULE.build_browser_fallback_code(
            "测试书", Path("/tmp/error.png")
        )
        self.assertIn("https://z-library.bz/", code)
        self.assertIn("page.context().route('**/*'", code)
        self.assertIn("originOf(item.href) === searchOrigin", code)
        self.assertIn("if (!allowedOrigins.has(origin))", code)
        self.assertIn("!homeResponse.ok()", code)
        self.assertIn("blockedNavigation", code)
        self.assertIn("ServiceWorkerContainer.prototype.register", code)
        self.assertIn("normalize(item.cardTitle) === requested", code)
        self.assertIn("item.cardExtension.trim().toLowerCase() === 'pdf'", code)
        self.assertIn("page.context().request.get(link.href", code)
        self.assertIn("maxRedirects: 0", code)
        self.assertIn("sourceDownloadUrl", code)
        self.assertIn("convertedto=pdf", code.lower())
        self.assertIn("z-library.im", code)
        self.assertNotIn("google.com", code)
        self.assertNotIn("bing.com", code)

    def test_browser_fallback_allows_only_confirmed_bz_to_im_site_chain(self):
        code = MODULE.build_browser_fallback_code(
            "Pride and Prejudice", Path("/tmp/error.png")
        )
        self.assertIn("https://z-library.bz/", code)
        self.assertIn("https://z-library.im/", code)
        self.assertIn("allowedOrigins", code)
        self.assertIn("'s/'", code)
        self.assertNotIn("z-library.sk", code)
        self.assertNotIn("google.com", code)

    def test_site_urls_allow_bz_and_im_but_reject_other_mirrors(self):
        self.assertEqual(
            MODULE.validate_site_url("https://z-library.bz/", purpose="入口"),
            "https://z-library.bz/",
        )
        self.assertEqual(
            MODULE.validate_site_url("https://z-library.im/s/book", purpose="搜索"),
            "https://z-library.im/s/book",
        )
        with self.assertRaises(MODULE.SkillError):
            MODULE.validate_site_url("https://z-library.sk/s/book", purpose="搜索")

    @unittest.skipUnless(shutil.which("node"), "node is required for JS syntax validation")
    def test_browser_fallback_javascript_is_valid(self):
        code = MODULE.build_browser_fallback_code(
            "测试书", Path("/tmp/error.png")
        )
        completed = subprocess.run(
            [
                shutil.which("node"),
                "-e",
                "new Function('return (' + process.argv[1] + ')')",
                code,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is required for JS syntax validation")
    def test_login_and_site_probe_javascript_are_valid(self):
        for code in (
            MODULE.build_login_guard_code(),
            MODULE.build_login_final_check_code(),
            MODULE.build_site_probe_code(),
        ):
            completed = subprocess.run(
                [
                    shutil.which("node"),
                    "-e",
                    "new Function('return (' + process.argv[1] + ')')",
                    code,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("allowedOrigins", code)
            self.assertIn("serviceWorkers", code)
            if "route('**/*'" in code:
                self.assertIn("page.context().route('**/*'", code)
                self.assertIn("if (!allowedOrigins.has(origin))", code)
        self.assertIn("searchUiValid", MODULE.build_site_probe_code())

    def test_download_link_extraction_handles_nested_shape(self):
        self.assertEqual(
            MODULE.nested_download_link({"file": {"downloadLink": "/dl/abc"}}),
            "/dl/abc",
        )

    def test_cookie_header_rejects_expired_target_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "remix_userid",
                                "value": "expired",
                                "domain": ".z-library.im",
                                "expires": time.time() - 10,
                            },
                            {
                                "name": "unrelated",
                                "value": "value",
                                "domain": ".example.com",
                                "expires": -1,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state.chmod(0o600)
            cookie, values = MODULE.cookie_header(state)
            self.assertEqual(cookie, "")
            self.assertEqual(values, {})

    def test_cookie_header_accepts_only_exact_new_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {"name": "valid", "value": "1", "domain": ".z-library.im", "expires": -1},
                            {"name": "parent", "value": "2", "domain": ".im", "expires": -1},
                            {"name": "old", "value": "3", "domain": ".zlib.li", "expires": -1},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state.chmod(0o600)

            cookie, values = MODULE.cookie_header(state)

            self.assertEqual(cookie, "valid=1")
            self.assertEqual(values, {"valid": "1"})

    def test_cookie_header_uses_only_exact_im_session_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {"name": "valid", "value": "1", "domain": ".z-library.im", "expires": -1},
                            {"name": "entry", "value": "2", "domain": ".z-library.bz", "expires": -1},
                            {"name": "parent", "value": "3", "domain": ".im", "expires": -1},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state.chmod(0o600)

            cookie, values = MODULE.cookie_header(state)

            self.assertEqual(cookie, "valid=1")
            self.assertEqual(values, {"valid": "1"})

    def test_api_search_uses_bz_frontend_contract(self):
        payload = {"books": [{"title": "测试书", "extension": "pdf", "dl": "/dl/a"}]}
        auth_values = {"remix_userid": "1", "remix_userkey": "2"}
        with mock.patch.object(
            MODULE,
            "cookie_header",
            return_value=("remix_userid=1; remix_userkey=2", auth_values),
        ):
            with mock.patch.object(MODULE, "request_json", return_value=payload) as request_json:
                book, cookie, _ = MODULE.api_search("测试书", Path("/tmp/state.json"))

        self.assertEqual(book["dl"], "/dl/a")
        self.assertEqual(cookie, "remix_userid=1; remix_userkey=2")
        url = request_json.call_args.args[0]
        body = request_json.call_args.kwargs["data"]
        self.assertEqual(url, "https://z-library.bz/api/search")
        self.assertIn(b'name="q"', body)
        self.assertIn(b'name="order"', body)
        self.assertNotIn(b'name="message"', body)

    def test_unverified_old_file_api_is_not_called(self):
        with mock.patch.object(MODULE, "request_json") as request_json:
            with self.assertRaises(MODULE.SkillError):
                MODULE.api_download_link({"id": "1", "hash": "abc"}, "cookie", {})
        request_json.assert_not_called()

    def test_relative_direct_pdf_link_resolves_on_new_host(self):
        self.assertEqual(
            MODULE.api_download_link({"dl": "/dl/book.pdf"}, "", {}),
            "https://z-library.im/dl/book.pdf",
        )

    def test_absolute_direct_pdf_link_on_unapproved_mirror_is_rejected(self):
        with self.assertRaises(MODULE.SkillError):
            MODULE.api_download_link(
                {"dl": "https://z-library.sk/dl/book.pdf"}, "", {}
            )

    def test_external_download_never_receives_zlibrary_cookie(self):
        headers = MODULE.download_headers(
            "https://cdn.example.org/book.pdf", "remix_userid=secret"
        )
        self.assertNotIn("Cookie", headers)
        same_host = MODULE.download_headers(
            "https://z-library.im/dl/book.pdf", "remix_userid=secret"
        )
        self.assertEqual(same_host["Cookie"], "remix_userid=secret")

    @mock.patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[(MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    def test_cross_origin_redirect_is_rejected(self, _mock_dns):
        request = MODULE.urllib.request.Request(
            "https://z-library.bz/dl/book",
            headers={"Cookie": "remix_userid=secret", "Authorization": "secret"},
        )
        handler = MODULE.SafeRedirectHandler()
        with self.assertRaises(MODULE.SkillError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://cdn.example.org/book.pdf",
            )

    @mock.patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[(MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    def test_download_redirect_from_im_to_public_https_file_host_is_allowed_without_secrets(
        self, _mock_dns
    ):
        request = MODULE.urllib.request.Request(
            "https://z-library.im/dl/book",
            headers={"Cookie": "remix_userid=secret", "Authorization": "secret"},
        )
        handler = MODULE.SafeRedirectHandler()

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://dln1.ncdn.ec/files/book.pdf",
        )

        self.assertEqual(redirected.full_url, "https://dln1.ncdn.ec/files/book.pdf")
        headers = {name.casefold(): value for name, value in redirected.header_items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)

    def test_http_and_nonstandard_ports_are_rejected(self):
        with self.assertRaises(MODULE.SkillError):
            MODULE.validate_download_url("http://z-library.bz/dl/book.pdf")
        with self.assertRaises(MODULE.SkillError):
            MODULE.validate_download_url("https://z-library.bz:8443/dl/book.pdf")

    @mock.patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[
            (MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("198.18.0.47", 443))
        ],
    )
    def test_proxy_fake_ip_is_allowed_only_for_fixed_host(self, _mock_dns):
        self.assertEqual(
            MODULE.validate_download_url("https://z-library.bz/api/search"),
            "https://z-library.bz/api/search",
        )
        with self.assertRaises(MODULE.SkillError):
            MODULE.validate_download_url("https://cdn.example.org/book.pdf")

    @mock.patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[
            (MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("198.18.0.88", 443))
        ],
    )
    def test_proxy_fake_ip_is_allowed_for_attested_file_redirect(self, _mock_dns):
        self.assertEqual(
            MODULE.validate_attested_download_url(
                "https://dln1.ncdn.ec/files/book.pdf",
                "https://z-library.im/dl/book",
            ),
            "https://dln1.ncdn.ec/files/book.pdf",
        )

    @mock.patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[
            (MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    def test_loopback_resolution_is_rejected_even_for_fixed_host(self, _mock_dns):
        with self.assertRaises(MODULE.SkillError):
            MODULE.validate_download_url("https://z-library.bz/api/search")

    @mock.patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[
            (MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("192.168.1.7", 443))
        ],
    )
    def test_dns_check_reuses_safe_address_policy(self, _mock_dns):
        ok, error = MODULE.check_dns()
        self.assertFalse(ok)
        self.assertIn("非公网", error)

    def test_api_requests_reject_other_hosts_before_network_access(self):
        with self.assertRaises(MODULE.SkillError):
            MODULE.request_json("https://z-library.sk/api/search", cookie="")

    def test_api_conversion_link_is_rejected(self):
        with self.assertRaises(MODULE.SkillError):
            MODULE.api_download_link(
                {"dl": "/dl/book?convertedTo=pdf"}, "", {}
            )
        with self.assertRaises(MODULE.SkillError):
            MODULE.api_download_link(
                {"dl": "/dl/book?convertedTo%253Dpdf"}, "", {}
            )

    @mock.patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[
            (MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    def test_same_origin_redirect_to_conversion_is_rejected(self, _mock_dns):
        request = MODULE.urllib.request.Request("https://z-library.bz/dl/book")
        handler = MODULE.SafeRedirectHandler()
        with self.assertRaises(MODULE.SkillError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://z-library.bz/dl/book?convertedTo%3Dpdf",
            )

    def test_cookie_state_must_be_private_and_well_formed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text("[]", encoding="utf-8")
            state.chmod(0o600)
            with self.assertRaises(MODULE.SkillError):
                MODULE.cookie_header(state)
            state.write_text(json.dumps({"cookies": []}), encoding="utf-8")
            state.chmod(0o644)
            with self.assertRaises(MODULE.SkillError):
                MODULE.cookie_header(state)

    def test_doctor_requires_live_search_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "My-wiki"
            (vault / ".obsidian").mkdir(parents=True)
            state_dir = root / "state"
            state_dir.mkdir()
            state = state_dir / MODULE.STORAGE_STATE_NAME
            state.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "remix_userid",
                                "value": "1",
                                "domain": ".z-library.im",
                                "expires": -1,
                            },
                            {
                                "name": "remix_userkey",
                                "value": "2",
                                "domain": ".z-library.im",
                                "expires": -1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state.chmod(0o600)
            completed = mock.Mock(returncode=0, stdout="0.1.17", stderr="")
            probe = {
                "homepage": True,
                "homepageStatus": 200,
                "homepageFinalUrl": "https://z-library.bz/",
                "searchBackend": False,
                "searchStatus": 503,
                "searchUiValid": False,
                "searchFinalUrl": "https://z-library.im/s/__hermes_healthcheck__",
            }
            with mock.patch.object(MODULE, "resolve_playwright_cli", return_value=["pw"]):
                with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
                    with mock.patch.object(MODULE, "check_site_with_browser", return_value=probe):
                        with mock.patch.object(MODULE, "check_dns", return_value=(True, None)):
                            result = MODULE.doctor(str(vault), state_dir)

            self.assertTrue(result["homepage"])
            self.assertFalse(result["search_backend"])
            self.assertEqual(result["search_http_status"], 503)
            self.assertFalse(result["ready"])

    def test_doctor_allows_anonymous_ready_state_when_live_ui_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "My-wiki"
            (vault / ".obsidian").mkdir(parents=True)
            state_dir = root / "state"
            state_dir.mkdir()
            completed = mock.Mock(returncode=0, stdout="0.1.17", stderr="")
            probe = {
                "homepage": True,
                "homepageStatus": 200,
                "homepageFinalUrl": "https://z-library.bz/",
                "searchBackend": True,
                "searchStatus": 503,
                "searchUiValid": True,
                "searchFinalUrl": "https://z-library.im/s/__hermes_healthcheck__",
            }
            with mock.patch.object(MODULE, "resolve_playwright_cli", return_value=["pw"]):
                with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
                    with mock.patch.object(MODULE, "check_site_with_browser", return_value=probe):
                        with mock.patch.object(MODULE, "check_dns", return_value=(True, None)):
                            result = MODULE.doctor(str(vault), state_dir)

            self.assertFalse(result["session"])
            self.assertTrue(result["search_backend"])
            self.assertTrue(result["ready"])

    def test_session_requires_both_authentication_cookies(self):
        self.assertFalse(MODULE.has_authenticated_session({"other": "1"}))
        self.assertFalse(MODULE.has_authenticated_session({"remix_userid": "1"}))
        self.assertTrue(
            MODULE.has_authenticated_session(
                {"remix_userid": "1", "remix_userkey": "2"}
            )
        )

    def test_browser_fallback_skips_invalid_session_and_runs_anonymously(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            state = state_dir / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "unrelated",
                                "value": "1",
                                "domain": ".z-library.im",
                                "expires": -1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state.chmod(0o600)
            envelope = json.dumps(
                {
                    "result": json.dumps(
                        {
                            "status": "link",
                            "downloadUrl": "https://z-library.im/dl/book",
                        }
                    )
                }
            )
            with mock.patch.object(MODULE, "validate_download_url", side_effect=lambda url, **_kwargs: url):
                with mock.patch.object(MODULE, "resolve_playwright_cli", return_value=["pw"]):
                    with mock.patch.object(MODULE, "run_cli", side_effect=["", envelope, ""]) as run_cli:
                        result = MODULE.browser_fallback("测试书", state, state_dir)

            self.assertEqual(result["status"], "link")
            commands = [call.args[2] for call in run_cli.call_args_list]
            self.assertFalse(any(command[0] == "state-load" for command in commands))

    def test_browser_probe_applies_ip_policy_before_launch(self):
        with mock.patch.object(
            MODULE,
            "validate_download_url",
            side_effect=MODULE.SkillError("拒绝访问非公网下载地址。"),
        ):
            with mock.patch.object(MODULE, "run_cli") as run_cli:
                with self.assertRaises(MODULE.SkillError):
                    MODULE.check_site_with_browser(["pw"])
        run_cli.assert_not_called()

    def test_missing_playwright_does_not_auto_fetch_with_npx(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(MODULE.Path, "home", return_value=Path(tmp)):
                with mock.patch.object(MODULE.shutil, "which", return_value=None):
                    with self.assertRaises(MODULE.SkillError):
                        MODULE.resolve_playwright_cli()

    def test_http_download_rejects_external_host_at_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MODULE.SkillError):
                MODULE.http_download(
                    "https://cdn.example.org/book.pdf",
                    Path(tmp) / "book.download",
                    cookie="secret",
                )

    def test_curl_download_accepts_only_attested_external_url_and_forwards_no_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "book.download"

            def fake_run(command, **_kwargs):
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_bytes(fake_pdf())
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(
                MODULE, "validate_download_url", side_effect=lambda url, **_kwargs: url
            ):
                with mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/curl"):
                    with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run) as run:
                        MODULE.http_download(
                            "https://dln1.ncdn.ec/files/book.pdf",
                            destination,
                            cookie="remix_userid=secret",
                            source_url="https://z-library.im/dl/book",
                        )

            command = run.call_args.args[0]
            self.assertIn("--max-redirs", command)
            self.assertEqual(command[command.index("--max-redirs") + 1], "0")
            self.assertNotIn("--location-trusted", command)
            self.assertFalse(any("remix_userid=secret" in item for item in command))
            self.assertEqual(destination.read_bytes(), fake_pdf())

    def test_attested_external_url_rejects_non_dl_source(self):
        with mock.patch.object(
            MODULE, "validate_download_url", side_effect=lambda url, **_kwargs: url
        ):
            with self.assertRaises(MODULE.SkillError):
                MODULE.validate_attested_download_url(
                    "https://dln1.ncdn.ec/files/book.pdf",
                    "https://z-library.im/book/abc/title.html",
                )

    def test_http_download_never_deletes_preexisting_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "collision.download"
            destination.write_bytes(b"KEEP-ME")
            with mock.patch.object(
                MODULE,
                "validate_download_url",
                side_effect=lambda url, **_kwargs: url,
            ):
                with mock.patch.object(MODULE.subprocess, "run") as run:
                    with self.assertRaises(MODULE.SkillError):
                        MODULE.http_download(
                            "https://z-library.bz/dl/book.pdf",
                            destination,
                            cookie="secret",
                        )

            self.assertEqual(destination.read_bytes(), b"KEEP-ME")
            run.assert_not_called()

    def test_download_command_stops_at_failed_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "My-wiki"
            (vault / ".obsidian").mkdir(parents=True)
            state_dir = root / "state"
            with mock.patch.object(MODULE, "doctor", return_value={"ready": False, "search_backend": False}):
                with mock.patch.object(MODULE, "download_book") as download_book:
                    exit_code = MODULE.main(
                        [
                            "--state-dir",
                            str(state_dir),
                            "--vault",
                            str(vault),
                            "download",
                            "Pride and Prejudice",
                            "--json",
                        ]
                    )

            self.assertEqual(exit_code, 3)
            download_book.assert_not_called()


if __name__ == "__main__":
    unittest.main()
