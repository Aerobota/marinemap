from django.db import models
from django.conf import settings
from django.core import serializers
from django.db.utils import DatabaseError
import tempfile
import time
import os

try:
    RASTDIR = settings.RASTER_DIR
except:
    RASTDIR = os.path.join(os.path.dirname(__file__), 'test_data')

RASTER_TYPES = ( 
                ("continuous", "continuous"),
                ("categorical", "catgorical"),
               )
try:
    STARSPAN_BIN = settings.STARSPAN_BIN
except:
    STARSPAN_BIN = 'starspan'


class RasterDataset(models.Model):
    name = models.CharField(max_length=30, unique=True)
    full_name = models.CharField(max_length=255, default="")
    filepath = models.FilePathField(max_length=255, path=RASTDIR, recursive=True)
    type = models.CharField(max_length=30, choices=RASTER_TYPES)
    
    def __unicode__(self):
        return unicode(self.name + " raster at " + self.filepath)
    
class ZonalCategory(models.Model):
    category = models.IntegerField()
    count = models.IntegerField()

    def __unicode__(self):
        return unicode("Category %s (count = %s)" % (self.category, self.count))

class ZonalStatsCache(models.Model):
    geom_hash = models.CharField(max_length=255)
    raster = models.ForeignKey('RasterDataset')
    sum = models.FloatField(null=True, blank=True)
    avg = models.FloatField(null=True, blank=True)
    min = models.FloatField(null=True, blank=True)
    max = models.FloatField(null=True, blank=True)
    mode = models.FloatField(null=True, blank=True)
    median = models.FloatField(null=True, blank=True)
    stdev = models.FloatField(null=True, blank=True)
    nulls = models.FloatField(null=True, blank=True)
    pixels = models.FloatField(null=True, blank=True)
    categories = models.ManyToManyField(ZonalCategory)
    date_modified = models.DateTimeField(auto_now=True)

    @property
    def json(self):
        return serializers.serialize("json", self)

    def __unicode__(self):
        return unicode("Zonal Stats for %s - avg:%s , pixels:%s, nulls:%s" % (self.raster.name, self.avg, self.pixels, self.nulls))

    class Meta:
        unique_together = ('geom_hash', 'raster')

def geom_to_file(geom, filepath):
    json = """{
"type": "FeatureCollection",
"features": [
{ "type": "Feature", "properties": { "id": 1 }, "geometry": %s }
]
}""" % geom.json
    fh = open(filepath,'w')
    fh.write(json)
    fh.close()
    assert os.path.exists(filepath)

def run_starspan_zonal(geom, rasterds, write_cache=False, pixprop=0.5):
    """
    Consider this a 'private' method .. dont call directly, use zonal_stats() instead
    Runs starspan and returns a ZonalStatsCache object
    If not write_cache, just return an unsaved object
    """
    # Create tempdir and cd in 
    tmpdir = tempfile.gettempdir()
    os.chdir(tmpdir)

    # Output geom to temp dataset
    timestamp = str(time.time())
    out_json = os.path.join(tmpdir, 'geom_%s.json' % timestamp)
    geom_to_file(geom, out_json)

    # Run starspan
    out_csv = os.path.join(tmpdir, 'output_%s_stats.csv' % timestamp)
    out_categories = os.path.join(tmpdir, 'output_%s_categories.csv' % timestamp)
    if os.path.exists(out_csv): os.remove(out_csv)
    if os.path.exists(out_categories): os.remove(out_categories)
    cmd = '%s --vector %s --where "id=1" --out-prefix %s/output_%s --out-type table --summary-suffix _stats.csv --raster %s --stats avg mode median min max sum stdev nulls --pixprop %s ' % (STARSPAN_BIN,out_json,tmpdir, timestamp, rasterds.filepath, pixprop)
    if rasterds.type == 'categorical':
        cmd += "--class-summary-suffix _categories.csv"

    starspan_out = os.popen(cmd).read()

    if not os.path.exists(out_csv):
        raise Exception("Starspan failed to produce output file: %s" % starspan_out)
    if rasterds.type == 'categorical' and not os.path.exists(out_categories):
        raise Exception("Starspan failed to produce output file: %s" % starspan_out)

    res = open(out_csv,'r').readlines()

    # Create zonal model
    hash = geom.wkt.__hash__()
    zonal = ZonalStatsCache(raster=rasterds, geom_hash=hash)

    # Make sure we have valid results output by starspan
    if len(res) == 2 and "Intersecting features: 0" not in starspan_out:
        headers = [x.strip() for x in res[0].split(',')]
        vals = [x.strip() for x in res[1].split(',')]
        assert len(headers) == len(vals)

        # loop and populate model
        for i in range(len(headers)):
            if "_Band1" in headers[i]:
                stat_type = headers[i].replace("_Band1",'')
                zonal.__dict__[stat_type] = float(vals[i])
            elif headers[i] == 'numPixels':
                zonal.pixels = float(vals[i])

    # Handle categories
    if rasterds.type == 'categorical' and zonal.pixels:
        zonal.save()
        res = open(out_categories).readlines()
        headers = [x.strip() for x in res[0].split(',')]
        assert headers == ['FID', 'class', 'count']
        for row in res[1:]:
            vals = [int(x.strip()) for x in row.split(',')]
            cat = vals[1]
            count = vals[2]
            zc = ZonalCategory.objects.create(category=cat, count=count)
            zc.save()
            zonal.categories.add(zc)

    # return zonal object (caching it if needed)
    if write_cache:
        try:
            if zonal.pixels:
                zonal.save()
        except:
            # Most likely another zonal stats cache for this geom/raster
            # was saved to the cache before this one completed.
            pass
    try:
        remove_tmp = settings.STARSPAN_REMOVE_TMP
    except:
        remove_tmp = True

    if remove_tmp:
        if os.path.exists(out_csv): os.remove(out_csv)
        if os.path.exists(out_json): os.remove(out_json)
        if os.path.exists(out_categories): os.remove(out_categories)
        import glob
        for fname in glob.glob(os.path.join(tmpdir, 'output_%s_*' % timestamp)):
            if os.path.exists(fname): os.remove(fname)


    return zonal

def clear_cache():
    objs = ZonalStatsCache.objects.all()
    objs.delete()

def zonal_stats(geom, rasterds, write_cache=True, read_cache=True, cache_only=False, pixprop=0.5):
    """
    Given a GEOSGeometry and a RasterDataset,
    compute the zonal stats and return json like
     { 'raster': 'elevation', 'stats': {'sum': 10234.2, 'mean': 12.4}}
    result can be stored in cache (write_cache)
     and cache value is returned if read_cache
    """
    if not geom.valid:
        return None

    hash = geom.wkt.__hash__()
     
    cached = None
    if read_cache:
        try:
            cached = ZonalStatsCache.objects.get(geom_hash=hash, raster=rasterds)
        except ZonalStatsCache.DoesNotExist:
            cached = None
        except DatabaseError:
            cached = None
    else:
        #If we're not reading the cache, 
        #we're not going to write to it either
        write_cache = False 

    if cached:
        result = cached
        result.from_cache = True
    else:
        if cache_only:
            # Return an empty result
            result = ZonalStatsCache(geom_hash=hash, raster=rasterds)
        else:
            result = run_starspan_zonal(geom, rasterds, write_cache=write_cache, pixprop=pixprop)
        result.from_cache = False
    
    return result
