# test_setup.py
import pyautogui
import boto3
from PIL import ImageGrab

print("Taking screenshot...")
img = ImageGrab.grab()
print(f"✅ Screenshot OK: {img.size}")

print("Mouse position:", pyautogui.position())
print("✅ pyautogui OK")

print("Checking boto3...")
client = boto3.client("bedrock-runtime", region_name="us-east-1")
print("✅ boto3 OK")