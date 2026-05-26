import configparser
import re
from tqdm import tqdm
import os
config = configparser.ConfigParser()

#Write desired project config from those inside Download_configs/ directory.
#If you need another config, create a new file under the same directory with appropriate name.
project_config = "download_sdb.ini"
config_path = os.path.join("Download_configs",project_config)

config.read(config_path)

# import required module
import os
# assign directory
ROIs_dir = r'c:\Users\sim-9\MEI_SDB\nonna_ROIs'   #folder of ROIs in the same directory
output_dir = r'c:\Users\sim-9\MEI_SDB\GEE_data_nonna'  #output folder where we download GEE images.

if not os.path.exists(output_dir): os.makedirs(output_dir)
 
# iterate over ROIs inside ROIs_dir
for filename in tqdm(os.listdir(ROIs_dir)):
    aoi = os.path.join(ROIs_dir, filename)
    aoi_number_match = re.search(r"_(\d+)\.geojson", filename)
    aoi_number = aoi_number_match.group(1) if aoi_number_match else "-1" #There shouldn't be parsing error.
    print(f"Running GEE-downloader on AOI: {aoi_number}")
    output = os.path.join(output_dir, f"aoi_{aoi_number}")   #name of folder to save results
    # checking if it is a file
    if os.path.isfile(aoi):
        config['GLOBAL']['aoi'] = aoi      #update aoi
        config['GLOBAL']['save_dir'] = output       #update name of folder
        with open("download.ini", "w") as file_object:
            config.write(file_object)         #saves changes
            
    os.system("python main.py -c download.ini")