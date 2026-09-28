# Building Clio

Clio is a CMake project with three parts:

| Target | What it is | Needs |
|---|---|---|
| `clio_core` | Static C++ library: Perforce access (P4API), `clio:` identifiers, pins, resolution | C++17 compiler, P4API, OpenSSL 3 |
| `deda.clio._core` | Python extension (nanobind, stable ABI for CPython ≥ 3.13) | Python 3.13, nanobind |
| `clioUsd` | USD asset resolver plugin for the `clio:` scheme | A USD build (26.08, or 25.08 or later) with C++ headers |

Targets today: **Python 3.13** and standalone **USD 26.08** (USD 25.08 or
later also builds). DCCs are not targeted yet.

## 1. Dependencies

### Perforce C++ API (P4API)

Download the API for your platform and OpenSSL major version from
<https://ftp.perforce.com/perforce/> (for example
`r26.1/bin.linux26x86_64/p4api-glibc2.12-openssl3.tgz`), check it against
the `SHA256SUMS` file in the same folder, and extract it:

```bash
B=https://ftp.perforce.com/perforce/r26.1/bin.linux26x86_64
curl -fO $B/SHA256SUMS -fO $B/p4api-glibc2.12-openssl3.tgz -fO $B/p4d
grep -E '\*(p4api-glibc2.12-openssl3.tgz|p4d)$' SHA256SUMS | sha256sum -c -
mkdir p4api && tar -xzf p4api-glibc2.12-openssl3.tgz -C p4api --strip-components=1
chmod +x p4d
export CLIO_P4API_ROOT=$PWD/p4api
```

`p4d` is only used by the tests, to run a throwaway server.

OpenSSL 3 development libraries must be installed (for example
`libssl-dev` on Debian/Ubuntu). By default Clio links OpenSSL **statically**
and hides its symbols (`CLIO_OPENSSL_STATIC=ON`), so it cannot clash with
the OpenSSL that Python or a host application loads.

### USD (only for the resolver plugin)

The plugin must be built against the same USD build it will run in.
`usd-core` wheels from PyPI have no C++ headers, so build OpenUSD from
source. A minimal build with Python 3.13 bindings:

```bash
git clone --depth 1 --branch v26.08 https://github.com/PixarAnimationStudios/OpenUSD.git
python3.13 OpenUSD/build_scripts/build_usd.py \
    --no-imaging --no-examples --no-tutorials --no-tools --no-docs --no-materialx \
    --python --build-variant release /opt/usd-26.08
export PXR_ROOT=/opt/usd-26.08
```

Use `--branch v25.08` (or later) for another supported version. Build the
plugin once per USD version.

## 2. Build with CMake (development)

```bash
python3.13 -m pip install nanobind
cmake --preset dev                  # core + Python extension + C++ tests
cmake --build --preset dev
ctest --preset dev

cmake --preset dev-usd              # also the USD plugin (uses $PXR_ROOT)
cmake --build --preset dev-usd
```

Options:

| Option | Default | Meaning |
|---|---|---|
| `CLIO_P4API_ROOT` | `$CLIO_P4API_ROOT` | Extracted P4API directory |
| `CLIO_BUILD_PYTHON` | `ON` | Build `deda.clio._core` |
| `CLIO_BUILD_USD` | `OFF` | Build `clioUsd` (set `pxr_DIR` to the USD install) |
| `CLIO_BUILD_TESTS` | `ON` when top level | Build the C++ tests |
| `CLIO_OPENSSL_STATIC` | `ON` | Link OpenSSL statically, with hidden symbols |
| `CLIO_P4D_EXECUTABLE` | found on `PATH` | `p4d` for integration tests |

## 3. Build and install the Python package

```bash
export CLIO_P4API_ROOT=/path/to/p4api
python3.13 -m pip install .                          # core + Python only
CLIO_BUILD_USD=ON PXR_ROOT=/opt/usd-26.08 python3.13 -m pip install .       # with the USD plugin
```

The wheel is tagged `cp313-abi3`. With the plugin it contains
`deda/clio/usd/plugin/clioUsd.so` and
`deda/clio/usd/plugin/clioUsd/resources/plugInfo.json`.

## 4. Test

```bash
python3.13 -m pip install pytest
export CLIO_TEST_P4D=/path/to/p4d
python3.13 -m pytest    # USD tests need pxr importable: PYTHONPATH=$PXR_ROOT/lib/python3.13/site-packages
```

Tests that need a server are skipped when `CLIO_TEST_P4D` is not set, and
the USD tests are skipped when `pxr` or the plugin is missing. Set
`CLIO_TEST_USD_PLUGIN` to a `.../clioUsd/resources` folder to test a plugin
from a CMake build tree.

## 5. Using the resolver

```bash
export PXR_PLUGINPATH_NAME=$(python3.13 -c "from deda.clio import usd; print(usd.plugin_path())")
```

```python
from pxr import Ar, Usd

settings = "depot=//imagine/main;root=/work/imagine;client=sam_imagine;pin=latest"
ctx = Ar.GetResolver().CreateContextFromString("clio", settings)
stage = Usd.Stage.Open("clio:/shots/sq010/sh0100/shot.usda", ctx)
```

Or set `CLIO_RESOLVER_CONTEXT` to the same settings string to give every
stage a default context. The settings keys are documented in
`cpp/clio_core/include/clio/core/settings.hpp` and in design doc §10.3.
