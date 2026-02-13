# from pdf_analysis.parse_pdf import parse_pdf_page_one, group_words_into_lines
# from extractors.insured import extract_applicant_info
from Acord125 import analyze_with_ollama, analyze_with_ollama64, analyze_with_gemini, extract_with_deepseek_ocr, extract_with_deepseek_ocr64
import os
import pdfplumber
import json
from google import genai


PDF_PATH = "sample_docs/application1.pdf"
TEMP_DIR = "sample_docs/temp_pages"

DEEPSEEK_MODE = False

def main():


    if DEEPSEEK_MODE:
        os.makedirs(TEMP_DIR, exist_ok=True)
        image_paths = []
        with pdfplumber.open(PDF_PATH) as pdf:
            # for i, page in enumerate(pdf.pages):
            out_path = os.path.join(TEMP_DIR, f"page.png")
            pdf.pages[0].to_image(resolution=300).original.convert("L").save(out_path, format="PNG")
            image_paths.append(out_path)
            
            result = extract_with_deepseek_ocr64(out_path)
            # result = analyze_with_ollama64(image_paths)

            # print(result)
    else:
        result = analyze_with_gemini(PDF_PATH)
        raw_response = result.get("response", "").strip()
        if raw_response.startswith("```"):
            lines = raw_response.splitlines()
            if lines:
                lines = lines[1:]          # remove ```json
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]         # remove ```
            raw_response = "\n".join(lines).strip()
        try:
            parsed = json.loads(raw_response)
            # print(json.dumps(parsed, indent=2))
        except json.JSONDecodeError:
            # print(result.get("response", ""))
    
if __name__ == "__main__":
    main()
