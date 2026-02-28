# EasySewer
> An urban drainage modeling toolkit

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
