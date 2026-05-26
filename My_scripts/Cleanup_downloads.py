import os
import pandas as pd
from collections import defaultdict

# 📂 Dossiers contenant les fichiers
tif_dir = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/L1/s2_msi"
meta_csv = "/mnt/0_ARCTUS_Projects/25_1_AE_Intercomparison/MetaData/meta_all.csv"

# 📥 Charger le fichier meta_all.csv
df_meta = pd.read_csv(meta_csv)

# 📌 Dictionnaire pour regrouper les fichiers par (SENSORID, PRODTYPE, YYYYMMDD, RES)
image_groups = defaultdict(list)

# 📂 Lister tous les fichiers .tif téléchargés
tif_files = [f for f in os.listdir(tif_dir) if f.endswith(".tif")]

# 🏷 Extraire les informations depuis les noms de fichiers
for file in tif_files:
    parts = file.split("_")

    # Vérification minimale que le fichier contient au moins 5 parties
    if len(parts) < 5:
        print(f"⚠️ Nom de fichier non valide: {file}")
        continue  # Ignorer ce fichier et passer au suivant

    # Extraction robuste :
    sensor, prodtype, date = parts[:3]  # Les 3 premiers éléments sont fixes
    res = parts[-1].replace(".tif", "")  # Le dernier élément est la résolution (sans .tif)
    site_id = "_".join(parts[3:-1])  # Tout ce qui est entre date et res est le Site_ID

    key = (sensor, prodtype, date, res)  # Clé unique pour identifier les doublons
    image_groups[key].append((file, site_id))

# 📊 Vérifier les doublons
for key, file_list in image_groups.items():
    if len(file_list) > 1:
        print(f"📌 Doublons trouvés pour {key}: {[f[0] for f in file_list]}")

        # 📍 Trouver tous les sites correspondant à ces fichiers
        matched_sites = set(df_meta[df_meta["Site_ID"].isin([f[1] for f in file_list])]["Site_ID"])

        # 🎯 Conserver un seul fichier (ex: le premier) et s’assurer qu'il couvre tous les sites
        main_file = file_list[0][0]  # Choisir le premier fichier comme référence
        print(f"✅ Conservation de : {main_file}, qui doit inclure {matched_sites}")

        # 🗑 Supprimer les fichiers en double (sauf celui sélectionné)
        for file, site in file_list[1:]:  # Ignorer le premier fichier
            file_path = os.path.join(tif_dir, file)
            os.remove(file_path)
            print(f"🗑 Suppression de : {file}")

print("🎉 Nettoyage terminé !")
