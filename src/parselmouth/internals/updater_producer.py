import json
import os
from pathlib import Path
import re

from parselmouth.internals.channels import SupportedChannels
from parselmouth.internals.conda_forge import (
    get_all_archs_available,
    get_subdir_repodata,
)
from parselmouth.internals.s3 import IndexMapping, s3_client


dist_info_pattern = r"([^/]+)-(\d+[^/]*)\.dist-info\/METADATA"
egg_info_pattern = r"([^/]+?)-(\d+[^/]*)\.egg-info\/PKG-INFO"

dist_pattern_compiled = re.compile(dist_info_pattern)
egg_pattern_compiled = re.compile(egg_info_pattern)


def main(
    output_dir: str,
    check_if_exists: bool,
    check_if_pypi_exists: bool,
    channel: SupportedChannels,
    subdir: str | None = None,
):
    subdirs = get_all_archs_available(channel)

    if subdirs is None:
        # fallback list of known subdirs
        subdirs = [
            "emscripten-wasm32",
            "freebsd-64",
            "linux-32",
            "linux-64",
            "linux-aarch64",
            "linux-armv6l",
            "linux-armv7l",
            "linux-ppc64",
            "linux-ppc64le",
            "linux-riscv64",
            "linux-s390x",
            "noarch",
            "osx-64",
            "osx-arm64",
            "wasi-wasm32",
            "win-32",
            "win-64",
            "win-arm64",
            "zos-z",
        ]

    # filter out the subdir we want to update
    if subdir and subdir in subdirs:
        subdirs = [subdir]
    elif subdir and subdir not in subdirs:
        raise ValueError(f"Subdir {subdir} not found in channel {channel}")

    all_packages: list[tuple[str, str]] = []

    if check_if_exists:
        existing_mapping_data = s3_client.get_channel_index(channel=channel)
        if not existing_mapping_data:
            existing_mapping_data = IndexMapping(root={})
    else:
        # a new channel may not have any mapping data. so we need to create an empty one
        existing_mapping_data = IndexMapping(root={})

    letters = set()

    for subdir in subdirs:
        repodatas = {}
        repodata = get_subdir_repodata(subdir, channel)

        repodatas.update(repodata["packages"])
        repodatas.update(repodata["packages.conda"])

        for package_name in repodatas:
            package = repodatas[package_name]
            sha256 = package["sha256"]

            if sha256 not in existing_mapping_data.root:
                all_packages.append(package_name)
                letters.add(f"{subdir}@{package_name[0]}")

            elif check_if_pypi_exists:
                # If the package already exists, we check if it has pypi_normalized_names
                existing_entry = existing_mapping_data.root[sha256]
                # If it does not have pypi_normalized_names, we add it to the list
                if existing_entry.pypi_normalized_names is None:
                    all_packages.append(package_name)
                    letters.add(f"{subdir}@{package_name[0]}")

    index_location = Path(output_dir) / channel / "index.json"
    os.makedirs(index_location.parent, exist_ok=True)

    with open(index_location, mode="w") as mapping_file:
        json.dump(existing_mapping_data.model_dump(), mapping_file)

    json_letters = json.dumps(list(letters))

    print(json_letters)
