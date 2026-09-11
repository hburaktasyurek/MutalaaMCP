# Mütalaa MCP

![Mütalaa MCP — Türk hukuk kaynakları, kullandığınız yapay zekâda. Ücretsiz, açık kaynak ve yerel çalışma.](docs/assets/social/mutalaa-mcp-tanitim.png)

**Kullandığınız yapay zekâ uygulamasından Türkiye’nin hukuk kaynaklarını araştırın.**
Ücretsiz, açık kaynaklı ve bilgisayarınızda çalışır.

[Kurulum rehberi](docs/client-setup/README.md) · [English](README.en.md) · [Mütalaa](https://mutalaa.tr/)

## Ne yapabilirsiniz?

- Mevzuat, yargı kararları ve Anayasa Mahkemesi kararları arayın.
- İlgili maddeyi veya belgeyi okuyun; resmî kaynak bağlantısını açın.
- “Kiracının aidat sorumluluğu nedir?” gibi gündelik sorularla araştırmaya başlayın.

## Yapay zekânla kur

Metni kopyalayıp kullandığınız yapay zekâya yapıştırın. Bilgisayarınıza erişebiliyorsa kurulumu yapar; erişemiyorsa adım adım yardımcı olur.

```text
Bilgisayarıma Mütalaa MCP’yi kurmama yardımcı ol.

Önce resmî kurulum rehberini oku:
https://github.com/hburaktasyurek/MutalaaMCP/blob/main/docs/client-setup/README.md
Beceri rehberi:
https://github.com/hburaktasyurek/MutalaaMCP/blob/main/docs/mutalaa-skill.md

İşletim sistemimi ve kullanacağım yapay zekâ uygulamasını bağlamdan belirle; bilmiyorsan sor. Rehbere erişemiyorsan bunu söyle ve içeriğini iste; komut veya destek bilgisi uydurma.

Bilgisayarımda komut çalıştırabiliyorsan mevcut kurulumu kontrol et ve rehberdeki uygun adımları uygula. Mevcut uygulama ayarlarımı ve diğer MCP bağlantılarımı koru. Komut çalıştıramıyorsan beni her seferinde tek, kısa bir adımla yönlendir; teknik bilgi bildiğimi varsayma. Uygulama yerel MCP desteklemiyorsa bu sınırlamayı açıkla.

Hesap girişi gerektiğinde beni ilgili ekrana yönlendir. Şifremi veya erişim belirteçlerimi sohbete yazmamı isteme. Destekleniyorsa Mütalaa becerisini de kur.

Son olarak gerçek bir Mütalaa araç çağrısıyla Gelir Vergisi Kanunu’nu bul ve kaynak bağlantısını getir. Web aramasını bu doğrulamanın yerine kullanma. Araçlara erişemiyorsan yeni sohbet veya uygulama adımında beni yönlendir; doğrulamanın beklediğini belirt.

Tamamlanan adımları ve benim yapmam gerekenleri kısaca belirt. Araç çağrısı başarılı olmadan kurulumun doğrulandığını söyleme.
```

## Elle kurulum

Bir **Mütalaa hesabı**, internet bağlantısı ve **yerel MCP bağlantısını destekleyen bir yapay zekâ uygulaması** gerekir.

1. [Kurulum rehberinden](docs/client-setup/README.md) bilgisayarınıza uygun adımları izleyin. Depoyu klonlamanız veya kod yazmanız gerekmez.
2. Uygulamanıza Mütalaa bağlantısını ekleyip hesabınızla giriş yapın.
3. Doğal hukuk sorularında araç kullanımını kolaylaştırmak için [Mütalaa becerisini](docs/mutalaa-skill.md) ekleyin.
4. Yeni bir sohbette şunu deneyin:

   > Mütalaa kullanarak 193 sayılı Gelir Vergisi Kanunu’nu bul ve resmî kaynak bağlantısını ver.

Araç çağrısında `mutalaamcp` görünüyorsa araştırma Mütalaa üzerinden yapılmıştır.
Yalnız web araması görünüyorsa [sorun giderme adımlarını](docs/client-setup/README.md#sorun-giderme) izleyin.

**Kurulum:** [Rehberdeki](docs/client-setup/README.md) terminal komutuyla belirli bir Release sürümü kurulur. macOS için imzasız yükleyici sunulmuyor; Windows CMD betiği deneysel.

## Verileriniz ve ücret

Yerel kullanım ücretsizdir; ticari kullanım kotası yoktur. Yapay zekâ uygulamanızın kendi ücretleri geçerli olabilir.
Araştırma ve önbellek bilgisayarınızda işlenir; kaynak istekleri ilgili resmî hizmetlere gider.
Mütalaa hesabı giriş ve etkinleştirme için kullanılır; hukuki sorgular ve belge metinleri etkinleştirme hizmetine gönderilmez.
Kullandığınız yapay zekâ uygulaması, sorunuz ve araç sonuçlarını kendi veri politikası kapsamında işler.

Mütalaa ürün duyuruları yanıtın sonunda ayrı bir bölümde görünebilir. [Duyuruların çalışma biçimi](docs/announcements.md).

## Geliştiriciler için

Sekiz araç, tek yerel sunucu: araştırma başlangıcı, mevzuat/karar/AYM araması, belge getirme,
madde getirme, mevzuat içinde arama ve madde ağacı. Girdiler ve yanıtlar [araç sözleşmesinde](contracts/tool-surface-v1.json) tanımlıdır.

- [Katkıda bulunma ve fork rehberi](CONTRIBUTING.md)
- [Hata bildirimi ve destek](SUPPORT.md)
- [Güvenlik açığı bildirme](SECURITY.md)

MIT lisanslıdır. [Lisans](LICENSE) · [Üçüncü taraf bildirimleri](THIRD_PARTY_NOTICES) · [Marka politikası](TRADEMARK_POLICY)

Mütalaa MCP hukuk araştırma yazılımıdır. Sonuçları birincil kaynaklardan doğrulayın; somut bir mesele için yetkin bir uzmana başvurun.
