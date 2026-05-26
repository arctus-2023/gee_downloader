#!/bin/bash

# List files
GOOD_LIST="/media/thomas/Arctus_data2/0_Arctus_Project/19_SAGEPORT/python/gee_downloader/good_images.txt"
BAD_LIST="/media/thomas/Arctus_data2/0_Arctus_Project/19_SAGEPORT/python/gee_downloader/bad_images.txt"

# Root directory where the satellite images are stored
ROOT_DIR="/media/thomas/Arctus_data1/Data/0_ARCTUS_Projects/26_SDB_Niskamoon/wemendji/"
# Directories to search
SEARCH_DIRS=(
  "$ROOT_DIR/L2RGB/s2_msi"
  "$ROOT_DIR/L2_SURF/s2_msi"
  "$ROOT_DIR/L1/s2_msi"
)

# Destination folders
GOOD_DEST="$ROOT_DIR/good"
BAD_DEST="$ROOT_DIR/bad"
mkdir -p "$BAD_DEST"
mkdir -p "$GOOD_DEST/L1_TOA" "$GOOD_DEST/L2RGB" "$GOOD_DEST/L2_SURF"

# Function to move all files containing the extracted ID
move_related_files() {
    local LIST=$1
    local DEST=$2
    local LABEL=$3

    echo "Processing $LABEL list..."

    while IFS= read -r line; do
        ID=${line:9}
        found=0
        for DIR in "${SEARCH_DIRS[@]}"; do
            matches=$(find "$DIR" -maxdepth 1 -type f -name "*${ID}")
            for file in $matches; do
                if [[ "$LABEL" == "GOOD" ]]; then
                    # For good images, maintain folder structure
                    if [[ "$DIR" == *"L2RGB"* ]]; then
                        echo "Moving $(basename "$file") → $DEST/L2RGB/"
                        mv "$file" "$DEST/L2RGB/"
                    elif [[ "$DIR" == *"L2_SURF"* ]]; then
                        echo "Moving $(basename "$file") → $DEST/L2_SURF/"
                        mv "$file" "$DEST/L2_SURF/"
                    elif [[ "$DIR" == *"L1"* ]]; then
                        echo "Moving $(basename "$file") → $DEST/L1_TOA/"
                        mv "$file" "$DEST/L1_TOA/"
                    fi
                else
                    # For bad images, use flat structure
                    echo "Moving $(basename "$file") → $DEST/"
                    mv "$file" "$DEST/"
                fi
                found=1
            done
        done
        if [[ $found -eq 0 ]]; then
            echo "⚠️  No match found for ID: $ID"
        fi
    done < "$LIST"
}

# Run both good and bad
move_related_files "$GOOD_LIST" "$GOOD_DEST" "GOOD"
move_related_files "$BAD_LIST" "$BAD_DEST" "BAD"