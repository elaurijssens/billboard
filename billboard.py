from PIL import Image
import requests
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import socket
import struct
import time
from datetime import datetime, time as dtime
from urllib.parse import urlparse
import yaml
import os
import random
from collections import deque

# --- Load configuration from YAML file and optional remote override ---
def load_configuration(config_path):
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    remote_url = config.get('remote_configuration_url')
    if remote_url:
        try:
            response = requests.get(remote_url, timeout=5)
            response.raise_for_status()
            remote_config = yaml.safe_load(response.text)
            print("🔗 Remote configuration loaded.")

            for key in ['active_start', 'active_end', 'sources', 'random', 'no_repeat_window', 'width', 'height', 'crop', 'crop_origin']:
                if key in remote_config:
                    config[key] = remote_config[key]

        except Exception as e:
            print(f"⚠️ Failed to load remote config: {e}")

    return config

# --- Utility: Check if current time is within the active interval ---
def is_nighttime(start_str, end_str):
    now = datetime.now().time()
    start = dtime.fromisoformat(start_str)
    end = dtime.fromisoformat(end_str)
    return not (start <= now < end) if start < end else not (now >= start or now < end)

# --- Utility: Create a single black image with configured dimensions ---
def create_black_part(width, height):
    return Image.new("RGB", (width, height), color=(0, 0, 0))

# --- Crop image to desired aspect ratio based on crop origin ---
def crop_to_aspect(img, target_width, target_height, crop_origin):
    target_ratio = target_width / target_height
    img_width, img_height = img.size
    img_ratio = img_width / img_height

    if img_ratio > target_ratio:
        new_width = int(target_ratio * img_height)
        new_height = img_height
    else:
        new_width = img_width
        new_height = int(img_width / target_ratio)

    horiz, vert = crop_origin.get('horizontal', 'center'), crop_origin.get('vertical', 'middle')

    if horiz == 'left':
        left = 0
    elif horiz == 'right':
        left = img_width - new_width
    else:
        left = (img_width - new_width) // 2

    if vert == 'top':
        top = 0
    elif vert == 'bottom':
        top = img_height - new_height
    else:
        top = (img_height - new_height) // 2

    box = (left, top, left + new_width, top + new_height)
    return img.crop(box)

# --- Load, optionally crop/resize, and split an image from URL or file path ---
def split_image(source, width, height, crop=False, crop_origin=None):
    crop_origin = crop_origin or {'horizontal': 'center', 'vertical': 'middle'}

    try:
        if urlparse(source).scheme in ("http", "https"):
            response = requests.get(source)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            print(f"✨ Downloaded image from URL: {source}")
        else:
            img = Image.open(source)
            print(f"📁 Loaded local image file: {source}")
    except Exception as e:
        raise RuntimeError(f"Failed to load image from '{source}': {e}")

    if crop:
        print("🌍 Cropping to aspect ratio")
        img = crop_to_aspect(img, width, height, crop_origin)

    img_width, img_height = img.size
    if img_width != width or img_height != height:
        print(f"🔧 Resizing image from {img_width}x{img_height} to {width}x{height}")
        img = img.resize((width, height), Image.LANCZOS)

    slice_height = height // 6
    parts = []
    for i in range(6):
        box = (0, i * slice_height, width, (i + 1) * slice_height)
        parts.append(img.crop(box))

    return parts

# --- Convert a PIL image to raw byte stream ---
def image_to_raw_pixels(img):
    try:
        img = img.convert('RGB')
        pixels = list(img.getdata())
        raw_data = bytearray()
        for r, g, b in pixels:
            raw_data.extend([r, g, b, 255])
        return raw_data, img.width, img.height
    except Exception as e:
        print(f"❌ Error processing image in memory: {e}")
        return None, None, None

# --- Send a single image to a target IP address ---
def send_image(img, command, host, port=54321, label=None):
    raw_data, width, height = image_to_raw_pixels(img)
    if raw_data is None:
        return

    if len(command) != 4:
        print("❌ Error: Command must be exactly 4 characters long.")
        return

    data_size = len(raw_data)
    label = label or "(unnamed part)"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
            header = b"multiverse:" + struct.pack('!I', data_size) + command.encode('utf-8')
            s.sendall(header)
            s.sendall(raw_data)
            print(f"✅ Sent {label} ({width}x{height}, {data_size} bytes) to {host}:{port}")
            s.shutdown(socket.SHUT_WR)
    except socket.error as e:
        print(f"❌ Socket error sending {label} to {host}: {e}")

# --- Main loop: Continuously display images or black screens ---
def main():
    config = load_configuration("config.yaml")
    sources = config.get("sources", [])
    targets = config.get("targets", [])
    active_start = config.get("active_start", "08:00")
    active_end = config.get("active_end", "23:00")
    use_random = config.get("random", False)
    no_repeat_window = config.get("no_repeat_window", 0)
    default_width = config.get("width", 256)
    default_height = config.get("height", 384)
    default_crop = config.get("crop", False)
    default_crop_origin = config.get("crop_origin", {'horizontal': 'center', 'vertical': 'middle'})
    recent_queue = deque(maxlen=no_repeat_window)

    print("🔁 Starting continuous loop through image sources. Press Ctrl+C to exit.\n")

    try:
        while True:
            if is_nighttime(active_start, active_end):
                print("🌙 Night hours — displaying black screens.")
                black = create_black_part(default_width, default_height // 6)
                parts = [black] * 6

                with ThreadPoolExecutor(max_workers=6) as executor:
                    futures = [
                        executor.submit(
                            send_image,
                            parts[i],
                            "sdat",
                            ip,
                            label=f"Night Part {i+1}"
                        )
                        for i, ip in enumerate(targets)
                    ]
                    for future in futures:
                        future.result()

                time.sleep(60)
                continue

            if use_random:
                defined_shares = [s.get('shares', None) for s in sources if isinstance(s, dict)]
                if any(s is not None for s in defined_shares):
                    total_defined = sum(s for s in defined_shares if s is not None)
                    num_sources = len(sources)
                    for s in sources:
                        if isinstance(s, dict) and 'shares' not in s:
                            s['shares'] = max(total_defined // num_sources, 1)
                else:
                    for s in sources:
                        if isinstance(s, dict):
                            s.setdefault('shares', 1)

                available_sources = [s for i, s in enumerate(sources) if i not in recent_queue]
                if not available_sources:
                    recent_queue.clear()
                    available_sources = sources[:]

                weights = [s.get('shares', 1) if isinstance(s, dict) else 1 for s in available_sources]
                selected = random.choices(available_sources, weights=weights, k=1)[0]
                selected_index = sources.index(selected)
                recent_queue.append(selected_index)
                entries = [selected]
            else:
                entries = sources

            for index, source_entry in enumerate(entries):
                if isinstance(source_entry, dict):
                    source = source_entry.get("path")
                    display_time = source_entry.get("display_time", 10)
                    crop = source_entry.get("crop", default_crop)
                    crop_origin = source_entry.get("crop_origin", default_crop_origin)
                else:
                    source = source_entry
                    display_time = 10
                    crop = default_crop
                    crop_origin = default_crop_origin

                print(f"\n📆 Processing source {index+1}/{len(entries)}: {source}")
                try:
                    image_parts = split_image(source, default_width, default_height, crop=crop, crop_origin=crop_origin)
                except Exception as e:
                    print(f"❌ Skipping {source}: {e}")
                    continue

                with ThreadPoolExecutor(max_workers=6) as executor:
                    futures = [
                        executor.submit(
                            send_image,
                            image_parts[i],
                            "sdat",
                            ip,
                            label=f"Image {index+1} - Part {i+1}"
                        )
                        for i, ip in enumerate(targets)
                    ]
                    for future in futures:
                        future.result()

                time.sleep(display_time)

    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")

if __name__ == "__main__":
    main()