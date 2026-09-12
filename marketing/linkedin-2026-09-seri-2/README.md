# Mütalaa MCP — LinkedIn seti, Seri 2

İkinci kampanya setinin LinkedIn uyarlaması. Instagram setindeki üç kart — "Söz uçar, kaynak kalır.", "Öyle dediler." ve "Ne ücret, ne aracı, ne sır." — LinkedIn akışında kırpılmadan görünen 1200 × 1200 kare düzene taşındı; gönderi kompozisyonları 1080 gövdeden kare tuvalin merkezine ötelenerek aynen korunur.

| Mesaj | Görsel · 1200 × 1200 |
|---|---|
| Söz uçar, kaynak kalır. | [LinkedIn kartı](01-soz-ucar-linkedin.png) |
| Öyle dediler. | [LinkedIn kartı](02-oyle-dediler-linkedin.png) |
| Ne ücret, ne aracı, ne sır. | [LinkedIn kartı](03-ucretsiz-linkedin.png) |

[Üç görselin önizlemesi](onizleme.png) · [Paylaşım metinleri](PAYLASIM-METINLERI.md)

Ana klasördeki üç PNG paylaşım boyutundadır. `png-2x/` klasöründe 2400 × 2400 piksellik sürümler bulunur.

## SVG ve marka dosyaları

`source/` klasöründeki üç SVG bağımsız açılır ve tamamen vektördür: başlıklar Newsreader, destek metinleri Helvetica Neue ile canlı metin olarak çizilir; sahneler doğrudan path ve şekillerden kurulur. Gönderi gövdeleri `../instagram-2026-09-seri-2/source/compose.cjs` modülünden aynen alınır; yalnızca tuval merkezlemesi değişir. Alt marka satırı yerine "m. Mütalaa MCP — mutalaa.tr" imza satırı kullanılır.

Render aşamasında `source/fonts/` altındaki Newsreader TTF'leri kullanılır.

## Üretim kaydı ve yeniden dışa aktarma

```sh
node source/compose.cjs          # üç SVG'yi yeniden yazar
npm install @resvg/resvg-js sharp
node source/render.cjs           # PNG'leri ve png-2x/ çıktılarını yeniden yazar
```

Render betiği SVG genişliğini dosyadan okur; 4× çizilip Lanczos3 ile 1200 × 1200 ve 2400 × 2400'e küçültülür. Fontlar `source/fonts/` altında aranır; farklı bir klasör için `MUTALAA_FONTS`, bağımlılıklar için `MUTALAA_RENDER_MODULES` ortam değişkeni kullanılır. `onizleme.png` ve üretim kayıtları bu işlemle yeniden üretilmez.
