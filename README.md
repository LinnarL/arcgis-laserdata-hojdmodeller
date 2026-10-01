# Laserdata Skog

ArcGIS Pro Python toolbox that builds height rasters for an area of interest from
Lantmäteriet's Laserdata Nedladdning, skog, and optionally saves the points as LAZ or LAS.
Each product has its own checkbox:

- **DSM** (ytmodell): highest point near each cell centre.
- **DTM** (markmodell): ground points triangulated and rasterised, so it has no gaps.
- **Höjdskillnad**: DSM minus DTM, in practice vegetation and building height. Negative values
  are set to 0.

Only the points inside the area's bounding box are read, straight from Lantmäteriet's
cloud-optimised files. Whole 10 x 10 km tiles (about 1 GB each) are never downloaded, and ArcGIS
never has to open the point cloud. The area is processed in blocks that fit a memory budget, several
at a time, so its size is limited by disk space and time rather than RAM.

## Requirements

- ArcGIS Pro 3.x, Basic licence is enough. Developed and tested on 3.6 with Python 3.13.
- No extra packages. `pdal`, `numpy`, `certifi` and GDAL's Python bindings ship with the default
  `arcgispro-py3` environment.
- A consumer key and secret from Lantmäteriet's API portal with **both**:
  - the API `STAC-hojd`, and
  - an order of [Laserdata Nedladdning, skog](https://geotorget.lantmateriet.se/geodataprodukter/laserdata-nedladdning-skog-api)
    on Geotorget (Beställning tab).

  With only the first, the tool fails with HTTP 403 and says so.

## Install

1. Clone or download this repo. Keep `LaserdataSkog.pyt` and `laserdata_worker.py` in the same
   folder; the toolbox runs the worker file in separate processes.
2. In ArcGIS Pro: Catalog, Toolboxes, Add Toolbox, select `LaserdataSkog.pyt`.
3. Open Lantmäteriet Laserdata Skog, Höjdmodeller från Laserdata Skog.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| Avgränsa området med | Polygoner i ett lager | Choose between a polygon layer and an extent |
| Intresseområde (polygoner) | - | Polygon layer in any coordinate system. All features, or the selection, are merged, and the rasters are clipped to the shapes |
| Utbredning | - | Rectangle: current display extent, a layer's extent, a drawn rectangle or typed coordinates. Typed coordinates are read in the active map's coordinate system; the log says which one was used |
| Consumer key / Consumer secret | - | Under Inloggning. The secret is a hidden field |
| DSM (ytmodell) | on | Checkbox |
| DTM (markmodell) | on | Checkbox |
| Höjdskillnad (DSM - DTM) | on | Checkbox. DSM and DTM are always computed for it, but only saved if also ticked |
| Punktfiler (LAZ/LAS) | off | Checkbox. Can be the only choice |
| Mapp för punktfiler | - | Enabled when Punktfiler is ticked. See "Point files" below |
| Format för punktfiler | LAZ | LAZ is 5-7 times smaller but cannot be opened in Pro on a Basic licence. LAS can be added straight to a map |
| Utdata-arbetsyta för raster | project geodatabase | Geodatabase or folder, needed when a raster is ticked. In a folder the rasters are GeoTIFF |
| Namnprefix | `laser` | Outputs are `<prefix>_dsm`, `<prefix>_dtm`, `<prefix>_hojdskillnad`. Existing ones are overwritten, with a warning in the dialog |
| Cellstorlek (m) | 1 | Under Avancerat |
| Minnesbudget (GB) | empty (automatic) | Under Avancerat. Roughly how much memory the tool may use on top of ArcGIS Pro. Sets the block size. Manual: at least 0.5 GB |
| Block samtidigt | empty (automatic) | Under Avancerat. Blocks processed at the same time, each in its own process. The memory budget is shared between them. Manual: 1 to 32 |

Every parameter has a tooltip in the dialog. The text lives in `TOOLTIPS` in the `.pyt`, which
writes it to `LaserdataSkog.HojdmodellerFranLaserdata.pyt.xml` when the toolbox loads.

## Large areas

The bounding box is split into square blocks sized so that each fits the memory budget divided by
the number of parallel blocks, using the point density Lantmäteriet publishes per tile. Blocks are
also made small enough that there are at least as many as parallel processes, but not under
250 m. Blocks
that do not touch the polygon are skipped, unless point files are requested. Each block is read
with a margin, so neighbouring blocks meet without a seam, and the blocks are mosaicked into the
final rasters at the end.

Measured on a 2021 scan near Uppsala (about 2.3 million points per km²):

| Area | Budget | Parallel blocks | Time | Peak memory |
|---|---|---|---|---|
| 9 km² | 2 GB | 1 | 72 s | 2.0 GB |
| 9 km² | 2 GB | 4 | 38 s | 1.7 GB |
| 25 km² | 4 GB | 4 | 75 s | 3.3 GB |

Both memory and parallel blocks are automatic by default, decided on the machine that runs the
tool when the run starts: half of the free RAM, leaving at least 2 GB for Pro and Windows, and one
process per logical processor thread minus one, at most 16 and at most one per 0.75 GB of the
budget. The log states what was chosen, and a typed process count that does not fit the budget is
reduced. A worker needs about 135 bytes per point read plus about 120 MB (measured; the tool plans
with 170 bytes and 150 MB). On a 16-thread machine with 34 GB free, 4 km² became 16 blocks of
516 m, 15 at a time. Gains beyond 4 parallel blocks have not been measured against Lantmäteriet's
server, hence the cap of 16. With point files there is one file per block and tile, so more
processes give more, smaller files.

Reading mostly waits on the network, which is why parallel blocks help. Each block costs about 1 s
extra to open the remote files, so a very small budget, which gives many small blocks, is slower.
Output size grows with area: at 1 m cells each raster is about 4 MB per km², and the same again
is needed temporarily in the scratch folder. The tool warns above 200 km².

Block processing gives the same DSM and the same point files as one single block (verified cell
by cell and point by point). The DTM can differ in a very small number of scattered cells: 0.02 %
of cells by at most 13 cm in the test. Where four ground points lie on one circle, which is
common with coordinates stored to the centimetre, two triangulations are equally valid and the
one chosen depends on the whole point set. It is not a seam; the cells are spread across the
area.

## Point files

One file per block and tile: `<prefix>_<tile>_<row>_<column>.laz` or `.las`, rows counted from
the north. The files contain the source data unchanged: every point inside the area's bounding
box, all classes including noise, all attributes, with Lantmäteriet's own point format, scale,
offset and header. Every point is in exactly one file, also where blocks meet. Verified against
a single-block run: same points, every field equal, no duplicates.

## Output

The rasters are clipped to the polygon, snapped to whole multiples of the cell size, in
SWEREF 99 TM + RH 2000, and added to the active map.

Each raster gets item metadata (Catalog, View Metadata): title with capture dates, a table of
the source tiles with scanning area, capture period, flying height, nominal point density and
last processing date, the processing method and point counts, credits to Lantmäteriet and a
link to the terms of use.

## Progress

The run is split into numbered steps shown in the progress bar and the messages. Blocks report
`Block k av n` with an estimate of the time left, based on the point count Lantmäteriet publishes
per tile. The height difference, mosaic and clip steps report per block or per raster.

## How each product is made

The rasters use only the position (X, Y, Z) and the classification of each point. Intensity,
return number and the other attributes are kept in the point files but not used.

Classes 7 (low noise) and 18 (high noise) are removed before any raster is built. All rasters
share one grid and are clipped to the polygon or extent at the end.

| Product | Points used | Cell value |
|---|---|---|
| DSM | All classes except noise, all returns | Highest point within cell size x √2 of the cell centre (1.41 m at 1 m cells). A single high point therefore also raises the four adjacent cells, which slightly widens crowns. Cells with no point that close are filled by inverse distance weighting from cells up to 3 cells away. Larger gaps stay NoData, typically open water, which returns no pulses |
| DTM | Class 2 (mark) only | Ground points are triangulated into a TIN, and each cell gets the TIN's height at the cell centre. No gaps: where ground points are missing, under dense canopy, buildings or water, the height is linearly interpolated across |
| Höjdskillnad | The DSM and DTM | DSM minus DTM per cell. Negative values, where the highest point lies below the ground surface, are measurement noise and set to 0. NoData where the DSM is NoData |
| Punktfiler | Everything read | Written unchanged, see "Point files" |

The same description is in each checkbox's tooltip and in each raster's metadata.

## About the data

- Density is 1-2 points per m², with roughly one ground point per m² in open forest. 1 m cells
  are a good default. Finer cells leave the DSM sparse.
- The DSM is NoData where no laser returns exist, typically open water. The DTM interpolates
  across such areas.
- If Lantmäteriet has scanned a tile more than once, the newest scan is used. When an area spans
  tiles scanned on different dates, the tool warns that the height difference may show a seam.
  Neighbouring tiles can differ by weeks or seasons, and leaf-off scans give lower and sparser
  deciduous crowns.
- Coverage is about 75 % of Sweden. The mountains are not included.
