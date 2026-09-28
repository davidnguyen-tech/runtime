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
| `monoStartupRealSign` | `false` (default, existing Test path); `true` selects the separately reviewed Real path |

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
`1ESPipelineTemplates/1ESPipelineTemplates`. The ordinary disabled path still
uses `refs/tags/release`; the diagnostic path selects the immutable YAML `ref`
`8bb89477eb3e61becfe1ab21442fe9aefb031b35`. Do not override that resource back to
the moving release tag in preview/run requests. A requested REST resource version
alone did not enforce the template resolution observed in run 3087006; actual
resource readback must match the reviewed commit. Azure Pipelines supports
[commit SHA repository refs](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/templates#store-templates-in-other-repositories).
The existing official/public template selection chooses a diagnostic wrapper
only for official diagnostics. That wrapper declares the literal reviewed SHA;
the default wrapper declares the literal release tag. Both forward to one shared,
unchanged official policy body. There is no ref selector parameter. Earlier
conditional mapping, `iif`, and direct-parameter resource forms failed preview in
this nested route, not proof of universal lack of support. The literal-wrapper
composition still requires provider preview and actual resource readback.
The diagnostic helper compares the
provider's resolved resource version with that expected commit and the actual
resource checkout, and hashes the checked-out entry template. The ordinary
default-off root's resource behavior is not changed.
The diagnostic template checkout is explicitly included in normal SDL source
scanning only on that path; self coverage and ordinary SDL settings are preserved.

`MonoStartupMetadata` contains `ValidateInputs`, `BuildRuntimePacks`, and
`TestSignRuntimePacks` by default, in dependency order. Explicit Real opt-in
replaces only the signing phase with `RealSignRuntimePacks`. The normal official job wrapper
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

### Held Real-signing route (not authorized to queue)

`monoStartupRealSign=true` is an explicit, default-off parameter on the same
manual-only diagnostic stage of definition 679. It uses the existing Windows
MicroBuild plugin with `_SignType=real`, `microbuildUseESRP=true` and the
ordinary `MicroBuild Signing Task (DevDiv)` and internal PME service-connection
selection in `install-microbuild.yml`. Its sign-only MSBuild invocation specifies
`SignType=real` and `DotNetSignType=real`, without changing the repository's
signing rules, certificates, revocation settings or trust stores. Both modes
require the evaluated `ItemsToSign` to contain only the staged x64 or arm64
package, one RID at a time. Neither `enablePublishing` nor
`enablePublishBuildAssets` is enabled; the final output is a nonproduction 1ES
pipeline artifact named `mono-android-startup-real-signed-unadmitted-<SignJobAttempt>`.
The unsigned build artifact is unchanged and separately retained. The Real
post-sign receipts distinguish `requestedSignType=Real` and
`normal-real-sign`; ordinary verification remains **policy-unqualified**,
even when it exits zero. This route neither grants service-connection access
nor establishes that the service will accept the diagnostic source/branch,
NuGet certificate chain, timestamping policy or clean Mac SDK consumer.

Run 3087201 retained original unsigned x64 and arm64 packages in
`mono-android-startup-unsigned-unadmitted-1` (artifact ID 76563209);
their SHA-256 values are respectively
`782a7ecf2dfcef327e3f42078f0e5e3f03a220af5cbfbf0320838aeab42a9994`
and `2e05844311f63daf4a46e26eef25961863e294a370efb1b1cd2935428cebeece`.
Those originals were Test-signed in that run; **do not feed its Test outputs
into Real signing**. The current signer refuses direct cross-run unsigned input:
it binds the producer's original pipeline build ID, source commit, attempt,
whole plan, template bytes and hashes to the signing job. A future cross-run
route would require its own reviewed provenance/receipt/hash gate. The current
Real opt-in instead rebuilds both normal packs in the same run; its
`10.0.12-startup.<new BuildId>.<Attempt>.s<new source12>` version and marker
must not be presented as run 3087201's bytes. Compare the new unsigned
package/member/native hashes to those originals as a **new candidate** before
any consumption. Leave Real CI queueing, service-policy admission, Mac
installation and guest use on hold pending explicit review; a repository-backed
preview only compiles a proposed graph and never proves signing authorization.
The separate runtime `TSAUpload` external-template configuration failure is
not package timestamp verification and must not be used to waive that check.

Only 1ES pipeline-artifact outputs are requested. There is no Publish action,
BAR/feed/channel registration or symbol promotion. Artifact names are
`mono-android-startup-unsigned-unadmitted-<BuildJobAttempt>` and
`mono-android-startup-test-signed-unadmitted-<SignJobAttempt>`.
The latter preserves `build/` byte-for-byte, unsigned packages in
`build/packages/`, and signed outputs/evidence in `postsign/`. The post-sign
build-receipt alias is an identical copy, not a rewrite of its relative refs.

The source-specific build root is schema **5**,
`mono-android-startup-build-receipt`; the signing root is schema **3**,
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

The source-specific `pipeline` object has exactly ten fields: `organization`,
`project`, `definitionId`, `pipelinePath`, `pipelineCommit`, `buildId`,
`jobAttempt`, `jobName`, `stageName`, and `phaseName`. The last three preserve
the actual `System.JobName`, `System.StageName`, and `System.PhaseName` strings;
the build ID and job attempt remain positive integers. The
[Azure predefined-variable documentation](https://learn.microsoft.com/en-us/azure/devops/pipelines/build/variables?view=azure-devops#system-variables)
distinguishes the logical phase from its runtime job instance. Actual run
3087046 recorded raw `jobName=__default`; its timeline identifies logical
`MonoStartupMetadata.BuildRuntimePacks` and
`MonoStartupMetadata.TestSignRuntimePacks` phases, with `.__default` job
instances. A declared YAML job name is therefore not the raw job-instance name.

Role validation requires stage `MonoStartupMetadata` and phase
`BuildRuntimePacks` or `TestSignRuntimePacks` for the respective operation;
the plan-only pipeline gate requires phase `ValidateInputs`. A syntactically
valid job-instance name alone never grants a role. Existing manual invocation,
definition 679, repository, source, and feature-branch checks remain required.
Signing additionally compares the downloaded build's actual `jobAttempt` with
the existing dependency-supplied `PRODUCER_ATTEMPT`. This producer-only check
does not add a consumer or shared-signer field. Common `experiment_attempt`
continues to bind `run_id`, package version, and marker; it is independent of
both job attempts. For example, common attempt 1, build attempt 2, and signing
attempt 3 are valid when the selected build dependency attempt is 2.

The shared signer's closed ten-field shape remains unchanged: only the original
eight pipeline identity fields, `requestedSignType`, and `templates` are emitted.
Stage/phase fields stay in the source-specific roots, and raw `jobName` is never
relabeled. Consumers must explicitly require build schema 5/sign schema 3,
validate their exact stage/phase roles, and compare only the original eight
identity fields with the shared signer. Historical schema 3/1 receipts, including
3087046, remain unchanged and are not retrospectively phase-qualified. Schema
4/2 receipts, including 3087132, are not retrospectively qualified for the new
template provenance requirements.

Each root adds `templateEvidence`, a `{fileName, sha256}` reference to
`template-evidence.json` in the build root or signing `postsign/` directory.
Its schema-1 `mono-startup-template-evidence` record contains `status` and an
ordered `records` array. Only `completed` evidence with all eleven entries is
eligible for comparison. The ordered runtime paths are `runtime-official.yml`,
`mono-android-startup-metadata.yml`, `pipeline-with-resources.yml`,
`templateDispatch.yml`, `template1es-mono-startup.yml`, `template1es-body.yml`,
the official/core job templates, `install-microbuild.yml`, and `eng/Signing.props`;
the final entry is the pinned external `v1/1ES.Official.PipelineTemplate.yml`.
The exact paths/order are enforced by the producer and its tests. The inactive
default `template1es.yml` is not evidence of the active diagnostic route.
This is not a claim that every transitive central template is copied.

Every entry retains `repository`, `commit`, `path`, `gitBlobId`, `blobIdEvidence`,
`canonicalFile`, `rawFile`, and `transform`. File references have only
`fileName` and `sha256`; all files are unique leaves inventoried in the owning
root. Canonical means the **untouched Git blob**, not normalized text.
Binary `git cat-file -s`, `git cat-file blob`, and `git rev-parse --verify`
observations bind the exact `commit:path`, repository working directory,
canonical bytes, size, and object ID. Their stdout and stderr are retained
separately; successful proof requires empty stderr. Unique successful
`source-head` / `template-head` observations bind each working directory to
the expected commit. Object-ID output is exactly forty lowercase hexadecimal
bytes plus LF; size output is positive decimal plus LF. Blob identity is
recomputed using Git's `blob <length><NUL><bytes>` SHA-1 framing.
These recorded Git operations attest commit/path resolution; hashing a blob
alone is not independent cryptographic proof of Git-tree inclusion.

Raw checkout SHA-256 remains the meaning of the unchanged shared four-field
template record. It must match its retained raw bytes independently in each
operation. Strict UTF-8 without NUL is required. The only transforms are exact
byte `identity`, or `lf-to-crlf`: a canonical blob with at least one LF and no
CR expanded by replacing every LF with CRLF. Every other byte, including any
existing BOM, is preserved. Mixed newline conversion, BOM stripping, whitespace
trimming, YAML reserialization and Git clean filters are not accepted.
Only after both raw-to-blob proofs pass are ordered repository/commit/path,
Git object ID and canonical SHA-256 compared across build and sign.

Payloads are limited to 1 MiB each; canonical/raw/ID/size bytes together are
limited to 16 MiB. Raw file size and Git blob size are checked and budgeted
before payload reads, then checked against captured lengths. Size/ID stdout
is capped at 64 bytes, stderr and sidecar at 64 KiB. The thirty-three additional
Git commands bring the current build ledger to sixty observations, within its
existing sixty-four limit; signing remains bounded at 128. Capture failures
retain bounded partial byte streams, commands and a failed sidecar with at
most 4096 failure-text characters before the root reference is finalized.
No success-shaped shared receipt is synthesized on failure.

Run 3087132 proves the phase/attempt identity fix and two unsigned builds, but
did not retain signing template bytes or hashes before its comparison failed.
Its exact mismatch cannot be reconstructed. The eight retained runtime hashes
match LF Git blobs, and controlled Git CRLF checkout reproduces differing raw
hashes; this is not retrospective proof of the signing agent's bytes. The
external pinned blob was independently verified as 8116 bytes, 182 LF, no CR
and no BOM, with SHA-256 matching that run's build receipt.

After fresh-output and input/output-disjointness checks establish a safe
destination, failed preflight retains a `failed` root with no package or common
post-sign receipt. `pipeline` is `null` if identity validation did not complete;
a genuinely validated identity can remain when a later gate fails.
Preflight failures alone add `providerContext`, containing exactly `stageName`,
`phaseName`, `jobName`, `jobAttempt`, and `truncatedFields`. The first four are
raw environment strings or `null` when missing, limited to 256 characters;
`truncatedFields` explicitly lists any retained prefixes. This context is not
authority, and validation always uses the original, untruncated values.
Successful roots and later command-failure roots do not add this context.
Unsafe or already-existing destinations are rejected without overwriting them.

Standard final `dotnet nuget verify --all` success is
`verified-policy-unqualified`, **not admission or clean-consumer trust**.
Verification failure produces `produced-verification-failed` receipts and
retains signed archives, final inventories, sidecars and combined output before
failing the job. Earlier signer/launch failures retain source-specific failure
evidence without inventing a completed common receipt. No verification result
authorizes deployment, package consumption or guest execution.

### Observed manual Real-signing run 3089937 (unadmitted)

After the held route above was reviewed, exactly one manual run of existing
definition 679 used feature source
`0d81e67d9044e68f7304a08df125ece26f054dbe` and the pinned 1ES commit
`8bb89477eb3e61becfe1ab21442fe9aefb031b35`. `ValidateInputs`,
`BuildRuntimePacks` and `RealSignRuntimePacks` succeeded. The overall result
was `partiallySucceeded` because the separate, inherited `SDLSources` TSA
task was `succeededWithIssues`; this is not evidence of a NuGet package
timestamp failure or success. No permission grant, signing-rule change,
publication, promotion, Mac installation or guest use was part of this run.

The same-run unsigned input is Azure PipelineArtifact
`mono-android-startup-unsigned-unadmitted-1` (artifact ID 76625767).
Its x64 and arm64 archive SHA-256 values are respectively
`948a66f1e59b3b4bdc6f66b9fa39713ac9e0b9577fe6b217916ceee3891e95b7`
and `44749cdeacc05e860ca9e44f33daef6729d751f32fd43f1d818548ca1fba991d`.
The corresponding Real-signed, still-unadmitted PipelineArtifact is
`mono-android-startup-real-signed-unadmitted-1` (artifact ID 76626017).
For version `10.0.12-startup.3089937.1.s0d81e67d9044`, its x64 archive is
26,501,239 bytes with SHA-256
`356962a4419d14b0d5352bb291136401dbf41c26bfd0345216e55eb456f97a14`;
its arm64 archive is 26,587,358 bytes with SHA-256
`76f58515b8d0f1249fb8a5a2ebee188a2fa885efc63d523790da0bcd84b28fe7`.
The schema-3 signing receipt (SHA-256
`774be14d603180cdec10abbde330ac81f12b13afbeb508784caf2d2bdea8a401`)
binds each original unsigned hash to the signed hash, records
`requestedSignType=Real`, and reports `verified-policy-unqualified`.
Normal Windows `dotnet nuget verify --all` exited zero for both signed packs
and observed a Microsoft Corporation author certificate fingerprint
`9A1B131BEE0605433056A4EA3815478A8E177961A968C6C0027C1093D1FEB630`.

These are **new candidate bytes**, not run 3087201's original unsigned or
Test-signed packages. The new and original unsigned packs each contain 300
members per RID. Across the two runs, the pinned container, NDK, Clang,
Android SDK, Java and per-RID C/C++ compiler evidence agree; the CMake cache
changes only in the configured build marker. The debugger and diagnostics
tracing static archives differ only in the intended marker/version bytes;
the crypto JAR's unpacked classes and compressed member payloads agree.
However, both RIDs' debugger and tracing shared libraries have differing
ELF `.text` and `.rodata` bytes and GNU build IDs (and debugger relocations
also differ). Neither matching static archives after substitution nor
matching JAR classes proves shared-library semantic equivalence. Retained
unsigned receipts, all four archive hashes and all 1,200 decompressed ZIP
member hashes were checked; transport receipts bind the downloaded artifacts.
Actual Intel Mac SDK trust, package timestamp policy, consumer readiness and
guest behavior remain unverified. Do not treat this Windows verification or
the separate SDL result as admission; do not install or execute these packs
on a guest without an independent review.

### Separate held Intel Mac signature check (default off)

The root parameter `verifyMonoStartupRealSignature=false` leaves the ordinary
graph and the existing Test/Real producer path unchanged. Explicitly enabling
it **without** `enableMonoStartupMetadata` selects only
`MonoStartupMacVerification.VerifyHeldRealSignatures`, under the same manual
internal definition 679 and inherited 1ES SDL policy. Supply
`monoStartupSourceCommit` with the exact separately reviewed verifier-route
commit; the job rejects any mismatch against `Build.SourceVersion`. Enabling
both diagnostic modes instead produces a failing, checkout-free guard stage. The verifier uses
the existing `Azure Pipelines` pool with the explicit `macOS-26` hosted Intel
image (rather than the ARM-routing `macos-latest-internal` image); the script
still requires actual Darwin x86_64 hardware, rejects Rosetta,
and refuses nonmanual, non-internal or production-branch contexts. This
route does not rebuild, re-sign, consume an UPack/feed, or request credentials,
service-connection grants, publishing or promotion.
Run 3090048 retrieved the held artifact but failed the native-Intel host gate
on `macos-latest-internal` before SDK use or package verification. The
replacement image ran natively on Intel in a separate DevDiv verification;
its availability in dnceng/internal is not established by that result.

The job's `DownloadPipelineArtifact@2` selects **specific build 3089937** in
the same `internal` project, definition 679, artifact
`mono-android-startup-real-signed-unadmitted-1` (ID 76626017). Before any SDK
acquisition, the verifier requires the exact schema-3 signing receipt SHA-256
`774be14d603180cdec10abbde330ac81f12b13afbeb508784caf2d2bdea8a401`,
validates every listed file against that receipt, rejects any unlisted files
or links and requires exactly the two signed `postsign/` packages (not the
unsigned copies in `build/packages/`). It checks each signed package hash
and size above, original unsigned package hashes, definition 679,
source/build/run identities,
post-sign bindings, complete signed member inventories and Windows
verification sidecars. An unexpected download layout or package fails closed.
An exact `pipelineId` downloads by run ID rather than searching by build-result
filter: the separate inherited SDL TSA task left the source run partially
succeeded. There is no latest-run or failed-build search; the successful
Real-sign job and the pinned receipt, packages and input/output bindings remain
independent requirements.

On an eligible host it downloads the official **osx-x64 SDK 10.0.401** archive
from its immutable version URL and requires SHA-512
`33401b4a2da8554e3306db6072ea8569d9fcc608509c271e0aa4b39e7cc432da3631f14e7e1e2445d67d72550d18ce44a8bbd2382a756867ad2edab6b1c963c0`
before extraction into agent-temporary storage. The actual SDK executable
must report `10.0.401`. The archive refuses links, special entries and
unsafe/duplicate paths; SDK extraction, CLI home and NuGet packages remain
under temporary storage. Before SDK first use the verifier suppresses only
process-local ASP.NET certificate generation, rejects inherited signature
verification disablement or offline revocation and records inherited verifier
settings without overriding them. It hashes the SDK executable and both
trusted-root bundles before and after verification, failing on drift. For each
signed package, it runs ordinary
`dotnet nuget verify --all --certificate-fingerprint
9A1B131BEE0605433056A4EA3815478A8E177961A968C6C0027C1093D1FEB630`
in the package directory, retaining each child exit code and hashed combined
output even on failure. Each child has a 120-second timeout and an 8 MiB
output cap; timeouts and excess output fail closed with retained evidence.
A nonzero verifier result fails the job after both
RIDs have been checked. Only a small nonproduction pipeline artifact with
logs and a root receipt is emitted; the SDK and packs are not republished.
Success means observed Mac SDK verification only, **not** policy admission,
guest readiness, installation or use. Run 3090048 established download access,
but that ARM-host attempt did not perform Runtime Mac verification.

### Observed held Intel Mac verification (run 3090066; overall pending)

The single corrected manual run 3090066 used source
`e5d91db40b7da1bf98e60464197670ed55e15462` and the `macOS-26` image.
The native Darwin x86_64 / no-Rosetta gate passed. The same-org
`DownloadPipelineArtifact@2` task retrieved the **existing** signed run
3089937 artifact `mono-android-startup-real-signed-unadmitted-1` (ID
76626017); there was no rebuild or re-sign. The verifier's retained receipt
(SHA-256 `ea245c881bea886657046f7ba853398ed72466759cc1b41fc86cde67faf5baec`)
binds the exact signed x64 package
`356962a4419d14b0d5352bb291136401dbf41c26bfd0345216e55eb456f97a14`
(26,501,239 bytes) and arm64 package
`76f58515b8d0f1249fb8a5a2ebee188a2fa885efc63d523790da0bcd84b28fe7`
(26,587,358 bytes) to source run 3089937 and its reviewed Real-signing
receipt. The official osx-x64 SDK reported `10.0.401`; both actual
`dotnet nuget verify --all` children exited 0 with no NuGet warnings in their
retained logs (x64 log SHA-256
`971e39b46a11799183fa0ace3559e22654291b4543e171e139457cdafb61fd36`;
arm64 log SHA-256
`2a7ba2e760f697a96c3459154fc54253dab7a224d7af07f0180a3199f1a2409d`).
The SDK executable, `codesignctl.pem` and `timestampctl.pem` hashes matched
before and after. `MonoStartupMacVerification.VerifyHeldRealSignatures`
succeeded and published nonproduction evidence artifact 76628551.

At this observation point, the separate inherited SDL stage and **overall
run result were still in progress**. These verifier results establish normal
Intel Mac SDK signature acceptance for only these two signed package bytes;
the receipt records `admitted=false` and `guestExecution=false`. No feed
publication, trust-root modification, guest installation or guest execution
occurred. Do not treat this check as package admission or consumer readiness.
