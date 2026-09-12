---
version: 2
slug: "marketing-instagram-2026-09-seri-2"
primary_target: "instagram-2026-09-seri-2"
related_targets: ["brand", "instagram-2026-09"]
---

# Instagram kampanyası — Seri 2

## Scope

Mode: Persuade. Hedef: hukuki araştırma yapan herkes — hukukçular ve gündelik haklarını araştıran vatandaşlar. Üç mesaj, her biri için bir 4:5 gönderi ve bir 9:16 hikâye. Teslimat PNG pazarlama görsellerini ve bağımsız vektör SVG kaynaklarını kapsar.

## Direction contract

THESIS: Kaynaksız söylenen her söz — tezgâhtarın, ev sahibinin, karşı tarafın, yapay zekânın — aynı oyuna dayanır: dinleyenin kaynağa bakmayacağına güvenir. Mütalaa tek hamleyle bozar: kaynağı gösterir.

OWN-WORLD: İlk setin kâğıt-mürekkep-pirinç dünyası aynen korunur; krem ve lacivert zeminler, Newsreader başlıklar, pirinç bağlantı çizgileri, m. işareti. Bu sette tüm kompozisyon tamamen vektördür; raster ürün sahnesi yoktur.

STORY: "Söz uçar, kaynak kalır." (savruyan balonlar karşısında mevzuat.adalet.gov.tr bağlantılı sabit belge) → "Öyle dediler. / Bir de kaynağına bakın." (üç iddia balonu tek resmî belgeye bağlanır) → "Ne ücret, ne aracı, ne sır." (bilgisayardan adalet.gov.tr'ye aracısız doğrudan ok). Soyut "resmî kaynak/hizmet" etiketleri yerine gerçek alan adları gösterilir. Eylem: kurulum rehberi bağlantısı.

FIRST VIEWPORT: Büyük serif başlık, tek baskın sahne, kısa alt metin, Mütalaa MCP imza satırı, alt marka satırı. Hikâyelerde üst ve alt arayüz alanları boş bırakılır; bağlantı etiketi alanı (540,1750).

## Content boundaries

İlk setin sınırları geçerlidir: kanıtsız başarı oranı, süre kazancı, tam gizlilik veya çevrimdışılık vaadi, hukukî hüküm yok. "Öyle dediler." balonları örnek söylemlerdir; doğru ya da yanlış oldukları iddia edilmez, kaynağa bakılması istenir. "Resmî kaynak" ifadesi gerçek entegrasyonlara dayanır (Bedesten/mevzuat.adalet.gov.tr, AYM kararlar bilgi bankası); bakanlık bağlılığı gibi hukuki statü iddiası yazılmaz.

## Teslimat kaydı

Üretim: `source/compose.cjs` altı bağımsız SVG yazar (marka grupları ilk setin onaylı SVG'lerinden aynen okunur); `source/render.cjs` resvg 4× + sharp Lanczos3 ile 1× ve 2× PNG üretir. Newsreader değişken TTF'leri `source/fonts/` altındadır; depodaki woff2 alt kümeleri Türkçe glif eksikliği ve resvg uyumluluğu nedeniyle kullanılmaz.

Onaylı metinler: "Söz uçar, kaynak kalır." / "Öyle dediler." / "Ne ücret, ne aracı, ne sır."; ücret notu "Yapay zekâ uygulamanızın ücretleri geçerli olabilir." aynen korunur. Paylaşım metinleri, bağlantı rehberi ve üretim manifesti kampanya klasöründedir.
