#!/usr/bin/env python3
"""
Nemlig GUI - Tkinter interface for produce scanning and inventory management.

Displays live camera feed from Raspberry Pi AI Camera with object detection overlays.
Automatically adds detected fruits/vegetables to inventory after a debounce period.

Usage:
    python nemlig_gui.py

    Or via justfile:
    just gui
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from pathlib import Path
import sys

# Conditional imports for GUI
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False

# Import from CLI module
from nemlig_cli import (
    Detection, InventoryItem, ShoppingItem,
    COCO_PRODUCE_CLASSES, RESTOCK_THRESHOLDS,
    INVENTORY_FILE, SHOPPING_LIST_FILE, DEFAULT_MODEL,
    CAMERA_AVAILABLE,
    load_inventory, save_inventory,
    load_shopping_list, save_shopping_list,
)

# Conditional camera imports
if CAMERA_AVAILABLE:
    from picamera2 import Picamera2
    from picamera2.devices.imx500 import IMX500

# Constants
WINDOW_WIDTH = 900
WINDOW_HEIGHT = 700
VIDEO_WIDTH = 480
VIDEO_HEIGHT = 360
UPDATE_INTERVAL_MS = 33  # ~30 FPS
DETECTION_DEBOUNCE_S = 2.0  # Seconds before auto-adding

# Training data directory
TRAINING_DATA_DIR = Path.home() / ".config" / "nemlig" / "training_data"
TRAINING_IMAGES_DIR = TRAINING_DATA_DIR / "images"
TRAINING_LABELS_DIR = TRAINING_DATA_DIR / "labels"

# Custom ONNX model path (from training)
CUSTOM_ONNX_MODEL = TRAINING_DATA_DIR / "trained_model" / "produce_detector" / "weights" / "best.onnx"

# Classes for custom model (must match train_model.py)
CUSTOM_MODEL_CLASSES = ["banana", "apple", "orange", "broccoli", "carrot", "pear", "person"]

# Produce classes for labeling (COCO class IDs + custom)
PRODUCE_CLASS_IDS = {
    "banana": 46,
    "apple": 47,
    "orange": 49,
    "broccoli": 50,
    "carrot": 51,
    "pear": 100,  # Custom class (not in COCO, will be trained)
    "person": 0,  # Can also label people
}

# Extended COCO classes for debugging (common objects)
COCO_LABELS = {
    0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 4: "airplane",
    5: "bus", 6: "train", 7: "truck", 8: "boat", 9: "traffic light",
    14: "bird", 15: "cat", 16: "dog", 17: "horse",
    39: "bottle", 41: "cup", 42: "fork", 43: "knife", 44: "spoon", 45: "bowl",
    46: "banana", 47: "apple", 48: "sandwich", 49: "orange", 50: "broccoli",
    51: "carrot", 52: "hot dog", 53: "pizza", 54: "donut", 55: "cake",
    56: "chair", 57: "couch", 58: "potted plant", 59: "bed", 60: "dining table",
    62: "tv", 63: "laptop", 64: "mouse", 65: "remote", 66: "keyboard",
    67: "cell phone", 72: "refrigerator", 73: "book", 74: "clock", 75: "vase",
}


class CameraThread(threading.Thread):
    """Background thread for camera capture and AI detection."""

    def __init__(
        self,
        frame_queue: queue.Queue,
        error_queue: queue.Queue,
        stop_event: threading.Event,
        model_path: str = DEFAULT_MODEL,
        min_confidence: float = 0.5,
        target_fps: int = 15,
    ):
        super().__init__(daemon=True)
        self.frame_queue = frame_queue
        self.error_queue = error_queue
        self.stop_event = stop_event
        self.model_path = model_path
        self.min_confidence = min_confidence
        self.frame_interval = 1.0 / target_fps

    def run(self):
        """Main capture loop."""
        try:
            print("[CameraThread] Loading model (please wait up to 60 seconds)...")
            self.error_queue.put("STATUS:Loading AI model...")

            imx500 = IMX500(self.model_path)
            picam2 = Picamera2(imx500.camera_num)
            print("[CameraThread] Camera initialized")
            self.error_queue.put("STATUS:Initializing camera...")

            config = picam2.create_preview_configuration(
                main={"size": (640, 480), "format": "RGB888"},
                buffer_count=4
            )
            picam2.start(config)

            # Enable auto exposure and auto white balance for better light handling
            picam2.set_controls({
                "AeEnable": True,           # Auto exposure
                "AwbEnable": True,          # Auto white balance
                "AeExposureMode": 0,        # Normal exposure mode
                "AwbMode": 0,               # Auto white balance mode
            })

            # Wait for model to be ready by checking for valid outputs
            print("[CameraThread] Waiting for AI model to load...")
            self.error_queue.put("STATUS:Waiting for AI model (can take 30-60s)...")

            model_ready = False
            start_time = time.time()
            while not model_ready and not self.stop_event.is_set():
                metadata = picam2.capture_metadata()
                np_outputs = imx500.get_outputs(metadata, add_batch=True)
                if np_outputs is not None:
                    model_ready = True
                    print(f"[CameraThread] Model ready after {time.time() - start_time:.1f}s")
                    self.error_queue.put("STATUS:Model ready!")
                else:
                    elapsed = int(time.time() - start_time)
                    if elapsed % 5 == 0:  # Update every 5 seconds
                        self.error_queue.put(f"STATUS:Loading AI model... {elapsed}s")
                    time.sleep(0.1)

            while not self.stop_event.is_set():
                # Capture frame and metadata
                frame = picam2.capture_array()
                metadata = picam2.capture_metadata()

                # Convert BGR to RGB (picamera2 returns BGR despite RGB888 format)
                frame = frame[:, :, ::-1].copy()

                # Process detections
                detections = self._process_detections(imx500, metadata, frame.shape)

                # Put in queue (non-blocking, drop if full)
                try:
                    self.frame_queue.put_nowait((frame, detections))
                except queue.Full:
                    pass  # Drop frame if GUI is behind

                # Small sleep to prevent CPU overload but keep it responsive
                time.sleep(0.01)

        except Exception as e:
            self.error_queue.put(str(e))
        finally:
            try:
                picam2.stop()
            except:
                pass

    def _process_detections(self, imx500, metadata, frame_shape) -> list[Detection]:
        """Extract produce detections from model output."""
        detections = []

        try:
            np_outputs = imx500.get_outputs(metadata, add_batch=True)

            if np_outputs is not None and len(np_outputs) >= 3:
                boxes = np_outputs[0][0]
                scores = np_outputs[1][0]
                classes = np_outputs[2][0]

                for box, score, class_id in zip(boxes, scores, classes):
                    score_val = float(score)
                    if score_val >= self.min_confidence:
                        class_id_int = int(class_id)
                        # Use extended COCO labels, fall back to produce classes
                        if class_id_int in COCO_LABELS:
                            label = COCO_LABELS[class_id_int]
                        elif class_id_int in COCO_PRODUCE_CLASSES:
                            label = COCO_PRODUCE_CLASSES[class_id_int]
                        else:
                            label = f"object_{class_id_int}"

                        detections.append(
                            Detection(
                                label=label,
                                confidence=score_val,
                                box=tuple(box),
                            )
                        )
        except Exception as e:
            # Log error to error queue for debugging
            self.error_queue.put(f"Detection error: {e}")

        return detections


class ONNXCameraThread(threading.Thread):
    """Camera thread using ONNX Runtime for CPU inference with custom model."""

    def __init__(
        self,
        frame_queue: queue.Queue,
        error_queue: queue.Queue,
        stop_event: threading.Event,
        model_path: str,
        min_confidence: float = 0.35,
        target_fps: int = 10,  # Lower FPS due to CPU inference
    ):
        super().__init__(daemon=True)
        self.frame_queue = frame_queue
        self.error_queue = error_queue
        self.stop_event = stop_event
        self.model_path = model_path
        self.min_confidence = min_confidence
        self.frame_interval = 1.0 / target_fps
        self.input_size = 320  # Model was trained at 320x320

    def run(self):
        """Main capture loop with ONNX inference."""
        try:
            print(f"[ONNXThread] Loading model: {self.model_path}", flush=True)
            self.error_queue.put("STATUS:Loading custom ONNX model...")

            # Load ONNX model
            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(self.model_path, sess_options)

            input_name = session.get_inputs()[0].name
            print(f"[ONNXThread] ONNX model loaded, input: {input_name}", flush=True)

            # Initialize camera - use IMX500 interface since that's the AI Camera hardware
            from picamera2 import Picamera2
            from picamera2.devices.imx500 import IMX500

            # Load a dummy model on IMX500 just to initialize the camera
            # We'll ignore its output and use our ONNX model instead
            print("[ONNXThread] Loading IMX500 camera (this takes ~30 seconds)...", flush=True)
            self.error_queue.put("STATUS:Loading IMX500 camera (takes ~30 seconds)...")
            dummy_model = "/usr/share/imx500-models/imx500_network_yolo11n_pp.rpk"
            imx500 = IMX500(dummy_model)
            picam2 = Picamera2(imx500.camera_num)
            print("[ONNXThread] Camera initialized via IMX500", flush=True)
            self.error_queue.put("STATUS:Camera initialized, configuring...")

            config = picam2.create_preview_configuration(
                main={"size": (640, 480), "format": "RGB888"},
                buffer_count=4
            )
            picam2.start(config)

            # Enable auto exposure
            picam2.set_controls({
                "AeEnable": True,
                "AwbEnable": True,
            })

            # Wait a moment for camera to stabilize
            time.sleep(1.0)

            self.error_queue.put("STATUS:Custom model ready (CPU inference)")
            print("[ONNXThread] Ready for inference", flush=True)

            last_inference = 0

            while not self.stop_event.is_set():
                # Capture frame
                frame = picam2.capture_array()
                frame = frame[:, :, ::-1].copy()  # BGR to RGB

                now = time.time()
                detections = []

                # Run inference at target FPS (CPU is slower)
                if now - last_inference >= self.frame_interval:
                    detections = self._run_inference(session, input_name, frame)
                    last_inference = now

                # Put in queue
                try:
                    self.frame_queue.put_nowait((frame, detections))
                except queue.Full:
                    pass

                time.sleep(0.01)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error_queue.put(str(e))
        finally:
            try:
                picam2.stop()
            except:
                pass

    def _run_inference(self, session, input_name, frame) -> list[Detection]:
        """Run YOLO inference on frame using ONNX Runtime."""
        detections = []

        try:
            h, w = frame.shape[:2]

            # Preprocess: resize, normalize, add batch dim
            img = cv2.resize(frame, (self.input_size, self.input_size))
            img = img.astype(np.float32) / 255.0
            img = np.transpose(img, (2, 0, 1))  # HWC to CHW
            img = np.expand_dims(img, 0)  # Add batch dimension

            # Run inference
            outputs = session.run(None, {input_name: img})

            # Parse YOLO output (format: [batch, num_classes+4, num_predictions])
            # Output shape is typically [1, 11, 2100] for 7 classes + 4 box coords
            output = outputs[0][0]  # Remove batch dim -> [11, 2100]

            # Transpose to [2100, 11] for easier processing
            output = output.T

            # For each prediction: [x_center, y_center, width, height, class0_conf, class1_conf, ...]
            for pred in output:
                x_center, y_center, bw, bh = pred[:4]
                class_scores = pred[4:]

                # Get best class
                class_id = np.argmax(class_scores)
                confidence = class_scores[class_id]

                if confidence >= self.min_confidence:
                    # Convert to normalized box coords
                    x1 = (x_center - bw / 2) / self.input_size
                    y1 = (y_center - bh / 2) / self.input_size
                    x2 = (x_center + bw / 2) / self.input_size
                    y2 = (y_center + bh / 2) / self.input_size

                    # Clamp to valid range
                    x1 = max(0.0, min(1.0, x1))
                    y1 = max(0.0, min(1.0, y1))
                    x2 = max(0.0, min(1.0, x2))
                    y2 = max(0.0, min(1.0, y2))

                    # Get label from custom classes
                    if class_id < len(CUSTOM_MODEL_CLASSES):
                        label = CUSTOM_MODEL_CLASSES[class_id]
                    else:
                        label = f"class_{class_id}"

                    detections.append(Detection(
                        label=label,
                        confidence=float(confidence),
                        box=(x1, y1, x2, y2)
                    ))

            # Apply NMS to reduce overlapping boxes
            detections = self._apply_nms(detections, iou_threshold=0.5)

        except Exception as e:
            self.error_queue.put(f"Inference error: {e}")

        return detections

    def _apply_nms(self, detections: list[Detection], iou_threshold: float = 0.5) -> list[Detection]:
        """Apply Non-Maximum Suppression to reduce overlapping detections."""
        if not detections:
            return []

        # Sort by confidence (descending)
        detections = sorted(detections, key=lambda d: d.confidence, reverse=True)

        keep = []
        while detections:
            best = detections.pop(0)
            keep.append(best)

            # Remove overlapping boxes of same class
            remaining = []
            for det in detections:
                if det.label != best.label or self._iou(best.box, det.box) < iou_threshold:
                    remaining.append(det)
            detections = remaining

        return keep

    def _iou(self, box1, box2) -> float:
        """Calculate Intersection over Union."""
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2])
        y2 = min(box1[3], box2[3])

        inter = max(0, x2 - x1) * max(0, y2 - y1)

        area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])

        union = area1 + area2 - inter

        return inter / union if union > 0 else 0


class DataCollector:
    """Collects and saves labeled training data for model fine-tuning."""

    def __init__(self):
        # Create directories if needed
        TRAINING_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        TRAINING_LABELS_DIR.mkdir(parents=True, exist_ok=True)
        self.sample_count = self._count_existing_samples()

    def _count_existing_samples(self) -> int:
        """Count existing training samples."""
        return len(list(TRAINING_IMAGES_DIR.glob("*.jpg")))

    def save_sample(self, frame: "np.ndarray", detections: list, corrections: dict = None):
        """
        Save a training sample with labels.

        Args:
            frame: The image frame (numpy array)
            detections: List of Detection objects
            corrections: Dict mapping detection index to corrected label
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        image_path = TRAINING_IMAGES_DIR / f"sample_{timestamp}.jpg"
        label_path = TRAINING_LABELS_DIR / f"sample_{timestamp}.txt"

        # Save image
        if CV2_AVAILABLE:
            # Convert RGB to BGR for cv2
            cv2.imwrite(str(image_path), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        else:
            Image.fromarray(frame).save(str(image_path))

        # Save labels in YOLO format: class_id x_center y_center width height (normalized)
        h, w = frame.shape[:2]
        with open(label_path, "w") as f:
            for i, det in enumerate(detections):
                # Apply correction if provided
                if corrections and i in corrections:
                    label = corrections[i]
                else:
                    label = det.label

                # Skip if label not in our produce classes
                if label not in PRODUCE_CLASS_IDS:
                    continue

                class_id = list(PRODUCE_CLASS_IDS.keys()).index(label)  # Use 0-indexed for training

                # Convert box to YOLO format (x_center, y_center, width, height) normalized
                x1, y1, x2, y2 = det.box
                if max(x1, y1, x2, y2) <= 1.5:  # Already normalized (allow slight overflow)
                    # Clamp to valid range 0-1
                    x1 = max(0.0, min(1.0, x1))
                    y1 = max(0.0, min(1.0, y1))
                    x2 = max(0.0, min(1.0, x2))
                    y2 = max(0.0, min(1.0, y2))
                    x_center = (x1 + x2) / 2
                    y_center = (y1 + y2) / 2
                    box_w = x2 - x1
                    box_h = y2 - y1
                else:  # Pixel coordinates
                    x_center = max(0.0, min(1.0, (x1 + x2) / 2 / w))
                    y_center = max(0.0, min(1.0, (y1 + y2) / 2 / h))
                    box_w = max(0.0, min(1.0, (x2 - x1) / w))
                    box_h = max(0.0, min(1.0, (y2 - y1) / h))

                # Skip invalid boxes (too small)
                if box_w < 0.01 or box_h < 0.01:
                    continue

                f.write(f"{class_id} {x_center:.6f} {y_center:.6f} {box_w:.6f} {box_h:.6f}\n")

        self.sample_count += 1
        return image_path

    def get_sample_count(self) -> int:
        """Get the number of collected samples."""
        return self.sample_count

    def get_data_path(self) -> Path:
        """Get the training data directory path."""
        return TRAINING_DATA_DIR


class DetectionSmoother:
    """Smooths detections to reduce flickering."""

    def __init__(self, persistence_frames: int = 10):
        self.persistence_frames = persistence_frames
        self.detection_history: dict[str, dict] = {}  # label -> {box, confidence, frames_left}

    def smooth(self, detections: list[Detection]) -> list[Detection]:
        """Smooth detections by persisting them for several frames."""
        current_labels = {}

        # Update with current detections
        for det in detections:
            current_labels[det.label] = det
            if det.label in self.detection_history:
                # Update existing - smooth the box position
                old = self.detection_history[det.label]
                # Interpolate box position for smoother movement
                old_box = old["box"]
                new_box = det.box
                smoothed_box = tuple(
                    old_box[i] * 0.3 + new_box[i] * 0.7  # 70% new, 30% old
                    for i in range(4)
                )
                self.detection_history[det.label] = {
                    "box": smoothed_box,
                    "confidence": det.confidence,
                    "frames_left": self.persistence_frames
                }
            else:
                # New detection
                self.detection_history[det.label] = {
                    "box": det.box,
                    "confidence": det.confidence,
                    "frames_left": self.persistence_frames
                }

        # Decrement frames for detections not seen this frame
        labels_to_remove = []
        for label in self.detection_history:
            if label not in current_labels:
                self.detection_history[label]["frames_left"] -= 1
                if self.detection_history[label]["frames_left"] <= 0:
                    labels_to_remove.append(label)

        for label in labels_to_remove:
            del self.detection_history[label]

        # Build smoothed detection list
        smoothed = []
        for label, data in self.detection_history.items():
            smoothed.append(Detection(
                label=label,
                confidence=data["confidence"],
                box=data["box"]
            ))

        return smoothed


class AutoAddManager:
    """Manages automatic inventory updates with debouncing."""

    def __init__(self, debounce_seconds: float = DETECTION_DEBOUNCE_S):
        self.debounce_seconds = debounce_seconds
        self.first_seen: dict[str, float] = {}
        self.confirmed: set[str] = set()

    def process_detections(self, detections: list[Detection]) -> list[str]:
        """
        Process new detections, return items ready to confirm.

        Items must be continuously detected for debounce_seconds before
        being returned as ready.
        """
        now = time.time()
        ready = []

        current_labels = {d.label for d in detections}

        # Remove items no longer visible
        for label in list(self.first_seen.keys()):
            if label not in current_labels:
                del self.first_seen[label]
                self.confirmed.discard(label)

        # Check each current detection
        for label in current_labels:
            if label not in self.first_seen:
                self.first_seen[label] = now

            # Check if seen for debounce period and not yet confirmed
            if label not in self.confirmed:
                if now - self.first_seen[label] >= self.debounce_seconds:
                    ready.append(label)
                    self.confirmed.add(label)

        return ready

    def get_progress(self, label: str) -> float:
        """Get debounce progress (0.0 to 1.0) for a label."""
        if label not in self.first_seen:
            return 0.0
        if label in self.confirmed:
            return 1.0

        elapsed = time.time() - self.first_seen[label]
        return min(1.0, elapsed / self.debounce_seconds)

    def reset(self):
        """Reset all tracking state."""
        self.first_seen.clear()
        self.confirmed.clear()


class NemligGUI(tk.Tk):
    """Main application window."""

    def __init__(self, use_custom_model: bool = False):
        super().__init__()
        self.use_custom_model = use_custom_model
        title = "Nemlig Produce Scanner"
        if use_custom_model:
            title += " (Custom Model)"
        self.title(title)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(800, 600)

        # State
        self.frame_queue = queue.Queue(maxsize=2)
        self.error_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.camera_thread = None
        self.auto_add = AutoAddManager()
        self.detection_smoother = DetectionSmoother(persistence_frames=15)  # Smooth detections
        self.data_collector = DataCollector()  # For collecting training data
        self.current_detections: list[Detection] = []
        self.current_frame = None  # Store current frame for saving
        self.label_corrections: dict[int, str] = {}  # Index -> corrected label
        self.label_text_rects: list[tuple] = []  # [(x1, y1, x2, y2, Detection), ...] for click detection
        self.fps_counter = 0
        self.fps_time = time.time()
        self.current_fps = 0.0

        # Check dependencies
        if not PIL_AVAILABLE:
            messagebox.showerror(
                "Missing Dependency",
                "Pillow is required for the GUI.\n\nInstall with:\npip install Pillow"
            )
            self.destroy()
            return

        if not CV2_AVAILABLE:
            messagebox.showwarning(
                "Missing Dependency",
                "OpenCV not found. Bounding boxes will not be drawn.\n\n"
                "Install with:\nsudo apt install python3-opencv"
            )

        # Build UI
        self._create_widgets()
        self._load_initial_data()

        # Start camera if available
        if self._check_camera():
            self._start_camera()
        else:
            self._show_camera_unavailable()

        # Start update loops
        self._update_video()
        self._check_errors()

        # Cleanup on close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_widgets(self):
        """Create all UI widgets."""
        # Configure grid
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # Main container
        main_frame = ttk.Frame(self, padding=10)
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.columnconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(0, weight=3)
        main_frame.rowconfigure(1, weight=2)

        # === Upper Section ===

        # Camera frame (left)
        camera_frame = ttk.LabelFrame(main_frame, text="Camera Feed", padding=5)
        camera_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 5))

        self.camera_canvas = tk.Canvas(
            camera_frame,
            width=VIDEO_WIDTH,
            height=VIDEO_HEIGHT,
            bg="black"
        )
        self.camera_canvas.pack(expand=True, fill="both")

        # Bind click on canvas to label detection
        self.camera_canvas.bind("<Button-1>", self._on_canvas_click)

        # Detection frame (right)
        detection_frame = ttk.LabelFrame(main_frame, text="Detected Items", padding=5)
        detection_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=(0, 5))
        detection_frame.rowconfigure(0, weight=1)
        detection_frame.columnconfigure(0, weight=1)

        # Detection treeview
        columns = ("item", "confidence", "progress")
        self.detection_tree = ttk.Treeview(
            detection_frame,
            columns=columns,
            show="headings",
            selectmode="extended"
        )
        self.detection_tree.heading("item", text="Item")
        self.detection_tree.heading("confidence", text="Confidence")
        self.detection_tree.heading("progress", text="Auto-Add")
        self.detection_tree.column("item", width=100)
        self.detection_tree.column("confidence", width=80)
        self.detection_tree.column("progress", width=80)
        self.detection_tree.grid(row=0, column=0, sticky="nsew")

        # Scrollbar for detection tree
        det_scroll = ttk.Scrollbar(detection_frame, orient="vertical", command=self.detection_tree.yview)
        det_scroll.grid(row=0, column=1, sticky="ns")
        self.detection_tree.configure(yscrollcommand=det_scroll.set)

        # Detection buttons
        det_btn_frame = ttk.Frame(detection_frame)
        det_btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        ttk.Button(
            det_btn_frame,
            text="Add to Inventory",
            command=self._add_selected_to_inventory
        ).pack(side="left", padx=(0, 5))

        ttk.Button(
            det_btn_frame,
            text="Add to Shopping List",
            command=self._add_selected_to_shopping
        ).pack(side="left")

        # Training/Labeling section
        training_frame = ttk.LabelFrame(detection_frame, text="Train Model", padding=5)
        training_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        # Correct label button
        ttk.Button(
            training_frame,
            text="Correct Label",
            command=self._correct_selected_label
        ).pack(side="left", padx=(0, 5))

        # Save sample button
        ttk.Button(
            training_frame,
            text="Save Sample",
            command=self._save_training_sample
        ).pack(side="left", padx=(0, 5))

        # Sample count label
        self.sample_count_label = ttk.Label(
            training_frame,
            text=f"Samples: {self.data_collector.get_sample_count()}"
        )
        self.sample_count_label.pack(side="left", padx=(10, 0))

        # Bind double-click to correct label
        self.detection_tree.bind("<Double-1>", lambda e: self._correct_selected_label())

        # === Lower Section ===

        # Inventory frame (left)
        inventory_frame = ttk.LabelFrame(main_frame, text="Inventory", padding=5)
        inventory_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(5, 0))
        inventory_frame.rowconfigure(0, weight=1)
        inventory_frame.columnconfigure(0, weight=1)

        # Inventory treeview
        inv_columns = ("item", "quantity", "status")
        self.inventory_tree = ttk.Treeview(
            inventory_frame,
            columns=inv_columns,
            show="headings",
            selectmode="browse"
        )
        self.inventory_tree.heading("item", text="Item")
        self.inventory_tree.heading("quantity", text="Qty")
        self.inventory_tree.heading("status", text="Status")
        self.inventory_tree.column("item", width=100)
        self.inventory_tree.column("quantity", width=50)
        self.inventory_tree.column("status", width=80)
        self.inventory_tree.grid(row=0, column=0, sticky="nsew")

        inv_scroll = ttk.Scrollbar(inventory_frame, orient="vertical", command=self.inventory_tree.yview)
        inv_scroll.grid(row=0, column=1, sticky="ns")
        self.inventory_tree.configure(yscrollcommand=inv_scroll.set)

        # Inventory buttons
        inv_btn_frame = ttk.Frame(inventory_frame)
        inv_btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        ttk.Button(
            inv_btn_frame,
            text="Refresh",
            command=self._refresh_inventory_display
        ).pack(side="left", padx=(0, 5))

        ttk.Button(
            inv_btn_frame,
            text="Clear All",
            command=self._clear_inventory
        ).pack(side="left")

        # Shopping list frame (right)
        shopping_frame = ttk.LabelFrame(main_frame, text="Shopping List", padding=5)
        shopping_frame.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(5, 0))
        shopping_frame.rowconfigure(0, weight=1)
        shopping_frame.columnconfigure(0, weight=1)

        # Shopping list treeview
        shop_columns = ("item", "quantity")
        self.shopping_tree = ttk.Treeview(
            shopping_frame,
            columns=shop_columns,
            show="headings",
            selectmode="browse"
        )
        self.shopping_tree.heading("item", text="Item")
        self.shopping_tree.heading("quantity", text="Qty")
        self.shopping_tree.column("item", width=120)
        self.shopping_tree.column("quantity", width=50)
        self.shopping_tree.grid(row=0, column=0, sticky="nsew")

        shop_scroll = ttk.Scrollbar(shopping_frame, orient="vertical", command=self.shopping_tree.yview)
        shop_scroll.grid(row=0, column=1, sticky="ns")
        self.shopping_tree.configure(yscrollcommand=shop_scroll.set)

        # Shopping list buttons
        shop_btn_frame = ttk.Frame(shopping_frame)
        shop_btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        ttk.Button(
            shop_btn_frame,
            text="Refresh",
            command=self._refresh_shopping_display
        ).pack(side="left", padx=(0, 5))

        ttk.Button(
            shop_btn_frame,
            text="Clear All",
            command=self._clear_shopping
        ).pack(side="left")

        # === Status Bar ===
        status_frame = ttk.Frame(main_frame)
        status_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        self.status_label = ttk.Label(status_frame, text="Initializing...")
        self.status_label.pack(side="left")

        self.fps_label = ttk.Label(status_frame, text="")
        self.fps_label.pack(side="right")

    def _check_camera(self) -> bool:
        """Check if camera hardware and libraries are available."""
        if self.use_custom_model:
            # For custom model, need ONNX and IMX500 camera hardware
            if not ONNX_AVAILABLE:
                print("[Check] ONNX Runtime not available")
                return False
            if not CUSTOM_ONNX_MODEL.exists():
                print(f"[Check] Custom model not found: {CUSTOM_ONNX_MODEL}")
                return False
            # Need IMX500 camera interface (the AI Camera IS an IMX500)
            if not CAMERA_AVAILABLE:
                print("[Check] Camera libraries not available")
                return False
            print("[Check] Custom model mode OK")
            return True

        # For IMX500 mode
        if not CAMERA_AVAILABLE:
            return False

        # Check model file exists
        if not Path(DEFAULT_MODEL).exists():
            return False

        return True

    def _start_camera(self):
        """Start the camera thread."""
        self.status_label.config(text="Starting camera...")

        if self.use_custom_model:
            # Use custom ONNX model with CPU inference
            print(f"[GUI] Using custom ONNX model: {CUSTOM_ONNX_MODEL.name}", flush=True)
            # Show loading message on canvas immediately
            self.camera_canvas.delete("all")
            self.camera_canvas.create_text(
                VIDEO_WIDTH // 2,
                VIDEO_HEIGHT // 2,
                text="Loading...\n\nInitializing custom model\n(this takes ~30 seconds)",
                font=("TkDefaultFont", 14),
                fill="white",
                justify=tk.CENTER
            )
            self.update_idletasks()  # Force display update
            self.camera_thread = ONNXCameraThread(
                frame_queue=self.frame_queue,
                error_queue=self.error_queue,
                stop_event=self.stop_event,
                model_path=str(CUSTOM_ONNX_MODEL),
                min_confidence=0.15,  # Lower threshold to catch weak fruit detections
            )
            self.camera_thread.start()
            self.status_label.config(text="Custom model active (CPU)")
        else:
            # Use IMX500 NPU with pre-trained model
            yolo11_model = "/usr/share/imx500-models/imx500_network_yolo11n_pp.rpk"
            yolov8_model = "/usr/share/imx500-models/imx500_network_yolov8n_pp.rpk"

            if Path(yolo11_model).exists():
                model_to_use = yolo11_model  # Prefer YOLO11 (newest)
            elif Path(yolov8_model).exists():
                model_to_use = yolov8_model
            else:
                model_to_use = DEFAULT_MODEL
            print(f"[GUI] Using IMX500 model: {Path(model_to_use).name}")

            self.camera_thread = CameraThread(
                frame_queue=self.frame_queue,
                error_queue=self.error_queue,
                stop_event=self.stop_event,
                model_path=model_to_use,
                min_confidence=0.35,
            )
            self.camera_thread.start()
            self.status_label.config(text="IMX500 active (NPU)")

    def _show_camera_unavailable(self):
        """Show placeholder when camera not available."""
        self.camera_canvas.delete("all")

        message = (
            "Camera Not Available\n\n"
            "To use camera features:\n"
            "1. Connect Raspberry Pi AI Camera (IMX500)\n"
            "2. Install: sudo apt install python3-picamera2\n"
            "3. Install: sudo apt install imx500-all imx500-models"
        )

        # Center the message
        self.camera_canvas.create_text(
            VIDEO_WIDTH // 2,
            VIDEO_HEIGHT // 2,
            text=message,
            font=("TkDefaultFont", 11),
            fill="gray",
            justify=tk.CENTER
        )

        self.status_label.config(text="Camera not available - using manual mode")

    def _update_video(self):
        """Poll frame queue and update display."""
        # Drain queue to get latest frame (drop intermediate frames)
        frame = None
        detections = []

        while True:
            try:
                frame, detections = self.frame_queue.get_nowait()
            except queue.Empty:
                break

        if frame is not None:
            # Store frame for training data collection
            self.current_frame = frame.copy()

            # Smooth detections to reduce flickering
            detections = self.detection_smoother.smooth(detections)
            self.current_detections = detections

            # Update FPS counter
            self.fps_counter += 1
            now = time.time()
            if now - self.fps_time >= 1.0:
                self.current_fps = self.fps_counter / (now - self.fps_time)
                self.fps_counter = 0
                self.fps_time = now
                self.fps_label.config(text=f"{self.current_fps:.1f} FPS")

            # Draw bounding boxes if CV2 available
            if CV2_AVAILABLE:
                # Always draw detection count on frame for debugging
                cv2.putText(frame, f"Detections: {len(detections)}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                if detections:
                    frame = self._draw_detections(frame, detections)

            # Convert to PhotoImage
            try:
                image = Image.fromarray(frame)
                image = image.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)

                # Update canvas
                self.camera_canvas.delete("all")
                self.camera_canvas.create_image(0, 0, anchor=tk.NW, image=photo)
                self.camera_canvas.image = photo  # Keep reference!
            except Exception:
                pass

            # Update detection list
            self._update_detection_list(detections)

            # Update status with detection count
            produce_count = sum(1 for d in detections if d.label in ("banana", "apple", "orange", "broccoli", "carrot"))
            if detections:
                self.status_label.config(text=f"Scanning - {len(detections)} objects ({produce_count} produce)")

            # Process auto-add (only for produce items)
            produce_detections = [d for d in detections if d.label in COCO_PRODUCE_CLASSES.values()]
            ready_items = self.auto_add.process_detections(produce_detections)
            for label in ready_items:
                self._auto_add_to_inventory(label)

        # Schedule next update
        self.after(UPDATE_INTERVAL_MS, self._update_video)

    def _draw_detections(self, frame: "np.ndarray", detections: list[Detection]) -> "np.ndarray":
        """Draw bounding boxes and labels on frame. Also stores text label positions for click detection."""
        frame_copy = frame.copy()
        h, w = frame_copy.shape[:2]

        # Clear and rebuild text label rectangles for click detection
        self.label_text_rects = []

        for det in detections:
            x1, y1, x2, y2 = det.box

            # Handle normalized coordinates
            if x2 <= 1.0:
                x1, y1, x2, y2 = int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
            else:
                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)

            # Clamp to frame bounds
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)

            # Get progress for color (green = confirmed, yellow = pending)
            progress = self.auto_add.get_progress(det.label)
            if progress >= 1.0:
                color = (0, 255, 0)  # Green
            else:
                # Interpolate yellow to green
                green = int(255 * progress)
                color = (255 - green, 255, 0)

            # Draw rectangle
            cv2.rectangle(frame_copy, (x1, y1), (x2, y2), color, 2)

            # Draw label background
            label = f"{det.label}: {det.confidence:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            label_x1 = x1
            label_y1 = y1 - th - 10
            label_x2 = x1 + tw + 4
            label_y2 = y1
            cv2.rectangle(frame_copy, (label_x1, label_y1), (label_x2, label_y2), color, -1)

            # Store text label rectangle for click detection (in pixel coordinates)
            self.label_text_rects.append((label_x1, label_y1, label_x2, label_y2, det))

            # Draw label text
            cv2.putText(
                frame_copy, label, (x1 + 2, y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2
            )

        return frame_copy

    def _update_detection_list(self, detections: list[Detection]):
        """Update detection Treeview with current detections."""
        # Clear existing items
        for item in self.detection_tree.get_children():
            self.detection_tree.delete(item)

        # Group by label, keep highest confidence
        best_detections: dict[str, Detection] = {}
        for det in detections:
            if det.label not in best_detections or det.confidence > best_detections[det.label].confidence:
                best_detections[det.label] = det

        # Add to treeview
        for label in sorted(best_detections.keys()):
            det = best_detections[label]
            progress = self.auto_add.get_progress(label)

            if progress >= 1.0:
                progress_str = "Added"
            else:
                progress_str = f"{progress * 100:.0f}%"

            self.detection_tree.insert(
                "", "end",
                values=(label, f"{det.confidence:.2f}", progress_str)
            )

    def _auto_add_to_inventory(self, label: str):
        """Automatically add a detected item to inventory."""
        inventory = load_inventory()
        now = datetime.now().isoformat(timespec="seconds")

        if label in inventory:
            inventory[label].quantity += 1
            inventory[label].last_seen = now
        else:
            inventory[label] = InventoryItem(name=label, quantity=1, last_seen=now)

        save_inventory(inventory)
        self._refresh_inventory_display()

        # Update status
        self.status_label.config(text=f"Auto-added: {label}")

    def _add_selected_to_inventory(self):
        """Add selected detections to inventory."""
        selection = self.detection_tree.selection()
        if not selection:
            return

        inventory = load_inventory()
        now = datetime.now().isoformat(timespec="seconds")
        added = []

        for item_id in selection:
            values = self.detection_tree.item(item_id, "values")
            label = values[0]

            if label in inventory:
                inventory[label].quantity += 1
                inventory[label].last_seen = now
            else:
                inventory[label] = InventoryItem(name=label, quantity=1, last_seen=now)

            added.append(label)
            self.auto_add.confirmed.add(label)  # Mark as confirmed

        save_inventory(inventory)
        self._refresh_inventory_display()
        self.status_label.config(text=f"Added to inventory: {', '.join(added)}")

    def _add_selected_to_shopping(self):
        """Add selected detections to shopping list."""
        selection = self.detection_tree.selection()
        if not selection:
            return

        shopping_list = load_shopping_list()
        now = datetime.now().isoformat(timespec="seconds")
        added = []

        for item_id in selection:
            values = self.detection_tree.item(item_id, "values")
            label = values[0]

            if label in shopping_list:
                shopping_list[label].quantity += 1
            else:
                shopping_list[label] = ShoppingItem(name=label, quantity=1, added_date=now)

            added.append(label)

        save_shopping_list(shopping_list)
        self._refresh_shopping_display()
        self.status_label.config(text=f"Added to shopping list: {', '.join(added)}")

    def _load_initial_data(self):
        """Load initial inventory and shopping list data."""
        self._refresh_inventory_display()
        self._refresh_shopping_display()

    def _refresh_inventory_display(self):
        """Reload and display inventory."""
        # Clear existing items
        for item in self.inventory_tree.get_children():
            self.inventory_tree.delete(item)

        inventory = load_inventory()

        for item in sorted(inventory.values(), key=lambda x: x.name):
            threshold = RESTOCK_THRESHOLDS.get(item.name, 2)

            if item.quantity < threshold:
                status = "LOW"
            else:
                status = "OK"

            self.inventory_tree.insert(
                "", "end",
                values=(item.name, item.quantity, status)
            )

    def _refresh_shopping_display(self):
        """Reload and display shopping list."""
        # Clear existing items
        for item in self.shopping_tree.get_children():
            self.shopping_tree.delete(item)

        shopping_list = load_shopping_list()

        for item in sorted(shopping_list.values(), key=lambda x: x.name):
            self.shopping_tree.insert(
                "", "end",
                values=(item.name, item.quantity)
            )

    def _clear_inventory(self):
        """Clear all inventory items."""
        if messagebox.askyesno("Confirm", "Clear all inventory items?"):
            if INVENTORY_FILE.exists():
                INVENTORY_FILE.unlink()
            self._refresh_inventory_display()
            self.status_label.config(text="Inventory cleared")

    def _clear_shopping(self):
        """Clear all shopping list items."""
        if messagebox.askyesno("Confirm", "Clear all shopping list items?"):
            if SHOPPING_LIST_FILE.exists():
                SHOPPING_LIST_FILE.unlink()
            self._refresh_shopping_display()
            self.status_label.config(text="Shopping list cleared")

    def _check_errors(self):
        """Check for errors and status updates from camera thread."""
        try:
            message = self.error_queue.get_nowait()
            if message.startswith("STATUS:"):
                # Status update, not error
                status = message[7:]  # Remove "STATUS:" prefix
                self.status_label.config(text=status)
                # Also show on canvas while loading (no frames yet)
                if self.frame_queue.empty():
                    self.camera_canvas.delete("all")
                    self.camera_canvas.create_text(
                        VIDEO_WIDTH // 2,
                        VIDEO_HEIGHT // 2,
                        text=f"Loading...\n\n{status}",
                        font=("TkDefaultFont", 14),
                        fill="white",
                        justify=tk.CENTER
                    )
            else:
                # Actual error
                self._handle_camera_error(message)
        except queue.Empty:
            pass

        # Schedule next check
        self.after(100, self._check_errors)  # Check more frequently

    def _handle_camera_error(self, error_msg: str):
        """Handle camera errors from the camera thread."""
        self.camera_thread = None
        self.camera_canvas.delete("all")

        self.camera_canvas.create_text(
            VIDEO_WIDTH // 2,
            VIDEO_HEIGHT // 2,
            text=f"Camera Error\n\n{error_msg}\n\n[Click to Retry]",
            font=("TkDefaultFont", 11),
            fill="red",
            justify=tk.CENTER,
            tags="error"
        )
        self.camera_canvas.tag_bind("error", "<Button-1>", lambda e: self._retry_camera())

        self.status_label.config(text=f"Camera error: {error_msg}")

    def _retry_camera(self):
        """Attempt to restart the camera."""
        # Stop existing thread if any
        if self.camera_thread and self.camera_thread.is_alive():
            self.stop_event.set()
            self.camera_thread.join(timeout=2.0)

        # Reset state
        self.stop_event.clear()
        self.auto_add.reset()

        # Clear queues
        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break

        while not self.error_queue.empty():
            try:
                self.error_queue.get_nowait()
            except queue.Empty:
                break

        # Try to start camera
        if self._check_camera():
            self._start_camera()
        else:
            self._show_camera_unavailable()

    def _on_canvas_click(self, event):
        """Handle click on camera canvas - check if clicked on a text label."""
        if self.current_frame is None or not self.label_text_rects:
            return

        # Scale click coordinates from canvas to original frame size
        frame_h, frame_w = self.current_frame.shape[:2]

        # Convert canvas coords to frame coords
        scale_x = frame_w / VIDEO_WIDTH
        scale_y = frame_h / VIDEO_HEIGHT
        click_x = event.x * scale_x
        click_y = event.y * scale_y

        # Check if click is inside any text label rectangle
        clicked_detection = None

        for (lx1, ly1, lx2, ly2, det) in self.label_text_rects:
            if lx1 <= click_x <= lx2 and ly1 <= click_y <= ly2:
                clicked_detection = det
                break  # Text labels don't overlap, so first match is fine

        if clicked_detection:
            self._show_label_dialog(clicked_detection)

    def _show_label_dialog(self, detection: Detection):
        """Show dialog to confirm/correct label and save sample."""
        # Freeze the current frame
        frozen_frame = self.current_frame.copy()
        frozen_detections = list(self.current_detections)

        # Create dialog
        dialog = tk.Toplevel(self)
        dialog.title("Label Detection")
        dialog.geometry("350x500")  # Taller to fit all options + buttons
        dialog.transient(self)
        dialog.wait_visibility()  # Wait for window to be visible
        dialog.grab_set()

        # Show the detected label
        ttk.Label(
            dialog,
            text=f"Detected: {detection.label}",
            font=("TkDefaultFont", 14, "bold")
        ).pack(pady=15)

        ttk.Label(
            dialog,
            text=f"Confidence: {detection.confidence:.1%}",
            font=("TkDefaultFont", 10)
        ).pack()

        ttk.Separator(dialog, orient="horizontal").pack(fill="x", pady=15)

        ttk.Label(dialog, text="What is this item?").pack()

        # Radio buttons for label selection
        selected_label = tk.StringVar(value=detection.label)

        labels_frame = ttk.Frame(dialog)
        labels_frame.pack(pady=10)

        for label in PRODUCE_CLASS_IDS.keys():
            rb = ttk.Radiobutton(
                labels_frame,
                text=label.capitalize(),
                value=label,
                variable=selected_label
            )
            rb.pack(anchor="w", padx=20, pady=2)

        # Add "Skip/Wrong" option
        ttk.Radiobutton(
            labels_frame,
            text="Not produce (skip)",
            value="_skip_",
            variable=selected_label
        ).pack(anchor="w", padx=20, pady=2)

        ttk.Separator(dialog, orient="horizontal").pack(fill="x", pady=15)

        def save_and_close():
            label = selected_label.get()
            if label == "_skip_":
                self.status_label.config(text="Skipped - not saved")
                dialog.destroy()
                return

            # Apply correction if different
            corrections = {}
            for i, det in enumerate(frozen_detections):
                if det.label == detection.label:
                    if label != detection.label:
                        corrections[i] = label
                    break

            # Save the sample
            self.data_collector.save_sample(frozen_frame, frozen_detections, corrections)
            count = self.data_collector.get_sample_count()
            self.sample_count_label.config(text=f"Samples: {count}")

            if label != detection.label:
                self.status_label.config(text=f"Saved: {detection.label} → {label} ({count} samples)")
            else:
                self.status_label.config(text=f"Saved: {label} confirmed ({count} samples)")

            dialog.destroy()

            # Show milestone messages
            if count in [10, 25, 50, 100]:
                messagebox.showinfo(
                    "Progress!",
                    f"You've collected {count} samples!\n\n"
                    + ("Keep going! 50+ recommended." if count < 50 else "Ready to train! Run:\npython3 train_model.py")
                )

        def cancel():
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=20, side="bottom")

        # Make Save button green and prominent
        save_btn = tk.Button(
            btn_frame, text="Save Sample", command=save_and_close,
            width=14, height=2, bg="#4CAF50", fg="white",
            font=("TkDefaultFont", 10, "bold")
        )
        save_btn.pack(side="left", padx=10)

        cancel_btn = tk.Button(
            btn_frame, text="Cancel", command=cancel,
            width=10, height=2
        )
        cancel_btn.pack(side="left", padx=10)

        # Focus the dialog
        dialog.focus_set()

    def _correct_selected_label(self):
        """Open dialog to correct the label of selected detection."""
        selection = self.detection_tree.selection()
        if not selection:
            messagebox.showinfo("Select Item", "Please select a detection to correct")
            return

        # Get current label
        item_id = selection[0]
        values = self.detection_tree.item(item_id, "values")
        current_label = values[0]

        # Create dialog to select correct label
        dialog = tk.Toplevel(self)
        dialog.title("Correct Label")
        dialog.geometry("300x250")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text=f"Current: {current_label}", font=("TkDefaultFont", 11, "bold")).pack(pady=10)
        ttk.Label(dialog, text="Select correct label:").pack()

        # Create buttons for each produce type
        selected_label = tk.StringVar(value=current_label)

        for label in PRODUCE_CLASS_IDS.keys():
            ttk.Radiobutton(
                dialog,
                text=label.capitalize(),
                value=label,
                variable=selected_label
            ).pack(anchor="w", padx=20)

        def apply_correction():
            new_label = selected_label.get()
            # Find detection index
            for i, det in enumerate(self.current_detections):
                if det.label == current_label:
                    self.label_corrections[i] = new_label
                    break
            self.status_label.config(text=f"Corrected: {current_label} -> {new_label}")
            dialog.destroy()

        ttk.Button(dialog, text="Apply", command=apply_correction).pack(pady=10)

    def _save_training_sample(self):
        """Save current frame with labels as training data."""
        if self.current_frame is None:
            messagebox.showwarning("No Frame", "No camera frame available to save")
            return

        if not self.current_detections:
            messagebox.showwarning("No Detections", "No detections to save. Point camera at produce first.")
            return

        # Save the sample with any corrections
        path = self.data_collector.save_sample(
            self.current_frame,
            self.current_detections,
            self.label_corrections
        )

        # Update sample count display
        count = self.data_collector.get_sample_count()
        self.sample_count_label.config(text=f"Samples: {count}")

        # Clear corrections after saving
        self.label_corrections.clear()

        self.status_label.config(text=f"Saved training sample ({count} total)")

        # Show hint about training
        if count == 10:
            messagebox.showinfo(
                "Training Tip",
                f"You've collected {count} samples!\n\n"
                f"Data saved to:\n{self.data_collector.get_data_path()}\n\n"
                "Collect 50-100 samples for best results, then run:\n"
                "python train_model.py"
            )

    def _on_close(self):
        """Clean shutdown."""
        self.stop_event.set()

        if self.camera_thread and self.camera_thread.is_alive():
            self.camera_thread.join(timeout=2.0)

        self.destroy()


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Nemlig Produce Scanner GUI")
    parser.add_argument(
        "--custom", "-c",
        action="store_true",
        help="Use custom trained ONNX model (CPU inference) instead of IMX500 NPU"
    )
    args = parser.parse_args()

    # Check if custom model requested but not available
    if args.custom:
        if not ONNX_AVAILABLE:
            print("ERROR: ONNX Runtime not installed.")
            print("Install with: uv pip install onnxruntime")
            return 1
        if not CUSTOM_ONNX_MODEL.exists():
            print(f"ERROR: Custom model not found at {CUSTOM_ONNX_MODEL}")
            print("Train a model first with: python train_model.py")
            return 1
        print(f"Using custom ONNX model: {CUSTOM_ONNX_MODEL}")

    app = NemligGUI(use_custom_model=args.custom)
    app.mainloop()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
