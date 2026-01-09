from textwrap import dedent
import os
import time
import requests 
import base64
import settings
from google import genai
import pathlib
from google.genai import types
from ollama import Client


# DEFAULT_MODEL = "gemma3:4b"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_PROMPT = dedent(
    """
    You are extracting data for premium finance intake.
    From the attached ACORD 125 image, extract the following fields.
    If a value is not clearly present, return null. Do not guess.
    Return valid JSON only using this schema:
    {
        "insured": {
            "name": null,
            "address": {
                "street": null,
                "city": null,
                "state": null,
                "zip": null
            }
        },
        "agency": {
            "name": null,
            "phone": null,
            "contact_name": null
        },
        "policies": [
            {
                "line_of_coverage": null,
                "carrier": null,
                "policy_number": null,
                "effective_date": null,
                "expiration_date": null,
                "annual_premium": null,
                "taxes_and_fees": {
                    "tax_amount": null,
                    "fee_amount": null,
                    "details": null
                },
                "minimum_earned": {
                    "type": null,
                    "value": null
                }
            }
        ],
        "totals": {
            "total_premium": null,
            "total_taxes_and_fees": null,
            "total_amount_financed": null
        },
        "metadata": {
            "source_document": null,
            "extraction_confidence": null,
            "extracted_at": null
        }
    }
    """
)
DEEPSEEK_PROMPT = (
    "Free OCR. Output the recognized text verbatim."
)
def extract_with_deepseek_ocr64(image_path):
    """Sends a base64 encoded image to Ollama for OCR using deepseek-ocr."""
    url = "http://localhost:11434/api/generate" # Default Ollama API endpoint
    with open(image_path, "rb") as f:
        img_bytes = f.read()
    base64_image = base64.b64encode(img_bytes).decode("utf-8")
    
    payload = {
        "model": "deepseek-ocr",
        "prompt": DEEPSEEK_PROMPT,
        "images": [base64_image],
        "stream": False # Set to True for streaming responses
    }
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        result = response.json()
        return result.get("response", "").strip()
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to Ollama: {e}")
        return None


def extract_with_deepseek_ocr(image_paths, grounding_prompt=DEEPSEEK_PROMPT):
    if not image_paths:
        raise ValueError("No image paths passed to extract_with_deepseek_ocr")

    client = Client(host="http://localhost:11434")
    results = []

    for path in image_paths:
        abs_path = os.path.abspath(path)
        chunks = client.generate(
            model="deepseek-ocr",
            prompt=f"<|grounding|>{grounding_prompt}",
            images=[abs_path],
            stream=True
        )
        # print("DeepSeek response keys:", response.keys())
        text = []
        for chunk in chunks:
            if chunk.get("response"):
                text.append(chunk["response"])
        results.append("".join(text))
    return results
def analyze_with_gemini(image_path, analysis_prompt=DEFAULT_PROMPT, model_name=DEFAULT_MODEL):
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    
    # Retry configuration
    max_retries = 5
    base_delay = 10
    
    for attempt in range(max_retries):
        try:
            # Upload video file
            # image_file = client.files.upload(file=image_path)
            # print(f"Completed upload: {image_file.uri}")
            
            # Wait for processing
            # while image_file.state == "PROCESSING":
            #     image_file = client.files.get(name=image_file.name)
                
            # if image_file.state == "FAILED":
            #     raise ValueError(f"Video processing failed")
            filepath = pathlib.Path(image_path)
            # Generate content
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(
                        data=filepath.read_bytes(),
                        mime_type='application/pdf',
                    ),                    analysis_prompt
                ]
            )
            
            # Parse the analysis
            content = response.text.strip()
            
            return {
                "response": content,
                "model": model_name,
                "success": True,
            }

            
        except Exception as e:
            error_str = str(e)
            print(f"Attempt {attempt + 1} failed: {str(e)}")
            
            # Check for rate limit error and extract retry delay
            if '429' in error_str or 'RESOURCE_EXHAUSTED' in error_str:
                import re
                match = re.search(r'retry in ([\d.]+)s', error_str)
                if match:
                    retry_delay = float(match.group(1)) + 2  # Add 2 second buffer
                    print(f"⏳ Rate limited. Waiting {retry_delay:.1f}s as instructed by API...")
                    time.sleep(retry_delay)
                    continue  # Retry the request
                else:
                    # No specific delay given, use exponential backoff
                    wait_time = base_delay * (2 ** attempt)
                    print(f"⏳ Rate limited. Waiting {wait_time}s (exponential backoff)...")
                    time.sleep(wait_time)
                    continue
            
            # For non-rate-limit errors
            if attempt == max_retries - 1:
                raise Exception(f"All {max_retries} attempts failed: {str(e)}")
            time.sleep(base_delay * (attempt + 1))


def analyze_with_ollama(image_paths, analysis_prompt=DEFAULT_PROMPT, model_name=DEFAULT_MODEL):
    if not image_paths:
        raise ValueError("No image paths provided to analyze_with_ollama")

    missing = [path for path in image_paths if not os.path.exists(path)]
    if missing:
        raise FileNotFoundError(f"Missing image files: {missing}")

    try:
        from ollama import Client
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "The 'ollama' Python package is not installed. Run 'pip install ollama' "
            "inside your environment before calling analyze_with_ollama."
        ) from exc

    try:
        client = Client(host="http://localhost:11434")
        response = client.generate(
            model=model_name,
            prompt=analysis_prompt,
            images=image_paths,
        )
        return {
            "response": response.get("response", ""),
            "model": model_name,
            "success": True,
        }
    except Exception as e:
        error_msg = f"Error with {model_name}: {str(e)}"
        print(error_msg)
        raise Exception(error_msg) from e

def analyze_with_ollama64(image_paths, analysis_prompt=DEFAULT_PROMPT, model_name=DEFAULT_MODEL):
    if not image_paths:
        raise ValueError("No image paths provided to analyze_with_ollama64")

    missing = [path for path in image_paths if not os.path.exists(path)]
    if missing:
        raise FileNotFoundError(f"Missing image files: {missing}")

    # Extract base64 frames
    img_list = []
    for path in image_paths:
        with open(path, "rb") as f:
            img_bytes = f.read()
        img_base64 = base64.b64encode(img_bytes).decode("utf-8")
        img_list.append(img_base64)

    # For Ollama API, images should be sent as base64 strings
    payload = {
        "model": model_name,
        "prompt": analysis_prompt,
        "images": img_list,
        "stream": False,
    }
    ollama_url = "http://localhost:11434/api/generate"
    print(f"Making request to Ollama endpoint: {ollama_url} with model: {model_name}")
    try:
        response = requests.post(ollama_url, json=payload, timeout=600)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error making request to Ollama: {e}")
        print(f"Response status: {response.status_code if 'response' in locals() else 'N/A'}")
        print(f"Response text: {response.text if 'response' in locals() else 'N/A'}")
        raise
