import tkinter as tk
from tkinter import filedialog
import configparser
import os


def browse_file(entry):
    filename = filedialog.askopenfilename(filetypes=[("GeoJSON files", "*.geojson")])
    entry.delete(0, tk.END)
    entry.insert(0, filename)


def browse_directory(entry):
    directory = filedialog.askdirectory()
    entry.delete(0, tk.END)
    entry.insert(0, directory)


def generate_config():
    config = configparser.ConfigParser()
    config['GLOBAL'] = {
        'aoi': aoi_entry.get(),
        'start_date': start_date_entry.get(),
        'end_date': end_date_entry.get(),
        'assets': assets_entry.get(),
        'target': target_entry.get(),
        'cloud_percentage': cloud_percentage_entry.get(),
        'save_dir': save_dir_entry.get(),
        'grid_x': 0.1,
        'grid_y': 0.1
    }

    config['LC08_L1TOA'] = {
        'source': 'LANDSAT/LC08/C02/T1_TOA',
        'include_bands': 'B1,B2,B3,B4,B5,B6,B7,B8,B9,B10,B11,QA_PIXEL,QA_RADSAT,SAA,SZA,VAA,VZA',
        'resolution': 30,
        'save_dir': 'L1',
        'anonym': 'l8_oli'
    }

    config['LC09_L1TOA'] = {
        'source': 'LANDSAT/LC09/C02/T1_TOA',
        'include_bands': 'B1,B2,B3,B4,B5,B6,B7,B8,B9,B10,B11,QA_PIXEL,QA_RADSAT,SAA,SZA,VAA,VZA',
        'resolution': 30,
        'save_dir': 'L1',
        'anonym': 'l9_oli2'
    }

    config['S2_L1TOA'] = {
        'source': 'COPERNICUS/S2_HARMONIZED',
        'include_bands': 'B1,B2,B3,B4,B5,B6,B7,B8,B8A,B9,B10,B11,B12',
        'resolution': 10,
        'save_dir': 'L1',
        'anonym': 's2_msi',
        'obs_geo_pixel': True
    }

    config['LC08_L2RGB'] = {
        'source': 'LANDSAT/LC08/C02/T1_L2',
        'include_bands': 'SR_B4,SR_B3,SR_B2',
        'resolution': 30,
        'save_dir': 'L2RGB',
        'anonym': 'l8_oli',
        'vmin': 7272.727272727273,
        'vmax': 18181.81818181818
    }

    config['LC09_L2RGB'] = {
        'source': 'LANDSAT/LC09/C02/T1_L2',
        'include_bands': 'SR_B4,SR_B3,SR_B2',
        'resolution': 30,
        'save_dir': 'L2RGB',
        'anonym': 'l9_oli2',
        'vmin': 7272.727272727273,
        'vmax': 18181.81818181818
    }

    config['S2_L2RGB'] = {
        'source': 'COPERNICUS/S2_SR_HARMONIZED',
        'include_bands': 'TCI_R, TCI_G, TCI_B',
        'resolution': 10,
        'save_dir': 'L2RGB',
        'anonym': 's2_msi',
        'vmin': 0,
        'vmax': 255
    }

    config['S1_L1C'] = {
        'source': 'COPERNICUS/S1_GRD',
        'include_bands': 'HH,HV,VV,VH,angle',
        'resolution': 10,
        'anonym': 's1_sar',
        'save_dir': 'L1'
    }

    config['S2_L2SURF'] = {
        'source': 'COPERNICUS/S2_SR_HARMONIZED',
        'include_bands': 'B1,B2,B3,B4,B5,B6,B7,B8,B8A,B9,B10,B11,B12',
        'resolution': 10,
        'save_dir': 'L2_SURF',
        'anonym': 's2_msi',
        'obs_geo_pixel': False
    }

    with open('generated_config.ini', 'w') as configfile:
        config.write(configfile)

    os.system('python main.py -c generated_config.ini')


# Create the main window
root = tk.Tk()
root.title("GEE Downloader Configuration")

# Create and place the labels and entries
tk.Label(root, text="AOI (GeoJSON file):").grid(row=0, column=0, sticky=tk.W)
aoi_entry = tk.Entry(root, width=50)
aoi_entry.grid(row=0, column=1)
tk.Button(root, text="Browse", command=lambda: browse_file(aoi_entry)).grid(row=0, column=2)

tk.Label(root, text="Start Date (YYYY-MM-DD):").grid(row=1, column=0, sticky=tk.W)
start_date_entry = tk.Entry(root, width=50)
start_date_entry.grid(row=1, column=1)

tk.Label(root, text="End Date (YYYY-MM-DD):").grid(row=2, column=0, sticky=tk.W)
end_date_entry = tk.Entry(root, width=50)
end_date_entry.grid(row=2, column=1)

tk.Label(root, text="Assets \n (LC08_L1TOA, LC08_L2RGB, S2_L1TOA, S2_L2RGB, S1_L1C, LC09_L1TOA,LC09_L2RGB,S2_L2SURF):").grid(row=3, column=0, sticky=tk.W)
assets_entry = tk.Entry(root, width=50)
assets_entry.grid(row=3, column=1)

tk.Label(root, text="Target (water/all):").grid(row=4, column=0, sticky=tk.W)
target_entry = tk.Entry(root, width=50)
target_entry.grid(row=4, column=1)

tk.Label(root, text="Cloud Percentage:").grid(row=5, column=0, sticky=tk.W)
cloud_percentage_entry = tk.Entry(root, width=50)
cloud_percentage_entry.grid(row=5, column=1)

tk.Label(root, text="Save Directory:").grid(row=6, column=0, sticky=tk.W)
save_dir_entry = tk.Entry(root, width=50)
save_dir_entry.grid(row=6, column=1)
tk.Button(root, text="Browse", command=lambda: browse_directory(save_dir_entry)).grid(row=6, column=2)

# Create and place the generate button
tk.Button(root, text="Generate Config and Run", command=generate_config).grid(row=7, column=1)

# Run the main loop
root.mainloop()