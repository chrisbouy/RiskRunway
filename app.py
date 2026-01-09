from pdf_analysis.parse_pdf import parse_pdf_page_one, group_words_into_lines
from extractors.insured import extract_applicant_info
from ai.Acord125ToJSON import analyze_with_ollama, analyze_with_ollama64, analyze_with_gemini
import os
import pdfplumber
import json
from google import genai


PDF_PATH = "sample_docs/acord125_filled.pdf"
TEMP_DIR = "sample_docs/temp_pages"


def main():
    # blocks = parse_pdf_page_one(PDF_PATH)
    # lines = group_words_into_lines(blocks)
    
    # print(f"\n{'='*60}")
    # print(f"Total lines extracted: {len(lines)}")
    # print(f"{'='*60}")
    
    # applicant_info = extract_applicant_info(lines)
    
    # print(f"\n{'='*60}")
    # print("=== EXTRACTED APPLICANT INFO ===")
    # print(f"{'='*60}")
    # for key, value in applicant_info.items():
    #     print(f"  {key}: {value}")    
    # insured = extract_insured_name(applicant_info)
    # print(insured.dict())
    os.makedirs(TEMP_DIR, exist_ok=True)
    image_paths = []
    with pdfplumber.open(PDF_PATH) as pdf:
        # for i in range(0, 1):
        output_path = os.path.join(TEMP_DIR, f"page.png")
        pdf.pages[0].to_image(resolution=300).original.save(output_path, format="PNG")
        image_paths.append(output_path)

        # result = analyze_with_ollama64(image_paths)
        result = analyze_with_gemini(image_path=output_path)
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
            print(json.dumps(parsed, indent=2))
        except json.JSONDecodeError:
            print(result.get("response", ""))
    
if __name__ == "__main__":
    main()
