![EasySewer](https://raw.githubusercontent.com/yiranji/easysewer/master/assets/icon.png)

# EasySewer
> An urban drainage modeling toolkit

EasySewer 2.0 uses `from easysewer import Model` for its typed model API.
See the [delivery scope and follow-up work](https://github.com/yiranji/easysewer/blob/master/docs/2.0-delivery.md),
[first runnable example](https://github.com/yiranji/easysewer/blob/master/docs/2.0-first-run.md) and [model guide](https://github.com/yiranji/easysewer/blob/master/docs/2.0-model.md).
EasySewer 2.0 does not support 1.x APIs or proprietary data migration.
Use the 2.0 model/runtime APIs and standard SWMM INP or current Model JSON.

## Installation

```console
python -m pip install --upgrade easysewer==2.1.0
python -c "from easysewer import Model; print(Model())"
```

EasySewer 2.1.0 requires Python 3.10 or later; CPython 3.10–3.14 is tested.
It provides platform-specific native wheels for:

| Platform | Native runtime baseline |
| --- | --- |
| Windows x86-64 | 64-bit x64 Python on compatible Windows x64 |
| Linux x86-64 | glibc 2.33 or later |
| Linux ARM64 (aarch64) | glibc 2.28 or later |
| Apple Silicon macOS (arm64) | macOS 14 deployment target; release CI on macOS 15 and 26 |

These wheels contain standard SWMM 5.2.4, the native OUT reader and their
non-system native dependencies. A compiler or separate system OpenMP installation
is not needed to use a supported wheel. Use a native 64-bit Python interpreter
and a current pip so it can select the matching platform wheel.
The [macOS 14.8.9 execution record](docs/2.0-testing.md#observed-macos-14-baseline)
binds an earlier exact artifact, not the final 2.1.0 wheel or initial macOS 14.0.

Version 2.1.0 also fixes the Windows virtual-environment worker launch failure
reported in [issue #5](https://github.com/yiranji/easysewer/issues/5), while keeping
worker identity and lifetime checks. Upgrade from 2.0.1 to obtain these changes;
the immutable 2.0.1 files do not gain ARM support or the diagnostic command.

Check the installed runtime before a simulation:

```console
python -m easysewer doctor
python -m easysewer doctor --json
python -m easysewer doctor --smoke-test --timeout 30
```

The default check loads libraries only in disposable processes; it does not solve
a model, write simulation files or install anything. The optional smoke test
solves a minimal model in a temporary directory. Readiness is not a guarantee of
numerical or scientific validity. FlexiblePonding remains an experimental,
scientifically unvalidated example of a replaceable backend, preserved on x86-64
and optional on ARM. A requested backend is never silently replaced.

Intel macOS, Windows ARM64 Python, musl Linux, 32-bit Python and `universal2`
are not supported native targets. Native checkpoint archives bind exact runtime
bytes and ABI; keep the original runtime for older archives. Cross-architecture
checkpoint continuation is not supported.

Modeling, conversion and pure result reading do not require a native solver.
See the [2.1.0 release notes](docs/2.0-delivery.md#210-release-notes), [installation, supported ABIs and doctor exit codes](docs/2.0-installation.md)
and [native build and qualification instructions](docs/2.0-testing.md).

See the [documentation index](https://github.com/yiranji/easysewer/blob/master/docs/README.md) for installation, the Quick Start notebook,
modeling, runtime, results, maintenance and known limits. The [editable network example](https://github.com/yiranji/easysewer/blob/master/examples/v2_edit_model.py)
demonstrates reference updates and transaction rollback.

## Introduction
Urban drainage models are essential tools for studying urban flooding issues, capable of simulating flooding processes under various rainfall scenarios. With advancements in computing technology, applying these models to large-scale areas for hydraulic calculation has become feasible. However, drainage systems comprise numerous elements, and the modeling process often involves collecting, converting, and integrating data from raw sources such as statistical tables, design drawings, or GIS systems. Consequently, this process is often tedious and time-consuming.

While modern tools like SWMM provide graphical user interfaces (GUIs) that allow users to intuitively view and modify drainage models, these manual operations—relying heavily on mouse and keyboard inputs—are inefficient, repetitive, and dependent on human experience. They are typically suitable only for modeling small areas. To efficiently and reliably model large-scale urban drainage systems, using scripts and APIs is an effective approach, enabling rapid data conversion and automated processing. Therefore, in practical urban drainage modeling, API-based scripted modeling is often a necessary choice.

## Motivation
As the most widely used urban drainage model, SWMM provides a C-based API. However, its native interface only offers functions for computational control (such as reading data during simulation) and does not support adding or modifying the model's structure (e.g., adding a new pipe via the API). This limits the automation and flexibility of modeling.

To address this, many scholars and engineers have developed packages based on different programming languages to extend SWMM's functionality, including `swmmr` (R), `MatSWMM` (MATLAB), and Python-based packages like `swmm_api` and `pyswmm`. Compared to other languages, Python demonstrates significant advantages:
1.  **Open Source**: Unlike commercial software like MATLAB, Python lowers the barrier to entry.
2.  **GIS Integration**: Mainstream GIS software (ArcGIS and QGIS) uses Python as a scripting language, facilitating interaction between drainage models and GIS systems.
3.  **Ecosystem**: With its popularity in scientific computing and AI, Python offers a vast community and rich open-source libraries.

However, current Python-based SWMM packages still have limitations:
*   `pyswmm` only encapsulates the basic functions of the SWMM native API. While it can modify existing parameters, it cannot dynamically edit the model structure.
*   `swmm_api` supports topological modifications (nodes and pipes) but lacks support for the SWMM native API. This means model calculation and result parsing rely on third-party tools (e.g., dependent on a local pre-compiled SWMM engine for calculation and `swmmtoolbox` for result parsing).

These limitations lead to complex toolchain dependencies and increased requirements for user experience, restricting the efficiency of urban drainage system modeling.

## Features
To overcome these challenges, this project develops **EasySewer**, an open-source Python package. It integrates the following functionalities to provide a comprehensive solution for SWMM modeling:

*   **Integrated Modeling**: Supports dynamic editing of model structures.
*   **Calculation**: Built-in capabilities to run simulations.
*   **Result Parsing**: Native support for parsing simulation results.

EasySewer aims to provide a robust supplementary solution to existing SWMM modeling tools, streamlining the workflow from data to results.
