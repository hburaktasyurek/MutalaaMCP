// npm install @resvg/resvg-js sharp
// node source/render.cjs
// Optional external dependency folder: MUTALAA_RENDER_MODULES=/path/to/node_modules
const fs = require('node:fs');
const path = require('node:path');
const dep = name => require(process.env.MUTALAA_RENDER_MODULES ? path.join(process.env.MUTALAA_RENDER_MODULES, name) : name);
const { Resvg } = dep('@resvg/resvg-js');
const sharp = dep('sharp');
async function main() {
  const out = path.resolve(__dirname, '..');
  fs.mkdirSync(path.join(out, 'png-2x'), { recursive: true });
  for (const name of fs.readdirSync(__dirname).filter(name => name.endsWith('.svg'))) {
    const svg = fs.readFileSync(path.join(__dirname, name));
    const large = new Resvg(svg, { fitTo: { mode: 'width', value: 4320 } }).render().asPng();
    for (const scale of [1, 2]) {
      await sharp(large).resize({ width: 1080 * scale, kernel: 'lanczos3' }).png({ compressionLevel: 9 }).toFile(path.join(out, scale === 1 ? '' : 'png-2x', name.replace(/\.svg$/, '.png')));
    }
    console.log(name);
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
