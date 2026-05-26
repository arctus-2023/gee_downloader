import os, glob
import numpy as np
import pickle


def get_descriptions_l1c(download_dir):
    info_pickels = glob.glob(os.path.join(download_dir, "*.pickle"))
    descriptions = []
    descriptions_meta = 'product_id, transmitterReceiverPolarisation, instrumentMode'
    acquisition_time = ''
    prefix = ''
    for _pf in info_pickels:
        with open(_pf, 'rb') as f:
            info = pickle.load(f)

        _id = info['id'].split('/')[-1]
        if acquisition_time == '':
            acquisition_time = _id.split('_')[4][:13]
        if prefix == '':
            prefix = _id.split('_')[0]

        transmitterReceiverPolarisation = '_'.join(info['properties']['transmitterReceiverPolarisation'])
        instrumentMode = info['properties']['instrumentMode']
        descriptions.append(','.join([_id, transmitterReceiverPolarisation, instrumentMode]))

        # descriptions.append(self.__extract_id_from_info(_pf))
        #  descriptions_meta = 'product_id'
    return prefix, acquisition_time, descriptions, descriptions_meta

