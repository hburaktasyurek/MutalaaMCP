---
name: "Mütalaa MCP"
description: "Mevcut web tanıtımı ve pazarlama kimliğinin kaynaklardan çıkarılmış görsel sistemi."
colors:
  brass: "#b5853b"
  brass-soft: "#c79a52"
  brass-surface: "#f3ead9"
  brass-deep: "#8a6224"
  paper: "#faf8f3"
  surface: "#ffffff"
  ink: "#1b2436"
  slate: "#586176"
  mist: "#8b92a1"
  muted: "#f3efe7"
  hairline: "#e7e3da"
  ink-bg: "#141b29"
  on-dark: "#edeae2"
  ok: "#3e7c5a"
  ink-hover: "#30394b"
typography:
  display:
    fontFamily: "\"Newsreader\", Georgia, \"Times New Roman\", serif"
    fontSize: "clamp(3rem, 6.5vw, 5.65rem)"
    fontWeight: 500
    lineHeight: 0.98
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "\"Newsreader\", Georgia, \"Times New Roman\", serif"
    fontSize: "clamp(2.2rem, 4.2vw, 3.65rem)"
    fontWeight: 500
    lineHeight: 1.03
    letterSpacing: "-0.035em"
  title:
    fontFamily: "\"Newsreader\", Georgia, \"Times New Roman\", serif"
    fontSize: "1.55rem"
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: "-0.015em"
  body:
    fontFamily: "\"Helvetica Neue\", Helvetica, Arial, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.65
  button:
    fontFamily: "\"Helvetica Neue\", Helvetica, Arial, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.92rem"
    fontWeight: 700
    lineHeight: 1.2
  label:
    fontFamily: "\"Helvetica Neue\", Helvetica, Arial, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 650
    lineHeight: 1.65
  source-number:
    fontFamily: "ui-monospace, \"SF Mono\", Menlo, Consolas, monospace"
    fontSize: "0.8rem"
    fontWeight: 800
    lineHeight: 1.65
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "12px"
  pill: "999px"
spacing:
  small-gap: "0.45rem"
  compact: "0.75rem"
  base: "1rem"
  panel: "1.25rem"
  group: "1.5rem"
  action: "2rem"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "0.7rem 1.1rem"
  button-primary-hover:
    backgroundColor: "{colors.ink-hover}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "0.7rem 1.1rem"
  button-secondary-hover:
    backgroundColor: "#fffdf9"
  button-light:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "0.7rem 1.1rem"
  button-light-hover:
    backgroundColor: "#f5f1e8"
  setup-prompt:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "1.25rem"
    width: "100%"
  navigation-link:
    textColor: "{colors.slate}"
    rounded: "{rounded.sm}"
    padding: "0.55rem 0.75rem"
  navigation-link-hover:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.ink}"
  source-chip:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.slate}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0.3rem 0.65rem"
  benefit-card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.xl}"
    padding: "1.6rem"
---

# Design System: Mütalaa MCP

## Overview

**Creative North Star: "Kâğıt, mürekkep ve pirinç"**

Bu ad, mevcut kimliğin malzemelerini tarif eder. Sıcak kâğıt zemin, lacivert mürekkep, pirinç ayrıntılar ve serif başlıklar hukuk araştırmasını sakin, okunabilir bir yayın diliyle anlatır. Web yüzeyinde ölçülü kartlar ve ince sınırlar; pazarlama görsellerinde kâğıt dokusu, metal bağlantılar ve yumuşak gölgeler kullanılır.

Bu kayıt mevcut sistemi belgeler; yeni bir marka veya font seçimi değildir. Web için ölçü ve font otoritesi ../docs/assets/site.css ile ../docs/assets/fonts/fonts.css; işaret otoritesi ../docs/assets/mutalaa-mark.svg; raster malzeme referansı ../docs/assets/social/mutalaa-mcp-tanitim.png dosyasıdır. PRODUCT.md içindeki Mütalaa adı, m. işareti ve Türkçe dil taahhüdü korunur.

**Key Characteristics:**

- Krem ve lacivert yüzeyler üzerinde kontrollü pirinç vurgular.
- Büyük serif başlıklar, sade sans serif açıklamalar.
- İnce sınırlar, yumuşak gölgeler ve ölçülü köşe yuvarlaklığı.

## Colors

Renk tokenları kaynak CSS adlarını korur; kesin değerlerin otoritesi ön bilgideki tokenlardır.

- **Primary:** `brass`, marka noktası ve vurgular; `brass-soft`, küçük durum noktası; `brass-surface`, numara ve kaynak rozetlerinin açık zemini; `brass-deep`, açık zemin üzerinde küçük pirinç metin (eyebrow, kart numarası, SSS işareti) için kontrastı karşılayan koyu pirinç.
- **Neutral:** `paper`, sayfa zemini; `surface`, beyaz kartlar; `ink`, ana metin ve birincil eylemler; `slate`, açıklamalar ve ikincil notlar; `mist`, metin dışı soluk ayrıntılar için palet rengi; `muted`, bölüm ve soru zeminleri; `hairline`, sınırlar.
- **Dark surfaces:** `ink-bg`, koyu panel; `on-dark`, açık metin; `ink-hover`, koyu eylemlerin üzerine gelme durumu.
- **Status:** `ok`, mevcut araştırma önizlemesinin küçük yerel durum noktasıdır.

**The Pirinç Vurgu Rule.** Pirinç; marka noktası, ince bağlantılar, odak halkaları ve küçük vurguların rengidir. Uzun gövde metinlerinin mevcut ana rengi mürekkeptir.

## Typography

**Display Font:** Newsreader; Georgia ve Times New Roman serif yedekleri. **Body Font:** Helvetica Neue; Helvetica, Arial ve mevcut sistem sans serif yedekleri. **Label/Mono Font:** Kaynak numaralarında mevcut sistem monospace yığını.

Ön bilgideki `display` ana başlığı, `headline` bölüm başlığını, `title` fayda kartı başlığını, `body` paragrafı, `button` eylemi, `label` kaynak türü etiketini temsil eder. Başlıklar sıkı, gövde metni daha rahat satır aralığı kullanır. Ana başlık genişliği (12.5ch), bölüm başlığı genişliği (18ch); açıklama blokları bileşene göre değişir.

Webin (720px) ve altındaki başlık boyutları ana başlık için `clamp(2.8rem, 14vw, 4.5rem)`, bölüm başlığı için `clamp(2.15rem, 10vw, 3.2rem)` olur. Bu ölçüler rasterlara doğrudan uygulanmaz. Newsreader normal ve italik yüzleri mevcut yerel `woff2` dosyalarından yüklenir; bu çalışma font eklemedi.

**The Başlık ve Gövde Rule.** Web başlıklarında Newsreader, açıklama ve etkileşim metinlerinde mevcut Helvetica yığını kullanılır. Raster referansların serif karakteri korunur; piksel görselinden kesin font ailesi veya CSS ölçüsü türetilmez.

## Layout

Web kabuğu en çok (74rem), geniş ekranda toplam (3rem), (720px) altında toplam (2rem) yatay boşluk bırakır. Büyük başlık ve araştırma önizlemesi iki sütunla başlar; ana içerik ızgaraları (960px) altında tek sütuna iner. Fayda kartları (720px) altında tek sütuna; eylemler (520px) altında tam genişlikli dikey düzene geçer. Bölüm aralığı `clamp(4.5rem, 8vw, 7.5rem)`, (520px) altında (4.25rem) olur.

Aralık tokenları tekrar eden gerçek CSS değerlerini adlandırır; yeni bir matematiksel ölçek tanımlamaz. Kampanya kompozisyonu ve kanal boşlukları `.impeccable/surfaces/marketing-instagram-2026-09.md` kapsamındadır; bu dosya bunları genel site kurallarına dönüştürmez.

## Elevation & Depth

Yüzeyler ince sınırlar ve kaynakta kullanılan hafif ve geniş gölgelerle ayrılır. Küçük kartlar ve ikincil eylem en hafif gölgeyi; araştırma önizlemesi ve koyu panel en geniş gölgeyi kullanır. Birincil eylemin kendi varsayılan ve üzerine gelme gölgeleri vardır. Kesin gölge, geçiş ve odak değerleri `.impeccable/design.json` içindedir.

**The Yumuşak Derinlik Rule.** Derinlik ince sınırlar, tonal yüzeyler ve dağınık gölgelerle kurulur. Kâğıt ve metalin fiziksel gölgeleri mevcut pazarlama dilinin parçasıdır.

## Shapes

Küçük eylemler `sm`, soru yüzeyi `md`, yanıt kartı ve metin alanı `lg`, büyük kartlar ve paneller `xl` köşelerini kullanır. Kaynak etiketleri kapsül, numara rozetleri dairedir. İnce çerçeveler mevcut dünya ile uyumludur. m. işaretindeki pirinç nokta ve serif harf geometrisi korunur.

## Components

- **Buttons:** Mürekkep zeminli birincil, beyaz ve ince çerçeveli ikincil, koyu panel için açık varyant. En az (3rem) yükseklik; üzerine gelmede (1px) yükselme ve (180ms) geçiş. Klavye odağı pirinç dış çizgiyle görünür; (520px) altında yükseklik en az (3.15rem).
- **Chips:** Kaynak türlerini ayıran kâğıt zeminli kapsüller. Statik etiketlerdir; seçili durum veya filtre davranışı tanımlanmaz.
- **Cards:** Beyaz fayda kartları ince çerçeve ve hafif gölge taşır; serif başlık ve sans serif paragraf eşleşir. Kaynak önizlemesinde ayrıca kâğıt renkli soru ve beyaz yanıt yüzeyi bulunur.
- **Inputs / Fields:** Mevcut kurulum metni salt okunur, dikey büyütülebilir bir metin alanıdır. En az (20rem) yüksekliğe, gövde fontuna, (1.6) satır aralığına ve pirinç odak çizgisine sahiptir. Hata veya devre dışı varyantı kaynakta tanımlı değildir.
- **Navigation:** Sade sans serif bağlantılar; üzerine gelmede mürekkep metin ve açık nötr zemin. Başlık yapışkandır ve buzlu kâğıt zeminiyle sayfanın üstünde kalır; çapa hedefleri `scroll-margin-top` ile başlığın altına girmez. (720px) altında tüm bağlantılar görünür kalır ve gezinme yatay taşabilir; birincil bağlantı sağa yaslanır. Örnekler mevcut davranışı kaydeder.

Bileşen ve durum kanıtı: `../docs/index.html`, `../docs/assets/site.css`. Sidecar yedi gerçek web bileşeninin bağımsız HTML/CSS örneklerini içerir. Web hareket azaltma tercihi kaydırmayı anlık yapar ve geçiş ile animasyon süresini (0.01ms) düzeyine indirir. Hareketli yüzeyde tek yazar anı vardır: araştırma önizlemesi sayfa açılışında (900ms) yumuşakça yerine oturur.

## Do's and Don'ts

### Do:

- Do mevcut m. işaretinin biçimini ve oranını koru; kaynak olarak ../docs/assets/mutalaa-mark.svg dosyasını kullan.
- Do web renklerini, font yığınlarını ve bileşen ölçülerini kaynak CSS ile eşleştir.
- Do Türkçe karakterleri, görseldeki metnin okunabilirliğini ve açık/koyu zemin ayrımını kontrol et.
- Do her kanalın yerleşim ve dışa aktarma kurallarını ilgili yüzey notunda tut.

### Don't:

- Don't bu kampanya uyarlamasını font veya marka değişikliği olarak yorumla.
- Don't raster piksel renklerini ve harf biçimlerini web tokenlarının yeni kesin değerleri olarak kaydet.
- Don't tek bir kampanyanın kompozisyonunu tüm siteye zorunlu şablon yap.
