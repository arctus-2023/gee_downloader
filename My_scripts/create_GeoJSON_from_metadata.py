import os
import geopandas as gpd
import pandas as pd
import pyproj
from shapely.geometry import Point
from shapely.ops import transform
import json

# Charger le CSV
csv_file = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/MetaData/meta_all.csv"
# csv_file = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/MetaData/test.csv"
df = pd.read_csv(csv_file, sep=",")  # Adaptez le séparateur si nécessaire
output_folder = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/GeoJSON_v2"

# Nettoyer et convertir la colonne 'Date' en format datetime
df['Date'] = df['Date'].apply(lambda x: x.split('T')[0])  # Séparer la date à 'T' et conserver la première partie
df['Date'] = pd.to_datetime(df['Date'], errors='coerce')  # Convertir, avec coercion des erreurs (mettre NaT pour les erreurs)

# Fonction pour créer un buffer de 5 km autour d'un point
def create_buffer(lon, lat, radius_km=5):
    # Créer un point en WGS84 (EPSG:4326)
    point = Point(lon, lat)

    # Convertir en projection métrique (EPSG:3857)
    gdf = gpd.GeoDataFrame(geometry=[point], crs="EPSG:4326")
    gdf = gdf.to_crs("EPSG:3857")

    # Appliquer un buffer de 5 km (5000 m)
    gdf["geometry"] = gdf.buffer(radius_km * 1000)

    # Reprojeter en WGS84 (EPSG:4326)
    gdf = gdf.to_crs("EPSG:4326")

    return gdf.geometry.iloc[0]  # Retourner la géométrie du buffer

# Boucle sur chaque ligne pour générer un fichier GeoJSON
# for _, row in df.iterrows():
#    site_id = row['Site_ID']
#    buffer_geom = create_buffer(row['Longitude'], row['Latitude'])

    # Créer un GeoDataFrame avec les propriétés et la géométrie
#    gdf = gpd.GeoDataFrame([row], geometry=[buffer_geom], crs="EPSG:4326")

    # Sauvegarder en GeoJSON
#    geojson_file = os.path.join(output_folder, f"{site_id}.geojson")
 #   gdf.to_file(geojson_file, driver="GeoJSON")

 #   print(f"Fichier créé : {geojson_file}")



# Grouper par date et par secteur (System_name)
for (date, system_name), group in df.groupby(['Date', 'System_name']):
    # Créer une liste des buffers pour chaque station du groupe
    buffers = [create_buffer(row['Longitude'], row['Latitude']) for _, row in group.iterrows()]

    # Créer un GeoDataFrame avec les données et les buffers
    gdf = gpd.GeoDataFrame(group, geometry=buffers, crs="EPSG:4326")

    # Sauvegarder en GeoJSON pour chaque date et secteur
    geojson_file = os.path.join(output_folder, f"{date}_{system_name}_sector.geojson")
    gdf.to_file(geojson_file, driver="GeoJSON")

    print(f"Fichier créé : {geojson_file}")


print("Tous les fichiers GeoJSON ont été générés avec succès !")

