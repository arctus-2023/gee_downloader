


def test_merge_download_dir_obsgeo():
    from gee.utils import merge_download_dir_obsgeo
    from gee import get_obsgeo_s2_l1toa
    dst_crs = merge_download_dir_obsgeo(func_obsgeo=get_obsgeo_s2_l1toa,
                              download_dir='/media/thomas/Arctus_data1/Data/0_ARCTUS_Projects/12_SmartEarth_BSI/data/smart_earth/L1/L1/s2_msi/2023-01-04',output_f='',descriptions='', descriptions_meta='')
    print(dst_crs)



if __name__ == '__main__':
    test_merge_download_dir_obsgeo()
