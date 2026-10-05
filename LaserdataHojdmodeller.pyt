# -*- coding: utf-8 -*-
"""
LaserdataHojdmodeller.pyt

Skapar tre höjdraster för ett intresseområde ur Lantmäteriets Laserdata
Nedladdning, skog: ytmodell (DSM), markmodell (DTM) och höjdskillnaden mellan
dem (DSM - DTM, i praktiken vegetationshöjd). Kan även spara de lästa punkterna
som LAZ eller LAS.

Datakälla
---------
STAC-katalog (öppen, ingen inloggning):
    https://api.lantmateriet.se/stac-hojd/v1
    Samling: dsm-skoglig-copc
    Sök:     POST /search  {"collections": [...], "bbox": [...], "limit": 100}
             Nästa sida via länken rel="next" (method + body).

Varje item är en ruta på 10 x 10 km i SWEREF 99 TM + RH 2000 (EPSG:5845),
med en asset "data" som pekar på en COPC-fil (.copc.laz, ~1 GB):
    https://dl1.lantmateriet.se/hojd/data/pointcloud/sls/<område>/m<id>.copc.laz
proj:bbox ger rutans hörn i SWEREF 99 TM, pc:count antalet punkter i rutan.

Nedladdning kräver OAuth2 (client credentials):
    POST https://apimanager.lantmateriet.se/oauth2/token
    Basic-auth med consumer key/secret, grant_type=client_credentials.
    Token gäller 3600 s. Nyckeln måste ha både API:et STAC-hojd och en
    beställning av Laserdata Nedladdning, skog, annars svarar dl1 med 403.

Hela rutor laddas aldrig ned. PDAL (ingår i ArcGIS Pro) läser COPC-filerna
direkt över HTTP och hämtar bara de delar av punktmolnet som ligger inom
intresseområdets utbredning. PDAL:s curl i Pro saknar CA-certifikat, så
ARBITER_CA_INFO pekas mot certifi innan pdal importeras - utan det fastnar
varje HTTPS-anrop i ett oändligt omförsök.

Området delas i block som var för sig ryms i minnesbudgeten (storleken räknas
från rutornas pc:count). Blocken bearbetas i egna processer
(laserdata_worker.py), flera åt gången, och sätts sedan ihop med
MosaicToNewRaster. Varje block läses med marginal så att grannblock möts utan
skarv. Förloppet visas per block med en uppskattning av återstående tid.

Varför processer och inte trådar: flera PDAL-pipelines i trådar i samma
process som arcpy kraschade processen (access violation i arcpy efter
blockfasen). Se laserdata_worker.py.

Klasser: 1 oklassad, 2 mark, 7 lågt brus, 18 högt brus. Brus tas bort före
rastren men sparas i punktfilerna. DSM = högsta punkt inom cellstorlek x rot 2
från cellens mitt (writers.gdal radius, uppmätt: en punkt sätter sin cell och
de fyra närmaste), luckor upp till DSM_WINDOW celler fylls med IDW från
grannceller. DTM = markpunkter trianguleras (TIN) och TIN:ens höjd i
cellmitten används, alltså utan luckor.

Verktygstips (parameterförklaringar) skrivs till
LaserdataHojdmodeller.HojdmodellerFranLaserdata.pyt.xml från TOOLTIPS nedan när
verktygslådan laddas, så att texten bara finns på ett ställe.

Krav: ArcGIS Pro 3.x. arcpy, numpy, pdal och certifi ingår i arcgispro-py3.
Ingen licensnivå utöver Basic behövs.
"""

import base64
import datetime
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

import importlib
import importlib.util
import queue
import subprocess
import sys

import arcpy
import numpy as np

# PDAL-delen ligger i laserdata_worker.py bredvid verktygslådan och körs i egna
# processer. Konstanterna delas därifrån, så att de bara finns på ett ställe.
# reload: Pro håller modulen i minnet mellan körningar och uppdateringar.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import laserdata_worker as _lw  # noqa: E402
importlib.reload(_lw)
from laserdata_worker import (  # noqa: E402
    CLASS_GROUND, DSM_PAD_CELLS, DSM_WINDOW, NODATA, NOISE_CLASSES, SUFFIX_DIFF, SUFFIX_DSM,
    SUFFIX_DTM)

# ── Konstanter ────────────────────────────────────────────────────────────────

STAC_SEARCH_URL = "https://api.lantmateriet.se/stac-hojd/v1/search"
TOKEN_URL = "https://apimanager.lantmateriet.se/oauth2/token"
COLLECTION = "dsm-skoglig-copc"
GEOTORGET_URL = "https://geotorget.lantmateriet.se/geodataprodukter/laserdata-nedladdning-skog-api"

USER_AGENT = "arcgis-laserdata-hojdmodeller/1.1"
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3

# Token gäller 3600 s. Hämta en ny innan nästa ruta om den är äldre än så här.
TOKEN_MAX_AGE_S = 50 * 60

SWEREF99TM_WKID = 3006
RH2000_WKID = 5613

# Varje block läses med denna marginal, så att TIN:en och DSM:ens radie och
# lucköppning har fullt underlag vid blockkanten och block möts utan skarv.
READ_MARGIN_M = 20.0

# Minnestopp per läst punkt i ett block: PDAL:s egen kopia och numpy-arrayen
# (51 byte var), den reducerade kopian för rastren (X, Y, Z, klass) och
# trianguleringen av markpunkterna. Uppmätt 133-143 byte för hela blocket med
# DSM och DTM (0,9-5,1 miljoner punkter); marginal för fler attribut och TIN.
PEAK_BYTES_PER_POINT = 170
# Fast minne per arbetsprocess (Python, numpy, PDAL), uppmätt ~116 MB.
WORKER_BASE_BYTES = 150e6

# Blocksidan hålls mellan dessa gränser (m). Varje block kostar ungefär 1 s
# extra för att öppna fjärrfilen, så små block blir märkbart långsammare.
MIN_BLOCK_M = 250
MAX_BLOCK_M = 5000

DEFAULT_CELL_SIZE = 1.0
# Automatiskt läge (tomma fält): hälften av det lediga arbetsminnet, men minst
# 2 GB kvar åt Pro och Windows, och en process per processortråd utom en.
# Taket på antal processer skyddar Lantmäteriets server och nätverket; varje
# block öppnar dessutom fjärrfilen (~1 s), så små block lönar sig inte.
AUTO_MEMORY_SHARE = 0.5
AUTO_MEMORY_RESERVE_GB = 2.0
MIN_MEMORY_GB = 0.5
MIN_GB_PER_WORKER = 0.75
MAX_WORKERS = 16

# Varna över denna yta: utdata blir stora och körningen lång.
LARGE_AREA_KM2 = 200

AOI_POLYGONS = "Polygoner (lager eller ritade i kartan)"
AOI_EXTENT = "Utbredning (kartvy, lager eller koordinater)"
# Tidigare etikett. Skript kan skicka den; updateParameters byter den mot
# AOI_POLYGONS innan ValueList-kontrollen (annars ERROR 000800).
AOI_POLYGONS_OLD = "Polygoner i ett lager"
AOI_EMPTY = "Rita minst en polygon i kartan eller välj ett polygonlager."

RAW_LAZ = "LAZ (komprimerad)"
RAW_LAS = "LAS (okomprimerad, kan öppnas i ArcGIS Pro)"
RAW_EXT = {RAW_LAZ: ".laz", RAW_LAS: ".las"}

# Mappar som synkas till molnet - olämpliga för stora punktfiler
_SYNC_HINTS = ("onedrive", "sharepoint", "dropbox", "google drive")

TOOL_SUMMARY = (
    "Skapar ytmodell (DSM), markmodell (DTM) och höjdskillnad (DSM - DTM) för ett "
    "intresseområde ur Lantmäteriets Laserdata Nedladdning, skog. Bara punkterna inom "
    "områdets utbredning hämtas, hela rutor laddas aldrig ned. Punkterna kan även sparas "
    "som LAZ eller LAS."
)

# Verktygstips per parameter, visas i verktygsdialogen. Se _write_tool_metadata.
TOOLTIPS = {
    "aoi_mode": (
        "Hur området avgränsas. 'Polygoner' använder polygoner ur ett lager eller polygoner "
        "som du ritar i kartan, och klipper resultatet till själva polygonerna. 'Utbredning' "
        "ger en rektangel: aktuell kartvy, utbredningen av ett lager, en ritad rektangel "
        "eller inskrivna koordinater."
    ),
    "aoi": (
        "Polygoner som avgränsar området. Välj ett polygonlager i listan, eller rita en eller "
        "flera polygoner i kartan med ritverktyget bredvid fältet (dubbelklicka för att "
        "avsluta en polygon). Från ett lager används alla objekt, eller bara de valda om "
        "lagret har ett urval. Alla polygoner slås ihop till ett område. Lagret kan ha "
        "vilket koordinatsystem som helst. Punkter hämtas inom polygonernas utbredning "
        "(bounding box), rastren klipps sedan till själva polygonerna. Verktyget går inte att "
        "köra förrän det finns minst en polygon."
    ),
    "aoi_extent": (
        "Rektangel som avgränsar området. I listan kan du välja kartvyns aktuella "
        "utbredning, eller ett lager för att använda dess utbredning. Du kan också rita en "
        "rektangel i kartan eller skriva in koordinater. Inskrivna koordinater tolkas i den "
        "aktiva kartans koordinatsystem; vilket som användes står i meddelandena."
    ),
    "consumer_key": (
        "Consumer key från Lantmäteriets API-portal. Nyckeln behöver både API:et STAC-hojd "
        "och en beställning av Laserdata Nedladdning, skog på Geotorget."
    ),
    "consumer_secret": (
        "Consumer secret som hör till nyckeln. Visas dold i dialogen och skrivs aldrig till "
        "meddelandena."
    ),
    "make_dsm": (
        "Skapa ytmodellen: trädtoppar, tak och mark där inget skymmer. Använder alla "
        "punkter utom brus (klass 7 och 18), alla returer. Varje cell får den högsta "
        "punkten inom cellstorleken x 1,41 från cellens mitt, så en hög punkt lyfter även de "
        "fyra närmaste cellerna. Tomma celler fylls från grannceller upp till 3 celler bort; "
        "större luckor, oftast öppet vatten, blir NoData."
    ),
    "make_dtm": (
        "Skapa markmodellen: markytan utan vegetation och byggnader. Använder bara punkter "
        "klassade som mark (klass 2). De trianguleras till ett TIN, och varje cell får TIN:ens "
        "höjd i cellens mitt. Modellen har inga luckor: där markpunkter saknas, under tät "
        "vegetation, byggnader eller vatten, är höjden interpolerad rakt över."
    ),
    "make_diff": (
        "Skapa höjdskillnaden DSM - DTM per cell, i praktiken vegetationens och byggnadernas "
        "höjd över mark. Negativa värden (högsta punkten under markytan, mätbrus) sätts till "
        "0, och cellen är NoData där DSM saknar värde. DSM och DTM beräknas då alltid, men "
        "sparas bara om de också är valda."
    ),
    "save_points": (
        "Spara punkterna som filer, oförändrade: alla klasser inklusive brus och alla "
        "attribut. Filerna täcker områdets bounding box, inte bara polygonerna. Kan väljas "
        "ensamt, utan några raster."
    ),
    "out_workspace": (
        "Geodatabas eller mapp där de valda rastren sparas. I en mapp blir de GeoTIFF. "
        "Behövs bara om något raster är valt."
    ),
    "prefix": (
        "Början på utdatanamnen: <prefix>_dsm, <prefix>_dtm och <prefix>_hojdskillnad. "
        "Befintliga raster med samma namn skrivs över. Bara bokstäver, siffror och "
        "understreck, och första tecknet måste vara en bokstav."
    ),
    "cell_size": (
        "Rastrens cellstorlek i meter. Punkttätheten är 1-2 punkter per m², varav ungefär "
        "en markpunkt per m² i öppen skog, så 1 m är ett bra standardval. Mindre celler "
        "ger en glest fylld DSM."
    ),
    "memory_gb": (
        "Ungefär hur mycket arbetsminne verktyget får använda utöver ArcGIS Pro självt. "
        "Tomt = automatiskt: hälften av det lediga arbetsminnet när körningen startar, men "
        "minst 2 GB lämnas kvar åt Pro och Windows. Vilket värde som användes står i "
        "meddelandena. Området delas i block som var för sig ryms i minnet, så ytan "
        "begränsas inte av minnet. Mindre budget ger fler och mindre block, vilket blir något "
        "långsammare (ungefär 1 s extra per block). Varje process behöver ungefär 170 byte "
        "per läst punkt plus 150 MB. Uppmätt: 25 km² med 4 GB tog 75 s och som mest 3,3 GB. "
        "Minst 0,5 GB."
    ),
    "workers": (
        "Antal block som bearbetas samtidigt, vart och ett i en egen process. Tomt = "
        "automatiskt: en process per processortråd utom en, högst 16, och högst en per "
        "0,75 GB i minnesbudgeten. Aldrig fler än det finns block, och blocken görs så små "
        "att de blir minst lika många som processerna. Läsningen väntar mest på nätverket, så "
        "4 block samtidigt gick ungefär dubbelt så fort som ett i taget; över 4 är vinsten "
        "inte uppmätt mot Lantmäteriets server. Med punktfiler blir det en fil per block och "
        "ruta, så fler processer ger fler och mindre filer. 1 till 32."
    ),
    "raw_folder": (
        "Mapp där punkterna sparas. Som standard i en enda fil, <prefix>_punkter.laz eller "
        ".las; annars en fil per block och ruta, se 'Samla punkterna i en fil'. Punkterna är "
        "oförändrade: alla klasser och attribut, samma värden som i Lantmäteriets filer. "
        "Filerna täcker områdets bounding box, inte hela rutor. Undvik mappar som synkas "
        "till molnet, som OneDrive."
    ),
    "merge_points": (
        "Markerat (standard) slås blockens punkter ihop till en fil, <prefix>_punkter.laz "
        "eller .las, som sista steg för punkterna. En befintlig fil med samma namn skrivs "
        "över. Sammanslagningen strömmar punkterna och behöver lite minne oavsett storlek, "
        "men disken behöver tillfälligt plats för punkterna två gånger. Kommer punkterna från "
        "rutor med olika punktformat används det senaste formatet, så inga attribut går "
        "förlorade.\n"
        "Avmarkerat blir det en fil per block och ruta, <prefix>_<ruta>_<rad>_<kolumn>, med "
        "Lantmäteriets eget punktformat, skala och offset oförändrade. Rad räknas från norr. "
        "Varje punkt finns i exakt en fil."
    ),
    "raw_format": (
        "Filformat för sparade punkter. LAZ är ungefär 5-7 gånger mindre men kan inte "
        "öppnas i ArcGIS Pro med en Basic-licens. LAS kan läggas till direkt i en karta "
        "men tar mer plats, ungefär 30 byte per punkt."
    ),
    "out_dsm": "Ytmodellen: högsta punkt nära varje cellmitt, klippt till intresseområdet.",
    "out_dtm": "Markmodellen: triangulerad från markpunkterna, klippt till intresseområdet.",
    "out_hojdskillnad": (
        "DSM minus DTM, i praktiken vegetationens och byggnadernas höjd. Negativa värden "
        "(mätbrus) sätts till 0."
    ),
}


# =============================================================================
# Förlopp
# =============================================================================

def _fmt_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "{} s".format(seconds)
    if seconds < 3600:
        return "{} min".format(int(round(seconds / 60.0)))
    return "{} h {} min".format(seconds // 3600, (seconds % 3600) // 60)


def _fmt_count(n):
    return "{:,}".format(int(n)).replace(",", " ")


def _fmt_gb(v):
    """GB med en decimal och decimalkomma: 16,9."""
    return "{:g}".format(round(float(v), 1)).replace(".", ",")


class _Steps:
    """Numrerade steg i förloppsindikatorn och i meddelandena."""

    def __init__(self, total, messages):
        self.total = total
        self.messages = messages
        self.k = 0
        self.t0 = time.time()

    def next(self, text):
        self.k += 1
        self.t_step = time.time()
        label = "Steg {} av {}: {}".format(self.k, self.total, text)
        arcpy.SetProgressor("default", label)
        self.messages.addMessage(label)

    def label(self, text):
        arcpy.SetProgressorLabel("Steg {} av {}: {}".format(self.k, self.total, text))

    def done(self, text=None):
        msg = "    klart på {}".format(_fmt_duration(time.time() - self.t_step))
        self.messages.addMessage(msg + (". " + text if text else "."))


# =============================================================================
# HTTP
# =============================================================================

def _http(req):
    """urlopen med omförsök vid tillfälliga fel. Returnerar (status, body)."""
    delay = 2
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            # HTTPError först: den är också URLError och OSError.
            if exc.code in (408, 429) or exc.code >= 500:
                if attempt < HTTP_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
            raise
        except (urllib.error.URLError, OSError):
            if attempt < HTTP_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _get_token(key, secret):
    auth = base64.b64encode("{}:{}".format(key, secret).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={"Authorization": "Basic " + auth, "User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        _status, body = _http(req)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise ValueError(
                "Lantmäteriet godkände inte consumer key/secret (HTTP {}). Kontrollera "
                "nyckeln i Lantmäteriets API-portal.".format(exc.code)
            )
        raise
    return json.loads(body.decode("utf-8"))["access_token"]


def _check_access(url, token):
    """Hämta en byte av första filen, för ett begripligt fel i stället för PDAL:s."""
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "Range": "bytes=0-0",
                      "User-Agent": USER_AGENT},
    )
    try:
        _http(req)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(
                "Nyckeln saknar behörighet till punktmolnen (HTTP {}). Den behöver både "
                "API:et STAC-hojd i Lantmäteriets API-portal och en beställning av "
                "Laserdata Nedladdning, skog på Geotorget:\n{}".format(exc.code, GEOTORGET_URL)
            )
        raise


def _stac_search(bbox_wgs84):
    """Alla items i samlingen som skär bbox, över alla sidor."""
    body = {"collections": [COLLECTION], "bbox": list(bbox_wgs84), "limit": 100}
    url, method = STAC_SEARCH_URL, "POST"
    items = []
    seen_pages = set()
    while url:
        data = json.dumps(body).encode("utf-8") if method == "POST" else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        _status, raw = _http(req)
        page = json.loads(raw.decode("utf-8"))
        feats = page.get("features", [])
        # Skydd mot en pager som returnerar samma sida om och om igen.
        page_key = tuple(f.get("id") for f in feats)
        if page_key in seen_pages:
            break
        seen_pages.add(page_key)
        items.extend(feats)

        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt or not feats:
            break
        url = nxt["href"]
        method = nxt.get("method", "GET").upper()
        body = nxt.get("body", body)
    return items


# =============================================================================
# Geometri
# =============================================================================

def _polygon_schema():
    """
    Tom polygonfeatureklass i SWEREF 99 TM, standardvärde för Feature Set-
    parametern så att dialogens ritverktyg ritar polygoner. Unikt namn i memory:
    memory-arbetsytan delas av hela Pro-sessionen, och Exists/Delete på ett fast
    namn har gett SQL-fel där.
    """
    name = "aoi_schema_{}".format(uuid.uuid4().hex[:12])
    arcpy.management.CreateFeatureclass("memory", name, "POLYGON",
                                        spatial_reference=arcpy.SpatialReference(SWEREF99TM_WKID))
    return "memory/" + name


def _is_layer(value):
    """
    Sant för ett Layer-objekt (ett lager valt i listan). arcpy.mp har ingen
    Layer-klass att jämföra med (AttributeError), klassen ligger i arcpy._mp.
    """
    return type(value).__name__ == "Layer"


def _has_features(value):
    """
    True om värdet har minst ett objekt (ett lagers urval räknas), False om det
    är tomt, None om det inte går att läsa. Läser bara första raden, så det är
    billigt även för stora lager.
    """
    try:
        with arcpy.da.SearchCursor(value, ["OID@"]) as cur:
            return next(iter(cur), None) is not None
    except Exception:
        return None


def _aoi_geometry(value, messages=None):
    """
    Alla polygoner i parametervärdet, sammanslagna, i SWEREF 99 TM.

    value är det som Feature Set-parametern (GPFeatureRecordSetLayer) ger i
    execute(): ett Layer-objekt när ett lager valts (då läses bara urvalet om
    det finns ett), ett record set med polygoner ritade i kartan, eller en
    featureklass som sökväg. SearchCursor och Describe fungerar på alla tre.
    Standardvärdet är en tom featureklass, så "inget valt" är noll polygoner,
    inte None.
    """
    if _has_features(value) is False:
        raise ValueError(AOI_EMPTY)
    # Kontrollera koordinatsystemet innan något omprojiceras: utan det skulle
    # polygonerna tyst läsas som om de redan vore i SWEREF 99 TM.
    sr_in = arcpy.Describe(value).spatialReference
    if sr_in is None or not (sr_in.factoryCode or sr_in.exportToString()):
        raise ValueError("Intresseområdet saknar koordinatsystem.")
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    geom = None
    n = 0
    with arcpy.da.SearchCursor(value, ["SHAPE@"], spatial_reference=sr) as cur:
        for (shape,) in cur:
            if shape is None or shape.area <= 0:
                continue
            n += 1
            geom = shape if geom is None else geom.union(shape)
    if geom is None:
        raise ValueError("Intresseområdet innehåller inga polygoner med yta.")
    if messages is not None:
        messages.addMessage("Intresseområde: {} polygon(er) i {}, sammanlagt {} ha.".format(
            n, sr_in.name, "{:.1f}".format(geom.area / 1e4).replace(".", ",")))
    return geom


def _active_map_sr():
    try:
        m = arcpy.mp.ArcGISProject("CURRENT").activeMap
        if m is not None and m.spatialReference is not None:
            return m.spatialReference, m.name
    except Exception:
        pass
    return None, None


def _aoi_from_extent(value, text, messages):
    """
    En GPExtent-parameter som polygon i SWEREF 99 TM.

    .value är ett geoprocessing-extentobjekt, inte arcpy.Extent. Hörnen är
    vanliga tal. Koordinatsystemet följer bara med när utbredningen kommer från
    ett lager eller en datakälla, och då bara som WKT2 i valueAsText efter de
    fyra talen. Inskrivna koordinater har inget; de tolkas i den aktiva kartans
    koordinatsystem, eftersom en utbredning vald i dialogen anges i kartans
    koordinater.
    """
    xmin, ymin, xmax, ymax = (float(value.XMin), float(value.YMin),
                              float(value.XMax), float(value.YMax))
    if not (xmax > xmin and ymax > ymin):
        raise ValueError("Utbredningen har ingen yta.")

    sr = None
    parts = (text or "").split(" ", 4)
    if len(parts) == 5 and parts[4].strip():
        sr = arcpy.SpatialReference()
        try:
            sr.loadFromString(parts[4].strip())
        except Exception:
            sr = None
    if sr is not None and (sr.factoryCode or sr.exportToString()):
        source = "utbredningens eget koordinatsystem"
    else:
        sr, map_name = _active_map_sr()
        if sr is not None and (sr.factoryCode or sr.exportToString()):
            source = "den aktiva kartans koordinatsystem ({})".format(map_name)
        else:
            sr = arcpy.SpatialReference(SWEREF99TM_WKID)
            source = "ingen aktiv karta, så SWEREF 99 TM antas"
    messages.addMessage("Utbredningen tolkas i {}: {}.".format(sr.name, source))

    # Förtäta kanterna, så att en utbredning i grader eller ett annat system
    # inte blir en för liten fyrhörning efter omprojicering.
    n = 16
    pts = ([(xmin + (xmax - xmin) * i / n, ymin) for i in range(n)]
           + [(xmax, ymin + (ymax - ymin) * i / n) for i in range(n)]
           + [(xmax - (xmax - xmin) * i / n, ymax) for i in range(n)]
           + [(xmin, ymax - (ymax - ymin) * i / n) for i in range(n)])
    poly = arcpy.Polygon(arcpy.Array([arcpy.Point(x, y) for x, y in pts]), sr)
    tm = arcpy.SpatialReference(SWEREF99TM_WKID)
    if sr.factoryCode != SWEREF99TM_WKID:
        poly = poly.projectAs(tm)
    if poly is None or poly.area <= 0:
        raise ValueError("Utbredningen kunde inte omvandlas till SWEREF 99 TM.")
    return poly


def _grid(extent, cell):
    """Rutnät justerat till jämna multiplar av cellstorleken."""
    x0 = math.floor(extent.XMin / cell) * cell
    y0 = math.floor(extent.YMin / cell) * cell
    width = int(math.ceil((extent.XMax - x0) / cell))
    height = int(math.ceil((extent.YMax - y0) / cell))
    return {"resolution": cell, "origin_x": x0, "origin_y": y0,
            "width": width, "height": height}


def _pick_tiles(items, aoi):
    """
    Behåll items vars ruta skär själva polygonen (inte bara dess bbox), och bara
    den senaste insamlingen per ruta ifall Lantmäteriet har skannat om den.
    """
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    newest = {}
    for it in items:
        props = it.get("properties", {})
        asset = it.get("assets", {}).get("data")
        pb = props.get("proj:bbox") or (asset or {}).get("proj:bbox")
        if not asset or not pb:
            continue
        xmin, ymin, xmax, ymax = pb[:4]
        rect = arcpy.Polygon(arcpy.Array([
            arcpy.Point(xmin, ymin), arcpy.Point(xmin, ymax),
            arcpy.Point(xmax, ymax), arcpy.Point(xmax, ymin)]), sr)
        if aoi.disjoint(rect):
            continue
        key = tuple(round(v) for v in pb[:4])
        dt = props.get("datetime") or ""
        if key not in newest or dt > newest[key]["datetime"]:
            newest[key] = {"id": it["id"], "href": asset["href"], "datetime": dt,
                           "bbox": (xmin, ymin, xmax, ymax),
                           "count": props.get("pc:count") or 0,
                           "start": props.get("start_datetime") or dt,
                           "end": props.get("end_datetime") or dt,
                           "area": props.get("skanningsomrade") or "",
                           "flyghojd": props.get("flyghojd"),
                           "punkttathet": props.get("punkttathet"),
                           "modified": props.get("data_modified") or ""}
    return sorted(newest.values(), key=lambda t: t["id"])


def _capture_period(tile):
    """'2021-03-07 - 2021-04-01', eller ett enda datum om start och slut är samma dag."""
    start, last = tile["start"][:10], tile["end"][:10]
    if not start:
        return "okänt"
    # end_datetime är midnatt efter sista flygdagen (2021-04-17T00 - 2021-04-18T00
    # är en enda dag), så backa en dag när slutet ligger på midnatt.
    if last > start and tile["end"][11:19] == "00:00:00":
        try:
            last = (datetime.date.fromisoformat(last) - datetime.timedelta(days=1)).isoformat()
        except ValueError:
            pass
    return start if last in ("", start) else "{} - {}".format(start, last)


def _rect_polygon(x0, y0, x1, y1):
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    return arcpy.Polygon(arcpy.Array([arcpy.Point(x0, y0), arcpy.Point(x0, y1),
                                      arcpy.Point(x1, y1), arcpy.Point(x1, y0)]), sr)


def _available_memory_gb():
    """Ledigt arbetsminne i GB, eller None. psutil ingår i arcgispro-py3."""
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        return None


def _cpu_threads():
    """Processortrådar som processen får använda (logiska kärnor)."""
    n = getattr(os, "process_cpu_count", os.cpu_count)()
    return max(1, n or 1)


def _resolve_resources(memory_gb, workers):
    """
    Minnesbudget och antal processer, där None betyder automatiskt. Läses på
    datorn som kör verktyget när körningen startar. Returnerar (minne i GB,
    processer, text om vad som valdes eller justerades, eller "").
    """
    notes = []
    if memory_gb is None:
        avail = _available_memory_gb()
        if avail is None:
            memory_gb = 4.0
            notes.append("minnesbudget 4 GB (ledigt minne kunde inte läsas)")
        else:
            memory_gb = max(MIN_MEMORY_GB, min(AUTO_MEMORY_SHARE * avail,
                                               avail - AUTO_MEMORY_RESERVE_GB))
            notes.append("minnesbudget {} GB av {} GB ledigt".format(
                _fmt_gb(memory_gb), _fmt_gb(avail)))
    memory_gb = max(MIN_MEMORY_GB, float(memory_gb))
    if workers is None:
        threads = _cpu_threads()
        workers = max(1, min(threads - 1, MAX_WORKERS, int(memory_gb / MIN_GB_PER_WORKER)))
        notes.append("{} processer ({} processortrådar)".format(workers, threads))
    workers = max(1, min(32, int(workers)))
    note = "Automatiskt: {}.".format(", ".join(notes)) if notes else ""
    # Även ett angivet antal måste rymmas: varje process behöver sitt fasta minne
    # och ett block av rimlig storlek.
    fit = max(1, int(memory_gb / MIN_GB_PER_WORKER))
    if workers > fit:
        note = (note + " " if note else "") + (
            "{} processer ryms inte i {} GB; använder {} (minst {} GB per process).".format(
                workers, _fmt_gb(memory_gb), fit,
                "{:g}".format(MIN_GB_PER_WORKER).replace(".", ",")))
        workers = fit
    return memory_gb, workers, note


def _plan_blocks(aoi, grid, tiles, memory_gb, workers, skip_outside=True):
    """
    Dela områdets rutnät i block som vart och ett ryms i minnesbudgeten delat
    med antalet samtidiga block. Blockstorleken räknas från den tätaste rutans
    punkttäthet (pc:count / rutans yta). Blocken görs också så små att de blir
    minst lika många som processerna (men inte under MIN_BLOCK_M). Block som inte
    skär polygonen hoppas över när skip_outside är sant; för punktfiler behövs alla, så att filerna täcker
    hela områdets bounding box.

    Returnerar (block, blocksida i m). Varje block har sina hörn, sitt delrutnät
    och de rutor det behöver läsa, med läsgränser inklusive marginal.
    """
    cell = grid["resolution"]
    # Marginalen ska också täcka DSM:ens extra kantceller plus sökradien.
    margin = max(READ_MARGIN_M, (DSM_PAD_CELLS + 2) * cell)
    density = max(t["count"] / ((t["bbox"][2] - t["bbox"][0]) * (t["bbox"][3] - t["bbox"][1]))
                  for t in tiles) or 1.0
    per_worker = memory_gb * 1e9 / max(1, workers) - WORKER_BASE_BYTES
    points_per_block = max(1e5, per_worker / PEAK_BYTES_PER_POINT)
    side = math.sqrt(points_per_block / density) - 2 * margin
    # Minst lika många block som processer, annars står processer oanvända:
    # läsningen väntar mest på nätverket, så parallellitet väger tyngre än den
    # extra sekunden det kostar att öppna fjärrfilen för varje block.
    area = grid["width"] * grid["height"] * cell * cell
    side = min(side, math.sqrt(area / max(1, workers)))
    side = min(MAX_BLOCK_M, max(MIN_BLOCK_M, side))
    side_cells = max(1, int(side // cell))

    blocks = []
    n_cols = int(math.ceil(grid["width"] / side_cells))
    n_rows = int(math.ceil(grid["height"] / side_cells))
    for ry in range(n_rows):
        for cx in range(n_cols):
            c0, r0 = cx * side_cells, ry * side_cells
            w = min(side_cells, grid["width"] - c0)
            h = min(side_cells, grid["height"] - r0)
            x0 = grid["origin_x"] + c0 * cell
            y0 = grid["origin_y"] + r0 * cell
            x1, y1 = x0 + w * cell, y0 + h * cell
            if skip_outside and aoi.disjoint(_rect_polygon(x0, y0, x1, y1)):
                continue
            mx0, my0 = x0 - margin, y0 - margin
            mx1, my1 = x1 + margin, y1 + margin
            reads = []
            expected = 0.0
            for t in tiles:
                tx0, ty0, tx1, ty1 = t["bbox"]
                bx0, bx1 = max(tx0, mx0), min(tx1, mx1)
                by0, by1 = max(ty0, my0), min(ty1, my1)
                if bx1 <= bx0 or by1 <= by0:
                    continue
                bounds = "([{:.2f},{:.2f}],[{:.2f},{:.2f}])".format(bx0, bx1, by0, by1)
                reads.append((t, bounds))
                expected += t["count"] * (bx1 - bx0) * (by1 - by0) / ((tx1 - tx0) * (ty1 - ty0))
            if not reads:
                continue
            blocks.append({
                # Rad räknas från norr, som man läser en karta.
                "row": n_rows - ry, "col": cx + 1,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "grid": {"resolution": cell, "origin_x": x0, "origin_y": y0,
                         "width": w, "height": h},
                "reads": reads, "expected": expected,
            })
    return blocks, side_cells * cell


class _Token:
    """Token som delas mellan trådarna och förnyas innan den går ut."""

    def __init__(self, key, secret):
        self._key, self._secret = key, secret
        self._lock = threading.Lock()
        self._token = _get_token(key, secret)
        self._time = time.time()

    def get(self):
        with self._lock:
            if time.time() - self._time > TOKEN_MAX_AGE_S:
                self._token = _get_token(self._key, self._secret)
                self._time = time.time()
            return self._token


# =============================================================================
# Arbetsprocesser
# =============================================================================

def _python_exe():
    """python.exe i den aktiva miljön. I Pro är sys.executable ArcGISPro.exe."""
    for cand in (os.path.join(sys.exec_prefix, "python.exe"), sys.executable):
        if cand and os.path.basename(cand).lower() == "python.exe" and os.path.isfile(cand):
            return cand
    raise ValueError("Hittar inte python.exe i den aktiva miljön ({}).".format(sys.exec_prefix))


class _WorkerPool:
    """
    Ett fast antal laserdata_worker-processer som tar ett block i taget.

    Varför processer och inte trådar: flera PDAL-pipelines i trådar i samma
    process som arcpy kraschade processen (access violation i arcpy efter
    blockfasen, reproducerbart med 4 trådar, aldrig med 1). Processerna startas
    en gång (cirka 1,7 s för Python, numpy och PDAL) och återanvänds.

    En läsartråd per process väntar på svar och lägger dem i en kö; trådarna
    gör bara I/O mot rören, aldrig PDAL eller arcpy.
    """

    def __init__(self, n, workdir):
        self._results = queue.Queue()
        self._procs = []
        self._logs = []
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        exe = _python_exe()
        script = os.path.join(_HERE, "laserdata_worker.py")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for i in range(n):
            log_path = os.path.join(workdir, "worker_{}.log".format(i))
            log = open(log_path, "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(
                [exe, "-u", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, encoding="utf-8", errors="replace", creationflags=flags,
                env=env, cwd=workdir)
            self._procs.append(proc)
            self._logs.append((log, log_path))
            threading.Thread(target=self._reader, args=(i, proc), daemon=True).start()

    def _reader(self, i, proc):
        for line in proc.stdout:
            if line.startswith(_lw.RESULT_PREFIX):
                self._results.put((i, json.loads(line[len(_lw.RESULT_PREFIX):])))
        self._results.put((i, None))  # processen har avslutats

    def submit(self, i, task):
        self._procs[i].stdin.write(json.dumps(task) + "\n")
        self._procs[i].stdin.flush()

    def get(self, timeout):
        """(processindex, svar) eller None vid timeout. Svar None = processen dog."""
        try:
            return self._results.get(timeout=timeout)
        except queue.Empty:
            return None

    def log_tail(self, i, n=15):
        log, path = self._logs[i]
        log.flush()
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-n:])
        except OSError:
            return ""

    def close(self, kill=False):
        for proc in self._procs:
            try:
                if kill:
                    proc.kill()
                else:
                    proc.stdin.close()
            except OSError:
                pass
        for proc in self._procs:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        for log, _path in self._logs:
            log.close()


def _block_task(blk, token, need_dsm, need_dtm, raw_folder, raw_ext, prefix, workdir):
    """Blocket som JSON-uppgift till en arbetsprocess (bara det den behöver)."""
    return {
        "block": {k: blk[k] for k in ("row", "col", "x0", "y0", "x1", "y1", "grid")}
        | {"reads": [{"href": t["href"], "id": t["id"], "bounds": bounds}
                     for t, bounds in blk["reads"]]},
        "token": token, "need_dsm": need_dsm, "need_dtm": need_dtm,
        "raw_folder": raw_folder, "raw_ext": raw_ext, "prefix": prefix, "workdir": workdir,
    }


# =============================================================================
# Utdata och metadata
# =============================================================================

def _merged_path(folder, prefix, ext):
    """Den sammanslagna punktfilen: <mapp>/<prefix>_punkter.laz eller .las."""
    return os.path.join(folder or "", "{}_punkter{}".format(prefix, ext))


def _out_path(workspace, prefix, suffix):
    name = "{}_{}".format(prefix, suffix)
    is_gdb = str(workspace).lower().endswith(".gdb")
    return os.path.join(workspace, name if is_gdb else name + ".tif")


def _default_workspace():
    try:
        gdb = arcpy.mp.ArcGISProject("CURRENT").defaultGeodatabase
        if gdb:
            return gdb
    except Exception:
        pass
    return None


def _add_to_map(paths, messages):
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:
        return
    m = aprx.activeMap
    if m is None:
        messages.addWarningMessage("Ingen aktiv karta - rastren läggs inte till.")
        return
    for p in paths:
        m.addDataFromPath(p)


_PRODUCTS = {
    SUFFIX_DSM: (
        "Ytmodell (DSM)",
        "Alla punkter utom brus (klass 7 och 18), alla returer. Varje cell har den högsta "
        "punkten inom {radius:.2f} m (cellstorleken x rot 2) från cellens mitt, så en hög "
        "punkt lyfter även de fyra närmaste cellerna. Celler utan punkt inom det avståndet "
        "är fyllda med inverst avståndsviktade värden från celler upp till {window} celler "
        "bort; större luckor, oftast öppet vatten, är NoData.",
    ),
    SUFFIX_DTM: (
        "Markmodell (DTM)",
        "Bara markpunkter (klass 2), triangulerade till ett TIN (Delaunay). Varje cell har "
        "TIN:ens höjd i cellens mitt. Modellen saknar luckor; där markpunkter saknas, under "
        "tät vegetation, byggnader eller vatten, är höjden linjärt interpolerad.",
    ),
    SUFFIX_DIFF: (
        "Höjdskillnad (DSM - DTM)",
        "Ytmodellen minus markmodellen per cell, i praktiken vegetationens och byggnadernas "
        "höjd över mark. Negativa värden (högsta punkten under markytan, mätbrus) är satta "
        "till 0. NoData där DSM saknar värde.",
    ),
}

TERMS_URL = ("https://www.lantmateriet.se/globalassets/geodata/geodataprodukter/"
             "anvandningsvillkor-for-laserdata-nedladdning-skog.pdf")


def _write_raster_metadata(path, suffix, tiles, run):
    """Titel, beskrivning, källrutor med insamlingsdatum, villkor och taggar."""
    title, method = _PRODUCTS[suffix]
    # Decimalkomma i den svenska texten.
    method = method.format(radius=run["cell"] * math.sqrt(2), window=DSM_WINDOW)
    method = re.sub(r"(\d)\.(\d)", r"\1,\2", method)
    h = escape
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            h(t["id"]), h(t["area"]), h(_capture_period(t)),
            "{} m".format(t["flyghojd"]) if t["flyghojd"] else "",
            "{} p/m²".format(t["punkttathet"]) if t["punkttathet"] else "",
            h(t["modified"][:10]))
        for t in tiles)
    periods = sorted({_capture_period(t) for t in tiles})
    ext = run["extent"]
    desc = (
        "<p>{method}</p>"
        "<p><b>Källa:</b> Lantmäteriet, Laserdata Nedladdning, skog (STAC-samling "
        "{coll}, {n} ruta/rutor). Flygburen laserskanning, klassificerat punktmoln.</p>"
        "<p><b>Insamlingsdatum:</b> {periods}. Årstiden påverkar vegetationshöjden: "
        "skanning utan löv kan ge lägre och glesare lövträdskronor än sommartid.</p>"
        "<table border='1' cellpadding='3'><tr><th>Ruta</th><th>Skanningsområde</th>"
        "<th>Insamlad</th><th>Flyghöjd</th><th>Punkttäthet (nominell)</th>"
        "<th>Punktmoln senast ändrat</th></tr>{rows}</table>"
        "<p><b>Bearbetning:</b> Cellstorlek {cell:g} m. {npts} punkter lästa inom "
        "områdets utbredning, varav {nground} markpunkter. Klippt till intresseområdet. "
        "Skapad {created} med verktyget {tool} (arcgis-laserdata-hojdmodeller).</p>"
        "<p><b>Koordinatsystem:</b> SWEREF 99 TM (EPSG:3006), höjder i meter i RH 2000 "
        "(EPSG:5613).</p>"
        "<p><b>Utbredning:</b> X {x0:.0f} - {x1:.0f}, Y {y0:.0f} - {y1:.0f}.</p>"
    ).format(method=h(method), coll=COLLECTION, n=len(tiles), periods=h(", ".join(periods)),
             rows=rows, cell=run["cell"], npts=_fmt_count(run["points"]),
             nground=_fmt_count(run["ground"]), created=run["created"],
             tool=h(HojdmodellerFranLaserdata().label),
             x0=ext.XMin, x1=ext.XMax, y0=ext.YMin, y1=ext.YMax)

    md = arcpy.metadata.Metadata(path)
    md.title = "{} från Laserdata Skog, {}".format(title, ", ".join(periods))
    md.summary = "{} i {:g} m upplösning ur Lantmäteriets Laserdata Nedladdning, skog, " \
                 "insamlad {}.".format(title, run["cell"], ", ".join(periods))
    md.description = desc
    md.tags = "Lantmäteriet, Laserdata Skog, laserskanning, höjdmodell, {}".format(
        {SUFFIX_DSM: "DSM, ytmodell", SUFFIX_DTM: "DTM, markmodell",
         SUFFIX_DIFF: "vegetationshöjd, höjdskillnad"}[suffix])
    md.credits = "© Lantmäteriet, Laserdata Nedladdning, skog."
    md.accessConstraints = (
        "Användningsvillkor för Laserdata Nedladdning, skog: {}".format(TERMS_URL))
    md.save()


def _write_tool_metadata(tool_cls, toolbox_alias):
    """
    Skriv verktygets metadatafil med parameterförklaringar från TOOLTIPS.

    Pro läser verktygstipsen i dialogen från <verktygslåda>.<verktyg>.pyt.xml
    (elementet dialogReference per parameter). Det finns inget attribut på
    arcpy.Parameter för detta. Filen skrivs bara om innehållet har ändrats.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    toolbox = os.path.splitext(os.path.basename(__file__))[0]
    path = os.path.join(here, "{}.{}.pyt.xml".format(toolbox, tool_cls.__name__))

    def html(text):
        body = escape(text).replace("\n", "</SPAN></P><P><SPAN>")
        return escape('<DIV STYLE="text-align:Left;"><P><SPAN>{}</SPAN></P></DIV>'.format(body))

    tool = tool_cls()
    params = []
    for p in tool.getParameterInfo():
        tip = TOOLTIPS.get(p.name)
        if not tip:
            continue
        params.append(
            '<param name="{n}" displayname="{d}" type="{t}" direction="{r}">'
            "<dialogReference>{h}</dialogReference>"
            "<pythonReference>{h}</pythonReference></param>".format(
                n=p.name, d=escape(p.displayName, {'"': "&quot;"}),
                t=p.parameterType, r=p.direction, h=html(tip))
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xml:lang="sv"><Esri><ArcGISFormat>1.0</ArcGISFormat></Esri>'
        '<tool name="{name}" displayname="{label}" toolboxalias="{alias}" xmlns="">'
        "<parameters>{params}</parameters><summary>{summary}</summary></tool>"
        "<dataIdInfo><idCitation><resTitle>{label}</resTitle></idCitation>"
        "<idAbs>{summary}</idAbs></dataIdInfo></metadata>\n"
    ).format(name=tool_cls.__name__, label=escape(tool.label), alias=toolbox_alias,
             params="".join(params), summary=html(TOOL_SUMMARY))

    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == xml:
                return
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
    except OSError:
        # Skrivskyddad plats: verktyget fungerar ändå, bara utan verktygstips.
        pass


# =============================================================================
# Toolbox
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Lantmäteriet Laserdata: höjdmodeller"
        self.alias = "laserdata_hojdmodeller"
        self.tools = [HojdmodellerFranLaserdata]
        _write_tool_metadata(HojdmodellerFranLaserdata, self.alias)


class HojdmodellerFranLaserdata:
    def __init__(self):
        self.label = "Höjdmodeller från Laserdata Skog"
        self.description = TOOL_SUMMARY + (
            " Kräver en consumer key och secret från Lantmäteriets API-portal med "
            "behörighet till STAC-hojd och Laserdata Nedladdning, skog."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_mode = arcpy.Parameter(
            displayName="Avgränsa området med", name="aoi_mode", datatype="GPString",
            parameterType="Required", direction="Input",
        )
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = [AOI_POLYGONS, AOI_EXTENT]
        p_mode.value = AOI_POLYGONS

        # Båda är Optional i ramverket; updateMessages kräver den som valts.
        # Feature Set: ett lager i listan eller polygoner ritade i kartan. Den
        # tomma polygonmallen gör att ritverktyget ritar polygoner.
        p_aoi = arcpy.Parameter(
            displayName="Intresseområde (polygoner)", name="aoi",
            datatype="GPFeatureRecordSetLayer", parameterType="Optional", direction="Input",
        )
        p_aoi.filter.list = ["Polygon"]
        p_aoi.value = _polygon_schema()

        p_extent = arcpy.Parameter(
            displayName="Utbredning", name="aoi_extent", datatype="GPExtent",
            parameterType="Optional", direction="Input",
        )
        p_extent.enabled = False

        p_key = arcpy.Parameter(
            displayName="Consumer key", name="consumer_key", datatype="GPString",
            parameterType="Required", direction="Input", category="Inloggning",
        )
        p_secret = arcpy.Parameter(
            displayName="Consumer secret", name="consumer_secret", datatype="GPStringHidden",
            parameterType="Required", direction="Input", category="Inloggning",
        )

        def checkbox(label, name, default):
            p = arcpy.Parameter(displayName=label, name=name, datatype="GPBoolean",
                                parameterType="Optional", direction="Input")
            p.value = default
            return p

        p_make_dsm = checkbox("DSM (ytmodell)", "make_dsm", True)
        p_make_dtm = checkbox("DTM (markmodell)", "make_dtm", True)
        p_make_diff = checkbox("Höjdskillnad (DSM - DTM)", "make_diff", True)
        p_save_pts = checkbox("Punktfiler (LAZ/LAS)", "save_points", False)

        p_raw = arcpy.Parameter(
            displayName="Mapp för punktfiler", name="raw_folder", datatype="DEFolder",
            parameterType="Optional", direction="Input",
        )
        p_raw.enabled = False
        p_raw_fmt = arcpy.Parameter(
            displayName="Format för punktfiler", name="raw_format", datatype="GPString",
            parameterType="Optional", direction="Input",
        )
        p_raw_fmt.filter.type = "ValueList"
        p_raw_fmt.filter.list = [RAW_LAZ, RAW_LAS]
        p_raw_fmt.value = RAW_LAZ
        p_raw_fmt.enabled = False
        p_merge = checkbox("Samla punkterna i en fil", "merge_points", True)
        p_merge.enabled = False

        # Optional i ramverket; krävs i updateMessages bara när ett raster är valt.
        p_ws = arcpy.Parameter(
            displayName="Utdata-arbetsyta för raster", name="out_workspace",
            datatype="DEWorkspace", parameterType="Optional", direction="Input",
        )
        default_ws = _default_workspace()
        if default_ws:
            p_ws.value = default_ws

        p_prefix = arcpy.Parameter(
            displayName="Namnprefix", name="prefix", datatype="GPString",
            parameterType="Required", direction="Input",
        )
        p_prefix.value = "laser"

        p_cell = arcpy.Parameter(
            displayName="Cellstorlek (m)", name="cell_size", datatype="GPDouble",
            parameterType="Optional", direction="Input", category="Avancerat",
        )
        p_cell.value = DEFAULT_CELL_SIZE

        p_mem = arcpy.Parameter(
            displayName="Minnesbudget (GB)", name="memory_gb", datatype="GPDouble",
            parameterType="Optional", direction="Input", category="Avancerat",
        )
        # Tomt = automatiskt, se _resolve_resources.

        p_workers = arcpy.Parameter(
            displayName="Block samtidigt", name="workers", datatype="GPLong",
            parameterType="Optional", direction="Input", category="Avancerat",
        )
        p_workers.filter.type = "Range"
        p_workers.filter.list = [1, 32]

        p_out = [
            arcpy.Parameter(displayName=label, name="out_" + suffix, datatype="DERasterDataset",
                            parameterType="Derived", direction="Output")
            for label, suffix in (("DSM", SUFFIX_DSM), ("DTM", SUFFIX_DTM),
                                  ("Höjdskillnad", SUFFIX_DIFF))
        ]

        return [p_mode, p_aoi, p_extent, p_key, p_secret, p_make_dsm, p_make_dtm, p_make_diff,
                p_save_pts, p_raw, p_raw_fmt, p_merge, p_ws, p_prefix, p_cell, p_mem, p_workers] + p_out

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        p = {q.name: q for q in parameters}
        if p["aoi_mode"].valueAsText == AOI_POLYGONS_OLD:
            p["aoi_mode"].value = AOI_POLYGONS
        by_extent = p["aoi_mode"].valueAsText == AOI_EXTENT
        p["aoi"].enabled = not by_extent
        p["aoi_extent"].enabled = by_extent
        save = bool(p["save_points"].value)
        p["raw_folder"].enabled = save
        p["raw_format"].enabled = save
        p["merge_points"].enabled = save
        rasters = any(p[n].value for n in ("make_dsm", "make_dtm", "make_diff"))
        p["out_workspace"].enabled = rasters
        p["cell_size"].enabled = rasters

    def updateMessages(self, parameters):
        p = {q.name: q for q in parameters}
        chosen = [s for s, n in ((SUFFIX_DSM, "make_dsm"), (SUFFIX_DTM, "make_dtm"),
                                 (SUFFIX_DIFF, "make_diff")) if p[n].value]
        save = bool(p["save_points"].value)

        if p["aoi_mode"].valueAsText == AOI_EXTENT:
            if not p["aoi_extent"].valueAsText:
                p["aoi_extent"].setErrorMessage("Ange en utbredning.")
        elif not p["aoi"].valueAsText:
            p["aoi"].setErrorMessage(AOI_EMPTY)
        elif not _is_layer(p["aoi"].value) and _has_features(p["aoi"].value) is False:
            # Tom polygonmall eller tom featureklass. Ett valt lager kontrolleras
            # först vid körningen: det kan vara en tjänst, och updateMessages körs
            # vid varje ändring i dialogen.
            p["aoi"].setErrorMessage(AOI_EMPTY)

        if not chosen and not save:
            p["make_dsm"].setErrorMessage("Välj minst en sak att skapa.")
        if chosen and not p["out_workspace"].valueAsText:
            p["out_workspace"].setErrorMessage("Ange var rastren ska sparas.")
        if save and not p["raw_folder"].valueAsText:
            p["raw_folder"].setErrorMessage("Ange en mapp för punktfilerna.")
        elif save and p["merge_points"].value is not False:
            pre = (p["prefix"].valueAsText or "").strip()
            target = _merged_path(p["raw_folder"].valueAsText, pre,
                                  RAW_EXT.get(p["raw_format"].valueAsText or RAW_LAZ, ".laz"))
            if pre and os.path.exists(target):
                p["merge_points"].setWarningMessage(
                    "Skrivs över: {}".format(os.path.basename(target)))

        ws = p["out_workspace"].valueAsText
        prefix = (p["prefix"].valueAsText or "").strip()
        if prefix and not (prefix[0].isalpha() and all(c.isalnum() or c == "_" for c in prefix)):
            p["prefix"].setErrorMessage(
                "Prefixet får bara innehålla bokstäver, siffror och understreck, och "
                "måste börja med en bokstav."
            )
        elif prefix and ws and chosen:
            existing = [_out_path(ws, prefix, s) for s in chosen]
            existing = [os.path.basename(e) for e in existing if arcpy.Exists(e)]
            if existing:
                p["prefix"].setWarningMessage("Skrivs över: " + ", ".join(existing))

        cell = p["cell_size"].value
        if cell is not None and not (0.25 <= cell <= 50):
            p["cell_size"].setErrorMessage("Cellstorleken ska vara mellan 0,25 och 50 m.")
        elif cell is not None and cell < 1:
            p["cell_size"].setWarningMessage(
                "Punkttätheten är 1-2 punkter/m². Under 1 m blir DSM:en glest fylld och "
                "DTM:en bara interpolerad mellan markpunkterna."
            )

        mem = p["memory_gb"].value
        if mem is not None and mem < MIN_MEMORY_GB:
            p["memory_gb"].setErrorMessage("Ange minst 0,5 GB, eller lämna tomt för automatiskt.")
        elif mem is not None:
            avail = _available_memory_gb()
            if avail is not None and mem > avail:
                p["memory_gb"].setWarningMessage(
                    "Mer än det lediga arbetsminnet ({} GB). Windows börjar då skriva till disk "
                    "och körningen blir mycket långsam.".format(_fmt_gb(avail)))

        raw = (p["raw_folder"].valueAsText or "").lower()
        if save and raw and any(h in raw for h in _SYNC_HINTS):
            p["raw_folder"].setWarningMessage(
                "Mappen ser ut att synkas till molnet. Punktfilerna kan bli flera GB och "
                "skulle då laddas upp."
            )

    def execute(self, parameters, messages):
        p = {q.name: q for q in parameters}
        products = [s for s, n in ((SUFFIX_DSM, "make_dsm"), (SUFFIX_DTM, "make_dtm"),
                                   (SUFFIX_DIFF, "make_diff")) if p[n].value]
        raw_folder = p["raw_folder"].valueAsText if p["save_points"].value else None

        try:
            if p["save_points"].value and not raw_folder:
                raise ValueError("Ange en mapp för punktfilerna.")
            if p["aoi_mode"].valueAsText == AOI_EXTENT:
                if not p["aoi_extent"].valueAsText:
                    raise ValueError("Ange en utbredning.")
                aoi = _aoi_from_extent(p["aoi_extent"].value, p["aoi_extent"].valueAsText,
                                       messages)
            else:
                if not p["aoi"].valueAsText:
                    raise ValueError(AOI_EMPTY)
                aoi = _aoi_geometry(p["aoi"].value, messages)
            outputs = _run(
                aoi,
                (p["consumer_key"].valueAsText or "").strip(),
                (p["consumer_secret"].valueAsText or "").strip(),
                products,
                raw_folder,
                RAW_EXT.get(p["raw_format"].valueAsText or RAW_LAZ, ".laz"),
                p["out_workspace"].valueAsText,
                p["prefix"].valueAsText.strip(),
                p["cell_size"].value or DEFAULT_CELL_SIZE,
                p["memory_gb"].value or None,
                p["workers"].value or None,
                messages,
                merge_points=p["merge_points"].value is not False,
            )
        except ValueError as exc:
            error = str(exc)
        else:
            error = None
        # Utanför except-blocket, så att Pro inte skriver ut hela kedjan av
        # undantag under det läsbara felmeddelandet.
        if error:
            messages.addErrorMessage(error)
            raise arcpy.ExecuteError

        names = [q.name for q in parameters]
        for suffix, path in outputs.items():
            arcpy.SetParameterAsText(names.index("out_" + suffix), path)

    def postExecute(self, parameters):
        return


# =============================================================================
# Körningens innehåll (separat funktion - går att testa utanför Pro)
# =============================================================================

def _run(aoi, key, secret, products, raw_folder, raw_ext, workspace, prefix, cell,
         memory_gb, workers, messages, merge_points=True):
    """
    aoi: polygon i SWEREF 99 TM (från _aoi_geometry eller _aoi_from_extent).
    products: de raster som ska sparas, en delmängd av SUFFIX_DSM/DTM/DIFF.
    raw_folder: mapp för punktfiler, eller None. Returnerar {suffix: sökväg}.
    merge_points: slå ihop punktfilerna till <prefix>_punkter i raw_folder. Blocken
    skrivs då först i körningens temp-mapp och slås ihop som ett eget steg.

    Området delas i block (se _plan_blocks) som bearbetas i `workers` trådar.
    Trådarna gör bara PDAL-arbete; höjdskillnad, mosaik, klippning och metadata
    görs med arcpy i huvudtråden.
    """
    if not products and not raw_folder:
        raise ValueError("Välj minst en sak att skapa: DSM, DTM, höjdskillnad eller punktfiler.")
    if products and not workspace:
        raise ValueError("Ange en utdata-arbetsyta för rastren.")
    if raw_folder and not os.path.isdir(raw_folder):
        raise ValueError("Mappen för punktfiler finns inte: {}".format(raw_folder))
    memory_gb, workers, auto_note = _resolve_resources(memory_gb, workers)
    if auto_note:
        messages.addMessage(auto_note)

    # Höjdskillnaden räknas ur DSM och DTM, så de skapas internt även om de
    # inte ska sparas.
    need_dsm = SUFFIX_DSM in products or SUFFIX_DIFF in products
    need_dtm = SUFFIX_DTM in products or SUFFIX_DIFF in products
    need_diff = SUFFIX_DIFF in products

    ext = aoi.extent
    grid = _grid(ext, cell)
    area_km2 = (ext.XMax - ext.XMin) * (ext.YMax - ext.YMin) / 1e6
    messages.addMessage("Intresseområdets utbredning: {:.2f} km² (SWEREF 99 TM).".format(area_km2))
    wanted = [{SUFFIX_DSM: "DSM", SUFFIX_DTM: "DTM", SUFFIX_DIFF: "höjdskillnad"}[s]
              for s in products] + (["punktfiler"] if raw_folder else [])
    messages.addMessage("Skapar: {}.".format(", ".join(wanted)))

    merge = bool(raw_folder) and merge_points
    n_steps = 3 + (3 if products else 0) + (1 if need_diff else 0) + (1 if merge else 0)
    steps = _Steps(n_steps, messages)
    workdir = os.path.join(arcpy.env.scratchFolder, "lds_" + uuid.uuid4().hex[:8])
    os.makedirs(workdir)
    # Blockens punktfiler: direkt i användarens mapp, eller i temp-mappen om de
    # ska slås ihop till en fil efteråt.
    block_raw = None
    if raw_folder:
        block_raw = os.path.join(workdir, "punkter") if merge else raw_folder
        os.makedirs(block_raw, exist_ok=True)
    try:
        steps.next("söker rutor i Lantmäteriets STAC-katalog")
        wgs = aoi.projectAs(arcpy.SpatialReference(4326)).extent
        items = _stac_search((wgs.XMin, wgs.YMin, wgs.XMax, wgs.YMax))
        tiles = _pick_tiles(items, aoi)
        if not tiles:
            raise ValueError(
                "Inga laserdata för området. Laserdata Skog täcker ungefär 75 % av Sverige, "
                "men inte fjällen."
            )
        blocks, side = _plan_blocks(aoi, grid, tiles, memory_gb, workers,
                                    skip_outside=not raw_folder)
        expected = sum(b["expected"] for b in blocks)
        steps.done("{} ruta/rutor. Ungefär {} miljoner punkter väntas.".format(
            len(tiles), _fmt_count(expected / 1e6)))
        for t in tiles:
            messages.addMessage("    {}: skanningsområde {}, insamlad {}.".format(
                t["id"], t["area"] or "okänt", _capture_period(t)))
        if len({_capture_period(t) for t in tiles}) > 1:
            messages.addWarningMessage(
                "Rutorna är skannade vid olika tillfällen. Rastren kan ha en skarv vid "
                "rutgränsen, särskilt om årstid eller år skiljer sig."
            )
        workers = min(workers, len(blocks))
        messages.addMessage(
            "    {} block på upp till {:.0f} x {:.0f} m, {} åt gången, inom en minnesbudget "
            "på {} GB.".format(len(blocks), side, side, workers, _fmt_gb(memory_gb)))
        if products and area_km2 > LARGE_AREA_KM2:
            out_gb = grid["width"] * grid["height"] * 4 * len(products) / 1e9
            messages.addWarningMessage(
                "Stort område: rastren blir ungefär {:.1f} GB, och lika mycket till behövs "
                "tillfälligt i {}.".format(out_gb, arcpy.env.scratchFolder))

        steps.next("hämtar token och kontrollerar behörighet")
        token = _Token(key, secret)
        _check_access(tiles[0]["href"], token.get())
        if importlib.util.find_spec("pdal") is None:
            raise ValueError(
                "Python-paketet pdal saknas i den aktiva miljön ({}). Det ingår i "
                "standardmiljön arcgispro-py3.".format(sys.prefix))
        steps.done()

        # ── Blocken ──────────────────────────────────────────────────────────
        steps.next("läser och bearbetar {} block, {} åt gången".format(len(blocks), workers))
        arcpy.SetProgressor("step", "", 0, 100, 1)
        parts = {SUFFIX_DSM: [], SUFFIX_DTM: [], SUFFIX_DIFF: []}
        diff_jobs = []
        n_points = n_ground = n_raw_files = 0
        raw_files = []
        done_expected = 0.0
        t_blocks = time.time()
        sr = arcpy.SpatialReference(SWEREF99TM_WKID, RH2000_WKID)
        old_ocs = arcpy.env.outputCoordinateSystem
        old_overwrite = arcpy.env.overwriteOutput
        old_extent = arcpy.env.extent
        pool = _WorkerPool(workers, workdir)
        failed = True
        try:
            arcpy.env.outputCoordinateSystem = sr
            arcpy.env.overwriteOutput = True
            pending = list(blocks)
            running = {}
            for i in range(workers):
                b = pending.pop(0)
                running[i] = b
                pool.submit(i, _block_task(b, token.get(), need_dsm, need_dtm, block_raw,
                                           raw_ext, prefix, workdir))
            k = 0
            while running:
                got = pool.get(timeout=1.0)
                if getattr(arcpy.env, "isCancelled", False):
                    raise ValueError("Avbrutet av användaren.")
                if got is None:
                    continue
                i, reply = got
                b = running.pop(i)
                if reply is None:
                    raise RuntimeError("Arbetsprocessen avslutades oväntat under block rad {}, "
                                       "kolumn {}:\n{}".format(b["row"], b["col"], pool.log_tail(i)))
                if not reply["ok"]:
                    raise RuntimeError("Fel i block rad {}, kolumn {}:\n{}".format(
                        b["row"], b["col"], reply["error"]))
                if pending:
                    nb = pending.pop(0)
                    running[i] = nb
                    pool.submit(i, _block_task(nb, token.get(), need_dsm, need_dtm, block_raw,
                                               raw_ext, prefix, workdir))

                res = reply["result"]
                k += 1
                n_points += res["points"]
                n_ground += res["ground"]
                n_raw_files += len(res["raw"])
                raw_files.extend(res["raw"])
                for suffix in (SUFFIX_DSM, SUFFIX_DTM):
                    if suffix in res:
                        parts[suffix].append(res[suffix])
                if need_diff and SUFFIX_DSM in res and SUFFIX_DTM in res:
                    diff_jobs.append((res, b))

                done_expected += b["expected"]
                elapsed = time.time() - t_blocks
                left = elapsed / done_expected * (expected - done_expected) if done_expected else 0
                msg = "    Block {} av {} (rad {}, kolumn {}): {} punkter".format(
                    k, len(blocks), b["row"], b["col"], _fmt_count(res["points"]))
                if res["raw"]:
                    msg += ", {} punktfil(er)".format(len(res["raw"]))
                messages.addMessage(msg + ".")
                steps.label("block {} av {} klara{}".format(
                    k, len(blocks), ", ca {} kvar".format(_fmt_duration(left)) if running else ""))
                arcpy.SetProgressorPosition(min(100, int(100 * done_expected / max(expected, 1))))
            failed = False
        finally:
            pool.close(kill=failed)
            arcpy.env.outputCoordinateSystem = old_ocs
            arcpy.env.overwriteOutput = old_overwrite

        if n_points == 0:
            raise ValueError("Inga punkter inom området.")
        if raw_folder and not merge:
            extra = ", {} punktfiler i {}".format(n_raw_files, raw_folder)
        elif raw_folder:
            extra = ", {} punktfiler att slå ihop".format(n_raw_files)
        else:
            extra = ""
        steps.done("{} punkter inom området{}.".format(_fmt_count(n_points), extra))
        if merge and raw_files:
            _merge_raw_files(raw_files, _merged_path(raw_folder, prefix, raw_ext), raw_folder,
                             workdir, steps, messages)
        if not products:
            messages.addMessage("Klart på {}.".format(_fmt_duration(time.time() - steps.t0)))
            return {}

        # ── Höjdskillnad, mosaik, klippning, metadata (bara huvudtråden) ─────
        outputs = {}
        try:
            arcpy.env.outputCoordinateSystem = sr
            arcpy.env.overwriteOutput = True

            if need_diff:
                steps.next("beräknar höjdskillnad, {} block".format(len(diff_jobs)))
                arcpy.SetProgressor("step", "", 0, max(1, len(diff_jobs)), 1)
                for k, (res, b) in enumerate(diff_jobs, 1):
                    steps.label("beräknar höjdskillnad, block {} av {}".format(k, len(diff_jobs)))
                    parts[SUFFIX_DIFF].append(_block_diff(res, b, workdir))
                    arcpy.SetProgressorPosition(k)
                steps.done()
            arcpy.env.extent = arcpy.Extent(
                grid["origin_x"], grid["origin_y"],
                grid["origin_x"] + grid["width"] * cell, grid["origin_y"] + grid["height"] * cell)

            steps.next("sätter ihop blocken till hela raster")
            mosaics = {}
            for i, suffix in enumerate(products, 1):
                if not parts[suffix]:
                    messages.addWarningMessage("Inga data för {}.".format(suffix))
                    continue
                steps.label("sätter ihop {} ({} av {}), {} block".format(
                    suffix, i, len(products), len(parts[suffix])))
                name = "m_{}.tif".format(suffix)
                arcpy.management.MosaicToNewRaster(
                    ";".join(parts[suffix]), workdir, name, sr, "32_BIT_FLOAT", cell, 1,
                    "FIRST", "FIRST")
                mosaics[suffix] = os.path.join(workdir, name)
            steps.done()

            steps.next("klipper rastren till intresseområdet och skriver metadata")
            arcpy.env.extent = old_extent
            # Klippmallen skrivs i körningens egen temp-mapp, inte i memory\:
            # Delete på en memory\-featureklass kraschade processen (access
            # violation) efter blockfasen. Mappen tas bort med resten i finally.
            clip_fc = arcpy.management.CopyFeatures([aoi], os.path.join(workdir, "aoi.shp"))[0]
            rect = "{} {} {} {}".format(ext.XMin, ext.YMin, ext.XMax, ext.YMax)
            run_info = {"extent": ext, "cell": cell, "points": n_points, "ground": n_ground,
                        "created": datetime.date.today().isoformat()}
            for i, suffix in enumerate(mosaics, 1):
                steps.label("klipper {} ({} av {})".format(suffix, i, len(mosaics)))
                dst = _out_path(workspace, prefix, suffix)
                arcpy.management.Clip(mosaics[suffix], rect, dst, clip_fc, str(NODATA),
                                      "ClippingGeometry", "NO_MAINTAIN_EXTENT")
                # Clip tappar PDAL:s sammansatta koordinatsystem, sätt det igen.
                arcpy.management.DefineProjection(dst, sr)
                try:
                    _write_raster_metadata(dst, suffix, tiles, run_info)
                except Exception as exc:
                    messages.addWarningMessage(
                        "Kunde inte skriva metadata för {}: {}".format(os.path.basename(dst), exc))
                outputs[suffix] = dst
                messages.addMessage("    Skapade {}.".format(dst))
            steps.done()
        finally:
            arcpy.env.outputCoordinateSystem = old_ocs
            arcpy.env.overwriteOutput = old_overwrite
            arcpy.env.extent = old_extent

        steps.next("lägger till rastren i kartan")
        _add_to_map(list(outputs.values()), messages)
        steps.done()
        messages.addMessage("Klart på {}.".format(_fmt_duration(time.time() - steps.t0)))
        return outputs
    finally:
        arcpy.ResetProgressor()
        # arcpy håller mappen öppen som arbetsyta efter MosaicToNewRaster, och då
        # blir en tom mapp kvar. Släpp just den mappen (utan argument skulle
        # alla arbetsytor i Pro-sessionen släppas, även användarens egna).
        try:
            arcpy.management.ClearWorkspaceCache(workdir)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _merge_raw_files(raw_files, target, raw_folder, workdir, steps, messages):
    """
    Slå ihop blockens punktfiler till target i en arbetsprocess (PDAL körs
    aldrig i Pro:s egen process). Om det misslyckas flyttas blockfilerna till
    raw_folder i stället, så att inga hämtade punkter går förlorade.
    """
    files = [f for f, _n in raw_files]
    expected = sum(n for _f, n in raw_files)
    steps.next("slår ihop {} punktfiler till en fil, {} punkter".format(
        len(files), _fmt_count(expected)))
    pool = _WorkerPool(1, workdir)
    reply, failed = None, True
    try:
        pool.submit(0, {"merge": {"files": files, "out": target.replace("\\", "/"),
                                  "expected": expected}})
        while reply is None:
            got = pool.get(timeout=1.0)
            if getattr(arcpy.env, "isCancelled", False):
                raise ValueError("Avbrutet av användaren.")
            if got is not None:
                reply = got[1] or {"ok": False, "error": pool.log_tail(0)}
        failed = False
    finally:
        pool.close(kill=failed)

    res = reply.get("result") if reply.get("ok") else None
    if res is None or res["count"] != expected:
        why = reply.get("error", "") if res is None else \
            "{} punkter i filen, {} väntade".format(_fmt_count(res["count"]), _fmt_count(expected))
        for f in files:
            shutil.move(f, os.path.join(raw_folder, os.path.basename(f)))
        if res is not None and os.path.exists(target):
            os.remove(target)
        messages.addWarningMessage(
            "Punktfilerna kunde inte slås ihop ({}). De {} blockfilerna ligger i stället i "
            "{}.".format(why.strip().splitlines()[-1] if why.strip() else "okänt fel",
                         len(files), raw_folder))
        steps.done()
        return
    for f in files:
        os.remove(f)
    size = os.path.getsize(target)
    note = "{} punkter i {} ({} MB)".format(_fmt_count(res["count"]), target,
                                            _fmt_count(size / 1e6))
    if len(res["formats"]) > 1:
        note += ", punktformat {} från rutor med format {}".format(
            res["dataformat_id"], ", ".join(str(f) for f in res["formats"]))
    steps.done(note + ".")
    if not res["exact"]:
        messages.addWarningMessage(
            "Rutorna har offset som inte går jämnt upp i skalan. Koordinaterna i den "
            "sammanslagna filen kan avvika med mindre än en skalenhet (normalt 1 cm). Avmarkera "
            "'Samla punkterna i en fil' för oförändrade koordinater.")


def _block_diff(res, blk, workdir):
    """DSM - DTM för ett block. Huvudtråden (arcpy), ett block i taget i minnet."""
    dsm = arcpy.RasterToNumPyArray(res[SUFFIX_DSM], nodata_to_value=np.nan)
    dtm = arcpy.RasterToNumPyArray(res[SUFFIX_DTM], nodata_to_value=np.nan)
    diff = dsm - dtm
    del dsm, dtm
    # Små negativa värden är mätbrus (DSM:ens högsta punkt under TIN:en).
    diff = np.where(diff < 0, 0, diff)
    diff = np.where(np.isnan(diff), NODATA, diff).astype(np.float32)
    path = os.path.join(workdir, "diff_{:03d}_{:03d}.tif".format(blk["row"], blk["col"]))
    cell = blk["grid"]["resolution"]
    arcpy.NumPyArrayToRaster(diff, arcpy.Point(blk["x0"], blk["y0"]), cell, cell, NODATA).save(path)
    return path
