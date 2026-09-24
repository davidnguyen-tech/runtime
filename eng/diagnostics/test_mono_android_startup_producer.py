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
IMAGE = producer.PRODUCER_IMAGE


class ProducerTests(unittest.TestCase):
    def recipe(self, **overrides):
        args = {"source": SOURCE, "run_id": "123.1", "image": IMAGE, "enabled": True}
        args.update(overrides)
        return producer.plan(**args)

    def pipeline_environment(self, job="BuildRuntimePacks"):
        return {"BUILD_REASON": "Manual", "SYSTEM_TEAMPROJECT": "internal",
                "BUILD_REPOSITORY_ID": producer.REPOSITORY_ID, "SYSTEM_DEFINITIONID": "679",
                "BUILD_SOURCEVERSION": SOURCE, "BUILD_SOURCEBRANCH": "refs/heads/test-only-diagnostics",
                "BUILD_BUILDID": "123", "SYSTEM_JOBATTEMPT": "1", "SYSTEM_JOBNAME": job,
                "BUILD_BUILDNUMBER": "20260924.1", "STARTUP_EXPERIMENT_ATTEMPT": "1"}

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
        # Compare the default root literally with its exact baseline. Parse the
        # diagnostic stage's JSON/YAML subset, without pretending to expand 1ES.
        baseline = producer.subprocess.run([
            "git", "show", producer.BASELINE + ":eng/pipelines/runtime-official.yml",
        ], cwd=ROOT, env=dict(producer.os.environ), stdout=producer.subprocess.PIPE,
            stderr=producer.subprocess.PIPE, text=True)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        original = baseline.stdout
        current = (ROOT / producer.PIPELINE_PATH).read_text()
        self.assertTrue(current.startswith(original.split("variables:\n")[0]))
        image_flag = "    enableMonoStartupMetadata: ${{ parameters.enableMonoStartupMetadata }}\n"
        self.assertEqual(current.count(image_flag), 1)
        self.assertEqual(current.replace(image_flag, "").split("variables:\n", 1)[1].split("    stages:\n", 1)[0],
                         original.split("variables:\n", 1)[1].split("    stages:\n", 1)[0])
        self.assertIn("name: enableMonoStartupMetadata", current)
        self.assertIn("  type: boolean\n  default: false\n", current)
        enabled, disabled = current.split("    stages:\n", 1)[1].split(
            "    - ${{ if eq(parameters.enableMonoStartupMetadata, false) }}:\n")
        self.assertEqual(enabled,
                         "    - ${{ if eq(parameters.enableMonoStartupMetadata, true) }}:\n"
                         "      - template: /eng/pipelines/mono-android-startup-metadata.yml\n"
                         "        parameters:\n"
                         "          sourceCommit: ${{ parameters.monoStartupSourceCommit }}\n"
                         "          attempt: ${{ parameters.monoStartupAttempt }}\n")
        default_stages = "\n".join(line[2:] if line else "" for line in disabled.splitlines())
        self.assertEqual(default_stages, original.split("    stages:\n", 1)[1].rstrip("\n"))
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        self.assertEqual(set(graph), {"parameters", "stages"})
        self.assertEqual(graph["parameters"], [{"name": "sourceCommit", "type": "string"},
                                               {"name": "attempt", "type": "number", "default": 1}])
        stage, = graph["stages"]
        self.assertEqual(stage["condition"], "and(succeeded(), eq(variables['Build.Reason'], 'Manual'))")
        self.assertEqual(len(stage["jobs"]), 3)
        for job in stage["jobs"]:
            self.assertEqual(job["template"], "/eng/common/templates-official/job/job.yml")
        preflight, build, sign = [job["parameters"] for job in stage["jobs"]]
        self.assertNotIn("container", preflight)
        self.assertIn("--plan-only", preflight["steps"][1]["bash"])
        self.assertIn("--validate-pipeline", preflight["steps"][1]["bash"])
        self.assertEqual(build["dependsOn"], preflight["name"])
        self.assertEqual(sign["dependsOn"], build["name"])
        self.assertEqual(build["container"], "android")
        self.assertEqual(build["steps"][0]["env"]["REVIEWED_SOURCE"], "${{ parameters.sourceCommit }}")
        self.assertEqual(sign["steps"][0]["env"]["REVIEWED_SOURCE"], "${{ parameters.sourceCommit }}")
        self.assertEqual(build["steps"][0]["name"], "BuildEvidence")
        self.assertIn("variable=ProducerAttempt;isOutput=true", build["steps"][0]["bash"])
        self.assertIn("--ndk-revision 27.2.12479018", build["steps"][0]["bash"])
        self.assertEqual(sign["artifacts"]["download"]["name"],
                         "mono-android-startup-unsigned-unadmitted-$(ProducerAttempt)")
        variables = {item["name"]: item["value"] for item in sign["variables"]}
        self.assertEqual(variables["ProducerAttempt"],
                         "$[ dependencies.BuildRuntimePacks.outputs['BuildEvidence.ProducerAttempt'] ]")
        self.assertEqual(variables["_SignType"], "test")
        self.assertTrue(sign["enableMicrobuild"])
        self.assertFalse(sign["microbuildUseESRP"])
        self.assertFalse(sign["enableMicrobuildForMacAndLinux"])
        self.assertFalse(sign["enablePublishing"])
        self.assertFalse(sign["enablePublishBuildAssets"])
        for job in (build, sign):
            self.assertEqual(job["steps"][0]["env"]["STARTUP_EXPERIMENT_ATTEMPT"], "${{ parameters.attempt }}")
            self.assertEqual([step["checkout"] for step in job["preSteps"]], ["self", "1ESPipelineTemplates"])
            self.assertTrue(all(not step["persistCredentials"] for step in job["preSteps"]))
            output, = job["templateContext"]["outputs"]
            self.assertEqual(output["output"], "pipelineArtifact")
            self.assertEqual(output["condition"], "succeededOrFailed()")
            self.assertFalse(output["isProduction"])
        self.assertNotIn("publish-build-assets.yml", json.dumps(graph))
        self.assertNotIn("ESRP", json.dumps(graph).replace("microbuildUseESRP", ""))
        plugin = (ROOT / "eng/common/core-templates/steps/install-microbuild.yml").read_text()
        self.assertIn("microbuildUseESRP", plugin)
        self.assertIn("in(variables['_SignType'], 'real', 'test')", plugin)

    def test_diagnostic_android_resource_default_matrix(self):
        path = "eng/pipelines/common/templates/pipeline-with-resources.yml"
        baseline = producer.subprocess.run([
            "git", "show", producer.BASELINE + ":" + path,
        ], cwd=ROOT, env=dict(producer.os.environ), stdout=producer.subprocess.PIPE,
            stderr=producer.subprocess.PIPE, text=True)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        current = (ROOT / path).read_text()
        parameter = "  - name: enableMonoStartupMetadata\n    type: boolean\n    default: false\n"
        self.assertEqual(current.count(parameter), 1)
        normal_image = producer.IMAGE_PREFIX.replace("@sha256:", ":") + "azurelinux-3.0-net10.0-cross-android-amd64"
        normal = f"      android:\n        image: {normal_image}\n"
        conditional = (
            "      android:\n"
            "        ${{ if and(parameters.isOfficialBuild, parameters.enableMonoStartupMetadata) }}:\n"
            f"          image: {producer.PRODUCER_IMAGE}\n"
            "        ${{ else }}:\n"
            f"          image: {normal_image}\n"
        )
        self.assertEqual(current.count(conditional), 1)
        forwarding = "      enableMonoStartupMetadata: ${{ parameters.enableMonoStartupMetadata }}\n"
        self.assertEqual(current.count(forwarding), 1)
        self.assertIn("    ${{ if parameters.isOfficialBuild }}:\n"
                      "      templatePath: template1es.yml\n" + forwarding +
                      "    ${{ else }}:\n"
                      "      templatePath: templatePublic.yml\n", current)
        for official in (False, True):
            for enabled in (False, True):
                with self.subTest(official=official, enabled=enabled):
                    image = producer.PRODUCER_IMAGE if official and enabled else normal_image
                    resolved = current.replace(parameter, "").replace(forwarding, "").replace(
                        conditional, f"      android:\n        image: {image}\n")
                    self.assertEqual(resolved, baseline.stdout.replace(normal, f"      android:\n        image: {image}\n"))
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        stage, = graph["stages"]
        self.assertEqual(next(item["value"] for item in stage["variables"] if item["name"] == "StartupMetadataImage"),
                         producer.PRODUCER_IMAGE)
        self.assertNotIn("enableMonoStartupMetadata", current.replace(forwarding, "").split(
            "    containers:\n", 1)[0].split("extends:\n", 1)[1])

    def test_host_checkout_and_container_path_execution(self):
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        jobs = [job["parameters"] for job in graph["stages"][0]["jobs"]]
        build = next(job for job in jobs if job["name"] == "BuildRuntimePacks")
        sign = next(job for job in jobs if job["name"] == "TestSignRuntimePacks")
        for checkout in build["preSteps"]:
            self.assertEqual(checkout["target"], {"container": "host"})
        self.assertEqual([step["path"] for step in build["preSteps"]], ["s", "startup-templates"])
        step, = build["steps"]
        self.assertEqual(step["target"], {"container": "android"})
        self.assertNotIn("workingDirectory", step)
        self.assertNotIn("PRODUCER_OUTPUT", step["env"])
        self.assertNotIn("STARTUP_1ES_ROOT", step["env"])
        self.assertNotIn("$(", step["bash"])
        self.assertNotIn("container", sign)
        self.assertEqual(sign["steps"][0]["env"]["PRODUCER_OUTPUT"],
                         "$(Build.ArtifactStagingDirectory)/mono-startup-test-signed")
        self.assertEqual(sign["steps"][0]["env"]["STARTUP_1ES_ROOT"], "$(Pipeline.Workspace)/startup-templates")
        self.assertEqual(build["templateContext"]["outputs"][0]["targetPath"],
                         "$(Build.ArtifactStagingDirectory)/mono-startup-build")
        bash = producer.shutil.which("bash")
        self.assertIsNotNone(bash, "An existing Bash is required to execute the actual Linux pipeline script")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "mapped source"
            workspace = root / "mapped workspace"
            staging = root / "mapped staging"
            for path in (source, workspace, staging):
                path.mkdir()
            capture = root / "invocation.txt"
            # Mock only the expensive producer boundary; execute the actual YAML Bash.
            probe = (
                'python3() {\n'
                '  [[ "$PWD" -ef "$BUILD_SOURCESDIRECTORY" ]] || return 91\n'
                '  printf "%s\\n" "$PWD" "$STARTUP_1ES_ROOT" "$@" > "$CAPTURE_FILE"\n'
                '  return "${PROBE_EXIT:-0}"\n'
                '}\n' + step["bash"]
            )
            script = root / "probe.sh"
            with script.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(probe)
            env = dict(producer.os.environ, BUILD_SOURCESDIRECTORY=source.as_posix(),
                       PIPELINE_WORKSPACE=workspace.as_posix(), BUILD_ARTIFACTSTAGINGDIRECTORY=staging.as_posix(),
                       CAPTURE_FILE=capture.as_posix(), REVIEWED_SOURCE=SOURCE, REVIEWED_CONTAINER=IMAGE,
                       BUILD_BUILDID="123", STARTUP_EXPERIMENT_ATTEMPT="1", SYSTEM_JOBATTEMPT="2",
                       ANDROID_NDK_ROOT=(root / "toolchain").as_posix(),
                       PRODUCER_OUTPUT="HOST-PATH-MUST-NOT-BE-USED",
                       STARTUP_1ES_ROOT="HOST-PATH-MUST-NOT-BE-USED")
            for failure in (None, "producer-exit", "BUILD_SOURCESDIRECTORY",
                            "PIPELINE_WORKSPACE", "BUILD_ARTIFACTSTAGINGDIRECTORY"):
                with self.subTest(failure=failure):
                    case_env = {**env, "PROBE_EXIT": "7" if failure == "producer-exit" else "0"}
                    if failure in ("BUILD_SOURCESDIRECTORY", "PIPELINE_WORKSPACE", "BUILD_ARTIFACTSTAGINGDIRECTORY"):
                        case_env[failure] = ""
                    if capture.exists():
                        capture.unlink()
                    result = producer.subprocess.run([bash, script.as_posix()], cwd=root, env=case_env,
                                                     stdout=producer.subprocess.PIPE, stderr=producer.subprocess.STDOUT, text=True)
                    if failure is None or failure == "producer-exit":
                        lines = capture.read_text().splitlines()
                        # Git Bash reports PWD as /d/... on Windows; check its directory leaf
                        # while asserting exact mapped inputs for all producer arguments.
                        self.assertTrue(lines[0].endswith("/mapped source"))
                        self.assertEqual(lines[1], workspace.as_posix() + "/startup-templates")
                        self.assertEqual(lines[2], "eng/diagnostics/produce-mono-android-startup.py")
                        self.assertEqual(lines[lines.index("--output") + 1], staging.as_posix() + "/mono-startup-build")
                        self.assertEqual(lines[lines.index("--run-id") + 1], "123.1")
                        self.assertEqual(lines[lines.index("--container") + 1], IMAGE)
                        self.assertNotIn("HOST-PATH-MUST-NOT-BE-USED", "\n".join(lines))
                    else:
                        self.assertFalse(capture.exists(), result.stdout)
                    if failure is None:
                        self.assertEqual(result.returncode, 0, result.stdout)
                        self.assertIn("variable=ProducerAttempt;isOutput=true]2", result.stdout)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn("variable=ProducerAttempt", result.stdout)
                        if failure == "producer-exit":
                            self.assertEqual(result.returncode, 7)

    def test_diagnostic_sdl_coverage_and_flag_dispatch(self):
        parameter = "  - name: enableMonoStartupMetadata\n    type: boolean\n    default: false\n"
        forwarding = (
            "\n    ${{ if and(eq(parameters.templatePath, 'template1es.yml'), parameters.enableMonoStartupMetadata) }}:\n"
            "      enableMonoStartupMetadata: true"
        )
        inclusion = (
            "      ${{ if parameters.enableMonoStartupMetadata }}:\n"
            "        sourceRepositoriesToScan:\n"
            "          include:\n"
            "          - repository: 1ESPipelineTemplates\n"
        )
        for name, addition in (("templateDispatch.yml", forwarding), ("template1es.yml", inclusion)):
            path = "eng/pipelines/common/templates/" + name
            baseline = producer.subprocess.run([
                "git", "show", producer.BASELINE + ":" + path,
            ], cwd=ROOT, env=dict(producer.os.environ), stdout=producer.subprocess.PIPE,
                stderr=producer.subprocess.PIPE, text=True)
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            current = (ROOT / path).read_text()
            self.assertEqual(current.count(parameter), 1)
            self.assertEqual(current.count(addition), 1)
            self.assertEqual(current.replace(parameter, "").replace(addition, ""), baseline.stdout)
            if name == "template1es.yml":
                self.assertIn("    sdl:\n" + inclusion + "      codeql:\n", current)
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        stage, = graph["stages"]
        checked_out = {step["checkout"] for job in stage["jobs"]
                       for step in job["parameters"].get("preSteps", [])}
        self.assertEqual(checked_out, {"self", "1ESPipelineTemplates"})

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
            f"{native}System.Private.CoreLib.dll": b"synthetic-test-only",
        }
        listed = "".join(f'<File Type="Native" Path="{name}" />' for name in files)
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
            {native + "System.Private.CoreLib.dll": None},
            {native + "System.Private.CoreLib.dll": None,
             "runtimes/android-x64/lib/net10.0/System.Private.CoreLib.dll": b"wrong-normal-Mono-layout"},
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
            with patch.object(producer.sys, "platform", "linux"), patch.dict(producer.os.environ, self.pipeline_environment()):
                with self.assertRaisesRegex(ValueError, "source-head failed"):
                    producer.produce(root, self.recipe(), root / "missing-ndk", "28.2.13676358")
            output = root / "artifacts/startup-metadata-producer"
            receipt = json.loads((output / "receipt.json").read_text())
            self.assertEqual(receipt["schemaVersion"], 3)
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

    def test_existing_definition_and_feature_branch_gate(self):
        with patch.dict(producer.os.environ, self.pipeline_environment()):
            identity = producer.pipeline_identity(SOURCE)
            self.assertEqual(identity["definitionId"], 679)
            self.assertEqual(identity["pipelineCommit"], SOURCE)
            for key, values in {
                "BUILD_REASON": ["IndividualCI", "PullRequest"],
                "SYSTEM_DEFINITIONID": ["1104", "1441", "679\n"],
                "BUILD_REPOSITORY_ID": ["wrong-repository"],
                "SYSTEM_TEAMPROJECT": ["public"],
                "BUILD_SOURCEVERSION": ["b" * 40],
                "BUILD_SOURCEBRANCH": ["refs/heads/main", "refs/heads/master", "refs/heads/release/10.0",
                                       "refs/heads/internal/release/10.0", "refs/pull/1/merge"],
                "BUILD_BUILDID": ["0", "001", "2147483648"],
                "SYSTEM_JOBATTEMPT": ["0", "1\n"],
            }.items():
                for value in values:
                    with self.subTest(key=key, value=value), patch.dict(producer.os.environ, {key: value}):
                        with self.assertRaises(ValueError):
                            producer.pipeline_identity(SOURCE)

    def configuration_fixture(self, folder, output, rid):
        directory = folder / f"artifacts/obj/mono/android.{producer.RID_ARCH[rid][0]}.Release"
        compiler = directory / "CMakeFiles/1.0"
        compiler.mkdir(parents=True)
        abi = "x86_64" if rid == "android-x64" else "arm64-v8a"
        (directory / "CMakeCache.txt").write_text(
            f"# SYNTHETIC TEST ONLY\nANDROID_ABI:UNINITIALIZED={abi}\nCMAKE_BUILD_TYPE:STRING=Release\n"
            f"MONO_ANDROID_STARTUP_BUILD_ID:STRING={self.recipe()['marker']}\n", encoding="ascii")
        for language in ("C", "CXX"):
            (compiler / f"CMake{language}Compiler.cmake").write_text("# SYNTHETIC COMPILER IDENTIFICATION\n", encoding="ascii")
        return producer.native_configuration(folder, output, rid, self.recipe()["marker"])

    def test_actual_configure_file_copy_and_marker_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            for rid in producer.RID_ARCH:
                configuration = self.configuration_fixture(root, output, rid)
                self.assertEqual(configuration["value"], self.recipe()["marker"])
                self.assertEqual(len(configuration["evidence"]), 3)
                for item in configuration["evidence"]:
                    self.assertEqual((root / item["sourcePath"]).read_bytes(), (output / item["fileName"]).read_bytes())
                    self.assertEqual(item["sha256"], producer.sha256(output / item["fileName"]))
                with self.assertRaisesRegex(ValueError, "cache marker"):
                    producer.native_configuration(root, output, rid, "wrong-marker")

    def test_signed_inventory_keeps_native_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.package(Path(folder), changes={".signature.p7s": b"SYNTHETIC-NOT-A-SIGNATURE"})
            result = producer.inventory_package(path, "android-x64", self.recipe()["version"],
                                                self.recipe()["marker"], signature_expected=True)
            self.assertTrue(result["signatureEntryPresent"])
            self.assertEqual(len(result["entries"]), 8)
            with self.assertRaisesRegex(ValueError, "signature-entry"):
                producer.inventory_package(path, "android-x64", self.recipe()["version"], self.recipe()["marker"])
            path = self.package(Path(folder))
            with self.assertRaisesRegex(ValueError, "signature-entry"):
                producer.inventory_package(path, "android-x64", self.recipe()["version"],
                                           self.recipe()["marker"], signature_expected=True)

    def test_normal_signing_scope_with_actual_msbuild(self):
        sdk = json.loads((ROOT / "global.json").read_text())["msbuild-sdks"]["Microsoft.DotNet.Arcade.Sdk"]
        sign_props = ROOT / ".packages/microsoft.dotnet.arcade.sdk" / sdk / "tools/Sign.props"
        self.assertTrue(sign_props.is_file(), "Native baseline must restore the pinned Arcade SDK")
        dotnet = ROOT / ".dotnet" / ("dotnet.exe" if producer.os.name == "nt" else "dotnet")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shipping = root / "packages/Release/Shipping"
            shipping.mkdir(parents=True)
            for rid in producer.RID_ARCH:
                package = self.package(shipping, rid)
                project = ET.Element("Project")
                properties = ET.SubElement(project, "PropertyGroup")
                for name, value in {
                    "TargetOS": "android", "TargetArchitecture": producer.RID_ARCH[rid][0],
                    "Configuration": "Release", "PostBuildSign": "false",
                    "RepositoryEngineeringDir": str(ROOT / "eng") + producer.os.sep,
                    "ArtifactsPackagesDir": str(root / "packages/Release") + producer.os.sep,
                    "ArtifactsShippingPackagesDir": str(shipping) + producer.os.sep,
                    "ArtifactsNonShippingPackagesDir": str(root / "packages/Release/NonShipping") + producer.os.sep,
                    "VisualStudioSetupOutputPath": str(root / "VSSetup/Release") + producer.os.sep,
                    "VisualStudioBuildPackagesDir": str(root / "VSSetup/Release/DevDivPackages") + producer.os.sep,
                }.items():
                    ET.SubElement(properties, name).text = value
                ET.SubElement(project, "Import", {"Project": str(sign_props)})
                project_path = root / "selection.proj"
                ET.ElementTree(project).write(project_path, encoding="utf-8")
                result = producer.subprocess.run([
                    str(dotnet), "msbuild", str(project_path), "-getItem:ItemsToSign", "-nologo", "-verbosity:quiet",
                ], cwd=ROOT, env=dict(producer.os.environ, NUGET_PACKAGES=str(ROOT / ".packages")),
                    stdout=producer.subprocess.PIPE, stderr=producer.subprocess.STDOUT, text=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                items = json.loads(result.stdout)["Items"]["ItemsToSign"]
                self.assertEqual(len(items), 1)
                self.assertEqual(Path(items[0]["FullPath"]).resolve(), package.resolve())
                package.unlink()

    def test_sfx_native_pdb_reader_target_scope_with_actual_msbuild(self):
        path = "src/installer/pkg/sfx/Microsoft.NETCore.App/Directory.Build.props"
        env = dict(producer.os.environ, NUGET_PACKAGES=str(ROOT / ".packages"))
        baseline = producer.subprocess.run(
            ["git", "show", producer.BASELINE + ":" + path], cwd=ROOT, env=env,
            stdout=producer.subprocess.PIPE, stderr=producer.subprocess.STDOUT, text=True)
        self.assertEqual(baseline.returncode, 0, baseline.stdout)
        dotnet = ROOT / ".dotnet" / ("dotnet.exe" if producer.os.name == "nt" else "dotnet")
        with tempfile.TemporaryDirectory() as folder:
            props = []
            for name, content in (("baseline", baseline.stdout), ("current", (ROOT / path).read_text())):
                # Preserve the original parent import while evaluating both real props
                # through DirectoryBuildPropsPath without modifying the checkout.
                content = content.replace("$(MSBuildThisFileDirectory)", str((ROOT / path).parent) + producer.os.sep)
                file = Path(folder) / (name + ".props")
                file.write_text(content, encoding="utf-8")
                props.append(file)
            for flavor, target, arch, source_only in (
                ("Mono", "android", "x64", False), ("Mono", "android", "arm64", False),
                ("Mono", "windows", "x64", False), ("Mono", "windows", "arm64", False),
                ("CoreCLR", "windows", "x64", False), ("CoreCLR", "windows", "arm64", False),
                ("Mono", "linux", "x64", False),
                ("Mono", "windows", "x64", True), ("Mono", "android", "x64", True),
            ):
                with self.subTest(flavor=flavor, target=target, arch=arch, source_only=source_only):
                    evaluations = []
                    for file in props:
                        project = (ROOT / path).parent / f"Microsoft.NETCore.App.Runtime.{flavor}.sfxproj"
                        result = producer.subprocess.run([
                            str(dotnet), "msbuild", str(project), f"/p:DirectoryBuildPropsPath={file}",
                            f"/p:TargetOS={target}", f"/p:TargetArchitecture={arch}",
                            f"/p:DotNetBuildSourceOnly={str(source_only).lower()}", "/p:Configuration=Release",
                            "-getItem:PackageReference,NativeRuntimeAsset", "-nologo", "-verbosity:quiet",
                        ], cwd=ROOT, env=env, stdout=producer.subprocess.PIPE,
                            stderr=producer.subprocess.STDOUT, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout)
                        items = json.loads(result.stdout)["Items"]
                        # Only the defining file differs between the two temporary imports.
                        for entries in items.values():
                            for item in entries:
                                for key in ("DefiningProjectFullPath", "DefiningProjectDirectory",
                                            "DefiningProjectName", "DefiningProjectExtension"):
                                    item.pop(key, None)
                        evaluations.append(items)
                    before, after = evaluations
                    dia = [item for item in before["PackageReference"]
                           if item["Identity"] == "Microsoft.DiaSymReader.Native"]
                    self.assertEqual(len(dia), 0 if source_only else 1)
                    if target != "windows" and not source_only:
                        before["PackageReference"] = [item for item in before["PackageReference"]
                                                      if item["Identity"] != "Microsoft.DiaSymReader.Native"]
                    self.assertEqual(after, before)
                    if target == "windows" and not source_only:
                        self.assertTrue(any("Microsoft.DiaSymReader.Native." in item["Identity"]
                                            for item in after["NativeRuntimeAsset"]))
                    elif target != "windows":
                        self.assertFalse(any("Microsoft.DiaSymReader.Native." in item["Identity"]
                                             for item in after["NativeRuntimeAsset"]))

    def test_actual_mono_sfx_corelib_classification(self):
        dotnet = ROOT / ".dotnet" / ("dotnet.exe" if producer.os.name == "nt" else "dotnet")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mono = root / "mono"
            mono.mkdir()
            (mono / "System.Private.CoreLib.dll").write_bytes(b"SYNTHETIC ASSET; METADATA TEST ONLY")
            project = ET.Element("Project")
            ET.SubElement(project, "Import", {"Project": str(ROOT / "eng/liveBuilds.targets")})
            path = root / "corelib-classification.proj"
            ET.ElementTree(project).write(path, encoding="utf-8")
            result = producer.subprocess.run([
                str(dotnet), "msbuild", str(path), "/t:ResolveRuntimeFilesFromLocalBuild",
                "/p:RuntimeFlavor=Mono", f"/p:MonoArtifactsPath={mono}{producer.os.sep}",
                "-getItem:RuntimeFiles", "-nologo", "-verbosity:quiet",
            ], cwd=ROOT, env=dict(producer.os.environ, NUGET_PACKAGES=str(ROOT / ".packages")),
                stdout=producer.subprocess.PIPE, stderr=producer.subprocess.STDOUT, text=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            item, = json.loads(result.stdout)["Items"]["RuntimeFiles"]
            self.assertEqual(item["Filename"] + item["Extension"], "System.Private.CoreLib.dll")
            self.assertEqual(item["IsNative"], "true")
        project = ET.parse(ROOT / "src/installer/pkg/sfx/Microsoft.NETCore.App/Microsoft.NETCore.App.Runtime.props")
        target = next(node for node in project.iter("Target") if node.attrib["Name"] == "AddRuntimeFilesToPackage")
        native = next(node for node in target.iter("RuntimeFiles") if node.attrib.get("Condition") == "'%(RuntimeFiles.IsNative)' == 'true'")
        self.assertEqual(native.find("TargetPath").text, "runtimes/$(RuntimeIdentifier)/native")

    def build_fixture(self, folder):
        folder.mkdir()
        packages = folder / "packages"
        packages.mkdir()
        recipe = self.recipe()
        with patch.dict(producer.os.environ, self.pipeline_environment()):
            identity = producer.pipeline_identity(SOURCE)
        receipt = {**recipe, "schemaVersion": 3, "kind": "mono-android-startup-build-receipt",
                   "status": "produced-unsigned-unadmitted", "pipeline": identity,
                   "packages": [], "package_sidecars": [], "command_observations": [], "native_configuration": {},
                   "experiment_attempt": 1,
                   "templates": [{"repository": "1ESPipelineTemplates/1ESPipelineTemplates", "commit": producer.EXPECTED_1ES_COMMIT,
                                  "path": "v1/1ES.Official.PipelineTemplate.yml", "sha256": "d" * 64}]}
        (folder / "source.patch").write_text("SYNTHETIC TEST INPUT, NOT AN ACTUAL SOURCE PATCH\n", encoding="ascii")
        receipt["patch_sha256"] = producer.sha256(folder / "source.patch")
        producer.write_json(folder / "fixture-notice.json", {
            "synthetic": True, "nativeBuildExecuted": False, "signerAndVerifier": "explicit-test-mocks",
            "qualification": "No real package, signature, compiler, template checkout, CI or guest claim",
        })
        for command in recipe["commands"]:
            log = folder / (command["name"] + ".log")
            log.write_text("SYNTHETIC BUILD OBSERVATION, NOT EXECUTED\n", encoding="ascii")
            receipt["command_observations"].append({
                "name": command["name"], "argv": command["argv"], "cwd": "synthetic-source",
                "status": "completed", "exitCode": 0, "logFileName": log.name, "logSha256": producer.sha256(log),
            })
        for rid in producer.RID_ARCH:
            package = self.package(packages, rid)
            inventory = producer.inventory_package(package, rid, recipe["version"], recipe["marker"])
            receipt["packages"].append(inventory)
            inventory_file = folder / f"inventory.{inventory['id']}.json"
            signature_file = folder / f"signature.{inventory['id']}.json"
            producer.write_json(inventory_file, inventory)
            def test_only_unsigned_verify(command, **kwargs):
                kwargs["stdout"].write(b"EXPLICIT TEST MOCK: unsigned synthetic archive\n")
                return producer.subprocess.CompletedProcess(command, 1)
            with patch.object(producer.subprocess, "run", side_effect=test_only_unsigned_verify):
                signature = producer.verify_signature("test-only-dotnet", package, inventory, {}, folder,
                                                      "10.0.110", receipt["command_observations"])
            producer.write_json(signature_file, signature)
            receipt["package_sidecars"].append({
                "id": inventory["id"], "inventoryFileName": inventory_file.name, "inventorySha256": producer.sha256(inventory_file),
                "signatureFileName": signature_file.name, "signatureSha256": producer.sha256(signature_file),
            })
            receipt["native_configuration"][rid] = self.configuration_fixture(folder.parent / "synthetic-config", folder, rid)
        producer.persist_receipt(folder, receipt)
        return receipt

    def test_build_artifact_binding_and_duplicate_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            build = self.build_fixture(root / "input")
            with patch.dict(producer.os.environ, self.pipeline_environment("TestSignRuntimePacks")):
                identity = producer.pipeline_identity(SOURCE)
            self.assertEqual(producer.validate_build_input(root / "input", self.recipe(), identity), build)
            for key, value in [("buildId", 124), ("pipelineCommit", "b" * 40)]:
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "exact producer invocation"):
                    producer.validate_build_input(root / "input", self.recipe(), {**identity, key: value})
            changed = {**build, "command_observations": []}
            producer.write_json(root / "input/receipt.json", changed)
            with self.assertRaisesRegex(ValueError, "observations"):
                producer.validate_build_input(root / "input", self.recipe(), identity)
            producer.write_json(root / "input/receipt.json", build)
            (root / "input/source.patch").write_text("tampered", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "hash/size"):
                producer.validate_build_input(root / "input", self.recipe(), identity)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"key": 1, "key": 2}', encoding="ascii")
            with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
                producer.read_json(duplicate)

    def sign_fixture(self, folder, verification_exit=0, sign_exit=0, verifier_launch_failure=False, sign_attempt=1):
        """Exercise real ZIP/receipt orchestration with explicitly mocked external signing boundaries."""
        input_folder = folder / "input"
        build = self.build_fixture(input_folder)
        root = folder / "source"
        root.mkdir()
        producer.write_json(root / "global.json", {"msbuild-sdks": {"Microsoft.DotNet.Arcade.Sdk": "test-only-sdk"}})
        output = folder / "output"
        def test_only_command(command, **kwargs):
            stdout = kwargs["stdout"]
            if command[0] == "pwsh":
                self.assertIn("/p:Publish=false", command)
                self.assertIn("/p:DotNetSignType=test", command)
                self.assertIn("/p:NuGetAudit=true", command)
                self.assertEqual(command[command.index("-projects") + 1], "src/mono/mono.proj")
                self.assertNotIn("-build", command)
                binlog = root / "artifacts/log/Release/Build.binlog"
                binlog.parent.mkdir(parents=True, exist_ok=True)
                binlog.write_bytes(b"EXPLICIT TEST MOCK, NOT A REAL MSBUILD BINLOG")
                stdout.write(b"EXPLICIT TEST MOCK, NOT AN ACTUAL SIGNER\n")
                if "-sign" in command:
                    package, = (root / "artifacts/packages/Release/Shipping").glob("*.nupkg")
                    with zipfile.ZipFile(package, "a") as archive:
                        archive.writestr(".signature.p7s", b"SYNTHETIC-NOT-A-VALID-SIGNATURE")
                    return producer.subprocess.CompletedProcess(command, sign_exit)
            elif "msbuild" in command:
                package, = (root / "artifacts/packages/Release/Shipping").glob("*.nupkg")
                arch = next(value.split("=", 1)[1] for value in command if value.startswith("/p:TargetArchitecture="))
                stdout.write(json.dumps({
                    "Properties": {"OfficialBuild": "true", "DotNetSignType": "test", "ForceDryRunSigning": "",
                                   "PostBuildSign": "false", "TargetRid": "android-" + arch},
                    "Items": {"ItemsToSign": [{"FullPath": str(package)}]},
                }).encode())
            elif "--version" in command:
                self.assertEqual(Path(kwargs["cwd"]), output / "postsign")
                stdout.write(b"10.0.110\n")
            elif command[1:4] == ["nuget", "verify", "--all"]:
                if verifier_launch_failure:
                    raise FileNotFoundError("EXPLICIT TEST MOCK: verifier launch failure")
                self.assertEqual(Path(kwargs["cwd"]) / command[-1], output / "postsign" / command[-1])
                stdout.write(b"EXPLICIT TEST MOCK VERIFIER; SYNTHETIC SIGNATURE IS NOT VALID\n")
                return producer.subprocess.CompletedProcess(command, verification_exit)
            else:
                self.fail("Unexpected command in explicit mock boundary: " + repr(command))
            return producer.subprocess.CompletedProcess(command, 0)
        environment = {**self.pipeline_environment("TestSignRuntimePacks"), "SYSTEM_JOBATTEMPT": str(sign_attempt)}
        with patch.dict(producer.os.environ, environment), \
                patch.object(producer.sys, "platform", "win32"), \
                patch.object(producer, "source_evidence", return_value=build["patch_sha256"]), \
                patch.object(producer, "template_evidence", return_value=build["templates"]), \
                patch.object(producer.subprocess, "run", side_effect=test_only_command):
            if sign_exit or verification_exit or verifier_launch_failure:
                with self.assertRaises((ValueError, FileNotFoundError)):
                    producer.test_sign(root, self.recipe(), input_folder, output)
            else:
                producer.test_sign(root, self.recipe(), input_folder, output)
        return output

    def test_postsign_success_and_failed_verification_preserve_evidence(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as folder:
                output = self.sign_fixture(Path(folder), verification_exit=exit_code, sign_attempt=2)
                root = producer.read_json(output / "receipt.json")
                self.assertEqual(root["status"], "verified-policy-unqualified" if exit_code == 0 else "produced-verification-failed")
                self.assertFalse(root["publication"])
                self.assertEqual(len(root["packages"]), 2)
                retained = {item["path"]: item["sha256"] for item in root["retained_files"]}
                self.assertEqual(retained["build/receipt.json"], producer.sha256(output / "build/receipt.json"))
                self.assertEqual((output / "postsign/build-receipt.json").read_bytes(),
                                 (output / "build/receipt.json").read_bytes())
                for entry in root["packages"]:
                    post = producer.read_json(output / "postsign" / entry["fileName"])
                    self.assertEqual(post["status"], root["status"])
                    self.assertNotEqual(post["input"]["sha256"], post["output"]["sha256"])
                    self.assertEqual(post["signer"]["requestedSignType"], "Test")
                    self.assertEqual(post["signer"]["jobAttempt"], 2)
                    self.assertEqual(post["version"], self.recipe()["version"])
                    self.assertEqual([item["role"] for item in post["operationEvidence"]], ["sign", "sign", "verify"])
                    for ref in (post["nativeProvenance"], post["memberDelta"], post["output"]["inventory"],
                                post["output"]["signature"], post["input"]["inventory"]):
                        self.assertEqual(ref["sha256"], producer.sha256(output / "postsign" / ref["fileName"]))
                    signature = producer.read_json(output / "postsign" / post["output"]["signature"]["fileName"])
                    self.assertEqual(signature["verificationExitCode"], exit_code)
                    self.assertEqual(signature["classification"],
                                     "signature-valid-policy-unqualified" if exit_code == 0 else "verification-failed")
                    native = producer.read_json(output / "postsign" / post["nativeProvenance"]["fileName"])
                    self.assertEqual(native["packageSha256"], post["output"]["sha256"])
                    self.assertEqual(native["configuration"]["value"], self.recipe()["marker"])
                    self.assertEqual(native["carriers"][0]["gnuBuildIdStatus"], "not-collected")
                    self.assertIsNone(native["carriers"][0]["gnuBuildId"])
                    self.assertEqual(native["carriers"][0]["machine"], producer.RID_ARCH[post["rid"]][1])
                    delta = producer.read_json(output / "postsign" / post["memberDelta"]["fileName"])
                    self.assertEqual([item["path"] for item in delta["changes"]], [".signature.p7s"])
                    self.assertFalse(delta["policyAdmitted"])

    def test_sign_failure_and_verifier_launch_failure_are_not_completed(self):
        for arguments in ({"sign_exit": 7}, {"verifier_launch_failure": True}):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as folder:
                output = self.sign_fixture(Path(folder), **arguments)
                root = producer.read_json(output / "receipt.json")
                self.assertEqual(root["status"], "failed")
                self.assertEqual(root["packages"], [])
                self.assertEqual(len(list((output / "postsign").glob("*.nupkg"))), 1)
                self.assertEqual(len(list((output / "build/packages").glob("*.nupkg"))), 2)
                last = root["command_observations"][-1]
                self.assertEqual(last["status"], "failed" if "sign_exit" in arguments else "launch-failed")
                self.assertEqual(last["exitCode"], 7 if "sign_exit" in arguments else None)
                self.assertFalse(list((output / "postsign").glob("postsign.*.json")))

    def test_signing_selection_and_properties_fail_closed(self):
        package = Path("exact-package.nupkg").resolve()
        evaluation = {"Properties": {"OfficialBuild": "true", "DotNetSignType": "test", "ForceDryRunSigning": "",
                                      "PostBuildSign": "false", "TargetRid": "android-x64"},
                      "Items": {"ItemsToSign": [{"FullPath": str(package)}]}}
        producer.validate_signing_selection(evaluation, package, "android-x64")
        for key, value in [("OfficialBuild", "false"), ("DotNetSignType", "real"), ("ForceDryRunSigning", "true"),
                           ("PostBuildSign", "true"), ("TargetRid", "android-arm64")]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "properties"):
                producer.validate_signing_selection({**evaluation, "Properties": {**evaluation["Properties"], key: value}},
                                                    package, "android-x64")
        for items in ([], [{"FullPath": "other.nupkg"}], evaluation["Items"]["ItemsToSign"] * 2):
            with self.assertRaisesRegex(ValueError, "exactly"):
                producer.validate_signing_selection({**evaluation, "Items": {"ItemsToSign": items}}, package, "android-x64")
        for value in ("", "123.1", "20260924.1\n", "20260924.1;echo"):
            with self.assertRaisesRegex(ValueError, "OfficialBuildId"):
                producer.signing_properties(self.recipe(), "android-x64", value)

    def test_producer_refuses_nonmanual(self):
        with patch.object(producer.sys, "platform", "linux"), patch.dict(producer.os.environ, {"BUILD_REASON": "PullRequest"}):
            with self.assertRaisesRegex(ValueError, "manual"):
                producer.produce(ROOT, self.recipe(), Path("missing"), "28.2.13676358")


if __name__ == "__main__":
    unittest.main()
