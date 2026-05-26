import os
import json

# Dossier contenant les fichiers GeoJSON
input_folder = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/GeoJSON"  # Remplace par le chemin de ton dossier de sortie
output_file = os.path.join(input_folder, "all_samples.geojson")

# Liste tous les fichiers GeoJSON sauf le fichier final s'il existe
geojson_files = [f for f in os.listdir(input_folder) if f.endswith(".geojson") and f != "all_sample.geojson"]

all_features = []

# Lire et fusionner les GeoJSON
for file in geojson_files:
    file_path = os.path.join(input_folder, file)

    with open(file_path, "r") as f:
        data = json.load(f)

        if "features" in data:
            all_features.extend(data["features"])

# Créer le GeoJSON combiné
combined_geojson = {
    "type": "FeatureCollection",
    "features": all_features
}

# Sauvegarder le fichier combiné
with open(output_file, "w") as f:
    json.dump(combined_geojson, f, indent=4)

print(f"GeoJSON combiné enregistré : {output_file}")