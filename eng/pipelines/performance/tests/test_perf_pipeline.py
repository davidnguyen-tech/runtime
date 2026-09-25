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

PERF_ONLY_PARAMETERS = {
    "externalRuntimeScope": ("string", "full"),
}

EXTERNAL_RUNTIME_FIELDS = (
    ("contractVersion", "externalRuntimeContractVersion"),
    ("coverageRowsSha256", "externalRuntimeCoverageRowsSha256"),
    ("scope", "externalRuntimeScope"),
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

        for name, (expected_type, expected_default) in PERF_ONLY_PARAMETERS.items():
            match = parameter_declaration(self.perf, name)
            self.assertIsNotNone(match)
            self.assertEqual((expected_type, expected_default), match.groups())
            self.assertIsNone(parameter_declaration(self.perf_slow, name))
        self.assertIn(
            "  - name: externalRuntimeScope\n"
            "    type: string\n"
            "    default: full\n"
            "    values:\n"
            "      - full\n"
            "      - x64",
            self.perf,
        )

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
        self.assertIn(
            "- ${{ if not(and(eq(parameters.externalRuntimeMode, true), "
            "eq(parameters.externalRuntimeScope, 'x64'))) }}:\n"
            "        - template: /eng/pipelines/runtime-wasm-perf-jobs.yml@performance",
            self.perf,
        )

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
        self.assertIn(
            "It intentionally\n# does not expose externalRuntimeScope",
            self.perf_slow,
        )

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
                    fields = EXTERNAL_RUNTIME_FIELDS
                    if text == self.perf_slow:
                        fields = tuple(
                            item for item in fields if item[0] != "scope"
                        )
                    for field, parameter in fields:
                        self.assertIn(
                            f"{field}: ${{{{ parameters.{parameter} }}}}",
                            block,
                        )
                    self.assertNotIn("each entry in", block)
                    self.assertNotIn("queueEligible", block)
                    self.assertNotIn("availability.status", block)

    def test_exact_performance_commit_pins_both_resources_for_preview(self):
        for text in (self.perf, self.perf_slow):
            self.assertIn(
                "ref: 696188811beac80c11392efb6876ca60190d5e9b",
                text,
            )
            self.assertIn(
                "Restore the merged/default performance ref before merging this runtime branch.",
                text,
            )

    def test_x64_scope_selects_only_four_viper_rows(self):
        condition = (
            "${{ if and(eq(parameters.externalRuntimeMode, true), "
            "eq(parameters.externalRuntimeScope, 'x64')) }}:"
        )
        start = self.perf.index(condition, self.perf.index("runtime-perf-jobs.yml"))
        block = self.perf[start : self.perf.index("jobParameters:", start)]
        self.assertEqual(1, block.count("viperMicro:"))
        self.assertEqual(1, block.count("viperMicroRuntimeAsync:"))
        self.assertEqual(2, block.count("enabled: true"))
        self.assertEqual(4, block.count("- linux_x64") + block.count("- windows_x64"))

        disabled = (
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
        for toggle in disabled:
            with self.subTest(toggle=toggle):
                self.assertIn(f"{toggle}:\n                enabled: false", block)

    def test_legacy_one_lane_contract_is_removed(self):
        combined = self.perf + self.perf_slow
        self.assertNotIn("runtimePackageMode", combined)
        self.assertNotIn("runtimePackageVersion", combined)
        self.assertNotIn("runtimePackagePlatform", combined)
        self.assertNotIn("runtimePackage:", combined)
        self.assertNotIn("artifactMap:", combined)


if __name__ == "__main__":
    unittest.main()
