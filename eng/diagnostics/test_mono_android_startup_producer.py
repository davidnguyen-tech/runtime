# Licensed to the .NET Foundation under one or more agreements.
# The .NET Foundation licenses this file to you under the MIT license.

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

SCRIPT = Path(__file__).with_name("produce-mono-android-startup.py")
ROOT = SCRIPT.parents[2]
spec = importlib.util.spec_from_file_location("producer", SCRIPT)
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)
SOURCE = "a" * 40
IMAGE = producer.IMAGE_PREFIX + "b" * 64


class ProducerTests(unittest.TestCase):
    def recipe(self, **overrides):
        args = {"source": SOURCE, "run_id": "123.1", "image": IMAGE, "enabled": True}
        args.update(overrides)
        return producer.plan(**args)

    def test_disabled(self):
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.recipe(enabled=False)

    def test_exact_inputs(self):
        for field, values in {
            "source": ["main", "a" * 39, "A" * 40, SOURCE + ";echo", SOURCE + "\n", SOURCE + "\r", SOURCE + "\r\n"],
            "run_id": ["123", "0.1", "1.0", "123.01", "1.1;echo", "9999999999999.1", "123.1\n", "123.1\r", "123.1\r\n"],
            "image": ["latest", producer.IMAGE_PREFIX + "A" * 64, IMAGE + " suffix", IMAGE + "\n", IMAGE + "\r", IMAGE + "\r\n"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.recipe(**{field: value})

    def test_two_normal_rids_and_versions(self):
        recipe = self.recipe()
        self.assertEqual(recipe["rids"], ["android-x64", "android-arm64"])
        self.assertEqual(len(recipe["commands"]), 6)
        self.assertEqual(recipe["version"], "10.0.12-startup.123.1.saaaaaaaaaaaa")
        self.assertTrue(self.recipe(source="0" * 40)["version"].endswith(".s000000000000"))
        self.assertNotEqual(recipe["version"], self.recipe(run_id="123.2")["version"])
        for command in recipe["commands"]:
            self.assertIn("/p:Version=" + recipe["version"], command["argv"])
            self.assertIn("/p:PackageVersion=" + recipe["version"], command["argv"])
            self.assertIn("/p:MonoAndroidStartupBuildId=" + recipe["marker"], command["argv"])
            self.assertIn("/p:NuGetAudit=true", command["argv"])
            self.assertIn("/p:NuGetAuditMode=all", command["argv"])
        for command in recipe["commands"][2::3]:
            self.assertEqual(command["argv"][:4], ["bash", "./dotnet.sh", "build", producer.PACK_PROJECT])
        self.assertNotIn("libs.pretest", producer.SUBSET)
        self.assertFalse(recipe["publication"])
        self.assertFalse(recipe["signing"])

    def test_real_pipeline_graph(self):
        # JSON is a YAML subset, permitting dependency-free parsing of this deliberately
        # template-free graph. These local branch checks are not Azure compilation.
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        self.assertEqual(graph["trigger"], "none")
        self.assertEqual(graph["pr"], "none")
        params = {p["name"]: p["default"] for p in graph["parameters"]}
        self.assertFalse(params["enableProducer"])
        condition = "${{ if eq(parameters.enableProducer, true) }}"
        disabled = "${{ if eq(parameters.enableProducer, false) }}"
        self.assertEqual(list(graph["stages"][0]), [disabled])
        self.assertEqual(list(graph["stages"][1]), [condition])
        for enabled in (False, True):
            stages = graph["stages"][1][condition] if enabled else graph["stages"][0][disabled]
            self.assertEqual(len(stages), 1)
            self.assertGreater(len(stages[0]["jobs"]), 0)
        guard = graph["stages"][0][disabled][0]["jobs"][0]
        self.assertNotIn("container", guard)
        self.assertEqual(guard["steps"], [
            {"checkout": "none"},
            {"bash": "printf '%s\\n' 'Diagnostic producer is disabled; no build or package action.'",
             "displayName": "Report disabled state"},
        ])
        stage = graph["stages"][1][condition][0]
        self.assertEqual(stage["condition"], "and(succeeded(), eq(variables['Build.Reason'], 'Manual'))")
        self.assertEqual(len(stage["jobs"]), 2)
        preflight, job = stage["jobs"]
        self.assertNotIn("container", preflight)
        self.assertIn("--plan-only", preflight["steps"][1]["bash"])
        self.assertEqual(job["dependsOn"], preflight["job"])
        self.assertEqual(job["container"], "${{ parameters.containerImage }}")
        self.assertEqual(len(job["steps"]), 3)
        self.assertFalse(job["steps"][0]["persistCredentials"])
        self.assertEqual(job["steps"][2]["task"], "PublishPipelineArtifact@1")
        self.assertIn("produce-mono-android-startup.py --enable", job["steps"][1]["bash"])
        self.assertNotIn("template", json.dumps(graph).lower())

    def test_marker_forward_and_invalidation(self):
        project = ET.parse(ROOT / "src/mono/mono.proj")
        forwarded = [node for node in project.iter("_MonoCMakeArgs")
                     if node.attrib.get("Include") == "-DMONO_ANDROID_STARTUP_BUILD_ID=$(MonoAndroidStartupBuildId)"]
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0].attrib["Condition"], "'$(TargetsAndroid)' == 'true'")
        check = next(node for node in project.iter("Target") if node.attrib["Name"] == "CheckEnv")
        self.assertTrue(any("MonoAndroidStartupBuildId" in node.attrib.get("Condition", "") for node in check))

    def test_real_msbuild_marker_gate(self):
        dotnet = ROOT / ".dotnet" / ("dotnet.exe" if producer.os.name == "nt" else "dotnet")
        self.assertTrue(dotnet.is_file(), "Run the native baseline build to initialize the repository SDK first")
        env = dict(producer.os.environ, NUGET_PACKAGES=str(ROOT / ".packages"), use_global_nuget_cache="false")
        for marker, valid in [("", True), ("runtime-4271-test", True), ("a" * 64, True),
                              ("a" * 65, False), ("INVALID", False), ("valid%0A", False),
                              ("valid%0D", False), ("valid%0D%0A", False)]:
            with self.subTest(marker=marker):
                result = producer.subprocess.run([
                    str(dotnet), "msbuild", str(ROOT / "src/mono/mono.proj"), "/t:CheckEnv",
                    "/p:MonoAndroidStartupBuildId=" + marker, "/v:quiet", "/nologo",
                ], cwd=ROOT, env=env, stdout=producer.subprocess.PIPE, stderr=producer.subprocess.STDOUT, text=True)
                self.assertEqual(result.returncode == 0, valid, result.stdout)
                if not valid:
                    self.assertIn("MonoAndroidStartupBuildId must be a bounded lowercase producer marker", result.stdout)

    def package(self, folder, rid="android-x64", changes=None):
        recipe = self.recipe()
        native = f"runtimes/{rid}/native/"
        header = bytearray(20)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = producer.RID_ARCH[rid][1].to_bytes(2, "little")
        files = {
            f"{native}libmonosgen-2.0.so": bytes(header),
            f"{native}libmonosgen-2.0.a": b"!<arch>\n",
            f"{native}libmono-component-debugger.so": bytes(header) + recipe["marker"].encode() + b"\0",
            f"{native}libSystem.Native.so": bytes(header),
            f"runtimes/{rid}/lib/net10.0/System.Private.CoreLib.dll": b"synthetic-test-only",
        }
        listed = "".join(f'<File Path="{name}" />' for name in files)
        files["data/RuntimeList.xml"] = (
            '<FileList FrameworkName="Microsoft.NETCore.App" TargetFrameworkVersion="10.0">' +
            listed + "</FileList>").encode()
        package_id = f"Microsoft.NETCore.App.Runtime.Mono.{rid}"
        files[f"{package_id}.nuspec"] = (
            f"<package><metadata><id>{package_id}</id><version>{recipe['version']}</version>"
            "</metadata></package>").encode()
        for name, value in (changes or {}).items():
            if value is None:
                files.pop(name)
            else:
                files[name] = value
        path = folder / f"{package_id}.{recipe['version']}.nupkg"
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        return path

    def test_actual_zip_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            for rid in producer.RID_ARCH:
                path = self.package(Path(folder), rid)
                result = producer.inventory_package(path, rid, self.recipe()["version"], self.recipe()["marker"])
                self.assertEqual(set(result), {"schemaVersion", "kind", "id", "version", "rid", "fileName",
                                               "sizeBytes", "sha256", "signatureEntryPresent", "entries"})
                self.assertEqual(result["schemaVersion"], 1)
                self.assertEqual(result["kind"], "guest-runtime-package-inventory")
                self.assertFalse(result["signatureEntryPresent"])
                self.assertEqual(len(result["entries"]), 7)
                self.assertEqual(result["sha256"], producer.sha256(path))
                self.assertEqual([entry["path"] for entry in result["entries"]],
                                 sorted(entry["path"] for entry in result["entries"]))

    def test_signature_receipt_with_explicit_test_mock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = self.package(root)
            inventory = producer.inventory_package(package, "android-x64", self.recipe()["version"], self.recipe()["marker"])
            def test_only_verify(command, **kwargs):
                self.assertEqual(command, ["test-only-dotnet", "nuget", "verify", "--all", package.name])
                kwargs["stdout"].write(b"TEST MOCK: unsigned synthetic ZIP\n")
                return producer.subprocess.CompletedProcess(command, 1)
            with patch.object(producer.subprocess, "run", side_effect=test_only_verify):
                observations = []
                result = producer.verify_signature("test-only-dotnet", package, inventory, {}, root, "test-only-sdk", observations)
            self.assertEqual(set(result), {"schemaVersion", "kind", "packageSha256", "signatureEntryPresent",
                                          "verificationExitCode", "classification", "toolVersion", "arguments",
                                          "outputFileName", "outputSha256"})
            self.assertEqual(result["classification"], "unsigned")
            self.assertEqual(result["verificationExitCode"], 1)
            self.assertEqual(result["packageSha256"], inventory["sha256"])
            self.assertEqual(result["outputSha256"], producer.sha256(root / result["outputFileName"]))
            self.assertEqual(observations[0]["argv"], ["test-only-dotnet", *result["arguments"]])
            self.assertEqual(observations[0]["exitCode"], result["verificationExitCode"])
            self.assertEqual(observations[0]["logSha256"], result["outputSha256"])

    def test_reject_wrong_or_incomplete_pack(self):
        native = "runtimes/android-x64/native/"
        mutations = [
            {native + "libmonosgen-2.0.so": None},
            {native + "libmonosgen-2.0.so": b"not-elf"},
            {native + "libmono-component-debugger.so": b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\0"},
            {"data/RuntimeList.xml": b'<FileList FrameworkName="Wrong" />'},
            {".signature.p7s": b"unverified-signature"},
            {"../outside": b"unsafe"},
            {"a//b": b"unsafe"}, {"a/./b": b"unsafe"}, {"C:drive": b"unsafe"},
            {"bad\npath": b"unsafe"},
            {"folder": b"file", "folder/member": b"conflict"},
            {"fold": b"a", "FOLD": b"b"},
            {"directory/": b"nonempty"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            for changes in mutations:
                path = self.package(Path(folder), changes=changes)
                with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                    producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])
            path = self.package(Path(folder))
            with self.assertRaises(ValueError):
                producer.inventory_package(path, "android-arm64", self.recipe()["version"], self.recipe()["marker"])

    def test_zip_bounds_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = self.package(root)
            for constant, limit in [("MAX_ENTRIES", 6), ("MAX_PACKAGE_BYTES", 8), ("MAX_ENTRY_BYTES", 8)]:
                with self.subTest(constant=constant), patch.object(producer, constant, limit), self.assertRaises(ValueError):
                    producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (producer.stat.S_IFLNK | 0o777) << 16
            path = self.package(root, changes={link: b"target"})
            with self.assertRaisesRegex(ValueError, "symlink"):
                producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])
            path = self.package(root, changes={"directory/": b""})
            inventory = producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])
            directory = next(entry for entry in inventory["entries"] if entry["path"] == "directory/")
            self.assertEqual(directory["sizeBytes"], 0)
            self.assertEqual(directory["sha256"], producer.hashlib.sha256(b"").hexdigest())
            with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("compressed-large", b"x" * 16384)
            with patch.object(producer, "MAX_PACKAGE_BYTES", path.stat().st_size + 1), self.assertRaisesRegex(ValueError, "contents exceed"):
                producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])

    def test_reject_encrypted_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.package(Path(folder))
            raw = bytearray(path.read_bytes())
            local = raw.index(b"PK\x03\x04")
            central = raw.index(b"PK\x01\x02")
            raw[local + 6] |= 1
            raw[central + 8] |= 1
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "Encrypted"):
                producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])

    def test_reject_raw_zip_backslash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.package(Path(folder), changes={"back/slash": b"unsafe"})
            raw = path.read_bytes()
            self.assertEqual(raw.count(b"back/slash"), 2)
            path.write_bytes(raw.replace(b"back/slash", b"back\\slash"))
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.infolist()[-1].orig_filename, "back\\slash")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])

    def test_execution_failure_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            observations = []
            command = [producer.sys.executable, "-c", "print('exit-seven'); raise SystemExit(7)"]
            with self.assertRaisesRegex(ValueError, "failed with exit 7"):
                producer.execute(command, Path(folder), dict(producer.os.environ), Path(folder), "failure", observations)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0], {
                "name": "failure", "argv": command, "cwd": folder, "status": "failed",
                "exitCode": 7, "logFileName": "failure.log",
                "logSha256": producer.sha256(Path(folder) / "failure.log"),
            })
            self.assertIn("exit-seven", (Path(folder) / "failure.log").read_text())

    def test_execution_success_and_launch_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            observations = []
            command = [producer.sys.executable, "-c", "print('actual-success')"]
            log = producer.execute(command, root, dict(producer.os.environ), root, "success", observations)
            self.assertEqual(observations[0], {
                "name": "success", "argv": command, "cwd": folder, "status": "completed",
                "exitCode": 0, "logFileName": "success.log", "logSha256": producer.sha256(log),
            })
            missing = [str(root / "nonexistent-executable")]
            with self.assertRaises(FileNotFoundError):
                producer.execute(missing, root, dict(producer.os.environ), root, "launch", observations)
            self.assertEqual(len(observations), 2)
            launch = observations[1]
            self.assertEqual(launch["argv"], missing)
            self.assertEqual(launch["status"], "launch-failed")
            self.assertIsNone(launch["exitCode"])
            self.assertEqual(launch["logFileName"], "launch.log")
            self.assertEqual(launch["logSha256"], producer.sha256(root / "launch.log"))
            self.assertTrue(launch["error"])

    def test_failed_producer_persists_actual_command_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(producer.sys, "platform", "linux"), patch.dict(producer.os.environ, {
                    "BUILD_REASON": "Manual", "BUILD_BUILDID": "123", "SYSTEM_JOBATTEMPT": "1"}):
                with self.assertRaisesRegex(ValueError, "source-head failed"):
                    producer.produce(root, self.recipe(), root / "missing-ndk", "28.2.13676358")
            output = root / "artifacts/startup-metadata-producer"
            receipt = json.loads((output / "receipt.json").read_text())
            self.assertEqual(receipt["schemaVersion"], 2)
            self.assertEqual(receipt["status"], "failed")
            observation, = receipt["command_observations"]
            self.assertEqual(observation["argv"], ["git", "rev-parse", "HEAD"])
            self.assertEqual(observation["status"], "failed")
            self.assertNotEqual(observation["exitCode"], 0)
            self.assertEqual(observation["logSha256"], producer.sha256(output / observation["logFileName"]))
            self.assertEqual(receipt["python_version"], producer.sys.version.split()[0])

    def test_python_minimum_and_streaming_hash(self):
        with patch.object(producer.sys, "version_info", (3, 8, 0)), self.assertRaisesRegex(ValueError, "Python 3.9"):
            producer.main()
        data = b"streaming-hash-fixture" * 10000
        with patch.object(producer.hashlib, "file_digest", create=True, side_effect=AssertionError("Requires Python 3.11")):
            self.assertEqual(producer.stream_sha256(io.BytesIO(data)), producer.hashlib.sha256(data).hexdigest())

    def test_producer_refuses_nonmanual(self):
        with patch.object(producer.sys, "platform", "linux"), patch.dict(producer.os.environ, {"BUILD_REASON": "PullRequest"}):
            with self.assertRaisesRegex(ValueError, "manual"):
                producer.produce(ROOT, self.recipe(), Path("missing"), "28.2.13676358")


if __name__ == "__main__":
    unittest.main()
