import json
from pathlib import Path
import tarfile
from ruamel import yaml
from typing import Generator, Optional, Tuple
import requests
import logging
import conda_forge_metadata.artifact_info
from conda_forge_metadata.artifact_info.info_json import (
    get_artifact_info_as_json,
    _extract_read,
)
from conda_forge_metadata.types import ArtifactData
from urllib.parse import urljoin

from parselmouth.internals.channels import ChannelUrls, SupportedChannels


def get_all_archs_available(channel: SupportedChannels) -> Optional[list[str]]:
    channel_url = ChannelUrls.main_channel(channel)

    response = requests.get(urljoin(channel_url, "channeldata.json"))

    if not response.ok:
        logging.error(
            f"Request for channeldata to {channel_url} failed. {response.reason}"
        )
        return None

    channel_json = response.json()
    # Collect all subdirectories
    subdirs: list[str] = []
    for package in channel_json["packages"].values():
        subdirs.extend(package.get("subdirs", []))

    return list(set(subdirs))


def get_subdir_repodata(
    subdir: str, channel: SupportedChannels = SupportedChannels.CONDA_FORGE
) -> dict:
    channel_url = ChannelUrls.main_channel(channel)

    subdir_repodata = urljoin(channel_url, f"{subdir}/repodata.json")
    response = requests.get(subdir_repodata)
    if not response.ok:
        logging.error(
            f"Request for repodata to {subdir_repodata} failed. {response.reason}"
        )

    response.raise_for_status()

    return response.json()


def get_all_packages_by_subdir(
    subdir: str, channel: SupportedChannels = SupportedChannels.CONDA_FORGE
) -> dict[str, dict]:
    repodatas: dict[str, dict] = {}

    repodata = get_subdir_repodata(subdir, channel)

    repodatas.update(repodata["packages"])
    repodatas.update(repodata["packages.conda"])

    return repodatas


def get_artifact_info(
    subdir,
    artifact,
    backend,
    channel: SupportedChannels = SupportedChannels.CONDA_FORGE,
):
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
