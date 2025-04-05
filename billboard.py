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

# --- Load configuration from YAML file and optional remote override ---
def load_configuration(config_path):
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    remote_url = config.get('remote_configuration_url')
    if remote_url:
        try:
            response = requests.get(remote_url, timeout=5)
            response.raise_for_status()
            remote_config = response.json()
            print("🔗 Remote configuration loaded.")

            if 'active_start' in remote_config and 'active_end' in remote_config:
                config['active_start'] = remote_config['active_start']
                config['active_end'] = remote_config['active_end']

            if 'sources' in remote_config:
                config['sources'] = remote_config['sources']

        except Exception as e:
            print(f"⚠️ Failed to load remote config: {e}")

    return config

# --- Utility: Check if current time is within the active interval ---
def is_nighttime(start_str, end_str):
    now = datetime.now().time()
    start = dtime.fromisoformat(start_str)
    end = dtime.fromisoformat(end_str)
    return not (start <= now < end) if start < end else not (now >= start or now < end)

# --- Utility: Create a single black 256x64 image ---
def create_black_part():
    return Image.new("RGB", (256, 64), color=(0, 0, 0))

# --- Load, optionally resize, and split an image from URL or file path ---
def split_image(source):
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

    width, height = img.size
    if width != 256 or height != 384:
        print(f"🔧 Resizing image from {width}x{height} to 256x384")
        img = img.resize((256, 384), Image.LANCZOS)

    parts = []
    slice_height = 64
    for i in range(6):
        box = (0, i * slice_height, 256, (i + 1) * slice_height)
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

    print("🔁 Starting continuous loop through image sources. Press Ctrl+C to exit.\n")

    try:
        while True:
            if is_nighttime(active_start, active_end):
                print("🌙 Night hours — displaying black screens.")
                black = create_black_part()
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

            for index, source_entry in enumerate(sources):
                if isinstance(source_entry, dict):
                    source = source_entry.get("path")
                    display_time = source_entry.get("display_time", 10)
                else:
                    source = source_entry
                    display_time = 10

                print(f"\n📆 Processing source {index+1}/{len(sources)}: {source}")
                try:
                    image_parts = split_image(source)
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
