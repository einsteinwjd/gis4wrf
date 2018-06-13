# GIS4WRF (https://doi.org/10.5281/zenodo.1288524)
# Copyright (c) 2018 D. Meyer and M. Riechert. Licensed under MIT.

from typing import Mapping, Tuple, List, Optional, Dict, Callable, Any
from collections import namedtuple
from enum import Enum, auto
from functools import partial
import os

import netCDF4 as nc

# Optional import for wrf-python as binary wheels are not yet available for all platforms.
# If wrf-python is not available, then derived variables are not offered.
# TODO remove try-except once wheels are available
try:
    import wrf
except ImportError:
    wrf = None

from gis4wrf.core.util import export, gdal, gdal_array, get_temp_dir, get_temp_vsi_path, read_vsi_string, remove_vsis, remove_dir
from gis4wrf.core.crs import CRS, LonLat, Coordinate2D
from gis4wrf.core.constants import ProjectionTypes
from gis4wrf.core.readers.categories import LANDUSE, LANDUSE_FIELDS
from gis4wrf.core.transforms.categories_to_gdal import get_gdal_categories

WRFNetCDFVariable = namedtuple('WRFNetCDFVariable', ['name', 'label', 'extra_dim_name'])
WRFNetCDFExtraDim = namedtuple('WRFNetCDFExtraDim', ['name', 'label', 'steps'])

__all__ = ['WRFNetCDFVariable', 'WRFNetCDFExtraDim']

# from wrf-python
COORD_VARS = ["XLAT", "XLONG", "XLAT_M", "XLONG_M", "XLAT_U", "XLONG_U",
              "XLAT_V", "XLONG_V", "CLAT", "CLONG"]

# Computed diagnostics variables from wrf-python.
MASS = ('Time', 'south_north', 'west_east')
BOTTOM_TOP_MASS = ('Time', 'bottom_top', 'south_north', 'west_east')
DIAG_DIMS = {
    'avo': BOTTOM_TOP_MASS,
    'eth': BOTTOM_TOP_MASS,
    'dbz': BOTTOM_TOP_MASS,
    'mdbz': MASS,
    'geopt': BOTTOM_TOP_MASS,
    'helicity': MASS,
    'omega': BOTTOM_TOP_MASS,
    'pvo': BOTTOM_TOP_MASS,
    'pw': MASS,
    'rh': BOTTOM_TOP_MASS,
    'rh2': MASS,
    'slp': MASS,
    'td2': MASS,
    'td': BOTTOM_TOP_MASS,
    'tc': BOTTOM_TOP_MASS,
    'theta': BOTTOM_TOP_MASS,
    'tk': BOTTOM_TOP_MASS,
    'tv': BOTTOM_TOP_MASS,
    'twb': BOTTOM_TOP_MASS,
    'updraft_helicity': MASS,
    'ua': BOTTOM_TOP_MASS,
    'va': BOTTOM_TOP_MASS,
    'wa': BOTTOM_TOP_MASS,
    'z': BOTTOM_TOP_MASS,
}
DIAG_VARS = {
    name: WRFNetCDFVariable(name, label, DIAG_DIMS[name][1] if len(DIAG_DIMS[name]) == 4 else None)
    for name, label in [
        ('avo', 'AVO* in 10-5 s-1 (Absolute Vorticity)'),
        ('eth', 'ETH* in K (Equivalent Potential Temperature)'),
        ('dbz', 'DBZ* in dBZ (Radar Reflectivity)'),
        ('mdbz', 'MDBZ* in dBZ (Maximum Radar Reflectivity)'),
        ('geopt', 'GEOPT* in m2 s-2 (Geopotential for the Mass Grid)'),
        ('helicity', 'HELICITY* in m2 s-2 (Storm Relative Helicity)'),
        ('omega', 'OMEGA* in Pa s-1 (Omega)'),
        ('pvo', 'PVO* in PVU (Potential Vorticity)'),
        ('pw', 'PW* in kg m-2 (Precipitable Water)'),
        ('rh', 'RH* in % (Relative Humidity)'),
        ('rh2', 'RH2* in % (2m Relative Humidity)'),
        ('slp', 'SLP* in hPA (Sea Level Pressure)'),
        ('td2', 'TD2* in °C (2m Dew Point Temperature)'),
        ('td', 'TD* in °C (Dew Point Temperature)'),
        ('tc', 'TC* in °C (Temperature)'),
        ('theta', 'THETA* in K (Potential Temperature)'),
        ('tk', 'TK* in K (Temperature)'),
        ('tv', 'TV* in K (Virtual Temperature)'),
        ('twb', 'TWB* in K (Wet Bulb Temperature)'),
        ('updraft_helicity', 'UPDRAFT_HELICITY* in m2 s-2 (Updraft Helicity)'),
        ('ua', 'UA* in m s-1 (U-component of Wind on Mass Points)'),
        ('va', 'VA* in m s-1 (V-component of Wind on Mass Points)'),
        ('wa', 'WA* in m s-1 (W-component of Wind on Mass Points)'),
        ('z', 'Z* in m (Model Height (MSL))'),
    ]
}

@export
class GDALFormat(Enum):
    HDF5_VRT = '.h5.vrt'
    NETCDF_VRT = '.nc.vrt'
    GTIFF = '.tif'

    @property
    def is_vrt(self):
        return self in [self.HDF5_VRT, self.NETCDF_VRT]

# TODO move to transforms package
@export
def convert_wrf_nc_var_to_gdal_dataset(
    path: str, var_name: str, extra_dim_index: Optional[int],
    interp_level: Optional[float], interp_vert_name: Optional[str],
    fmt: GDALFormat=GDALFormat.GTIFF, use_vsi: bool=False) -> Tuple[str,Callable[[],None]]:
    ''' IMPORTANT: The NetCDF VRT datasets returned by this function require the
        GDAL config option GDAL_NETCDF_BOTTOMUP to be set to 'NO'.
        The default of GDAL is 'YES' which would work as well (by flipping the y axis
        part of the geo transform) but is extremely slow as GDAL can then
        only read one line at a time, compared to a whole block otherwise.
        This is a performance bug which we can work around here since we construct
        the geotransform ourselves anyway.
        References:
        http://lists.osgeo.org/pipermail/gdal-dev/2016-November/045573.html
        https://github.com/perrygeo/ncvrt#--flip-or-invert-latitude-of-bottom-up-data
    '''
    if fmt == GDALFormat.GTIFF:
        # LU_INDEX has a color table which is unsupported with TIFF, so we force HDF5_VRT instead.
        # (GDAL: "SetColorTable() not supported for multi-sample TIFF files.")
        if var_name == 'LU_INDEX':
            fmt = GDALFormat.HDF5_VRT

    if fmt == GDALFormat.HDF5_VRT:
        # TODO remove once gdal bug is fixed: https://github.com/OSGeo/gdal/issues/622 
        if var_name in ['E', 'F']:
            fmt = GDALFormat.NETCDF_VRT

    if var_name in DIAG_VARS:
        assert wrf is not None
        fmt = GDALFormat.GTIFF

    if interp_level is not None:
        assert interp_vert_name
        fmt = GDALFormat.GTIFF

    # WPS netCDF output files have only float32 variables and there
    # seems to be a unique no-data value which is 32768.
    # TODO find out where in WPS's source code this value is defined
    no_data = 32768.0

    time_steps = get_wrf_nc_time_steps(path)

    ds = nc.Dataset(path)
    attrs = ds.__dict__ # type: dict

    rows = ds.dimensions['south_north'].size
    cols = ds.dimensions['west_east'].size

    crs = get_crs(ds)
    geo_transform = get_geo_transform(ds, crs)

    if var_name == 'LU_INDEX':
        landuse_color_table, landuse_cat_names = get_landuse_categories(ds)
    
    if var_name in DIAG_VARS or interp_level is not None:
        try:
            var = wrf.getvar(ds, var_name, timeidx=wrf.ALL_TIMES, missing=no_data, meta=False)
        except:
            var = wrf.getvar(ds, var_name, timeidx=wrf.ALL_TIMES, meta=False)
        if interp_level is not None:
            vert = wrf.getvar(ds, interp_vert_name, timeidx=wrf.ALL_TIMES, meta=False)
            var = wrf.interplevel(var, vert, interp_level, missing=no_data, meta=False)
            dims = MASS
        else:
            dims = DIAG_DIMS[var_name]
        shape = var.shape
    else:
        var = ds.variables[var_name]
        dims = var.dimensions
        shape = var.shape
    assert len(dims) == len(shape)
    if len(dims) == 4:
        # TODO remove once performance issues with VRT are resolved
        #      (see below)
        fmt = GDALFormat.GTIFF

    use_vrt = fmt.is_vrt
    ext = fmt.value

    if use_vsi:
        out_path = get_temp_vsi_path(ext)
    else:
        out_dir = get_temp_dir()
        out_path = os.path.join(out_dir, 'tmp' + ext)

    if use_vrt:
        driver_name = 'VRT'
    elif fmt == GDALFormat.GTIFF:
        driver_name = 'GTIFF'

    driver = gdal.GetDriverByName(driver_name) # type: gdal.Driver

    is_4d = len(shape) == 4
    if is_4d:
        assert extra_dim_index is not None
        extra_dim_size = shape[1]
        assert extra_dim_index < extra_dim_size
    else:
        assert extra_dim_index is None

    print('Adding {}'.format(var_name))
    type_code = gdal_array.NumericTypeCodeToGDALTypeCode(var.dtype)

    times = shape[0]

    gdal_ds = driver.Create(out_path, cols, rows, times, type_code) # type: gdal.Dataset
    gdal_ds.SetProjection(crs.wkt)
    gdal_ds.SetGeoTransform(geo_transform)

    for band_idx in range(1, times + 1):
        band = gdal_ds.GetRasterBand(band_idx) # type: gdal.Band
        band.SetNoDataValue(no_data)

        time_step = time_steps[band_idx-1]
        band.SetDescription(time_step)

        if var_name == 'LU_INDEX' and landuse_cat_names:
            band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
            band.SetRasterColorTable(landuse_color_table)
            band.SetRasterCategoryNames(landuse_cat_names)
        
        if use_vrt:
            # GDAL's HDF5 driver does not support reading 4D variables
            # whereas the NetCDF driver exposes 4D as 2D with many bands but has performance issues
            # (see https://github.com/OSGeo/gdal/issues/620). Therefore, for now, 4D is only
            # supported as GeoTIFF and not VRT.
            assert not is_4d

            if fmt == GDALFormat.HDF5_VRT:
                subdataset_name = 'HDF5:"{path}"://{var_name}'.format(path=path, var_name=var_name)
            elif fmt == GDALFormat.NETCDF_VRT:
                subdataset_name = 'NETCDF:"{path}":{var_name}'.format(path=path, var_name=var_name)

            band.SetMetadata({'source_0': ('''
                <SimpleSource>
                    <SourceFilename relativeToVRT="0">{name}</SourceFilename>
                    <SourceBand>{band}</SourceBand>
                    <SrcRect xOff="0" yOff="0" xSize="{cols}" ySize="{rows}" />
                    <DstRect xOff="0" yOff="0" xSize="{cols}" ySize="{rows}" />
                </SimpleSource>''').format(name=subdataset_name, band=band_idx, rows=rows, cols=cols)}, 'vrt_sources')
        elif fmt == GDALFormat.GTIFF:
            data = var[band_idx - 1]
            if is_4d:
                data = data[extra_dim_index]
            band.WriteArray(data)

    gdal_ds.FlushCache()

    if use_vsi:
        dispose = partial(remove_vsis, [out_path])
    else:
        dispose = partial(remove_dir, out_dir)

    return out_path, dispose

@export
def get_supported_wrf_nc_variables(path: str) -> Dict[str,WRFNetCDFVariable]:
    extra_dims = get_wrf_nc_extra_dims(path)
    ds = nc.Dataset(path)
    variables = {}
    for var_name in ds.variables:
        if var_name in COORD_VARS:
            print('Ignoring {}, coord var'.format(var_name))
            continue

        var = ds.variables[var_name]
        dims = var.dimensions
        shape = var.shape

        if len(dims) > 4:
            # should never happen
            print('Ignoring {}, too many dims: {}'.format(var_name, dims))
            continue

        if dims[0] != 'Time':
            # should never happen
            print('Ignoring {}, time dim missing, dims: {}'.format(var_name, dims))
            continue

        # TODO support staggered vars
        if dims[-2:] != ('south_north', 'west_east'):
            print('Ignoring {}, staggered, dims: {}'.format(var_name, dims))
            continue

        if len(dims) == 4:
            extra_dim = dims[1]
            if extra_dim not in extra_dims:
                print('Ignoring {}, unsupported z dimension: {}'.format(var_name, extra_dim))
                continue
        else:
            extra_dim = None

        try:
            description = var.getncattr('description')
        except AttributeError:
            description = None
        try:
            units = var.getncattr('units')
        except AttributeError:
            units = None

        label = var_name
        if units and units != '-':
            label += ' in ' + units
        if description and description != '-':
            label += ' (' + description.lower() + ')'

        variables[var_name] = WRFNetCDFVariable(name=var_name, label=label, extra_dim_name=extra_dim)

    if wrf is not None:
        is_wps = 'bottom_top' not in ds.dimensions
        if not is_wps:
            variables.update(DIAG_VARS)
    
    return variables

@export
def get_wrf_nc_extra_dims(path: str) -> Dict[str,WRFNetCDFExtraDim]:
    ds = nc.Dataset(path)
    dims = ds.dimensions
    attrs = ds.__dict__
    extra_dims = {} # type: Dict[str,WRFNetCDFExtraDim]

    def add_dim(name: str, label: str, step_fn: Optional[Callable[[int],Any]]=None):
        if name not in dims:
            return
        if step_fn is None:
            step_fn = lambda i: i
        steps = [str(step_fn(i)) for i in range(1, dims[name].size + 1)]
        extra_dims[name] = WRFNetCDFExtraDim(name=name, label=label, steps=steps)

    add_dim('bottom_top', 'Vertical Level')
    add_dim('soil_layers_stag', 'Soil Depth Layer')

    # the following exist in geogrid output only
    landuse_scheme = attrs.get('MMINLU')
    landuse_categories = LANDUSE.get(landuse_scheme, {})
    add_dim('land_cat', 'Land Use Category', lambda i: landuse_categories.get(i, (str(i), ''))[0])
    add_dim('soil_cat', 'Soil Type Category')
    add_dim('month', 'Month')

    # the following exist in metgrid output only
    add_dim('num_metgrid_levels', 'Vertical Level')
    # TODO add num_st_layers, num_sm_layers, z-dimension00**

    return extra_dims

@export
def get_wrf_nc_time_steps(path: str) -> List[str]:
    ds = nc.Dataset(path)
    steps = []
    # Each time step is stored as a sequence of 1-byte chars, e.g.:
    # array([b'2', b'0', b'0', b'5', b'-', b'0', b'8', b'-', b'2', b'8', b'_',
    #   b'0', b'0', b':', b'0', b'0', b':', b'0', b'0'],
    #  dtype='|S1')
    # ... which we convert to a plain string '2005-08-28_00:00:00'
    # and replace the underscore with a space: '2005-08-28 00:00:00'.
    for val in ds.variables['Times']:
        time = ''.join([c.decode() for c in val])
        time = time.replace('_', ' ')
        steps.append(time)
    return steps

def get_landuse_categories(ds: nc.Dataset) -> Tuple[gdal.ColorTable,List[str]]:
    attrs = ds.__dict__ # type: dict

    landuse_scheme = attrs.get('MMINLU')
    landuse_categories = LANDUSE.get(landuse_scheme, {}).copy()
    landuse_num_cats = attrs.get('NUM_LAND_CAT', 0)

    for field, (label, color) in LANDUSE_FIELDS.items():
        field = field.upper()
        if field not in attrs:
            continue
        val = attrs[field]
        if not 1 <= val <= landuse_num_cats:
            continue
        if val in landuse_categories:
            continue
        landuse_categories[val] = (label, color)

    return get_gdal_categories(landuse_categories, 1, landuse_num_cats)

def get_crs(ds: nc.Dataset) -> CRS:
    attrs = ds.__dict__ # type: dict
    proj_id = attrs['MAP_PROJ']

    if proj_id == ProjectionTypes.LAT_LON:
        pole_lat = attrs['POLE_LAT']
        pole_lon = attrs['POLE_LON']
        if pole_lat != 90.0 or pole_lon != 0.0:
            raise NotImplementedError('Rotated pole not supported')
        crs = CRS.create_lonlat()

    elif proj_id == ProjectionTypes.LAMBERT_CONFORMAL:
        crs = CRS.create_lambert(
            truelat1=attrs['TRUELAT1'],
            truelat2=attrs['TRUELAT2'],
            origin=LonLat(lon=attrs['STAND_LON'], lat=attrs['MOAD_CEN_LAT']))

    elif proj_id == ProjectionTypes.MERCATOR:
        crs = CRS.create_mercator(
            truelat1=attrs['TRUELAT1'],
            origin_lon=attrs['STAND_LON'])

    elif proj_id == ProjectionTypes.POLAR_STEREOGRAPHIC:
        crs = CRS.create_polar(
            truelat1=attrs['TRUELAT1'],
            origin_lon=attrs['STAND_LON'])

    else:
        raise NotImplementedError('Projection {} not supported'.format(proj_id))

    return crs

def get_geo_transform(ds: nc.Dataset, crs: CRS) -> Tuple[float,float,float,float,float,float]:
    lons_u = ds.variables['XLONG_U']
    lons_v = ds.variables['XLONG_V']
    lats_u = ds.variables['XLAT_U']
    lats_v = ds.variables['XLAT_V']

    dim_x = ds.dimensions['west_east'].size
    dim_y = ds.dimensions['south_north'].size

    # TODO check that nests are non-moving
    # assume lat/lon coordinates are identical each time step
    t = 0

    lower_left_u = LonLat(lon=lons_u[t,0,0], lat=lats_u[t,0,0])
    lower_right_u = LonLat(lon=lons_u[t,0,-1], lat=lats_u[t,0,-1])
    lower_left_v = LonLat(lon=lons_v[t,0,0], lat=lats_v[t,0,0])
    upper_left_v = LonLat(lon=lons_v[t,-1,0], lat=lats_v[t,-1,0])

    proj_id = ds.getncattr('MAP_PROJ')
    if proj_id == ProjectionTypes.LAT_LON and lower_left_u.lon == lower_right_u.lon:
        # global coverage
        # WRF uses either 0,0 or 180,180 here, but it should use 0,360 or -180,180.
        # Let's fix it by looking at the center longitude.
        # Note that this is only an issue for the U grid as the corner points lie
        # exactly at the discontinuity, whereas in V the points are half a cell size away.
        cen_lon = ds.getncattr('CEN_LON')
        lon_min = cen_lon - 180
        lon_max = cen_lon + 180
        lower_left_u = LonLat(lon=lon_min, lat=lower_left_u.lat)
        lower_right_u = LonLat(lon=lon_max, lat=lower_right_u.lat)

    lower_left_u_xy = crs.to_xy(lower_left_u)
    lower_right_u_xy = crs.to_xy(lower_right_u)
    lower_left_v_xy = crs.to_xy(lower_left_v)
    upper_left_v_xy = crs.to_xy(upper_left_v)

    dx = (lower_right_u_xy.x - lower_left_u_xy.x)/dim_x
    dy = (upper_left_v_xy.y - lower_left_v_xy.y)/dim_y

    geo_transform = (lower_left_u_xy.x, dx, 0, lower_left_v_xy.y, 0, dy)

    return geo_transform