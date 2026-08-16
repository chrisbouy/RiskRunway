// Placeholder icons - Replace these with actual PNG files
// You need to add the following icon files:
// - icon16.png (16x16 pixels)
// - icon48.png (48x48 pixels)
// - icon128.png (128x128 pixels)

// To create simple icons, you can use an online tool like:
// https://www.favicon.cc/
// https://convertio.co/png-ico/

// Or create them programmatically using a canvas:
/*
const sizes = [16, 48, 128];
sizes.forEach(size => {
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  
  // Draw blue background
  ctx.fillStyle = '#3b82f6';
  ctx.fillRect(0, 0, size, size);
  
  // Draw white document icon
  ctx.fillStyle = '#ffffff';
  const padding = size * 0.2;
  ctx.fillRect(padding, padding, size - padding * 2, size - padding * 2);
  
  // Save as PNG
  const dataUrl = canvas.toDataURL('image/png');
  console.log(`${size}x${size}:`, dataUrl);
});
*/
