# scapecontrol

**Automated OPM / SCAPE acquisition pipeline for a 40× light-sheet microscope, driven from Python via [pycromanager](https://github.com/micro-manager/pycro-manager).**

This repository automates a multi-step microscopy workflow on a 40× SCAPE
(swept confocally-aligned planar excitation) oblique-plane light-sheet
microscope built around an ASI Tiger controller, dual Photometrics Kinetix22
cameras, and a PCO widefield camera. The pipeline:

1. acquires a **tiled brightfield overview** of the sample (PCO camera + LED),
2. **detects organoids** of interest and builds a stage position list,
3. runs **volumetric OPM acquisitions** at each position via MicroManager's
   LightSheetManager (LSM) plugin.

All stages are notebook-driven so each step can be reviewed before the next.

---

## Table of contents

- [Hardware](#hardware)
- [Software setup](#software-setup)
- [LightSheetManager plugin](#lightsheetmanager-plugin)
- [Repository layout](#repository-layout)
- [The pipeline, step by step](#the-pipeline-step-by-step)
- [Coordinate conventions](#coordinate-conventions)
- [Python module reference (`opm_acquisition.py`)](#python-module-reference-opm_acquisitionpy)
- [Position-file formats](#position-file-formats)

---

## Hardware

| Component | Device name in MicroManager |
|---|---|
| XY stage (ASI Tiger) | `XYStage:XY:31` |
| Z stage | `ZStage:Z:32` |
| Piezo stage | `PiezoStage:P:34` |
| Filter slider (widefield ↔ light-sheet mirror) | `FilterSlider:S:35` |
| Emission filter wheel — camera 1 | `FilterWheel:1:38` |
| Emission filter wheel — camera 2 | `FilterWheel:0:38` |
| PLogic (laser gate / channel selector) | `PLogic:E:36` |
| White LED (brightfield) | `LED:L:37:4` |
| OPM camera 1 (Kinetix22) | `Kinetix22-1` |
| OPM camera 2 (Kinetix22) | `Kinetix22-2` |
| Widefield / brightfield camera (PCO) | `Widefield` |

**MicroManager config file:** `mm2p0_40xSCAPE_v23TEST_withoutPCO_newLSM.cfg`

**Camera / imaging modes** (MicroManager config-group presets):

| Preset | Effect |
|---|---|
| `Camera → Widefield` | PCO camera active, filter slider to widefield position |
| `Camera → Multi` | both Kinetix cameras active, filter slider to light-sheet position |

**Pixel sizes:**

- PCO widefield: **0.36 µm/px**, 1024×1024 px → FOV ≈ 369 µm
- Kinetix (OPM): handled by LightSheetManager

---

## Software setup

```bash
pip install pycromanager>=1.0.2 numpy tifffile scikit-image napari magicgui matplotlib
```

You also need `lsm_pycromanager.py` (the LightSheetManager Python bridge, included
here) in the same directory as `opm_acquisition.py`. It is sourced from the
[LightSheetManager repo](https://github.com/micro-manager/LightSheetManager).

**Runtime requirements:**

1. MicroManager 2.0 running with the correct config loaded.
2. In MicroManager, enable the ZMQ server: **Tools ▸ Options ▸ "Run server on port 4827"**
   so pycromanager can connect.
3. Start Python from an environment that has the dependencies above.

---

## LightSheetManager plugin

The OPM half of this pipeline is built **on top of the
[LightSheetManager](https://github.com/micro-manager/LightSheetManager) (LSM)
MicroManager plugin** — LSM owns the volumetric light-sheet acquisition itself
(galvo scan, slice timing, camera triggering, laser gating). This repository drives
LSM rather than reimplementing it: `opm_acquisition.py` moves the stage and calls
LSM through the `lsm_pycromanager.py` bridge.

Because the pipeline was developed against LSM, the plugin version matters.

### Verified working stack

| Component | Version |
|---|---|
| MMCore | 12.5.0 (Device API 75, Module API 10) |
| LightSheetManager plugin | **0.7.4** |
| PVCAM device adapter | **1.3.76** |
| PVCAM runtime (Teledyne SDK) | 3.10.2 |
| ASI Tiger firmware | TigerComm 3.42, stages 3.41, scanner 3.36, PLogic 3.45 |

> **Note on the PVCAM device adapter.** The adapter version that ships with a given
> MicroManager nightly is *not* always compatible with dual-camera triggered
> acquisition. Adapter **1.3.76** is the version verified working here. Newer
> adapters bundled with some nightlies have failed on the simultaneous
> two-camera path. If dual-camera acquisition misbehaves after a MicroManager
> update, check the adapter version first — it is reported per camera as the
> `PVCAM Adapter Version` property.

### Managing plugin versions

LSM plugin jars live in the MicroManager install under `mmplugins\`. Only one may be
active at a time. The convention used on this system is to keep every released jar in
that folder and **enable exactly one by its file extension**:

```
LightSheetManager-0.7.4.jar     <- active (loaded by MicroManager)
LightSheetManager-0.7.3.jars    <- disabled (trailing "s")
LightSheetManager-0.6.5.jars    <- disabled
```

To switch versions, rename the active jar to `.jars` and the desired one to `.jar`,
then restart MicroManager. Keeping the full history makes it quick to bisect a
regression back to a specific plugin release.

### LSM device mapping

LSM resolves logical roles to physical devices through the `LightSheetDeviceManager`
device. The mapping used here:

| LSM role | Device |
|---|---|
| `MicroscopeGeometry` | `SCAPE` |
| `LightSheetType` | `Static` |
| `ImagingCamera1` | `Kinetix22-2` |
| `ImagingCamera2` | `Kinetix22-1` |
| `SimultaneousCameras` | `2` |
| `TriggerCamera` / `TriggerLaser` | `PLogic:E:36` |
| `IllumSlice` | `Scanner:AB:33` |
| `ImagingFocus` | `PiezoStage:P:34` |
| `SampleXY` / `SampleZ` | `XYStage:XY:31` / `ZStage:Z:32` |

During an acquisition LSM takes over the trigger chain itself: it switches both Kinetix
cameras to `Edge Trigger` and sets the PLogic `OutputChannel` to match the selected
channel preset. Any trigger-mode or laser-channel state set beforehand from Python is
therefore overridden once LSM starts — configure those through LSM, not around it.

---

## Repository layout

| File | Type | Purpose |
|---|---|---|
| [`opm_acquisition.py`](opm_acquisition.py) | module | Core pipeline: hardware constants, mode switching, brightfield overview, position-list management, OPM acquisition. Imported by the notebooks. |
| [`brightfield_overview.ipynb`](brightfield_overview.ipynb) | notebook | Acquire a tiled brightfield overview and stitch it into `mosaic.tif`. |
| [`organoid_picker.ipynb`](organoid_picker.ipynb) | notebook | Detect organoids in the mosaic, review/edit interactively in napari, export a stage position list. |
| [`microwells.ipynb`](microwells.ipynb) | notebook | Generate a microwell grid from 3 marker wells, and/or merge two position lists. |
| [`lsm_pycromanager.py`](lsm_pycromanager.py) | module | LightSheetManager Python bridge (from the LSM repo). |
| [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md) | doc | Long-form reference with every function and constant documented. |

---

## The pipeline, step by step

```
1. brightfield_overview.ipynb
   Park the stage at the well centre → run cells →
   mosaic.tif + tile_positions.json

2. organoid_picker.ipynb
   Load the mosaic → auto-detect organoids → review/edit points in napari →
   export a .pos file

3. (optional) microwells.ipynb
   Merge organoid positions with a microwell grid, or build a grid from markers

4. Load the .pos file into the MicroManager Stage Position List

5. LightSheetManager → start a multi-position OPM acquisition
```

### 1. Brightfield overview — `brightfield_overview.ipynb`

- Connects to MicroManager, switches to widefield (PCO) mode.
- Edit the grid settings (`RANGE_X_UM`, `RANGE_Y_UM`, `OVERLAP`) — default covers a
  6900 µm × 6900 µm area (e.g. an ibidi 8-well slide well).
- Snake-scans the grid, saving each tile plus a `tile_positions.json` that records the
  exact stage XY of every tile.
- Stitches the tiles into `mosaic.tif`.

**Outputs** (in `{OUTPUT_DIR}/brightfield_overview/`):

- `tile_r###_c###.tif` — individual raw tiles
- `tile_positions.json` — per-tile stage X/Y, pixel size, Z focus
- `mosaic.tif` — stitched mosaic (per-tile normalised, uint16)

### 2. Organoid picking — `organoid_picker.ipynb`

- Loads `mosaic.tif` + `tile_positions.json` and builds a `px_to_stage()` mapping.
- **Auto-detection**: downscale → Gaussian smooth → Otsu threshold → `regionprops`,
  filtered by diameter and circularity. A half-pixel correction is applied when mapping
  the downscaled centroid back to full resolution.
- **napari review**: opens the mosaic with detected points drawn as size-matched circles,
  plus an interactive re-detection panel (sliders for smoothing, threshold × Otsu,
  min/max diameter, circularity). Select-mode is active — press **A** to add points,
  **Delete** to remove them.
- **Export**: converts the reviewed points to stage coordinates via `px_to_stage()` and
  writes a MicroManager 2.0 `.pos` file.

### 3. Microwell tools — `microwells.ipynb`

Two independent utilities:

- **Generate a grid from 3 marker wells** — mark 3 wells (origin, end of first row,
  start of second row); the notebook derives the row/column vectors and generates a
  snake-scan grid over the whole plate.
- **Merge two position lists** — concatenate two `.pos` files, re-label positions
  contiguously, and optionally apply a global Z offset.

### 4–6. OPM acquisition

Load the exported `.pos` file into the MicroManager Stage Position List, then drive the
acquisition either from the LightSheetManager GUI or from `opm_acquisition.py`'s
`run_opm_at_positions()`.

---

## Coordinate conventions

| Axis | Stage direction | Mosaic direction |
|---|---|---|
| X | +X moves the stage right → the image shifts **left** | column increases left→right (no flip) |
| Y | +Y moves the stage up → the image shifts **up** | row 0 = top = **highest Y** (flip) |

Mosaic assembly therefore flips rows but not columns:

```python
mr = (max_row - r) * h   # row 0 of the mosaic = highest Y
mc = c * w               # col 0 of the mosaic = lowest X
```

`tile_positions.json` stores the exact stage XY of every tile, so pixel→stage conversion
does not depend on how the mosaic image was stitched or displayed.

---

## Python module reference (`opm_acquisition.py`)

| Function | Purpose |
|---|---|
| `switch_to_widefield(core)` | Switch to the PCO camera, enable the LED, disable lasers. Waits ~15 s for the filter slider. |
| `switch_to_lightsheet(core)` | Switch to the Kinetix cameras and restore binning/trigger for Live preview. LSM takes over trigger control when it starts an acquisition. |
| `acquire_brightfield_overview(core, save_dir, grid=…)` | Snake-scan the grid, save tiles + `tile_positions.json`. |
| `_snake_grid(grid, center_x, center_y)` | Generate snake-scan tile positions. Column index always maps to the same physical X regardless of scan direction. |
| `positions_from_grid(grid)` | Generate a position at every tile centre. |
| `push_positions_to_mm(studio, positions)` | Write positions into the MicroManager Stage Position List. |
| `load_positions_from_mm(studio)` | Read the current MicroManager Stage Position List. |
| `configure_lsm(lsm)` | Set LSM acquisition parameters (slices, exposure). |
| `run_opm_at_positions(lsm, core, positions)` | Move to each position and trigger an LSM acquisition. |
| `main()` | Full pipeline: overview → position list → OPM acquisition. |

**Key constants** (edit at the top of the file before running):

- `OUTPUT_DIR` — where acquisitions are written
- `BF_PIXEL_SIZE_UM = 0.36`, `BF_CAMERA_PX = 1024` — PCO camera geometry
- `BF_LED_INTENSITY = 20`, `BF_EXPOSURE_MS = 20.0` — brightfield exposure
- `OVERVIEW_GRID` — default tile grid (±660 µm, 330 µm step, ~10% overlap)
- `LSM_SLICES_PER_VIEW`, `LSM_EXPOSURE_MS` — OPM volume parameters

---

## Position-file formats

The pipeline reads and writes the **MicroManager 2.0 Property Map** JSON position format:

```json
{
  "encoding": "UTF-8",
  "format": "Micro-Manager Property Map",
  "major_version": 2, "minor_version": 0,
  "map": {
    "StagePositions": { "type": "PROPERTY_MAP", "array": [ ... ] }
  }
}
```

Each entry carries `DevicePositions` with a `ZStage` (one double) and an `XYStage`
(two doubles: X, Y). The older MM "VERSION 3" `.pos` format is supported for reading only
(the 3-marker microwell input); everything is written back as MM 2.0.

---

See [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md) for the full long-form reference.
