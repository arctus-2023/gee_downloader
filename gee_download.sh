#!/bin/bash

# Path configuration
SCRIPT_DIR="/media/thomas/Arctus_data2/0_Arctus_Project/19_SAGEPORT/python/gee_downloader/gee_downloader.py"
CONFIG_FILE="/media/thomas/Arctus_data2/0_Arctus_Project/19_SAGEPORT/python/gee_downloader/My_scripts/download_intelliport.ini"
LOG_DIR="/media/thomas/Arctus_data2/0_Arctus_Project/19_SAGEPORT/python/gee_downloader/logs"
LOG_FILE="${LOG_DIR}/gee_download_$(date +\%Y\%m\%d_\%H\%M\%S).log"
ENV_NAME="gee_downloader"
ENV_YML="/media/thomas/Arctus_data2/0_Arctus_Project/19_SAGEPORT/python/gee_downloader/environment.yml"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

echo "Starting GEE download process at $(date)" >> "$LOG_FILE" 2>&1

# Check if conda is available
if ! command -v conda &> /dev/null; then
    echo "Error: conda is not installed or not in PATH" >> "$LOG_FILE" 2>&1
    exit 1
fi

# Initialize conda for bash script
eval "$(conda shell.bash hook)"

# Check if environment exists
if ! conda env list | grep -q "$ENV_NAME"; then
    echo "Environment $ENV_NAME not found. Creating from $ENV_YML..." >> "$LOG_FILE" 2>&1

    if [ -f "$ENV_YML" ]; then
        conda env create -f "$ENV_YML" >> "$LOG_FILE" 2>&1
        if [ $? -ne 0 ]; then
            echo "Failed to create conda environment from $ENV_YML" >> "$LOG_FILE" 2>&1
            exit 1
        fi
        echo "Successfully created $ENV_NAME environment" >> "$LOG_FILE" 2>&1
    else
        echo "Error: Environment YML file not found at $ENV_YML" >> "$LOG_FILE" 2>&1
        exit 1
    fi
fi

# Activate the environment
echo "Activating $ENV_NAME conda environment" >> "$LOG_FILE" 2>&1
conda activate "$ENV_NAME"

if [ $? -ne 0 ]; then
    echo "Failed to activate conda environment $ENV_NAME" >> "$LOG_FILE" 2>&1
    exit 1
fi

echo "Starting GEE download at $(date)" >> "$LOG_FILE" 2>&1

# Run the downloader script with the config file
python -c "
import sys
sys.path.append('$SCRIPT_DIR')
from gee_downloader import GEEDownloader
import configparser

config = configparser.ConfigParser()
config.read('$CONFIG_FILE')

downloader = GEEDownloader(**dict(config['GLOBAL']))
downloader.asset_dic = {}
for section in config.sections():
    if section != 'GLOBAL':
        if 'asset_dic' not in downloader.__dict__:
            downloader.asset_dic = {'image_collection': {}}
        parts = section.split('_')
        if len(parts) > 1:
            prefix = '_'.join(parts[:2]).lower()
            if prefix not in downloader.asset_dic['image_collection']:
                downloader.asset_dic['image_collection'][prefix] = {'sensor_type': 'optical', 'config': {}}
            downloader.asset_dic['image_collection'][prefix]['config'][section] = dict(config[section])

downloader.run()
print('Download completed successfully')
" >> "$LOG_FILE" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "GEE download completed successfully at $(date)" >> "$LOG_FILE" 2>&1
else
    echo "GEE download failed with exit code $EXIT_CODE at $(date)" >> "$LOG_FILE" 2>&1
fi

# Deactivate the conda environment
conda deactivate
