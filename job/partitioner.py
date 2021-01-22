#
# (C) Copyright IBM Corp. 2019
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# import laspy
import math
import copy
import logging
import requests
import numpy as np
import struct
from .variance import variancex_y
from pwlidar_cloud import utils
from multiprocessing.pool import ThreadPool
from pwlidar_cloud.storage import Storage
from pwlidar_cloud.storage.utils import CloudObject, CloudObjectUrl
from pwlidar_cloud.job.lasdata_parse import parse_header
logger = logging.getLogger(__name__)

CHUNK_SIZE_MIN = 0*1024  # 0MB
CHUNK_THRESHOLD = 128*1024  # 128KB



def scaled_x_dimension(las_file):
    x_dimension = las_file.X
    scale = las_file.header.scale[0]
    offset = las_file.header.offset[0]
    return(x_dimension*scale + offset)


def scaled_y_dimension(las_file):
    y_dimension = las_file.Y
    scale = las_file.header.scale[1]
    offset = las_file.header.offset[1]
    return(y_dimension*scale + offset)


def scaled_z_dimension(las_file):
    z_dimension = las_file.Z
    scale = las_file.header.scale[1]
    offset = las_file.header.offset[1]
    return(z_dimension*scale + offset)


def create_partitions(pywren_config, map_iterdata, rows, cols, partition_type):
    """
    Method that returns the function that will create the partitions of the objects in the Cloud
    """
    logger.debug('Starting partitioner')

    parts_per_object = None

    sbs = set()
    buckets = set()
    prefixes = set()
    obj_names = set()
    urls = set()

    logger.debug("Parsing input data")
    for elem in map_iterdata:
        if 'url' in elem:
            urls.add(elem['url'])
        elif 'obj' in elem:
            sb, bucket, prefix, obj_name = utils.split_object_url(elem['obj'])
            if obj_name:
                obj_names.add((bucket, prefix))
            elif prefix:
                prefixes.add((bucket, prefix))
            else:
                buckets.add(bucket)
            sbs.add(sb)

    if len(sbs) > 1:
        raise Exception('Currently we only support to process one storage backend at a time'
                        'Specified storage backends: {}'.format(sb))

    if [prefixes, obj_names, urls, buckets].count(True) > 1:
        raise Exception('You must provide as an input data a list of bucktes, '
                        'a list of buckets with object prefix, a list of keys '
                        'or a list of urls. Intermingled types are not allowed.')

    if not urls:
        # process objects from an object store. No url
        sb = sbs.pop()
        storage_handler = Storage(pywren_config, sb).get_storage_handler()
        objects = {}
        if obj_names:
            for bucket, prefix in obj_names:
                logger.debug("Listing objects in '{}://{}'".format(sb, '/'.join([bucket, prefix])))
                objects[bucket] = storage_handler.list_objects(bucket, prefix)
        elif prefixes:
            for bucket, prefix in prefixes:
                logger.debug("Listing objects in '{}://{}'".format(sb, '/'.join([bucket, prefix])))
                objects[bucket] = storage_handler.list_objects(bucket, prefix)
        elif buckets:
            for bucket in buckets:
                logger.debug("Listing objects in '{}://{}'".format(sb, bucket))
                objects[bucket] = storage_handler.list_objects(bucket)

        keys_dict = {}
        header = {}
        for bucket in objects:
            keys_dict[bucket] = {}
            for obj in objects[bucket]:
                keys_dict[bucket][obj['Key']] = {}
                keys_dict[bucket][obj['Key']]['Size'] = obj['Size']
                extra_get_args = {'Range': "bytes=" + str(96) + "-" + str(100)}
                header_offset = storage_handler.get_object(bucket, obj['Key'], extra_get_args = extra_get_args)
                header_offset = struct.unpack('<L', header_offset[0:4])[0]
                extra_get_args['Range'] = "bytes=" + str(0) + "-" + str(header_offset)
                file_header = storage_handler.get_object(bucket, obj['Key'], extra_get_args = extra_get_args)
                keys_dict[bucket][obj['Key']]['header'] = parse_header(file_header)
                # keys_dict[bucket][obj['Key']]['header'] = parse_header(bucket, obj['Key'])

    if buckets or prefixes:
        partitions, parts_per_object = _split_objects_from_buckets(map_iterdata, keys_dict, rows, cols, partition_type)

    elif obj_names:
        partitions, parts_per_object = _split_objects_from_keys(map_iterdata, keys_dict, rows, cols, partition_type)

    # elif urls:
        # partitions, parts_per_object = _split_objects_from_urls(map_iterdata, chunk_size, chunk_number)

    else:
        raise ValueError('You did not provide any bucket or object key/url')

    return partitions, parts_per_object


def _split_objects_from_buckets(map_func_args_list, keys_dict, chunk_size, chunk_number, partition_type):
    """
    Create partitions from bucket/s
    """
    logger.info('Creating dataset tiles from bucket/s ...')
    partitions = []
    parts_per_object = []

    for entry in map_func_args_list:
        # Each entry is a bucket
        sb, bucket, prefix, obj_name = utils.split_object_url(entry['obj'])

        if chunk_size or chunk_number:
            logger.info('Creating tiles from objects within: {}'.format(bucket))
        else:
            logger.info('Discovering objects within: {}'.format(bucket))

        for key, obj_size in keys_dict[bucket].items():
            if prefix in key and obj_size > 0:
                logger.debug('Creating tiles from object {} size {}'.format(key, obj_size))
                total_partitions = 0
                size = 0

                if chunk_number:
                    chunk_rest = obj_size % chunk_number
                    chunk_size = obj_size // chunk_number + chunk_rest

                if chunk_size and chunk_size < CHUNK_SIZE_MIN:
                    chunk_size = None

                if chunk_size is not None and obj_size > chunk_size:
                    while size < obj_size:
                        brange = (size, size+chunk_size+CHUNK_THRESHOLD)
                        size += chunk_size
                        partition = entry.copy()
                        partition['obj'] = CloudObject(sb, bucket, key)
                        partition['obj'].data_byte_range = brange
                        partition['obj'].chunk_size = chunk_size
                        partition['obj'].part = total_partitions
                        partitions.append(partition)
                        total_partitions = total_partitions + 1
                else:
                    partition = entry.copy()
                    partition['obj'] = CloudObject(sb, bucket, key)
                    partition['obj'].data_byte_range = None
                    partition['obj'].chunk_size = chunk_size
                    partition['obj'].part = total_partitions
                    partitions.append(partition)
                    total_partitions = 1

                parts_per_object.append(total_partitions)

    return partitions, parts_per_object


def _split_objects_from_keys(map_func_args_list, keys_dict, rows, cols, partition_type):
    """
    Create partitions from a list of objects keys
    """

    if rows and cols:
        num_tiles = rows * cols
        if num_tiles > 1:
            logger.info('Creating tiles from object keys...')
        if num_tiles > 32767:
            raise Exception("There are too many output tiles. Try choosing a larger grid width.")
    else:
        num_tiles = None
        # elif num_tiles == 0:
        #     raise Exception(""" rows * cols must not equal zero. Try choosing another values so that
        #                         the result not equal zero.""")

    partitions = []
    parts_per_object = []

    for entry in map_func_args_list:
        # each entry is a key
        sb, bucket, prefix, obj_name = utils.split_object_url(entry['obj'])
        key = '/'.join([prefix, obj_name]) if prefix else obj_name
        try:
            obj_size = keys_dict[bucket][key]['Size']
            file_meta = keys_dict[bucket][key]['header']
        except Exception:
            raise Exception('Object key "{}" does not exist in "{}" bucket'.format(key, bucket))
    
    
        # Define Max and Min
        max_X = file_meta['MaxX'] # scaled_x.max()
        max_Y = file_meta['MaxY'] # scaled_y.max()
        min_X = file_meta['MinX'] # scaled_x.min()
        min_Y = file_meta['MinY'] # scaled_y.min()
        logger.info("Max X is {}, and Max Y is {}".format(max_X, max_Y))
        logger.info("Min X is {}, and Min Y is {}".format(min_X, min_Y))
    
        # Tiling operation
        # logger.info("start tiling ...")
        mn_X = min_X
        mn_Y = min_Y
        total_partitions = 0
        # print("--------------------------------------------")

        # Delta for both (x and y)
        delta = 0.2 # variancex_y(inFile)
        logger.info('delta = {}'.format(delta))
    
        if num_tiles is not None:
            if num_tiles> 1:
                # Identify tiles
                pointX_offset = round(((max_X - min_X) / rows), 2)   # math.ceil()
                pointY_offset = round(((max_Y - min_Y) / cols), 2) # math.ceil()
                # print(pointX_num, pointY_num)
            
                for y in range(cols):
                    tilY_st = mn_Y + (pointY_offset * y)
                    
                    for x in range(rows):
                        partition = {}
                        tilX_st = mn_X + (pointX_offset * x)
                        logger.info('tile X starts from: {}'.format(tilX_st))
                        logger.info('tile Y starts from: {}'.format(tilY_st))
                    
                        if (tilX_st == mn_X and tilY_st == mn_Y):
                            # The limits of the X-axis values
                            limX_vals = (tilX_st, round((tilX_st + pointX_offset), 2))
                            addupp_X_inf = (round((tilX_st + pointX_offset), 2), round((tilX_st + pointX_offset + delta), 2))
                            addlow_X_inf = (0, 0)
                            logger.info("limitation of X values for tile {}, {} is {}".format(x, y, limX_vals))

                            # The limits of the Y-axis values                
                            limY_vals = (tilY_st, round((tilY_st + pointY_offset), 2))
                            addupp_Y_inf = (round((tilY_st + pointY_offset), 2), round((tilY_st + pointY_offset + delta), 2))
                            addlow_Y_inf = (0, 0)
                            min_X += pointX_offset
                            logger.info("limitation of Y values for tile {}, {} is {}".format(x, y, limY_vals))

                        elif (tilX_st != mn_X and tilY_st == mn_Y):
                            # The limits of the X-axis values
                            limX_vals = (tilX_st, round((tilX_st + pointX_offset), 2)) if not(round((tilX_st + pointX_offset), 2) > max_X) and x < (rows - 1) else (tilX_st, max_X)
                            addupp_X_inf = (round((tilX_st + pointX_offset), 2), round((tilX_st + pointX_offset + delta), 2)) if not(round((tilX_st + pointX_offset), 2) > max_X) and x < (rows - 1) else (0, 0)
                            addlow_X_inf = (tilX_st, round((tilX_st - delta), 2))
                            logger.info("limitation of X values for tile {}, {} is {}".format(x, y, limX_vals))

                            # The limits of the Y-axis values
                            limY_vals = (tilY_st, round((tilY_st + pointY_offset), 2))
                            addupp_Y_inf = (round((tilY_st + pointY_offset), 2), round((tilY_st + pointY_offset + delta), 2))
                            addlow_Y_inf = 0
                            logger.info("limitation of Y values for tile {}, {} is {}".format(x, y, limY_vals))
                    
                        elif (tilX_st == mn_X and tilY_st != mn_Y):
                            # The limits of the X-axis values
                            limX_vals = (tilX_st, round((tilX_st + pointX_offset), 2)) if not(round((tilX_st + pointX_offset), 2) > max_X) and x < (rows - 1) else (tilX_st, max_X)
                            addupp_X_inf = (round((tilX_st + pointX_offset), 2), round((tilX_st + pointX_offset + delta), 2))
                            addlow_X_inf = (0, 0)
                            logger.info("limitation of X values for tile {}, {} is {}".format(x, y, limX_vals))

                            # The limits of the Y-axis values
                            limY_vals = (tilY_st, round((tilY_st + pointY_offset), 2)) if not(round((tilY_st + pointY_offset), 2) > max_Y) and y < (cols - 1) else (tilY_st, max_Y)
                            addupp_Y_inf = (round((tilY_st + pointY_offset), 2), round((tilY_st + pointY_offset + delta), 2)) if not(round((tilY_st + pointY_offset), 2) > max_Y) and y < (cols - 1) else (0, 0)
                            addlow_Y_inf = (tilY_st, round((tilY_st - delta), 2))
                            logger.info("limitation of Y values for tile {}, {} is {}".format(x, y, limY_vals))
                    
                        elif (tilX_st != mn_X and tilY_st != mn_Y):
                            # The limits of the X-axis values
                            limX_vals = (tilX_st, round((tilX_st + pointX_offset), 2)) if not(round((tilX_st + pointX_offset), 2) > max_X) and x < (rows - 1) else (tilX_st, max_X)
                            addupp_X_inf = (round((tilX_st + pointX_offset), 2), round((tilX_st + pointX_offset + delta), 2)) if not(round((tilX_st + pointX_offset), 2) > max_X) and x < (rows - 1) else (0, 0)
                            addlow_X_inf = (tilX_st, round((tilX_st - delta), 2))
                            logger.info("limitation of X values for tile {}, {} is {}".format(x, y, limX_vals))

                            # The limits of the Y-axis values
                            limY_vals = (tilY_st, round((tilY_st + pointY_offset), 2)) if not(round((tilY_st + pointY_offset), 2) > max_Y) and y < (cols - 1)else (tilY_st, max_Y)
                            addupp_Y_inf = (round((tilY_st + pointY_offset), 2), round((tilY_st + pointY_offset + delta), 2)) if not(round((tilY_st + pointY_offset), 2) > max_Y) and y < (cols - 1) else (0, 0)
                            addlow_Y_inf = (tilY_st, round((tilY_st - delta), 2))
                            logger.info("limitation of Y values for tile {}, {} is {}".format(x, y, limY_vals))
                        # print("--------------------------------------------")
                
                    partition = entry.copy()
                    partition['obj'] = CloudObject(sb, bucket, key)
                    partition['obj'].limit_X_values = limX_vals
                    partition['obj'].addupp_X_val = addupp_X_inf
                    partition['obj'].addlow_X_val = addlow_X_inf
                    partition['obj'].limit_Y_values = limY_vals
                    partition['obj'].addupp_Y_val = addupp_Y_inf
                    partition['obj'].addlow_Y_val = addlow_Y_inf
                    partition['obj'].data_byte_range = None
                    partition['obj'].pointsX_offset = pointX_offset
                    partition['obj'].pointsY_offset = pointY_offset
                    partition['obj'].part = total_partitions
                    partitions.append(partition)
                    total_partitions = total_partitions + 1  
        else:
            
            partition = entry.copy()
            partition['obj'] = CloudObject(sb, bucket, key)
            partition['obj'].limit_X_values = None
            partition['obj'].addupp_X_val = None
            partition['obj'].addlow_X_val = None
            partition['obj'].limit_Y_values = None
            partition['obj'].addupp_Y_val = None
            partition['obj'].addlow_Y_val = None
            partition['obj'].data_byte_range = None
            partition['obj'].pointsX_offset = round((max_X - min_X), 2)
            partition['obj'].pointsY_offset = round((max_Y - min_Y), 2)
            partition['obj'].part = total_partitions
            partitions.append(partition)
            total_partitions = total_partitions + 1  
            
            
            # print("****************************************************************************")
        parts_per_object.append(total_partitions)
    return partitions, parts_per_object


