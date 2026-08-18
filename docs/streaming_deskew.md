# Streaming deskew — deskew volumes during acquisition

## Concept

NDTiff supports concurrent read-while-write. This means you can open the dataset
while LSM is still writing and deskew each volume as soon as all its z-planes land
on disk — without waiting for the full acquisition to finish.

Current flow in `omezarr_lightsheet.ipynb`:
```
acquire → wait for writer to finish → deskew all volumes
```

Pipelined flow:
```
acquire (running in background)
  └─ poll for complete volumes → deskew → save   (while acquisition continues)
```

## Helper functions already in omezarr_lightsheet.ipynb

- `volume_ready(ds, ax, p, t, c, n_z)` — True once all z-planes of one (position, time, channel) are on disk
- `read_volume(ds, ax, p, t, c, n_z, h, w, dtype)` — reads a complete volume into a numpy array
- `Deskewer.run(image3D, flip)` — GPU deskew via pyclesperanto
- `axes_of(ds)` — returns current axis dict from the open dataset

## Implementation

Replace the "wait for writer → deskew" block with this loop.
Run acquisition setup cells as usual, then use this instead of the current acquire+deskew cell:

```python
from ndtiff import Dataset

POLL_S    = 0.5
TIMEOUT_S = 7200

save_root = Path(SAVE_DIRECTORY)
before = {p.name for p in save_root.glob(f'{SAVE_NAME_PREFIX}*') if p.is_dir()}

t0 = time.time()
lsm.acquisitions().request_run()
print('Acquisition started — waiting for dataset folder...')

# --- wait for the ND-TIFF folder to appear (same as current notebook) ---
run_dir = None
while time.time() - t0 < 120:
    new = {p.name for p in save_root.glob(f'{SAVE_NAME_PREFIX}*') if p.is_dir()} - before
    cand = [save_root / n for n in new if (save_root / n / 'NDTiff.index').exists()]
    if cand:
        run_dir = max(cand, key=lambda p: p.stat().st_mtime)
        break
    time.sleep(POLL_S)
if run_dir is None:
    raise RuntimeError('No dataset appeared within 120 s — check LSM save mode.')
print('Dataset:', run_dir.name)

# --- open dataset once and keep it open during the whole acquisition ---
ds  = Dataset(str(run_dir))
dsk = None       # Deskewer initialised on first complete volume
done = set()     # (p, t, c) tuples already deskewed

while time.time() - t0 < TIMEOUT_S:
    ax        = axes_of(ds)
    positions = ax.get('position', [0])
    timepoints = ax.get('time', [0])
    chans     = ax.get('channel', [0])

    for p in positions:
        for t in timepoints:
            for c in chans:
                key = (p, t, c)
                if key in done:
                    continue
                if not volume_ready(ds, ax, p, t, c, EXPECTED_NZ):
                    continue

                # --- first volume: initialise the Deskewer ---
                if dsk is None:
                    c0    = coords_for(ax, p, t, c, 0)
                    probe = ds.read_image(**c0)
                    mdimg = ds.read_metadata(**c0)
                    vxy   = VOXEL_XY_OVERRIDE or mdimg.get('PixelSizeUm')
                    if not vxy:
                        raise ValueError('No PixelSizeUm in metadata — set VOXEL_XY_OVERRIDE')
                    dsk   = Deskewer(EXPECTED_NZ, probe.shape[0], probe.shape[1], vxy, voxel_z_um)
                    cams  = camera_map(ds, ax, p, t)
                    print(f'  Deskewer ready  vxy={dsk.vx} um  vz={dsk.vz:.4f} um')
                    print(f'  channel -> camera: {cams}')

                # --- deskew ---
                flip = bool(dual_camera and cams.get(c) == FLIP_CAMERA)
                img  = read_volume(ds, ax, p, t, c, dsk.n_z, dsk.h, dsk.w, ds.dtype)
                tv   = time.time()
                vol, mz, _ = dsk.run(img, flip)
                print(f'  deskewed  p={p} t={t} c={c}  {vol.shape}  '
                      f'max={int(vol.max())}  {time.time()-tv:.2f}s')

                # --- save one OME-Zarr per (timepoint, channel) ---
                out_path = OMEZARR_DIR / f'{OMEZARR_NAME}_t{t:04d}_c{c}.ome.zarr'
                vx = float(dsk.vx if DESKEW_OUT_VOXEL_UM is None else DESKEW_OUT_VOXEL_UM)
                image = OMEZarrImage(
                    data=vol.copy(),
                    axes=['z', 'y', 'x'],
                    scale={'z': vx, 'y': vx, 'x': vx},
                    axes_units={'z': 'micrometer', 'y': 'micrometer', 'x': 'micrometer'},
                )
                OMEZarrMultiscale(image, chunks=(1, 256, 256)).to_ome_zarr(out_path)
                done.add(key)
                print(f'  saved -> {out_path.name}  ({len(done)} volumes total)')

    # --- stop when acquisition is done and all volumes are deskewed ---
    n_expected = (len(positions) *
                  (NUM_TIME_POINTS if USE_TIME_POINTS else 1) *
                  len(chans))
    if not lsm.acquisitions().is_running() and len(done) >= n_expected:
        break

    time.sleep(POLL_S)

ds.close()
print(f'\nDone — {len(done)} volumes deskewed and saved in {time.time()-t0:.1f}s')
```

## Notes

### lsm.acquisitions().is_running()
Confirm this method exists first:
```python
[m for m in dir(lsm.acquisitions()) if 'run' in m.lower()]
```
If it is missing, replace the stop condition with an index-size stability check
(same pattern as the current "wait for writer" block).

### GPU speed vs acquisition speed
If GPU deskew is slower than the acquisition interval, volumes queue up and the
loop falls behind. Check by comparing deskew time printed above against your
`TIME_INTERVAL_S`. If it is too slow, move deskew to a background thread:

```python
import threading, queue

deskew_queue = queue.Queue()

def deskew_worker():
    while True:
        item = deskew_queue.get()
        if item is None:
            break
        key, img, flip, out_path = item
        vol, mz, _ = dsk.run(img, flip)
        OMEZarrMultiscale(...).to_ome_zarr(out_path)
        done.add(key)
        deskew_queue.task_done()

worker = threading.Thread(target=deskew_worker, daemon=True)
worker.start()

# In the polling loop, replace the deskew block with:
deskew_queue.put((key, img.copy(), flip, out_path))

# After the loop:
deskew_queue.put(None)
worker.join()
```

### Output layout
Each volume is saved as a separate `.ome.zarr` folder:
```
ls_deskewed_t0000_c0.ome.zarr
ls_deskewed_t0001_c0.ome.zarr
...
```
To combine all timepoints into one `(T, Z, Y, X)` OME-Zarr after acquisition,
collect volumes into a list and wrap with `OMEZarrImage(axes=['t','z','y','x'], ...)`.
