import os
import requests
import json
import time
import random
import base64
from io import BytesIO
import boto3

class LLMClient:
    def generate_json(self, prompt: str) -> dict:
        raise NotImplementedError

    def generate_json_with_images(self, prompt: str, images: list) -> dict:
        """
        Generate JSON from a prompt + list of PIL images.
        Subclasses that support vision should override this.
        Default falls back to text-only (no images).
        """
        raise NotImplementedError("This LLM client does not support vision.")

    @staticmethod
    def _pil_to_base64(image, format="JPEG") -> str:
        """Convert a PIL image to a base64-encoded string."""
        buffer = BytesIO()
        # JPEG doesn't support alpha channel, convert if needed
        if format.upper() == "JPEG" and image.mode in ("RGBA", "P", "LA"):
            image = image.convert("RGB")
        image.save(buffer, format=format, quality=85)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

class GroqClient(LLMClient):
    def __init__(self, api_key: str, model="llama-3.3-70b-versatile"):
        self.api_key = api_key
        self.model = model
        self.vision_model = "meta-llama/llama-4-scout-17b-16e-instruct"
        self.url = "https://api.groq.com/openai/v1/chat/completions"

    def generate_json(self, prompt: str) -> dict:
        # print(f"Groq prompt: {prompt}")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": "You return ONLY valid JSON. No explanations."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }

        response = requests.post(self.url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        # print(f"Groq response: {response.json()}")

        content = response.json()["choices"][0]["message"]["content"]
        # print(f"Groq content: {content}")
          # Strip markdown code block fences
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        return json.loads(content)  # Return parsed dict

    def generate_json_with_images(self, prompt: str, images: list) -> dict:
        """Send images + prompt to Groq vision model."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Build content array with images first, then the text prompt
        content_parts = []
        for img in images:
            b64 = self._pil_to_base64(img, format="JPEG")
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}"
                }
            })
        content_parts.append({
            "type": "text",
            "text": prompt
        })

        payload = {
            "model": self.vision_model,
            "temperature": 0.1,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "system",
                    "content": "You return ONLY valid JSON. No explanations."
                },
                {
                    "role": "user",
                    "content": content_parts
                }
            ]
        }

        response = requests.post(self.url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        return json.loads(content)


class BedrockClient(LLMClient):
    def __init__(self, model="us.anthropic.claude-haiku-4-5-20251001-v1:0", region="us-east-1"):
        self.model = model
        self.region = region
        self.client = boto3.client("bedrock-runtime", region_name=self.region) 

    def generate_json(self, prompt: str) -> dict:
        response = self.client.converse(
            modelId=self.model,
            messages=[
                {
                    "role": "user", 
                    "content": [
                        {"text": prompt}
                    ]
                }
            ],
            # Bedrock usually returns a content list of text blocks
        )

        content_blocks = response["output"]["message"]["content"]
        full_text = "".join(block.get("text", "") for block in content_blocks)
        full_text = full_text.strip()
        if full_text.startswith("```json"):
            full_text = full_text[7:].strip()
        elif full_text.startswith("```"):
            full_text = full_text[3:].strip()
        if full_text.endswith("```"):
            full_text = full_text[:-3].strip()

        return json.loads(full_text)

    def generate_json_with_images(self, prompt: str, images: list) -> dict:
        """Send images + prompt to Bedrock Claude vision."""
        content_parts = []
        for img in images:
            b64 = self._pil_to_base64(img, format="JPEG")
            content_parts.append({
                "image": {
                    "format": "jpeg",
                    "source": {
                        "bytes": base64.b64decode(b64)
                    }
                }
            })
        content_parts.append({"text": prompt})

        response = self.client.converse(
            modelId=self.model,
            messages=[
                {
                    "role": "user",
                    "content": content_parts
                }
            ],
        )

        content_blocks = response["output"]["message"]["content"]
        full_text = "".join(block.get("text", "") for block in content_blocks)

        # Log the raw response for debugging empty/unexpected replies
        stop_reason = response.get("stopReason", "unknown")
        print(f"[Bedrock Vision] stopReason={stop_reason}, response_len={len(full_text)}, raw={full_text[:300]!r}")

        full_text = full_text.strip()
        if full_text.startswith("```json"):
            full_text = full_text[7:].strip()
        elif full_text.startswith("```"):
            full_text = full_text[3:].strip()
        if full_text.endswith("```"):
            full_text = full_text[:-3].strip()

        if not full_text:
            print(f"[Bedrock Vision] WARNING: Empty response from model. Full response: {response}")
            return {}

        return json.loads(full_text)


class GeminiClient(LLMClient):
    def __init__(self, genai_client, model):
        self.client = genai_client
        self.model = model

    def generate_json(self, prompt: str) -> dict:
        # print(f"Gemini prompt: {prompt}")
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        )

        return json.loads(response.text)

    def generate_json_with_images(self, prompt: str, images: list) -> dict:
        """Send images + prompt to Gemini vision."""
        from google.genai import types

        parts = []
        for img in images:
            buffer = BytesIO()
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.save(buffer, format="JPEG", quality=85)
            parts.append(types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/jpeg"))
        parts.append(types.Part.from_text(text=prompt))

        response = self.client.models.generate_content(
            model=self.model,
            contents=[types.Content(parts=parts)],
            config={
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        )

        return json.loads(response.text)
