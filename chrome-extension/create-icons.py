#!/usr/bin/env python3
"""
Generate Air Quotes Chrome extension icons
Uses exact SVG path from the drop zone file icon
M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20M10,19L12,15H9V10H15V15L13,19H10Z
"""

from PIL import Image, ImageDraw
import os

def create_icon(size):
    """Create a single icon with the specified size"""
    # Create image with dark blue background
    img = Image.new('RGB', (size, size), color='#1e3a8a')
    draw = ImageDraw.Draw(img)
    
    scale = size / 24.0
    line_width = max(1, int(scale * 0.8))
    
    silver = '#c0c0c0'
    
    # === Part 1: File outline (M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2) ===
    # Left edge: from (6,2) down to (6,22) - but actually starts at 4 due to arc
    # Simplified: draw rectangle with rounded corners
    # Main file rectangle (excluding fold): from (4,4) to (20,22)
    
    # Draw file outline (rectangle with folded corner)
    # Left edge
    draw.line([(4*scale, 4*scale), (4*scale, 22*scale)], fill=silver, width=line_width)
    # Bottom edge
    draw.line([(4*scale, 22*scale), (20*scale, 22*scale)], fill=silver, width=line_width)
    # Right edge (bottom part)
    draw.line([(20*scale, 22*scale), (20*scale, 8*scale)], fill=silver, width=line_width)
    # Top edge (left part, before fold)
    draw.line([(4*scale, 4*scale), (14*scale, 4*scale)], fill=silver, width=line_width)
    # Diagonal fold line
    draw.line([(14*scale, 4*scale), (20*scale, 8*scale)], fill=silver, width=line_width)
    # Bottom edge of fold
    draw.line([(14*scale, 8*scale), (20*scale, 8*scale)], fill=silver, width=line_width)
    # Left edge of fold
    draw.line([(14*scale, 4*scale), (14*scale, 8*scale)], fill=silver, width=line_width)
    
    # === Part 2: Inner fold area (M18,20H6V4H13V9H18V20) ===
    # This is the folded corner triangle - draw darker
    # Actually this path traces: from (18,20) to (6,20) to (6,4) to (13,4) to (13,9) to (18,20)
    # The fold triangle is already drawn above
    
    # === Part 3: Quote marks (M10,19L12,15H9V10H15V15L13,19H10) ===
    # This draws two quote marks inside the file
    # First quote: M10,19 -> L12,15 -> H9 -> V10 -> H15 -> V15 -> L13,19 -> H10
    
    # Left quote mark (first one)
    # Points: (10,19) -> (12,15) -> (9,15) -> (9,10) -> (15,10) -> (15,15) -> (13,19) -> (10,19)
    # This creates the shape of a quotation mark
    quote1 = [
        (10*scale, 19*scale),
        (12*scale, 15*scale),
        (9*scale, 15*scale),
        (9*scale, 10*scale),
        (15*scale, 10*scale),
        (15*scale, 15*scale),
        (13*scale, 19*scale),
    ]
    draw.polygon(quote1, fill=silver)
    
    return img

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    sizes = [16, 48, 128]
    
    for size in sizes:
        img = create_icon(size)
        filename = f'icon{size}.png'
        filepath = os.path.join(script_dir, filename)
        img.save(filepath, 'PNG')
        print(f'Created {filename}')
    
    print('Icons generated successfully!')

if __name__ == '__main__':
    main()
