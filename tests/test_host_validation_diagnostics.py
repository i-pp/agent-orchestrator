"""Offline checks for secret-free host validation diagnostics."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import validate_npm_codex_isolation_host as validation


class HostValidationDiagnosticsTests(unittest.TestCase):
    def test_failure_classifies_node_missing_without_returning_paths(self):
        private_path = "/synthetic-user/.nvm/versions/node/bin/node"
        error = subprocess.CalledProcessError(
            127, [private_path], stderr=f"{private_path}: No such file or directory")
        result = validation.safe_process_failure(error)
        self.assertIn("NODE_RUNTIME_OR_EXECUTABLE_MISSING", result)
        self.assertNotIn(private_path, result)

    def test_failure_classifies_namespace_denial_without_returning_stderr(self):
        private_detail = "/synthetic-user/private: Operation not permitted"
        error = subprocess.CalledProcessError(1, ["codex"], stderr=private_detail)
        result = validation.safe_process_failure(error)
        self.assertIn("NAMESPACE_OR_CAPABILITY_DENIED", result)
        self.assertNotIn(private_detail, result)

    def test_inner_canary_diagnostics_report_only_allowlisted_markers(self):
        result = subprocess.CompletedProcess(
            ["codex"], 24,
            stdout="ACEIS_STAGE=PACKAGE_VISIBLE\nACEIS_FAIL=AUTH_VISIBLE\n"
                   "ACEIS_STAGE=/secret/path\n",
            stderr="/synthetic-user/private: Permission denied")
        detail = validation.canary_failure_details(result)
        self.assertIn("stage=PACKAGE_VISIBLE", detail)
        self.assertIn("check=AUTH_VISIBLE", detail)
        self.assertIn("NAMESPACE_OR_CAPABILITY_DENIED", detail)
        self.assertNotIn("/synthetic-user", detail)

    def test_generated_inner_probe_compiles(self):
        with tempfile.TemporaryDirectory(prefix="diagnostic-code-") as temp:
            base = Path(temp)
            package = base / "node_modules/@openai/codex"
            runtime = base / "runner"
            secret = base / "secret"
            socket_path = base / "daemon.sock"
            code = validation.inner_codex_canary_code(
                {"package_root": package, "dependency_name": "codex-linux-x64"},
                runtime, secret, socket_path, "\\0synthetic", 54321)
            compile(code, "<generated inner canary>", "exec")

    def test_regression_summary_names_test_without_printing_traceback(self):
        private_path = "/synthetic-user/private/file"
        result = subprocess.CompletedProcess(
            ["python", "-m", "unittest"], 1, stdout="",
            stderr=("FAIL: test_installed_npm_codex_version_works_in_private_namespace "
                    "(test_version_isolation.VersionIsolationTests)\n"
                    f"AssertionError: {private_path}\n"))
        summary = validation.regression_failure_summary(result)
        self.assertIn("test_installed_npm_codex_version_works_in_private_namespace", summary)
        self.assertNotIn(private_path, summary)


if __name__ == "__main__":
    unittest.main()
