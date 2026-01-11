import pdfplumber
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Optional
import io
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
import re

@dataclass
class TextBlock:
    page: int
    text: str
    x0: float
    top: float
    x1: float
    bottom: float

def preprocess_image_v1(image):
    """Method 1: High contrast with adaptive enhancement"""
    if not isinstance(image, Image.Image):
        image = Image.open(io.BytesIO(image))
    
    img = image.convert('L')
    
    # Increase contrast significantly
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.5)
    
    # Increase sharpness
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(2.0)
    
    # Threshold
    img = img.point(lambda x: 0 if x < 128 else 255, '1')
    
    return img

def preprocess_image_v2(image):
    """Method 2: Less aggressive, preserve more detail"""
    if not isinstance(image, Image.Image):
        image = Image.open(io.BytesIO(image))
    
    img = image.convert('L')
    
    # Slight contrast boost
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.5)
    
    # Denoise
    img = img.filter(ImageFilter.MedianFilter(size=3))
    
    return img

def preprocess_image_v3(image):
    """Method 3: Original image, minimal processing"""
    if not isinstance(image, Image.Image):
        image = Image.open(io.BytesIO(image))
    
    # Just convert to grayscale
    return image.convert('L')

def parse_pdf_page_one(path: str) -> List[TextBlock]:
    """Only process page 1 with multiple OCR attempts"""
    blocks = []
    
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        print(f"Processing page 1...")
        
        # Try text extraction first
        words = page.extract_words()
        
        if words:
            print(f"  Found {len(words)} words via text extraction")
            for word in words:
                blocks.append(TextBlock(
                    page=1,
                    text=word['text'],
                    x0=word['x0'],
                    top=word['top'],
                    x1=word['x1'],
                    bottom=word['bottom']
                ))
        else:
            # Fall back to OCR with multiple preprocessing methods
            print(f"  No text found, trying OCR...")
            
            # Higher resolution for scanned forms
            page_image = page.to_image(resolution=300).original
            
            # Try different preprocessing methods
            best_blocks = []
            best_word_count = 0
            
            for method_num, preprocess_func in enumerate([preprocess_image_v1, preprocess_image_v2, preprocess_image_v3], 1):
                print(f"  Trying preprocessing method {method_num}...")
                
                img = preprocess_func(page_image)
                
                # Try with better Tesseract config
                # PSM 6 = assume uniform block of text
                # PSM 11 = sparse text, find as much as possible
                for psm in [6, 11]:
                    config = f'--oem 3 --psm {psm}'
                    
                    try:
                        ocr_data = pytesseract.image_to_data(
                            img, 
                            output_type=pytesseract.Output.DICT,
                            config=config
                        )
                        
                        temp_blocks = []
                        word_count = 0
                        
                        for i, text in enumerate(ocr_data['text']):
                            # Lower confidence threshold
                            if text.strip() and int(ocr_data['conf'][i]) > 20:
                                temp_blocks.append(TextBlock(
                                    page=1,
                                    text=text.strip(),
                                    x0=float(ocr_data['left'][i]),
                                    top=float(ocr_data['top'][i]),
                                    x1=float(ocr_data['left'][i] + ocr_data['width'][i]),
                                    bottom=float(ocr_data['top'][i] + ocr_data['height'][i])
                                ))
                                word_count += 1
                        
                        print(f"    Method {method_num} PSM {psm}: {word_count} words")
                        
                        # Keep the best result
                        if word_count > best_word_count:
                            best_word_count = word_count
                            best_blocks = temp_blocks
                    
                    except Exception as e:
                        print(f"    Error with method {method_num} PSM {psm}: {e}")
            
            blocks = best_blocks
            print(f"  Using best result: {best_word_count} words")
    
    return blocks

def group_words_into_lines(blocks: List[TextBlock], y_tolerance: float = 8.0):
    """Group words into lines - increased tolerance for better grouping"""
    lines = defaultdict(list)

    for block in blocks:
        key = round(block.top / y_tolerance) * y_tolerance
        lines[(block.page, key)].append(block)

    grouped_lines = []

    for (page, _), words in sorted(lines.items()):
        words_sorted = sorted(words, key=lambda w: w.x0)
        line_text = " ".join(w.text for w in words_sorted)

        grouped_lines.append({
            "page": page,
            "text": line_text,
            "words": words_sorted,
            "top": min(w.top for w in words_sorted),
            "bottom": max(w.bottom for w in words_sorted)
        })

    return grouped_lines