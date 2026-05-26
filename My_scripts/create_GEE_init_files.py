import configparser
import os
import pandas as pd
import json
from shapely.geometry import Point, mapping

# 📌 Chemins des fichiers
csv_file = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/MetaData/meta_all.csv"
# csv_file = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/MetaData/test.csv"
geojson_dir = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/GeoJSON"
save_ini_dir = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/configs"

# 📌 Création des dossiers si nécessaire
os.makedirs(save_ini_dir, exist_ok=True)

# 📌 Lecture du fichier CSV
df = pd.read_csv(csv_file)

# 📌 Vérifier que les colonnes essentielles sont présentes
required_columns = {"Site_ID", "Longitude", "Latitude", "Date"}
if not required_columns.issubset(df.columns):
    raise ValueError(f"Le fichier CSV doit contenir les colonnes suivantes : {required_columns}")

# 📌 Parcourir chaque ligne du CSV
for _, row in df.iterrows():
    site_id = row["Site_ID"]
    lon = row["Longitude"]
    lat = row["Latitude"]
    date = row["Date"].split("T")[0]  # Garder uniquement YYYY-MM-DD
    aoi = os.path.join(geojson_dir, f"{site_id}.geojson")

    # 📌 Création du fichier .ini
    ini_path = os.path.join(save_ini_dir, f"{site_id}.ini")
    config = configparser.ConfigParser()
    config["GLOBAL"] = {
        "aoi": aoi,
        "start_date": date,
        "end_date": date,
        "assets": "LC08_L1TOA, LC08_L2RGB, S2_L1TOA, S2_L2RGB, LC09_L1TOA,LC09_L2RGB",
        "target": "water",
        "cloud_percentage": "50",
        "grid_x": "0.1",
        "grid_y": "0.1",
        "save_dir": "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/"
    }
    config["LC08_L1TOA"] = {
        "source": "LANDSAT/LC08/C02/T1_TOA",
        "include_bands":  "B1, B2, B3, B4, B5, B6, B7, B8, B9, B10, B11, QA_PIXEL, QA_RADSAT, SAA, SZA, VAA, VZA",
        "resolution": "30",
        "save_dir": "L1",
        "anonym": "l8_oli"
    }
    config["LC09_L1TOA"] = {
        "source": "LANDSAT/LC09/C02/T1_TOA",
        "include_bands":  "B1, B2, B3, B4, B5, B6, B7, B8, B9, B10, B11, QA_PIXEL, QA_RADSAT, SAA, SZA, VAA, VZA",
        "resolution": "30",
        "save_dir": "L1",
        "anonym": "l9_oli2"
    }
    config["S2_L1TOA"] = {
        "source": "COPERNICUS/S2_HARMONIZED",
        "include_bands": "B1, B2, B3, B4, B5, B6, B7, B8, B8A, B9, B10, B11, B12",
        "resolution": "10",
        "save_dir": "L1",
        "anonym": "s2_msi",
        "obs_geo_pixel": "True",
    }
    config["LC08_L2RGB"] ={
        "source": "LANDSAT/LC08/C02/T1_L2",
        "include_bands": "SR_B4, SR_B3, SR_B2",
        "resolution": "30",
        "save_dir": "L2RGB",
        "anonym": "l8_oli",
        "vmin": "7272.727272727273",
        "vmax": "18181.81818181818",
    }
    config["LC09_L2RGB"] ={
        "source": "LANDSAT/LC09/C02/T1_L2",
        "include_bands": "SR_B4, SR_B3, SR_B2",
        "resolution": "30",
        "save_dir": "L2RGB",
        "anonym": "l9_oli2",
        "vmin": "7272.727272727273",
        "vmax": "18181.81818181818",
    }
    config["S2_L2RGB"]={
        "source": "COPERNICUS/S2_SR_HARMONIZED",
        "include_bands": "TCI_R, TCI_G, TCI_B",
        "resolution": "10",
        "save_dir": "L2RGB",
        "anonym": "s2_msi",
        "vmin": "0",
        "vmax": "255"
    }

    with open(ini_path, "w") as configfile:
        config.write(configfile)

    print(f"✅ Fichier INI généré : {ini_path}")

print("🎉 Tous les fichiers INI et GeoJSON ont été créés !")
