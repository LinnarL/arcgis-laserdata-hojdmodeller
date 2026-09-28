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
never has to open the point cloud.

## Requirements

- ArcGIS Pro 3.x, Basic licence is enough. Developed and tested on 3.6 with Python 3.13.
- No extra packages. `pdal`, `numpy` and `certifi` ship with the default `arcgispro-py3`
  environment.
- A consumer key and secret from Lantmäteriet's API portal with **both**:
  - the API `STAC-hojd`, and
  - an order of [Laserdata Nedladdning, skog](https://geotorget.lantmateriet.se/geodataprodukter/laserdata-nedladdning-skog-api)
    on Geotorget (Beställning tab).

  With only the first, the tool fails with HTTP 403 and says so.

## Install

1. Clone or download this repo.
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
| Punktfiler (LAZ/LAS) | off | Checkbox. Can be the only choice, then only one tile at a time is held in memory |
| Mapp för punktfiler | - | Enabled when Punktfiler is ticked. One file per tile: `<prefix>_<tile>.laz` or `.las`. Only the area's bounding box, not whole tiles, all classes including noise |
| Format för punktfiler | LAZ | LAZ is 5-7 times smaller but cannot be opened in Pro on a Basic licence. LAS can be added straight to a map |
| Utdata-arbetsyta för raster | project geodatabase | Geodatabase or folder, needed when a raster is ticked. In a folder the rasters are GeoTIFF |
| Namnprefix | `laser` | Outputs are `<prefix>_dsm`, `<prefix>_dtm`, `<prefix>_hojdskillnad`. Existing ones are overwritten, with a warning in the dialog |
| Cellstorlek (m) | 1 | Under Avancerat |
| Största tillåtna yta (km²) | 10 | Under Avancerat. All points are held in memory while rasters are built, about 100-150 MB per km² of bounding box |

Every parameter has a tooltip in the dialog. The text lives in `TOOLTIPS` in the `.pyt`, which
writes it to `LaserdataSkog.HojdmodellerFranLaserdata.pyt.xml` when the toolbox loads.

## Output

The rasters are clipped to the polygon, snapped to whole multiples of the cell size, in
SWEREF 99 TM + RH 2000, and added to the active map.

Each raster gets item metadata (Catalog, View Metadata): title with capture dates, a table of
the source tiles with scanning area, capture period, flying height, nominal point density and
last processing date, the processing method and point counts, credits to Lantmäteriet and a
link to the terms of use.

## Progress

The run is split into numbered steps shown in the progress bar and the messages. Tiles are read
one at a time with `Ruta k av n` and an estimate of the time left, based on the point count
Lantmäteriet publishes per tile. Building the DSM and DTM are single PDAL calls with no internal
progress. The message names the step and the number of points instead.

## How each product is made

The rasters use only the position (X, Y, Z) and the classification of each point. Intensity,
return number and the other attributes are kept in the point files but not used.

Points are read tile by tile inside the area's bounding box plus a 20 m margin, so cells at the
edge have full support. Classes 7 (low noise) and 18 (high noise) are removed before any raster
is built. All rasters share one grid and are clipped to the polygon or extent at the end.

| Product | Points used | Cell value |
|---|---|---|
| DSM | All classes except noise, all returns | Highest point within cell size x √2 of the cell centre (1.41 m at 1 m cells). A single high point therefore also raises the four adjacent cells, which slightly widens crowns. Cells with no point that close are filled by inverse distance weighting from cells up to 3 cells away. Larger gaps stay NoData, typically open water, which returns no pulses |
| DTM | Class 2 (mark) only | Ground points are triangulated into a TIN, and each cell gets the TIN's height at the cell centre. No gaps: where ground points are missing, under dense canopy, buildings or water, the height is linearly interpolated across |
| Höjdskillnad | The DSM and DTM | DSM minus DTM per cell. Negative values, where the highest point lies below the ground surface, are measurement noise and set to 0. NoData where the DSM is NoData |
| Punktfiler | Everything read | Written unchanged, one file per tile: all classes including noise, all attributes. Cut to the bounding box plus margin, not to the polygon |

The same description is in each checkbox's tooltip and in each raster's metadata.

## About the data

- Density is 1-2 points per m², with roughly one ground point per m² in open forest. 1 m cells
  are a good default. Finer cells leave the DSM sparse.
- Classes used: 2 (mark) for the DTM, everything except 7 and 18 (noise) for the DSM.
- The DSM is NoData where no laser returns exist, typically open water. The DTM interpolates
  across such areas.
- If Lantmäteriet has scanned a tile more than once, the newest scan is used. When an area spans
  tiles scanned on different dates, the tool warns that the height difference may show a seam.
  Neighbouring tiles can differ by weeks or seasons, and leaf-off scans give lower and sparser
  deciduous crowns.
- Coverage is about 75 % of Sweden. The mountains are not included.
