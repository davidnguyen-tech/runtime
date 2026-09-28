# Licensed to the .NET Foundation under one or more agreements.
# The .NET Foundation licenses this file to you under the MIT license.

import importlib.util
import io
import json
from pathlib import Path
import runpy
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile

SCRIPT = Path(__file__).with_name("produce-mono-android-startup.py")
ROOT = SCRIPT.parents[2]
spec = importlib.util.spec_from_file_location("producer", SCRIPT)
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)
SOURCE = "a" * 40
IMAGE = producer.PRODUCER_IMAGE
MAC_VERIFIER = runpy.run_path(str(SCRIPT.with_name("verify-mono-android-startup-mac.py")))


class ProducerTests(unittest.TestCase):
    def test_held_mac_verification_is_isolated_and_default_off(self):
        root = (ROOT / producer.PIPELINE_PATH).read_text()
        template = (ROOT / "eng/pipelines/mono-android-startup-mac-verify.yml").read_text()
        self.assertIn("name: verifyMonoStartupRealSignature\n", root)
        self.assertIn("  default: false\n", root.split("name: verifyMonoStartupRealSignature", 1)[1].split("variables:", 1)[0])
        self.assertIn("sourceCommit: ${{ parameters.monoStartupSourceCommit }}", root)
        self.assertIn("and(eq(parameters.verifyMonoStartupRealSignature, true), eq(parameters.enableMonoStartupMetadata, false))", root)
        self.assertIn("and(eq(parameters.enableMonoStartupMetadata, false), eq(parameters.verifyMonoStartupRealSignature, false))", root)
        for expected in ("condition: and(succeeded(), eq(variables['Build.Reason'], 'Manual'))",
                         "name: Azure Pipelines", "vmImage: macOS-26", "name: VerifyHeldRealSignatures",
                         "buildType: specific", "project: internal", "definition: '679'",
                         "buildVersionToDownload: specific", "pipelineId: '3089937'",
                         "artifactName: mono-android-startup-real-signed-unadmitted-1",
                         "isProduction: false", "condition: succeededOrFailed()",
                         "enablePublishing: false", "enablePublishBuildAssets: false",
                         "persistCredentials: false", "REVIEWED_SOURCE: ${{ parameters.sourceCommit }}"):
            self.assertIn(expected, template)
        self.assertNotIn("publish-build-assets.yml", template)
        self.assertNotIn("allowFailedBuilds", template)
        self.assertNotIn("allowPartiallySucceededBuilds", template)
        self.assertNotIn("MicroBuild", template)
        self.assertNotIn("feed", template.lower())
        self.assertNotIn("darc", template.lower())
        self.assertEqual(MAC_VERIFIER["SDK_SHA512"],
                         "33401b4a2da8554e3306db6072ea8569d9fcc608509c271e0aa4b39e7cc432da3631f14e7e1e2445d67d72550d18ce44a8bbd2382a756867ad2edab6b1c963c0")
        self.assertEqual(MAC_VERIFIER["ARTIFACT_ID"], 76626017)
        self.assertEqual(set(MAC_VERIFIER["PACKAGES"]), set(producer.RID_ARCH))

    def test_held_mac_verifier_refuses_unreviewed_context_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(producer.os.environ, {"BUILD_REASON": "PullRequest"}), \
                    patch.object(MAC_VERIFIER["subprocess"], "run") as external:
                with self.assertRaisesRegex(ValueError, "Only reviewed internal manual"):
                    MAC_VERIFIER["verify"](root / "missing", root / "evidence", "b" * 40)
                external.assert_not_called()
            receipt = producer.read_json(root / "evidence/receipt.json")
            self.assertEqual(receipt["status"], "failed")
            self.assertFalse(receipt["admitted"])
            self.assertFalse(receipt["guestExecution"])
            self.assertEqual(receipt["packages"], [])
            self.assertEqual(receipt["observations"], [])
            self.assertEqual(receipt["retainedFiles"], [])
            with patch.dict(producer.os.environ, {
                    "BUILD_REASON": "Manual", "SYSTEM_TEAMPROJECT": "internal",
                    "SYSTEM_DEFINITIONID": "679", "BUILD_SOURCEVERSION": "c" * 40}), \
                    patch.object(MAC_VERIFIER["subprocess"], "run") as external:
                with self.assertRaisesRegex(ValueError, "Only reviewed internal manual"):
                    MAC_VERIFIER["verify"](root / "missing", root / "mismatched-source", "b" * 40)
                external.assert_not_called()
            self.assertEqual(producer.read_json(root / "mismatched-source/receipt.json")["status"], "failed")

    def test_held_mac_host_rejects_rosetta_and_non_intel(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            observations = []
            with patch.object(MAC_VERIFIER["sys"], "platform", "darwin"), \
                    patch.object(MAC_VERIFIER["platform"], "machine", return_value="arm64"), \
                    patch.object(MAC_VERIFIER["subprocess"], "run") as external:
                with self.assertRaisesRegex(ValueError, "Native Darwin x86_64"):
                    MAC_VERIFIER["mac_host"](folder, observations)
                external.assert_not_called()
            with patch.object(MAC_VERIFIER["sys"], "platform", "darwin"), \
                    patch.object(MAC_VERIFIER["platform"], "machine", return_value="x86_64"), \
                    patch.object(MAC_VERIFIER["subprocess"], "run", side_effect=[
                        producer.subprocess.CompletedProcess([], 0, b"1\n")]):
                with self.assertRaisesRegex(ValueError, "Rosetta"):
                    MAC_VERIFIER["mac_host"](folder, observations)
            self.assertEqual(observations[0]["exitCode"], 0)
            self.assertEqual(observations[0]["log"]["sha256"], producer.sha256(folder / "hw.optional.arm64.log"))

    def test_held_mac_sdk_hash_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            output = root / "evidence"
            workspace.mkdir()
            output.mkdir()
            with patch.object(MAC_VERIFIER["urllib"].request, "urlopen", return_value=io.BytesIO(b"not SDK")), \
                    patch.dict(MAC_VERIFIER["sdk_install"].__globals__,
                               {"extract_sdk_archive": Mock()}) as globals_:
                with self.assertRaisesRegex(ValueError, "SHA-512 mismatch"):
                    MAC_VERIFIER["sdk_install"](workspace, output, [])
                globals_["extract_sdk_archive"].assert_not_called()

    def test_held_mac_signing_receipt_rejects_other_definition(self):
        receipt = {"schemaVersion": 3, "kind": "mono-android-startup-sign-receipt",
                   "status": "verified-policy-unqualified", "source": MAC_VERIFIER["SOURCE"],
                   "publication": False, "guest_execution": False,
                   "pipeline": {"buildId": MAC_VERIFIER["RUN"], "definitionId": 680,
                                "phaseName": "RealSignRuntimePacks",
                                "pipelineCommit": MAC_VERIFIER["SOURCE"]},
                   "signer": {"requestedSignType": "Real"}}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "receipt.json").write_text("{}")
            with patch.dict(MAC_VERIFIER["validate_input"].__globals__,
                            {"read_json": lambda _: receipt,
                             "sha256": lambda _: MAC_VERIFIER["RECEIPT_SHA256"]}):
                with self.assertRaisesRegex(ValueError, "provenance mismatch"):
                    MAC_VERIFIER["validate_input"](folder)

    def test_held_mac_command_failure_records_actual_exit_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            observations = []
            with self.assertRaisesRegex(ValueError, "failed"):
                MAC_VERIFIER["run"]([sys.executable, "-c", "print('synthetic verifier failure'); raise SystemExit(7)"],
                                    output, output, "verify.x64", observations)
            self.assertEqual(observations[0]["exitCode"], 7)
            self.assertEqual(observations[0]["log"]["sha256"], producer.sha256(output / "verify.x64.log"))

    def test_held_mac_command_timeout_and_output_limit_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            observations = []
            with self.assertRaisesRegex(ValueError, "timed out"):
                MAC_VERIFIER["run"]([sys.executable, "-c",
                                     "import time; print('started', flush=True); time.sleep(10)"],
                                    output, output, "timeout", observations, timeout_seconds=0.2)
            self.assertTrue(observations[0]["timedOut"])
            self.assertIsInstance(observations[0]["exitCode"], int)
            self.assertEqual(observations[0]["log"]["sha256"], producer.sha256(output / "timeout.log"))
            with self.assertRaisesRegex(ValueError, "output limit"):
                MAC_VERIFIER["run"]([sys.executable, "-c", "import sys; sys.stdout.write('x'*8192)"],
                                    output, output, "overflow", observations, max_log_bytes=1024)
            self.assertTrue(observations[1]["logLimitExceeded"])
            self.assertEqual((output / "overflow.log").stat().st_size, 1024)
            self.assertEqual(observations[1]["log"]["sha256"], producer.sha256(output / "overflow.log"))

    def test_held_mac_sdk_archive_refuses_unsafe_paths_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sdk.tar.gz"
            for name, kind in (("../escape", "file"), ("/absolute", "file"),
                               ("sdk/link", "link"), ("sdk/device", "special")):
                with self.subTest(name=name):
                    with tarfile.open(archive, "w:gz") as target:
                        entry = tarfile.TarInfo(name)
                        if kind == "file":
                            entry.size = 1
                            target.addfile(entry, io.BytesIO(b"x"))
                        elif kind == "link":
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = "../escape"
                            target.addfile(entry)
                        else:
                            entry.type = tarfile.CHRTYPE
                            target.addfile(entry)
                    destination = root / "sdk"
                    destination.mkdir(exist_ok=True)
                    with self.assertRaisesRegex(ValueError, "Unsafe SDK archive path|link or special"):
                        MAC_VERIFIER["extract_sdk_archive"](archive, destination)
                    self.assertEqual(list(destination.iterdir()), [])

    def test_held_mac_trust_snapshot_tracks_executable_and_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory)
            binary = sdk / "dotnet"
            binary.write_bytes(b"SDK executable")
            roots = sdk / "sdk/10.0.401/trustedroots"
            roots.mkdir(parents=True)
            for name in ("codesignctl.pem", "timestampctl.pem"):
                (roots / name).write_bytes(name.encode())
            before = MAC_VERIFIER["sdk_trust_snapshot"](binary)
            self.assertEqual(len(before), 3)
            (roots / "codesignctl.pem").write_bytes(b"modified")
            self.assertNotEqual(before, MAC_VERIFIER["sdk_trust_snapshot"](binary))

    def test_held_mac_rejects_inherited_signature_and_revocation_bypasses(self):
        context = {"BUILD_REASON": "Manual", "SYSTEM_TEAMPROJECT": "internal",
                   "SYSTEM_DEFINITIONID": "679", "BUILD_REPOSITORY_ID": producer.REPOSITORY_ID,
                   "SYSTEM_STAGENAME": "MonoStartupMacVerification",
                   "SYSTEM_PHASENAME": "VerifyHeldRealSignatures",
                   "BUILD_SOURCEVERSION": "b" * 40, "BUILD_SOURCEBRANCH": "refs/heads/reviewed-feature"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, settings in enumerate(({"DOTNET_NUGET_SIGNATURE_VERIFICATION": "false"},
                                               {"NUGET_CERT_REVOCATION_MODE": "offline"})):
                with self.subTest(settings=settings), patch.dict(producer.os.environ, context | settings, clear=True), \
                        patch.dict(MAC_VERIFIER["verify"].__globals__,
                                   {"validate_input": lambda _: self.fail("Must reject before artifact inspection")}):
                    output = root / str(index)
                    with self.assertRaisesRegex(ValueError, "Inherited NuGet"):
                        MAC_VERIFIER["verify"](root / "input", output, "b" * 40)
                    receipt = producer.read_json(output / "receipt.json")
                    self.assertEqual(receipt["status"], "failed")
                    for name, value in settings.items():
                        self.assertEqual(receipt["inheritedVerifierSettings"][name], value)

    def test_held_mac_rejects_sdk_trust_change_after_verification(self):
        context = {"BUILD_REASON": "Manual", "SYSTEM_TEAMPROJECT": "internal",
                   "SYSTEM_DEFINITIONID": "679", "BUILD_REPOSITORY_ID": producer.REPOSITORY_ID,
                   "SYSTEM_STAGENAME": "MonoStartupMacVerification",
                   "SYSTEM_PHASENAME": "VerifyHeldRealSignatures",
                   "BUILD_SOURCEVERSION": "b" * 40, "BUILD_SOURCEBRANCH": "refs/heads/reviewed-feature"}
        package = {"rid": "android-x64", "package": {"fileName": "android-x64.nupkg"}}
        def install(workspace, *_):
            sdk = workspace / "sdk"
            roots = sdk / "sdk/10.0.401/trustedroots"
            roots.mkdir(parents=True)
            (sdk / "dotnet").write_bytes(b"SDK executable")
            for name in ("codesignctl.pem", "timestampctl.pem"):
                (roots / name).write_bytes(name.encode())
            return sdk / "dotnet"
        def execute(command, _, output, name, observations, env):
            log = output / (name + ".log")
            log.write_bytes(b"10.0.401\n" if command[-1] == "--version" else b"verified\n")
            observations.append({"name": name, "exitCode": 0, "log": MAC_VERIFIER["reference"](log)})
            if command[-1] != "--version":
                (Path(env["DOTNET_ROOT"]) / "sdk/10.0.401/trustedroots/codesignctl.pem").write_bytes(b"changed")
            return log
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            with patch.dict(producer.os.environ, context, clear=True), \
                    patch.dict(MAC_VERIFIER["verify"].__globals__,
                               {"validate_input": lambda _: [package], "mac_host": lambda *_: None,
                                "sdk_install": install, "run": execute}):
                with self.assertRaisesRegex(ValueError, "trust roots changed"):
                    MAC_VERIFIER["verify"](root / "input", root / "output", "b" * 40)
            receipt = producer.read_json(root / "output/receipt.json")
            self.assertEqual(receipt["status"], "failed")
            self.assertNotEqual(receipt["sdkTrust"]["before"], receipt["sdkTrust"]["after"])

    def test_held_mac_checks_both_packages_even_when_first_verifier_fails(self):
        context = {"BUILD_REASON": "Manual", "SYSTEM_TEAMPROJECT": "internal",
                   "SYSTEM_DEFINITIONID": "679", "BUILD_REPOSITORY_ID": producer.REPOSITORY_ID,
                   "SYSTEM_STAGENAME": "MonoStartupMacVerification",
                   "SYSTEM_PHASENAME": "VerifyHeldRealSignatures",
                   "BUILD_SOURCEVERSION": "b" * 40,
                   "BUILD_SOURCEBRANCH": "refs/heads/reviewed-feature"}
        packages = [{"rid": rid, "package": {"fileName": rid + ".nupkg"}}
                    for rid in MAC_VERIFIER["PACKAGES"]]
        def execute(command, _, output, name, observations, env):
            self.assertEqual(env["DOTNET_GENERATE_ASPNET_CERTIFICATE"], "false")
            self.assertTrue(Path(env["DOTNET_CLI_HOME"]).is_dir())
            self.assertTrue(Path(env["NUGET_PACKAGES"]).is_dir())
            log = output / (name + ".log")
            if command[-1] == "--version":
                log.write_bytes(b"10.0.401\n")
                exit_code = 0
            else:
                log.write_bytes(b"NuGet verification result\n")
                exit_code = 7 if command[-1] == "android-x64.nupkg" else 0
            observations.append({"name": name, "exitCode": exit_code, "log": MAC_VERIFIER["reference"](log)})
            if exit_code:
                raise ValueError("verifier failed")
            return log
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            with patch.dict(producer.os.environ, context, clear=True), \
                    patch.dict(MAC_VERIFIER["verify"].__globals__,
                               {"validate_input": lambda _: packages,
                                "mac_host": lambda *_: None,
                                "sdk_install": lambda *_: Path("/sdk/dotnet"),
                                "sdk_trust_snapshot": lambda _: [{"sha256": "unchanged"}],
                                "run": execute}):
                with self.assertRaisesRegex(ValueError, "Intel Mac normal verifier rejected"):
                    MAC_VERIFIER["verify"](root / "input", root / "output", "b" * 40)
            receipt = producer.read_json(root / "output/receipt.json")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["verifierSourceCommit"], "b" * 40)
            self.assertEqual([entry["verificationExitCode"] for entry in receipt["packages"]], [7, 0])
            verify_logs = [entry for entry in receipt["observations"] if entry["name"].startswith("verify.")]
            self.assertEqual([entry["exitCode"] for entry in verify_logs], [7, 0])
            for entry in verify_logs:
                self.assertEqual(entry["log"]["sha256"], producer.sha256(root / "output" / entry["log"]["fileName"]))
            self.assertEqual(len(receipt["retainedFiles"]), 4)

    def recipe(self, **overrides):
        args = {"source": SOURCE, "run_id": "123.1", "image": IMAGE, "enabled": True}
        args.update(overrides)
        return producer.plan(**args)

    def pipeline_environment(self, phase="BuildRuntimePacks"):
        return {"BUILD_REASON": "Manual", "SYSTEM_TEAMPROJECT": "internal",
                "BUILD_REPOSITORY_ID": producer.REPOSITORY_ID, "SYSTEM_DEFINITIONID": "679",
                "BUILD_SOURCEVERSION": SOURCE, "BUILD_SOURCEBRANCH": "refs/heads/test-only-diagnostics",
                "BUILD_BUILDID": "123", "SYSTEM_JOBATTEMPT": "1", "SYSTEM_JOBNAME": "__default",
                "SYSTEM_STAGENAME": "MonoStartupMetadata", "SYSTEM_PHASENAME": phase, "PRODUCER_ATTEMPT": "1",
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
        real_parameter = ("- name: monoStartupRealSign\n"
                          "  displayName: Use approved Real signing instead of Test signing (manual diagnostic only)\n"
                          "  type: boolean\n"
                          "  default: false\n")
        verify_parameter = ("- name: verifyMonoStartupRealSignature\n"
                            "  displayName: Verify held Real-signed startup packs on Intel Mac without publishing\n"
                            "  type: boolean\n"
                            "  default: false\n")
        self.assertEqual(current.count(real_parameter), 1)
        self.assertEqual(current.count(verify_parameter), 1)
        current_default = current.replace(real_parameter, "").replace(verify_parameter, "").replace(
            "Build and sign artifact-only Mono Android startup diagnostics",
            "Build and TEST-sign artifact-only Mono Android startup diagnostics")
        self.assertTrue(current_default.startswith(original.split("variables:\n")[0]))
        image_flag = "    enableMonoStartupMetadata: ${{ or(parameters.enableMonoStartupMetadata, parameters.verifyMonoStartupRealSignature) }}\n"
        self.assertEqual(current.count(image_flag), 1)
        self.assertEqual(current_default.replace(image_flag, "").split("variables:\n", 1)[1].split("    stages:\n", 1)[0],
                         original.split("variables:\n", 1)[1].split("    stages:\n", 1)[0])
        self.assertIn("name: enableMonoStartupMetadata", current)
        self.assertIn("  type: boolean\n  default: false\n", current)
        self.assertIn("          realSign: ${{ parameters.monoStartupRealSign }}\n", current)
        enabled, disabled = current.split("    stages:\n", 1)[1].split(
            "    - ${{ if and(eq(parameters.enableMonoStartupMetadata, false), eq(parameters.verifyMonoStartupRealSignature, false)) }}:\n")
        verify_branch, enabled = enabled.split(
            "    - ${{ if and(eq(parameters.enableMonoStartupMetadata, true), eq(parameters.verifyMonoStartupRealSignature, false)) }}:\n")
        self.assertEqual(verify_branch,
                         "    - ${{ if and(eq(parameters.verifyMonoStartupRealSignature, true), eq(parameters.enableMonoStartupMetadata, false)) }}:\n"
                         "      - template: /eng/pipelines/mono-android-startup-mac-verify.yml\n"
                         "        parameters:\n"
                         "          sourceCommit: ${{ parameters.monoStartupSourceCommit }}\n")
        enabled, reject = enabled.split(
            "    - ${{ if and(eq(parameters.enableMonoStartupMetadata, true), eq(parameters.verifyMonoStartupRealSignature, true)) }}:\n")
        self.assertEqual(enabled,
                         "      - template: /eng/pipelines/mono-android-startup-metadata.yml\n"
                         "        parameters:\n"
                         "          sourceCommit: ${{ parameters.monoStartupSourceCommit }}\n"
                         "          attempt: ${{ parameters.monoStartupAttempt }}\n"
                         "          realSign: ${{ parameters.monoStartupRealSign }}\n")
        self.assertIn("stage: InvalidMonoStartupMode", reject)
        self.assertIn("checkout: none", reject)
        self.assertIn("- bash: exit 1", reject)
        default_stages = "\n".join(line[2:] if line else "" for line in disabled.splitlines())
        self.assertEqual(default_stages, original.split("    stages:\n", 1)[1].rstrip("\n"))
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        self.assertEqual(set(graph), {"parameters", "stages"})
        self.assertEqual(graph["parameters"], [{"name": "sourceCommit", "type": "string"},
                                               {"name": "attempt", "type": "number", "default": 1},
                                               {"name": "realSign", "type": "boolean", "default": False}])
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
        self.assertEqual(variables["_SignType"], "${{ iif(parameters.realSign, 'real', 'test') }}")
        self.assertTrue(sign["enableMicrobuild"])
        self.assertEqual(sign["microbuildUseESRP"], "${{ parameters.realSign }}")
        self.assertFalse(sign["enableMicrobuildForMacAndLinux"])
        self.assertFalse(sign["enablePublishing"])
        self.assertFalse(sign["enablePublishBuildAssets"])
        self.assertEqual(sign["name"], "${{ iif(parameters.realSign, 'RealSignRuntimePacks', 'TestSignRuntimePacks') }}")
        self.assertEqual(sign["steps"][0]["pwsh"].count("--real-sign', '--test-sign"), 1)
        self.assertEqual(sign["templateContext"]["outputs"][0]["artifactName"],
                         "mono-android-startup-${{ iif(parameters.realSign, 'real', 'test') }}-signed-unadmitted-$(System.JobAttempt)")
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
        self.assertIn("ConnectedServiceName: 'MicroBuild Signing Task (DevDiv)'", plugin)
        self.assertIn("if eq(parameters.microbuildUseESRP, true)", plugin)
        for real_sign, phase, sign_type in ((False, "TestSignRuntimePacks", "test"),
                                             (True, "RealSignRuntimePacks", "real")):
            with self.subTest(real_sign=real_sign):
                resolved = json.loads(json.dumps(sign).replace(
                    "${{ iif(parameters.realSign, 'RealSignRuntimePacks', 'TestSignRuntimePacks') }}", phase
                ).replace(
                    "${{ iif(parameters.realSign, '--real-sign', '--test-sign') }}", f"--{sign_type}-sign"
                ).replace(
                    "${{ iif(parameters.realSign, 'real', 'test') }}", sign_type
                ).replace(
                    '"${{ parameters.realSign }}"', "true" if real_sign else "false"
                ))
                self.assertEqual(resolved["name"], phase)
                self.assertEqual(resolved["microbuildUseESRP"], real_sign)
                self.assertEqual(resolved["variables"][0]["value"], sign_type)
                self.assertIn(f"--{sign_type}-sign", resolved["steps"][0]["pwsh"])
                self.assertEqual(resolved["templateContext"]["outputs"][0]["artifactName"],
                                 f"mono-android-startup-{sign_type}-signed-unadmitted-$(System.JobAttempt)")
                self.assertEqual(resolved["artifacts"]["download"]["name"],
                                 "mono-android-startup-unsigned-unadmitted-$(ProducerAttempt)")
                self.assertFalse(resolved["enablePublishing"])
                self.assertFalse(resolved["enablePublishBuildAssets"])

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
        selection = (
            "      ${{ if parameters.enableMonoStartupMetadata }}:\n"
            "        templatePath: template1es-mono-startup.yml\n"
            "      ${{ else }}:\n"
            "        templatePath: template1es.yml\n"
        )
        self.assertEqual(current.count(selection), 1)
        self.assertIn("    ${{ if parameters.isOfficialBuild }}:\n" + selection +
                      "    ${{ else }}:\n"
                      "      templatePath: templatePublic.yml\n", current)
        for official in (False, True):
            for enabled in (False, True):
                with self.subTest(official=official, enabled=enabled):
                    image = producer.PRODUCER_IMAGE if official and enabled else normal_image
                    template = "template1es-mono-startup.yml" if official and enabled else "template1es.yml"
                    resolved = current.replace(parameter, "").replace(selection, f"      templatePath: {template}\n").replace(
                        conditional, f"      android:\n        image: {image}\n")
                    expected = baseline.stdout.replace(normal, f"      android:\n        image: {image}\n").replace(
                        "      templatePath: template1es.yml\n", f"      templatePath: {template}\n")
                    self.assertEqual(resolved, expected)
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        stage, = graph["stages"]
        self.assertEqual(next(item["value"] for item in stage["variables"] if item["name"] == "StartupMetadataImage"),
                         producer.PRODUCER_IMAGE)
        self.assertNotIn("enableMonoStartupMetadata", current.replace(selection, "").split(
            "    containers:\n", 1)[0].split("extends:\n", 1)[1])

    def test_host_checkout_and_container_path_execution(self):
        graph = json.loads((ROOT / "eng/pipelines/mono-android-startup-metadata.yml").read_text())
        jobs = [job["parameters"] for job in graph["stages"][0]["jobs"]]
        build = next(job for job in jobs if job["name"] == "BuildRuntimePacks")
        sign = next(job for job in jobs if job["name"] ==
                    "${{ iif(parameters.realSign, 'RealSignRuntimePacks', 'TestSignRuntimePacks') }}")
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
                         "$(Build.ArtifactStagingDirectory)/mono-startup-${{ iif(parameters.realSign, 'real', 'test') }}-signed")
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

    def test_literal_template_wrappers_share_unchanged_policy(self):
        directory = ROOT / "eng/pipelines/common/templates"
        inclusion = (
            "      ${{ if parameters.enableMonoStartupMetadata }}:\n"
            "        sourceRepositoriesToScan:\n"
            "          include:\n"
            "          - repository: 1ESPipelineTemplates\n"
        )
        originals = {}
        for name in ("templateDispatch.yml", "template1es.yml", "templatePublic.yml"):
            path = "eng/pipelines/common/templates/" + name
            baseline = producer.subprocess.run([
                "git", "show", producer.BASELINE + ":" + path,
            ], cwd=ROOT, env=dict(producer.os.environ), stdout=producer.subprocess.PIPE,
                stderr=producer.subprocess.PIPE, text=True)
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            originals[name] = baseline.stdout
            if name != "template1es.yml":
                self.assertEqual((directory / name).read_text(), baseline.stdout)
        normal = (directory / "template1es.yml").read_text()
        diagnostic = (directory / "template1es-mono-startup.yml").read_text()
        body = (directory / "template1es-body.yml").read_text()
        self.assertEqual(normal.split("extends:\n")[0], originals["template1es.yml"].split("extends:\n")[0])
        self.assertEqual(diagnostic.replace(producer.EXPECTED_1ES_COMMIT, "refs/tags/release").replace(
            "    enableMonoStartupMetadata: true\n", ""), normal)
        forwarding = (
            "  template: template1es-body.yml\n"
            "  parameters:\n"
            "    containers: ${{ parameters.containers }}\n"
            "    stages: ${{ parameters.stages }}\n"
        )
        self.assertEqual(normal.split("extends:\n")[1], forwarding)
        self.assertEqual(body.count(inclusion), 1)
        self.assertEqual(body.split("extends:\n")[0],
                         "parameters:\n  - name: stages\n    type: stageList\n"
                         "  - name: containers\n    type: object\n"
                         "  - name: enableMonoStartupMetadata\n    type: boolean\n    default: false\n\n")
        self.assertEqual(body.split("extends:\n")[1].replace(inclusion, ""),
                         originals["template1es.yml"].split("extends:\n")[1])
        reviewed = producer.subprocess.run([
            "git", "show", "a0115fb6b71937a0e99037e594c33dca35c0351a:eng/pipelines/common/templates/template1es.yml",
        ], cwd=ROOT, env=dict(producer.os.environ), stdout=producer.subprocess.PIPE,
            stderr=producer.subprocess.PIPE, text=True)
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.assertEqual(body.split("extends:\n")[1], reviewed.stdout.split("extends:\n")[1])
        for content in (normal, diagnostic, body, (directory / "templateDispatch.yml").read_text(),
                        (directory / "pipeline-with-resources.yml").read_text(),
                        (ROOT / "eng/pipelines/runtime-official.yml").read_text()):
            self.assertNotIn("oneESTemplateRef", content)
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
            self.assertEqual(receipt["schemaVersion"], 5)
            self.assertEqual(receipt["status"], "failed")
            self.assertNotIn("providerContext", receipt)
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
            identity = producer.pipeline_identity(SOURCE, "BuildRuntimePacks")
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
                            producer.pipeline_identity(SOURCE, "BuildRuntimePacks")

    def test_provider_phase_and_raw_job_instance_identity(self):
        for phase in ("ValidateInputs", "BuildRuntimePacks", "TestSignRuntimePacks", "RealSignRuntimePacks"):
            with self.subTest(phase=phase), patch.dict(producer.os.environ, self.pipeline_environment(phase)):
                identity = producer.pipeline_identity(SOURCE, phase)
                self.assertEqual(set(identity), {"organization", "project", "definitionId", "pipelinePath",
                                                "pipelineCommit", "buildId", "jobAttempt", "jobName",
                                                "stageName", "phaseName"})
                self.assertEqual(identity["jobName"], "__default")
                self.assertEqual(identity["phaseName"], phase)
                self.assertEqual(identity["stageName"], "MonoStartupMetadata")
                for key, values in {
                    "SYSTEM_STAGENAME": [None, "", "AnotherStage", "MonoStartupMetadata\n"],
                    "SYSTEM_PHASENAME": [None, "", "AnotherRole", phase + "\n"],
                    "SYSTEM_JOBNAME": [None, "", "bad/name", "x" * 257, "__default\n"],
                }.items():
                    for value in values:
                        with self.subTest(key=key, value=value), patch.dict(producer.os.environ):
                            if value is None:
                                producer.os.environ.pop(key, None)
                            else:
                                producer.os.environ[key] = value
                            with self.assertRaises(ValueError):
                                producer.pipeline_identity(SOURCE, phase)

    def test_failed_identity_preflight_retains_bounded_raw_context(self):
        for signing in (False, True):
            phase = "TestSignRuntimePacks" if signing else "BuildRuntimePacks"
            for key, value in (("SYSTEM_PHASENAME", "AnotherRole"), ("SYSTEM_STAGENAME", None),
                               ("SYSTEM_JOBNAME", "x" * 257), ("SYSTEM_JOBATTEMPT", "1\n"),
                               ("BUILD_SOURCEVERSION", "b" * 40), ("SYSTEM_DEFINITIONID", "1104")):
                with self.subTest(signing=signing, key=key), tempfile.TemporaryDirectory() as folder:
                    base = Path(folder)
                    root = base / "source"
                    root.mkdir()
                    output = base / "output"
                    environment = self.pipeline_environment(phase)
                    with patch.dict(producer.os.environ, environment), \
                            patch.object(producer.sys, "platform", "win32" if signing else "linux"), \
                            patch.object(producer.subprocess, "run") as external:
                        if value is None:
                            producer.os.environ.pop(key, None)
                        else:
                            producer.os.environ[key] = value
                        with self.assertRaises(ValueError):
                            if signing:
                                producer.test_sign(root, self.recipe(), base / "input", output)
                            else:
                                producer.produce(root, self.recipe(), base / "ndk", "27.2.12479018", output)
                        external.assert_not_called()
                    receipt = producer.read_json(output / "receipt.json")
                    self.assertEqual(receipt["schemaVersion"], 3 if signing else 5)
                    self.assertEqual(receipt["status"], "failed")
                    self.assertIsNone(receipt["pipeline"])
                    self.assertEqual(receipt["packages"], [])
                    self.assertEqual(receipt["command_observations"], [])
                    self.assertEqual(receipt["retained_files"], [])
                    self.assertFalse(list(output.rglob("postsign.*.json")))
                    context = receipt["providerContext"]
                    self.assertEqual(set(context), {"stageName", "phaseName", "jobName", "jobAttempt", "truncatedFields"})
                    self.assertEqual(context["truncatedFields"], ["jobName"] if key == "SYSTEM_JOBNAME" else [])
                    for field, variable in (("stageName", "SYSTEM_STAGENAME"), ("phaseName", "SYSTEM_PHASENAME"),
                                            ("jobName", "SYSTEM_JOBNAME"), ("jobAttempt", "SYSTEM_JOBATTEMPT")):
                        raw = value if key == variable else environment[variable]
                        self.assertEqual(context[field], raw[:256] if raw is not None else None)

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
        crossgen = ROOT / "src/tasks/Crossgen2Tasks"
        # These are the real inputs copied by Crossgen2Tasks.csproj. Evaluation
        # registers tasks but never executes R2R or needs the generated task DLL.
        crossgen_imports = [
            f"/p:Crossgen2SdkOverridePropsPath={crossgen / 'ShimFilesSimulatingLogicInSdkRepo/Microsoft.NET.CrossGen.props'}",
            f"/p:Crossgen2SdkOverrideTargetsPath={crossgen / 'Microsoft.NET.CrossGen.targets'}",
        ]
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
                        command = [
                            str(dotnet), "msbuild", str(project), f"/p:DirectoryBuildPropsPath={file}",
                            f"/p:TargetOS={target}", f"/p:TargetArchitecture={arch}",
                            f"/p:DotNetBuildSourceOnly={str(source_only).lower()}", "/p:Configuration=Release",
                            "-getItem:PackageReference,NativeRuntimeAsset", "-nologo", "-verbosity:quiet",
                        ]
                        if flavor == "CoreCLR":
                            missing = Path(folder) / "unbuilt/Microsoft.NET.CrossGen.props"
                            self.assertFalse(missing.exists())
                            failure = producer.subprocess.run(
                                command + [f"/p:Crossgen2SdkOverridePropsPath={missing}"],
                                cwd=ROOT, env=env, stdout=producer.subprocess.PIPE,
                                stderr=producer.subprocess.STDOUT, text=True)
                            self.assertNotEqual(failure.returncode, 0)
                            self.assertIn("MSB4019", failure.stdout)
                            self.assertIn(str(missing), failure.stdout)
                        result = producer.subprocess.run(command + crossgen_imports,
                            cwd=ROOT, env=env, stdout=producer.subprocess.PIPE,
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

    def template_fixture(self, output, observations, crlf=False):
        """Synthetic command attestations; real-Git capture is exercised separately."""
        recipe = self.recipe()
        identities = producer.template_identities(recipe)
        sidecar = {"schemaVersion": 1, "kind": "mono-startup-template-evidence", "status": "completed", "records": []}
        templates = []

        def observation(name, argv, cwd, data, stderr=False):
            log = output / (name + ".log")
            log.write_bytes(data)
            if stderr:
                (output / (name + ".stderr.log")).write_bytes(b"")
            observations.append({"name": name, "argv": argv, "cwd": cwd, "status": "completed", "exitCode": 0,
                                 "logFileName": log.name, "logSha256": producer.sha256(log)})
            return producer.reference(log)

        observation("source-head", ["git", "rev-parse", "HEAD"], "synthetic-runtime", (SOURCE + "\n").encode())
        observation("template-head", ["git", "rev-parse", "HEAD"], "synthetic-external",
                    (producer.EXPECTED_1ES_COMMIT + "\n").encode())
        for index, identity in enumerate(identities):
            canonical = ("# SYNTHETIC TEMPLATE INPUT, NOT ACTUAL GIT ATTESTATION\n" + identity["path"] + "\n").encode()
            raw = canonical.replace(b"\n", b"\r\n") if crlf else canonical
            cwd = "synthetic-runtime" if index < 10 else "synthetic-external"
            operand = identity["commit"] + ":" + identity["path"]
            name = f"template-{index:02d}"
            raw_file = output / (name + "-raw.bin")
            raw_file.write_bytes(raw)
            blob_id = producer.hashlib.sha1(b"blob " + str(len(canonical)).encode() + b"\0" + canonical).hexdigest()
            observation(name + "-size", ["git", "cat-file", "-s", operand], cwd, (str(len(canonical)) + "\n").encode(), True)
            blob_ref = observation(name + "-blob", ["git", "cat-file", "blob", operand], cwd, canonical, True)
            id_ref = observation(name + "-id", ["git", "rev-parse", "--verify", operand], cwd, (blob_id + "\n").encode(), True)
            sidecar["records"].append({**identity, "gitBlobId": blob_id, "blobIdEvidence": id_ref,
                                       "canonicalFile": blob_ref, "rawFile": producer.reference(raw_file),
                                       "transform": "lf-to-crlf" if crlf else "identity"})
            templates.append({**identity, "sha256": producer.sha256(raw_file)})
        producer.write_json(output / producer.TEMPLATE_SIDECAR, sidecar)
        return templates

    def real_template_fixture(self, base, crlf=False):
        """Create isolated Git objects and exercise actual binary producer capture."""
        source = base / "source"
        external = base / "external"
        output = base / "evidence"
        output.mkdir(parents=True)
        commits = []
        for root, paths in ((source, producer.TEMPLATE_PATHS), (external, ["v1/1ES.Official.PipelineTemplate.yml"])):
            root.mkdir()
            def git(*args):
                return producer.subprocess.run(["git", *args], cwd=root, env=dict(producer.os.environ),
                                               check=True, stdout=producer.subprocess.PIPE,
                                               stderr=producer.subprocess.PIPE).stdout
            git("init", "--quiet")
            git("config", "core.autocrlf", "true")
            for index, path in enumerate(paths):
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes((b"\xef\xbb\xbf" if index == 1 else b"") +
                                 b"# Synthetic real-Git test only\nvalue: fixture\n")
            git("add", "--", *paths)
            tree = git("write-tree").decode().strip()
            commit = git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit-tree", tree, "-m", "Synthetic template test\n\nCo-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>").decode().strip()
            git("update-ref", "HEAD", commit)
            if crlf:
                for path in paths:
                    (root / path).unlink()
                git("checkout-index", "--force", "--", *paths)
                git("add", "--", *paths)
                self.assertEqual(git("write-tree").decode().strip(), tree)
            commits.append(commit)
        recipe = self.recipe(source=commits[0])
        recipe["expected_1es_commit"] = commits[1]
        env = dict(producer.os.environ, STARTUP_1ES_COMMIT=commits[1], STARTUP_1ES_ROOT=str(external))
        observations = []
        producer.execute(["git", "rev-parse", "HEAD"], source, env, output, "source-head", observations)
        return source, recipe, env, output, observations

    def test_real_git_template_capture_and_finite_transforms(self):
        for crlf in (False, True):
            with self.subTest(crlf=crlf), tempfile.TemporaryDirectory() as folder:
                source, recipe, env, output, observations = self.real_template_fixture(Path(folder), crlf)
                templates = producer.template_evidence(source, recipe, env, output, observations)
                ref = producer.reference(output / producer.TEMPLATE_SIDECAR)
                identities = producer.validate_template_evidence(output, recipe, templates, ref, observations)
                sidecar = producer.read_json(output / producer.TEMPLATE_SIDECAR)
                self.assertEqual(len(identities), 11)
                self.assertEqual(len(observations), 36)  # source head + external head/status + 33 captures
                self.assertLessEqual(27 + len(observations) - 3, 64)
                for record, raw_record in zip(sidecar["records"], templates):
                    self.assertEqual(record["transform"], "lf-to-crlf" if crlf else "identity")
                    self.assertEqual(record["rawFile"]["sha256"], raw_record["sha256"])
                    self.assertEqual({key: record[key] for key in ("repository", "commit", "path")},
                                     {key: raw_record[key] for key in ("repository", "commit", "path")})
                self.assertTrue((output / "template-01-blob.log").read_bytes().startswith(b"\xef\xbb\xbf"))
                self.assertTrue((output / "template-01-raw.bin").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_template_transform_rejects_non_checkout_changes(self):
        for canonical in (b"value\nnext\n", b"\xef\xbb\xbfvalue\nnext\n"):
            self.assertEqual(producer.template_transform(canonical, canonical), "identity")
            self.assertEqual(producer.template_transform(canonical, canonical.replace(b"\n", b"\r\n")), "lf-to-crlf")
            for raw in (canonical.rstrip(), canonical + b" ", canonical.replace(b"\n", b"\r"),
                        canonical.replace(b"\n", b"\r\n", 1), canonical + b"\xef\xbb\xbf",
                        canonical.removeprefix(b"\xef\xbb\xbf") if canonical.startswith(b"\xef") else b"\xef\xbb\xbf" + canonical):
                with self.subTest(canonical=canonical, raw=raw), self.assertRaises(ValueError):
                    producer.template_transform(canonical, raw)
        for canonical, raw in ((b"\xff\n", b"\xff\n"), (b"a\0\n", b"a\0\n"),
                               (b"a\r\nb\n", b"a\r\nb\r\n"), (b"value", b"value\r\n")):
            with self.subTest(canonical=canonical), self.assertRaises(ValueError):
                producer.template_transform(canonical, raw)

    def test_template_capture_failure_preserves_discriminator(self):
        with tempfile.TemporaryDirectory() as folder:
            source, recipe, env, output, observations = self.real_template_fixture(Path(folder))
            raw = source / producer.TEMPLATE_PATHS[0]
            raw.write_bytes(b"altered template\n")
            with self.assertRaisesRegex(ValueError, "Unsupported template"):
                producer.template_evidence(source, recipe, env, output, observations)
            sidecar = producer.read_json(output / producer.TEMPLATE_SIDECAR)
            self.assertEqual(sidecar["status"], "failed")
            self.assertEqual(sidecar["records"], [])
            self.assertEqual((output / "template-00-raw.bin").read_bytes(), raw.read_bytes())
            self.assertTrue((output / "template-00-blob.log").is_file())
            self.assertTrue((output / "template-00-id.log").is_file())
            receipt = {"templateEvidence": None}
            producer.persist_receipt(output, receipt)
            self.assertEqual(receipt["templateEvidence"], producer.reference(output / producer.TEMPLATE_SIDECAR))
            self.assertIn(producer.TEMPLATE_SIDECAR, {item["path"] for item in receipt["retained_files"]})

    def test_template_pre_read_limits_and_exact_chain(self):
        expected = [
            "eng/pipelines/runtime-official.yml", "eng/pipelines/mono-android-startup-metadata.yml",
            "eng/pipelines/common/templates/pipeline-with-resources.yml",
            "eng/pipelines/common/templates/templateDispatch.yml",
            "eng/pipelines/common/templates/template1es-mono-startup.yml",
            "eng/pipelines/common/templates/template1es-body.yml",
            "eng/common/templates-official/job/job.yml", "eng/common/core-templates/job/job.yml",
            "eng/common/core-templates/steps/install-microbuild.yml", "eng/Signing.props",
            "v1/1ES.Official.PipelineTemplate.yml",
        ]
        self.assertEqual([item["path"] for item in producer.template_identities(self.recipe())], expected)
        for limit_kind in ("file", "aggregate"):
            with self.subTest(limit=limit_kind), tempfile.TemporaryDirectory() as folder:
                source, recipe, env, output, observations = self.real_template_fixture(Path(folder))
                field = "TEMPLATE_BYTES" if limit_kind == "file" else "TEMPLATE_TOTAL_BYTES"
                with patch.object(producer, field, 1), self.assertRaisesRegex(ValueError, "bound"):
                    producer.template_evidence(source, recipe, env, output, observations)
                self.assertFalse((output / "template-00-raw.bin").exists())
                self.assertFalse((output / "template-00-blob.log").exists())
                self.assertEqual(producer.read_json(output / producer.TEMPLATE_SIDECAR)["status"], "failed")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            observations = []
            templates = self.template_fixture(output, observations)
            reference = producer.reference(output / producer.TEMPLATE_SIDECAR)
            with patch.object(producer, "TEMPLATE_TOTAL_BYTES", 1), self.assertRaisesRegex(ValueError, "aggregate"):
                producer.validate_template_evidence(output, self.recipe(), templates, reference, observations)
            for changed in (observations + [observations[0]], observations + [None]):
                with self.assertRaises(ValueError):
                    producer.validate_template_evidence(output, self.recipe(), templates, reference, changed)
            templates[0]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "link"):
                producer.validate_template_evidence(output, self.recipe(), templates, reference, observations)

    def test_template_binary_capture_bounds_and_stderr(self):
        for stdout_bytes, stderr_bytes, limit in ((131073, 0, 65537), (20, 0, 8), (0, 65537, 64), (0, 1, 64)):
            with self.subTest(stdout=stdout_bytes, stderr=stderr_bytes), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                observations = []
                command = [producer.sys.executable, "-c",
                           f"import os; os.write(1,b'x'*{stdout_bytes}); os.write(2,b'y'*{stderr_bytes})"]
                with self.assertRaises(ValueError):
                    producer.execute(command, root, dict(producer.os.environ), root, "capture", observations,
                                     stdout_limit=limit)
                self.assertLessEqual((root / "capture.log").stat().st_size, limit)
                self.assertLessEqual((root / "capture.stderr.log").stat().st_size, 65536)
                self.assertEqual(len(observations), 1)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = b"\xef\xbb\xbfvalue\r\nnext\n"
            producer.execute([producer.sys.executable, "-c", f"import os; os.write(1,{data!r})"],
                             root, dict(producer.os.environ), root, "capture", [], stdout_limit=len(data))
            self.assertEqual((root / "capture.log").read_bytes(), data)

    def test_template_proof_rejects_forged_fields_and_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            observations = []
            templates = self.template_fixture(output, observations, crlf=True)
            original = producer.read_json(output / producer.TEMPLATE_SIDECAR)
            producer.validate_template_evidence(output, self.recipe(), templates,
                                                producer.reference(output / producer.TEMPLATE_SIDECAR), observations)
            for mutation in ("oid", "transform", "reorder", "duplicate", "missing", "extra"):
                sidecar = json.loads(json.dumps(original))
                if mutation == "oid":
                    sidecar["records"][0]["gitBlobId"] = "0" * 40
                elif mutation == "transform":
                    sidecar["records"][0]["transform"] = "identity"
                elif mutation == "reorder":
                    sidecar["records"].reverse()
                elif mutation == "duplicate":
                    sidecar["records"][1] = sidecar["records"][0]
                elif mutation == "missing":
                    sidecar["records"].pop()
                else:
                    sidecar["records"].append(sidecar["records"][0])
                producer.write_json(output / producer.TEMPLATE_SIDECAR, sidecar)
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    producer.validate_template_evidence(output, self.recipe(), templates,
                                                        producer.reference(output / producer.TEMPLATE_SIDECAR), observations)
            producer.write_json(output / producer.TEMPLATE_SIDECAR, original)
            for name, key, value in (("template-00-blob", "cwd", "wrong-repository"),
                                     ("template-00-id", "argv", ["git", "rev-parse", "HEAD"]),
                                     ("source-head", "status", "failed"), ("template-head", "exitCode", 1)):
                changed = json.loads(json.dumps(observations))
                next(item for item in changed if item["name"] == name)[key] = value
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    producer.validate_template_evidence(output, self.recipe(), templates,
                                                        producer.reference(output / producer.TEMPLATE_SIDECAR), changed)
            for name, data in (("template-00-size.log", b"0001\n"), ("source-head.log", (SOURCE + "\r\n").encode()),
                               ("template-00-id.log", b"0" * 40 + b"\n"), ("template-00-blob.stderr.log", b"warning")):
                file = output / name
                before = file.read_bytes()
                file.write_bytes(data)
                changed = json.loads(json.dumps(observations))
                for item in changed:
                    if item["logFileName"] == name:
                        item["logSha256"] = producer.sha256(file)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    producer.validate_template_evidence(output, self.recipe(), templates,
                                                        producer.reference(output / producer.TEMPLATE_SIDECAR), changed)
                file.write_bytes(before)

    def build_fixture(self, folder, build_attempt=1):
        folder.mkdir()
        packages = folder / "packages"
        packages.mkdir()
        recipe = self.recipe()
        with patch.dict(producer.os.environ, {**self.pipeline_environment(), "SYSTEM_JOBATTEMPT": str(build_attempt)}):
            identity = producer.pipeline_identity(SOURCE, "BuildRuntimePacks")
        receipt = {**recipe, "schemaVersion": 5, "kind": "mono-android-startup-build-receipt",
                   "status": "produced-unsigned-unadmitted", "pipeline": identity,
                   "packages": [], "package_sidecars": [], "command_observations": [], "native_configuration": {},
                   "experiment_attempt": 1, "templateEvidence": None}
        receipt["templates"] = self.template_fixture(folder, receipt["command_observations"])
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

    @patch.dict(producer.os.environ, {"PRODUCER_ATTEMPT": "1"})
    def test_build_artifact_binding_and_duplicate_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            build = self.build_fixture(root / "input")
            with patch.dict(producer.os.environ, self.pipeline_environment("TestSignRuntimePacks")):
                identity = producer.pipeline_identity(SOURCE, "TestSignRuntimePacks")
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

    def test_build_role_schema_and_actual_dependency_attempt_binding(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(producer.os.environ, {**self.pipeline_environment("TestSignRuntimePacks"),
                                                 "PRODUCER_ATTEMPT": "2", "SYSTEM_JOBATTEMPT": "3"}):
            root = Path(folder)
            build = self.build_fixture(root / "input", build_attempt=2)
            identity = producer.pipeline_identity(SOURCE, "TestSignRuntimePacks")
            self.assertEqual(producer.validate_build_input(root / "input", self.recipe(), identity), build)
            for key, value in (("stageName", None), ("stageName", "WrongStage"), ("phaseName", None),
                               ("phaseName", "TestSignRuntimePacks"), ("jobName", "bad/name"),
                               ("jobAttempt", None), ("jobAttempt", "2"), ("jobAttempt", True), ("jobAttempt", 1),
                               ("unexpected", "field")):
                changed = {**build, "pipeline": {**build["pipeline"], key: value}}
                if value is None:
                    del changed["pipeline"][key]
                producer.write_json(root / "input/receipt.json", changed)
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    producer.validate_build_input(root / "input", self.recipe(), identity)
            for version in (3, 4):
                producer.write_json(root / "input/receipt.json", {**build, "schemaVersion": version})
                with self.assertRaisesRegex(ValueError, "schema-5"):
                    producer.validate_build_input(root / "input", self.recipe(), identity)
            producer.write_json(root / "input/receipt.json", build)
            for value in (None, "", "1", "02", "2\n", "2147483648"):
                with self.subTest(producer_attempt=value), patch.dict(producer.os.environ):
                    if value is None:
                        producer.os.environ.pop("PRODUCER_ATTEMPT", None)
                    else:
                        producer.os.environ["PRODUCER_ATTEMPT"] = value
                    with self.assertRaisesRegex(ValueError, "downloaded producer attempt"):
                        producer.validate_build_input(root / "input", self.recipe(), identity)

    def test_sign_preflight_rejects_wrong_download_attempt_before_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            self.build_fixture(base / "input", build_attempt=2)
            root = base / "source"
            root.mkdir()
            output = base / "output"
            with patch.dict(producer.os.environ, self.pipeline_environment("TestSignRuntimePacks")), \
                    patch.object(producer.sys, "platform", "win32"), patch.object(producer.subprocess, "run") as external:
                with self.assertRaisesRegex(ValueError, "downloaded producer attempt"):
                    producer.test_sign(root, self.recipe(), base / "input", output)
                external.assert_not_called()
            receipt = producer.read_json(output / "receipt.json")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["pipeline"]["jobName"], "__default")
            self.assertEqual(receipt["pipeline"]["phaseName"], "TestSignRuntimePacks")
            self.assertIn("providerContext", receipt)
            self.assertEqual(receipt["packages"], [])
            self.assertEqual(receipt["command_observations"], [])
            self.assertEqual(receipt["retained_files"], [])
            self.assertFalse(list(output.rglob("postsign.*.json")))

    def test_preflight_platform_and_output_safety(self):
        for signing in (False, True):
            with self.subTest(signing=signing), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                root = base / "source"
                root.mkdir()
                output = base / "output"
                with patch.dict(producer.os.environ, self.pipeline_environment()), \
                        patch.object(producer.sys, "platform", "linux" if signing else "win32"), \
                        patch.object(producer.subprocess, "run") as external:
                    with self.assertRaisesRegex(ValueError, "Windows" if signing else "Linux"):
                        if signing:
                            producer.test_sign(root, self.recipe(), base / "input", output)
                        else:
                            producer.produce(root, self.recipe(), base / "ndk", "27.2.12479018", output)
                    original = (output / "receipt.json").read_bytes()
                    receipt = producer.read_json(output / "receipt.json")
                    self.assertEqual(receipt["status"], "failed")
                    self.assertIsNone(receipt["pipeline"])
                    self.assertIn("providerContext", receipt)
                    with self.assertRaises(FileExistsError):
                        if signing:
                            producer.test_sign(root, self.recipe(), base / "input", output)
                        else:
                            producer.produce(root, self.recipe(), base / "ndk", "27.2.12479018", output)
                    self.assertEqual((output / "receipt.json").read_bytes(), original)
                    if signing:
                        with self.assertRaisesRegex(ValueError, "disjoint"):
                            producer.test_sign(root, self.recipe(), output, output / "nested")
                        self.assertFalse((output / "nested").exists())
                    external.assert_not_called()

    def sign_fixture(self, folder, verification_exit=0, sign_exit=0, verifier_launch_failure=False,
                     sign_attempt=1, build_attempt=1, sign_type="test", phase=None):
        """Exercise real ZIP/receipt orchestration with explicitly mocked external signing boundaries."""
        input_folder = folder / "input"
        build = self.build_fixture(input_folder, build_attempt)
        root = folder / "source"
        root.mkdir()
        producer.write_json(root / "global.json", {"msbuild-sdks": {"Microsoft.DotNet.Arcade.Sdk": "test-only-sdk"}})
        output = folder / "output"
        def test_only_command(command, **kwargs):
            stdout = kwargs["stdout"]
            if command[0] == "pwsh":
                self.assertIn("/p:Publish=false", command)
                self.assertIn(f"/p:SignType={sign_type}", command)
                self.assertIn(f"/p:DotNetSignType={sign_type}", command)
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
                    "Properties": {"OfficialBuild": "true", "SignType": sign_type, "DotNetSignType": sign_type,
                                   "ForceDryRunSigning": "", "PostBuildSign": "false", "Publish": "false",
                                   "DotNetPublishUsingPipelines": "false", "TargetRid": "android-" + arch},
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
        environment = {**self.pipeline_environment(phase or ("RealSignRuntimePacks" if sign_type == "real" else
                                                              "TestSignRuntimePacks")), "SYSTEM_JOBATTEMPT": str(sign_attempt),
                       "PRODUCER_ATTEMPT": str(build_attempt)}
        with patch.dict(producer.os.environ, environment), \
                patch.object(producer.sys, "platform", "win32"), \
                patch.object(producer, "source_evidence", return_value=build["patch_sha256"]), \
                patch.object(producer, "template_evidence", side_effect=lambda root, recipe, env, output, observations:
                             self.template_fixture(output, observations, crlf=True)), \
                patch.object(producer.subprocess, "run", side_effect=test_only_command):
            if sign_exit or verification_exit or verifier_launch_failure:
                with self.assertRaises((ValueError, FileNotFoundError)):
                    producer.sign_runtime_packs(root, self.recipe(), input_folder, output, sign_type)
            else:
                producer.sign_runtime_packs(root, self.recipe(), input_folder, output, sign_type)
        return output

    def test_real_sign_evidence_and_phase_are_isolated_from_test(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as folder:
                output = self.sign_fixture(Path(folder), sign_type="real", verification_exit=exit_code,
                                           sign_attempt=2, build_attempt=1)
                receipt = producer.read_json(output / "receipt.json")
                self.assertEqual(receipt["pipeline"]["phaseName"], "RealSignRuntimePacks")
                self.assertEqual(receipt["signer"]["requestedSignType"], "Real")
                self.assertFalse(receipt["publication"])
                self.assertEqual(len(receipt["packages"]), 2)
                for entry in receipt["packages"]:
                    post = producer.read_json(output / "postsign" / entry["fileName"])
                    self.assertEqual(post["signer"]["requestedSignType"], "Real")
                    sign = producer.read_json(output / "postsign" / f"sign.{post['rid']}.receipt.json")
                    self.assertEqual(sign["operation"], "normal-real-sign")
                    self.assertEqual(post["input"]["sha256"],
                                     producer.sha256(output / "build/packages" / post["input"]["fileName"]))
                self.assertEqual(receipt["status"],
                                 "verified-policy-unqualified" if exit_code == 0 else "produced-verification-failed")

    def test_real_sign_rejects_test_phase_before_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "required RealSignRuntimePacks"):
                self.sign_fixture(Path(folder), sign_type="real", phase="TestSignRuntimePacks")
            receipt = producer.read_json(Path(folder) / "output/receipt.json")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["command_observations"], [])

    def test_postsign_success_and_failed_verification_preserve_evidence(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as folder:
                output = self.sign_fixture(Path(folder), verification_exit=exit_code, sign_attempt=3, build_attempt=2)
                root = producer.read_json(output / "receipt.json")
                self.assertEqual(root["schemaVersion"], 3)
                self.assertNotIn("providerContext", root)
                self.assertEqual(root["pipeline"]["phaseName"], "TestSignRuntimePacks")
                self.assertEqual(root["pipeline"]["jobName"], "__default")
                build = producer.read_json(output / "build/receipt.json")
                self.assertEqual(build["pipeline"]["jobAttempt"], 2)
                self.assertEqual(build["experiment_attempt"], 1)
                self.assertNotEqual(root["signer"]["templates"], build["templates"])
                self.assertLessEqual(len(build["command_observations"]), 64)
                self.assertLessEqual(len(root["command_observations"]), 128)
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
                    self.assertEqual(post["signer"]["jobAttempt"], 3)
                    self.assertEqual(post["signer"]["jobName"], "__default")
                    self.assertEqual(set(post["signer"]), {"organization", "project", "definitionId", "pipelinePath",
                                                          "pipelineCommit", "buildId", "jobAttempt", "jobName",
                                                          "requestedSignType", "templates"})
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
                self.assertNotIn("providerContext", root)
                self.assertEqual(root["pipeline"]["phaseName"], "TestSignRuntimePacks")
                self.assertEqual(root["packages"], [])
                self.assertEqual(len(list((output / "postsign").glob("*.nupkg"))), 1)
                self.assertEqual(len(list((output / "build/packages").glob("*.nupkg"))), 2)
                last = root["command_observations"][-1]
                self.assertEqual(last["status"], "failed" if "sign_exit" in arguments else "launch-failed")
                self.assertEqual(last["exitCode"], 7 if "sign_exit" in arguments else None)
                self.assertFalse(list((output / "postsign").glob("postsign.*.json")))

    def test_signing_selection_and_properties_fail_closed(self):
        package = Path("exact-package.nupkg").resolve()
        evaluation = {"Properties": {"OfficialBuild": "true", "SignType": "test", "DotNetSignType": "test",
                                      "ForceDryRunSigning": "", "PostBuildSign": "false", "Publish": "false",
                                      "DotNetPublishUsingPipelines": "false", "TargetRid": "android-x64"},
                      "Items": {"ItemsToSign": [{"FullPath": str(package)}]}}
        producer.validate_signing_selection(evaluation, package, "android-x64")
        real = {**evaluation, "Properties": {**evaluation["Properties"], "SignType": "real",
                                             "DotNetSignType": "real"}}
        producer.validate_signing_selection(real, package, "android-x64", "real")
        for mode, selected in (("real", evaluation), ("test", real)):
            with self.assertRaisesRegex(ValueError, "properties"):
                producer.validate_signing_selection(selected, package, "android-x64", mode)
        for key, value in [("OfficialBuild", "false"), ("SignType", "real"), ("DotNetSignType", "real"),
                           ("ForceDryRunSigning", "true"), ("PostBuildSign", "true"), ("Publish", "true"),
                           ("DotNetPublishUsingPipelines", "true"), ("TargetRid", "android-arm64")]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "properties"):
                producer.validate_signing_selection({**evaluation, "Properties": {**evaluation["Properties"], key: value}},
                                                    package, "android-x64")
        for items in ([], [{"FullPath": "other.nupkg"}], evaluation["Items"]["ItemsToSign"] * 2):
            with self.assertRaisesRegex(ValueError, "exactly"):
                producer.validate_signing_selection({**evaluation, "Items": {"ItemsToSign": items}}, package, "android-x64")
        for value in ("", "123.1", "20260924.1\n", "20260924.1;echo"):
            with self.assertRaisesRegex(ValueError, "OfficialBuildId"):
                producer.signing_properties(self.recipe(), "android-x64", value)
        self.assertIn("/p:DotNetSignType=real",
                      producer.signing_properties(self.recipe(), "android-arm64", "20260924.1", "real"))
        for mode in ("", "Test", "dryrun", "real;echo"):
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                producer.signing_properties(self.recipe(), "android-x64", "20260924.1", mode)

    def test_producer_refuses_nonmanual(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(producer.sys, "platform", "linux"), \
                patch.dict(producer.os.environ, {"BUILD_REASON": "PullRequest"}):
            with self.assertRaisesRegex(ValueError, "manual"):
                producer.produce(Path(folder), self.recipe(), Path("missing"), "28.2.13676358")
            receipt = producer.read_json(Path(folder) / "artifacts/startup-metadata-producer/receipt.json")
            self.assertEqual(receipt["status"], "failed")
            self.assertIsNone(receipt["pipeline"])


if __name__ == "__main__":
    unittest.main()
