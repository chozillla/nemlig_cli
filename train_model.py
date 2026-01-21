#!/usr/bin/env python3
"""
Train a custom YOLO11 model for produce detection using collected samples.

This script fine-tunes YOLO11 (the newest YOLO version) on your labeled
produce images, then provides instructions for converting to IMX500 format.

YOLO Versions:
    - YOLO11 (2024) - newest, best accuracy (what we use)
    - YOLOv8 (2023) - previous generation
    - Note: YOLOv12 does not exist yet!

Usage:
    python train_model.py

Prerequisites:
    pip install ultralytics

    Or with uv:
    uv pip install ultralytics
"""

import os
import shutil
from pathlib import Path
import random

# Training data paths
DATA_DIR = Path.home() / ".config" / "nemlig" / "training_data"
IMAGES_DIR = DATA_DIR / "images"
LABELS_DIR = DATA_DIR / "labels"
OUTPUT_DIR = DATA_DIR / "trained_model"

# Produce classes (must match nemlig_gui.py PRODUCE_CLASS_IDS)
CLASSES = ["banana", "apple", "orange", "broccoli", "carrot", "pear", "person"]


def check_prerequisites():
    """Check if ultralytics is installed."""
    try:
        from ultralytics import YOLO
        return True
    except ImportError:
        print("ERROR: ultralytics not installed!")
        print("\nInstall with:")
        print("  pip install ultralytics")
        print("  # or")
        print("  uv pip install ultralytics")
        return False


def prepare_dataset():
    """Prepare dataset in YOLO format with train/val split."""
    if not IMAGES_DIR.exists():
        print(f"ERROR: No training images found at {IMAGES_DIR}")
        print("\nCollect samples using the GUI first:")
        print("  1. Run: python nemlig_gui.py")
        print("  2. Point camera at produce")
        print("  3. Click 'Correct Label' if wrong")
        print("  4. Click 'Save Sample'")
        print("  5. Repeat 50-100 times")
        return None

    images = list(IMAGES_DIR.glob("*.jpg"))
    if len(images) < 10:
        print(f"WARNING: Only {len(images)} samples found.")
        print("Recommend collecting at least 50-100 samples for good results.")
        if len(images) < 5:
            print("ERROR: Need at least 5 samples to train.")
            return None

    print(f"Found {len(images)} training samples")

    # Create dataset directory structure
    dataset_dir = DATA_DIR / "dataset"
    train_images = dataset_dir / "train" / "images"
    train_labels = dataset_dir / "train" / "labels"
    val_images = dataset_dir / "val" / "images"
    val_labels = dataset_dir / "val" / "labels"

    for d in [train_images, train_labels, val_images, val_labels]:
        d.mkdir(parents=True, exist_ok=True)

    # Split 80% train, 20% validation
    random.shuffle(images)
    split_idx = int(len(images) * 0.8)
    train_set = images[:split_idx]
    val_set = images[split_idx:]

    print(f"Train: {len(train_set)}, Validation: {len(val_set)}")

    # Copy files
    for img in train_set:
        shutil.copy(img, train_images / img.name)
        label_file = LABELS_DIR / img.with_suffix(".txt").name
        if label_file.exists():
            shutil.copy(label_file, train_labels / label_file.name)

    for img in val_set:
        shutil.copy(img, val_images / img.name)
        label_file = LABELS_DIR / img.with_suffix(".txt").name
        if label_file.exists():
            shutil.copy(label_file, val_labels / label_file.name)

    # Create dataset YAML
    yaml_content = f"""# Nemlig Produce Dataset
path: {dataset_dir}
train: train/images
val: val/images

# Classes
names:
"""
    for i, cls in enumerate(CLASSES):
        yaml_content += f"  {i}: {cls}\n"

    yaml_path = dataset_dir / "dataset.yaml"
    yaml_path.write_text(yaml_content)

    print(f"Dataset prepared at: {dataset_dir}")
    return yaml_path


def train_model(dataset_yaml: Path, epochs: int = 50):
    """Train YOLO11 model on the dataset."""
    from ultralytics import YOLO

    print("\n" + "="*50)
    print("Starting YOLO11 Training")
    print("="*50)

    # Use YOLO11 nano as base (newest, best accuracy)
    model = YOLO("yolo11n.pt")

    # Train
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = model.train(
        data=str(dataset_yaml),
        epochs=epochs,
        imgsz=320,  # Match IMX500 input size
        batch=8,
        patience=10,
        project=str(OUTPUT_DIR),
        name="produce_detector",
        exist_ok=True,
    )

    # Get best model path
    best_model = OUTPUT_DIR / "produce_detector" / "weights" / "best.pt"

    print("\n" + "="*50)
    print("Training Complete!")
    print("="*50)
    print(f"\nBest model saved to: {best_model}")

    return best_model


def export_for_imx500(model_path: Path):
    """Export model for IMX500 (provides instructions)."""
    from ultralytics import YOLO

    print("\n" + "="*50)
    print("Exporting for IMX500")
    print("="*50)

    # First export to ONNX
    model = YOLO(model_path)
    onnx_path = model.export(format="onnx", imgsz=320)

    print(f"\nONNX model exported to: {onnx_path}")

    print("\n" + "="*50)
    print("NEXT STEPS - Convert to IMX500 .rpk format")
    print("="*50)
    print("""
To deploy on IMX500, you need to convert the ONNX model to .rpk format.

Option 1: Use Sony's Model Compression Toolkit (MCT)
------------------------------------------------------
1. Install imx500-tools:
   sudo apt install imx500-tools

2. Visit Sony's Aitrios portal for conversion tools:
   https://developer.aitrios.sony-semicon.com/

3. Or use Ultralytics IMX500 export (if available):
   https://docs.ultralytics.com/integrations/sony-imx500/

Option 2: Use the ONNX model directly with CPU inference
------------------------------------------------------
The trained ONNX model can be used with OpenCV DNN or ONNX Runtime
for CPU-based inference (slower but works without conversion).

Model files:
""")
    print(f"  PyTorch: {model_path}")
    print(f"  ONNX:    {onnx_path}")


def main():
    print("="*50)
    print("Nemlig Produce Model Trainer")
    print("="*50)

    # Check prerequisites
    if not check_prerequisites():
        return 1

    # Prepare dataset
    dataset_yaml = prepare_dataset()
    if dataset_yaml is None:
        return 1

    # Ask user before training
    print(f"\nReady to train on {len(list(IMAGES_DIR.glob('*.jpg')))} samples.")
    response = input("Start training? This may take 10-30 minutes. [y/N]: ").strip().lower()

    if response != 'y':
        print("Training cancelled.")
        return 0

    # Train
    model_path = train_model(dataset_yaml, epochs=50)

    # Export
    if model_path and model_path.exists():
        export_for_imx500(model_path)

    print("\n" + "="*50)
    print("Done!")
    print("="*50)

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
