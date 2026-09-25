#!/usr/bin/env python3
# Licensed to the .NET Foundation under one or more agreements.
# The .NET Foundation licenses this file to you under the MIT license.

"""Python 3.9+ manual artifact-only Mono Android build/normal TEST-sign evidence; never publishes."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

BASELINE = "4271d88e0aebf3d04f188f1334c2220d80555ef6"
RID_ARCH = {"android-x64": ("x64", 62), "android-arm64": ("arm64", 183)}
PACK_PROJECT = "src/installer/pkg/sfx/Microsoft.NETCore.App/Microsoft.NETCore.App.Runtime.Mono.sfxproj"
SUBSET = "mono.runtime+mono.corelib+libs.native+libs.sfx"
IMAGE_PREFIX = "mcr.microsoft.com/dotnet-buildtools/prereqs@sha256:"
PRODUCER_IMAGE = IMAGE_PREFIX + "62d9af8ee1655023cc103457f52e4679efbe1d81392d8668147e91a931082b3d"
EXPECTED_1ES_COMMIT = "8bb89477eb3e61becfe1ab21442fe9aefb031b35"
PIPELINE_PATH = "eng/pipelines/runtime-official.yml"
REPOSITORY_ID = "a2f9a77f-0d37-4eaf-aecc-5b3ff7457ad6"
TEMPLATE_PATHS = [
    PIPELINE_PATH, "eng/pipelines/mono-android-startup-metadata.yml",
    "eng/pipelines/common/templates/pipeline-with-resources.yml",
    "eng/pipelines/common/templates/template1es.yml",
    "eng/common/templates-official/job/job.yml", "eng/common/core-templates/job/job.yml",
    "eng/common/core-templates/steps/install-microbuild.yml", "eng/Signing.props",
]
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRY_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 20000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stream_sha256(stream):
    # Share bounded hashing between ordinary files and decompressed ZIP streams.
    digest = hashlib.sha256()
    while chunk := stream.read(65536):
        digest.update(chunk)
    return digest.hexdigest()


def sha256(path):
    with path.open("rb") as stream:
        return stream_sha256(stream)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="ascii")


def read_json(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    require(0 < path.stat().st_size <= 16 * 1024 * 1024, "JSON receipt exceeds bounds")
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)


def reference(path):
    return {"fileName": path.name, "sha256": sha256(path)}


def persist_receipt(output, receipt):
    receipt["retained_files"] = [
        {"path": path.relative_to(output).as_posix(), "sizeBytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(output.rglob("*")) if path.is_file() and path != output / "receipt.json"
    ]
    write_json(output / "receipt.json", receipt)


def provider_context():
    # Capture only these non-secret provider fields, including rejected preflight values.
    values = {field: os.environ.get(variable) for field, variable in (
        ("stageName", "SYSTEM_STAGENAME"), ("phaseName", "SYSTEM_PHASENAME"),
        ("jobName", "SYSTEM_JOBNAME"), ("jobAttempt", "SYSTEM_JOBATTEMPT"),
    )}
    return {**{field: value[:256] if value is not None else None for field, value in values.items()},
            "truncatedFields": [field for field, value in values.items() if value is not None and len(value) > 256]}


def validate_pipeline_role(identity, phase):
    require(set(identity) == {"organization", "project", "definitionId", "pipelinePath", "pipelineCommit",
                              "buildId", "jobAttempt", "jobName", "stageName", "phaseName"},
            "Unexpected pipeline identity fields")
    require(identity.get("stageName") == "MonoStartupMetadata" and identity.get("phaseName") == phase,
            "Pipeline stage/phase differs from the required " + phase + " role")
    job = identity.get("jobName")
    require(isinstance(job, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,255}", job),
            "Invalid pipeline job instance identity")


def pipeline_identity(source, phase):
    require(os.environ.get("BUILD_REASON") == "Manual", "Only a manual pipeline invocation is allowed")
    require(os.environ.get("SYSTEM_TEAMPROJECT") == "internal" and
            os.environ.get("BUILD_REPOSITORY_ID") == REPOSITORY_ID and
            os.environ.get("SYSTEM_DEFINITIONID") == "679", "Only the existing internal runtime-official definition is allowed")
    require(os.environ.get("BUILD_SOURCEVERSION") == source, "Pipeline source differs from reviewed source")
    branch = os.environ.get("BUILD_SOURCEBRANCH", "")
    require(branch.startswith("refs/heads/") and branch not in ("refs/heads/main", "refs/heads/master") and
            not branch.startswith(("refs/heads/release/", "refs/heads/internal/release/")),
            "Diagnostics require a reviewed feature branch, not a production branch")
    identity = {"organization": "dnceng", "project": "internal", "definitionId": 679,
                "pipelinePath": PIPELINE_PATH, "pipelineCommit": source}
    for field, variable in [("buildId", "BUILD_BUILDID"), ("jobAttempt", "SYSTEM_JOBATTEMPT")]:
        value = os.environ.get(variable, "")
        require(re.fullmatch(r"[1-9][0-9]{0,9}", value) and int(value) <= 2147483647,
                f"Invalid pipeline {variable}")
        identity[field] = int(value)
    identity.update({field: os.environ.get(variable) for field, variable in (
        ("stageName", "SYSTEM_STAGENAME"), ("phaseName", "SYSTEM_PHASENAME"), ("jobName", "SYSTEM_JOBNAME"),
    )})
    validate_pipeline_role(identity, phase)
    return identity


def plan(source, run_id, image, enabled=False):
    require(enabled, "Producer is disabled; explicit --enable is required")
    require(re.fullmatch(r"[0-9a-f]{40}", source), "Exact source commit is required")
    require(re.fullmatch(r"[1-9][0-9]{0,11}\.[1-9][0-9]{0,3}", run_id),
            "Run identity must be BuildId.ExperimentAttempt")
    require(image.startswith(IMAGE_PREFIX) and re.fullmatch(r"[0-9a-f]{64}", image[len(IMAGE_PREFIX):]),
            "Reviewed immutable MCR container digest is required")
    version = f"10.0.12-startup.{run_id}.s{source[:12]}"
    marker = f"runtime-4271-{run_id}-{source[:12]}"
    common = [
        "/p:TargetOS=android", "/p:RuntimeFlavor=Mono",
        f"/p:Version={version}", f"/p:PackageVersion={version}",
        f"/p:MonoAndroidStartupBuildId={marker}",
        "/p:NuGetAudit=true", "/p:NuGetAuditMode=all", "/p:TreatWarningsAsErrors=true",
    ]
    commands = []
    for rid, (arch, _) in RID_ARCH.items():
        properties = common + [f"/p:TargetArchitecture={arch}"]
        commands.extend([
            {"name": f"{rid}-build", "argv": [
                "bash", "./build.sh", SUBSET, "-os", "android", "-arch", arch,
                "-c", "Release", *common]},
            {"name": f"{rid}-properties", "argv": [
                "./.dotnet/dotnet", "msbuild", PACK_PROJECT, *properties,
                "-getProperty:Version,PackageVersion,RuntimeIdentifier,MonoAndroidStartupBuildId",
                "-verbosity:quiet"]},
            {"name": f"{rid}-pack", "argv": [
                "bash", "./dotnet.sh", "build", PACK_PROJECT, "-c", "Release", *properties]},
        ])
    return {"baseline": BASELINE, "source": source, "run_id": run_id, "container": image,
            "expected_1es_commit": EXPECTED_1ES_COMMIT,
            "version": version, "marker": marker, "rids": list(RID_ARCH), "commands": commands,
            "output_class": "unsigned-unadmitted-normal-runtime-packs",
            "publication": False, "signing": False, "guest_execution": False}


def inventory_package(path, rid, version, marker, signature_expected=False):
    require(rid in RID_ARCH, "Unexpected RID")
    expected_id = f"Microsoft.NETCore.App.Runtime.Mono.{rid}"
    require(path.name == f"{expected_id}.{version}.nupkg", "Unexpected normal package filename")
    require(0 < path.stat().st_size <= MAX_PACKAGE_BYTES, "Oversized or empty package")
    entries = []
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        require(0 < len(members) <= MAX_ENTRIES, "ZIP entry count exceeds bounds")
        require(sum(entry.file_size for entry in members) <= MAX_PACKAGE_BYTES, "ZIP contents exceed byte bound")
        names = {entry.filename for entry in members}
        require(len(names) == len(members), "Duplicate ZIP entry")
        require((".signature.p7s" in names) == signature_expected,
                "Package signature-entry presence differs from the expected build/sign stage")
        normalized = {}
        for entry in members:
            name = entry.filename
            require(entry.orig_filename == name, "Unsafe normalized ZIP entry path")
            parts = name.removesuffix("/").split("/")
            require(0 < len(name) <= 1024 and all(part not in ("", ".", "..") for part in parts) and
                    not any(ord(c) < 32 or ord(c) == 127 or c in "\\:" for c in name),
                    "Unsafe ZIP entry path")
            key = name.removesuffix("/").casefold()
            require(key not in normalized, "Case-fold or file/directory ZIP collision")
            normalized[key] = entry.is_dir()
            require(not entry.flag_bits & 1 and not stat.S_ISLNK(entry.external_attr >> 16),
                    "Encrypted or symlink ZIP entry")
            require(0 <= entry.file_size <= MAX_ENTRY_BYTES, "Oversized ZIP entry")
            require(not entry.is_dir() or entry.file_size == 0, "Nonempty ZIP directory")
        for name in normalized:
            require(all(normalized.get(str(parent), True) for parent in PurePosixPath(name).parents),
                    "ZIP file conflicts with a parent directory")
        for entry in sorted(members, key=lambda value: value.filename):
            with archive.open(entry) as stream:
                digest = stream_sha256(stream)
            entries.append({"path": entry.filename, "sizeBytes": entry.file_size, "sha256": digest})
        nuspecs = [name for name in names if name.endswith(".nuspec")]
        require(len(nuspecs) == 1, "Exactly one normal nuspec is required")
        metadata = ET.fromstring(archive.read(nuspecs[0]))
        # NuGet uses several schema namespace revisions; match local names only.
        fields = {node.tag.rsplit("}", 1)[-1]: node.text for node in metadata.iter()}
        require(fields.get("id") == expected_id and fields.get("version") == version,
                "Nuspec identity/version mismatch")
        native = f"runtimes/{rid}/native/"
        required = [
            f"{native}libmonosgen-2.0.so", f"{native}libmonosgen-2.0.a",
            f"{native}libmono-component-debugger.so",
            f"{native}libSystem.Native.so",
            f"{native}System.Private.CoreLib.dll",
            "data/RuntimeList.xml",
        ]
        require(all(name in names for name in required), "Incomplete normal runtime pack closure")
        manifest = ET.fromstring(archive.read("data/RuntimeList.xml"))
        require(manifest.attrib.get("FrameworkName") == "Microsoft.NETCore.App" and
                manifest.attrib.get("TargetFrameworkVersion") == "10.0", "RuntimeList framework mismatch")
        listed = {node.attrib.get("Path") for node in manifest.iter("File")}
        require(all(name in listed for name in required[:-1]), "RuntimeList omits required pack assets")
        require(all(name in names for name in listed), "RuntimeList refers to absent pack assets")
        require(all(not name or not name.startswith("runtimes/") or name.startswith(f"runtimes/{rid}/")
                    for name in listed), "RuntimeList contains another RID")
        for name in names:
            if not name.endswith(".so"):
                continue
            require(name.startswith(native), "Native library outside expected RID")
            with archive.open(name) as stream:
                header = stream.read(20)
            require(len(header) == 20 and header[:6] == b"\x7fELF\x02\x01" and
                    int.from_bytes(header[18:20], "little") == RID_ARCH[rid][1],
                    "Native ELF ABI mismatch")
        expected_marker = marker.encode("ascii") + b"\0"
        found = False
        with archive.open(f"{native}libmono-component-debugger.so") as stream:
            tail = b""
            while chunk := stream.read(65536):
                data = tail + chunk
                if expected_marker in data:
                    found = True
                    break
                tail = data[-len(expected_marker):]
        require(found, "Debugger marker absent from normal pack")
    return {"schemaVersion": 1, "kind": "guest-runtime-package-inventory",
            "rid": rid, "id": expected_id, "version": version, "fileName": path.name,
            "sha256": sha256(path), "sizeBytes": path.stat().st_size, "entries": entries,
            "signatureEntryPresent": signature_expected}


def verify_signature(dotnet, package, inventory, env, output, tool_version, observations):
    # Keep nonzero verifier evidence for both unsigned inputs and untrusted TEST outputs.
    arguments = ["nuget", "verify", "--all", package.name]
    log = execute([str(dotnet), *arguments], package.parent, env, output,
                  f"signature.{inventory['id']}", observations, check=False)
    exit_code = observations[-1]["exitCode"]
    require(sha256(package) == inventory["sha256"], "Package changed during signature verification")
    present = inventory["signatureEntryPresent"]
    classification = ("signature-valid-policy-unqualified" if exit_code == 0 else "verification-failed") if present else "unsigned"
    return {"schemaVersion": 1, "kind": "guest-runtime-package-signature",
            "packageSha256": inventory["sha256"], "signatureEntryPresent": present,
            "verificationExitCode": exit_code, "classification": classification,
            "toolVersion": tool_version, "arguments": arguments,
            "outputFileName": log.name, "outputSha256": sha256(log)}


def execute(command, root, env, output, name, observations, check=True):
    log = output / f"{name}.log"
    observation = {"name": name, "argv": list(command), "cwd": str(root),
                   "status": "log-open-failed", "exitCode": None,
                   "logFileName": log.name, "logSha256": None}
    try:
        with log.open("wb") as stream:
            observation["status"] = "launch-failed"
            result = subprocess.run(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT)
            observation["exitCode"] = result.returncode
            observation["status"] = "completed" if result.returncode == 0 else "failed"
    except OSError as error:
        observation["error"] = str(error)
        raise
    finally:
        observations.append(observation)
        if log.is_file():
            observation["logSha256"] = sha256(log)
    require(not check or observation["exitCode"] == 0,
            f"{name} failed with exit {observation['exitCode']}; see {log}")
    return log


def source_evidence(root, recipe, env, output, observations):
    source_log = execute(["git", "rev-parse", "HEAD"], root, env, output, "source-head", observations)
    require(source_log.read_text(encoding="utf-8").strip() == recipe["source"],
            "Checked out source differs from reviewed source")
    status_log = execute(["git", "status", "--porcelain"], root, env, output, "source-status", observations)
    require(not status_log.read_bytes(), "Producer source checkout is dirty")
    execute(["git", "merge-base", "--is-ancestor", BASELINE, "HEAD"],
            root, env, output, "source-baseline", observations)
    execute(["git", "diff", "--binary", f"--output={output / 'source.patch'}", BASELINE, "HEAD"],
            root, env, output, "source-patch", observations)
    return sha256(output / "source.patch")


def template_evidence(root, recipe, env, output, observations):
    commit = env.get("STARTUP_1ES_COMMIT", "")
    require(commit == recipe["expected_1es_commit"], "Actual resolved 1ES commit differs from the reviewed diagnostic baseline")
    require(env.get("STARTUP_1ES_ROOT"), "Checked-out resolved 1ES repository is required")
    templates = Path(env["STARTUP_1ES_ROOT"]).resolve()
    log = execute(["git", "rev-parse", "HEAD"], templates, env, output, "template-head", observations)
    require(log.read_text(encoding="utf-8").strip() == commit, "1ES checkout differs from provider-resolved commit")
    status = execute(["git", "status", "--porcelain"], templates, env, output, "template-status", observations)
    require(not status.read_bytes(), "Resolved 1ES template checkout is dirty")
    records = [{"repository": "dotnet/runtime", "commit": recipe["source"], "path": path,
                "sha256": sha256(root / path)} for path in TEMPLATE_PATHS]
    path = "v1/1ES.Official.PipelineTemplate.yml"
    records.append({"repository": "1ESPipelineTemplates/1ESPipelineTemplates", "commit": commit,
                    "path": path, "sha256": sha256(templates / path)})
    return records


def native_configuration(root, output, rid, marker):
    directory = root / f"artifacts/obj/mono/android.{RID_ARCH[rid][0]}.Release"
    cache = directory / "CMakeCache.txt"
    require(re.search(rf"^MONO_ANDROID_STARTUP_BUILD_ID:(?:STRING|UNINITIALIZED)={re.escape(marker)}$",
                      cache.read_text(encoding="utf-8"), re.M), "Actual CMake cache marker differs from the build plan")
    sources = [("cache", cache)]
    for language in ("C", "CXX"):
        matches = list(directory.glob(f"CMakeFiles/*/CMake{language}Compiler.cmake"))
        require(len(matches) == 1, "Expected exactly one actual CMake compiler identification file")
        sources.append((language.lower() + "-compiler", matches[0]))
    evidence = []
    for role, source in sources:
        destination = output / f"cmake-{role}.{rid}.txt"
        shutil.copyfile(source, destination)
        evidence.append({"sourcePath": source.relative_to(root).as_posix(), **reference(destination)})
    return {"msbuildProperty": "MonoAndroidStartupBuildId", "cmakeVariable": "MONO_ANDROID_STARTUP_BUILD_ID",
            "value": marker, "evidence": evidence}


def produce(root, recipe, ndk, ndk_revision, output=None):
    require(not (root / "artifacts").exists(), "Producer requires a fresh checkout without build outputs")
    require(not (root / ".packages").exists(), "Producer requires a fresh isolated NuGet cache")
    output = output or root / "artifacts/startup-metadata-producer"
    output.mkdir(parents=True)
    observations = []
    receipt = {**recipe, "schemaVersion": 4, "kind": "mono-android-startup-build-receipt", "pipeline": None,
               "status": "started", "packages": [], "package_sidecars": [], "ndk_revision": ndk_revision,
               "native_configuration": {},
               "experiment_attempt": int(recipe["run_id"].split(".")[1]),
               "python_version": sys.version.split()[0], "python_executable": sys.executable,
               "command_observations": observations}
    env = os.environ.copy()
    env["NUGET_PACKAGES"] = str(root / ".packages")
    env["use_global_nuget_cache"] = "false"
    env["ANDROID_NDK_ROOT"] = str(ndk)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    preflight = True
    # Log only explicitly selected tool output; never dump the process environment.
    try:
        require(sys.platform == "linux", "Full producer requires the reviewed Linux container")
        receipt["pipeline"] = pipeline_identity(recipe["source"], "BuildRuntimePacks")
        require(recipe["container"] == PRODUCER_IMAGE, "Producer image differs from the reviewed immutable baseline")
        require(recipe["run_id"] == f"{os.environ.get('BUILD_BUILDID')}.{os.environ.get('STARTUP_EXPERIMENT_ATTEMPT')}",
                "Run identity does not match the common experiment attempt")
        preflight = False
        write_json(output / "plan.json", recipe)
        receipt["patch_sha256"] = source_evidence(root, recipe, env, output, observations)
        receipt["templates"] = template_evidence(root, recipe, env, output, observations)
        properties = ndk / "source.properties"
        require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", ndk_revision), "Invalid expected NDK revision")
        text = properties.read_text(encoding="utf-8")
        require(re.search(rf"^Pkg\.Revision\s*=\s*{re.escape(ndk_revision)}\s*$", text, re.M),
                "NDK source.properties does not match reviewed revision")
        clang = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
        require(clang.is_file(), "Expected container NDK clang is absent")
        (output / "ndk-source.properties").write_bytes(properties.read_bytes())
        receipt["ndk_source_properties_sha256"] = sha256(properties)
        receipt["clang_sha256"] = sha256(clang)
        require(env.get("ANDROID_SDK_ROOT") and env.get("JAVA_HOME"), "Preprovisioned Android SDK and JDK are required")
        sdk = Path(env["ANDROID_SDK_ROOT"]).resolve()
        java_home = Path(env["JAVA_HOME"]).resolve()
        sdk_properties = sorted([*sdk.glob("platforms/*/source.properties"), *sdk.glob("build-tools/*/source.properties")])
        require(0 < len(sdk_properties) <= 128, "Expected bounded installed Android platform/build-tools inventory")
        require(all(path.stat().st_size <= 65536 for path in sdk_properties), "Installed SDK properties exceed bounds")
        receipt["android_sdk"] = {"root": str(sdk), "installed_properties": [
            {"path": str(path.relative_to(sdk)), "sha256": sha256(path),
             "contents": path.read_text(encoding="utf-8")} for path in sdk_properties
        ]}
        receipt["java"] = {"home": str(java_home), "releaseSha256": sha256(java_home / "release"),
                           "javaSha256": sha256(java_home / "bin/java"), "javacSha256": sha256(java_home / "bin/javac")}
        shutil.copyfile(java_home / "release", output / "jdk-release.txt")
        for name, argv in [
            ("python-version", [sys.executable, "--version"]),
            ("clang-version", [str(clang), "--version"]),
            ("cmake-version", ["cmake", "--version"]),
            ("ninja-version", ["ninja", "--version"]),
            ("java-version", [str(java_home / "bin/java"), "-version"]),
            ("javac-version", [str(java_home / "bin/javac"), "-version"]),
        ]:
            execute(argv, root, env, output, name, observations)
        execute(["bash", "./build.sh", "mono.runtime", "-c", "Debug"], root, env, output, "host-baseline", observations)
        execute([sys.executable, "-m", "unittest", "discover", "-s", "eng/diagnostics",
                 "-p", "test_mono_android_startup_producer.py"], root, env, output, "producer-tests", observations)
        host = root / "artifacts/obj/mono/linux.x64.Debug"
        execute(["cmake", "-S", "src/mono", "-B", str(host), "-DMONO_DEBUGGER_STARTUP_TESTS=ON"],
                root, env, output, "host-test-configure", observations)
        execute(["cmake", "--build", str(host), "--target", "mono-debugger-startup-tests"],
                root, env, output, "host-test-build", observations)
        execute([str(host / "mono/mini/mono-debugger-startup-tests"), str(output / "host-fixtures.jsonl")],
                root, env, output, "host-test-run", observations)
        execute(["./.dotnet/dotnet", "--info"], root, env, output, "dotnet-info", observations)
        version_log = execute(["./.dotnet/dotnet", "--version"], root, env, output, "dotnet-version", observations)
        tool_version = version_log.read_text(encoding="utf-8").strip()
        require(re.fullmatch(r"[0-9][a-zA-Z0-9._-]{0,127}", tool_version), "Unexpected SDK version output")
        for step in recipe["commands"]:
            log = execute(step["argv"], root, env, output, step["name"], observations)
            if step["name"].endswith("-build"):
                rid = step["name"].removesuffix("-build")
                receipt["native_configuration"][rid] = native_configuration(root, output, rid, recipe["marker"])
            if step["name"].endswith("-properties"):
                evaluated = json.loads(log.read_text(encoding="utf-8"))["Properties"]
                rid = step["name"].removesuffix("-properties")
                require(evaluated == {
                    "Version": recipe["version"], "PackageVersion": recipe["version"],
                    "RuntimeIdentifier": rid, "MonoAndroidStartupBuildId": recipe["marker"],
                }, "Evaluated normal pack metadata differs from producer plan")
        verified = []
        for rid in RID_ARCH:
            name = f"Microsoft.NETCore.App.Runtime.Mono.{rid}.{recipe['version']}.nupkg"
            matches = list((root / "artifacts/packages/Release").rglob(name))
            require(len(matches) == 1, f"Expected exactly one normal package for {rid}")
            inventory = inventory_package(matches[0], rid, recipe["version"], recipe["marker"])
            receipt["packages"].append(inventory)
            signature = verify_signature(root / ".dotnet/dotnet", matches[0], inventory, env, output, tool_version, observations)
            sidecars = {"id": inventory["id"]}
            for name, content in [("inventory", inventory), ("signature", signature)]:
                file = output / f"{name}.{inventory['id']}.json"
                write_json(file, content)
                sidecars[name + "FileName"] = file.name
                sidecars[name + "Sha256"] = sha256(file)
            receipt["package_sidecars"].append(sidecars)
            verified.append(matches[0])
        packages = output / "packages"
        packages.mkdir()
        for package in verified:
            shutil.copyfile(package, packages / package.name)
        receipt["status"] = "produced-unsigned-unadmitted"
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile, ET.ParseError) as error:
        receipt["status"] = "failed"
        receipt["failure"] = str(error)
        if preflight:
            receipt["providerContext"] = provider_context()
        raise
    finally:
        persist_receipt(output, receipt)


def validate_build_input(folder, recipe, identity):
    receipt = read_json(folder / "receipt.json")
    require(isinstance(receipt, dict) and receipt.get("schemaVersion") == 4 and
            receipt.get("kind") == "mono-android-startup-build-receipt" and
            receipt.get("status") == "produced-unsigned-unadmitted", "Successful schema-4 normal build receipt required")
    require(all(receipt.get(key) == value for key, value in recipe.items()), "Build receipt differs from reviewed plan")
    build = receipt.get("pipeline", {})
    require(isinstance(build, dict) and all(build.get(key) == identity[key] for key in
                ("organization", "project", "definitionId", "buildId", "pipelinePath", "pipelineCommit")) and
            recipe["run_id"] == f"{build.get('buildId')}.{receipt.get('experiment_attempt')}",
            "Build artifact does not belong to this exact producer invocation")
    validate_pipeline_role(build, "BuildRuntimePacks")
    producer_attempt = os.environ.get("PRODUCER_ATTEMPT", "")
    require(re.fullmatch(r"[1-9][0-9]{0,9}", producer_attempt) and int(producer_attempt) <= 2147483647 and
            type(build.get("jobAttempt")) is int and build["jobAttempt"] == int(producer_attempt),
            "Build artifact attempt differs from the downloaded producer attempt")
    retained = receipt.get("retained_files", [])
    require(isinstance(retained, list) and 0 < len(retained) <= 4096, "Invalid build evidence inventory")
    names = set()
    for entry in retained:
        require(isinstance(entry, dict), "Invalid retained-file record")
        name = entry.get("path", "")
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name) and
                all(part not in (".", "..") for part in name.split("/")) and name not in names,
                "Unsafe or duplicate retained-file path")
        names.add(name)
        path = folder / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == entry.get("sizeBytes") and
                sha256(path) == entry.get("sha256"), "Build evidence hash/size mismatch")
    require(not any(path.is_symlink() for path in folder.rglob("*")), "Symlink in downloaded evidence")
    require(names == {path.relative_to(folder).as_posix() for path in folder.rglob("*")
                      if path.is_file() and path != folder / "receipt.json"}, "Uninventoried build evidence")
    require(sha256(folder / "source.patch") == receipt.get("patch_sha256"), "Build source patch mismatch")
    observations = receipt.get("command_observations", [])
    require(isinstance(observations, list) and 0 < len(observations) <= 256, "Missing actual build command observations")
    for command in recipe["commands"]:
        matches = [item for item in observations if isinstance(item, dict) and item.get("name") == command["name"]]
        require(len(matches) == 1 and matches[0].get("argv") == command["argv"] and
                matches[0].get("status") == "completed" and matches[0].get("exitCode") == 0 and
                matches[0].get("logFileName") == command["name"] + ".log" and
                matches[0].get("logSha256") == sha256(folder / (command["name"] + ".log")),
                "Actual build command does not match the successful planned operation")
    inventories = []
    sidecars = []
    expected_packages = set()
    for rid in RID_ARCH:
        package_id = f"Microsoft.NETCore.App.Runtime.Mono.{rid}"
        package = folder / "packages" / f"{package_id}.{recipe['version']}.nupkg"
        inventory = inventory_package(package, rid, recipe["version"], recipe["marker"])
        inventory_file = folder / f"inventory.{package_id}.json"
        signature_file = folder / f"signature.{package_id}.json"
        require(read_json(inventory_file) == inventory, "Original inventory differs from actual unsigned package")
        signature = read_json(signature_file)
        require(isinstance(signature, dict) and signature.get("schemaVersion") == 1 and
                signature.get("kind") == "guest-runtime-package-signature" and
                signature.get("packageSha256") == inventory["sha256"] and
                signature.get("signatureEntryPresent") is False and signature.get("classification") == "unsigned",
                "Unsigned build signature sidecar mismatch")
        configuration = receipt.get("native_configuration", {}).get(rid, {})
        require(configuration.get("value") == recipe["marker"] and
                configuration.get("msbuildProperty") == "MonoAndroidStartupBuildId" and
                configuration.get("cmakeVariable") == "MONO_ANDROID_STARTUP_BUILD_ID",
                "Actual native configuration does not bind the marker")
        expected_evidence = []
        for role in ("cache", "c-compiler", "cxx-compiler"):
            file = folder / f"cmake-{role}.{rid}.txt"
            expected_evidence.append(reference(file))
        require([{key: item.get(key) for key in ("fileName", "sha256")}
                 for item in configuration.get("evidence", [])] == expected_evidence,
                "Native configure evidence differs from the build receipt")
        require(re.search(rf"^MONO_ANDROID_STARTUP_BUILD_ID:(?:STRING|UNINITIALIZED)={re.escape(recipe['marker'])}$",
                          (folder / f"cmake-cache.{rid}.txt").read_text(encoding="utf-8"), re.M),
                "Retained CMake cache marker mismatch")
        sidecars.append({"id": package_id, "inventoryFileName": inventory_file.name,
                         "inventorySha256": sha256(inventory_file), "signatureFileName": signature_file.name,
                         "signatureSha256": sha256(signature_file)})
        inventories.append(inventory)
        expected_packages.add(package)
    require(receipt.get("packages") == inventories and receipt.get("package_sidecars") == sidecars,
            "Build root package binding differs from actual sidecars")
    require(set((folder / "packages").rglob("*")) == expected_packages, "Unexpected package selection")
    return receipt


def signing_properties(recipe, rid, official_build_id):
    require(rid in RID_ARCH, "Unexpected signing RID")
    require(re.fullmatch(r"20[0-9]{6}\.[1-9][0-9]*", official_build_id), "Normal date-based OfficialBuildId required")
    return [
        "/p:TargetOS=android", f"/p:TargetArchitecture={RID_ARCH[rid][0]}", "/p:RuntimeFlavor=Mono",
        "/p:Subset=mono.runtime", f"/p:Version={recipe['version']}", f"/p:PackageVersion={recipe['version']}",
        f"/p:OfficialBuildId={official_build_id}", "/p:SignType=test", "/p:DotNetSignType=test",
        "/p:PostBuildSign=false", "/p:Publish=false", "/p:DotNetPublishUsingPipelines=false",
        "/p:NuGetAudit=true", "/p:NuGetAuditMode=all", "/p:TreatWarningsAsErrors=true",
    ]


def validate_signing_selection(evaluation, package, rid):
    require(isinstance(evaluation, dict), "Invalid normal signing evaluation")
    properties = evaluation.get("Properties", {})
    require(properties.get("OfficialBuild", "").lower() == "true" and
            properties.get("DotNetSignType") == "test" and
            properties.get("ForceDryRunSigning", "").lower() != "true" and
            properties.get("PostBuildSign", "").lower() == "false" and properties.get("TargetRid") == rid,
            "Evaluated normal signing properties differ from the TEST-only plan")
    items = evaluation.get("Items", {}).get("ItemsToSign", [])
    require(len(items) == 1 and Path(items[0].get("FullPath", "")).resolve() == package.resolve(),
            "Normal ItemsToSign must select exactly this diagnostic RID package")


def write_postsign(output, recipe, build_receipt, before, after, signature, signer, sign_evidence, verify_evidence):
    package_id = after["id"]
    inventory_file = output / f"inventory.{package_id}.json"
    signature_file = output / f"signature.{package_id}.json"
    write_json(inventory_file, after)
    write_json(signature_file, signature)
    carrier_path = f"runtimes/{after['rid']}/native/libmono-component-debugger.so"
    carrier = next(entry for entry in after["entries"] if entry["path"] == carrier_path)
    configuration = build_receipt["native_configuration"][after["rid"]]
    native_file = output / f"native-provenance.{package_id}.json"
    write_json(native_file, {
        "schemaVersion": 1, "kind": "guest-runtime-native-provenance",
        "id": package_id, "rid": after["rid"], "version": after["version"], "packageSha256": after["sha256"],
        "inventory": reference(inventory_file), "sourceCommit": recipe["source"],
        "patchSha256": build_receipt["patch_sha256"], "buildMarker": recipe["marker"],
        "configuration": {
            "msbuildProperty": configuration["msbuildProperty"], "cmakeVariable": configuration["cmakeVariable"],
            "value": configuration["value"],
            "evidence": [reference(output / item["fileName"]) for item in configuration["evidence"]],
        },
        "carriers": [{**carrier, "elfClass": 2, "endianness": 1, "machine": RID_ARCH[after["rid"]][1],
                      "gnuBuildId": None, "gnuBuildIdStatus": "not-collected", "markerMatch": "nul-terminated"}],
    })
    old = {entry["path"]: entry for entry in before["entries"]}
    new = {entry["path"]: entry for entry in after["entries"]}
    delta_file = output / f"member-delta.{package_id}.json"
    write_json(delta_file, {
        "schemaVersion": 1, "kind": "mono-android-startup-member-delta",
        "inputSha256": before["sha256"], "outputSha256": after["sha256"], "policyAdmitted": False,
        "changes": [{"path": path, "before": old.get(path), "after": new.get(path)}
                    for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)],
    })
    verified = signature["verificationExitCode"] == 0
    result = {
        "schemaVersion": 1, "kind": "guest-runtime-package-postsign",
        "status": "verified-policy-unqualified" if verified else "produced-verification-failed",
        "id": package_id, "rid": after["rid"], "version": after["version"],
        "source": {"repository": "dotnet/runtime", "baselineCommit": BASELINE,
                   "sourceCommit": recipe["source"], "patchSha256": build_receipt["patch_sha256"]},
        "buildReceipt": reference(output / "build-receipt.json"),
        "input": {**{key: before[key] for key in ("fileName", "sizeBytes", "sha256")},
                  "inventory": reference(output / f"input-inventory.{package_id}.json")},
        "output": {**{key: after[key] for key in ("fileName", "sizeBytes", "sha256")},
                   "inventory": reference(inventory_file), "signature": reference(signature_file)},
        "signer": signer,
        "operationEvidence": [
            {"role": "sign", "evidenceType": "command-receipt", "reference": reference(sign_evidence)},
            {"role": "sign", "evidenceType": "build-binlog", "reference": reference(output / f"sign.{after['rid']}.binlog")},
            {"role": "verify", "evidenceType": "command-receipt", "reference": reference(verify_evidence)},
        ],
        "memberDelta": reference(delta_file),
        "nativeProvenance": reference(native_file),
    }
    file = output / f"postsign.{package_id}.json"
    write_json(file, result)
    return reference(file)


def test_sign(root, recipe, input_folder, output):
    require(not (root / "artifacts").exists() and not (root / ".packages").exists(),
            "TEST-sign job requires a fresh checkout and isolated cache")
    require(output != input_folder and input_folder not in output.parents and output not in input_folder.parents,
            "Input and output evidence directories must be disjoint")
    output.mkdir(parents=True)
    evidence = output / "postsign"
    evidence.mkdir()
    observations = []
    receipt = {
        "schemaVersion": 2, "kind": "mono-android-startup-sign-receipt", "status": "started",
        "source": recipe["source"], "pipeline": None,
        "publication": False, "guest_execution": False,
        "evidenceDirectories": {"build": "build", "presign": "build/packages", "postsign": "postsign"},
        "commandLogDirectory": "postsign", "command_observations": observations, "packages": [],
        "capture_limits": ["No inner signer/repack process exit or task version is inferred from the outer command.",
                           "Normal sign binlogs and final verifier output do not establish clean-consumer trust.",
                           "Template hashes cover the listed entries; the resolved 1ES commit binds its repository."],
    }
    env = dict(os.environ, NUGET_PACKAGES=str(root / ".packages"), use_global_nuget_cache="false",
               PYTHONDONTWRITEBYTECODE="1")
    preflight = True
    try:
        require(sys.platform == "win32", "Normal TEST signing requires the existing Windows MicroBuild job")
        identity = pipeline_identity(recipe["source"], "TestSignRuntimePacks")
        receipt["pipeline"] = identity
        require(recipe["run_id"] == f"{identity['buildId']}.{os.environ.get('STARTUP_EXPERIMENT_ATTEMPT')}",
                "Signing must preserve the common experiment identity, not its own job attempt")
        require(recipe["container"] == PRODUCER_IMAGE, "Build image differs from the reviewed immutable baseline")
        build = validate_build_input(input_folder, recipe, identity)
        preflight = False
        require(source_evidence(root, recipe, env, evidence, observations) == build["patch_sha256"],
                "Signing source patch differs from the build source")
        templates = template_evidence(root, recipe, env, evidence, observations)
        require(templates == build.get("templates"), "Signing templates differ from the original build")
        # The shared signer schema retains raw job identity, not the source-specific phase fields.
        signer = {key: identity[key] for key in ("organization", "project", "definitionId", "pipelinePath",
                                                "pipelineCommit", "buildId", "jobAttempt", "jobName")}
        signer.update(requestedSignType="Test", templates=templates)
        receipt["signer"] = signer
        shutil.copytree(input_folder, output / "build")
        shutil.copyfile(input_folder / "receipt.json", evidence / "build-receipt.json")
        shipping = root / "artifacts/packages/Release/Shipping"
        shipping.mkdir(parents=True)
        for inventory in build["packages"]:
            shutil.copyfile(input_folder / f"inventory.{inventory['id']}.json",
                            evidence / f"input-inventory.{inventory['id']}.json")
            for item in build["native_configuration"][inventory["rid"]]["evidence"]:
                shutil.copyfile(input_folder / item["fileName"], evidence / item["fileName"])
        official_id = env.get("BUILD_BUILDNUMBER", "")
        sdk = read_json(root / "global.json")["msbuild-sdks"]["Microsoft.DotNet.Arcade.Sdk"]
        sign_project = root / ".packages/microsoft.dotnet.arcade.sdk" / sdk / "tools/Sign.proj"
        dotnet = root / ".dotnet/dotnet.exe"
        binlog = root / "artifacts/log/Release/Build.binlog"
        failed_verification = []
        for before in build["packages"]:
            rid = before["rid"]
            package = shipping / before["fileName"]
            # Keep ordinary signing globs unchanged; only this RID is staged at a time.
            require(not list(shipping.iterdir()), "Signing staging directory is not empty")
            shutil.copyfile(input_folder / "packages" / before["fileName"], package)
            properties = signing_properties(recipe, rid, official_id)
            command = ["pwsh", "-NoLogo", "-NoProfile", "-File", "eng/common/build.ps1",
                       "-ci", "-configuration", "Release", "-projects", "src/mono/mono.proj"]
            start = len(observations)
            execute([*command, "-restore", *properties], root, env, evidence, f"restore-sign-tools.{rid}", observations)
            require(binlog.is_file(), "Normal restore binlog is absent")
            shutil.move(str(binlog), evidence / f"restore-sign-tools.{rid}.binlog")
            selection = execute([
                str(dotnet), "msbuild", str(sign_project), f"/p:RepoRoot={root}{os.sep}",
                "/p:Configuration=Release", *properties,
                "-getProperty:OfficialBuild,DotNetSignType,ForceDryRunSigning,PostBuildSign,TargetRid",
                "-getItem:ItemsToSign", "-nologo", "-verbosity:quiet",
            ], root, env, evidence, f"sign-selection.{rid}", observations)
            validate_signing_selection(read_json(selection), package, rid)
            try:
                execute([*command, "-sign", *properties], root, env, evidence, f"normal-sign.{rid}", observations)
            finally:
                # Preserve even failed signer output; never overwrite the original build archive.
                shutil.move(str(package), evidence / package.name)
                if binlog.is_file():
                    shutil.move(str(binlog), evidence / f"sign.{rid}.binlog")
                write_json(evidence / f"sign.{rid}.receipt.json", {
                    "schemaVersion": 1, "kind": "mono-android-startup-command-receipt",
                    "operation": "normal-test-sign", "command_observations": observations[start:],
                    "selection": reference(selection),
                    "binlog": reference(evidence / f"sign.{rid}.binlog") if (evidence / f"sign.{rid}.binlog").is_file() else None,
                })
            require((evidence / f"sign.{rid}.binlog").is_file(), "Actual normal signing binlog is absent")
            final_package = evidence / package.name
            after = inventory_package(final_package, rid, recipe["version"], recipe["marker"], signature_expected=True)
            write_json(evidence / f"inventory.{after['id']}.json", after)
            start = len(observations)
            version_log = execute([str(dotnet), "--version"], final_package.parent, env, evidence,
                                  f"verify-sdk-version.{rid}", observations)
            tool_version = version_log.read_text(encoding="utf-8").strip()
            require(re.fullmatch(r"[0-9][a-zA-Z0-9._-]{0,127}", tool_version), "Unexpected verifier SDK version")
            signature = verify_signature(dotnet, final_package, after, env, evidence, tool_version, observations)
            verification_receipt = evidence / f"verify.{rid}.receipt.json"
            write_json(verification_receipt, {
                "schemaVersion": 1, "kind": "mono-android-startup-command-receipt",
                "operation": "standard-nuget-verify", "command_observations": observations[start:],
            })
            receipt["packages"].append(write_postsign(
                evidence, recipe, build, before, after, signature, signer,
                evidence / f"sign.{rid}.receipt.json", verification_receipt))
            if signature["verificationExitCode"] != 0:
                failed_verification.append(rid)
        receipt["status"] = "produced-verification-failed" if failed_verification else "verified-policy-unqualified"
        require(not failed_verification, "Standard verification failed for " + ", ".join(failed_verification) +
                "; signed bytes, inventories and actual failures are retained without trust changes")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile, ET.ParseError) as error:
        if receipt["status"] != "produced-verification-failed":
            receipt["status"] = "failed"
        receipt["failure"] = str(error)
        if preflight:
            receipt["providerContext"] = provider_context()
        raise
    finally:
        persist_receipt(output, receipt)


def main():
    require(sys.version_info >= (3, 9), "Producer requires Python 3.9 or newer")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--validate-pipeline", action="store_true")
    parser.add_argument("--test-sign", action="store_true")
    parser.add_argument("--source", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--ndk", type=Path)
    parser.add_argument("--ndk-revision")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    recipe = plan(args.source, args.run_id, args.container, args.enable)
    require(not (args.test_sign and args.plan_only), "TEST signing is not a plan-only operation")
    if args.validate_pipeline:
        pipeline_identity(recipe["source"], "ValidateInputs")
        require(recipe["container"] == PRODUCER_IMAGE, "Pipeline image differs from the reviewed baseline")
        require(recipe["run_id"] == f"{os.environ.get('BUILD_BUILDID')}.{os.environ.get('STARTUP_EXPERIMENT_ATTEMPT')}",
                "Run identity does not match the common experiment attempt")
    if args.plan_only:
        print(json.dumps(recipe, indent=2))
        return
    root = Path(__file__).resolve().parents[2]
    if args.test_sign:
        require(args.input is not None and args.output is not None, "TEST signing requires owned input/output directories")
        test_sign(root, recipe, args.input.resolve(), args.output.resolve())
    else:
        require(args.ndk is not None and args.ndk_revision is not None, "Reviewed NDK path/revision required")
        produce(root, recipe, args.ndk.resolve(), args.ndk_revision, args.output.resolve() if args.output else None)


if __name__ == "__main__":
    main()
