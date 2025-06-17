#!/usr/bin/env bash

README_VERSION="${README_VERSION:-0f303d99d37256976219edd8a2ad0df4875b1cdd}"
DIR="$(dirname "$(realpath "${BASH_SOURCE[@]}")")"

if ! [ -d "${DIR}/.venv" ]; then
    python3 -m venv "${DIR}/.venv"
    source "${DIR}/.venv/bin/activate"
    pip install "${DIR}/tools/md2json"
else
    source "${DIR}/.venv/bin/activate"
fi

TEMP_DIR="$(mktemp -d)"

function cleanup() {
    rm -rf "${TEMP_DIR}"
}

trap cleanup EXIT

wget -q --show-progress -O "${TEMP_DIR}/Caliptra.md" "https://raw.githubusercontent.com/chipsalliance/Caliptra/${README_VERSION}/README.md"

md2json --section versioning "$@" --output "${DIR}/src/data/caliptra-versions.json" "${TEMP_DIR}/Caliptra.md"
md2json --section repositories "$@" --output "${DIR}/src/data/caliptra-repositories.json" "${TEMP_DIR}/Caliptra.md"
