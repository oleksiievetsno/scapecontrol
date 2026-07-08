# scapecontrol — Project Summary
*Automated OPM/SCAPE acquisition pipeline using pycromanager*

---

## Overview

This project automates a multi-step microscopy workflow on a 40× SCAPE (swept confocally-aligned planar excitation) light-sheet microscope. The pipeline acquires a tiled brightfield overview, detects organoids of interest, generates a position list, and runs volumetric OPM acquisitions at each position via LightSheetManager.

---

## Hardware

| Component | Device name in MM |
|---|---|
| XY stage (ASI Tiger) | `XYStage:XY:31` |
| Z stage | `ZStage:Z:32` |
| Piezo stage | `PiezoStage:P:34` |
| Filter slider | `FilterSlider:S:35` |
| Emission filter wheel cam 1 | `FilterWheel:1:38` |
| Emission filter wheel cam 2 | `FilterWheel:0:38` |
| PLogic (laser gate/channel selector) | `PLogic:E:36` |
| White LED (brightfield) | `LED:L:37:4` |
| OPM camera 1 (Kinetix22) | `Kinetix22-1` |
| OPM camera 2 (Kinetix22) | `Kinetix22-2` |
| Widefield/brightfield camera (PCO) | `Widefield` |

**MicroManager config file:** `mm2p0_40xSCAPE_v23TEST_withoutPCO_newLSM.cfg`

**Camera modes:**
- `Camera / Widefield` config group preset → PCO camera active, filter slider to widefield position
- `Camera / Multi` config group preset → both Kinetix cameras active, filter slider to lightsheet position

**Pixel sizes:**
- PCO widefield: 0.36 µm/px, 1024×1024 px → FOV ≈ 369 µm
- Kinetix (OPM): handled by LightSheetManager

**Stage directions:**
- +X stage → image shifts LEFT
- +Y stage → image shifts UP
- Mosaic assembly: row index flips (higher Y = top of image), column index does not flip

---

## Software dependencies

```
pycromanager >= 1.0.2
numpy
tifffile
scikit-image
napari
magicgui
matplotlib
```

Also requires `lsm_pycromanager.py` from the LightSheetManager repo placed in the same directory as `opm_acquisition.py`.

---

## Files

### `opm_acquisition.py` — Core pipeline module

All hardware constants and pipeline functions. Imported by notebooks.

**Key functions:**

| Function | Purpose |
|---|---|
| `switch_to_widefield(core)` | Switches to PCO camera, enables LED, disables lasers. Waits 15 s for filter slider. |
| `switch_to_lightsheet(core)` | Switches to Kinetix cameras, restores binning and trigger mode for live preview. |
| `_snake_grid(grid, center_x, center_y)` | Generates snake-scan tile positions. Col index always maps to the same X regardless of scan direction (bug fix: avoids mirrored tiles on odd rows). |
| `acquire_brightfield_overview(core, save_dir, grid)` | Runs tiled brightfield scan, saves individual TIFFs + `tile_positions.json` with stage coordinates per tile. |
| `positions_from_grid(grid)` | Generates a position at every tile centre (for simple grid acquisition). |
| `push_positions_to_mm(studio, positions)` | Writes positions to MM Stage Position List. |
| `load_positions_from_mm(studio)` | Reads current MM Stage Position List. |
| `configure_lsm(lsm)` | Sets LSM acquisition parameters (slices, exposure). |
| `run_opm_at_positions(lsm, core, positions)` | Moves stage to each position and triggers LSM acquisition. |
| `main()` | Full pipeline: overview → position list → OPM acquisition. |

**Important constants:**
- `BF_PIXEL_SIZE_UM = 0.36` — PCO pixel size
- `BF_CAMERA_PX = 1024` — PCO chip size
- `BF_LED_INTENSITY = 20` — LED power (%)
- `BF_EXPOSURE_MS = 20.0`
- `OVERVIEW_GRID` — default 5×5 tile grid (±660 µm, 330 µm step, ~10% overlap)

---

### `brightfield_overview.ipynb` — Tiled brightfield acquisition

**Cells:**
1. Connect to MM via pycromanager (`Core`, `Studio`)
2. Switch to widefield mode, preview one frame (histogram + image)
3. **Grid settings** — edit `RANGE_X_UM`, `RANGE_Y_UM`, `OVERLAP` (default: 6900 µm × 6900 µm for ibidi 8-well slide)
4. Preview tile grid on a plot (stage path visualisation)
5. Acquire — calls `acquire_brightfield_overview()`, saves tiles + `tile_positions.json`
6. Display mosaic — stitches tiles into a single image, saves `mosaic.tif` (uint16 TIFF)

**Output files** (in `{OUTPUT_DIR}/brightfield_overview/`):
- `tile_r###_c###.tif` — individual raw tiles
- `tile_positions.json` — per-tile stage X/Y coordinates + pixel size + Z focus
- `mosaic.tif` — stitched mosaic (normalised per-tile, uint16)

**Mosaic assembly convention:**
```python
mr = (max_row - r) * h   # row 0 of mosaic = highest Y (rows flip)
mc = c * w               # col 0 of mosaic = lowest X (cols don't flip)
```

---

### `organoid_picker.ipynb` — Organoid detection and position export

**Cells:**
1. **Settings** — `OVERVIEW_DIR`, `POS_FILE_OUT`, `Z_OVERRIDE_UM`
2. **Load** — reads `mosaic.tif` + `tile_positions.json`, builds `col_to_x`, `row_to_y` lookup dicts and `px_to_stage()` function
3. **Auto-detection** (optional) — threshold + regionprops on downscaled mosaic:
   - Parameters: `DOWNSCALE=4`, `SMOOTHING_UM=50`, `INVERT=True`, `MIN_DIAMETER_UM=80`, `MAX_DIAMETER_UM=600`, `MIN_CIRCULARITY=0.5`
   - Half-pixel correction applied when mapping downscaled centroid back to full resolution
   - Shows matplotlib preview of detections
4. **napari** — opens mosaic with detected points as size-matched circles; interactive re-detection panel (sliders for smoothing, threshold ×Otsu, min/max diameter, circularity); select mode active for Delete; press A to add
5. **Export** — reads `points_layer.data`, converts via `px_to_stage()`, saves MM2.0 `.pos` file
6. **Preview** — matplotlib overlay of exported positions in physical stage coordinates

**`px_to_stage()` mapping logic:**
```python
# Determine which tile the pixel falls in
c = col_px // CAMERA_PX          # tile column index
r = max_row - row_px // CAMERA_PX # tile row index (flipped)
# Stage position = tile centre + within-tile offset
x = col_to_x[c] + (col_px % CAMERA_PX - CAMERA_PX/2) * PIXEL_SIZE_UM
y = row_to_y[r] - (row_px % CAMERA_PX - CAMERA_PX/2) * PIXEL_SIZE_UM
```

**Position file format:** MM2.0 Property Map JSON (not the old VERSION 3 format).

---

### `microwells.ipynb` — Position list tools

Two independent sections:

**Section 1 — Generate grid from 3 marker wells**
- Input: `.pos` file (old MM VERSION 3 format) with exactly 3 positions
  - Pos0 = origin (top-left well)
  - Pos1 = end of first row (top-right well)
  - Pos2 = start of second row (well below origin)
- Derives row and column vectors, generates snake-scan grid
- Settings: `LENGTH`, `WIDTH` (number of steps between wells)
  - Small plate: 22 × 25 steps
  - Large plate: 34 × 39 steps
- Output: MM2.0 `.pos` file

**Section 2 — Merge two position lists**
- Loads two MM2.0 `.pos` files
- Re-labels Pos indices to be contiguous
- Applies optional Z offset (`Z_OFFSET_UM`) to all positions
- Saves merged MM2.0 `.pos` file
- Plots both lists colour-coded

**MM2.0 position format:**
```json
{
  "encoding": "UTF-8",
  "format": "Micro-Manager Property Map",
  "major_version": 2, "minor_version": 0,
  "map": {
    "StagePositions": {
      "type": "PROPERTY_MAP",
      "array": [ ... ]
    }
  }
}
```
Each position entry contains `DevicePositions` with `ZStage` (single double) and `XYStage` (two doubles: X, Y).

---

### `laser_live_test.ipynb` — Live mode laser diagnostic

Diagnostic notebook for fixing laser triggering in MM Live mode.

**Background:** The LightSheetManager plugin sets Kinetix cameras to `External Trigger` mode. After LSM runs, MM Live mode with Kinetix cameras only flashes the laser once (the PLogic gate receives no more trigger pulses from the camera).

**Root cause:** PLogic gates the laser using camera trigger output pulses. In `External Trigger` mode, the camera waits for an external hardware trigger and doesn't self-generate pulses → no continuous gate signal → laser off. In `Internal Trigger` mode, the camera self-clocks and generates continuous trigger pulses → PLogic → laser stays on during Live.

**Fix (must run before each Live session after LSM use):**
```python
core.set_property('Kinetix22-1', 'TriggerMode', 'Internal Trigger')
core.set_property('Kinetix22-2', 'TriggerMode', 'Internal Trigger')
core.set_property('PLogic:E:36', 'OutputChannel', 'output 3 only')
core.set_auto_shutter(False)
core.snap_image(); _ = core.get_tagged_image()  # prime PLogic latch
core.set_shutter_open(True)
```

**Restore after Live (before using LSM):**
```python
core.set_shutter_open(False)
core.set_auto_shutter(True)
core.set_property('PLogic:E:36', 'OutputChannel', 'none of outputs 1-7')
```

**Known values:**
- Working laser channel: `'output 3 only'`
- Trigger mode string: `'Internal Trigger'` (not `'Internal'`)
- `get_device_property_names()` returns a Java `StrVector` — must not iterate directly; probe individual property names with `get_property()` / `try-except`

---

## Coordinate system notes

| Axis | Stage direction | Mosaic direction |
|---|---|---|
| X | +X moves stage right → image shifts LEFT | col increases left→right (no flip) |
| Y | +Y moves stage up → image shifts UP | row 0 = top = highest Y (flip) |

The mosaic coordinate flip is confirmed: `mr = (max_row - r) * h`.

`tile_positions.json` stores exact stage XY per tile, enabling accurate pixel→stage conversion without depending on the mosaic assembly method.

---

## Typical workflow

```
1. brightfield_overview.ipynb
   → park stage at well centre → run cells → mosaic.tif + tile_positions.json

2. organoid_picker.ipynb
   → load mosaic → auto-detect → review/edit in napari → export .pos file

3. (optional) microwells.ipynb
   → merge organoid positions with microwell grid

4. Load .pos file into MM Stage Position List

5. laser_live_test.ipynb  (if needed)
   → run "before Live" cell → check sample in Live mode → run "after Live" cell

6. LightSheetManager → start multi-position OPM acquisition
```

---

## Known issues / workarounds

| Issue | Workaround |
|---|---|
| MM Live mode laser blinks and goes off | Run "before Live" cell in `laser_live_test.ipynb` before each Live session |
| LSM requires auto-shutter ON; Live requires it OFF | Run restore cell after Live, fix cell before Live |
| `get_device_property_names()` returns non-iterable Java StrVector | Use `get_property()` with try/except to probe individual property names |
| Filter slider takes ~15 s to move | `switch_to_widefield()` / `switch_to_lightsheet()` include `time.sleep(15)` |
| Old MM VERSION 3 `.pos` format | Use `read_xyz_old()` to read, always write MM2.0 format |
