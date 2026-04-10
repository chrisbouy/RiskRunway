# test_bedrock.py
import boto3
import base64
from PIL import ImageGrab

print("Taking screenshot...")
img = ImageGrab.grab()
img.save("/tmp/test_shot.png")
with open("/tmp/test_shot.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

print("Calling Bedrock...")
client = boto3.client("bedrock-runtime", region_name="us-east-1")

response = client.converse(
    modelId="us.anthropic.claude-sonnet-4-6",
    messages=[{
        "role": "user",
        "content": [
            {
                # Bedrock uses "image" as the key, not "type"
                "image": {
                    "format": "png",
                    "source": {
                        "bytes": open("/tmp/test_shot.png", "rb").read()
                    }
                }
            },
            {
                # Bedrock uses "text" as the key, not "type"
                "text": "Describe what you see on screen in one sentence."
            }
        ]
    }],
    # additionalModelRequestFields={
    #     "tools": [{
    #         "type": "computer_20250124",
    #         "name": "computer",
    #         "display_width_px": 1440,
    #         "display_height_px": 900,
    #         "display_number": 0
    #     }],
    #     "anthropic_beta": ["computer-use-2025-01-24"]
    # }
)

content = response["output"]["message"]["content"]
for block in content:
    if block.get("text"):
        print(f"✅ Bedrock responded: {block['text']}")