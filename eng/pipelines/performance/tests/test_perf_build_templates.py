from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = ROOT / "eng" / "pipelines" / "performance" / "templates"


def read_template(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


class PerfBuildTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = read_template("perf-build-jobs.yml")
        cls.coreclr = read_template("perf-coreclr-build-jobs.yml")
        cls.arm64 = read_template("perf-arm64-build-jobs.yml")
        cls.ios = read_template("perf-ios-scenarios-build-jobs.yml")
        cls.wasm = read_template("perf-wasm-build-jobs.yml")

    def test_external_runtime_mode_defaults_off_for_each_build_family(self):
        for name, text in (
            ("build", self.build),
            ("coreclr", self.coreclr),
            ("arm64", self.arm64),
            ("ios", self.ios),
            ("wasm", self.wasm),
        ):
            with self.subTest(template=name):
                self.assertIn("externalRuntimeMode: false", text)
                self.assertIn("externalRuntime: {}", text)

    def test_standard_coreclr_producers_are_suppressed_only_when_enabled(self):
        self.assertIn(
            "if and(ne(parameters.externalRuntimeMode, true), "
            "or(eq(parameters.linux_x64, true),",
            self.coreclr,
        )
        self.assertIn(
            "if and(ne(parameters.externalRuntimeMode, true), "
            "eq(parameters.android_arm64, true))",
            self.coreclr,
        )
        self.assertIn(
            "if and(ne(parameters.externalRuntimeMode, true), "
            "eq(parameters.coreclr_r2r_interpreter, true))",
            self.coreclr,
        )
        self.assertIn("BuildArtifacts_$(osGroup)", self.coreclr)
        self.assertIn("AndroidCoreCLR", self.coreclr)
        self.assertIn("coreclr_r2r_interpreter", self.coreclr)

    def test_top_level_build_retains_mono_and_forwards_external_runtime(self):
        self.assertIn(
            "template: /eng/pipelines/performance/templates/perf-mono-build-jobs.yml",
            self.build,
        )
        self.assertIn("mono_x64: true", self.build)
        self.assertIn("monoAot_x64: true", self.build)
        self.assertIn(
            "externalRuntimeMode: ${{ parameters.externalRuntimeMode }}",
            self.build,
        )
        self.assertIn("externalRuntime: ${{ parameters.externalRuntime }}", self.build)

    def test_arm64_retains_mono_and_mono_aot_while_suppressing_coreclr(self):
        self.assertIn("mono_arm64: ${{ parameters.mono }}", self.arm64)
        self.assertIn("monoAot_arm64: ${{ parameters.monoAot }}", self.arm64)
        self.assertIn("if eq(parameters.coreclr, true)", self.arm64)
        self.assertIn(
            "externalRuntimeMode: ${{ parameters.externalRuntimeMode }}",
            self.arm64,
        )
        self.assertIn("externalRuntime: ${{ parameters.externalRuntime }}", self.arm64)

    def test_ios_retains_mono_and_native_aot_but_suppresses_coreclr(self):
        self.assertIn("if eq(parameters.mono, true)", self.ios)
        self.assertIn("if eq(parameters.nativeAot, true)", self.ios)
        self.assertIn(
            "if and(ne(parameters.externalRuntimeMode, true), "
            "eq(parameters.coreclr, true))",
            self.ios,
        )
        self.assertIn("nameSuffix: iOSNativeAOT", self.ios)
        self.assertIn("nameSuffix: iOSCoreCLR", self.ios)

    def test_wasm_coreclr_build_remains_for_mono_workload_dependencies(self):
        self.assertNotIn("ne(parameters.externalRuntimeMode, true)", self.wasm)
        self.assertIn("nameSuffix: wasm_coreclr", self.wasm)
        self.assertIn(
            "dependsOn:\n"
            "      - build_browser_wasm_linux_Release_wasm_coreclr",
            self.wasm,
        )
        self.assertIn("downloadCoreClrWorkloadPackages: true", self.wasm)


if __name__ == "__main__":
    unittest.main()
