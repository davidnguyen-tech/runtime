from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[4]
PIPELINE = ROOT / "eng" / "pipelines" / "performance" / "perf.yml"

EXPERIMENT_PARAMETERS = {
    "runtimePackageMode": "false",
    "runtimePackageVersion": "''",
    "runtimePackageFeed": "'https://pkgs.dev.azure.com/dnceng/public/_packaging/dotnet-experimental/nuget/v3/index.json'",
    "runtimePackageRid": "linux-x64",
    "runtimePackageSha512": "''",
    "runtimePackageSourceCommit": "''",
    "runtimePackageSourceBranch": "''",
    "runtimePackageProducerBuildId": "''",
    "runtimePackagePlatform": "linux_x64",
}

DISABLED_RUNTIME_TOGGLES = (
    "viperMicroRuntimeAsync",
    "cobaltMicroRuntimeAsync",
    "monoMicro",
    "monoInterpreter",
    "monoAot",
    "androidCoreclrJit",
    "cobaltMicro",
    "cobaltSveMicro",
    "cobaltMicroR2RInterpreter",
    "androidCoreclrR2r",
)

RUNTIME_PACKAGE_ENVIRONMENT = (
    "PERFLAB_RUNTIME_PACKAGE_VERSION",
    "PERFLAB_RUNTIME_PACKAGE_RID",
    "PERFLAB_RUNTIME_PACKAGE_SUITE",
    "PERFLAB_RUNTIME_PACKAGE_GC",
    "PERFLAB_REPO",
    "PERFLAB_BRANCH",
    "PERFLAB_HASH",
    "PERFLAB_RUNNAME",
    "RUNTIME_PACKAGE_BUILD_ID",
    "RUNTIME_PACKAGE_COMMIT",
    "RUNTIME_PACKAGE_FEED",
    "RUNTIME_PACKAGE_ID",
    "RUNTIME_PACKAGE_RID",
    "RUNTIME_PACKAGE_SHA512",
)


class PerfPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PIPELINE.read_text(encoding="utf-8")
        candidate_match = re.search(
            r"^      - \$\{\{ if and\(eq\(parameters\.runtimePackageMode, true\).*",
            cls.text,
            re.MULTILINE | re.DOTALL,
        )
        if candidate_match is None:
            raise AssertionError("The runtime package candidate block is missing.")
        cls.candidate = candidate_match.group(0)

    def test_experiment_parameters_default_off_with_reviewed_platform(self):
        for name, expected_default in EXPERIMENT_PARAMETERS.items():
            with self.subTest(parameter=name):
                match = re.search(
                    rf"  - name: {name}\n    type: (boolean|string)\n    default: (.+)",
                    self.text,
                )
                self.assertIsNotNone(match)
                self.assertEqual(expected_default, match.group(2))

        for name in ("runtimePackageRid", "runtimePackagePlatform"):
            self.assertRegex(
                self.text,
                rf"  - name: {name}\n"
                r"    type: string\n"
                r"    default: linux[-_]x64\n"
                r"    values:\n"
                r"    - linux[-_]x64",
            )

    def test_default_flow_retains_standard_wasm_and_runtime_templates(self):
        self.assertIn(
            "- ${{ if eq(parameters.runtimePackageMode, false) }}:\n"
            "        - template: /eng/pipelines/runtime-wasm-perf-jobs.yml@performance",
            self.text,
        )
        self.assertIn(
            "- ${{ if and(eq(parameters.runtimePackageMode, false), "
            "ne(variables['System.TeamProject'], 'public'), "
            "notin(variables['Build.Reason'], 'Schedule')) }}:\n"
            "        - template: /eng/pipelines/runtime-perf-jobs.yml@performance",
            self.text,
        )
        self.assertIn("enableHelixJobMonitor", self.text)
        self.assertIn("/eng/common/core-templates/job/helix-job-monitor.yml", self.text)

    def test_candidate_is_one_ordinary_linux_x64_viper_lane(self):
        self.assertNotIn("runtime-wasm-perf-jobs.yml", self.candidate)
        self.assertEqual(2, self.candidate.count("runtimePackageMode: true"))
        self.assertIn(
            "viperMicro:\n"
            "              enabled: true\n"
            "              configs:\n"
            "              - ${{ parameters.runtimePackagePlatform }}",
            self.candidate,
        )
        for toggle in DISABLED_RUNTIME_TOGGLES:
            with self.subTest(toggle=toggle):
                self.assertRegex(
                    self.candidate,
                    rf"            {toggle}:\n              enabled: false",
                )
        self.assertNotIn("additionalJobIdentifier:", self.candidate)
        self.assertNotIn("experimentName:", self.candidate)

    def test_candidate_forwards_only_reviewed_package_environment(self):
        self.assertIn("runtimePackageMode: true", self.candidate)
        environment_match = re.search(
            r"              runEnvVars:\n"
            r"(?P<entries>(?:              - [A-Z0-9_]+=.*\n)+)",
            self.candidate,
        )
        self.assertIsNotNone(environment_match)
        names = tuple(
            line.strip()[2:].split("=", 1)[0]
            for line in environment_match.group("entries").splitlines()
        )
        self.assertEqual(RUNTIME_PACKAGE_ENVIRONMENT, names)
        self.assertNotRegex(self.text, r"name: runtimePackage(?:Environment|Env|RunEnvVars)")

    def test_feature_branch_pins_matching_performance_contract(self):
        self.assertIn(
            "ref: refs/heads/davidnguyen-tech-consume-runtimelab-gc-experiment",
            self.text,
        )
        self.assertIn(
            "Restore the merged/default performance ref before merging this runtime branch.",
            self.text,
        )


if __name__ == "__main__":
    unittest.main()
