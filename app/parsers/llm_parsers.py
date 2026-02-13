import os
import requests
import json
import time
import random

class LLMClient:
    def generate_json(self, prompt: str) -> dict:
        raise NotImplementedError

class GroqClient(LLMClient):
    def __init__(self, api_key: str, model="llama-3.3-70b-versatile"):
        self.api_key = api_key
        self.model = model
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
