# OME-Zarr with pycromanager

## Background

[MM-OME-Zarr-Storage](https://github.com/micro-manager/MM-OME-Zarr-Storage) is a Java library
for streaming images to OME-Zarr v0.5 / Zarr v3. It has no Micro-Manager Storage adapter yet
(documented as "trivial to write" but not implemented).

## Recommended approach — Python-side hook

Use a pycromanager image hook to collect tiles, then write with `zarr` + `ome-zarr`.

Install:
```bash
pip install zarr ome-zarr
```

```python
import zarr
import numpy as np
from ome_zarr.io import parse_url
from ome_zarr.writer import write_image
from pycromanager import Acquisition

frames = []

def image_saved_hook(image, metadata):
    frames.append(image)

with Acquisition(image_saved_fn=image_saved_hook, show_display=False) as acq:
    acq.acquire(events)

stack = np.stack(frames)   # shape: (n_tiles, y, x)

store = parse_url("/path/to/overview.ome.zarr", mode="w").store
root = zarr.group(store)
write_image(
    image=stack,
    group=root,
    axes="tyx",
    coordinate_transformations=[
        [{"type": "scale", "scale": [1.0, 0.36, 0.36]}]   # PCO pixel size µm
    ],
)
```

## Notes

- This buffers all tiles in RAM before writing — fine for overview mosaics.
- For streaming write (tile-by-tile, no RAM buffer), direct Java bridge to
  `OMEZarrStorage` via `pycromanager.JavaObject` is possible but more involved.
- Output is readable by napari (`napari-ome-zarr`), Fiji (via MoBIE), and zarr-python v3.
