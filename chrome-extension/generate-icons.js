// Script to generate Air Quotes icons
// Run with: node generate-icons.js

const fs = require('fs');
const path = require('path');

// We'll create simple PNG files using a base64 encoded template
// Dark blue: #1e3a8a (primary), Silver: #c0c0c0 (quote symbol)

const sizes = [16, 48, 128];

// Simple PNG generator using raw bytes (minimal PNG format)
// This creates solid color squares that can be used as placeholders

function createPNG(size) {
    // PNG header
    const signature = Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);
    
    // IHDR chunk
    const width = size;
    const height = size;
    const bitDepth = 8;
    const colorType = 2; // RGB
    const ihdrData = Buffer.alloc(13);
    ihdrData.writeUInt32BE(width, 0);
    ihdrData.writeUInt32BE(height, 4);
    ihdrData.writeUInt8(bitDepth, 8);
    ihdrData.writeUInt8(colorType, 9);
    ihdrData.writeUInt8(0, 10); // compression
    ihdrData.writeUInt8(0, 11); // filter
    ihdrData.writeUInt8(0, 12); // interlace
    
    const ihdrChunk = createChunk('IHDR', ihdrData);
    
    // Create image data (dark blue background with silver quote mark)
    const rawData = [];
    const darkBlue = { r: 0x1e, g: 0x3a, b: 0x8a };
    const silver = { r: 0xc0, g: 0xc0, b: 0xc0 };
    const darkSilver = { r: 0x88, g: 0x88, b: 0x88 };
    
    for (let y = 0; y < height; y++) {
        rawData.push(0); // filter byte
        for (let x = 0; x < width; x++) {
            // Calculate position relative to icon center
            const cx = x - width / 2;
            const cy = y - height / 2;
            
            // Draw rounded rectangle background with outline
            const margin = Math.max(1, size * 0.1);
            const cornerRadius = Math.max(2, size * 0.2);
            
            // Check if we're in the main area (with rounded corners)
            const inMainArea = x >= margin && x < width - margin && y >= margin && y < height - margin;
            
            // Simple quote mark shape (two curved lines like opening quote)
            const quoteWidth = size * 0.35;
            const quoteHeight = size * 0.5;
            const quoteLeft = width * 0.25;
            const quoteTop = height * 0.25;
            
            // First quote mark (left)
            const inQuote1 = isInQuoteMark(x, y, quoteLeft, quoteTop, quoteWidth, quoteHeight, size);
            
            // Second quote mark (right, slightly offset)
            const quote2Left = width * 0.45;
            const inQuote2 = isInQuoteMark(x, y, quote2Left, quoteTop, quoteWidth, quoteHeight, size);
            
            let r, g, b;
            
            if (inQuote1 || inQuote2) {
                // Silver quote symbol
                r = silver.r;
                g = silver.g;
                b = silver.b;
            } else if (x === margin || x === width - margin - 1 || y === margin || y === height - margin - 1) {
                // Outline (dark silver)
                r = darkSilver.r;
                g = darkSilver.g;
                b = darkSilver.b;
            } else if (inMainArea) {
                // Dark blue background
                r = darkBlue.r;
                g = darkBlue.g;
                b = darkBlue.b;
            } else {
                // Transparent (white for RGB)
                r = 255;
                g = 255;
                b = 255;
            }
            
            rawData.push(r, g, b);
        }
    }
    
    // Compress with zlib
    const zlib = require('zlib');
    const compressed = zlib.deflateSync(Buffer.from(rawData));
    
    const idatChunk = createChunk('IDAT', compressed);
    
    // IEND chunk
    const iendChunk = createChunk('IEND', Buffer.alloc(0));
    
    return Buffer.concat([signature, ihdrChunk, idatChunk, iendChunk]);
}

function isInQuoteMark(x, y, left, top, width, height, size) {
    // Create a simple quote mark shape using ellipses
    const centerX1 = left + width / 2;
    const centerY1 = top + height / 3;
    const rx1 = width / 3;
    const ry1 = height / 4;
    
    const centerX2 = left + width / 2;
    const centerY2 = top + height * 2 / 3;
    const rx2 = width / 3;
    const ry2 = height / 4;
    
    // Check if point is inside either ellipse
    const inEllipse1 = Math.pow((x - centerX1) / rx1, 2) + Math.pow((y - centerY1) / ry1, 2) <= 1;
    const inEllipse2 = Math.pow((x - centerX2) / rx2, 2) + Math.pow((y - centerY2) / ry2, 2) <= 1;
    
    return inEllipse1 || inEllipse2;
}

function createChunk(type, data) {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length, 0);
    
    const typeBuffer = Buffer.from(type);
    const crcData = Buffer.concat([typeBuffer, data]);
    
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(crcData), 0);
    
    return Buffer.concat([length, typeBuffer, data, crc]);
}

function crc32(data) {
    let crc = 0xFFFFFFFF;
    const table = [];
    
    for (let i = 0; i < 256; i++) {
        let c = i;
        for (let j = 0; j < 8; j++) {
            c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
        }
        table[i] = c;
    }
    
    for (let i = 0; i < data.length; i++) {
        crc = table[(crc ^ data[i]) & 0xFF] ^ (crc >>> 8);
    }
    
    return (crc ^ 0xFFFFFFFF) >>> 0;
}

// Generate icons
sizes.forEach(size => {
    const png = createPNG(size);
    const filename = `icon${size}.png`;
    fs.writeFileSync(path.join(__dirname, filename), png);
    console.log(`Created ${filename}`);
});

console.log('Icons generated successfully!');
