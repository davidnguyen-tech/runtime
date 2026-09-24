# Mono Android startup metadata

This diagnostic-only capture observes the existing Mono debugger component and
socket transport. It does not change debugger waits, deadlines, retries, socket
cleanup, protocol messages, or error handling.

## Gates and distribution

Normal builds compile the hooks away. An Android producer must explicitly set
the CMake cache variable `MONO_ANDROID_STARTUP_BUILD_ID` to a marker matching
`[a-z0-9][a-z0-9._-]{0,63}`. Non-Android builds remain disabled, even with a marker.
The marker is a producer label, **not** a measured binary hash.
Normal builds forward `/p:MonoAndroidStartupBuildId=<marker>` to CMake. The
configure command includes the value even when empty, so removing the opt-in
invalidates the cached configure command instead of retaining an enabled build.

At `mono_debugger_agent_init_internal`, the component takes one validated copy
of the effective `DOTNET_ANDROID_STARTUP_CAPTURE_ID` environment value. Exactly
32 lowercase hexadecimal characters enable capture; absent or malformed values
disable it without identity reads, clock sampling, formatting, or sink writes.
Android supplies this value through the normal APK `AndroidEnvironment` build
input and its existing environment setup. There is no global property gate.
The token is opaque capture correlation, not authentication or a debugger session
identifier. The consumer must require the matching Android producer token;
compiled Android input rejection does not guarantee runtime rejection after
existing environment overrides.

Only coherent, signed, uniquely versioned normal runtime packs for Android x64
and arm64 may be admitted to the investigation. Source builds, object files,
and marker strings do not establish this. Do not replace individual ELF files.
Producer receipts must identify source, package closure, toolchain/container,
signing, package and APK hashes, and the nonpromotion artifact route. No pipeline
publication, BAR registration, queue, installation, or package consumption is
implied by this feature or its local tests.

## Output

Each message to the `mono-startup-meta` logcat tag is one complete ASCII JSON
object, at most 2048 bytes excluding its terminating NUL. Schema 1 records contain
only fixed event/operation enums, booleans, numeric outcomes and endpoint metadata,
and validated capture/build/process identity. There are no raw options, addresses,
payloads, method names, assembly names, or pointer dumps. The existing general
debugger log level is neither enabled nor reused.

The runtime component emits:

* `capture_start` and `capture_health`;
* `component_init`, `agent_config`, `finish_init`, and `transport_select`;
* `socket_create`, `socket_option`, `bind`, `listen`, `endpoint_query`, `wait`,
  `accept`, `connect`, `handshake`, `shutdown`, and `close`.

Component initialization is the actual agent entry point, not the component
vtable loader. Its enabled=false return is distinguished from the enabled path.
Finish-init distinguishes startup/lazy and the existing already-initialized CAS
skip; the skip does not prove a concurrent transport initialization completed.
Socket operation begin/end records observe the original operation once, including
failures. Handshake reports only logical success/failure, with no protocol contents
or byte counts. Custom transports are identified without claiming socket coverage.
Only existing `getsockname` calls provide the dynamically assigned local port.

The first monotonic/realtime/monotonic bracket is sampled after gate validation
and **before** boot/start identity I/O, then attached to `capture_start`. Subsequent
records use the same three-clock sampling pattern with fresh samples. These are observation-site timestamps, not exact
syscall entry/completion timestamps. All 64-bit values use decimal strings.
Missing clock or identity values are JSON null with partial status.
`/proc/self/stat` parsing uses the final command-name delimiter. Identity reads
are bounded and occur only once after opt-in.

`socket_id` is a positive, component-local descriptor-wrapper generation, not a
kernel handle. Accepted sockets carry the known listener's generation as
`parent_socket_id`. Reused fd numbers do not reuse a generation. Unknown
generation/endpoint values remain null. The bounded registry is append-only:
it does not close or otherwise repair existing descriptor leaks. Failed close
does not imply a completed lifetime. Helpers restore errno, and failed syscall
errno is captured immediately before other observation work. Successful and
logical outcomes never emit ambient errno.

## Accounting and interpretation

Each enabled component has independent atomic sequence and health counters:
`attempted`, `writeAccepted`, `writeFailed`, `formatDropped`, `capDropped`.
Requests 1 through 256 each get at most one formatting and sink attempt; failure
does not refund a slot. Request 257 increments `capDropped` and substitutes the
sole reserved cap-health record at sequence 257. Later requests update counters
only. There are at most 257 sink attempts, no retries or fallback sink, and no
health thread.

Health snapshots precede their own formatting/write outcomes. Concurrent samples
are not a transaction; do not require a conservation equation. Sequence allocation
is not syscall order, write order, or cross-DSO order. Positive log API results
mean accepted by that API, not retained by a collector. Initialization publishes
the enabled recorder after its first acknowledgement; concurrent hooks before
publication do not wait for capture. Kill, abort, capture failures, missing prefix,
or a capped tail preclude claims of lifetime completeness.

No events means unknown, not "never initialized". Join only independently
qualified capture/build/component/PID/UID/boot/start identities. Keep package,
installed/loaded-byte, and host-debugger session attestation separate.

## Local validation

The opt-in CMake target `mono-debugger-startup-tests` is enabled with
`-DMONO_DEBUGGER_STARTUP_TESTS=ON` in an existing native Mono build directory.
It executes the production C recorder and production syscall wrappers with
deterministic platform/syscall injection. It covers malformed/default-off gates,
first-clock ordering, numeric bounds, identity parsing, loss and cap accounting,
errno preservation, failed listen, exactly-once syscall behavior, accepted socket
parentage, fd reuse, disabled/lazy initialization metadata, and eight-thread
contention. It never probes a debugger socket.

An optional executable argument writes JSONL formatting fixtures. These contain
synthetic identities and outcomes and are **not guest execution evidence**.
Windows host tests do not validate Android Bionic/logcat behavior. Cross-compiling
the actual enabled debugger objects validates the Android translation units but
does not qualify signed normal packs, a device run, or historical producer tools.

## Manual artifact-only producer through the existing official root

The existing `eng/pipelines/runtime-official.yml` root has
`enableMonoStartupMetadata=false` by default. Its ordinary triggers, main-only
Localization/Source_Index stages, assetless Publish stage, 1ES wrapper and SDL
settings remain unchanged in that mode. When explicitly enabled, the root
instead includes `eng/pipelines/mono-android-startup-metadata.yml` as a stage
template; the ordinary Publish graph is excluded at template expansion time.
This is not a new pipeline definition or a standalone diagnostics pipeline.

The diagnostic stage requires a manual invocation of the existing dnceng
`internal` definition **679**, private TfsGit repository `dotnet-runtime`
(`a2f9a77f-0d37-4eaf-aecc-5b3ff7457ad6`), and a reviewed feature branch descending
from `4271d88e0aebf3d04f188f1334c2220d80555ef6`. Set these root parameters:

| Parameter | Value |
|---|---|
| `enableMonoStartupMetadata` | `true` |
| `monoStartupSourceCommit` | Exact reviewed 40-character feature commit |
| `monoStartupAttempt` | Positive whole-experiment attempt, normally `1` |

Both ABIs and the signing job use the same
`10.0.12-startup.<BuildId>.<ExperimentAttempt>.s<source12>` version and marker.
Job retry numbers are recorded separately and select the exact build artifact;
they never independently change the signing job's package version. Reusing an
unpublished version on retry is not a reproducibility claim: archive hashes and
actual job attempts still distinguish each result.

The existing private publication route is
`https://dev.azure.com/dnceng/internal/_git/dotnet-runtime`, using a new reviewed
feature ref such as `refs/heads/davidnguyen-tech-mono-guest-listener-evidence`.
Publication and queueing require separate approval. After publication, request
a **repository-backed preview** of definition 679 with that matching self ref
and commit, not a `yamlOverride` and not the release-only baseline paired with
`main`. Review the expanded graph before queueing the same source/resources.
Local structural tests are not provider compilation or signing authorization.

The root retains its existing `1ESPipelineTemplates` repository resource:
`1ESPipelineTemplates/1ESPipelineTemplates`, `refs/tags/release`. For this
diagnostic baseline, pin its preview/run resource version to
`8bb89477eb3e61becfe1ab21442fe9aefb031b35`. The diagnostic helper compares the
provider's resolved resource version with that expected commit and the actual
resource checkout, and hashes the checked-out entry template. The ordinary
default-off root's resource behavior is not changed.
The diagnostic template checkout is explicitly included in normal SDL source
scanning only on that path; self coverage and ordinary SDL settings are preserved.

`MonoStartupMetadata` contains `ValidateInputs`, `BuildRuntimePacks`, and
`TestSignRuntimePacks`, in dependency order. The normal official job wrapper
and branch-selected internal pool are used throughout. The Linux job selects
the existing named `android` container. Only the enabled official diagnostic
path pins that resource in `pipeline-with-resources.yml`; ordinary official
and public mappings retain their existing image tag. The diagnostic image is
`mcr.microsoft.com/dotnet-buildtools/prereqs@sha256:62d9af8ee1655023cc103457f52e4679efbe1d81392d8668147e91a931082b3d`,
with expected NDK `27.2.12479018`. Its preprovisioned SDK/JDK/NDK are a **new
explicit toolchain baseline**, not historical binary equivalence. Actual
versions, compiler hashes, SDK component properties and JDK release evidence
are collected in the job; image environment declarations alone are not proof
of a successful build. This route does not extract local SDK archives, accept
licenses on the user's behalf, install a guest, or start an emulator.

Both Linux checkout steps explicitly target the host; the producer step targets
`android`. Inside that step, source, staging and template paths derive from the
agent-mapped `BUILD_SOURCESDIRECTORY`, `BUILD_ARTIFACTSTAGINGDIRECTORY` and
`PIPELINE_WORKSPACE` environment variables. Host-expanded custom path variables
and mount-prefix substitutions are not used. Artifact declarations retain their
normal host-side staging paths and `isProduction: false`; Windows signing runs
without a container and retains its existing host paths.

The producer script builds the normal
`mono.runtime+mono.corelib+libs.native+libs.sfx` prerequisites and the existing
`Microsoft.NETCore.App.Runtime.Mono.sfxproj` for exactly Android x64 and arm64.
This excludes unrelated `libs.pretest` dependencies, not runtime pack assets.
The shared SFX package reference to `Microsoft.DiaSymReader.Native` is scoped to
Windows targets, matching its native Windows payload consumer; Android packs
do not restore this unused PDB reader. The source-only exclusion remains intact.
Real MSBuild item comparisons cover this scope; successful evaluation alone does
not establish a successful strict-audit restore or package build.
The evaluation-only Windows CoreCLR controls import the checked-in Crossgen task
props/targets that the normal task project copies to its output. They do not
require historical Crossgen build outputs or execute ReadyToRun tasks, and
explicitly reproduce the missing-generated-import failure before evaluating.
It does not override audit/dependency versions, invent a package ZIP, or swap ELF
files. Existing build and sfx closure checks remain active. Global `Version` and
`PackageVersion` carry the common experiment version through
normal package generation; evaluated properties and the nuspec are checked.
RuntimeList framework identity and RID-specific asset paths are checked without
inventing package-version attributes that the existing manifest does not have.

The route requires a fresh clean source checkout, an isolated local NuGet cache,
and the original baseline as an ancestor of the exact reviewed source commit.
It retains the source patch/hash, marker, commands/logs, immutable image input,
NDK `source.properties`, compiler hash/version, complete package-entry inventories,
native ELF ABI checks and package hashes. The native recorder/wrapper tests run on
the host before either normal pack build. The Python producer tests execute the
real planning, ZIP inventory, malformed-input and command-failure paths.
The marker tests invoke the actual MSBuild gate, including LF/CR/CRLF rejection.

Each actual archive gets an `inventory.<id>.json` sidecar with its identity,
size, hash, signature-entry presence, and decompressed member hashes. A separate
`signature.<id>.json` records the actual `dotnet nuget verify --all <fileName>`
exit code, SDK version, and hash of retained combined stdout/stderr. Verification
runs in the archive's directory with the recorded leaf filename. Unsigned
archives remain unsigned regardless of verifier output; a valid signature alone
would still be policy-unqualified. Sidecar filenames and hashes are retained in
the producer-specific build receipt, not treated as source or guest attestation.

The Windows job selects the existing nonproduction MicroBuild path:
`enableMicrobuild=true`, `_SignType=test`, `microbuildUseESRP=false`.
`eng/common/core-templates/steps/install-microbuild.yml` already permits TEST
signing on Windows and explicitly excludes ESRP service connections for
nonproduction signing. No signing rule, certificate selector, service
connection, trust store, permission or ordinary signing template is changed.
The job invokes normal `eng/common/build.ps1` to restore signing tools and the
native Mono project, then invokes its sign-only action with the real
`OfficialBuildId`, `SignType=test` and `DotNetSignType=test`. It does not rebuild
the product or restore the unrelated libraries/test graph. Audit gates remain
enabled. Only one diagnostic RID archive is staged at a time; the actual
normal Arcade `Sign.proj` evaluation must select exactly that archive before
signing. Neither a final RID property nor a planned command proves selection.

Only 1ES pipeline-artifact outputs are requested. There is no Publish action,
BAR/feed/channel registration or symbol promotion. Artifact names are
`mono-android-startup-unsigned-unadmitted-<BuildJobAttempt>` and
`mono-android-startup-test-signed-unadmitted-<SignJobAttempt>`.
The latter preserves `build/` byte-for-byte, unsigned packages in
`build/packages/`, and signed outputs/evidence in `postsign/`. The post-sign
build-receipt alias is an identical copy, not a rewrite of its relative refs.

The source-specific build root is schema **3**,
`mono-android-startup-build-receipt`; the signing root is schema **1**,
`mono-android-startup-sign-receipt`. Both retain actual command observations,
including failure/launch failure. The latter also records evidence-directory
roles, real per-RID signing binlogs, evaluated signing selection, and the
unsigned-to-signed archive binding. Common inventory/signature sidecars remain
schema 1. `postsign.<id>.json` is a schema-1
`guest-runtime-package-postsign`, with source/build/input/output/signer
identities and actual operation-evidence references. Its
`native-provenance.<id>.json` companion binds the full final debugger-component
member hash, ELF class/endianness/machine, exact NUL-terminated marker, and raw
per-RID CMake cache/compiler-identification copies. GNU build IDs are explicitly
`null` / `not-collected`, not claimed absent. Member deltas are complete
observations requiring independent policy review, not an allow-all signing rule.

Standard final `dotnet nuget verify --all` success is
`verified-policy-unqualified`, **not admission or clean-consumer trust**.
Verification failure produces `produced-verification-failed` receipts and
retains signed archives, final inventories, sidecars and combined output before
failing the job. Earlier signer/launch failures retain source-specific failure
evidence without inventing a completed common receipt. No verification result
authorizes deployment, package consumption or guest execution.
