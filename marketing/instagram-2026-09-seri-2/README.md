# Mütalaa MCP — Instagram seti, Seri 2

İkinci kampanya seti. Mesajlar ilk setten farklıdır: tanıtım yerine akılda kalıcı üç kart — "Söz uçar, kaynak kalır.", "Öyle dediler." ve "Ne ücret, ne aracı, ne sır." Kompozisyonlar `source/compose.cjs` ile tamamen vektör üretilir; raster ürün sahnesi yoktur. HB GLOBAL ve imza, ilk setin onaylı SVG'lerindeki vektör gruplarından aynen alınır.

| Mesaj | Gönderi · 1080 × 1350 | Hikâye · 1080 × 1920 |
|---|---|---|
| Söz uçar, kaynak kalır. | [Gönderi](01-soz-ucar-gonderi.png) | [Hikâye](01-soz-ucar-hikaye.png) |
| Öyle dediler. | [Gönderi](02-oyle-dediler-gonderi.png) | [Hikâye](02-oyle-dediler-hikaye.png) |
| Ne ücret, ne aracı, ne sır. | [Gönderi](03-ucretsiz-gonderi.png) | [Hikâye](03-ucretsiz-hikaye.png) |

[Altı görselin önizlemesi](onizleme.png) · [Paylaşım metinleri ve bağlantı](PAYLASIM-METINLERI.md)

Ana klasördeki altı PNG paylaşım boyutundadır. `png-2x/` klasöründe iki kat çözünürlüklü sürümler bulunur: gönderiler 2160 × 2700, hikâyeler 2160 × 3840 piksel. Hikâye bağlantı etiketini standart boyutta yaklaşık (540, 1750) merkezine, alt marka satırının altındaki boş alana yerleştirin; 2× dosyada bu konum (1080, 3500) olur.

## SVG ve marka dosyaları

`source/` klasöründeki altı SVG bağımsız açılır ve tamamen vektördür: başlıklar Newsreader, destek metinleri Helvetica Neue ile canlı metin olarak çizilir; sahneler doğrudan path ve şekillerden kurulur. HB GLOBAL ve imza doğrudan vektör çizim gruplarıdır. Haricî resim veya font dosyası gerekmez; render aşamasında `source/fonts/` altındaki Newsreader TTF'leri kullanılır.

Marka kaynakları [ortak marka klasöründedir](../brand/README.md); bu set siyah sürümleri krem kartlarda, beyaz sürümleri lacivert kartta kullanır. İndirme paketi `brand/` ve bu klasörü yan yana içerir.

## Üretim kaydı ve yeniden dışa aktarma

SVG'ler `source/compose.cjs` ile üretilir, PNG'ler `source/render.cjs` ile 4× boyutta çizilip Lanczos3 yöntemiyle standart ve 2× boyutlara küçültülür. Metinler, sahne bilgileri ve marka kaynak özetleri [üretim kaydındadır](uretim-kaydi.json).

```sh
node source/compose.cjs          # altı SVG'yi yeniden yazar
npm install @resvg/resvg-js sharp
node source/render.cjs           # PNG'leri ve png-2x/ çıktılarını yeniden yazar
```

Render betiği fontları `source/fonts/` altında arar; farklı bir klasör için `MUTALAA_FONTS` ortam değişkeni kullanılır. `onizleme.png` ve üretim kayıtları bu işlemle yeniden üretilmez.
