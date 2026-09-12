# Mütalaa MCP — Instagram seti

HB GLOBAL logosu ve H. Burak Taşyürek imzasının SVG dosyalarında görünmesi ve PNG imzasının daha düzgün çizilmesi için hazırlanmış düzeltme sürümüdür. Logo ve imza artık ana SVG dosyalarının içinde doğrudan vektör çizimler olarak bulunur.

| Mesaj | Gönderi · 1080 × 1350 | Hikâye · 1080 × 1920 |
|---|---|---|
| Hukuk araştırması | [Gönderi](01-arastirma-gonderi.png) | [Hikâye](01-arastirma-hikaye.png) |
| Resmî kaynak | [Gönderi](02-resmi-kaynak-gonderi.png) | [Hikâye](02-resmi-kaynak-hikaye.png) |
| Ücretsiz ve yerel kullanım | [Gönderi](03-ucretsiz-gonderi.png) | [Hikâye](03-ucretsiz-hikaye.png) |

[Altı görselin önizlemesi](onizleme.png) · [Paylaşım metinleri ve bağlantı](PAYLASIM-METINLERI.md)

Ana klasördeki altı PNG paylaşım boyutundadır. `png-2x/` klasöründe aynı görsellerin iki kat çözünürlüklü sürümleri bulunur: gönderiler 2160 × 2700, hikâyeler 2160 × 3840 piksel. Hikâye bağlantı etiketini standart boyutta yaklaşık (540, 1750) merkezine, alt marka satırının altındaki boş alana yerleştirin; 2× dosyada bu konum (1080, 3500) olur.

## SVG ve marka dosyaları

`source/` klasöründeki altı SVG bağımsız açılır. Ürün sahnesi dosyaya gömülü bir PNG'dir; HB GLOBAL ve imza ayrı, doğrudan vektör çizim gruplarıdır. Haricî resim veya font dosyası gerekmez. Başlıklar, açıklamalar ve ana görsel raster katmanda kaldığı için kompozisyonun tamamı vektör değildir.

Tüm kanalların kullandığı dört bağımsız marka SVG'si [ortak marka klasöründe](../brand/README.md) bulunur. Instagram seti bu kaynakları kullanır:

- [HB GLOBAL — siyah](../brand/hb-global-siyah.svg)
- [HB GLOBAL — beyaz](../brand/hb-global-beyaz.svg)
- [H. Burak Taşyürek imzası — siyah](../brand/hburaktasyurek-imza-siyah.svg)
- [H. Burak Taşyürek imzası — beyaz](../brand/hburaktasyurek-imza-beyaz.svg)

İndirme paketi de `brand/` ve `instagram-2026-09/` klasörlerini yan yana içerir; birlikte çıkarıldığında bu bağlantılar çalışır. Ana kompozisyon SVG'leri, markaları doğrudan içerdikleri için tek başına da açılır.

Beyaz sürümler şeffaf zeminlidir ve koyu zeminlerde kullanılmalıdır. Beyaz bir sayfada beyaz çizgilerin görünmemesi dosyanın boş olduğu anlamına gelmez.

İmzanın iki rengi de sağlanan beyaz vektör dosyasındaki aynı 15 çizimden türetilmiştir; yalnızca dolgu rengi değiştirilmiştir. Önceki siyah SVG yüklemesinin içindeki raster imza bu sürümde kullanılmaz. HB GLOBAL'in sağlanan biçimi ve siyah/beyaz dolgu düzeni korunmuştur.

## Üretim kaydı ve yeniden dışa aktarma

PNG'ler SVG'den 4× boyutta çizilip Lanczos3 yöntemiyle standart ve 2× boyutlara küçültülmüştür. Mevcut ürün görsellerinin üretim promptları, kaynak dosya özetleri ve yerleşim bilgileri [üretim kaydındadır](uretim-kaydi.json).

Node.js yüklü bir ortamda, bu klasörden çalıştırarak altı SVG'den PNG'leri yeniden oluşturabilirsiniz:

```sh
npm install @resvg/resvg-js sharp
node source/render.cjs
```

Bu işlem standart ve `png-2x/` PNG'lerini yeniden yazar. `onizleme.png` ve üretim kayıtlarını yeniden üretmez.
