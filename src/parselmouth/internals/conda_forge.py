from collections import defaultdict
import json
import os
from pathlib import Path
import tarfile
from ruamel import yaml
from typing import Generator, Tuple
import requests
import logging
import conda_forge_metadata.artifact_info
from conda_forge_metadata.artifact_info.info_json import (
    get_artifact_info_as_json,
    _extract_read,
)
from conda_forge_metadata.types import ArtifactData
from urllib.parse import urljoin
import tempfile
import shutil
import subprocess
import io

from parselmouth.internals.channels import ChannelUrls, SupportedChannels

from dotenv import load_dotenv


def load_anaconda_token() -> str:
    """Load Anaconda API token from .env file."""
    load_dotenv()
    token = os.getenv("ANACONDA_TOKEN")
    if not token:
        raise ValueError("ANACONDA_TOKEN not found in .env file")
    return token


def fetch_channel_labels(channel: SupportedChannels) -> list[str]:
    """Fetch all labels for a given channel from Anaconda API."""
    token = load_anaconda_token()
    headers = {"Authorization": f"token {token}"}

    response = requests.get(
        f"https://api.anaconda.org/channels/{channel}", headers=headers
    )
    response.raise_for_status()

    channel_data = response.json()
    return list(channel_data.keys())


def get_all_archs_available(channel: SupportedChannels) -> list[str]:
    channel_url = ChannelUrls.main_channel(channel)

    response = requests.get(urljoin(channel_url, "channeldata.json"))

    response.raise_for_status()

    channel_json = response.json()
    # Collect all subdirectories
    subdirs: list[str] = []
    for package in channel_json["packages"].values():
        subdirs.extend(package.get("subdirs", []))

    return list(set(subdirs))


def get_subdir_repodata(
    subdir: str,
    channel: SupportedChannels = SupportedChannels.CONDA_FORGE,
    label: str | None = None,
) -> dict:
    channel_url = ChannelUrls.main_channel(channel)

    if label:
        # For labeled channels, use label-specific URL format
        subdir_repodata = (
            f"https://conda.anaconda.org/{channel}/label/{label}/{subdir}/repodata.json"
        )
    else:
        # For standard channels, use the regular URL
        subdir_repodata = urljoin(channel_url, f"{subdir}/repodata.json")

    response = requests.get(subdir_repodata)
    if not response.ok:
        logging.error(
            f"Request for repodata to {subdir_repodata} failed. {response.reason}"
        )

    response.raise_for_status()

    return response.json()


def get_all_packages_by_subdir(
    subdir: str,
    channel: SupportedChannels = SupportedChannels.CONDA_FORGE,
    label: str | None = None,
) -> dict[str, dict]:
    repodatas: dict[str, dict[str, dict]] = defaultdict(dict)

    if not label and channel == SupportedChannels.TANGO_CONTROLS:
        # Fetch all labels for the channel
        labels = fetch_channel_labels(channel)
        for label in labels:
            repodata = get_subdir_repodata(subdir, channel, label)
            # repodatas[label] = {}
            repodatas[label].update(repodata["packages"])
            repodatas[label].update(repodata["packages.conda"])
    else:
        repodata = get_subdir_repodata(subdir, channel)
        repodatas["main"] = {}
        repodatas["main"].update(repodata["packages"])
        repodatas["main"].update(repodata["packages.conda"])

    return repodatas


def download_and_extract_artifact(
    channel: str,
    subdir: str,
    artifact: str,
) -> ArtifactData | None:
    """Download and extract artifact data directly for channels that don't support range requests."""

    artifact_url = f"https://conda.anaconda.org/{channel}/{subdir}/{artifact}"

    try:
        # Download the package to a temporary file
        response = requests.get(artifact_url, stream=True)
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            for chunk in response.iter_content(chunk_size=8192):
                temp_file.write(chunk)
            temp_file_path = temp_file.name

        try:
            # Try to detect file type and extract accordingly
            import zipfile

            # First try as zip file (for .conda files)
            with zipfile.ZipFile(temp_file_path, "r") as zip_file:
                file_list = zip_file.namelist()
                logging.debug(f"Files in {artifact}: {file_list}")

                # Look for info-*.tar.* files (standard format)
                info_files = [f for f in file_list if f.startswith("info-")]

                if info_files:
                    info_file = info_files[0]
                    logging.debug(f"Processing info file: {info_file}")

                    if info_file.endswith(".tar.zst"):
                        # Handle Zstandard compressed tar files
                        return _extract_from_zst_tar(zip_file, info_file)
                    else:
                        # Handle regular tar files
                        with zip_file.open(info_file) as info_tar:
                            with tarfile.open(fileobj=info_tar) as tar:
                                return _extract_artifact_data_from_tar(tar)
                else:
                    logging.warning(f"No info files found in {artifact}")
                    return None

        except Exception as e:
            logging.warning(f"Failed to process as zip file: {e}")
            return None

        finally:
            # Clean up temporary file
            os.unlink(temp_file_path)

    except Exception as e:
        logging.error(f"Failed to download or process {artifact}: {e}")
        return None


def _extract_from_zst_tar(zip_file, zst_file_path: str) -> ArtifactData | None:
    """Extract artifact data from a Zstandard compressed tar file within a zip."""
    try:
        # Try using zstd library if available
        try:
            import zstandard as zstd

            with zip_file.open(zst_file_path) as zst_data:
                # Read all zst data into memory first
                zst_bytes = zst_data.read()
                dctx = zstd.ZstdDecompressor()
                decompressed_bytes = dctx.decompress(zst_bytes)

                # Open decompressed tar data from memory
                with tarfile.open(
                    fileobj=io.BytesIO(decompressed_bytes), mode="r"
                ) as tar:
                    return _extract_artifact_data_from_tar_stream(tar)
        except ImportError:
            # Fallback to subprocess if zstd library not available
            logging.info("zstandard library not available, trying subprocess")

            # Extract zst file to temporary location
            with tempfile.NamedTemporaryFile(
                suffix=".tar.zst", delete=False
            ) as temp_zst:
                with zip_file.open(zst_file_path) as zst_data:
                    shutil.copyfileobj(zst_data, temp_zst)
                temp_zst_path = temp_zst.name

            try:
                # Decompress using zstd command line tool
                result = subprocess.run(
                    ["zstd", "-d", "-c", temp_zst_path], capture_output=True, check=True
                )

                # Open decompressed tar data
                with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r") as tar:
                    return _extract_artifact_data_from_tar_stream(tar)
            finally:
                os.unlink(temp_zst_path)

    except Exception as e:
        logging.error(f"Failed to extract from zstd file {zst_file_path}: {e}")
        return None


def _extract_artifact_data_from_zip(zip_file) -> ArtifactData | None:
    """Extract artifact data from a zip file structure."""
    data = {
        "metadata_version": 1,
        "name": "",
        "version": "",
        "index": {},
        "about": {},
        "rendered_recipe": {},
        "raw_recipe": "",
        "conda_build_config": {},
        "files": [],
    }

    YAML = yaml.YAML(typ="safe")
    YAML.allow_duplicate_keys = True
    old_files = None

    for file_path in zip_file.namelist():
        path = Path(file_path)

        # Skip directories
        if file_path.endswith("/"):
            continue

        # Handle different possible structures
        if len(path.parts) > 1 and path.parts[0] == "info":
            path = Path(*path.parts[1:])

        if path.parts and path.parts[0] in ("test", "tests", "licenses"):
            continue

        try:
            with zip_file.open(file_path) as content_file:
                content_str = content_file.read().decode("utf-8", errors="ignore")

            if path.name == "index.json":
                index = json.loads(content_str)
                data["name"] = index.get("name", "")
                data["version"] = index.get("version", "")
                data["index"] = index
            elif path.name == "about.json":
                data["about"] = json.loads(content_str)
            elif path.name == "conda_build_config.yaml":
                data["conda_build_config"] = YAML.load(content_str)
            elif path.name == "files":
                old_files = content_str.splitlines()
            elif path.name == "paths.json":
                paths = json.loads(content_str)
                paths = paths.get("paths", [])
                all_paths = [p["_path"] for p in paths]
                data["files"] = [
                    f for f in all_paths if not f.lower().endswith((".pyc", ".txt"))
                ]
            elif path.name == "meta.yaml.template":
                data["raw_recipe"] = content_str
            elif path.name == "meta.yaml":
                if ("{{" in content_str or "{%" in content_str) and not data[
                    "raw_recipe"
                ]:
                    data["raw_recipe"] = content_str
                else:
                    data["rendered_recipe"] = YAML.load(content_str)

        except Exception as e:
            logging.warning(f"Failed to process {file_path}: {e}")
            continue

    # Fallback to old files format if paths.json wasn't found
    if not data["files"] and old_files is not None:
        data["files"] = [
            f for f in old_files if not f.lower().endswith((".pyc", ".txt"))
        ]

    if data["name"]:
        return data  # type: ignore

    return None


def _extract_artifact_data_from_tar_stream(tar: tarfile.TarFile) -> ArtifactData | None:
    """Extract artifact data from a tar file, handling streaming data carefully."""
    data = {
        "metadata_version": 1,
        "name": "",
        "version": "",
        "index": {},
        "about": {},
        "rendered_recipe": {},
        "raw_recipe": "",
        "conda_build_config": {},
        "files": [],
    }

    YAML = yaml.YAML(typ="safe")
    YAML.allow_duplicate_keys = True
    old_files = None

    # Process all members in order, since seeking backwards isn't allowed
    for member in tar.getmembers():
        if not member.isfile():
            continue

        path = Path(member.name)
        if len(path.parts) > 1 and path.parts[0] == "info":
            path = Path(*path.parts[1:])
        if path.parts and path.parts[0] in ("test", "tests", "licenses"):
            continue

        # Skip problematic files that might cause seeking issues
        if path.name in ("git", ".git", "has_prefix"):
            logging.debug(f"Skipping problematic file: {member.name}")
            continue

        try:
            content = tar.extractfile(member)
            if content is None:
                continue

            # Read content carefully to avoid seeking issues
            try:
                content_bytes = content.read()
                content_str = content_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                logging.warning(f"Failed to read content from {member.name}: {e}")
                continue

            if path.name == "index.json":
                try:
                    index = json.loads(content_str)
                    data["name"] = index.get("name", "")
                    data["version"] = index.get("version", "")
                    data["index"] = index
                except json.JSONDecodeError as e:
                    logging.warning(f"Failed to parse index.json: {e}")
            elif path.name == "about.json":
                try:
                    data["about"] = json.loads(content_str)
                except json.JSONDecodeError as e:
                    logging.warning(f"Failed to parse about.json: {e}")
            elif path.name == "conda_build_config.yaml":
                try:
                    data["conda_build_config"] = YAML.load(content_str)
                except Exception as e:
                    logging.warning(f"Failed to parse conda_build_config.yaml: {e}")
            elif path.name == "files":
                old_files = content_str.splitlines()
            elif path.name == "paths.json":
                try:
                    paths = json.loads(content_str)
                    paths = paths.get("paths", [])
                    all_paths = [p["_path"] for p in paths]
                    data["files"] = [
                        f for f in all_paths if not f.lower().endswith((".pyc", ".txt"))
                    ]
                except json.JSONDecodeError as e:
                    logging.warning(f"Failed to parse paths.json: {e}")
            elif path.name == "meta.yaml.template":
                data["raw_recipe"] = content_str
            elif path.name == "meta.yaml":
                try:
                    if ("{{" in content_str or "{%" in content_str) and not data[
                        "raw_recipe"
                    ]:
                        data["raw_recipe"] = content_str
                    else:
                        data["rendered_recipe"] = YAML.load(content_str)
                except Exception as e:
                    logging.warning(f"Failed to parse meta.yaml: {e}")

        except Exception as e:
            logging.warning(f"Failed to process {member.name}: {e}")
            continue

    # Fallback to old files format if paths.json wasn't found
    if not data["files"] and old_files is not None:
        data["files"] = [
            f for f in old_files if not f.lower().endswith((".pyc", ".txt"))
        ]

    if data["name"]:
        return data  # type: ignore

    return None


def _extract_artifact_data_from_tar(tar: tarfile.TarFile) -> ArtifactData | None:
    """Extract artifact data from a tar file."""
    data = {
        "metadata_version": 1,
        "name": "",
        "version": "",
        "index": {},
        "about": {},
        "rendered_recipe": {},
        "raw_recipe": "",
        "conda_build_config": {},
        "files": [],
    }

    YAML = yaml.YAML(typ="safe")
    YAML.allow_duplicate_keys = True
    old_files = None

    for member in tar.getmembers():
        if not member.isfile():
            continue

        path = Path(member.name)
        if len(path.parts) > 1 and path.parts[0] == "info":
            path = Path(*path.parts[1:])
        if path.parts and path.parts[0] in ("test", "licenses"):
            continue

        try:
            content = tar.extractfile(member)
            if content is None:
                continue
            content_str = content.read().decode("utf-8", errors="ignore")

            if path.name == "index.json":
                index = json.loads(content_str)
                data["name"] = index.get("name", "")
                data["version"] = index.get("version", "")
                data["index"] = index
            elif path.name == "about.json":
                data["about"] = json.loads(content_str)
            elif path.name == "conda_build_config.yaml":
                data["conda_build_config"] = YAML.load(content_str)
            elif path.name == "files":
                old_files = content_str.splitlines()
            elif path.name == "paths.json":
                paths = json.loads(content_str)
                paths = paths.get("paths", [])
                all_paths = [p["_path"] for p in paths]
                data["files"] = [
                    f for f in all_paths if not f.lower().endswith((".pyc", ".txt"))
                ]
            elif path.name == "meta.yaml.template":
                data["raw_recipe"] = content_str
            elif path.name == "meta.yaml":
                if ("{{" in content_str or "{%" in content_str) and not data[
                    "raw_recipe"
                ]:
                    data["raw_recipe"] = content_str
                else:
                    data["rendered_recipe"] = YAML.load(content_str)

        except Exception as e:
            logging.warning(f"Failed to process {member.name}: {e}")
            continue

    # Fallback to old files format if paths.json wasn't found
    if not data["files"] and old_files is not None:
        data["files"] = [
            f for f in old_files if not f.lower().endswith((".pyc", ".txt"))
        ]

    if data["name"]:
        return data  # type: ignore

    return None


def get_artifact_info(
    subdir,
    artifact,
    backend,
    channel: str = SupportedChannels.CONDA_FORGE.value,
    label: str | None = None,
):
    # channel_enum = SupportedChannels(channel)

    # For Tango Controls, use direct download method to avoid range request issues
    if backend == "download":
        return download_and_extract_artifact(channel, subdir, artifact)

    if backend == "streamed" and artifact.endswith(".tar.bz2"):
        # bypass get_artifact_info_as_json as it does not support .tar.bz2
        from conda_forge_metadata.streaming import get_streamed_artifact_data

        return _patched_info_json_from_tar_generator(
            get_streamed_artifact_data(channel, subdir, artifact),
            skip_files_suffixes=(".pyc", ".txt"),
        )

    # patch the info_json_from_tar_generator to handle the paths.json
    # instead of the `files`
    # TODO: Upstream this fix to conda-forge-metadata itself
    conda_forge_metadata.artifact_info.info_json.info_json_from_tar_generator = (
        _patched_info_json_from_tar_generator
    )
    return get_artifact_info_as_json(channel, subdir, artifact, backend)


def _patched_info_json_from_tar_generator(
    tar_tuples: Generator[Tuple[tarfile.TarFile, tarfile.TarInfo], None, None],
    skip_files_suffixes: Tuple[str, ...] = (".pyc", ".txt"),
) -> ArtifactData | None:
    # https://github.com/regro/libcflib/blob/062858e90af/libcflib/harvester.py#L14
    data = {
        "metadata_version": 1,
        "name": "",
        "version": "",
        "index": {},
        "about": {},
        "rendered_recipe": {},
        "raw_recipe": "",
        "conda_build_config": {},
        "files": [],
    }
    YAML = yaml.YAML(typ="safe")
    # some recipes have duplicate keys;
    # e.g. linux-64/clangxx_osx-64-16.0.6-h027b494_6.conda
    YAML.allow_duplicate_keys = True
    old_files = None
    for tar, member in tar_tuples:
        path = Path(member.name)
        if len(path.parts) > 1 and path.parts[0] == "info":
            path = Path(*path.parts[1:])
        if path.parts and path.parts[0] in ("test", "licenses"):
            continue
        if path.name == "index.json":
            index = json.loads(_extract_read(tar, member, default="{}"))
            data["name"] = index.get("name", "")
            data["version"] = index.get("version", "")
            data["index"] = index
        elif path.name == "about.json":
            data["about"] = json.loads(_extract_read(tar, member, default="{}"))
        elif path.name == "conda_build_config.yaml":
            data["conda_build_config"] = YAML.load(
                _extract_read(tar, member, default="{}")
            )
        elif path.name == "files":
            # fallback to old files if no paths.json is found
            old_files = _extract_read(tar, member, default="").splitlines()
        elif path.name == "paths.json":
            paths = json.loads(_extract_read(tar, member, default="{}"))
            paths = paths.get("paths", [])
            all_paths = [p["_path"] for p in paths]
            if skip_files_suffixes:
                all_paths = [
                    f for f in all_paths if not f.lower().endswith(skip_files_suffixes)
                ]
            data["files"] = all_paths
        elif path.name == "meta.yaml.template":
            data["raw_recipe"] = _extract_read(tar, member, default="")
        elif path.name == "meta.yaml":
            x = _extract_read(tar, member, default="{}")
            if ("{{" in x or "{%" in x) and not data["raw_recipe"]:
                data["raw_recipe"] = x
            else:
                data["rendered_recipe"] = YAML.load(x)

    # in case no paths.json is found, fallback to reading paths
    # from `files` file.
    if not data["files"] and old_files is not None:
        if skip_files_suffixes:
            old_files = [
                f for f in old_files if not f.lower().endswith(skip_files_suffixes)
            ]
        data["files"] = old_files

    if data["name"]:
        return data  # type: ignore

    return None
