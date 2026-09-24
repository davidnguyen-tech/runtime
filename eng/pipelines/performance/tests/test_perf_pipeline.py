import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[4]
PERF = ROOT / "eng" / "pipelines" / "performance" / "perf.yml"
PERF_SLOW = ROOT / "eng" / "pipelines" / "performance" / "perf-slow.yml"

COMMON_PARAMETERS = {
    "externalRuntimeMode": ("boolean", "false"),
    "externalRuntimeContractVersion": ("string", "''"),
    "externalRuntimeCoverageRowsSha256": ("string", "''"),
    "externalRuntimeProducerDefinitionId": ("number", "0"),
    "externalRuntimeProducerBuildId": ("number", "0"),
    "externalRuntimeProducerBuildNumber": ("string", "''"),
    "externalRuntimeSourceRepository": ("string", "''"),
    "externalRuntimeSourceBranch": ("string", "''"),
    "externalRuntimeSourceCommit": ("string", "''"),
    "externalRuntimeCampaignId": ("string", "''"),
    "externalRuntimeCohortId": ("string", "''"),
    "externalRuntimeCohortManifestSha256": ("string", "''"),
    "externalRuntimeSkipPerfLabUpload": ("boolean", "true"),
    "externalRuntimeIdempotencyKey": ("string", "''"),
    "externalRuntimeAttempt": ("number", "1"),
}

EXTERNAL_RUNTIME_FIELDS = (
    ("contractVersion", "externalRuntimeContractVersion"),
    ("coverageRowsSha256", "externalRuntimeCoverageRowsSha256"),
    ("producerDefinitionId", "externalRuntimeProducerDefinitionId"),
    ("producerBuildId", "externalRuntimeProducerBuildId"),
    ("producerBuildNumber", "externalRuntimeProducerBuildNumber"),
    ("sourceRepository", "externalRuntimeSourceRepository"),
    ("sourceBranch", "externalRuntimeSourceBranch"),
    ("sourceCommit", "externalRuntimeSourceCommit"),
    ("campaignId", "externalRuntimeCampaignId"),
    ("cohortId", "externalRuntimeCohortId"),
    ("cohortManifestSha256", "externalRuntimeCohortManifestSha256"),
    ("artifactMapJson", "externalRuntimeArtifactMapJson"),
    ("skipPerfLabUpload", "externalRuntimeSkipPerfLabUpload"),
    ("idempotencyKey", "externalRuntimeIdempotencyKey"),
    ("attempt", "externalRuntimeAttempt"),
)


def parameter_declaration(text, name):
    parameters = text.split("\ntrigger:", 1)[0]
    return re.search(
        rf"(?m)^ *\- name: {name}\n +type: ([a-z]+)\n +default: (.+)$",
        parameters,
    )


class PerfPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.perf = PERF.read_text(encoding="utf-8")
        cls.perf_slow = PERF_SLOW.read_text(encoding="utf-8")

    def test_common_external_runtime_parameters_are_typed_and_default_off(self):
        for path, text in ((PERF, self.perf), (PERF_SLOW, self.perf_slow)):
            for name, (expected_type, expected_default) in COMMON_PARAMETERS.items():
                with self.subTest(path=path.name, parameter=name):
                    match = parameter_declaration(text, name)
                    self.assertIsNotNone(match)
                    self.assertEqual((expected_type, expected_default), match.groups())

        for text in (self.perf, self.perf_slow):
            parameters = text.split("\ntrigger:", 1)[0]
            match = parameter_declaration(
                parameters,
                "externalRuntimeArtifactMapJson",
            )
            self.assertIsNotNone(match)
            self.assertEqual(
                ("string", """'{"schemaVersion":1,"entries":[]}'"""),
                match.groups(),
            )
            self.assertEqual(
                {"schemaVersion": 1, "entries": []},
                json.loads(match.group(2)[1:-1]),
            )
            self.assertNotIn("externalRuntimeArtifactMap\n", parameters)

    def test_disabled_702_graph_retains_native_templates_and_conditions(self):
        self.assertIn(
            "- template: /eng/pipelines/runtime-wasm-perf-jobs.yml@performance",
            self.perf,
        )
        self.assertIn(
            "- ${{ if and(ne(variables['System.TeamProject'], 'public'), "
            "notin(variables['Build.Reason'], 'Schedule')) }}:\n"
            "        - template: /eng/pipelines/runtime-perf-jobs.yml@performance",
            self.perf,
        )
        self.assertNotIn("if eq(parameters.externalRuntimeMode, false)", self.perf)
        self.assertIn("/eng/common/core-templates/job/helix-job-monitor.yml", self.perf)

    def test_disabled_1012_graph_retains_native_selectors(self):
        self.assertIn(
            "${{ if or(in(variables['Build.Reason'], 'Schedule'), "
            "parameters.runScheduledJobs) }}:",
            self.perf_slow,
        )
        self.assertIn(
            "${{ if or(notin(variables['Build.Reason'], 'Schedule', 'Manual'), "
            "parameters.runPrivateJobs) }}:",
            self.perf_slow,
        )
        self.assertIn(
            "- template: /eng/pipelines/runtime-slow-perf-jobs.yml@performance",
            self.perf_slow,
        )
        self.assertNotIn("if eq(parameters.externalRuntimeMode, false)", self.perf_slow)

    def test_complete_manifest_is_forwarded_without_row_filtering(self):
        expected_templates = {
            self.perf: (
                "runtime-wasm-perf-jobs.yml@performance",
                "runtime-perf-jobs.yml@performance",
            ),
            self.perf_slow: ("runtime-slow-perf-jobs.yml@performance",),
        }
        for text, templates in expected_templates.items():
            for template in templates:
                with self.subTest(template=template):
                    start = text.index(f"/eng/pipelines/{template}")
                    block = text[start : start + 2600]
                    self.assertIn("externalRuntimeMode: true", block)
                    self.assertIn("externalRuntime:", block)
                    for field, parameter in EXTERNAL_RUNTIME_FIELDS:
                        self.assertIn(
                            f"{field}: ${{{{ parameters.{parameter} }}}}",
                            block,
                        )
                    self.assertNotIn("each entry in", block)
                    self.assertNotIn("queueEligible", block)
                    self.assertNotIn("availability.status", block)

    def test_feature_branch_pins_both_performance_resources_for_preview(self):
        for text in (self.perf, self.perf_slow):
            self.assertIn(
                "ref: refs/heads/davidnguyen-tech-consume-runtimelab-gc-experiment",
                text,
            )
            self.assertIn(
                "Restore the merged/default performance ref before merging this runtime branch.",
                text,
            )

    def test_legacy_one_lane_contract_is_removed(self):
        combined = self.perf + self.perf_slow
        self.assertNotIn("runtimePackageMode", combined)
        self.assertNotIn("runtimePackageVersion", combined)
        self.assertNotIn("runtimePackagePlatform", combined)
        self.assertNotIn("runtimePackage:", combined)
        self.assertNotIn("artifactMap:", combined)


if __name__ == "__main__":
    unittest.main()
