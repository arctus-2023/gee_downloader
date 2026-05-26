# GEE Download

GEE Download is a Python-based tool to download images from Google Earth Engine (GEE) to local storage in GeoTIFF format for a given Area of Interest (AOI) defined in a GeoJSON file and a specified date range.

## Features

- Download images from GEE in GeoTIFF format.
- Supports multiple satellite data sources.
- Configurable via an `.INI` file.
- Docker support for easy deployment.

## Requirements

### Python Packages

- `rasterio`
- `earthengine-api`
- `google-cloud-bigquery`
- `db_dtypes`

### Google Cloud SDK

Install the `gcloud CLI` on your machine: [Google Cloud SDK Installation](https://cloud.google.com/sdk/docs/install)

### Google Cloud Project Setup

1. Create a new project on Google Cloud.
2. Enable BigQuery and Google Earth Engine APIs in your project.
3. Register your project to use Google Earth Engine: [GEE Registration](https://code.earthengine.google.com/register)

### Authentication

Run the following commands to authenticate `gcloud` in the CLI (inside the `gee_download` directory):

```sh
gcloud auth login
gcloud auth application-default login
```

### Usage:
To run the script, use the following command:
`python main.py -c download.ini`

### Configuration:
The configuration is done via an .INI file. Below is an example configuration (download.ini):

```commandline
[GLOBAL]
aoi = /path/to/your/aoi.geojson
start_date = 2024-08-01
end_date = 2024-12-18
assets = LC08_L1TOA, LC08_L2RGB, S2_L1TOA, S2_L2RGB, S1_L1C, LC09_L1TOA,LC09_L2RGB
target = water
cloud_percentage = 70
grid_x = 0.1
grid_y = 0.1
save_dir = /path/to/save/directory/

[LC08_L1TOA]
source = LANDSAT/LC08/C02/T1_TOA
include_bands = B1,B2,B3,B4,B5,B6,B7,B8,B9,B10,B11,QA_PIXEL,QA_RADSAT,SAA,SZA,VAA,VZA
resolution = 30
save_dir = L1
anonym = l8_oli


[LC09_L1TOA]
source = LANDSAT/LC09/C02/T1_TOA
#NOTE : the priority of exclude_bands is higher than include_bands
include_bands = B1,B2,B3,B4,B5,B6,B7,B8,B9,B10,B11,QA_PIXEL,QA_RADSAT,SAA,SZA,VAA,VZA
resolution = 30
save_dir = L1
anonym = l9_oli2

[S2_L1TOA]
source = COPERNICUS/S2_HARMONIZED
#NOTE : the priority of exclude_bands is higher than include_bands
include_bands = B1,B2,B3,B4,B5,B6,B7,B8,B8A,B9,B10,B11,B12
resolution = 10
save_dir = L1
anonym = s2_msi
### using acurate observation geometry instead of the mean values stored in the meta info of 'properties'
obs_geo_pixel = True

[LC08_L2RGB]
source = LANDSAT/LC08/C02/T1_L2
include_bands = SR_B4,SR_B3,SR_B2
resolution = 30
save_dir = L2RGB
anonym = l8_oli
## var opticalBands = image.select('SR_B.').multiply(0.0000275).add(-0.2); （0.2+0)/0.0000275
vmin = 7272.727272727273
;（0.2+0.3)/0.0000275
vmax = 18181.81818181818


[LC09_L2RGB]
source = LANDSAT/LC09/C02/T1_L2
include_bands = SR_B4,SR_B3,SR_B2
resolution = 30
save_dir = L2RGB
anonym = l9_oli2
vmin = 7272.727272727273
;（0.2+0.3)/0.0000275
vmax = 18181.81818181818

[S2_L2RGB]
source = COPERNICUS/S2_SR_HARMONIZED
include_bands = TCI_R, TCI_G, TCI_B
resolution = 10
save_dir = L2RGB
anonym = s2_msi
vmin = 0
vmax = 255

[S1_L1C]
source = COPERNICUS/S1_GRD
#NOTE : the priority of exclude_bands is higher than include_bands
include_bands = HH,HV,VV,VH,angle
resolution = 10
anonym = s1_sar
save_dir = L1

[S2_L2SURF]
source = COPERNICUS/S2_SR_HARMONIZED
#NOTE : the priority of exclude_bands is higher than include_bands
include_bands = B1,B2,B3,B4,B5,B6,B7,B8,B8A,B9,B10,B11,B12
resolution = 10
save_dir = L2_SURF
anonym = s2_msi
### using acurate observation geometry instead of the mean values stored in the meta info of 'properties'
obs_geo_pixel = False
```
### Docker
Building the Docker Image
To build the Docker image, run the following command:

```sh
docker build -t gee_download .
```

Running the Docker Container
To run the Docker container, run the following command:

```sh   
docker run -it --rm -v $(pwd)/download.ini:/app/download.ini gee_download```
```

### License
This project is licensed under the MIT License. See the LICENSE file for more details.

## Acknowledgments

* [Google Earth Engine](https://earthengine.google.com/)
* [Google Cloud Platform](https://cloud.google.com/)