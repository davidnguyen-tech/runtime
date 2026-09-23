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

## Manual artifact-only producer

`eng/pipelines/mono-android-startup-metadata.yml` is a disjoint pipeline with no
CI/PR trigger and `enableProducer=false`. It uses JSON syntax (a YAML subset) to
allow dependency-free tests to parse the entire graph and expand its sole boolean
conditional. A non-container preflight validates the exact reviewed source and
immutable MCR image digest before the dependent container job can start. Manual
invocation is also checked inside the producer. The parent must review the
source, expanded graph, image digest, NDK path/revision, and expected outputs
before any queue is authorized. The reviewed container must also supply the
normal Android SDK and Java build prerequisites; an NDK alone is insufficient.

The producer script builds the normal
`mono.runtime+mono.corelib+libs.native+libs.sfx` prerequisites and the existing
`Microsoft.NETCore.App.Runtime.Mono.sfxproj` for exactly Android x64 and arm64.
This excludes unrelated `libs.pretest` dependencies, not runtime pack assets.
It does not override audit/dependency versions, invent a package ZIP, or swap ELF
files. Existing build and sfx closure checks remain active. Global `Version` and
`PackageVersion` carry `10.0.12-startup.<BuildId>.<JobAttempt>.s<source12>` through
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

Only `PublishPipelineArtifact` is reachable; no official publishing template,
BAR/feed/channel registration, symbol promotion, signing task or policy override
is included. A successful result is **unsigned and unadmitted**. Independent
normal signing and trust receipts remain mandatory before capture consumption.
Retained log/receipt artifacts from a failed run are not consumable packs.
