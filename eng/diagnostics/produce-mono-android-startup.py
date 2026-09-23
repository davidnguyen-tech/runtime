#!/usr/bin/env python3
# Licensed to the .NET Foundation under one or more agreements.
# The .NET Foundation licenses this file to you under the MIT license.

"""Python 3.9+ manual artifact-only Mono Android producer; records child-command outcomes, never signs or publishes."""

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


def plan(source, run_id, image, enabled=False):
    require(enabled, "Producer is disabled; explicit --enable is required")
    require(re.fullmatch(r"[0-9a-f]{40}", source), "Exact source commit is required")
    require(re.fullmatch(r"[1-9][0-9]{0,11}\.[1-9][0-9]{0,3}", run_id),
            "Run identity must be BuildId.JobAttempt")
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
            "version": version, "marker": marker, "rids": list(RID_ARCH), "commands": commands,
            "output_class": "unsigned-unadmitted-normal-runtime-packs",
            "publication": False, "signing": False, "guest_execution": False}


def inventory_package(path, rid, version, marker):
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
        require(".signature.p7s" not in names, "This route admits only explicitly unsigned output")
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
            f"runtimes/{rid}/lib/net10.0/System.Private.CoreLib.dll",
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
            "signatureEntryPresent": False}


def verify_signature(dotnet, package, inventory, env, output, tool_version, observations):
    # Unlike build commands, verification of this route's unsigned packages must fail.
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


def produce(root, recipe, ndk, ndk_revision):
    require(sys.platform == "linux", "Full producer requires the reviewed Linux container")
    require(os.environ.get("BUILD_REASON") == "Manual", "Only a manual pipeline invocation is allowed")
    require(recipe["run_id"] == f"{os.environ.get('BUILD_BUILDID')}.{os.environ.get('SYSTEM_JOBATTEMPT')}",
            "Run identity does not match the pipeline invocation")
    require(not (root / "artifacts").exists(), "Producer requires a fresh checkout without build outputs")
    require(not (root / ".packages").exists(), "Producer requires a fresh isolated NuGet cache")
    output = root / "artifacts/startup-metadata-producer"
    output.mkdir(parents=True)
    observations = []
    receipt = {**recipe, "schemaVersion": 2, "kind": "mono-android-startup-build-receipt",
               "status": "started", "packages": [], "package_sidecars": [], "ndk_revision": ndk_revision,
               "python_version": sys.version.split()[0], "python_executable": sys.executable,
               "command_observations": observations}
    env = os.environ.copy()
    env["NUGET_PACKAGES"] = str(root / ".packages")
    env["use_global_nuget_cache"] = "false"
    env["ANDROID_NDK_ROOT"] = str(ndk)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Log only explicitly selected tool output; never dump the process environment.
    try:
        (output / "plan.json").write_text(json.dumps(recipe, indent=2) + "\n", encoding="ascii")
        source_log = execute(["git", "rev-parse", "HEAD"], root, env, output, "source-head", observations)
        require(source_log.read_text(encoding="utf-8").strip() == recipe["source"],
                "Checked out source differs from reviewed source")
        status_log = execute(["git", "status", "--porcelain"], root, env, output, "source-status", observations)
        require(not status_log.read_bytes(), "Producer source checkout is dirty")
        execute(["git", "merge-base", "--is-ancestor", BASELINE, "HEAD"],
                root, env, output, "source-baseline", observations)
        execute(["git", "diff", "--binary", f"--output={output / 'source.patch'}", BASELINE, "HEAD"],
                root, env, output, "source-patch", observations)
        receipt["patch_sha256"] = sha256(output / "source.patch")
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
        for name, argv in [
            ("python-version", [sys.executable, "--version"]),
            ("clang-version", [str(clang), "--version"]),
            ("cmake-version", ["cmake", "--version"]),
            ("ninja-version", ["ninja", "--version"]),
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
                file.write_text(json.dumps(content, indent=2) + "\n", encoding="ascii")
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
        raise
    finally:
        receipt["retained_files"] = [
            {"path": path.relative_to(output).as_posix(), "sizeBytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(output.rglob("*")) if path.is_file() and path.name != "receipt.json"
        ]
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="ascii")


def main():
    require(sys.version_info >= (3, 9), "Producer requires Python 3.9 or newer")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--source", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--ndk", type=Path)
    parser.add_argument("--ndk-revision")
    args = parser.parse_args()
    recipe = plan(args.source, args.run_id, args.container, args.enable)
    if args.plan_only:
        print(json.dumps(recipe, indent=2))
        return
    require(args.ndk is not None and args.ndk_revision is not None, "Reviewed NDK path/revision required")
    produce(Path(__file__).resolve().parents[2], recipe, args.ndk.resolve(), args.ndk_revision)


if __name__ == "__main__":
    main()
