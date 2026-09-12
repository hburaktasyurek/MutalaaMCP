# Mütalaa MCP — X (Twitter) seti, Seri 2

İkinci kampanya setinin X uyarlaması. Instagram setindeki üç kart — "Söz uçar, kaynak kalır.", "Öyle dediler." ve "Ne ücret, ne aracı, ne sır." — X akışında kırpılmadan görünen 1600 × 900 yatay düzene taşındı: başlık ve marka sol sütunda, sahne sağda.

| Mesaj | Görsel · 1600 × 900 |
|---|---|
| Söz uçar, kaynak kalır. | [X kartı](01-soz-ucar-x.png) |
| Öyle dediler. | [X kartı](02-oyle-dediler-x.png) |
| Ne ücret, ne aracı, ne sır. | [X kartı](03-ucretsiz-x.png) |

[Üç görselin önizlemesi](onizleme.png) · [Paylaşım metinleri](PAYLASIM-METINLERI.md)

Ana klasördeki üç PNG paylaşım boyutundadır. `png-2x/` klasöründe 3200 × 1800 piksellik sürümler bulunur.

## SVG ve marka dosyaları

`source/` klasöründeki üç SVG bağımsız açılır ve tamamen vektördür: başlıklar Newsreader, destek metinleri Helvetica Neue ile canlı metin olarak çizilir; sahneler doğrudan path ve şekillerden kurulur. Kartlarda alt marka satırı yerine sol üstte "m. Mütalaa MCP" bloğu ve sol altta mutalaa.tr imzası kullanılır; HB GLOBAL ve imza vektörleri yatay düzene taşınmadığı için bu sette yer almaz.

Bütün çizim parçaları `../instagram-2026-09-seri-2/source/compose.cjs` modülünden alınır; sahne dili ve renkler tek kaynaktan gelir. Render aşamasında `source/fonts/` altındaki Newsreader TTF'leri kullanılır.

## Üretim kaydı ve yeniden dışa aktarma

```sh
node source/compose.cjs          # üç SVG'yi yeniden yazar
npm install @resvg/resvg-js sharp
node source/render.cjs           # PNG'leri ve png-2x/ çıktılarını yeniden yazar
```

Render betiği SVG genişliğini dosyadan okur; 4× çizilip Lanczos3 ile 1600 × 900 ve 3200 × 1800'e küçültülür. Fontlar `source/fonts/` altında aranır; farklı bir klasör için `MUTALAA_FONTS`, bağımlılıklar için `MUTALAA_RENDER_MODULES` ortam değişkeni kullanılır. `onizleme.png` ve üretim kayıtları bu işlemle yeniden üretilmez.
