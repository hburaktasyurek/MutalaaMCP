# Mütalaa MCP — Pazarlama

Logo, imza ve görsel kimlik bütün kanallarda ortak kullanılır. Her kanalın kompozisyonları, ölçüleri ve paylaşım metinleri kendi kampanya klasöründe tutulur.

| Kaynak | Kullanım |
|---|---|
| [Ortak marka varlıkları](brand/README.md) | HB GLOBAL ve kişisel imzanın siyah/beyaz vektörleri; bütün kanallar buradan alır. |
| [Instagram · Eylül 2026](instagram-2026-09/README.md) | Üç gönderi, üç hikâye, kaynak kompozisyonlar ve paylaşım metinleri. |
| [Instagram · Eylül 2026 · Seri 2](instagram-2026-09-seri-2/README.md) | “Söz uçar, kaynak kalır.” / “Öyle dediler.” / “Ne ücret, ne aracı, ne sır.” — tamamen vektör üç gönderi, üç hikâye. |

Renk, tipografi ve malzeme dili için [DESIGN.md](DESIGN.md); ürün anlatımı ve iddialar için [PRODUCT.md](PRODUCT.md) esas alınır. Bu belgeler bütün pazarlama kanallarının ortak bağlamıdır. Mütalaa'nın m. işaretinin mevcut kaynak dosyası, yazılım deposundaki `docs/assets/mutalaa-mark.svg` konumundadır.

Tasarım çalışmalarının kapsamı `marketing/` dizinidir. Impeccable çağrılarında `marketing/<kanal-klasörü>` hedefi kullanılır; araç ürün ve tasarım belgelerini buradan bulur. Tasarım tokenları ve kanal kayıtları `marketing/.impeccable/` altında bulunur; yazılım deposunun kökünde ürün/tasarım belgesi kopyası tutulmaz.

Yeni kanal setleri `kanal-YYYY-MM/` biçiminde açılır; örneğin `linkedin-2026-09/`. Bu klasörlere ayrı logo/imza kopyaları oluşturmak yerine `../brand/` içindeki kaynaklar kullanılır. Başlıklar, kırpım, yerleşim ve kanal ölçüleri yeni kullanım için uyarlanır.

Git'te ortak marka varlıkları, kullanım notları, paylaşım metinleri ve kanal önizlemeleri tutulur. Tam PNG/SVG çıktıları ile ZIP paketleri dağıtım varlıklarıdır; `.gitignore` bunları kanal adından bağımsız olarak dışarıda bırakır. Yerel özgün yüklemeler `brand/originals/` altında saklanır.

Instagram indirme paketi bu README'yi, `brand/` ortak kaynaklarını ve `instagram-2026-09/` setini birlikte içerir. İki klasörü yan yana tutmak, metinlerdeki göreli bağlantıları korur.
