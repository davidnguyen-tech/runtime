#!/usr/bin/env python3
# Licensed to the .NET Foundation under one or more agreements.
# The .NET Foundation licenses this file to you under the MIT license.

"""Manual, artifact-only Intel Mac verification of the held Real-signed runtime packs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request

producer = runpy.run_path(str(Path(__file__).with_name("produce-mono-android-startup.py")))
require = producer["require"]
read_json = producer["read_json"]
sha256 = producer["sha256"]
inventory_package = producer["inventory_package"]

SOURCE = "0d81e67d9044e68f7304a08df125ece26f054dbe"
RUN = 3089937
VERSION = "10.0.12-startup.3089937.1.s0d81e67d9044"
MARKER = "runtime-4271-3089937.1-0d81e67d9044"
ARTIFACT_ID = 76626017
ARTIFACT_NAME = "mono-android-startup-real-signed-unadmitted-1"
RECEIPT_SHA256 = "774be14d603180cdec10abbde330ac81f12b13afbeb508784caf2d2bdea8a401"
AUTHOR = "9A1B131BEE0605433056A4EA3815478A8E177961A968C6C0027C1093D1FEB630"
SDK_URL = "https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.401/dotnet-sdk-10.0.401-osx-x64.tar.gz"
SDK_SHA512 = "33401b4a2da8554e3306db6072ea8569d9fcc608509c271e0aa4b39e7cc432da3631f14e7e1e2445d67d72550d18ce44a8bbd2382a756867ad2edab6b1c963c0"
MAX_LOG_BYTES = 8 * 1024 * 1024
CHILD_TIMEOUT_SECONDS = 120
PACKAGES = {
    "android-x64": ("356962a4419d14b0d5352bb291136401dbf41c26bfd0345216e55eb456f97a14", 26501239),
    "android-arm64": ("76f58515b8d0f1249fb8a5a2ebee188a2fa885efc63d523790da0bcd84b28fe7", 26587358),
}
ORIGINALS = {
    "android-x64": "948a66f1e59b3b4bdc6f66b9fa39713ac9e0b9577fe6b217916ceee3891e95b7",
    "android-arm64": "44749cdeacc05e860ca9e44f33daef6729d751f32fd43f1d818548ca1fba991d",
}


def reference(path):
    return {"fileName": path.name, "sizeBytes": path.stat().st_size, "sha256": sha256(path)}


def validate_input(folder):
    receipt_file = folder / "receipt.json"
    require(receipt_file.is_file() and not receipt_file.is_symlink() and sha256(receipt_file) == RECEIPT_SHA256,
            "Held signing receipt hash mismatch")
    receipt = read_json(receipt_file)
    pipeline = receipt.get("pipeline", {})
    require(receipt.get("schemaVersion") == 3 and receipt.get("kind") == "mono-android-startup-sign-receipt"
            and receipt.get("status") == "verified-policy-unqualified" and receipt.get("source") == SOURCE
            and receipt.get("publication") is False and receipt.get("guest_execution") is False
            and isinstance(pipeline, dict) and pipeline.get("buildId") == RUN
            and pipeline.get("definitionId") == 679
            and pipeline.get("phaseName") == "RealSignRuntimePacks"
            and pipeline.get("pipelineCommit") == SOURCE
            and isinstance(receipt.get("signer"), dict)
            and receipt.get("signer", {}).get("requestedSignType") == "Real",
            "Held Real-signing receipt provenance mismatch")
    retained = receipt.get("retained_files")
    require(isinstance(retained, list) and 0 < len(retained) <= 4096, "Invalid retained inventory")
    names = set()
    for entry in retained:
        name = entry.get("path", "") if isinstance(entry, dict) else ""
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name)
                and all(part not in (".", "..") for part in name.split("/")) and name not in names,
                "Unsafe retained path")
        names.add(name)
        file = folder / name
        require(file.is_file() and not file.is_symlink() and file.stat().st_size == entry.get("sizeBytes")
                and sha256(file) == entry.get("sha256"), "Held artifact retained-file mismatch")
    require(not any(path.is_symlink() for path in folder.rglob("*"))
            and names == {path.relative_to(folder).as_posix() for path in folder.rglob("*")
                          if path.is_file() and path != receipt_file},
            "Unlisted file or link in downloaded artifact")
    require((folder / "build/receipt.json").read_bytes() == (folder / "postsign/build-receipt.json").read_bytes(),
            "Unsigned receipt alias differs")
    build = read_json(folder / "build/receipt.json")
    require(build.get("schemaVersion") == 5 and build.get("status") == "produced-unsigned-unadmitted"
            and build.get("source") == SOURCE and isinstance(build.get("pipeline"), dict)
            and build["pipeline"].get("buildId") == RUN
            and build.get("pipeline", {}).get("phaseName") == "BuildRuntimePacks", "Original build provenance mismatch")
    require(isinstance(receipt.get("packages"), list) and len(receipt["packages"]) == 2,
            "Exactly two signed post-sign receipts required")
    verified = []
    for rid, (digest, size) in PACKAGES.items():
        name = f"Microsoft.NETCore.App.Runtime.Mono.{rid}.{VERSION}.nupkg"
        original = folder / "build/packages" / name
        signed = folder / "postsign" / name
        require(original.is_file() and sha256(original) == ORIGINALS[rid]
                and signed.is_file() and signed.stat().st_size == size and sha256(signed) == digest,
                "Held RID package differs from reviewed bytes")
        inventory = inventory_package(signed, rid, VERSION, MARKER, signature_expected=True)
        sidecar = folder / "postsign" / f"inventory.Microsoft.NETCore.App.Runtime.Mono.{rid}.json"
        require(inventory == read_json(sidecar), "Signed member inventory differs from actual package")
        post_file = folder / "postsign" / f"postsign.Microsoft.NETCore.App.Runtime.Mono.{rid}.json"
        post = read_json(post_file)
        require({"fileName": post_file.name, "sha256": sha256(post_file)} in receipt["packages"]
                and post.get("status") == "verified-policy-unqualified"
                and post.get("input", {}).get("sha256") == ORIGINALS[rid]
                and post.get("output", {}).get("sha256") == digest
                and post.get("output", {}).get("inventory", {}).get("sha256") == sha256(sidecar)
                and post.get("signer") == receipt["signer"],
                "Post-sign receipt does not bind the reviewed input and output")
        signature = read_json(folder / "postsign" / f"signature.Microsoft.NETCore.App.Runtime.Mono.{rid}.json")
        require(signature.get("packageSha256") == digest and signature.get("signatureEntryPresent") is True
                and signature.get("verificationExitCode") == 0
                and signature.get("classification") == "signature-valid-policy-unqualified",
                "Windows signature evidence mismatch")
        verified.append({"rid": rid, "package": reference(signed), "unsignedSha256": ORIGINALS[rid]})
    expected_names = {f"Microsoft.NETCore.App.Runtime.Mono.{rid}.{VERSION}.nupkg" for rid in PACKAGES}
    require({p.name for p in (folder / "postsign").glob("*.nupkg")} == expected_names
            and {p.name for p in (folder / "build/packages").glob("*.nupkg")} == expected_names,
            "Unexpected signed or unsigned package")
    return verified


def stop_child(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()


def run(command, cwd, output, name, observations, env=None,
        timeout_seconds=CHILD_TIMEOUT_SECONDS, max_log_bytes=MAX_LOG_BYTES):
    log = output / (name + ".log")
    observation = {"name": name, "argv": [str(x) for x in command]}
    try:
        with log.open("wb") as stream:
            with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, start_new_session=os.name == "posix") as process:
                exceeded = threading.Event()
                errors = []

                def capture():
                    remaining = max_log_bytes
                    try:
                        while chunk := process.stdout.read(65536):
                            retained = chunk[:remaining]
                            stream.write(retained)
                            remaining -= len(retained)
                            if len(retained) != len(chunk):
                                exceeded.set()
                                stop_child(process)
                                break
                    except OSError as error:
                        errors.append(error)
                        stop_child(process)

                reader = threading.Thread(target=capture, daemon=True)
                reader.start()
                timed_out = False
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop_child(process)
                    process.wait()
                reader.join(timeout=5)
                if reader.is_alive():
                    stop_child(process)
                    reader.join(timeout=5)
                observation.update({"exitCode": process.returncode, "timedOut": timed_out,
                                    "logLimitExceeded": exceeded.is_set(),
                                    "captureFailed": reader.is_alive() or bool(errors)})
        observation["log"] = reference(log)
        observations.append(observation)
        require(not observation["captureFailed"] and not timed_out
                and not exceeded.is_set() and process.returncode == 0,
                name + " failed, timed out, exceeded the output limit, or could not capture output")
    except OSError as error:
        observation.update({"launchFailure": str(error), "log": reference(log) if log.is_file() else None})
        observations.append(observation)
        raise
    return log


def mac_host(output, observations):
    require(sys.platform == "darwin" and platform.machine() == "x86_64", "Native Darwin x86_64 required")
    for name in ("hw.optional.arm64", "sysctl.proc_translated"):
        log = output / (name + ".log")
        result = subprocess.run(["sysctl", "-n", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        log.write_bytes(result.stdout[:4096])
        observations.append({"name": name, "exitCode": result.returncode, "log": reference(log)})
        require(result.returncode in (0, 1) and (result.stdout.strip() == b"0" if result.returncode == 0
                else b"unknown oid" in result.stdout.lower()),
                "Cannot establish native Intel Mac host (or Rosetta detected)")


def sdk_install(workspace, output, observations):
    archive = workspace / "sdk-osx-x64.tar.gz"
    digest = hashlib.sha512()
    size = 0
    with urllib.request.urlopen(SDK_URL, timeout=120) as source, archive.open("xb") as target:
        while chunk := source.read(65536):
            size += len(chunk)
            require(size <= 1024 * 1024 * 1024, "SDK download exceeds size bound")
            digest.update(chunk)
            target.write(chunk)
    require(digest.hexdigest() == SDK_SHA512, "Official osx-x64 SDK archive SHA-512 mismatch")
    observations.append({"name": "sdk-download", "url": SDK_URL, "sha512": digest.hexdigest(),
                         "archiveSizeBytes": archive.stat().st_size})
    sdk = workspace / "sdk"
    sdk.mkdir()
    extract_sdk_archive(archive, sdk)
    binary = sdk / "dotnet"
    require(binary.is_file() and not binary.is_symlink(), "Official SDK dotnet binary absent")
    return binary


def extract_sdk_archive(archive_path, destination):
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        require(0 < len(members) <= 100000 and
                sum(member.size for member in members) <= 4 * 1024 * 1024 * 1024,
                "SDK archive exceeds entry or expanded-byte bound")
        targets = set()
        for member in members:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if name in ("", ".") and member.isdir():
                continue
            parts = name.rstrip("/").split("/")
            require(member.isfile() or member.isdir(), "SDK archive contains a link or special entry")
            require(name and not name.startswith("/") and
                    all(part not in ("", ".", "..") and "\\" not in part and ":" not in part for part in parts)
                    and "/".join(parts) == name.rstrip("/"), "Unsafe SDK archive path")
            key = "/".join(parts).casefold()
            require(key not in targets, "Duplicate or case-colliding SDK archive path")
            targets.add(key)
        for member in members:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if name in ("", ".") and member.isdir():
                continue
            target = destination.joinpath(*name.rstrip("/").split("/"))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("xb") as sink:
                    shutil.copyfileobj(source, sink, 65536)
                target.chmod(member.mode & 0o777)


def sdk_trust_snapshot(binary):
    sdk = binary.parent
    paths = [binary] + [sdk / "sdk" / "10.0.401" / "trustedroots" / name
                        for name in ("codesignctl.pem", "timestampctl.pem")]
    require(all(path.is_file() and not path.is_symlink() for path in paths),
            "Pinned SDK executable or trusted roots missing")
    return [{"path": path.relative_to(sdk).as_posix(), **reference(path)} for path in paths]


def verify(folder, output, verifier_source):
    require(not output.exists() and output != folder and folder not in output.parents and output not in folder.parents,
            "Fresh disjoint evidence output required")
    output.mkdir(parents=True)
    receipt = {"schemaVersion": 1, "kind": "held-mono-android-mac-signature-verification",
               "status": "started", "sourceRun": RUN, "sourceCommit": SOURCE, "sourceArtifactId": ARTIFACT_ID,
               "sourceArtifactName": ARTIFACT_NAME, "admitted": False, "guestExecution": False,
               "observations": [], "packages": []}
    try:
        require(re.fullmatch(r"[0-9a-f]{40}", verifier_source) is not None
                and os.environ.get("BUILD_SOURCEVERSION") == verifier_source
                and os.environ.get("BUILD_REASON") == "Manual" and os.environ.get("SYSTEM_TEAMPROJECT") == "internal"
                and os.environ.get("SYSTEM_DEFINITIONID") == "679"
                and os.environ.get("BUILD_REPOSITORY_ID") == producer["REPOSITORY_ID"]
                and os.environ.get("SYSTEM_STAGENAME") == "MonoStartupMacVerification"
                and os.environ.get("SYSTEM_PHASENAME") == "VerifyHeldRealSignatures"
                and os.environ.get("BUILD_SOURCEBRANCH", "").startswith("refs/heads/")
                and not os.environ["BUILD_SOURCEBRANCH"].startswith(("refs/heads/main", "refs/heads/master",
                                                                       "refs/heads/release/", "refs/heads/internal/release/")),
                "Only reviewed internal manual feature-branch verification is allowed")
        receipt["verifierSourceCommit"] = verifier_source
        inherited = {name: os.environ.get(name) for name in
                     ("DOTNET_NUGET_SIGNATURE_VERIFICATION", "NUGET_CERT_REVOCATION_MODE")}
        receipt["inheritedVerifierSettings"] = inherited
        require(inherited["DOTNET_NUGET_SIGNATURE_VERIFICATION"] in (None, "true", "True", "TRUE")
                and inherited["NUGET_CERT_REVOCATION_MODE"] in (None, "online", "Online", "ONLINE"),
                "Inherited NuGet signature or revocation settings are not trusted defaults")
        receipt["packages"] = validate_input(folder)
        mac_host(output, receipt["observations"])
        with tempfile.TemporaryDirectory(prefix="mono-startup-sdk-", dir=os.environ.get("AGENT_TEMPDIRECTORY")) as temp:
            binary = sdk_install(Path(temp), output, receipt["observations"])
            cli_home = Path(temp) / "cli-home"
            packages = Path(temp) / "nuget-packages"
            cli_home.mkdir()
            packages.mkdir()
            environment = dict(os.environ, DOTNET_ROOT=str(binary.parent), DOTNET_MULTILEVEL_LOOKUP="0",
                               DOTNET_ROLL_FORWARD="Disable", DOTNET_GENERATE_ASPNET_CERTIFICATE="false",
                               DOTNET_CLI_HOME=str(cli_home), NUGET_PACKAGES=str(packages))
            before = sdk_trust_snapshot(binary)
            receipt["sdkTrust"] = {"before": before}
            try:
                for package in receipt["packages"]:
                    rid = package["rid"]
                    file = folder / "postsign" / package["package"]["fileName"]
                    version = run([str(binary), "--version"], file.parent, output, f"sdk-version.{rid}",
                                  receipt["observations"], environment)
                    require(version.read_bytes().strip() == b"10.0.401", "Verifier SDK version mismatch")
                    try:
                        run([str(binary), "nuget", "verify", "--all", "--certificate-fingerprint", AUTHOR, file.name],
                            file.parent, output, f"verify.{rid}", receipt["observations"], environment)
                        package["verificationExitCode"] = 0
                    except ValueError:
                        package["verificationExitCode"] = receipt["observations"][-1]["exitCode"]
            finally:
                receipt["sdkTrust"]["after"] = sdk_trust_snapshot(binary)
                require(receipt["sdkTrust"]["after"] == before, "Verifier SDK executable or trust roots changed")
        require(all(package.get("verificationExitCode") == 0 for package in receipt["packages"]),
                "Intel Mac normal verifier rejected one or more held packages")
        receipt["status"] = "verified-policy-unqualified"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        receipt["status"] = "failed"
        receipt["failure"] = str(error)
        raise
    finally:
        receipt["retainedFiles"] = [reference(path) for path in sorted(output.iterdir()) if path.is_file()
                                    and path.name != "receipt.json"]
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=True) + "\n", encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", required=True, help="Reviewed commit of this verification route")
    args = parser.parse_args()
    verify(args.input.resolve(), args.output.resolve(), args.source)


if __name__ == "__main__":
    main()
