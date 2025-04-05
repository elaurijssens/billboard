import logging
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
import threading
from collections import deque

CACHE_DIR = "./image_cache"
CACHE_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 1 week
REMOTE_CONFIG_INTERVAL = 300  # Check remote config every 5 minutes
os.makedirs(CACHE_DIR, exist_ok=True)

# --- Logging setup ---
logging.basicConfig(
    filename='/var/log/image_display_daemon.log',
    filemode='a',
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

config = {}
config_lock = threading.Lock()

# --- Load configuration from YAML file and optional remote override ---
def load_configuration(config_path):
    global config
    with open(config_path, 'r') as f:
        base_config = yaml.safe_load(f)

    with config_lock:
        config.update(base_config)

    fetch_remote_config()

# --- Fetch remote configuration and override specific keys ---
def fetch_remote_config():
    global config
    remote_url = config.get('remote_configuration_url')
    if not remote_url:
        return

    try:
        response = requests.get(remote_url, timeout=5)
        response.raise_for_status()
        remote_config = yaml.safe_load(response.text)
        logger.info("Remote configuration reloaded.")

        with config_lock:
            for key in ['active_start', 'active_end', 'sources', 'random', 'no_repeat_window', 'width', 'height', 'crop', 'crop_origin', 'system_logo']:
                if key in remote_config:
                    config[key] = remote_config[key]

    except Exception as e:
        logger.warning(f"Failed to reload remote config: {e}")

# --- Background thread to refresh remote config ---
def schedule_remote_config_check():
    def run():
        while True:
            time.sleep(REMOTE_CONFIG_INTERVAL)
            fetch_remote_config()
    t = threading.Thread(target=run, daemon=True)
    t.start()

# --- Utility: Check if current time is within the active interval ---
def is_nighttime(start_str, end_str):
    now = datetime.now().time()
    start = dtime.fromisoformat(start_str)
    end = dtime.fromisoformat(end_str)
    return not (start <= now < end) if start < end else not (now >= start or now < end)

# --- Utility: Create a single black image ---
def create_black_part(width, height):
    return Image.new("RGB", (width, height), color=(0, 0, 0))

# --- Cache utilities ---
def get_cache_path(url):
    safe_name = url.replace('://', '_').replace('/', '_')
    return os.path.join(CACHE_DIR, safe_name)

def prune_stale_cache():
    now = time.time()
    for filename in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, filename)
        if os.path.isfile(path) and now - os.path.getmtime(path) > CACHE_MAX_AGE_SECONDS:
            os.remove(path)
            logger.info(f"Removed stale cache: {filename}")

def load_image_with_cache(source):
    if urlparse(source).scheme in ("http", "https"):
        cache_path = get_cache_path(source)
        try:
            response = requests.get(source, timeout=5)
            response.raise_for_status()
            with open(cache_path, 'wb') as f:
                f.write(response.content)
            logger.info(f"Fetched and cached: {source}")
            return Image.open(BytesIO(response.content))
        except Exception:
            if os.path.exists(cache_path):
                logger.warning(f"Using cached version of {source}")
                return Image.open(cache_path)
            else:
                raise RuntimeError("No cache available.")
    else:
        return Image.open(source)

# --- Crop to aspect ratio ---
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

    horiz = crop_origin.get('horizontal', 'center')
    vert = crop_origin.get('vertical', 'middle')

    left = 0 if horiz == 'left' else img_width - new_width if horiz == 'right' else (img_width - new_width) // 2
    top = 0 if vert == 'top' else img_height - new_height if vert == 'bottom' else (img_height - new_height) // 2

    return img.crop((left, top, left + new_width, top + new_height))

# --- Split image ---
def split_image(source, width, height, crop=False, crop_origin=None):
    crop_origin = crop_origin or {'horizontal': 'center', 'vertical': 'middle'}
    try:
        img = load_image_with_cache(source)
    except Exception as e:
        logger.warning(f"{e} Skipping source.")
        return None

    if crop:
        logger.info("Cropping to aspect ratio")
        img = crop_to_aspect(img, width, height, crop_origin)

    if img.size != (width, height):
        logger.info(f"Resizing from {img.size} to {width}x{height}")
        img = img.resize((width, height), Image.LANCZOS)

    slice_height = height // 6
    return [img.crop((0, i * slice_height, width, (i + 1) * slice_height)) for i in range(6)]

# --- Send image ---
def image_to_raw_pixels(img):
    img = img.convert('RGB')
    raw_data = bytearray()
    for r, g, b in img.getdata():
        raw_data.extend([r, g, b, 255])
    return raw_data, img.width, img.height

def send_image(img, command, host, port=54321, label=None):
    raw_data, width, height = image_to_raw_pixels(img)
    if len(command) != 4:
        logger.error("Command must be 4 characters.")
        return

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
            header = b"multiverse:" + struct.pack('!I', len(raw_data)) + command.encode('utf-8')
            s.sendall(header + raw_data)
            logger.info(f"Sent {label or ''} ({width}x{height}) to {host}:{port}")
            s.shutdown(socket.SHUT_WR)
    except socket.error as e:
        logger.error(f"Socket error to {host}: {e}")

# --- Main loop ---
def main():
    global config
    load_configuration("config.yaml")
    schedule_remote_config_check()
    prune_stale_cache()

    recent_queue = deque(maxlen=config.get("no_repeat_window", 0))

    try:
        while True:
            with config_lock:
                sources = config.get("sources", [])
                targets = config.get("targets", [])
                active_start = config.get("active_start", "08:00")
                active_end = config.get("active_end", "23:00")
                use_random = config.get("random", False)
                width = config.get("width", 256)
                height = config.get("height", 384)
                crop = config.get("crop", False)
                crop_origin = config.get("crop_origin", {'horizontal': 'center', 'vertical': 'middle'})
                system_logo = config.get("system_logo")

            if is_nighttime(active_start, active_end):
                logger.info("Night mode — showing black screens.")
                parts = [create_black_part(width, height // 6)] * 6
            else:
                entries = sources[:]
                if use_random:
                    entries = [s for i, s in enumerate(sources) if i not in recent_queue] or sources[:]
                    weights = [s.get('shares', 1) if isinstance(s, dict) else 1 for s in entries]
                    selected = random.choices(entries, weights=weights, k=1)[0]
                    recent_queue.append(sources.index(selected))
                    entries = [selected]

                parts = None
                for entry in entries:
                    src = entry.get("path") if isinstance(entry, dict) else entry
                    display_time = entry.get("display_time", 10) if isinstance(entry, dict) else 10
                    c = entry.get("crop", crop) if isinstance(entry, dict) else crop
                    co = entry.get("crop_origin", crop_origin) if isinstance(entry, dict) else crop_origin
                    parts = split_image(src, width, height, crop=c, crop_origin=co)
                    if parts:
                        break

                if not parts:
                    if system_logo:
                        logger.warning("No valid images. Showing system logo.")
                        parts = split_image(system_logo, width, height)
                    if not parts:
                        logger.warning("No valid images or logo. Showing black screen.")
                        parts = [create_black_part(width, height // 6)] * 6

            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = [
                    executor.submit(send_image, parts[i], "sdat", ip, label=f"Part {i+1}")
                    for i, ip in enumerate(targets)
                ]
                for future in futures:
                    future.result()

            time.sleep(display_time if 'display_time' in locals() else 10)

    except KeyboardInterrupt:
        logger.info("Daemon stopped by user.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Unhandled exception occurred:")
