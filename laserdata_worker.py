# -*- coding: utf-8 -*-
"""
laserdata_worker.py

PDAL-delen av LaserdataSkog.pyt: läser ett block ur Lantmäteriets COPC-filer,
sparar punktfiler och skriver blockets DSM och DTM som GeoTIFF.

Körs som egen process, en per samtidigt block, och importerar aldrig arcpy.
Flera PDAL-pipelines i trådar i samma process som arcpy kraschade processen
(access violation i arcpy efter blockfasen, uppmätt i Pro 3.6; båda laddar
Esris gdal_e.dll). I egna processer delar PDAL inget med arcpy.

Protokoll: en uppgift per rad som JSON på stdin, ett svar per rad på stdout
med prefixet RESULT_PREFIX (annan utskrift från PDAL ignoreras av anroparen).
Processen avslutas när stdin stängs.

Uppgift:  {"block": {...}, "token": "...", "need_dsm": bool, "need_dtm": bool,
           "raw_folder": str|null, "raw_ext": ".laz", "prefix": "...", "workdir": "..."}
Svar:     {"ok": true, "result": {...}} eller {"ok": false, "error": "traceback"}

LaserdataSkog.pyt importerar konstanterna och process_block härifrån, så att
de bara finns på ett ställe.
"""

import json
import os
import sys
import traceback

import numpy as np
from numpy.lib.recfunctions import repack_fields

RESULT_PREFIX = "@@LDS_RESULT "

NODATA = -9999.0
CLASS_GROUND = 2
NOISE_CLASSES = (7, 18)
POINTS_DIM = ["X", "Y", "Z", "Classification"]

# Luckor i DSM mindre än så här många celler fylls med IDW från grannar.
DSM_WINDOW = 3
# Extra celler runt varje blocks DSM: lucköppningen plus writers.gdal:s radie
# (cellstorlek x rot 2, alltså högst 2 celler), så att kantcellerna får samma
# grannar som i en körning utan block. Kräver READ_MARGIN_M i .pyt >= detta.
DSM_PAD_CELLS = DSM_WINDOW + 3

SUFFIX_DSM = "dsm"
SUFFIX_DTM = "dtm"
SUFFIX_DIFF = "hojdskillnad"


def import_pdal():
    # Pro:s PDAL-bygge har curl utan CA-certifikat. Utan detta misslyckas varje
    # HTTPS-anslutning och arbiter försöker om i all oändlighet.
    try:
        import certifi
        os.environ.setdefault("ARBITER_CA_INFO", certifi.where())
    except ImportError:
        pass
    import pdal
    return pdal


def write_dsm(pdal, arrays, grid, path):
    """
    DSM för blockets rutnät. Rastret beräknas med DSM_PAD_CELLS extra celler
    runt blocket och klipps sedan tillbaka: lucköppningen (window_size) fyller
    en tom cell från grannceller, och vid en blockkant finns grannarna annars
    bara på ena sidan. Uppmätt: utan kanten skilde sig 70 celler inom 2,5 m
    från blockgränserna mot en körning i ett enda block, med kanten inga.
    """
    from osgeo import gdal
    gdal.UseExceptions()
    cell = grid["resolution"]
    pad = dict(grid, origin_x=grid["origin_x"] - DSM_PAD_CELLS * cell,
               origin_y=grid["origin_y"] - DSM_PAD_CELLS * cell,
               width=grid["width"] + 2 * DSM_PAD_CELLS,
               height=grid["height"] + 2 * DSM_PAD_CELLS)
    padded = path[:-4] + "_pad.tif"
    # writers.gdal lägger flera arrayer i samma rutnät, så rutorna behöver inte
    # slås ihop först (det skulle dubbla minnesåtgången en stund).
    stage = {"type": "writers.gdal", "filename": padded, "output_type": "max",
             "window_size": DSM_WINDOW, "data_type": "float32", "nodata": NODATA}
    stage.update(pad)
    pdal.Pipeline(json.dumps([stage]), arrays=arrays).execute()
    x0, y0 = grid["origin_x"], grid["origin_y"]
    x1, y1 = x0 + grid["width"] * cell, y0 + grid["height"] * cell
    gdal.Translate(path, padded, projWin=[x0, y1, x1, y0])
    gdal.GetDriverByName("GTiff").Delete(padded)


def write_dtm(pdal, ground, grid, path):
    """
    TIN av markpunkterna, rastrerad. En enda array: filters.delaunay
    trianguerar varje array för sig, och separata rutor skulle ge en lucka
    längs rutgränsen.

    Blockindelningen ändrar DTM:en obetydligt: uppmätt 0,02 % av cellerna, som
    mest 13 cm, spridda över ytan och inte samlade vid blockgränserna. Orsaken
    är att fyra punkter på samma cirkel (vanligt med koordinater i hela cm) har
    två lika giltiga Delaunay-trianguleringar, och vilken som väljs beror på
    hela punktmängden, inte på punkternas ordning. Att ta bort punkter 500 m
    bort ändrade enstaka celler med några mm.
    """
    face = {"type": "filters.faceraster"}
    face.update(grid)
    stages = [
        {"type": "filters.delaunay"},
        face,
        {"type": "writers.raster", "filename": path, "data_type": "float32", "nodata": NODATA},
    ]
    pdal.Pipeline(json.dumps(stages), arrays=[ground]).execute()


def process_block(pdal, task):
    """
    Läs och bearbeta ett block.

    Punktfiler skrivs i samma pipeline som läsningen, med forward: all så att
    Lantmäteriets header (punktformat, skala, offset, system-id m.m.) följer med
    och punkterna är oförändrade. where-villkoret är halvöppet, så en punkt på
    en blockgräns hamnar i exakt en fil trots att blocken läses med marginal.
    """
    blk = task["block"]
    raw_folder, raw_ext, prefix = task["raw_folder"], task["raw_ext"], task["prefix"]
    need_dsm, need_dtm = task["need_dsm"], task["need_dtm"]
    where = "X >= {x0!r} && X < {x1!r} && Y >= {y0!r} && Y < {y1!r}".format(**blk)

    arrays, raw_files = [], []
    n_inside = n_ground = 0
    for rd in blk["reads"]:
        stages = [{"type": "readers.copc", "bounds": rd["bounds"],
                   "filename": {"path": rd["href"],
                                "headers": {"Authorization": "Bearer " + task["token"]}}}]
        raw_path = None
        if raw_folder:
            raw_path = os.path.join(raw_folder, "{}_{}_{:03d}_{:03d}{}".format(
                prefix, rd["id"], blk["row"], blk["col"], raw_ext)).replace("\\", "/")
            stages.append({"type": "writers.las", "filename": raw_path, "forward": "all",
                           "extra_dims": "all", "where": where})
        pipe = pdal.Pipeline(json.dumps(stages))
        pipe.execute()
        pts = pipe.arrays
        pts = pts[0] if len(pts) == 1 else np.concatenate(pts)
        del pipe

        inside = ((pts["X"] >= blk["x0"]) & (pts["X"] < blk["x1"])
                  & (pts["Y"] >= blk["y0"]) & (pts["Y"] < blk["y1"]))
        if raw_path:
            if inside.any():
                raw_files.append([raw_path, int(inside.sum())])
            elif os.path.exists(raw_path):
                os.remove(raw_path)

        if need_dsm or need_dtm:
            keep = ~np.isin(pts["Classification"], NOISE_CLASSES)
            n_inside += int((inside & keep).sum())
            n_ground += int((inside & keep & (pts["Classification"] == CLASS_GROUND)).sum())
            # Bara det rastren använder, så att blocket tar mindre minne.
            arrays.append(repack_fields(pts[keep][POINTS_DIM]))
        else:
            n_inside += int(inside.sum())
        del pts, inside

    tag = "{:03d}_{:03d}".format(blk["row"], blk["col"])
    workdir = task["workdir"]
    result = {"points": n_inside, "ground": n_ground, "raw": raw_files}
    if need_dsm and any(len(a) for a in arrays):
        result[SUFFIX_DSM] = os.path.join(workdir, "dsm_{}.tif".format(tag)).replace("\\", "/")
        write_dsm(pdal, arrays, blk["grid"], result[SUFFIX_DSM])
    if need_dtm:
        ground = [a[a["Classification"] == CLASS_GROUND] for a in arrays]
        ground = np.concatenate(ground) if ground else np.zeros(0)
        del arrays
        if len(ground) >= 3:
            result[SUFFIX_DTM] = os.path.join(workdir, "dtm_{}.tif".format(tag)).replace("\\", "/")
            write_dtm(pdal, ground, blk["grid"], result[SUFFIX_DTM])
    return result


def main():
    # Sökvägar kan innehålla å, ä och ö; läs och skriv alltid UTF-8.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    pdal = import_pdal()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            out = {"ok": True, "result": process_block(pdal, json.loads(line))}
        except Exception:
            out = {"ok": False, "error": traceback.format_exc()}
        sys.stdout.write(RESULT_PREFIX + json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
