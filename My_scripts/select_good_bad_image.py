import os
import tkinter as tk
from tkinter import filedialog
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import rasterio
import numpy as np
import matplotlib.pyplot as plt

class GeoTIFFClassifierApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GeoTIFF Classifier")

        self.canvas_frame = tk.Frame(self.root)
        self.canvas_frame.pack(fill="both", expand=True)

        self.image_list = []
        self.image_index = 0

        self.good_file = open("good_images.txt", "a")
        self.bad_file = open("bad_images.txt", "a")

        self.load_images()

        self.root.bind("<Right>", self.mark_good)
        self.root.bind("<Left>", self.mark_bad)

        if self.image_list:
            self.display_image()
        else:
            print("No TIFF images found.")
            self.root.destroy()

    def load_images(self):
        folder = filedialog.askdirectory(title="Select Folder with TIFF Images")
        if not folder:
            self.root.destroy()
            return
        self.image_list = [os.path.join(folder, f) for f in os.listdir(folder)
                           if f.lower().endswith(('.tif', '.tiff'))]
        self.image_list.sort()

    def display_image(self):
        image_path = self.image_list[self.image_index]
        with rasterio.open(image_path) as src:
            try:
                # Use first 3 bands if available, else just the first band
                count = src.count
                if count >= 3:
                    img = np.stack([src.read(1), src.read(2), src.read(3)], axis=-1)
                    img = np.clip(img / np.percentile(img, 85), 0, 1)  # basic enhancement
                else:
                    img = src.read(1)
                    img = np.clip(img / np.percentile(img, 85), 0, 1)

            except Exception as e:
                print(f"Error reading image {image_path}: {e}")
                self.next_image()
                return

        fig, ax = plt.subplots(figsize=(12, 12))
        ax.imshow(img)
        ax.set_title(os.path.basename(image_path))
        ax.axis('off')

        # Clear previous canvas if it exists
        for widget in self.canvas_frame.winfo_children():
            widget.destroy()

        canvas = FigureCanvasTkAgg(fig, master=self.canvas_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

    def mark_good(self, event):
        self.save_and_next("good")

    def mark_bad(self, event):
        self.save_and_next("bad")

    def save_and_next(self, label):
        current_image = self.image_list[self.image_index]
        filename = os.path.basename(current_image)
        if label == "good":
            self.good_file.write(filename + "\n")
        else:
            self.bad_file.write(filename + "\n")
        self.image_index += 1
        self.next_image()

    def next_image(self):
        if self.image_index < len(self.image_list):
            self.display_image()
        else:
            self.good_file.close()
            self.bad_file.close()
            print("All images reviewed.")
            self.root.quit()

if __name__ == "__main__":
    root = tk.Tk()
    app = GeoTIFFClassifierApp(root)
    root.mainloop()
