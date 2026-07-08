"""
OPM Automated Acquisition Pipeline — SCAPE/ASI LightSheetManager
=================================================================
Microscope: 40x SCAPE with ASI Tiger, dual Kinetix22 cameras + PCO widefield
Config:     mm2p0_40xSCAPE_v23TEST_withoutPCO_newLSM.cfg

Workflow:
  1. Switch to widefield (LED) mode and acquire a tiled brightfield overview
  2. Auto-generate a position list from the tile grid (or let you edit it in MM)
  3. Switch to LightSheetManager and run OPM acquisition at every position

Requirements:
    pip install pycromanager>=1.0.2 numpy

Place lsm_pycromanager.py (from LightSheetManager repo) in the same directory:
    https://github.com/micro-manager/LightSheetManager/blob/main/src/python/lsm_pycromanager.py
"""

import time
import logging
from pathlib import Path

import numpy as np
from pycromanager import Acquisition, Studio, Core
from lsm_pycromanager import LightSheetManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("OPM_Pipeline")

# ---------------------------------------------------------------------------
# Hardware device names (from your .cfg)
# ---------------------------------------------------------------------------
XY_STAGE          = "XYStage:XY:31"
Z_STAGE           = "ZStage:Z:32"
PIEZO_STAGE       = "PiezoStage:P:34"
FILTER_SLIDER     = "FilterSlider:S:35"   # positions: "Widefield", "LightSheet", "Alignment Laser"
FILTER_WHEEL_CAM1 = "FilterWheel:1:38"    # emission filter — camera 1 (Kinetix22-1)
FILTER_WHEEL_CAM2 = "FilterWheel:0:38"    # emission filter — camera 2 (Kinetix22-2)
PLOGIC            = "PLogic:E:36"         # laser shutter / channel selector
LED_WHITE         = "LED:L:37:4"          # white LED for brightfield/widefield
CAMERA_1          = "Kinetix22-1"         # primary OPM camera (ImagingCamera2 in LSM)
CAMERA_2          = "Kinetix22-2"         # secondary OPM camera (ImagingCamera1 in LSM)
CAMERA_WIDEFIELD  = "Widefield"           # PCO camera for brightfield/widefield overview

# ---------------------------------------------------------------------------
# Acquisition configuration — edit before running
# ---------------------------------------------------------------------------
OUTPUT_DIR = Path(r"C:\Users\aifadmin\Desktop\TEST")

# ---- Brightfield overview --------------------------------------------------
BF_EXPOSURE_MS   = 20.0
BF_LED_INTENSITY = 20   # % (0–100)

# Widefield (PCO) camera — known pixel size
BF_PIXEL_SIZE_UM = 0.36   # µm/px
BF_CAMERA_PX     = 1024   # pixels (square chip)

# Tile grid in µm — centered on current stage position at acquisition time
# Widefield (PCO) camera: 1024×1024 px at 0.36 µm/px → FOV ≈ 369 µm; 330 µm step = ~10% overlap
OVERVIEW_GRID = dict(
    x_start = -660.0,   # µm relative to center (2 tiles each side)
    x_end   =  660.0,
    y_start = -660.0,
    y_end   =  660.0,
    step_um =  330.0,
)

# Emission filter for brightfield overview (usually empty position)
BF_FILTER_WHEEL_CAM1 = "1-empty"
BF_FILTER_WHEEL_CAM2 = "1-empty"

# ---- LightSheetManager OPM settings ---------------------------------------
LSM_SLICES_PER_VIEW = 50
LSM_EXPOSURE_MS     = 20.0

# If True: push draft positions to MM Stage Position List and wait for you
# to review/edit them in the GUI before starting OPM acquisitions.
MANUAL_POSITION_REVIEW = True


# ---------------------------------------------------------------------------
# Helper — hardware state switching
# ---------------------------------------------------------------------------

def switch_to_widefield(core: Core) -> None:
    """Configure hardware for LED widefield / brightfield imaging (PCO camera)."""
    log.info("Switching to widefield (LED) mode...")
    # Apply the 'Widefield' camera config preset (sets filter slider, camera, shutter)
    core.set_config("Camera", "Widefield")
    # Disable all laser lines
    core.set_property(PLOGIC, "OutputChannel", "none of outputs 1-7")
    # Emission filters to open/empty positions
    core.set_property(FILTER_WHEEL_CAM1, "Label", BF_FILTER_WHEEL_CAM1)
    core.set_property(FILTER_WHEEL_CAM2, "Label", BF_FILTER_WHEEL_CAM2)
    # Set LED intensity and exposure
    core.set_property(LED_WHITE, "LED Intensity(%)", str(BF_LED_INTENSITY))
    core.set_exposure(BF_EXPOSURE_MS)
    time.sleep(15)  # filter slider (mirror) takes ~15s to move
    for dev in [FILTER_WHEEL_CAM1, FILTER_WHEEL_CAM2]:
        try:
            core.wait_for_device(dev)
        except Exception:
            pass
    log.info("Widefield mode ready.")


def switch_to_lightsheet(core: Core) -> None:
    """Restore hardware to light-sheet mode (LSM will take over from here)."""
    log.info("Switching to light-sheet mode...")
    # Apply the 'Multi' camera config preset (sets filter slider to LightSheet, both cameras)
    core.set_config("Camera", "Multi")
    # Restore binning to 1×1 for OPM
    core.set_property(CAMERA_1, "Binning", "1x1")
    core.set_property(CAMERA_2, "Binning", "1x1")
    # Reset trigger mode so Live preview works after this call
    # (LSM will switch to external trigger when it starts an acquisition)
    core.set_property(CAMERA_1, "TriggerMode", "Internal Trigger")
    core.set_property(CAMERA_2, "TriggerMode", "Internal Trigger")
    core.set_property(PLOGIC, "OutputChannel", "output 3 only")
    time.sleep(15)  # filter slider (mirror) takes ~15s to move
    for dev in [CAMERA_1, CAMERA_2]:
        try:
            core.wait_for_device(dev)
        except Exception:
            pass
    log.info("Light-sheet mode ready.")


# ---------------------------------------------------------------------------
# Step 1 — Brightfield tiled overview
# ---------------------------------------------------------------------------

def _snake_grid(grid: dict, center_x: float = 0.0, center_y: float = 0.0) -> list[dict]:
    """Return XY positions for a snake-scan tile grid centered on (center_x, center_y).
    Column indices are always tied to the same physical X position regardless of scan direction."""
    xs = np.arange(grid["x_start"], grid["x_end"] + grid["step_um"], grid["step_um"]) + center_x
    ys = np.arange(grid["y_start"], grid["y_end"] + grid["step_um"], grid["step_um"]) + center_y
    col_indices = list(range(len(xs)))
    events = []
    for row_i, y in enumerate(ys):
        # Snake: alternate physical direction, but col index always maps to xs[col_i]
        scan_order = col_indices if row_i % 2 == 0 else col_indices[::-1]
        for col_i in scan_order:
            events.append({
                "axes": {"row": int(row_i), "col": int(col_i)},
                "x": float(xs[col_i]),
                "y": float(y),
                "exposure": BF_EXPOSURE_MS,
            })
    return events


def acquire_brightfield_overview(core: Core, save_dir: Path,
                                  center_x: float = None, center_y: float = None,
                                  grid: dict = None) -> list[dict]:
    """Tile-scan the sample with the Widefield (PCO) camera and save each tile as a TIFF.
    Grid is centered on (center_x, center_y); defaults to current stage position.
    Pass a custom grid dict to override the default OVERVIEW_GRID."""
    import tifffile

    switch_to_widefield(core)

    if center_x is None:
        center_x = core.get_x_position()
    if center_y is None:
        center_y = core.get_y_position()
    log.info(f"Grid center: x={center_x:.1f} um  y={center_y:.1f} um")

    overview_dir = save_dir / "brightfield_overview"
    overview_dir.mkdir(parents=True, exist_ok=True)

    active_grid = grid if grid is not None else OVERVIEW_GRID
    events = _snake_grid(active_grid, center_x=center_x, center_y=center_y)
    log.info(f"Brightfield overview: {len(events)} tiles → {overview_dir}")

    z_focus = core.get_position(Z_STAGE)

    tiles = []
    for ev in events:
        core.set_xy_position(ev["x"], ev["y"])
        core.wait_for_device(XY_STAGE)
        time.sleep(0.1)

        core.snap_image()
        tagged = core.get_tagged_image()

        import numpy as np
        img = np.reshape(tagged.pix, [tagged.tags["Height"], tagged.tags["Width"]])

        fname = overview_dir / f"tile_r{ev['axes']['row']:03d}_c{ev['axes']['col']:03d}.tif"
        tifffile.imwrite(str(fname), img)
        tiles.append({**ev, "path": str(fname)})
        log.info(f"  tile r{ev['axes']['row']} c{ev['axes']['col']}  x={ev['x']:.0f} y={ev['y']:.0f}")

    # Save tile→stage mapping so downstream tools (e.g. napari point picking)
    # can convert mosaic pixel coordinates back to stage coordinates exactly,
    # independent of how the mosaic image itself gets stitched/displayed.
    import json
    meta = {
        "pixel_size_um": BF_PIXEL_SIZE_UM,
        "camera_px": BF_CAMERA_PX,
        "z_focus_um": z_focus,
        "tiles": [
            {"row": t["axes"]["row"], "col": t["axes"]["col"], "x": t["x"], "y": t["y"]}
            for t in tiles
        ],
    }
    with open(overview_dir / "tile_positions.json", "w") as f:
        json.dump(meta, f, indent=2)

    log.info(f"Overview complete — {len(tiles)} tiles saved to {overview_dir}")
    return tiles


# ---------------------------------------------------------------------------
# Step 2 — Position list management
# ---------------------------------------------------------------------------

def positions_from_grid(grid: dict) -> list[dict]:
    """
    Generate a position at every tile centre.
    Replace or extend this with image-analysis logic to select only
    positions that contain sample (e.g. threshold the overview mosaic).
    """
    xs = np.arange(grid["x_start"], grid["x_end"] + grid["step_um"], grid["step_um"])
    ys = np.arange(grid["y_start"], grid["y_end"] + grid["step_um"], grid["step_um"])
    return [
        {"x": float(x), "y": float(y), "label": f"Pos_{i:03d}"}
        for i, (x, y) in enumerate((x, y) for y in ys for x in xs)
    ]


def push_positions_to_mm(studio: Studio, positions: list[dict]) -> None:
    """Write positions into the MicroManager Stage Position List."""
    pm = studio.positions()
    pos_list = pm.get_position_list()
    pos_list.clear_all_positions()
    for pos in positions:
        msp = pos_list.make_multi_stage_position(pos["label"])
        msp.set_2d_position(XY_STAGE, pos["x"], pos["y"])
        pos_list.add_position(msp)
    pm.set_position_list(pos_list)
    log.info(f"Pushed {len(positions)} positions to MicroManager position list.")


def load_positions_from_mm(studio: Studio) -> list[dict]:
    """Read whatever is currently in the MicroManager Stage Position List."""
    pm = studio.positions()
    pos_list = pm.get_position_list()
    positions = []
    for i in range(pos_list.get_number_of_positions()):
        msp = pos_list.get_position(i)
        positions.append({
            "label": msp.get_label(),
            "x": msp.get_x(XY_STAGE),
            "y": msp.get_y(XY_STAGE),
        })
    log.info(f"Loaded {len(positions)} positions from MicroManager.")
    return positions


# ---------------------------------------------------------------------------
# Step 3 — OPM acquisition via LightSheetManager
# ---------------------------------------------------------------------------

def configure_lsm(lsm) -> None:
    """Set OPM acquisition parameters via the LightSheetManager builder API."""
    builder = lsm.acquisitions().settings().copy_builder()
    builder.volume_builder().slices_per_view(LSM_SLICES_PER_VIEW)
    builder.slice_builder().sample_exposure(LSM_EXPOSURE_MS)
    settings = builder.build()
    log.info("LSM settings:\n" + settings.to_string())
    lsm.acquisitions().update_settings(settings)


def _wait_for_lsm(lsm, timeout_s: int = 900, poll_s: float = 2.0) -> None:
    """Block until LightSheetManager reports the acquisition has finished."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if not lsm.acquisitions().is_running():
                return
        except Exception:
            pass  # transient bridge error — keep polling
        time.sleep(poll_s)
    raise TimeoutError(f"LSM acquisition did not finish within {timeout_s} s.")


def run_opm_at_positions(lsm, core: Core, positions: list[dict]) -> None:
    """Move the XY stage to each position and trigger an OPM acquisition."""
    for pos in positions:
        log.info(f"-> {pos['label']}  x={pos['x']:.1f} um  y={pos['y']:.1f} um")

        core.set_xy_position(pos["x"], pos["y"])
        core.wait_for_device(XY_STAGE)
        time.sleep(0.2)   # brief settle after move

        lsm.acquisitions().request_run()
        _wait_for_lsm(lsm)
        log.info(f"  done: {pos['label']}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    studio = Studio()
    core   = Core()

    # Phase 1: widefield overview
    acquire_brightfield_overview(core, OUTPUT_DIR)

    # Phase 2: build / review position list
    positions = positions_from_grid(OVERVIEW_GRID)
    push_positions_to_mm(studio, positions)

    if MANUAL_POSITION_REVIEW:
        input(
            "\n[ACTION REQUIRED]\n"
            "  - Open 'Plugins -> Stage Position List' in MicroManager\n"
            "  - Edit / add / remove positions as needed\n"
            "  - Press Enter here when ready to start OPM acquisitions..."
        )
        positions = load_positions_from_mm(studio)

    if not positions:
        log.error("No positions in list — aborting.")
        return

    log.info(f"Will acquire {len(positions)} OPM positions.")

    # Phase 3: OPM acquisition
    switch_to_lightsheet(core)

    with LightSheetManager() as lsm:
        configure_lsm(lsm)
        run_opm_at_positions(lsm, core, positions)

    log.info("Pipeline complete.")


if __name__ == "__main__":
    main()
