# Ortak marka varlıkları

Bu dosyalar Instagram, LinkedIn, X ve diğer pazarlama yüzeyleri için ortak kaynaktır. Onaylanan geometri burada korunur; kanal klasörlerinde ayrı sürümler üretilmez.

| Varlık | Açık zemin | Koyu zemin |
|---|---|---|
| HB GLOBAL | [Siyah SVG](hb-global-siyah.svg) | [Beyaz SVG](hb-global-beyaz.svg) |
| H. Burak Taşyürek imzası | [Siyah SVG](hburaktasyurek-imza-siyah.svg) | [Beyaz SVG](hburaktasyurek-imza-beyaz.svg) |

Her dosya doğrudan vektör çizimlerinden oluşur; raster imza, haricî resim ve font bağımlılığı içermez. İki imza rengi aynı 15 çizimin farklı dolgularıdır. HB GLOBAL dosyaları sekizer çizim içerir.

Kullanırken:

- En-boy oranını koruyun; imzayı yeniden çizmeyin veya yapay olarak kalınlaştırmayın.
- Açık zeminde siyah, koyu zeminde beyaz dosyaları seçin. Beyaz dosyalar şeffaf zeminlidir.
- Ana SVG kompozisyonuna marka çizimlerini doğrudan ekleyin. Başka bir SVG'yi gömülü resim olarak kullanmak önceki uyumluluk sorununu yeniden oluşturabilir.
- PNG'yi son kanal ölçüsüne uygun, kenar yumuşatması korunarak dışa aktarın. Mevcut Instagram çıktıları 4× çizilip standart ve 2× boyuta küçültülmüştür.

Onaylanmış ilk uygulama [Instagram setindedir](../instagram-2026-09/README.md): HB GLOBAL alt solda, kişisel imza alt sağda bulunur. Diğer kanallarda yerleşim ve güvenli boşluklar yeni ölçüye göre uyarlanır.

`originals/`, kullanıcının yüklediği özgün dosyaların yerel arşividir; Git'e ve dağıtım paketine dahil edilmez. Üretimde bu README'de listelenen onaylı vektörler esas alınır. Görsel kimliğin genel kuralları [ortak tasarım rehberinde](../DESIGN.md) tutulur.
