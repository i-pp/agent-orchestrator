"""Offline tests for the npm Codex mount layout. No namespaces or auth are used."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
import isolated_entry


class CodexPackageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-package-")
        self.base = Path(self.temp.name)
        self.package = self.base / "node_modules/@openai/codex"
        self.package.mkdir(parents=True)
        (self.package / "bin").mkdir()
        self.executable = self.package / "bin/codex.js"
        self.executable.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        self.executable.chmod(0o755)
        (self.package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin").mkdir(parents=True)
        self.vendor_binary = self.package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
        self.vendor_binary.write_bytes(b"synthetic executable")
        self.vendor_binary.chmod(0o755)
        self.write_manifests()

    def tearDown(self):
        self.temp.cleanup()

    def write_manifests(self, *, name="@openai/codex", version="0.165.0",
                         dependency_spec="npm:@openai/codex@0.165.0-linux-x64",
                         dep_name="@openai/codex", dep_version="0.165.0-linux-x64",
                         dep_os=None, dep_cpu=None, bin_entry="bin/codex.js"):
        manifest = {
            "name": name,
            "version": version,
            "bin": {"codex": bin_entry},
            "optionalDependencies": {"@openai/codex-linux-x64": dependency_spec},
        }
        (self.package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        dependency_manifest = {
            "name": dep_name,
            "version": dep_version,
            "os": dep_os or ["linux"],
            "cpu": dep_cpu or ["x64"],
        }
        (self.package / "node_modules/@openai/codex-linux-x64/package.json").write_text(
            json.dumps(dependency_manifest), encoding="utf-8")

    def test_npm_bin_symlink_resolves_to_verified_package_and_dependency(self):
        public_bin = self.base / "bin/codex"
        public_bin.parent.mkdir()
        public_bin.symlink_to(self.executable)

        layout = isolated_entry.codex_install_layout(public_bin)

        self.assertEqual(layout["executable"], self.executable)
        self.assertEqual(layout["package_root"], self.package)
        self.assertEqual(layout["dependency"], self.package / "node_modules/@openai/codex-linux-x64")
        self.assertEqual(layout["dependency_name"], "codex-linux-x64")

    def test_system_executable_does_not_add_an_extra_mount(self):
        layout = isolated_entry.codex_install_layout("/usr/bin/true")
        self.assertTrue(layout["executable"].is_relative_to("/usr"))
        self.assertIsNone(layout["package_root"])
        self.assertIsNone(isolated_entry.validate_codex_node(layout, None))

    def test_npm_wrapper_requires_one_executable_node_runtime(self):
        layout = isolated_entry.codex_install_layout(self.executable)
        with self.assertRaisesRegex(ValueError, "Node.js runtime"):
            isolated_entry.validate_codex_node(layout, None)

        node = self.base / "node"
        node.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
        node.chmod(0o755)
        self.assertEqual(isolated_entry.validate_codex_node(layout, node), node.resolve())

        node.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "not executable"):
            isolated_entry.validate_codex_node(layout, node)

    def test_only_external_node_file_is_mounted_and_runtime_node_is_rejected(self):
        layout = isolated_entry.codex_install_layout(self.executable)
        node = self.base / "host-runtime/node"
        node.parent.mkdir()
        node.write_text("synthetic node runtime", encoding="utf-8")
        node.chmod(0o755)
        events = []
        isolated_entry.mount_codex_node(node.resolve(), self.base / "runner",
                                        lambda *args: events.append(args))
        self.assertEqual(events, [(node.resolve(), node.resolve())])

        with self.assertRaisesRegex(ValueError, "isolated runtime tree"):
            isolated_entry.mount_codex_node(node.resolve(), node.parent,
                                            lambda *args: self.fail("must not mount"))

    def test_mount_plan_masks_node_modules_and_exposes_one_readonly_dependency(self):
        layout = isolated_entry.codex_install_layout(self.executable)
        jail = self.base / "jail"
        jail.mkdir()
        events = []

        def target(path):
            return jail / str(path).lstrip("/")

        def mount(*args):
            events.append(("mount", args))

        def bind(source, destination, readonly=True):
            events.append(("bind", Path(source), Path(destination), readonly))
            target(destination).mkdir(parents=True, exist_ok=True)
            if Path(destination) == layout["package_root"]:
                (target(destination) / "node_modules").mkdir()

        isolated_entry.mount_codex_package(layout, target, mount, bind)

        self.assertEqual(events[0], ("bind", self.package, self.package, True))
        self.assertEqual(events[1][0], "mount")
        self.assertEqual(events[1][1][:2], ("-t", "tmpfs"))
        self.assertIn("mode=0755,nosuid,nodev,noexec,size=64k", events[1][1])
        self.assertEqual(events[2], (
            "bind", layout["dependency"],
            self.package / "node_modules/@openai/codex-linux-x64", True))
        self.assertEqual(events[3][0], "mount")
        self.assertEqual(events[3][1][:2], ("-o", "remount,ro,nosuid,nodev,noexec"))
        self.assertEqual(len([event for event in events if event[0] == "bind"]), 2)

    def test_missing_or_mismatched_package_parts_fail_closed(self):
        cases = [
            ("wrong package identity", lambda: self.write_manifests(name="attacker/codex")),
            ("wrong executable entry", lambda: self.write_manifests(bin_entry="bin/other.js")),
            ("wrong optional alias", lambda: self.write_manifests(dependency_spec="*")),
            ("wrong dependency identity", lambda: self.write_manifests(dep_name="attacker/codex")),
            ("wrong dependency version", lambda: self.write_manifests(dep_version="0.1.0")),
            ("wrong dependency architecture", lambda: self.write_manifests(dep_cpu=["arm64"])),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                self.write_manifests()
                mutate()
                with self.assertRaises(ValueError):
                    isolated_entry.codex_install_layout(self.executable)

    def test_missing_dependency_and_symlinked_dependency_fail_closed(self):
        dependency = self.package / "node_modules/@openai/codex-linux-x64"
        dependency.rename(dependency.with_name("codex-linux-x64.saved"))
        with self.assertRaises(ValueError):
            isolated_entry.codex_install_layout(self.executable)
        dependency.with_name("codex-linux-x64.saved").rename(dependency)

        outside = self.base / "outside"
        outside.mkdir()
        dependency.rename(dependency.with_name("codex-linux-x64.saved"))
        dependency.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            isolated_entry.codex_install_layout(self.executable)

    def test_platform_executable_symlink_escape_fails_closed(self):
        vendor = self.vendor_binary
        outside = self.base / "outside-codex"
        outside.write_bytes(b"synthetic external executable")
        outside.chmod(0o755)
        vendor.unlink()
        vendor.symlink_to(outside)
        with self.assertRaises(ValueError):
            isolated_entry.codex_install_layout(self.executable)

    def test_unrelated_executable_and_symlink_escape_are_rejected(self):
        unrelated = self.base / "not-a-codex"
        unrelated.write_text("fixture", encoding="utf-8")
        unrelated.chmod(0o755)
        with self.assertRaises(ValueError):
            isolated_entry.codex_install_layout(unrelated)

        escaped = self.base / "node_modules/@openai/codex/bin/escape.js"
        escaped.symlink_to(unrelated)
        with self.assertRaises(ValueError):
            isolated_entry.codex_install_layout(escaped)

    def test_mount_failure_propagates_before_any_exec_can_follow(self):
        layout = isolated_entry.codex_install_layout(self.executable)
        jail = self.base / "jail"
        jail.mkdir()
        calls = []

        def target(path):
            return jail / str(path).lstrip("/")

        def bind(source, destination, readonly=True):
            calls.append("bind")
            target(destination).mkdir(parents=True, exist_ok=True)
            if Path(destination) == layout["package_root"]:
                (target(destination) / "node_modules").mkdir()

        def mount(*args):
            calls.append("mount")
            raise OSError("synthetic mount refusal")

        with self.assertRaises(OSError):
            isolated_entry.mount_codex_package(layout, target, mount, bind)
        self.assertEqual(calls, ["bind", "mount"])


if __name__ == "__main__":
    unittest.main()
