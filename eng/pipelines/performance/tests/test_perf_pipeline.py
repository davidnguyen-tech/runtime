from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[4]
PIPELINE = ROOT / "eng" / "pipelines" / "performance" / "perf.yml"

EXPERIMENT_PARAMETERS = {
    "runtimePackageMode": "false",
    "runtimePackageVersion": "''",
    "runtimePackageRid": "linux-x64",
    "runtimePackageSha512": "''",
    "runtimePackageSourceCommit": "''",
    "runtimePackageSourceBranch": "''",
    "runtimePackageProducerBuildId": "''",
    "runtimePackagePlatform": "linux_x64",
}

RUNTIME_PACKAGE_FIELDS = (
    ("version", "runtimePackageVersion"),
    ("rid", "runtimePackageRid"),
    ("sha512", "runtimePackageSha512"),
    ("sourceCommit", "runtimePackageSourceCommit"),
    ("sourceBranch", "runtimePackageSourceBranch"),
    ("producerBuildId", "runtimePackageProducerBuildId"),
    ("platform", "runtimePackagePlatform"),
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
        self.assertEqual(1, self.candidate.count("runtimePackageMode: true"))
        self.assertNotIn("viperMicro:", self.candidate)
        self.assertNotIn("monoMicro:", self.candidate)
        self.assertNotIn("androidCoreclrJit:", self.candidate)
        self.assertNotIn("additionalJobIdentifier:", self.candidate)
        self.assertNotIn("experimentName:", self.candidate)

    def test_candidate_forwards_only_structured_package_identity(self):
        self.assertIn("runtimePackageMode: true", self.candidate)
        self.assertIn("runtimePackage:", self.candidate)
        for field, parameter in RUNTIME_PACKAGE_FIELDS:
            with self.subTest(field=field):
                self.assertIn(
                    f"{field}: ${{{{ parameters.{parameter} }}}}",
                    self.candidate,
                )
        self.assertNotIn("runEnvVars:", self.candidate)
        self.assertNotIn("jobParameters:", self.candidate)
        self.assertNotIn("runtimePackageFeed", self.text)
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
